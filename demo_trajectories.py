"""
demo_trajectories.py —— 三个典型对话的轨迹回放

为什么要有这份脚本
------------------
需求里给了三条「预期轨迹」。预期不能只写在文档里——那样没法证明它真的发生。
所以这里直接把三个问题丢进 Harness 跑一遍，把「第几轮、调了什么工具、多久、
结果摘要」打印出来，并校验「不该调的工具一次都没调」。

运行：
    python demo_trajectories.py            # 跑三条，真实模型
    python demo_trajectories.py a          # 只跑示例 a

建单前的中断会被自动点「确认」——脚本扮演的就是员工那只手。
真实前端里这一步是人点的，服务端行为完全一样。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# 这个脚本独立于 app.py 运行，必须自己把 .env 读进来，
# 否则 llm 会因为找不到 LLM_API_KEY 退回离线模拟，轨迹就全成了假的。
load_dotenv(Path(__file__).parent / ".env", override=False)

import agent
import db
from rag_service import kb_service

# 每条用例：编号、意图说明、问题、期望 skill、禁止出现的工具
CASES: list[dict[str, Any]] = [
    {
        "key": "a",
        "intent": "只查计费规则",
        "question": "华东速运 上海→南京 的体积重计费标准和阶梯价是多少",
        "skill": "policy_qa",
        "forbidden": ["get_dispute_quota", "request_evidence",
                      "validate_invoice", "create_dispute_draft"],
        "note": "查制度 + 查价目，直接作答；不查额度、不要凭证、不校验、不建草稿",
    },
    {
        "key": "b",
        "intent": "整月争议核对（含建单）",
        "question": "帮我核上个月华东速运的争议运费",
        "skill": "freight_dispute",
        "forbidden": [],
        "note": "查制度 → 查额度 → 要凭证 → 校验 → 建草稿（人工确认后）→ 作答",
    },
    {
        "key": "c",
        "intent": "只校验单张单据",
        "question": "这张称重单 CD2026090203 能计入运费吗",
        "skill": "policy_qa",
        "forbidden": ["get_dispute_quota", "request_evidence",
                      "create_dispute_draft", "validate_invoice"],
        "note": "只调 check_weight_ticket 给结论；不查制度、不查额度、不建草稿",
    },
]


async def run_case(case: dict[str, Any], user_id: str) -> dict[str, Any]:
    """跑一条用例，返回轨迹（逐轮事件）。"""
    session = await db.create_session(user_id, title=f"轨迹-{case['key']}")
    steps: list[dict[str, Any]] = []
    answer: list[str] = []
    confirms: list[dict[str, Any]] = []
    run_id: str | None = None

    round_seen: dict[str, int] = {}
    started = time.perf_counter()

    async for event in agent.run(
        user_id=user_id, session_id=session["id"],
        question=case["question"], history=[], mock=False,
    ):
        name, data = event["event"], event["data"]

        if name == "state":
            run_id = data.get("run_id")

        elif name == "tool_call":
            steps.append({
                "round": data.get("round"), "tool": data.get("name"),
                "title": data.get("title"), "args": data.get("args", {}),
                "duration_ms": None, "summary": "",
            })
            round_seen[data.get("name")] = data.get("round")

        elif name == "tool_result":
            for item in reversed(steps):
                if item["tool"] == data.get("name") and item["summary"] == "":
                    item["duration_ms"] = data.get("duration_ms")
                    item["summary"] = data.get("summary")
                    item["ok"] = data.get("ok")
                    break

        elif name == "delta":
            answer.append(data.get("text", ""))

        elif name == "confirm_open":
            # 脚本扮演员工：看一眼草稿，2.5 秒后点确认。
            # 别设成 1 秒——正文流式推送本身就要一两秒，太短的延迟会让「等待时长」看起来是 0。
            confirms.append({"confirm_id": data.get("confirm_id"),
                             "amount": data.get("amount"),
                             "decision": "confirm"})
            asyncio.get_event_loop().call_later(
                2.5, agent.submit_confirmation,
                data.get("confirm_id"), user_id, "confirm", None,
            )

    elapsed = int((time.perf_counter() - started) * 1000)
    return {
        "case": case, "session_id": session["id"], "run_id": run_id,
        "steps": steps, "answer": "".join(answer), "confirms": confirms,
        "elapsed_ms": elapsed,
    }


def print_trajectory(result: dict[str, Any]) -> bool:
    """打印一条轨迹，并返回「禁止工具是否全部未出现」的校验结果。"""
    case = result["case"]
    print(f"\n{'=' * 72}")
    print(f"示例 {case['key']}：意图={case['intent']}")
    print(f"用户输入：{case['question']}")
    print(f"期望路径：{case['note']}")
    print(f"实际耗时：{result['elapsed_ms']} ms，run_id={result['run_id']}")

    if not result["steps"]:
        print("  ！本轮没有工具调用（模型直接作答）")
    for item in result["steps"]:
        args = ", ".join(f"{k}={v}" for k, v in item["args"].items())
        dur = f"{item['duration_ms']}ms" if item["duration_ms"] is not None else "-"
        mark = "✓" if item.get("ok", True) else "✗"
        print(f"  第{item['round']}轮 {mark} [{item['tool']}] {item['title']}  ({dur})")
        print(f"          参数：{args[:110]}")
        print(f"          结果：{item['summary']}")

    for confirm in result["confirms"]:
        print(f"  ⏸ 人工确认中断：金额 ¥{confirm['amount']}，脚本自动点了「确认」")

    used = {item["tool"] for item in result["steps"]}
    leftover = [name for name in case["forbidden"] if name in used]
    if leftover:
        print(f"  ✗ 违反约束：不该调用却调用了 {leftover}")
        return False
    if case["forbidden"]:
        print(f"  ✓ 约束满足：未调用 {', '.join(case['forbidden'])}")
    return True


async def main(only: str | None = None) -> int:
    await db.init_db()
    await kb_service.ensure_ready()

    user = await db.get_user_by_username("employee")
    if user is None:
        print("找不到演示账号 employee，请先初始化数据库")
        return 1
    user_id = user["id"]

    passed = 0
    total = 0
    for case in CASES:
        if only and case["key"] != only:
            continue
        total += 1
        result = await run_case(case, user_id)
        if print_trajectory(result):
            passed += 1
        print("\n  最终回答（前 220 字）：")
        print("  " + result["answer"][:220].replace("\n", "\n  "))

    print(f"\n{'=' * 72}")
    print(f"合计：{passed}/{total} 条轨迹满足约束")
    return 0 if passed == total else 2


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    raise SystemExit(asyncio.run(main(arg)))
