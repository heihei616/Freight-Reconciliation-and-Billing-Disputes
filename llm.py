"""
llm.py —— 供应商无关的流式大模型调用

设计要点
--------
1. 供应商不写死：全部通过环境变量配置，走 OpenAI 兼容协议。
   只要对方兼容 /chat/completions + stream，就能直接切过去。
2. 支持 mock 流式：没有 API Key 时也能演示完整的打字机效果，
   方便答辩/评审时离线展示，不会因为没配密钥就白屏。
3. 提示词组装沿用阶段 1 的 build_messages()（带 [资料N] 引用编号），
   只在中间插入最近 6 轮历史，并做字符预算保护。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator, Sequence

import httpx

# 复用阶段 1 的提示词组装逻辑，保证 CLI 与 Web 的提问方式一致。
from reimburse_agent_mvp import build_messages

# 上下文字符预算：超出后从最早的历史开始丢弃，避免把上下文撑爆。
MAX_CONTEXT_CHARS = 3000


class LLMError(RuntimeError):
    """模型调用异常；message 直接用于中文错误提示。"""


def is_mock_mode() -> bool:
    """未配置密钥时自动降级为 mock，让界面仍然可演示。"""
    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    if provider == "mock":
        return True
    return provider == "openai" and not os.getenv("LLM_API_KEY", "").strip()


def provider_info() -> dict[str, Any]:
    """健康检查展示用，不含密钥本身。"""
    return {
        "provider": os.getenv("LLM_PROVIDER", "openai"),
        "model": os.getenv("LLM_MODEL", "gpt-4o-mini"),
        "base_url": os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        "mock": is_mock_mode(),
        "configured": bool(os.getenv("LLM_API_KEY", "").strip()),
    }


def compose_messages(question: str,
                     results: Sequence[dict[str, Any]],
                     history: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    """
    组装发给模型的消息列表。

    结构：[system 提示词] + [最近 6 轮历史] + [本次问题 + 检索资料]
      - system 与末条 user 直接复用阶段 1 的 build_messages()；
      - 历史只保留 user/assistant 两种角色，且受字符预算约束。
    """
    base = build_messages(question, results)
    system_msg, final_user_msg = base[0], base[1]

    # 历史按时间正序拼接；超预算时从最早的开始丢。
    history_msgs: list[dict[str, str]] = [
        {"role": m["role"], "content": m["content"]}
        for m in history
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]

    def total_chars(msgs: list[dict[str, str]]) -> int:
        return sum(len(m["content"]) for m in msgs)

    while history_msgs and total_chars(history_msgs) > MAX_CONTEXT_CHARS:
        history_msgs.pop(0)

    return [system_msg, *history_msgs, final_user_msg]


async def stream_chat(messages: Sequence[dict[str, str]],
                      mock: bool = False) -> AsyncIterator[str]:
    """
    流式生成回答，逐段 yield 文本增量。

    mock=True 或未配置密钥时走离线模拟，仍保持流式，前端体验一致。
    """
    if mock or is_mock_mode():
        async for piece in _stream_mock(messages):
            yield piece
        return

    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    if provider != "openai":
        raise LLMError(f"不支持的 LLM_PROVIDER：{provider}（可选 openai / mock）")

    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    timeout = float(os.getenv("LLM_TIMEOUT", "60"))

    payload = {
        "model": model,
        "messages": list(messages),
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0.1")),
        "stream": True,
    }
    # 通义千问等「思考型」模型默认会先输出推理过程，首字延迟明显变长。
    # 本场景是制度问答，不需要展示思考链，因此显式关闭。
    # 用开关控制而不是写死，换回不支持该参数的供应商时设 LLM_ENABLE_THINKING=1 即可。
    if os.getenv("LLM_ENABLE_THINKING", "1").lower() in ("0", "false", "no"):
        payload["enable_thinking"] = False
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{base_url}/chat/completions", json=payload, headers=headers
            ) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode("utf-8", "ignore")[:300]
                    raise LLMError(f"模型服务返回 {response.status_code}：{detail}")

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece
    except httpx.TimeoutException as exc:
        raise LLMError("模型服务响应超时，请稍后重试") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"模型服务连接失败：{exc}") from exc


async def _stream_mock(messages: Sequence[dict[str, str]]) -> AsyncIterator[str]:
    """
    离线模拟：根据检索到的资料拼一段可读回答，再按小块吐出。

    它不会假装自己是真实模型——开头就标明「离线模拟」，避免误导演示对象。
    """
    last = messages[-1] if messages else {"content": ""}
    user_content = last.get("content") or ""
    if last.get("role") == "tool":
        # 智能体循环里最后一条可能是工具结果，此时没有「资料」可展示
        user_content = ""
    # 从提示词里粗略提取资料片段，让模拟回答看起来与检索结果相关。
    preview = user_content.split("以下为检索到的制度资料：", 1)[-1].strip()
    preview = preview.replace("\n", " ")[:180] or "（离线模式未执行工具调用）"

    text = (
        "【离线模拟模式】当前未配置大模型密钥，以下内容是本地生成的占位回答，"
        "用于验证「检索 → 提示词 → 流式输出 → 引用展示」这条链路。\n\n"
        f"与你问题最相关的制度片段是：「{preview}」\n\n"
        "离线模式下不会执行工具调用，因此运单核对、争议起草等能力不可用。"
        "配置 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 后重启服务，即可得到真实模型回答。"
        "注意：检索与引用都是真实的，只有正文是模拟的。"
    )

    # 每 4 个字符吐一次，20ms 间隔，观感接近真实打字机。
    for i in range(0, len(text), 4):
        yield text[i:i + 4]
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# 智能体：带工具的流式调用
# ---------------------------------------------------------------------------

async def stream_with_tools(messages: Sequence[dict[str, Any]],
                            tools: Sequence[dict[str, Any]] | None = None,
                            mock: bool = False) -> AsyncIterator[dict[str, Any]]:
    """
    流式调用，同时解析文本增量与工具调用增量。

    产出的事件（dict）：
      {"type": "delta", "text": "..."}                 正文增量，可直接推给前端
      {"type": "tool_calls", "calls": [...]}           本轮要执行的工具（可能多个）
      {"type": "finish", "reason": "...", "calls": n}  收尾

    工具调用在 OpenAI 兼容协议里是**分片下发**的：id 在第一片，name 也可能分片，
    arguments 是一段一段的 JSON 字符串。这里按 index 累加，最后拼成完整对象——
    少累加一片，参数就会是半个 JSON，后面 json.loads 必崩。
    """
    if mock or is_mock_mode():
        async for piece in _stream_mock(messages):
            yield {"type": "delta", "text": piece}
        yield {"type": "finish", "reason": "stop", "calls": 0}
        return

    provider = os.getenv("LLM_PROVIDER", "openai").lower()
    if provider != "openai":
        raise LLMError(f"不支持的 LLM_PROVIDER：{provider}（可选 openai / mock）")

    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    timeout = float(os.getenv("LLM_TIMEOUT", "90"))

    payload: dict[str, Any] = {
        "model": model,
        "messages": list(messages),
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0.1")),
        "stream": True,
    }
    if tools:
        payload["tools"] = list(tools)
        payload["tool_choice"] = "auto"
    if os.getenv("LLM_ENABLE_THINKING", "1").lower() in ("0", "false", "no"):
        payload["enable_thinking"] = False

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    calls: dict[int, dict[str, Any]] = {}
    finish_reason = "stop"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{base_url}/chat/completions", json=payload, headers=headers
            ) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode("utf-8", "ignore")[:300]
                    raise LLMError(f"模型服务返回 {response.status_code}：{detail}")

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        yield {"type": "delta", "text": delta["content"]}
                    for chunk in delta.get("tool_calls") or []:
                        index = int(chunk.get("index", 0))
                        slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if chunk.get("id"):
                            slot["id"] = chunk["id"]
                        function = chunk.get("function") or {}
                        if function.get("name"):
                            slot["name"] += function["name"]
                        if function.get("arguments"):
                            slot["arguments"] += function["arguments"]
    except httpx.TimeoutException as exc:
        raise LLMError("模型服务响应超时，请稍后重试") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"模型服务连接失败：{exc}") from exc

    ordered = [calls[i] for i in sorted(calls)]
    if ordered:
        for position, call in enumerate(ordered, start=1):
            # 少数供应商不返回 id，补一个稳定的占位，tool 消息要靠它配对
            call["id"] = call["id"] or f"call_{position}"
        yield {"type": "tool_calls", "calls": ordered}
    yield {"type": "finish", "reason": finish_reason, "calls": len(ordered)}
