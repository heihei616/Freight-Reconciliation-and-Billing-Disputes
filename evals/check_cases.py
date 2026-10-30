"""
check_cases.py —— 评测用例自检

用途
----
`eval_cases.json` 是人工写的，手写数据最常见的错不是"想错了"，而是"抄错了"：
工具名少一个字母、运单号多一位、账单号写成了别的月份。
这类错误在跑评测时表现为「模型答错」，白白浪费一次模型调用和一次人工排查，
所以先用零成本的方式在校验层挡掉。

检查七件事
----------
1. JSON 结构：30 条、字段恰好 4 个且拼写正确；
2. 分类配比：制度 10 / 工具 10 / 违规 5 / 多轮 5；
3. 陷阱题：恰好 3 条，且都落在制度类；
4. 工具名：必须真实存在于 tools.SCHEMAS（防止编造或拼错）；
5. 单号：问题里出现的运单/账单/称重单号必须真实存在（防止写错一位数字）；
6. 期望轨迹：工具调用类不得留空，制度类只应检索；
7. 重复：题目不得完全重复。

运行
----
    python evals/check_cases.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# 允许从项目根目录直接运行：把根目录放进 sys.path 才能 import 业务模块。
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import domain          # noqa: E402  取真实单号做存在性校验
import tools           # noqa: E402  取真实工具名做白名单校验

CASES_PATH = Path(__file__).with_name("eval_cases.json")

EXPECTED_FIELDS = ("question", "expected_tools", "expected_behavior", "category")
EXPECTED_COUNTS = {"制度类": 10, "工具调用类": 10, "违规意图类": 5, "多轮上下文类": 5}
TRAP_MARK = "【陷阱题】"
TRAP_COUNT = 3

# 三类标识符：运单 YD+10 位、账单 B+6位-2字母-3位、称重单 CD+10 位
_WAYBILL_RE = re.compile(r"YD\d{10}")
_BILL_RE = re.compile(r"B\d{6}-[A-Z]{2}-\d{3}")
_TICKET_RE = re.compile(r"CD\d{10}")


def _fail(errors: list[str], message: str) -> None:
    errors.append(message)


def main() -> int:
    raw = CASES_PATH.read_text(encoding="utf-8")
    cases = json.loads(raw)
    errors: list[str] = []

    # ---- 1. 结构与数量 -------------------------------------------------
    if not isinstance(cases, list):
        print("顶层必须是 JSON 数组")
        return 1
    if len(cases) != 30:
        _fail(errors, f"用例总数应为 30，实际 {len(cases)}")

    seen_questions: dict[str, int] = {}
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            _fail(errors, f"第 {index} 条不是对象")
            continue
        if tuple(case.keys()) != EXPECTED_FIELDS:
            _fail(errors, f"第 {index} 条字段为 {list(case.keys())}，应为 {list(EXPECTED_FIELDS)}")
        for field in EXPECTED_FIELDS:
            value = case.get(field)
            if field == "expected_tools":
                if not isinstance(value, list):
                    _fail(errors, f"第 {index} 条 expected_tools 必须是数组")
            elif not isinstance(value, str) or not value.strip():
                _fail(errors, f"第 {index} 条 {field} 为空或类型不对")

        question = case.get("question", "")
        if question in seen_questions:
            _fail(errors, f"第 {index} 条与第 {seen_questions[question]} 条题目重复")
        seen_questions[question] = index

    # ---- 2. 分类配比 ---------------------------------------------------
    counts: dict[str, int] = {}
    for case in cases:
        category = case.get("category")
        counts[category] = counts.get(category, 0) + 1
    for category, expected in EXPECTED_COUNTS.items():
        actual = counts.get(category, 0)
        if actual != expected:
            _fail(errors, f"{category} 应为 {expected} 条，实际 {actual}")
    unknown = set(counts) - set(EXPECTED_COUNTS)
    if unknown:
        _fail(errors, f"出现未定义的分类：{sorted(unknown)}")

    # ---- 3. 陷阱题 -----------------------------------------------------
    traps = [c for c in cases if TRAP_MARK in c.get("expected_behavior", "")]
    if len(traps) != TRAP_COUNT:
        _fail(errors, f"陷阱题应为 {TRAP_COUNT} 条，实际 {len(traps)}")
    for trap in traps:
        if trap.get("category") != "制度类":
            _fail(errors, f"陷阱题必须落在制度类：{trap.get('question')[:20]}")
        if "未找到" not in trap.get("expected_behavior", ""):
            _fail(errors, f"陷阱题的 expected_behavior 必须写明「未找到」：{trap.get('question')[:20]}")

    # ---- 4~6. 逐条校验工具名 / 单号 / 轨迹 -----------------------------
    for index, case in enumerate(cases, start=1):
        category = case.get("category")
        case_tools = case.get("expected_tools") or []
        question = case.get("question", "")
        behavior = case.get("expected_behavior", "")

        for tool_name in case_tools:
            if tool_name not in tools.SCHEMAS:
                _fail(errors, f"第 {index} 条工具名不存在：{tool_name}")

        if category == "工具调用类" and not case_tools:
            _fail(errors, f"第 {index} 条工具调用类必须标注期望工具")
        # 制度类只应检索：允许追加只读佐证（如 get_rate_card），但绝不能进入核对/建单链路。
        if category == "制度类":
            if not case_tools or case_tools[0] != "search_policy":
                _fail(errors, f"第 {index} 条制度类应以 search_policy 起手，实际 {case_tools}")
            forbidden = {"validate_invoice", "create_dispute_draft", "calc_expected_freight"}
            hit = forbidden & set(case_tools)
            if hit:
                _fail(errors, f"第 {index} 条制度类不应出现 {sorted(hit)}")

        for waybill_no in _WAYBILL_RE.findall(question):
            if domain.get_waybill(waybill_no) is None:
                _fail(errors, f"第 {index} 条运单号不存在：{waybill_no}")
        for bill_no in _BILL_RE.findall(question):
            if domain.get_bill(bill_no) is None:
                _fail(errors, f"第 {index} 条账单号不存在：{bill_no}")
        for ticket_no in _TICKET_RE.findall(question):
            if ticket_no not in domain.WEIGHT_TICKETS:
                _fail(errors, f"第 {index} 条称重单号不存在：{ticket_no}")

        # 违规类必须体现"拒绝/说明边界"，否则测不出合规指引
        if category == "违规意图类" and not any(
            word in behavior for word in ("拒绝", "说明", "不得")
        ):
            _fail(errors, f"第 {index} 条违规意图类必须写明拒绝或合规指引")

    # ---- 输出 ----------------------------------------------------------
    if errors:
        print(f"共 {len(errors)} 个问题：")
        for message in errors:
            print("  -", message)
        return 1

    print(f"通过：{len(cases)} 条用例，分类配比 {counts}，陷阱题 {len(traps)} 条")
    print("工具名、运单号、账单号、称重单号均已在业务代码中核实存在")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
