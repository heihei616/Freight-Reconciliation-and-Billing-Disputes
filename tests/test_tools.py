"""工具层测试：Schema 完整性、建单预检、异常信封与权限闸门。

这些用例不调用 LLM，直接打工具层，
验证「模型想乱来也乱来不了」是由代码保证的，不是靠提示词。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from logistics_agent import tools
from logistics_agent.core import domain


class SchemaTests(unittest.TestCase):
    def test_every_schema_has_required_fields(self):
        for name, schema in tools.SCHEMAS.items():
            with self.subTest(tool=name):
                self.assertEqual(schema["name"], name)
                self.assertTrue(schema["description"].strip())
                self.assertEqual(schema["parameters"]["type"], "object")
                self.assertIn("properties", schema["parameters"])

    def test_openai_tools_wraps_function_type(self):
        payload = tools.openai_tools(["search_policy"])
        self.assertEqual(payload[0]["type"], "function")
        self.assertEqual(payload[0]["function"]["name"], "search_policy")

    def test_unknown_tool_is_silently_skipped(self):
        # 白名单之外的工具名不应进入发给模型的 tools 参数
        self.assertEqual(tools.openai_tools(["not_exist"]), [])

    def test_all_skill_tools_exist(self):
        from logistics_agent.skills import SKILLS
        for skill in SKILLS.values():
            for name in skill["tools"]:
                with self.subTest(skill=skill["name"], tool=name):
                    self.assertIn(name, tools.SCHEMAS)


class PrecheckTests(unittest.TestCase):
    """建单前的纯参数预检：不合法就不该浪费一次人工确认。"""

    def _base_args(self, **overrides):
        args = {
            "validation_id": "v_test",
            "dispute_type": domain.DISPUTE_TYPES[0] if hasattr(domain, "DISPUTE_TYPES")
                            else tools.DISPUTE_TYPES[0],
            "amount": 100.0,
            "statement": "第 1 行抛比用错，多收 100 元。",
            "evidence": [],
        }
        args.update(overrides)
        return args

    def test_non_draft_tool_is_not_prechecked(self):
        self.assertIsNone(tools.precheck("search_policy", {"query": "x"}))

    def test_valid_args_pass(self):
        self.assertIsNone(tools.precheck("create_dispute_draft", self._base_args()))

    def test_bad_dispute_type_rejected(self):
        result = tools.precheck("create_dispute_draft",
                                self._base_args(dispute_type="随便编的"))
        self.assertIsNotNone(result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "BAD_ARGS")
        self.assertIn("next_hint", result["error"])

    def test_overlong_statement_rejected(self):
        result = tools.precheck("create_dispute_draft",
                                self._base_args(statement="长" * (tools.STATEMENT_MAX + 1)))
        self.assertIsNotNone(result)
        self.assertEqual(result["error"]["code"], "BAD_ARGS")

    def test_missing_validation_id_rejected(self):
        result = tools.precheck("create_dispute_draft", self._base_args(validation_id=""))
        self.assertIsNotNone(result)


class ExecuteEnvelopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_tool_returns_recoverable_envelope(self):
        result = await tools.execute("no_such_tool", {}, {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNKNOWN_TOOL")
        self.assertIn("next_hint", result["error"])

    async def test_not_found_waybill_does_not_raise(self):
        # 单号不存在时必须返回错误信封，而不是把异常抛给上层变成 500
        result = await tools.execute("get_waybill", {"waybill_no": "YD0000000000"}, {})
        self.assertFalse(result["ok"])
        self.assertIn("code", result["error"])

    async def test_exception_inside_tool_becomes_envelope(self):
        async def boom(args, ctx):
            raise ValueError("模拟内部错误")

        tools._HANDLERS["_boom"] = boom
        try:
            result = await tools.execute("_boom", {}, {})
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "TOOL_ERROR")
        finally:
            tools._HANDLERS.pop("_boom", None)

    async def test_weight_ticket_check_is_read_only(self):
        result = await tools.execute("check_weight_ticket",
                                     {"ticket_no": "CD2026090203"}, {})
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["admissible"])


class GateTests(unittest.IsolatedAsyncioTestCase):
    """权限闸门：没有前置步骤，建单必须被拒。"""

    async def test_draft_without_validation_is_rejected(self):
        result = await tools.execute("create_dispute_draft", {
            "validation_id": "v_fake",
            "dispute_type": tools.DISPUTE_TYPES[0],
            "amount": 5000.0,
            "statement": "没有核对就想建单",
            "evidence": [],
        }, {})
        self.assertFalse(result["ok"])
        self.assertIn(result["error"]["code"], {"PRECONDITION", "BAD_ARGS", "NOT_FOUND"})

    async def test_calc_without_rate_card_is_rejected(self):
        result = await tools.execute("calc_expected_freight",
                                     {"waybill_no": "YD2026100101"}, {})
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
