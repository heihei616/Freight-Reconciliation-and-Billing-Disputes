"""MVP 离线验收：不启动浏览器、不调用模型，直接锁定核心业务链路。

覆盖：制度知识库、确定性核对、Agent 框架（工具白名单/effect 分级/审核点）、
三处人工确认/审核点，以及完整 HTTP 链路。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "mvp"))

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from core import (  # noqa: E402
    AGENT,
    Effect,
    KnowledgeBase,
    answer,
    audit,
    confirm_evidence,
    create_draft,
    decide,
    load_clauses,
    review_audit,
    reviews,
    search,
)
from core import engine as engine_module  # noqa: E402
from core import EngineError  # noqa: E402


def _reset_state():
    """清空内存态，保证审核相关用例相互独立。"""
    engine_module._REVIEWS.clear()
    engine_module._DRAFTS.clear()


def _pass_review_gates(bill_no: str) -> None:
    """通过审核点①（差异复核）和②（证据审核）的辅助函数。"""
    review_audit(bill_no, "验收-复核员", "acknowledge")
    confirm_evidence(bill_no, "验收-证据审核员", "confirm")


# —— 制度知识库 ——

class KnowledgeBaseTests(unittest.TestCase):
    def test_loads_eight_clauses_in_order(self):
        kb = KnowledgeBase()
        self.assertEqual(kb.count(), 8)
        self.assertEqual(
            [c.id for c in kb.clauses()],
            [f"P-{number:02d}" for number in range(1, 9)],
        )

    def test_cite_returns_stable_clause(self):
        kb = KnowledgeBase()
        clause = kb.cite("P-05")
        self.assertEqual(clause.id, "P-05")
        self.assertIn("争议额度", clause.title)

    def test_search_returns_explainable_hits(self):
        kb = KnowledgeBase()
        hits = kb.search("体积重抛比怎么算")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["id"], "P-01")


# —— 制度语料（原有 PolicyTests）——

class PolicyTests(unittest.TestCase):
    def test_corpus_has_eight_unique_clauses(self):
        clauses = load_clauses()
        self.assertEqual(len(clauses), 8)
        self.assertEqual(
            {item["id"] for item in clauses},
            {f"P-{number:02d}" for number in range(1, 9)},
        )
        self.assertTrue(all(item["title"] and item["text"] for item in clauses))

    def test_volume_weight_query_cites_p01(self):
        results = search("体积重抛比怎么算")
        self.assertTrue(results)
        self.assertEqual(results[0]["id"], "P-01")

    def test_unrelated_query_does_not_invent_a_citation(self):
        self.assertEqual(search("员工年终奖如何计算"), [])


# —— 确定性核对（原有 AuditTests）——

class AuditTests(unittest.TestCase):
    def test_sf_bill_has_three_issues_and_exact_delta(self):
        result = audit("B202610-SF-001")
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(result["total_delta"], 4169.61)
        self.assertEqual(result["charged_total"], 16713.60)
        self.assertEqual(result["expected_total"], 12543.99)
        self.assertEqual(len(result["issues"]), 3)
        self.assertEqual(
            {item["code"] for item in result["issues"]},
            {"WEIGHT_BASIS_MISMATCH", "FUEL_RATE_MISMATCH", "DUPLICATE_CHARGE"},
        )
        self.assertEqual(result["biggest"]["code"], "DUPLICATE_CHARGE")
        self.assertEqual(result["biggest"]["delta"], 3360.00)
        self.assertEqual(result["biggest"]["policy"]["id"], "P-04")
        self.assertTrue(result["biggest"]["steps"])
        self.assertTrue(result["citations"])

    def test_every_issue_has_calculation_steps_and_policy(self):
        result = audit("B202610-SF-001")
        for issue in result["issues"]:
            self.assertTrue(issue["steps"], issue["code"])
            self.assertTrue(issue["policy"]["id"], issue["code"])
            self.assertTrue(issue["policy"]["text"], issue["code"])

    def test_passed_bill_has_zero_delta_and_policy_basis(self):
        result = audit("B202610-ZT-001")
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["total_delta"], 0.0)
        self.assertEqual(result["issues"], [])
        self.assertIsNone(result["biggest"])
        self.assertTrue(result["citations"])

    def test_tier_error_uses_the_real_biggest_issue(self):
        result = audit("B202609-HD-001")
        self.assertEqual(result["total_delta"], 540.80)
        self.assertEqual(result["biggest"]["code"], "TIER_MISMATCH")
        self.assertEqual(result["biggest"]["policy"]["id"], "P-02")

    def test_missing_bill_is_a_404(self):
        with self.assertRaises(EngineError) as caught:
            audit("B-NOT-FOUND")
        self.assertEqual(caught.exception.status_code, 404)


# —— 制度问答（原有 AnswerTests）——

class AnswerTests(unittest.TestCase):
    def test_difference_answer_reuses_audit_result(self):
        result = answer("为什么这张账单多了钱？", "B202610-SF-001")
        self.assertIn("4169.61", result["answer"])
        self.assertIn("重复计费", result["answer"])
        self.assertTrue(result["citations"])

    def test_quota_answer_uses_bill_carrier_and_period(self):
        result = answer("争议额度还剩多少？", "B202610-SF-001")
        self.assertIn("75830.39", result["answer"])
        self.assertEqual(result["citations"][0]["id"], "P-05")

    def test_unknown_answer_is_honest(self):
        result = answer("员工年终奖如何计算？")
        self.assertIn("未找到", result["answer"])
        self.assertEqual(result["citations"], [])


# —— Agent 框架 ——

class AgentTests(unittest.TestCase):
    def test_agent_exposes_read_and_review_tools(self):
        effects = {tool.effect for tool in AGENT.tools.values()}
        self.assertIn(Effect.READ, effects)
        self.assertIn(Effect.REVIEW, effects)

    def test_agent_has_at_least_three_review_points(self):
        points = AGENT.review_points()
        names = {item["name"] for item in points}
        self.assertGreaterEqual(len(points), 3)
        self.assertIn("review_audit", names)
        self.assertIn("confirm_evidence", names)
        self.assertIn("decide_draft", names)

    def test_agent_invokes_through_whitelist_and_audits(self):
        before = len(AGENT.trail)
        result = AGENT.invoke("audit_bill", bill_no="B202610-SF-001")
        self.assertEqual(result["total_delta"], 4169.61)
        self.assertEqual(len(AGENT.trail), before + 1)
        self.assertEqual(AGENT.trail[-1]["tool"], "audit_bill")
        self.assertEqual(AGENT.trail[-1]["effect"], "read")

    def test_agent_rejects_unknown_tool(self):
        with self.assertRaises(EngineError) as caught:
            AGENT.invoke("drop_database")
        self.assertEqual(caught.exception.code, "UNKNOWN_TOOL")

    def test_capability_lists_whitelist_and_review_points(self):
        capability = AGENT.capability()
        self.assertIn("tools", capability)
        self.assertIn("review_points", capability)
        self.assertGreaterEqual(len(capability["tools"]), 8)
        self.assertGreaterEqual(len(capability["review_points"]), 3)


# —— 三处人工确认/审核点 ——

class ReviewPointTests(unittest.TestCase):
    def setUp(self):
        _reset_state()

    def test_review_audit_acknowledges_difference(self):
        record = review_audit("B202610-SF-001", "复核员甲", "acknowledge")
        self.assertEqual(record["audit"]["decision"], "acknowledge")
        self.assertEqual(record["audit"]["reviewer"], "复核员甲")
        self.assertEqual(record["audit"]["issue_count"], 3)
        self.assertIsNotNone(reviews("B202610-SF-001"))

    def test_review_audit_reject_blocks_draft(self):
        review_audit("B202610-SF-001", "复核员甲", "reject")
        # reject 后既不能进入证据审核，也不能生成草稿
        with self.assertRaises(EngineError) as caught:
            confirm_evidence("B202610-SF-001", "证据审核员", "confirm")
        self.assertEqual(caught.exception.code, "REVIEW_REQUIRED")
        with self.assertRaises(EngineError) as caught:
            create_draft("B202610-SF-001")
        self.assertEqual(caught.exception.code, "REVIEW_REQUIRED")

    def test_evidence_review_requires_audit_review_first(self):
        with self.assertRaises(EngineError) as caught:
            confirm_evidence("B202610-SF-001", "证据审核员", "confirm")
        self.assertEqual(caught.exception.code, "REVIEW_REQUIRED")

    def test_create_draft_requires_both_review_gates(self):
        with self.assertRaises(EngineError) as caught:
            create_draft("B202610-SF-001")
        self.assertEqual(caught.exception.code, "REVIEW_REQUIRED")

    def test_full_review_chain_records_three_reviewers(self):
        # 审核点① 差异复核
        review_audit("B202610-SF-001", "复核员甲", "acknowledge")
        # 审核点② 证据审核
        confirm_evidence("B202610-SF-001", "证据审核员乙", "confirm")
        # 生成草稿（通过前置门）
        draft = create_draft("B202610-SF-001")
        self.assertEqual(draft["reviewed_by"], "复核员甲")
        self.assertEqual(draft["evidence_reviewed_by"], "证据审核员乙")
        # 审核点③ 争议终审
        decided = decide(draft["draft_id"], "confirm", "终审人丙")
        self.assertEqual(decided["status"], "confirmed")
        self.assertEqual(decided["reviewer"], "终审人丙")


# —— 争议草稿状态机（原有 DraftTests + 审核门）——

class DraftTests(unittest.TestCase):
    def setUp(self):
        _reset_state()

    def test_confirm_is_a_one_way_transition(self):
        _pass_review_gates("B202610-SF-001")
        draft = create_draft("B202610-SF-001")
        self.assertEqual(draft["status"], "pending")
        self.assertEqual(draft["amount"], 4169.61)
        self.assertIn("4169.61", draft["body"])
        confirmed = decide(draft["draft_id"], "confirm")
        self.assertEqual(confirmed["status"], "confirmed")
        with self.assertRaises(EngineError) as caught:
            decide(draft["draft_id"], "confirm")
        self.assertEqual(caught.exception.status_code, 409)

    def test_cancelled_draft_cannot_be_confirmed(self):
        _pass_review_gates("B202610-SF-001")
        draft = create_draft("B202610-SF-001")
        cancelled = decide(draft["draft_id"], "cancel")
        self.assertEqual(cancelled["status"], "cancelled")
        with self.assertRaises(EngineError) as caught:
            decide(draft["draft_id"], "confirm")
        self.assertEqual(caught.exception.status_code, 409)

    def test_passed_bill_cannot_create_draft(self):
        with self.assertRaises(EngineError) as caught:
            create_draft("B202610-ZT-001")
        self.assertEqual(caught.exception.code, "NO_OVERCHARGE")


# —— HTTP 链路 ——

class HttpTests(unittest.TestCase):
    def setUp(self):
        _reset_state()
        self.client = TestClient(app)

    def test_complete_http_flow(self):
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()["ok"])
        self.assertGreaterEqual(health.json()["review_points"], 3)

        bills = self.client.get("/api/bills")
        self.assertEqual(bills.status_code, 200)
        self.assertEqual(len(bills.json()["items"]), 3)

        checked = self.client.post("/api/audit", json={"bill_no": "B202610-SF-001"})
        self.assertEqual(checked.status_code, 200)
        self.assertEqual(checked.json()["total_delta"], 4169.61)

        answered = self.client.post(
            "/api/answer",
            json={"question": "为什么多了钱？", "bill_no": "B202610-SF-001"},
        )
        self.assertEqual(answered.status_code, 200)
        self.assertIn("4169.61", answered.json()["answer"])

        # 审核点① 差异复核
        reviewed = self.client.post(
            "/api/reviews/audit",
            json={"bill_no": "B202610-SF-001", "reviewer": "复核员", "decision": "acknowledge"},
        )
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.json()["audit"]["decision"], "acknowledge")

        # 审核点② 证据审核
        evidence = self.client.post(
            "/api/reviews/evidence",
            json={"bill_no": "B202610-SF-001", "reviewer": "证据审核员", "decision": "confirm"},
        )
        self.assertEqual(evidence.status_code, 200)

        created = self.client.post("/api/drafts", json={"bill_no": "B202610-SF-001"})
        self.assertEqual(created.status_code, 201)
        draft_id = created.json()["draft_id"]

        # 审核点③ 争议终审
        decided = self.client.post(
            f"/api/drafts/{draft_id}/decision",
            json={"action": "confirm", "reviewer": "终审人"},
        )
        self.assertEqual(decided.status_code, 200)
        self.assertEqual(decided.json()["status"], "confirmed")
        self.assertEqual(decided.json()["reviewer"], "终审人")

        repeated = self.client.post(
            f"/api/drafts/{draft_id}/decision", json={"action": "confirm"}
        )
        self.assertEqual(repeated.status_code, 409)
        self.assertIn("code", repeated.json())

        drafts = self.client.get("/api/drafts")
        self.assertTrue(
            any(item["draft_id"] == draft_id for item in drafts.json()["items"])
        )

    def test_agent_endpoint_reports_capability(self):
        response = self.client.get("/api/agent")
        self.assertEqual(response.status_code, 200)
        self.assertIn("review_points", response.json())
        self.assertGreaterEqual(len(response.json()["review_points"]), 3)

    def test_http_errors_are_structured(self):
        missing = self.client.post("/api/audit", json={"bill_no": "B-NOT-FOUND"})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["code"], "BILL_NOT_FOUND")

        homepage = self.client.get("/")
        self.assertEqual(homepage.status_code, 200)
        self.assertIn("text/html", homepage.headers["content-type"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
