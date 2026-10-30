"""
check_gates.py —— 闸门自检：不经过大模型，直接压工具层

为什么要单独有这份脚本
----------------------
「模型不会跳过校验」这件事不能靠读提示词来判断，必须能被机器验证。
所以这里绕过 LLM，按最坏情况模拟模型的操作顺序（包括故意跳步、篡改金额），
确认每一次越权都被服务端挡住。演示前跑一遍，答辩时也拿得出证据。

运行：python check_gates.py
"""

from __future__ import annotations

import asyncio
import sys

import db
import tools
from rag_service import kb_service

PASSED = 0
FAILED = 0


def check(title: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [通过] {title}")
    else:
        FAILED += 1
        print(f"  [失败] {title} {detail}")


async def call(ctx: dict, name: str, **args) -> dict:
    return await tools.execute(name, args, ctx)


def _reset_session(session_id: str) -> None:
    """
    清掉上一次自检留下的轨迹。

    前置条件是「回查本会话调用轨迹」判定的，如果上一轮跑过 get_waybill，
    这一轮的「跳过取运单直接算」就会被历史数据放行，自检变成假通过。
    所以每次开跑前先把本会话的痕迹抹干净——检查本身必须可重复。
    """
    conn = db._get_conn()
    with db._lock:
        for table in ("tool_calls", "validations", "drafts"):
            conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
        conn.commit()


async def main() -> int:
    await db.init_db()
    await kb_service.ensure_ready()   # search_policy 依赖真实索引，必须先加载
    session_id = "s_gate_check"
    _reset_session(session_id)
    ctx = {"session_id": session_id, "user_id": "u_gate", "step": 1}

    print("\n== 场景 1：跳过取运单直接算运费 ==")
    payload = await call(ctx, "calc_expected_freight", waybill_nos=["YD2026100101"])
    check("被拒绝且原因是前置条件", not payload["ok"]
          and payload["error"]["code"] == "PRECONDITION", str(payload))
    check("给了下一步提示", "get_waybill" in payload["error"].get("next_hint", ""))

    print("\n== 场景 2：跳过计算直接核对账单 ==")
    await call(ctx, "get_bill_lines", bill_no="B202610-SF-001")
    payload = await call(ctx, "validate_invoice", bill_no="B202610-SF-001",
                         citation_ids=["c_fake_0000"])
    check("被拒绝且原因是前置条件", not payload["ok"]
          and payload["error"]["code"] == "PRECONDITION", str(payload))

    print("\n== 场景 3：伪造制度引用编号 ==")
    for no in ("YD2026100101", "YD2026100102", "YD2026100104", "YD2026100106"):
        await call(ctx, "get_waybill", waybill_no=no)
    await call(ctx, "calc_expected_freight",
               waybill_nos=["YD2026100101", "YD2026100102", "YD2026100104", "YD2026100106"])
    payload = await call(ctx, "validate_invoice", bill_no="B202610-SF-001",
                         citation_ids=["c_0000000000"])
    check("伪造 citation_id 被拒绝", not payload["ok"]
          and payload["error"]["code"] == "PRECONDITION", str(payload))

    print("\n== 场景 4：正常核对多收账单 ==")
    search = await call(ctx, "search_policy", query="计费争议提出时限与责任划分")
    citations = [c["citation_id"] for c in search["data"]["citations"]]
    payload = await call(ctx, "validate_invoice", bill_no="B202610-SF-001",
                         citation_ids=citations[:1])
    check("核对完成", payload["ok"], str(payload))
    validation = payload["data"]
    check("结论由服务端判定为 fail", validation["verdict"] == "fail", str(validation.get("verdict")))
    check("识别出 3 处问题", len(validation["issues"]) == 3,
          f"实际 {len(validation['issues'])} 处")
    check("差额合计 4169.61", abs(validation["total_delta"] - 4169.61) < 0.01,
          str(validation["total_delta"]))

    print("\n== 场景 5：篡改争议金额 ==")
    payload = await call(ctx, "create_dispute_draft",
                         validation_id=validation["validation_id"],
                         dispute_type="计费差异", amount=100.0,
                         statement="测试")
    check("金额不符被拒绝", not payload["ok"]
          and payload["error"]["code"] == "AMOUNT_MISMATCH", str(payload))

    print("\n== 场景 6：合规起草 + 一票一用 ==")
    payload = await call(ctx, "create_dispute_draft",
                         validation_id=validation["validation_id"],
                         dispute_type="计费差异", amount=4169.61,
                         statement="体积重抛比按 5000 计、燃油按旧费率 8%、YD2026100104 重复计费",
                         evidence=["运单 YD2026100101 磅单", "合同价目 RC-SF-SHCT-2026Q3"])
    check("草稿创建成功", payload["ok"], str(payload))
    draft = payload["data"]
    check("状态是 draft 且标注需人工确认",
          draft["status"] == "draft" and draft["requires_human_confirmation"] is True)

    payload = await call(ctx, "create_dispute_draft",
                         validation_id=validation["validation_id"],
                         dispute_type="计费差异", amount=4169.61, statement="再来一张")
    check("同一凭证不能建第二张", not payload["ok"]
          and payload["error"]["code"] == "VALIDATION_CONSUMED", str(payload))

    print("\n== 场景 7：账单无误时不得建单 ==")
    ctx2 = {"session_id": session_id + "_2", "user_id": "u_gate", "step": 1}
    await call(ctx2, "get_bill_lines", bill_no="B202610-ZT-001")
    for no in ("YD2026100103", "YD2026100105"):
        await call(ctx2, "get_waybill", waybill_no=no)
    await call(ctx2, "calc_expected_freight", waybill_nos=["YD2026100103", "YD2026100105"])
    search2 = await call(ctx2, "search_policy", query="运费对账周期")
    payload = await call(ctx2, "validate_invoice", bill_no="B202610-ZT-001",
                         citation_ids=[search2["data"]["citations"][0]["citation_id"]])
    check("结论为 pass", payload["ok"] and payload["data"]["verdict"] == "pass", str(payload))
    payload = await call(ctx2, "create_dispute_draft",
                         validation_id=payload["data"]["validation_id"],
                         dispute_type="计费差异", amount=0.01, statement="强行建单")
    check("pass 结论被拒绝建单", not payload["ok"]
          and payload["error"]["code"] == "VERDICT_PASS", str(payload))

    print("\n== 场景 8：提交不在模型能力范围内 ==")
    check("工具集中没有 submit_dispute", "submit_dispute" not in tools.SCHEMAS)
    check("工具集中没有 submit 相关名字",
          not any("submit" in name for name in tools.SCHEMAS))

    print(f"\n结果：{PASSED} 项通过，{FAILED} 项失败")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
