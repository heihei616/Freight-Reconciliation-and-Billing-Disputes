"""能力包路由与系统提示词约束测试。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from logistics_agent import skills


class RouteTests(unittest.TestCase):
    """路由判断直接决定模型能看到哪些工具，判错等于越权。"""

    def test_pure_policy_questions_stay_read_only(self):
        questions = [
            "计费争议提出时限是多久",
            "哪些情况需要转人工",
            "交接需要留哪些记录",
            "货损赔付标准是多少",
            "试点验收指标是什么",
        ]
        for q in questions:
            with self.subTest(question=q):
                self.assertEqual(skills.route(q), "policy_qa")

    def test_bill_and_waybill_numbers_enter_dispute_flow(self):
        self.assertEqual(skills.route("核对 B202610-SF-001"), "freight_dispute")
        self.assertEqual(skills.route("YD2026100101 这票怎么回事"), "freight_dispute")
        self.assertEqual(skills.route("b202610-sf-001"), "freight_dispute")  # 小写也认

    def test_action_words_trigger_dispute_flow(self):
        self.assertEqual(skills.route("帮我把上个月的账对一下"), "freight_dispute")
        self.assertEqual(skills.route("这笔多收的钱能要回来吗"), "freight_dispute")

    def test_active_case_forces_dispute_flow(self):
        # 会话里已有校验凭证/草稿时，即使这句话像制度提问也不能降级
        self.assertEqual(skills.route("时限是多久", active_case=True), "freight_dispute")

    def test_carrier_and_period_without_policy_words(self):
        self.assertEqual(skills.route("核一下上个月顺丰的账"), "freight_dispute")

    def test_policy_word_blocks_period_heuristic(self):
        # 「上月计费规则」是在问规则，不是要对账
        self.assertEqual(skills.route("上个月顺丰的计费规则是什么"), "policy_qa")


class SkillDefinitionTests(unittest.TestCase):
    def test_policy_skill_has_no_write_tools(self):
        read_only = set(skills.SKILLS["policy_qa"]["tools"])
        self.assertNotIn("create_dispute_draft", read_only)
        self.assertNotIn("get_quota", read_only)

    def test_dispute_skill_contains_full_chain(self):
        chain = set(skills.SKILLS["freight_dispute"]["tools"])
        for required in ["search_policy", "get_bill_lines", "calc_expected_freight",
                         "validate_invoice", "create_dispute_draft"]:
            self.assertIn(required, chain)


class PromptTests(unittest.TestCase):
    """提示词里的硬约束是最后一道防线，不能被改没。"""

    def _prompt(self, state=None):
        return skills.build_system_prompt("freight_dispute", state or {})

    def test_prompt_contains_core_forbidden_rules(self):
        prompt = self._prompt()
        self.assertIn("不得编造", prompt)
        self.assertIn("没有提交权限", prompt)
        self.assertIn("制度中未找到", prompt)

    def test_prompt_forbids_inner_monologue(self):
        prompt = self._prompt()
        self.assertIn("只写要说给用户的话", prompt)

    def test_state_is_injected_into_prompt(self):
        prompt = self._prompt({
            "state": "validated", "bill_no": "B202610-SF-001",
            "verdict": "fail", "total_delta": 4169.61,
        })
        self.assertIn("B202610-SF-001", prompt)
        self.assertIn("validated", prompt)
        self.assertIn("fail", prompt)

    def test_unknown_skill_falls_back_to_policy(self):
        prompt = skills.build_system_prompt("not_a_skill", {})
        self.assertIn("policy_qa", prompt)

    def test_tone_rules_present(self):
        prompt = self._prompt()
        self.assertIn("说话方式", prompt)


if __name__ == "__main__":
    unittest.main()
