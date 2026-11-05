"""核心业务规则的离线回归测试，不访问数据库、网络或 LLM。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from logistics_agent.core import domain


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


class FreightBoundaryTests(unittest.TestCase):
    """边界与真值回归：金额一旦算错，后面的校验结论就全错。"""

    def test_unknown_waybill_returns_none(self):
        self.assertIsNone(domain.get_waybill("YD0000000000"))

    def test_unknown_bill_returns_none(self):
        self.assertIsNone(domain.get_bill("B000000-XX-000"))

    def test_rate_card_picked_by_date(self):
        # 价目有版本，取错版本 = 取错单价
        rate = domain.pick_rate_card("顺丰速运", "上海-成都", "2026-10-15")
        self.assertIsNotNone(rate)
        self.assertEqual(rate["carrier"], "顺丰速运")

    def test_missing_rate_card_returns_none(self):
        self.assertIsNone(domain.pick_rate_card("不存在的承运商", "上海-北京", "2026-10-15"))

    def test_expected_freight_for_sample_waybill(self):
        waybill = domain.get_waybill("YD2026090201")
        rate = domain.pick_rate_card(waybill["carrier"], waybill["lane"], "2026-09-15")
        result = domain.compute_freight(waybill, rate)
        self.assertEqual(result["expected_amount"], 711.36)

    def test_compute_freight_exposes_derivation(self):
        # 推导过程必须可解释，否则员工无法向承运商说明差额来源
        waybill = domain.get_waybill("YD2026100104")
        rate = domain.pick_rate_card("顺丰速运", "上海-成都", "2026-10-15")
        result = domain.compute_freight(waybill, rate)
        for key in ["charge_weight", "weight_basis", "expected_amount"]:
            self.assertIn(key, result)

    def test_list_bills_can_filter_by_carrier(self):
        bills = domain.list_bills(carrier="顺丰速运")
        self.assertTrue(bills)
        self.assertTrue(all(b["carrier"] == "顺丰速运" for b in bills))

    def test_validate_result_has_evidence_fields(self):
        result = domain.validate_bill("B202610-SF-001")
        self.assertIn("issues", result)
        for issue in result["issues"]:
            self.assertIn("code", issue)
            self.assertIn("delta", issue)

    def test_quota_lookup(self):
        quota = domain.get_quota("顺丰速运", "2026-10")
        self.assertIsNotNone(quota)

    def test_weight_ticket_reason_given_when_rejected(self):
        result = domain.check_weight_ticket("CD2026090203")
        self.assertFalse(result["admissible"])
        self.assertTrue(str(result))


if __name__ == "__main__":
    unittest.main()
