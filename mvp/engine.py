"""轻量 MVP 的确定性业务内核：检索、账单核对、问答与人工确认。

金额和核对结论只来自主项目的 domain.py；本模块不做浮点运费推导，
也不调用模型或向量数据库，确保演示结果可重复、可测试。
"""

from __future__ import annotations

import re
import sys
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from logistics_agent.core import domain  # noqa: E402

POLICY_PATH = Path(__file__).with_name("policies.md")


class EngineError(Exception):
    """可直接映射为 HTTP 响应的业务错误。"""

    def __init__(self, message: str, status_code: int = 400, code: str = "BAD_REQUEST"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


_CLAUSE_HEADING = re.compile(r"^##\s+(P-\d{2})\s+(.+?)\s*$", re.MULTILINE)
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "P-01": ("体积重", "体积重量", "抛比", "实重", "实际重量", "计费重量", "泡货", "6000", "5000"),
    "P-02": ("阶梯", "单价", "基础运费", "最低收费", "最低一票", "落档", "价目"),
    "P-03": ("燃油", "附加费", "费率", "上一季度", "油价"),
    "P-04": ("重复计费", "重复", "核对", "逐行", "账单金额", "应收金额", "冲回"),
    "P-05": ("争议额度", "额度", "月度上限", "审批", "结算经理", "争议金额"),
    "P-06": ("证据", "凭证", "称重单", "签收回执", "签章", "盖章", "材料", "偏差"),
    "P-07": ("人工确认", "确认", "草稿", "争议单", "提交", "取消", "绕过"),
    "P-08": ("审计", "留痕", "差异解释", "计费明细", "复盘", "价目版本"),
}
_ISSUE_POLICY = {
    "WEIGHT_BASIS_MISMATCH": "P-01",
    "UNIT_PRICE_MISMATCH": "P-02",
    "TIER_MISMATCH": "P-02",
    "RATE_NOT_FOUND": "P-02",
    "FUEL_RATE_MISMATCH": "P-03",
    "DUPLICATE_CHARGE": "P-04",
    "WAYBILL_NOT_FOUND": "P-04",
    "AMOUNT_MISMATCH": "P-08",
}
_CODE_LABELS = {
    "WEIGHT_BASIS_MISMATCH": "计费重量口径",
    "UNIT_PRICE_MISMATCH": "合同单价",
    "TIER_MISMATCH": "阶梯档位",
    "RATE_NOT_FOUND": "价目缺失",
    "FUEL_RATE_MISMATCH": "燃油费率",
    "DUPLICATE_CHARGE": "重复计费",
    "WAYBILL_NOT_FOUND": "运单不存在",
    "AMOUNT_MISMATCH": "金额不符",
}
_DRAFTS: dict[str, dict[str, Any]] = {}
_DRAFT_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_bill_no(bill_no: str) -> str:
    value = (bill_no or "").strip().upper()
    if not value:
        raise EngineError("请提供账单号", 400, "EMPTY_BILL_NO")
    if len(value) > 40:
        raise EngineError("账单号过长", 400, "BAD_BILL_NO")
    return value


@lru_cache(maxsize=1)
def load_clauses() -> list[dict[str, str]]:
    """把 policies.md 解析为稳定的 P-01～P-08 条款。"""
    text = POLICY_PATH.read_text(encoding="utf-8")
    matches = list(_CLAUSE_HEADING.finditer(text))
    clauses: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        clauses.append({"id": match.group(1), "title": match.group(2), "text": body})
    ids = [item["id"] for item in clauses]
    expected = [f"P-{number:02d}" for number in range(1, 9)]
    if ids != expected or any(not item["text"] for item in clauses):
        raise RuntimeError("MVP 制度语料必须完整包含 P-01～P-08，且顺序稳定")
    return clauses


def _citation(clause_id: str) -> dict[str, str]:
    for clause in load_clauses():
        if clause["id"] == clause_id:
            return dict(clause)
    raise KeyError(clause_id)


def search(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """可解释的关键词检索；没有命中时返回空列表而不是编一个条款。"""
    question = (query or "").strip().lower()
    if not question:
        raise EngineError("问题不能为空", 400, "EMPTY_QUESTION")
    if len(question) > 500:
        raise EngineError("问题过长，请控制在 500 字以内", 400, "QUESTION_TOO_LONG")
    limit = max(1, min(int(limit), 5))
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for clause in load_clauses():
        score = 0
        for keyword in _KEYWORDS[clause["id"]]:
            if keyword.lower() in question:
                score += 2 if keyword in clause["title"] else 1
        if score:
            ranked.append((score, clause["id"], {**clause, "score": score}))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in ranked[:limit]]


def bills() -> list[dict[str, Any]]:
    return domain.list_bills()


def audit(bill_no: str) -> dict[str, Any]:
    """核对账单并补充制度引用与复算步骤，不重写任何金额逻辑。"""
    normalized = _clean_bill_no(bill_no)
    try:
        result = deepcopy(domain.validate_bill(normalized))
    except KeyError as exc:
        raise EngineError(f"账单不存在：{normalized}", 404, "BILL_NOT_FOUND") from exc
    bill = domain.get_bill(normalized)
    if bill is None:  # 防御性分支：validate_bill 已做同样的存在性校验
        raise EngineError(f"账单不存在：{normalized}", 404, "BILL_NOT_FOUND")

    details_by_waybill: dict[str, dict[str, Any]] = {}
    for row in result["rows"]:
        details_by_waybill.setdefault(row["waybill_no"], row["expected_detail"])

    for issue in result["issues"]:
        policy_id = _ISSUE_POLICY.get(issue["code"], "P-08")
        issue["policy"] = _citation(policy_id)
        issue["label"] = _CODE_LABELS.get(issue["code"], "金额不符")
        detail = details_by_waybill.get(issue["waybill_no"])
        issue["steps"] = list(detail["steps"]) if detail else [issue["explain"]]
        issue["rule_version"] = detail["rule_version"] if detail else None

    biggest = None
    if result["issues"]:
        biggest = max(result["issues"], key=lambda item: abs(item["delta"]))

    citation_ids: list[str] = []
    for issue in result["issues"]:
        clause_id = issue["policy"]["id"]
        if clause_id not in citation_ids:
            citation_ids.append(clause_id)
    if not citation_ids:
        citation_ids = ["P-04", "P-08"]

    verdict = result["verdict"]
    delta = result["total_delta"]
    if verdict == "fail":
        summary = f"发现 {len(result['issues'])} 处差异，合计多收 {delta:.2f} 元，建议发起争议。"
    elif verdict == "warn":
        summary = f"发现 {len(result['issues'])} 处少收差异，合计 {delta:.2f} 元，需人工复核。"
    else:
        summary = "逐行复算未发现金额差异，可以查看计算步骤与制度依据。"

    result.update({
        "issued_at": bill["issued_at"],
        "charged_total": bill["total_amount"],
        "expected_total": round(bill["total_amount"] - delta, 2),
        "biggest": biggest,
        "citations": [_citation(item) for item in citation_ids],
        "summary": summary,
        "verdict_label": {"fail": "核对不通过", "warn": "需人工复核", "pass": "核对通过"}[verdict],
    })
    return result


def answer(question: str, bill_no: str | None = None) -> dict[str, Any]:
    """按明确意图路由；账单事实优先于制度检索，未命中则明确说不知道。"""
    query = (question or "").strip()
    if not query:
        raise EngineError("问题不能为空", 400, "EMPTY_QUESTION")
    if len(query) > 500:
        raise EngineError("问题过长，请控制在 500 字以内", 400, "QUESTION_TOO_LONG")

    if bill_no and any(word in query for word in ("额度", "审批")):
        normalized = _clean_bill_no(bill_no)
        bill = domain.get_bill(normalized)
        if bill is None:
            raise EngineError(f"账单不存在：{normalized}", 404, "BILL_NOT_FOUND")
        quota = domain.get_quota(bill["carrier"], bill["period"])
        if quota is None:
            text = f"{bill['carrier']} {bill['period']} 尚未登记争议额度，请由结算经理确认。"
        else:
            text = (
                f"{bill['carrier']} {bill['period']} 的争议额度为 {quota['monthly_limit']:.2f} 元，"
                f"已使用 {quota['used']:.2f} 元，剩余 {quota['remaining']:.2f} 元。"
                f"{quota['rule']}。"
            )
        return {"answer": text, "citations": [_citation("P-05")], "intent": "quota"}

    if any(word in query for word in ("称重单", "签章", "盖章")):
        text = (
            f"有效称重单须早于提货、承运商与发货方签章齐全、与运单申报重量偏差不超过 "
            f"{domain.WEIGHT_TOLERANCE:.0%}，且关联运单已签收。"
        )
        evidence = None
        if bill_no:
            evidence = domain.check_evidence(_clean_bill_no(bill_no))
            if not evidence.get("found", False):
                raise EngineError(f"账单不存在：{bill_no}", 404, "BILL_NOT_FOUND")
            text += "当前账单" + ("标准证据已齐备。" if evidence["complete"] else "仍缺：" + "、".join(evidence["missing"]) + "。")
        return {"answer": text, "citations": [_citation("P-06")], "intent": "evidence", "evidence": evidence}

    difference_words = ("为什么", "多了", "差异", "差额", "核对", "多收", "多少钱")
    policy_words = ("体积", "抛比", "燃油", "阶梯", "最低收费", "制度", "条款")
    if bill_no and any(word in query for word in difference_words) and not any(
        word in query for word in policy_words
    ):
        result = audit(bill_no)
        if result["verdict"] == "pass":
            text = f"账单 {result['bill_no']} 逐行复算未发现差异，合计差额为 0.00 元。"
        else:
            biggest = result["biggest"]
            text = (
                f"账单 {result['bill_no']} 的合计差额为 {result['total_delta']:.2f} 元。"
                f"最大一项是第 {biggest['line_no']} 行的{biggest['label']}，"
                f"差额 {biggest['delta']:.2f} 元。{biggest['explain']}。"
            )
        return {
            "answer": text,
            "citations": result["citations"],
            "intent": "bill_audit",
            "audit": result,
        }

    matches = search(query)
    if not matches:
        return {
            "answer": "未找到与该问题直接相关的制度条款。请补充计费、燃油、证据或争议流程等关键词；我不会猜测未登记的规则。",
            "citations": [],
            "intent": "not_found",
        }
    top = matches[0]
    first_paragraph = top["text"].split("\n\n", 1)[0]
    return {"answer": first_paragraph, "citations": matches[:2], "intent": "policy"}


def create_draft(bill_no: str) -> dict[str, Any]:
    """只生成待确认草稿；金额不接受客户端传入。"""
    result = audit(bill_no)
    if result["verdict"] != "fail" or result["total_delta"] <= 0:
        raise EngineError("该账单没有多收差额，不能创建争议单", 400, "NO_OVERCHARGE")
    evidence = domain.check_evidence(result["bill_no"])
    evidence_text = (
        "标准证据已齐备"
        if evidence["complete"]
        else "提交前须补齐：" + "、".join(evidence["missing"])
    )
    lines = [
        f"争议对象：{result['carrier']} / {result['period']} / {result['bill_no']}",
        f"主张金额：{result['total_delta']:.2f} 元（来自服务端核对结果）",
        "差异明细：",
    ]
    for index, issue in enumerate(result["issues"], start=1):
        lines.append(
            f"{index}. 第 {issue['line_no']} 行 / {issue['waybill_no']}：{issue['explain']}"
            f"（差额 {issue['delta']:.2f} 元，依据 {issue['policy']['id']}）"
        )
    lines.extend([
        "制度依据：" + "、".join(item["id"] + " " + item["title"] for item in result["citations"]),
        "证据状态：" + evidence_text,
        "处理说明：确认后仅进入演示复核队列，不执行外部提交或财务过账。",
    ])
    draft_id = f"DR-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
    draft = {
        "draft_id": draft_id,
        "title": f"{result['carrier']}{result['period']}账单计费争议",
        "bill_no": result["bill_no"],
        "carrier": result["carrier"],
        "period": result["period"],
        "amount": result["total_delta"],
        "body": "\n".join(lines),
        "issues": deepcopy(result["issues"]),
        "citations": deepcopy(result["citations"]),
        "missing_evidence": evidence["missing"],
        "status": "pending",
        "created_at": _now(),
        "decided_at": None,
    }
    with _DRAFT_LOCK:
        _DRAFTS[draft_id] = draft
    return deepcopy(draft)


def list_drafts() -> list[dict[str, Any]]:
    with _DRAFT_LOCK:
        items = sorted(_DRAFTS.values(), key=lambda item: item["created_at"], reverse=True)
        return deepcopy(items)


def decide(draft_id: str, action: str) -> dict[str, Any]:
    normalized_action = (action or "").strip().lower()
    if normalized_action not in {"confirm", "cancel"}:
        raise EngineError("action 只能是 confirm 或 cancel", 400, "BAD_ACTION")
    normalized_id = (draft_id or "").strip().upper()
    with _DRAFT_LOCK:
        draft = _DRAFTS.get(normalized_id)
        if draft is None:
            raise EngineError(f"争议草稿不存在：{normalized_id}", 404, "DRAFT_NOT_FOUND")
        if draft["status"] != "pending":
            raise EngineError("草稿已确认或已取消，不能重复操作", 409, "DRAFT_CLOSED")
        draft["status"] = "confirmed" if normalized_action == "confirm" else "cancelled"
        draft["decided_at"] = _now()
        return deepcopy(draft)
