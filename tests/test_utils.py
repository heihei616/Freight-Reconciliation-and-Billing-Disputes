"""utils 纯函数测试：不依赖数据库、网络与环境变量。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from logistics_agent.utils import masking, text


class NormalizeTextTests(unittest.TestCase):
    def test_full_width_bill_no_becomes_half_width(self):
        # 用户从微信/Excel 复制来的单号常常是全角，不规范化就匹配不上正则
        self.assertEqual(text.normalize_text("Ｂ２０２６１０－ＳＦ－００１"), "B202610-SF-001")

    def test_collapses_repeated_spaces(self):
        self.assertEqual(text.normalize_text("a   b　　c"), "a b c")

    def test_removes_control_and_zero_width_chars(self):
        self.assertEqual(text.normalize_text("abc​def"), "abcdef")

    def test_keeps_inner_line_breaks(self):
        self.assertEqual(text.normalize_text("第一行\n第二行"), "第一行\n第二行")

    def test_empty_and_none_like_input(self):
        self.assertEqual(text.normalize_text(""), "")
        self.assertTrue(text.is_blank("   "))
        self.assertTrue(text.is_blank(None))
        self.assertFalse(text.is_blank("x"))


class TruncateTests(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(text.truncate("abc", 10), "abc")

    def test_long_text_gets_suffix(self):
        self.assertEqual(text.truncate("abcdefgh", 5), "abcd…")

    def test_limit_must_be_positive(self):
        with self.assertRaises(ValueError):
            text.truncate("abc", 0)

    def test_one_line_flattens_newlines(self):
        self.assertEqual(text.one_line("a\nb   c"), "a b c")


class RedactTests(unittest.TestCase):
    def test_waybill_masks_middle(self):
        self.assertEqual(text.redact_middle("YD2026100101"), "YD2***01")

    def test_short_value_fully_masked(self):
        self.assertEqual(text.redact_middle("abc"), "***")

    def test_negative_args_rejected(self):
        with self.assertRaises(ValueError):
            text.redact_middle("abcdef", head=-1)


class ExtractIdentifierTests(unittest.TestCase):
    def test_dedup_keeps_order_and_uppercases(self):
        found = text.extract_identifiers(
            "先看 yd2026100101，再看 YD2026100102，最后回到 YD2026100101",
            r"YD\d{10}",
        )
        self.assertEqual(found, ["YD2026100101", "YD2026100102"])

    def test_group_pattern_returns_group_content(self):
        found = text.extract_identifiers("B202610-SF-001", r"(B\d{6}-[A-Z]{2}-\d{3})")
        self.assertEqual(found, ["B202610-SF-001"])

    def test_no_match_returns_empty_list(self):
        self.assertEqual(text.extract_identifiers("没有单号", r"YD\d{10}"), [])


class MaskingTests(unittest.TestCase):
    def test_api_key_fully_removed(self):
        result = masking.mask_text("key=sk-abcd1234efgh5678")
        self.assertNotIn("sk-abcd1234efgh5678", result)
        self.assertIn("[REDACTED_API_KEY]", result)

    def test_jwt_removed(self):
        token = "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiMTIzIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c"
        self.assertNotIn(token, masking.mask_text(f"token: {token}"))

    def test_bearer_token_removed(self):
        self.assertNotIn("abcdef123456", masking.mask_text("Bearer abcdef123456"))

    def test_password_assignment_removed(self):
        result = masking.mask_text("password=MyPass123")
        self.assertNotIn("MyPass123", result)

    def test_phone_keeps_head_and_tail(self):
        self.assertEqual(masking.mask_text("拨打 13812345678"), "拨打 138****5678")

    def test_email_masks_local_part(self):
        result = masking.mask_text("zhangsan@example.com")
        self.assertNotIn("zhangsan@", result)
        self.assertIn("@example.com", result)

    def test_id_card_masked(self):
        self.assertNotIn("19900101", masking.mask_text("110105199001011234"))

    def test_amount_is_not_masked(self):
        # 审计必须保留金额，否则日志失去意义
        self.assertIn("4169.61", masking.mask_text("差额 4169.61 元"))

    def test_recursive_masking_covers_nested_structures(self):
        payload = {"args": {"statement": "联系 13812345678", "items": ["sk-abcd1234efgh"]}}
        result = masking.mask_value(payload)
        self.assertIn("138****5678", result["args"]["statement"])
        self.assertNotIn("sk-abcd1234efgh", result["args"]["items"][0])

    def test_non_string_scalars_pass_through(self):
        self.assertEqual(masking.mask_value({"n": 12, "b": True}), {"n": 12, "b": True})

    def test_scan_reports_categories_without_leaking_value(self):
        hits = masking.scan_for_secrets("sk-abcd1234efgh 手机 13812345678")
        self.assertIn("api_key", hits)
        self.assertIn("phone", hits)
        # 报告只给类别，绝不能回显原文，否则扫描日志本身成了泄露源
        self.assertNotIn("sk-abcd1234efgh", str(hits))

    def test_clean_text_has_no_hits(self):
        self.assertEqual(masking.scan_for_secrets("体积重按 6000 抛比折算"), [])

    def test_env_lookup_is_not_flagged(self):
        # api_key = os.getenv(...) 是正确写法，扫描器不该报警，
        # 否则真正的硬编码密钥会淹没在误报里。
        self.assertNotIn("credential_assignment",
                         masking.scan_for_secrets('api_key = os.getenv("LLM_API_KEY")'))

    def test_type_annotation_is_not_flagged(self):
        self.assertEqual(masking.scan_for_secrets("password: str) -> None:"), [])

    def test_real_assignment_is_flagged(self):
        hits = masking.scan_for_secrets('password = "hunter2real"')
        self.assertIn("credential_assignment", hits)


if __name__ == "__main__":
    unittest.main()
