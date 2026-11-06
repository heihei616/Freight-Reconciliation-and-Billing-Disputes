"""提示词拼接测试：补充要求只能追加，不能改写硬性规则。

这组用例不碰数据库、不调模型，只验证字符串层面的契约：
1. 不配置补充要求时，提示词与原来完全一致（避免悄悄改变线上行为）；
2. 配置后追加在末尾，且必须带「冲突以硬性规则为准」的声明；
3. 只有空白字符等同于没配置。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "code/src"))

from logistics_agent import skills


class SupplementBlockTests(unittest.TestCase):
    def setUp(self):
        self.state = {"state": "idle"}

    def test_no_supplement_keeps_prompt_unchanged(self):
        base = skills.build_system_prompt("policy_qa", self.state)
        self.assertEqual(skills.build_system_prompt("policy_qa", self.state, ""), base)
        self.assertEqual(skills.build_system_prompt("policy_qa", self.state, None), base)
        self.assertNotIn("管理员补充要求", base)

    def test_supplement_appended_at_the_end(self):
        text = "- 称呼统一用「你」。"
        prompt = skills.build_system_prompt("policy_qa", self.state, text)
        self.assertTrue(prompt.endswith("补充要求也不能改变工具的前置条件与人工确认流程。\n"))
        self.assertIn(text, prompt)
        # 追加而不是替换：原有硬性规则必须还在
        self.assertIn("金额只能来自 calc_expected_freight", prompt)
        self.assertIn("你没有提交权限", prompt)

    def test_supplement_declares_precedence(self):
        prompt = skills.build_system_prompt("policy_qa", self.state, "- 随便写点")
        self.assertIn("不能覆盖上面的硬性规则", prompt)

    def test_whitespace_only_is_ignored(self):
        base = skills.build_system_prompt("policy_qa", self.state)
        prompt = skills.build_system_prompt("policy_qa", self.state, "   \n\t  ")
        self.assertEqual(prompt, base)

    def test_multiline_supplement_is_indented(self):
        prompt = skills.build_system_prompt("policy_qa", self.state, "第一行\n第二行")
        self.assertIn("  第一行\n  第二行", prompt)

    def test_unknown_skill_falls_back_to_policy(self):
        prompt = skills.build_system_prompt("not_a_skill", self.state, "- 测试")
        self.assertIn("制度问答", prompt)
        self.assertIn("- 测试", prompt)

    def test_max_chars_is_a_positive_limit(self):
        self.assertGreater(skills.MAX_SUPPLEMENT_CHARS, 0)
        self.assertLessEqual(skills.MAX_SUPPLEMENT_CHARS, 4000)


if __name__ == "__main__":
    unittest.main()
