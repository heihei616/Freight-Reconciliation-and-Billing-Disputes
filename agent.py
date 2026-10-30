"""
agent.py —— 智能体编排：状态注入 → 工具循环 → 事件流

一轮提问里发生的事情
--------------------
1. 从数据库推导「会话现在已经走到哪一步」（不靠模型自己记）；
2. 按问题路由到 skill，决定本轮模型能看见哪些工具；
3. 把状态与规则拼进 system，进入工具循环：
      模型说话 → 要工具 → 服务端执行 → 结果回灌 → 模型接着说
   最多 6 轮、单个工具同参数最多 2 次，防止死循环与刷调用；
4. 边执行边往前端推事件（步骤条、引用、草稿卡片都是这么来的）。

错误不会中断整轮：工具被闸门拒绝时，模型会拿到 next_hint 自己纠正，
这正是「会自主决策」与「按固定流程走」的区别。
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

import db
import llm
import skills
import tools

MAX_TOOL_CALLS = 16        # 单轮总调用上限（比步数更硬的一道闸）
MAX_SAME_CALL = 2          # 同一工具同参数最多重试几次

TOOL_TITLES = {
    "search_policy": "检索制度条款",
    "get_waybill": "核对运单事实",
    "get_bill_lines": "读取账单明细",
    "get_rate_card": "查询合同价目",
    "calc_expected_freight": "计算应收运费",
    "validate_invoice": "逐行核对账单",
    "create_dispute_draft": "起草争议单",
    "escalate_to_human": "转人工处理",
    "load_skill": "切换能力包",
}


# ---------------------------------------------------------------------------
# 状态推导：全部来自数据库里已有的痕迹，避免"模型记错了自己的进度"
# ---------------------------------------------------------------------------

async def derive_state(session_id: str) -> dict[str, Any]:
    """把工具轨迹 + 凭证 + 草稿折叠成一句「现在到哪一步了」。"""
    calls = await db.list_tool_calls(session_id, limit=200)

    bill_no: str | None = None
    waybills: list[str] = []
    computed: list[str] = []

    # 轨迹是按时间倒序返回的，先反转成正序，取最后一次成功的值
    for call in reversed(calls):
        result = call.get("result") or {}
        if not result.get("ok"):
            continue
        data = result.get("data") or {}
        if call["name"] == "get_bill_lines" and data.get("bill_no"):
            bill_no = data["bill_no"]
        elif call["name"] == "get_waybill" and data.get("waybill_no"):
            if data["waybill_no"] not in waybills:
                waybills.append(data["waybill_no"])
        elif call["name"] == "calc_expected_freight":
            for item in data.get("results", []):
                if item.get("waybill_no") and item["waybill_no"] not in computed:
                    computed.append(item["waybill_no"])

    validation = await db.latest_validation(session_id)
    draft = await db.latest_draft(session_id)

    if draft:
        state = "drafted"
    elif validation:
        state = "validated"
    elif computed:
        state = "computed"
    elif bill_no:
        state = "verified"
    elif waybills:
        state = "collecting"
    else:
        state = "idle"

    return {
        "state": state,
        "bill_no": bill_no,
        "waybills": waybills,
        "computed": computed,
        "verdict": validation["verdict"] if validation else None,
        "total_delta": validation["total_delta"] if validation else None,
        "validation_id": validation["id"] if validation else None,
        "draft_id": draft["id"] if draft else None,
        "draft_status": draft["status"] if draft else None,
    }


# ---------------------------------------------------------------------------
# 工具结果摘要：前端步骤条只显示一行，不能让用户读 JSON
# ---------------------------------------------------------------------------

def _summarize(name: str, payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        error = payload.get("error") or {}
        return f"{error.get('code', 'ERROR')}：{error.get('message', '执行失败')}"

    data = payload.get("data") or {}
    if name == "search_policy":
        return f"命中 {len(data.get('citations', []))} 条条款"
    if name == "get_waybill":
        return (f"{data.get('waybill_no')} · {data.get('carrier')} {data.get('lane')} · "
                f"实重 {data.get('weight_kg')}kg / 体积 {data.get('volume_m3')}m³")
    if name == "get_bill_lines":
        return f"{data.get('bill_no')} · {len(data.get('lines', []))} 行 · 合计 {data.get('total_amount')} 元"
    if name == "get_rate_card":
        rate = data.get("rate_card") or {}
        return f"{rate.get('version')} · {rate.get('price_per_kg')} 元/kg · 抛比 {rate.get('volume_ratio')}"
    if name == "calc_expected_freight":
        results = data.get("results", [])
        total = round(sum(r.get("expected_amount", 0) for r in results), 2)
        return f"{len(results)} 张运单，应收合计 {total} 元"
    if name == "validate_invoice":
        return (f"结论 {data.get('verdict')} · {len(data.get('issues', []))} 处问题 · "
                f"差额 {data.get('total_delta')} 元")
    if name == "create_dispute_draft":
        return f"草稿 {data.get('draft_id')} · 金额 {data.get('amount')} 元 · 待人工确认"
    if name == "escalate_to_human":
        return f"已登记人工介入：{data.get('reason')}"
    if name == "load_skill":
        return f"切换到 {data.get('title')}（{data.get('skill')}）"
    return "完成"


def _tool_content(payload: dict[str, Any]) -> str:
    """回灌给模型的工具消息：完整 JSON，模型才能看到 next_hint 自我纠正。"""
    return json.dumps(payload, ensure_ascii=False)


class ThinkingFilter:
    """
    过滤模型写在正文里的「[Thinking] … [回复]」段落。

    有些混合推理模型在开启工具调用时会把思考过程直接写进 content，
    用户在气泡里看到一大段自我盘算是很糟的体验。这里做流式过滤：
    标记可能被切成两半，所以未命中的尾巴要留在缓冲区里等下一片。

    放在服务端而不是前端：落库的内容也必须是干净的，
    否则审计页、导出记录里都会留着这段思维过程。
    """

    OPEN = "[Thinking]"
    CLOSE = "[回复]"

    def __init__(self) -> None:
        self._buffer = ""
        self._skipping = False

    def feed(self, text: str) -> str:
        self._buffer += text
        out: list[str] = []
        while True:
            if self._skipping:
                index = self._buffer.find(self.CLOSE)
                if index == -1:
                    # 丢掉绝大部分，只留可能被切断的半个结束标记
                    if len(self._buffer) > len(self.CLOSE):
                        self._buffer = self._buffer[-len(self.CLOSE):]
                    return "".join(out)
                self._buffer = self._buffer[index + len(self.CLOSE):]
                self._skipping = False
                continue

            index = self._buffer.find(self.OPEN)
            if index != -1:
                out.append(self._buffer[:index])
                self._buffer = self._buffer[index + len(self.OPEN):]
                self._skipping = True
                continue

            keep = len(self.OPEN) - 1
            if len(self._buffer) > keep:
                out.append(self._buffer[:-keep])
                self._buffer = self._buffer[-keep:]
            return "".join(out)

    def flush(self) -> str:
        """流结束时调用：正常状态下把残留吐出来，思考段则直接丢弃。"""
        rest = "" if self._skipping else self._buffer
        self._buffer = ""
        return rest


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

async def run(user_id: str, session_id: str, question: str,
              history: list[dict[str, Any]], mock: bool = False
              ) -> AsyncIterator[dict[str, Any]]:
    """
    执行一轮智能体对话，产出事件字典：

    state / status / delta / sources / tool_call / tool_result / draft / done / error
    """
    started = time.perf_counter()
    state = await derive_state(session_id)
    active_case = bool(state["validation_id"] or state["draft_id"])
    skill_name = skills.route(question, active_case=active_case)

    system_prompt = skills.build_system_prompt(skill_name, state)
    messages: list[dict[str, Any]] = (
        [{"role": "system", "content": system_prompt}]
        + [{"role": m["role"], "content": m["content"]}
           for m in history if m.get("role") in ("user", "assistant") and m.get("content")]
        + [{"role": "user", "content": question}]
    )

    yield {"event": "state", "data": {
        "skill": skill_name,
        "skill_title": skills.SKILLS[skill_name]["title"],
        "stage": state["state"],
        "tools": skills.SKILLS[skill_name]["tools"],
        "mock": mock or llm.is_mock_mode(),
    }}

    total_text: list[str] = []
    tool_call_count = 0
    call_fingerprints: dict[str, int] = {}
    seen_citations: dict[str, dict[str, Any]] = {}
    hit_limit = True     # 默认按「用尽轮次」处理，正常跳出时置 False
    thinking = ThinkingFilter()

    for step in range(1, tools.MAX_TOOL_STEPS + 1):
        yield {"event": "status", "data": {
            "stage": "thinking",
            "text": "正在分析问题…" if step == 1 else f"已获得新结果，继续分析（第 {step} 轮）",
        }}

        step_text: list[str] = []
        pending: list[dict[str, Any]] = []

        try:
            async for chunk in llm.stream_with_tools(
                messages, tools.openai_tools(skills.SKILLS[skill_name]["tools"]), mock=mock
            ):
                if chunk["type"] == "delta":
                    piece = thinking.feed(chunk["text"])
                    if piece:
                        step_text.append(piece)
                        total_text.append(piece)
                        yield {"event": "delta", "data": {"text": piece}}
                elif chunk["type"] == "tool_calls":
                    pending = chunk["calls"]
        except llm.LLMError as exc:
            yield {"event": "error", "data": {"code": 3002, "message": str(exc)}}
            return

        if not pending:
            hit_limit = False
            break

        # 把模型这一轮的「话 + 工具请求」记进上下文，否则下一轮它会重复要工具
        messages.append({
            "role": "assistant",
            "content": "".join(step_text) or None,
            "tool_calls": [
                {"id": call["id"], "type": "function",
                 "function": {"name": call["name"], "arguments": call["arguments"] or "{}"}}
                for call in pending
            ],
        })

        for call in pending:
            name = call["name"]
            title = TOOL_TITLES.get(name, name)

            # 参数是模型生成的 JSON 字符串，必须当作不可信输入解析
            try:
                args = json.loads(call["arguments"] or "{}")
                if not isinstance(args, dict):
                    raise ValueError("参数必须是 JSON 对象")
            except (json.JSONDecodeError, ValueError) as exc:
                payload = {"ok": False, "error": {
                    "code": "BAD_ARGS",
                    "message": f"工具参数不是合法 JSON：{exc}",
                    "next_hint": "请重新生成参数，确保是合法 JSON 对象",
                }}
                yield {"event": "tool_result", "data": {
                    "name": name, "title": title, "ok": False,
                    "summary": payload["error"]["message"],
                }}
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": _tool_content(payload)})
                continue

            fingerprint = name + json.dumps(args, ensure_ascii=False, sort_keys=True)
            call_fingerprints[fingerprint] = call_fingerprints.get(fingerprint, 0) + 1
            if call_fingerprints[fingerprint] > MAX_SAME_CALL:
                payload = {"ok": False, "error": {
                    "code": "TOOL_LOOP",
                    "message": "同样的调用已经重复多次，结果不会变化",
                    "next_hint": "请换一个思路：要么换参数，要么调用 escalate_to_human",
                }}
                yield {"event": "tool_result", "data": {
                    "name": name, "title": title, "ok": False, "summary": payload["error"]["message"],
                }}
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": _tool_content(payload)})
                continue

            tool_call_count += 1
            if tool_call_count > MAX_TOOL_CALLS:
                yield {"event": "error", "data": {
                    "code": 3004, "message": "本轮工具调用次数已达上限，请把问题拆小一些再问",
                }}
                return

            yield {"event": "tool_call", "data": {
                "name": name, "title": title, "args": args, "step": step,
            }}

            begin = time.perf_counter()
            # 注意：调用轨迹由 tools.execute 自己落库（谁执行谁记账），
            # 这里只负责计时与推事件，不重复写库。
            payload = await tools.execute(name, args, {
                "session_id": session_id, "user_id": user_id, "step": step,
            })
            duration_ms = int((time.perf_counter() - begin) * 1000)

            yield {"event": "tool_result", "data": {
                "name": name, "title": title, "ok": bool(payload.get("ok")),
                "summary": _summarize(name, payload), "duration_ms": duration_ms,
            }}

            # 检索结果顺手推给前端做「引用来源」，用户不必等正文就能看依据
            if name == "search_policy" and payload.get("ok"):
                for citation in payload["data"].get("citations", []):
                    seen_citations[citation["citation_id"]] = citation
                if seen_citations:
                    yield {"event": "sources", "data": {
                        "citations": list(seen_citations.values()),
                    }}

            # 草稿生成后单独推一张卡片，前端据此渲染「确认提交」按钮
            if name == "create_dispute_draft" and payload.get("ok"):
                draft = payload["data"]
                yield {"event": "draft", "data": {
                    "draft_id": draft.get("draft_id"),
                    "bill_no": draft.get("bill_no"),
                    "amount": draft.get("amount"),
                    "dispute_type": draft.get("dispute_type"),
                    "statement": draft.get("statement"),
                    "status": draft.get("status"),
                }}

            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": _tool_content(payload)})

            # 模型可以中途换能力包：换了就刷新工具白名单与系统提示
            if name == "load_skill" and payload.get("ok"):
                new_skill = payload["data"]["skill"]
                if new_skill != skill_name:
                    skill_name = new_skill
                    messages[0]["content"] = skills.build_system_prompt(skill_name, state)
                    yield {"event": "state", "data": {
                        "skill": skill_name,
                        "skill_title": skills.SKILLS[skill_name]["title"],
                        "stage": state["state"],
                        "tools": skills.SKILLS[skill_name]["tools"],
                    }}

    # 两种需要收尾的情况：
    #   1) 模型只调工具没说话（有些模型会这样收尾）；
    #   2) 轮次用尽（工具流程还没走完），此时若不补一次生成，
    #      用户会看到一段说到一半就断掉的话。
    need_summary = hit_limit or not "".join(total_text).strip()
    if need_summary and not (mock or llm.is_mock_mode()):
        hint = ("请基于以上工具结果，用中文给出结论、依据与下一步。"
                if not hit_limit else
                "本轮工具调用次数已达上限。请基于目前已获得的结果给出阶段性结论："
                "说明已经查到什么、还差哪一步、建议用户下一步怎么做，不要编造未取得的数据。")
        messages.append({"role": "user", "content": hint})
        try:
            async for chunk in llm.stream_with_tools(messages, tools=None, mock=False):
                if chunk["type"] == "delta":
                    piece = thinking.feed(chunk["text"])
                    if piece:
                        total_text.append(piece)
                        yield {"event": "delta", "data": {"text": piece}}
        except llm.LLMError as exc:
            yield {"event": "error", "data": {"code": 3002, "message": str(exc)}}
            return

    tail = thinking.flush()
    if tail.strip():
        total_text.append(tail)
        yield {"event": "delta", "data": {"text": tail}}

    final_state = await derive_state(session_id)
    yield {"event": "done", "data": {
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "tool_calls": tool_call_count,
        "skill": skill_name,
        "stage": final_state["state"],
        "citation_count": len(seen_citations),
        "draft_id": final_state["draft_id"],
    }}
