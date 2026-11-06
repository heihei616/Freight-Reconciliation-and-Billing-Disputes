"""
人工确认测试：闸门状态从进程内存搬到 SQLite 之后，语义必须一条不少。

这组用例故意按「两个 worker 的视角」来写：
    A 创建确认 → B 点确认 → A 的等待者从库里读到终态。
如果哪天有人把状态改回全局字典，这些用例会在跨进程那几条上直接失败，
而不是等到部署两个 worker 之后才由员工发现「点确认报 409」。
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "code/src"))

from logistics_agent import agent, db


class ConfirmDbTestCase(unittest.TestCase):
    """每个用例一个临时库；同时清空 agent 的本地唤醒表。"""

    def setUp(self):
        self._orig_path = db.DB_PATH
        self._orig_conn = db._conn
        db._conn = None
        self._tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self._tmp.name) / "confirm.db"
        asyncio.run(db.init_db())

        self.user = asyncio.run(db.create_user("tester", "hash", "测试员", "employee"))
        self.user_id = self.user["id"]
        self.other = asyncio.run(db.create_user("other", "hash", "另一位", "employee"))
        self.session = asyncio.run(db.create_session(self.user_id, "确认测试"))

        agent._CONFIRMS.clear()
        self._orig_timeout = agent.CONFIRM_TIMEOUT
        agent.CONFIRM_TIMEOUT = 5

    def tearDown(self):
        agent.CONFIRM_TIMEOUT = self._orig_timeout
        agent._CONFIRMS.clear()
        if db._conn is not None:
            db._conn.close()
        db._conn = self._orig_conn
        db.DB_PATH = self._orig_path
        self._tmp.cleanup()

    # -- 小工具 ------------------------------------------------------------

    def new_confirm(self, cid: str = "cf_test_1", ttl: int = 60,
                    statement: str = "原始草稿正文") -> str:
        asyncio.run(db.create_pending_confirm(
            cid, "run_1", self.session["id"], self.user_id,
            "create_dispute_draft",
            {"bill_no": "B202610-SF-001", "amount": 4169.61, "statement": statement},
            ttl,
        ))
        return cid


class PendingLifecycleTests(ConfirmDbTestCase):
    def test_created_record_is_pending_and_args_roundtrip(self):
        cid = self.new_confirm()
        row = asyncio.run(db.get_pending_confirm(cid))
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["tool"], "create_dispute_draft")
        self.assertEqual(row["args"]["bill_no"], "B202610-SF-001")
        self.assertAlmostEqual(row["args"]["amount"], 4169.61)

    def test_confirm_moves_to_terminal_and_keeps_edited_statement(self):
        cid = self.new_confirm()
        ok, reason = asyncio.run(
            db.resolve_pending_confirm(cid, self.user_id, "confirm", "员工改过的正文"))
        self.assertTrue(ok, reason)

        row = asyncio.run(db.get_pending_confirm(cid))
        self.assertEqual(row["status"], "confirmed")
        self.assertEqual(row["statement"], "员工改过的正文")
        self.assertEqual(row["decided_by"], self.user_id)
        self.assertIsNotNone(row["decided_at"])

    def test_blank_statement_keeps_original(self):
        """不传正文表示「沿用模型原文」，不能把原文覆盖成空。"""
        cid = self.new_confirm(statement="模型生成的原文")
        ok, _ = asyncio.run(db.resolve_pending_confirm(cid, self.user_id, "confirm", None))
        self.assertTrue(ok)
        row = asyncio.run(db.get_pending_confirm(cid))
        self.assertIsNone(row["statement"])                    # 员工没改，就不写这一列
        self.assertEqual(row["args"]["statement"], "模型生成的原文")  # 原文还在参数里

    def test_cancel_moves_to_cancelled(self):
        cid = self.new_confirm()
        ok, _ = asyncio.run(db.resolve_pending_confirm(cid, self.user_id, "cancel", None))
        self.assertTrue(ok)
        self.assertEqual(asyncio.run(db.get_pending_confirm(cid))["status"], "cancelled")

    def test_second_click_is_rejected(self):
        cid = self.new_confirm()
        self.assertTrue(asyncio.run(
            db.resolve_pending_confirm(cid, self.user_id, "confirm", None))[0])
        ok, reason = asyncio.run(
            db.resolve_pending_confirm(cid, self.user_id, "cancel", None))
        self.assertFalse(ok)
        self.assertIn("已经处理过", reason)
        # 关键：第二次点击不能改写第一次的结论
        self.assertEqual(asyncio.run(db.get_pending_confirm(cid))["status"], "confirmed")

    def test_others_cannot_confirm(self):
        cid = self.new_confirm()
        ok, reason = asyncio.run(
            db.resolve_pending_confirm(cid, self.other["id"], "confirm", None))
        self.assertFalse(ok)
        self.assertIn("无权", reason)
        self.assertEqual(asyncio.run(db.get_pending_confirm(cid))["status"], "pending")

    def test_unknown_id_is_rejected(self):
        ok, reason = asyncio.run(
            db.resolve_pending_confirm("cf_not_exist", self.user_id, "confirm", None))
        self.assertFalse(ok)
        self.assertIn("不存在", reason)

    def test_invalid_action_is_rejected(self):
        cid = self.new_confirm()
        ok, reason = asyncio.run(
            db.resolve_pending_confirm(cid, self.user_id, "approve", None))
        self.assertFalse(ok)
        self.assertIn("action", reason)


class ConfirmExpiryTests(ConfirmDbTestCase):
    def test_expired_confirm_cannot_be_clicked(self):
        cid = self.new_confirm(ttl=-1)
        ok, reason = asyncio.run(
            db.resolve_pending_confirm(cid, self.user_id, "confirm", None))
        self.assertFalse(ok)
        self.assertIn("时限", reason)
        # 拒绝的同时把记录推到终态，避免它一直挂在「待确认」里
        self.assertEqual(asyncio.run(db.get_pending_confirm(cid))["status"], "timeout")

    def test_expired_records_are_not_listed(self):
        self.new_confirm("cf_alive", ttl=60)
        self.new_confirm("cf_dead", ttl=-1)
        rows = asyncio.run(db.list_pending_confirms(self.session["id"]))
        self.assertEqual([r["id"] for r in rows], ["cf_alive"])

    def test_sweep_marks_stale_as_timeout(self):
        self.new_confirm("cf_alive", ttl=60)
        self.new_confirm("cf_dead", ttl=-1)
        self.assertEqual(asyncio.run(db.expire_stale_confirms()), 1)
        self.assertEqual(asyncio.run(db.get_pending_confirm("cf_dead"))["status"], "timeout")
        self.assertEqual(asyncio.run(db.get_pending_confirm("cf_alive"))["status"], "pending")

    def test_close_cannot_forge_confirmation(self):
        """收尾接口只能写 timeout/abandoned，不能被拿来伪造「员工已确认」。"""
        cid = self.new_confirm()
        asyncio.run(db.close_pending_confirm(cid, "confirmed"))
        self.assertEqual(asyncio.run(db.get_pending_confirm(cid))["status"], "abandoned")

    def test_close_does_not_override_a_real_decision(self):
        cid = self.new_confirm()
        asyncio.run(db.resolve_pending_confirm(cid, self.user_id, "confirm", None))
        asyncio.run(db.close_pending_confirm(cid, "abandoned"))
        self.assertEqual(asyncio.run(db.get_pending_confirm(cid))["status"], "confirmed")


class AwaitAcrossWorkersTests(ConfirmDbTestCase):
    """模拟「A 等待、B 点确认」：等待方只能靠库发现决策。"""

    def test_waiter_reads_decision_written_by_another_worker(self):
        cid = self.new_confirm()
        item = agent.PendingConfirm(
            id=cid, session_id=self.session["id"], user_id=self.user_id,
            tool="create_dispute_draft", args={},
        )
        # A 的本地唤醒表里没有这条记录，等价于「确认请求落在另一个 worker」
        agent._CONFIRMS.clear()

        async def scenario():
            waiter = asyncio.create_task(agent._await_confirmation(item))
            await asyncio.sleep(0.05)
            await db.resolve_pending_confirm(cid, self.user_id, "confirm", "跨进程改的正文")
            return await asyncio.wait_for(waiter, timeout=3)

        decision, statement = asyncio.run(scenario())
        self.assertEqual(decision, "confirm")
        self.assertEqual(statement, "跨进程改的正文")

    def test_local_confirm_wakes_waiter_immediately(self):
        cid = self.new_confirm()
        item = agent.PendingConfirm(
            id=cid, session_id=self.session["id"], user_id=self.user_id,
            tool="create_dispute_draft", args={},
        )
        agent._CONFIRMS[cid] = item

        async def scenario():
            waiter = asyncio.create_task(agent._await_confirmation(item))
            await asyncio.sleep(0.05)
            ok, reason = await agent.submit_confirmation(cid, self.user_id, "confirm", None)
            self.assertTrue(ok, reason)
            return await asyncio.wait_for(waiter, timeout=3)

        decision, _ = asyncio.run(scenario())
        self.assertEqual(decision, "confirm")
        # submit_confirmation 必须把状态写进库，而不只是叫醒本地 Event
        self.assertEqual(asyncio.run(db.get_pending_confirm(cid))["status"], "confirmed")

    def test_waiter_times_out_without_decision(self):
        cid = self.new_confirm(ttl=60)
        item = agent.PendingConfirm(
            id=cid, session_id=self.session["id"], user_id=self.user_id,
            tool="create_dispute_draft", args={},
        )
        agent.CONFIRM_TIMEOUT = 0.3
        decision, statement = asyncio.run(agent._await_confirmation(item))
        self.assertEqual(decision, "timeout")
        self.assertIsNone(statement)

    def test_pending_list_for_page_rebuild(self):
        cid = self.new_confirm(statement="服务端保存的正文")
        rows = asyncio.run(agent.list_pending_confirms(self.session["id"]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], cid)
        self.assertEqual(rows[0]["args"]["statement"], "服务端保存的正文")


if __name__ == "__main__":
    unittest.main()
