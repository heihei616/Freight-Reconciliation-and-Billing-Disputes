"""审计与日志保留测试。

关键点：这些用例跑在临时数据库上，不碰 data/runtime/chat.db。
做法是把 db 模块的全局连接与路径替换掉，测完再还原——
db.py 是懒加载单例，所以必须同时把 _conn 置空，否则会连到旧库。
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from logistics_agent import db


class TempDbTestCase(unittest.TestCase):
    """每个用例一个独立的库文件，避免相互污染。"""

    def setUp(self):
        self._orig_path = db.DB_PATH
        self._orig_conn = db._conn
        db._conn = None
        self._tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self._tmp.name) / "test.db"
        asyncio.run(db.init_db())
        user = asyncio.run(db.create_user("tester", "hash", "测试员", "employee"))
        self.user_id = user["id"]

    def tearDown(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = self._orig_conn
        db.DB_PATH = self._orig_path
        self._tmp.cleanup()

    def make_run(self, question: str, status: str = "done") -> str:
        """造一条执行记录，附带两个步骤。"""
        session = asyncio.run(db.create_session(self.user_id, question[:20]))
        run = asyncio.run(db.create_run(session["id"], self.user_id, question, "policy_qa"))
        asyncio.run(db.log_step(
            run["id"], session["id"], 1, "tool", "search_policy",
            {"query": "交接"}, 120, True, None, "命中 3 条制度", "先查制度",
        ))
        asyncio.run(db.log_step(
            run["id"], session["id"], 2, "tool", "calc_expected_freight",
            {"waybill_no": "YD2026100101"}, 80, False, "NOT_FOUND", "运单不存在", None,
        ))
        asyncio.run(db.finish_run(run["id"], status, 1200))
        return run["id"]


class AuditQueryTests(TempDbTestCase):
    def test_run_joins_user_display_name(self):
        run_id = self.make_run("核对 B202610-SF-001")
        run = asyncio.run(db.get_audit_run(run_id))
        self.assertIsNotNone(run)
        self.assertEqual(run["display_name"], "测试员")
        self.assertEqual(run["username"], "tester")
        # 写步骤日志时同步累加计数：两条 tool 步骤 → 轮次 2、工具 2 次
        self.assertEqual(run["rounds"], 2)
        self.assertEqual(run["tool_calls"], 2)
        self.assertEqual(run["confirm_count"], 0)

    def test_steps_are_ordered_by_round(self):
        run_id = self.make_run("核对账单")
        steps = asyncio.run(db.list_steps(run_id))
        self.assertEqual([s["round"] for s in steps], [1, 2])
        self.assertEqual(steps[0]["tool_name"], "search_policy")
        self.assertEqual(steps[0]["tool_args"], {"query": "交接"})
        self.assertEqual(steps[1]["error_code"], "NOT_FOUND")
        self.assertEqual(steps[1]["ok"], 0)

    def test_filter_by_status(self):
        self.make_run("第一个问题", status="done")
        self.make_run("第二个问题", status="error")
        done = asyncio.run(db.list_audit_runs(status="done"))
        error = asyncio.run(db.list_audit_runs(status="error"))
        self.assertEqual(len(done), 1)
        self.assertEqual(len(error), 1)
        self.assertEqual(error[0]["question"], "第二个问题")

    def test_filter_by_keyword_matches_question_and_id(self):
        run_id = self.make_run("核对 B202610-SF-001")
        self.make_run("制度里转人工怎么规定")
        hit = asyncio.run(db.list_audit_runs(keyword="B202610"))
        self.assertEqual(len(hit), 1)
        # 关键词也能命中 run_id，方便从日志里直接定位
        self.assertEqual(len(asyncio.run(db.list_audit_runs(keyword=run_id))), 1)
        self.assertEqual(len(asyncio.run(db.list_audit_runs(keyword="不存在的词"))), 0)

    def test_filter_by_user(self):
        self.make_run("某个问题")
        self.assertEqual(len(asyncio.run(db.list_audit_runs(user_id=self.user_id))), 1)
        self.assertEqual(len(asyncio.run(db.list_audit_runs(user_id="u_not_exist"))), 0)

    def test_days_filter_excludes_old_runs(self):
        self.make_run("新问题")
        # 手动把时间改到 2020 年，模拟历史数据
        conn = db._get_conn()
        conn.execute("UPDATE agent_runs SET started_at = '2020-01-01T00:00:00+08:00'")
        conn.commit()
        self.assertEqual(len(asyncio.run(db.list_audit_runs(days=7))), 0)
        self.assertEqual(len(asyncio.run(db.list_audit_runs(days=0))), 1)

    def test_count_matches_list(self):
        self.make_run("一")
        self.make_run("二")
        self.assertEqual(asyncio.run(db.count_audit_runs(days=7)), 2)
        self.assertEqual(asyncio.run(db.count_audit_runs(keyword="一")), 1)

    def test_stats_aggregates_tools_and_error_rate(self):
        self.make_run("成功的一次", status="done")
        self.make_run("失败的一次", status="error")
        stats = asyncio.run(db.audit_stats(days=7))
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["error_rate"], 50.0)
        by_name = {tool["tool_name"]: tool for tool in stats["tools"]}
        self.assertEqual(by_name["search_policy"]["calls"], 2)
        self.assertEqual(by_name["calc_expected_freight"]["failures"], 2)


class PurgeTests(TempDbTestCase):
    def test_dry_run_reports_without_deleting(self):
        self.make_run("待清理")
        conn = db._get_conn()
        conn.execute("UPDATE agent_runs SET started_at = '2020-01-01T00:00:00+08:00'")
        conn.execute("UPDATE agent_steps SET created_at = '2020-01-01T00:00:00+08:00'")
        conn.commit()

        result = asyncio.run(db.purge_logs(days=30, dry_run=True))
        self.assertEqual(result["deleted"]["agent_runs"], 1)
        self.assertEqual(result["deleted"]["agent_steps"], 2)
        # 预演不能真删
        self.assertEqual(len(asyncio.run(db.list_audit_runs(days=0))), 1)

    def test_real_purge_removes_only_old_rows(self):
        self.make_run("旧的")
        conn = db._get_conn()
        conn.execute(
            "UPDATE agent_runs SET started_at = '2020-01-01T00:00:00+08:00' "
            "WHERE question = '旧的'"
        )
        conn.execute(
            "UPDATE agent_steps SET created_at = '2020-01-01T00:00:00+08:00' "
            "WHERE run_id = (SELECT id FROM agent_runs WHERE question = '旧的')"
        )
        conn.commit()
        self.make_run("新的")

        result = asyncio.run(db.purge_logs(days=30))
        self.assertEqual(result["deleted"]["agent_runs"], 1)
        remaining = asyncio.run(db.list_audit_runs(days=0))
        self.assertEqual([r["question"] for r in remaining], ["新的"])

    def test_retention_settings_reads_env(self):
        original = os.environ.get("LOG_RETENTION_DAYS")
        try:
            os.environ["LOG_RETENTION_DAYS"] = "30"
            settings = asyncio.run(db.get_retention_settings())
            self.assertEqual(settings["days"], 30)
            self.assertTrue(settings["enabled"])

            os.environ["LOG_RETENTION_DAYS"] = "0"
            self.assertFalse(asyncio.run(db.get_retention_settings())["enabled"])

            os.environ["LOG_RETENTION_DAYS"] = "不是数字"
            self.assertEqual(asyncio.run(db.get_retention_settings())["days"], 90)
        finally:
            if original is None:
                os.environ.pop("LOG_RETENTION_DAYS", None)
            else:
                os.environ["LOG_RETENTION_DAYS"] = original


class PromptRevisionTests(TempDbTestCase):
    def test_save_and_list_revisions(self):
        asyncio.run(db.save_prompt_revision("第一版", "save", "admin"))
        asyncio.run(db.save_prompt_revision("第二版", "save", "admin"))

        revisions = asyncio.run(db.list_prompt_revisions(10))
        self.assertEqual(len(revisions), 2)
        # 倒序：最新的在前
        self.assertEqual(revisions[0]["preview"], "第二版")
        self.assertEqual(revisions[0]["operator"], "admin")
        # 列表不返回全文，只给摘要与字数
        self.assertNotIn("content", revisions[0])
        self.assertEqual(revisions[0]["length"], 3)

    def test_get_revision_returns_full_content(self):
        rev_id = asyncio.run(db.save_prompt_revision("完整内容", "save", "admin"))
        revision = asyncio.run(db.get_prompt_revision(rev_id))
        self.assertEqual(revision["content"], "完整内容")
        self.assertEqual(revision["action"], "save")

    def test_missing_revision_returns_none(self):
        self.assertIsNone(asyncio.run(db.get_prompt_revision("pr_not_exist")))


if __name__ == "__main__":
    unittest.main()
