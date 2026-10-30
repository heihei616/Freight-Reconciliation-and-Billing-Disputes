"""核心业务规则的离线回归测试，不访问数据库、网络或 LLM。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from logistics_agent.core import domain
from logistics_agent.skills import route


class FreightDomainTests(unittest.TestCase):
    def test_volume_weight_freight(self):
        waybill = domain.get_waybill("YD2026100104")
        rate = domain.pick_rate_card("顺丰速运", "上海-成都", "2026-10-15")
        result = domain.compute_freight(waybill, rate)
        self.assertEqual(result["charge_weight"], 1000.0)
        self.assertEqual(result["weight_basis"], "体积重")
        self.assertEqual(result["expected_amount"], 3360.0)

    def test_sf_bill_finds_three_planted_errors(self):
        result = domain.validate_bill("B202610-SF-001")
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(result["total_delta"], 4169.61)
        self.assertEqual(
            {issue["code"] for issue in result["issues"]},
            {"WEIGHT_BASIS_MISMATCH", "FUEL_RATE_MISMATCH", "DUPLICATE_CHARGE"},
        )

    def test_correct_bill_passes(self):
        result = domain.validate_bill("B202610-ZT-001")
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["total_delta"], 0.0)

    def test_tier_mismatch(self):
        result = domain.validate_bill("B202609-HD-001")
        self.assertEqual(result["total_delta"], 540.8)
        self.assertEqual(result["issues"][0]["code"], "TIER_MISMATCH")

    def test_weight_ticket_admissibility(self):
        rejected = domain.check_weight_ticket("CD2026090203")
        accepted = domain.check_weight_ticket("CD2026090201")
        self.assertFalse(rejected["admissible"])
        self.assertTrue(accepted["admissible"])


class SkillRouteTests(unittest.TestCase):
    def test_policy_question_stays_read_only(self):
        self.assertEqual(route("计费争议提出时限是多久"), "policy_qa")

    def test_bill_number_enters_dispute_flow(self):
        self.assertEqual(route("核对 B202610-SF-001"), "freight_dispute")


if __name__ == "__main__":
    unittest.main()
