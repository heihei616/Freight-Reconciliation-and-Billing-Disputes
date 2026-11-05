"""执行循环的预算约束测试。

不启动模型、不跑循环，只验证「闸门函数」本身：
预算算错会导致两种事故——放太松，模型反复换关键词查同一个不存在的问题；
收太紧，正常的多步核对被误杀。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from logistics_agent import agent


class ToolBudgetTests(unittest.TestCase):
    def test_search_budget_stops_after_limit(self):
        counts: dict[str, int] = {}
        allowed = 0
        while not agent.tool_budget_exceeded("search_policy", counts):
            allowed += 1
            counts["search_policy"] = counts.get("search_policy", 0) + 1
            self.assertLess(allowed, 10, "预算没有上限，会无限放行")
        self.assertEqual(allowed, agent.MAX_TOOL_BY_NAME["search_policy"])

    def test_other_tools_are_not_capped_by_name(self):
        self.assertFalse(agent.tool_budget_exceeded("get_waybill", {"get_waybill": 99}))
        self.assertFalse(agent.tool_budget_exceeded("calc_expected_freight", {}))

    def test_limits_are_positive_and_within_total(self):
        self.assertGreater(len(agent.MAX_TOOL_BY_NAME), 0)
        for name, limit in agent.MAX_TOOL_BY_NAME.items():
            with self.subTest(tool=name):
                self.assertGreater(limit, 0)
                # 单项预算不能超过总预算，否则这道闸永远不会触发
                self.assertLess(limit, agent.MAX_TOOL_CALLS)

    def test_fingerprint_limit_is_stricter_than_budget(self):
        # 同样参数重复 2 次就该拒，比「换关键词」宽松一点是合理的
        self.assertGreaterEqual(agent.MAX_SAME_CALL, 1)
        self.assertLessEqual(agent.MAX_SAME_CALL, agent.MAX_TOOL_CALLS)

    def test_confirm_timeout_is_bounded(self):
        # 太长会让 SSE 连接一直挂着，太短员工来不及看清金额
        self.assertGreaterEqual(agent.CONFIRM_TIMEOUT, 30)
        self.assertLessEqual(agent.CONFIRM_TIMEOUT, 600)


class PreambleFilterTests(unittest.TestCase):
    def setUp(self):
        self.filter = agent.PreambleFilter()

    def test_drops_english_planning_sentence(self):
        self.assertEqual(self.filter.feed("I'll look up the policy."), "")
        self.assertEqual(self.filter.feed(" Let me check the rate card."), "")

    def test_keeps_chinese_answer(self):
        text = "制度里没查到明确的时限规定。"
        self.assertEqual(self.filter.feed(text), text)

    def test_keeps_english_without_planning_verb(self):
        # 不是准备句就不能删，否则用户要求英文回答时会被吃掉
        text = "The rate card has 3 tiers."
        self.assertEqual(self.filter.feed(text), text)

    def test_keeps_mixed_sentence_with_chinese(self):
        text = "FAISS 是向量检索库。"
        self.assertEqual(self.filter.feed(text), text)

    def test_handles_chunk_split_across_feeds(self):
        # 流式分片会把一句话切开，不能在第一片就下结论
        self.assertEqual(self.filter.feed("I'll look"), "")
        self.assertEqual(self.filter.feed(" up the policy."), "")

    def test_releases_after_buffer_limit(self):
        # 一直没有句末标点时必须放行，不能让用户干等
        text = "A" * (agent.PreambleFilter.MAX_BUFFER + 10)
        self.assertEqual(self.filter.feed(text), text)

    def test_discard_drops_pending_buffer(self):
        self.assertEqual(self.filter.feed("I'll look"), "")   # 可能是准备句，先缓冲
        self.filter.discard()
        self.assertEqual(self.filter.flush(), "")

    def test_chinese_streams_immediately(self):
        # 正常中文不能等句号才显示，否则首字延迟会很明显
        self.assertEqual(self.filter.feed("制度里没有这条"), "制度里没有这条")

    def test_flush_drops_english_preamble_without_period(self):
        self.filter.feed("Let me check the rate card")
        self.assertEqual(self.filter.flush(), "")


if __name__ == "__main__":
    unittest.main()
