"""领域层：确定性账单核对、制度问答、争议草稿与人工审核状态机。

金额与核对结论只来自主项目 domain.py；本模块不推导浮点运费，不调用模型或
向量库，确保演示结果可重复、可测试。

人工审核状态机（3 处人工确认/审核点）：
- 审核点① 差异复核 review_audit：核对发现差异后，由人工确认差异是否成立。
- 审核点② 证据审核 confirm_evidence：证据缺失时，由人工确认「已知缺证仍继续」或中止。
- 审核点③ 争议终审 decide：草稿最终确认/取消，记录终审人。

生成争议草稿 create_draft 要求先通过审核点①和②，否则返回 409，保证不可逆动作前有人工把关。
"""

from __future__ import annotations

import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from . import domain
from .knowledge import DEFAULT_POLICY_PATH, KnowledgeBase


class EngineError(Exception):
    """可直接映射为 HTTP 响应的业务错误。"""

    def __init__(self, message: str, status_code: int = 400, code: str = "BAD_REQUEST"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


# —— 知识库单例 ——
KNOWLEDGE = KnowledgeBase(DEFAULT_POLICY_PATH)

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
_REVIEWS: dict[str, dict[str, Any]] = {}
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_bill_no(bill_no: str) -> str:
    value = (bill_no or "").strip().upper()
    if not value:
        raise EngineError("请提供账单号", 400, "EMPTY_BILL_NO")
    if len(value) > 40:
        raise EngineError("账单号过长", 400, "BAD_BILL_NO")
    return value


def _clean_reviewer(reviewer: str | None, fallback: str) -> str:
    return (reviewer or "").strip() or fallback


def _clause_dict(clause) -> dict[str, str]:
    return {"id": clause.id, "title": clause.title, "text": clause.text}


def load_clauses() -> list[dict[str, str]]:
    """把 policies.md 解析为 P-01～P-08 条款（供健康检查与验收使用）。"""
    return [_clause_dict(clause) for clause in KNOWLEDGE.clauses()]


def search(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """可解释的关键词检索；未命中返回空列表而不是编一个条款。"""
    question = (query or "").strip().lower()
    if not question:
        raise EngineError("问题不能为空", 400, "EMPTY_QUESTION")
    if len(question) > 500:
        raise EngineError("问题过长，请控制在 500 字以内", 400, "QUESTION_TOO_LONG")
    return KNOWLEDGE.search(query, limit)


def _citation(clause_id: str) -> dict[str, str]:
    return _clause_dict(KNOWLEDGE.cite(clause_id))


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


# —— 人工审核状态机 ——

def reviews(bill_no: str) -> dict[str, Any] | None:
    """查询某账单的审核记录（审核点①差异复核、②证据审核）。"""
    normalized = _clean_bill_no(bill_no)
    with _LOCK:
        record = _REVIEWS.get(normalized)
        return deepcopy(record) if record else None


def list_reviews() -> list[dict[str, Any]]:
    """列出全部审核记录。"""
    with _LOCK:
        return deepcopy(sorted(_REVIEWS.values(), key=lambda item: item["bill_no"]))


def review_audit(bill_no: str, reviewer: str | None, decision: str) -> dict[str, Any]:
    """审核点①：人工复核「差异是否成立」。

    decision 仅接受 acknowledge（确认差异成立）或 reject（驳回）。
    只有 acknowledge 后才允许进入后续证据审核与争议草稿。
    """
    normalized = _clean_bill_no(bill_no)
    reviewer = _clean_reviewer(reviewer, "未署名审核人")
    decision = (decision or "").strip().lower()
    if decision not in {"acknowledge", "reject"}:
        raise EngineError("decision 只能是 acknowledge 或 reject", 400, "BAD_DECISION")
    result = audit(normalized)
    if result["verdict"] != "fail":
        raise EngineError("该账单没有多收差异，无需人工复核", 400, "NOTHING_TO_REVIEW")
    with _LOCK:
        record = _REVIEWS.setdefault(normalized, {"bill_no": normalized})
        record["audit"] = {
            "decision": decision,
            "reviewer": reviewer,
            "at": _now(),
            "delta": result["total_delta"],
            "issue_count": len(result["issues"]),
        }
        return deepcopy(record)


def confirm_evidence(bill_no: str, reviewer: str | None, decision: str) -> dict[str, Any]:
    """审核点②：人工审核「证据完整性」。

    decision 仅接受 confirm（已知缺证仍继续）或 abort（中止）。
    证据齐全时，confirm 表示人工确认证据齐备；缺失时 confirm 表示人工认可「缺证提交」。
    前置：必须先通过审核点①（差异已 acknowledge）。
    """
    normalized = _clean_bill_no(bill_no)
    reviewer = _clean_reviewer(reviewer, "未署名审核人")
    decision = (decision or "").strip().lower()
    if decision not in {"confirm", "abort"}:
        raise EngineError("decision 只能是 confirm 或 abort", 400, "BAD_DECISION")
    with _LOCK:
        record = _REVIEWS.get(normalized)
        audit_step = record.get("audit") if record else None
    if not audit_step or audit_step["decision"] != "acknowledge":
        raise EngineError("请先复核差异（审核点①）", 409, "REVIEW_REQUIRED")
    evidence = domain.check_evidence(normalized)
    if not evidence.get("found", False):
        raise EngineError(f"账单不存在：{normalized}", 404, "BILL_NOT_FOUND")
    with _LOCK:
        record = _REVIEWS.setdefault(normalized, {"bill_no": normalized})
        record["evidence"] = {
            "decision": decision,
            "reviewer": reviewer,
            "at": _now(),
            "complete": evidence["complete"],
            "missing": evidence["missing"],
        }
        return deepcopy(record)


def _require_reviews_passed(result: dict[str, Any]) -> dict[str, Any]:
    """create_draft 的前置门：必须已通过审核点①和②。"""
    with _LOCK:
        record = _REVIEWS.get(result["bill_no"])
    if not record or not record.get("audit") or record["audit"]["decision"] != "acknowledge":
        raise EngineError("请先复核差异（审核点①）", 409, "REVIEW_REQUIRED")
    if not record.get("evidence") or record["evidence"]["decision"] != "confirm":
        raise EngineError("请先审核证据（审核点②）", 409, "EVIDENCE_REVIEW_REQUIRED")
    return record


def create_draft(bill_no: str) -> dict[str, Any]:
    """只生成待确认草稿；金额不接受客户端传入，且必须先通过审核点①和②。"""
    result = audit(bill_no)
    if result["verdict"] != "fail" or result["total_delta"] <= 0:
        raise EngineError("该账单没有多收差额，不能创建争议单", 400, "NO_OVERCHARGE")
    review_record = _require_reviews_passed(result)
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
        "reviewed_by": review_record["audit"]["reviewer"],
        "evidence_reviewed_by": review_record["evidence"]["reviewer"],
    }
    with _LOCK:
        _DRAFTS[draft_id] = draft
    return deepcopy(draft)


def list_drafts() -> list[dict[str, Any]]:
    with _LOCK:
        items = sorted(_DRAFTS.values(), key=lambda item: item["created_at"], reverse=True)
        return deepcopy(items)


def decide(draft_id: str, action: str, reviewer: str | None = None) -> dict[str, Any]:
    """审核点③：争议终审（确认提交 / 取消），记录终审人。"""
    normalized_action = (action or "").strip().lower()
    if normalized_action not in {"confirm", "cancel"}:
        raise EngineError("action 只能是 confirm 或 cancel", 400, "BAD_ACTION")
    normalized_id = (draft_id or "").strip().upper()
    with _LOCK:
        draft = _DRAFTS.get(normalized_id)
        if draft is None:
            raise EngineError(f"争议草稿不存在：{normalized_id}", 404, "DRAFT_NOT_FOUND")
        if draft["status"] != "pending":
            raise EngineError("草稿已确认或已取消，不能重复操作", 409, "DRAFT_CLOSED")
        draft["status"] = "confirmed" if normalized_action == "confirm" else "cancelled"
        draft["decided_at"] = _now()
        draft["reviewer"] = _clean_reviewer(reviewer, "未署名终审人")
        return deepcopy(draft)
