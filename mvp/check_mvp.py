"""MVP 离线验收：不启动浏览器、不调用模型，直接锁定核心业务链路。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "mvp"))

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from engine import (  # noqa: E402
    EngineError,
    answer,
    audit,
    create_draft,
    decide,
    load_clauses,
    search,
)


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


class DraftTests(unittest.TestCase):
    def test_confirm_is_a_one_way_transition(self):
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


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_complete_http_flow(self):
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()["ok"])

        bills = self.client.get("/api/bills")
        self.assertEqual(bills.status_code, 200)
        self.assertEqual(len(bills.json()["items"]), 3)

        checked = self.client.post(
            "/api/audit", json={"bill_no": "B202610-SF-001"}
        )
        self.assertEqual(checked.status_code, 200)
        self.assertEqual(checked.json()["total_delta"], 4169.61)

        answered = self.client.post(
            "/api/answer",
            json={"question": "为什么多了钱？", "bill_no": "B202610-SF-001"},
        )
        self.assertEqual(answered.status_code, 200)
        self.assertIn("4169.61", answered.json()["answer"])

        created = self.client.post(
            "/api/drafts", json={"bill_no": "B202610-SF-001"}
        )
        self.assertEqual(created.status_code, 201)
        draft_id = created.json()["draft_id"]
        decided = self.client.post(
            f"/api/drafts/{draft_id}/decision", json={"action": "confirm"}
        )
        self.assertEqual(decided.status_code, 200)
        self.assertEqual(decided.json()["status"], "confirmed")

        repeated = self.client.post(
            f"/api/drafts/{draft_id}/decision", json={"action": "confirm"}
        )
        self.assertEqual(repeated.status_code, 409)
        self.assertIn("code", repeated.json())

        drafts = self.client.get("/api/drafts")
        self.assertTrue(
            any(item["draft_id"] == draft_id for item in drafts.json()["items"])
        )

    def test_http_errors_are_structured(self):
        missing = self.client.post("/api/audit", json={"bill_no": "B-NOT-FOUND"})
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["code"], "BILL_NOT_FOUND")

        homepage = self.client.get("/")
        self.assertEqual(homepage.status_code, 200)
        self.assertIn("text/html", homepage.headers["content-type"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
