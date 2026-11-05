"""
指标与 /metrics 端点测试。

重点不在「数字对不对」，而在三件容易出事的地方：
标签基数是否会爆、标签值是否会破坏文本格式、默认是否只对本机开放。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

from logistics_agent import db
from logistics_agent.app import app
from logistics_agent.metrics import Metrics


class MetricsRenderTests(unittest.TestCase):
    def setUp(self):
        self.metrics = Metrics()

    def test_basic_gauges(self):
        text = self.metrics.render(version="1.4.0")
        self.assertIn('logistics_build_info{version="1.4.0"} 1', text)
        self.assertIn("logistics_uptime_seconds", text)
        self.assertIn("logistics_sse_active 0", text)

    def test_request_counter_groups_by_labels(self):
        self.metrics.observe_request("GET", "/api/health", 200, 0.01)
        self.metrics.observe_request("GET", "/api/health", 200, 0.02)
        self.metrics.observe_request("POST", "/api/chat", 429, 0.5)
        text = self.metrics.render(version="x")
        self.assertIn(
            'logistics_http_requests_total{method="GET",route="/api/health",status="200"} 2', text)
        self.assertIn(
            'logistics_http_requests_total{method="POST",route="/api/chat",status="429"} 1', text)
        self.assertIn('logistics_http_request_seconds_count{method="GET",route="/api/health"} 2', text)

    def test_empty_route_falls_back_to_unmatched(self):
        self.metrics.observe_request("GET", "", 404, 0.01)
        text = self.metrics.render(version="x")
        self.assertIn('route="unmatched"', text)

    def test_label_values_are_escaped(self):
        """路由里出现引号不能把 exposition 格式弄坏。"""
        self.metrics.observe_request("GET", '/odd"path\\x', 200, 0.01)
        text = self.metrics.render(version="x")
        self.assertIn(r'route="/odd\"path\\x"', text)
        # 每一行都必须是 `名称{标签} 值` 或注释/空行
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            self.assertRegex(line, r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{.*\})? -?[0-9.eE+-]+$")

    def test_sse_counter_never_goes_negative(self):
        self.metrics.sse_finished()          # 未开始就结束
        self.assertIn("logistics_sse_active 0", self.metrics.render(version="x"))
        self.metrics.sse_started()
        self.metrics.sse_started()
        self.metrics.sse_finished()
        self.assertIn("logistics_sse_active 1", self.metrics.render(version="x"))

    def test_extra_gauges(self):
        text = self.metrics.render(version="x", extra={"logistics_pending_confirms": 3})
        self.assertIn("logistics_pending_confirms 3", text)

    def test_output_is_sorted(self):
        for route in ("/b", "/a", "/c"):
            self.metrics.observe_request("GET", route, 200, 0.01)
        text = self.metrics.render(version="x")
        positions = [text.index(f'route="{r}"') for r in ("/a", "/b", "/c")]
        self.assertEqual(positions, sorted(positions))


class MetricsEndpointTests(unittest.TestCase):
    def setUp(self):
        self._orig_path, self._orig_conn = db.DB_PATH, db._conn
        db._conn = None
        self._tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self._tmp.name) / "metrics.db"
        import asyncio
        asyncio.run(db.init_db())
        self.client = TestClient(app)

    def tearDown(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = self._orig_conn
        db.DB_PATH = self._orig_path
        self._tmp.cleanup()

    def test_default_allows_only_localhost(self):
        """TestClient 的来源是 testclient，不是回环地址，默认应被拒绝。"""
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 403)

    def test_forwarded_request_is_not_treated_as_local(self):
        """
        同机反向代理会让所有外部请求都来自 127.0.0.1。

        只看 client.host 的话，一个公网用户经 nginx 访问就能读到指标；
        带 X-Forwarded-For 的请求必须按「非本机」处理。
        """
        from fastapi.testclient import TestClient

        local = TestClient(app, client=("127.0.0.1", 12345))
        self.assertEqual(local.get("/metrics").status_code, 200)
        proxied = local.get("/metrics", headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(proxied.status_code, 403)

    def test_token_grants_access(self):
        with patch.dict(os.environ, {"METRICS_TOKEN": "s3cret"}):
            resp = self.client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/plain", resp.headers["content-type"])
        self.assertIn("logistics_build_info", resp.text)
        self.assertIn("logistics_pending_confirms 0", resp.text)

    def test_wrong_token_is_rejected(self):
        with patch.dict(os.environ, {"METRICS_TOKEN": "s3cret"}):
            resp = self.client.get("/metrics", headers={"Authorization": "Bearer nope"})
        self.assertEqual(resp.status_code, 403)

    def test_query_token_is_accepted(self):
        with patch.dict(os.environ, {"METRICS_TOKEN": "s3cret"}):
            resp = self.client.get("/metrics?token=s3cret")
        self.assertEqual(resp.status_code, 200)

    def test_disabled_switch(self):
        with patch.dict(os.environ, {"METRICS_ENABLED": "0", "METRICS_TOKEN": "s3cret"}):
            resp = self.client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
