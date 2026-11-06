"""
HTTP 层加固测试：探针、安全响应头、CSRF 校验。

这组用例不启动完整的 lifespan（不加载索引、不建演示账号），
因为要验证的是中间件与探针本身；就绪探针的「未就绪」分支正好利用这一点。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "code/src"))

from fastapi.testclient import TestClient

from logistics_agent import auth, db
from logistics_agent.app import app


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_liveness_does_not_touch_dependencies(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("version", body)
        self.assertIn("uptime_sec", body)

    def test_readiness_reports_each_check(self):
        """没加载索引时应判定未就绪：探针要能阻止「带病接流量」。"""
        with tempfile.TemporaryDirectory() as tmp:
            original_path, original_conn = db.DB_PATH, db._conn
            db._conn = None
            db.DB_PATH = Path(tmp) / "probe.db"
            try:
                resp = self.client.get("/readyz")
            finally:
                if db._conn is not None:
                    db._conn.close()
                db._conn = original_conn
                db.DB_PATH = original_path

        self.assertEqual(resp.status_code, 503)
        body = resp.json()
        self.assertEqual(body["status"], "not_ready")
        names = {c["name"] for c in body["checks"]}
        self.assertEqual(names, {"database", "knowledge_base", "model"})
        db_check = next(c for c in body["checks"] if c["name"] == "database")
        self.assertTrue(db_check["ok"], db_check["detail"])


class SecurityHeaderTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_headers_on_html_page(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.headers["x-content-type-options"], "nosniff")
        self.assertEqual(resp.headers["x-frame-options"], "DENY")
        self.assertEqual(resp.headers["referrer-policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", resp.headers["content-security-policy"])
        self.assertIn("base-uri 'self'", resp.headers["content-security-policy"])

    def test_headers_on_json_api(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.headers["x-content-type-options"], "nosniff")

    def test_request_id_is_short_hex(self):
        resp = self.client.get("/healthz")
        rid = resp.headers["x-request-id"]
        self.assertEqual(len(rid), 12)
        int(rid, 16)                     # 必须是合法十六进制

    def test_hsts_only_behind_https(self):
        plain = self.client.get("/healthz")
        self.assertNotIn("strict-transport-security", plain.headers)

        proxied = self.client.get("/healthz", headers={"X-Forwarded-Proto": "https"})
        self.assertIn("max-age=31536000", proxied.headers["strict-transport-security"])


class CsrfTests(unittest.TestCase):
    """浏览器跨站提交一定带 Origin；用它拦住「已登录用户被诱导点提交」。"""

    def setUp(self):
        self.client = TestClient(app)

    def test_cross_site_post_is_rejected(self):
        resp = self.client.post(
            "/api/chat",
            json={"question": "测试"},
            headers={"Cookie": "kb_token=abc", "Origin": "https://evil.example"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], 1026)
        self.assertIn("request_id", resp.json())

    def test_same_origin_post_is_not_blocked(self):
        resp = self.client.post(
            "/api/chat",
            json={"question": "测试"},
            headers={"Cookie": "kb_token=abc", "Origin": "http://testserver"},
        )
        self.assertNotEqual(resp.status_code, 403)

    def test_referer_is_used_when_origin_missing(self):
        resp = self.client.post(
            "/api/chat",
            json={"question": "测试"},
            headers={"Cookie": "kb_token=abc",
                     "Referer": "https://evil.example/page"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_post_without_cookie_is_allowed_through(self):
        """curl / 服务间调用没有浏览器来源，本来就不带 Cookie 上下文。"""
        resp = self.client.post(
            "/api/chat",
            json={"question": "测试"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertNotEqual(resp.status_code, 403)

    def test_safe_methods_are_not_checked(self):
        resp = self.client.get("/healthz", headers={"Origin": "https://evil.example"})
        self.assertEqual(resp.status_code, 200)

    def test_allowed_origins_env(self):
        with patch.dict(os.environ, {"ALLOWED_ORIGINS": "https://partner.example"}):
            resp = self.client.post(
                "/api/chat",
                json={"question": "测试"},
                headers={"Cookie": "kb_token=abc",
                         "Origin": "https://partner.example"},
            )
        self.assertNotEqual(resp.status_code, 403)

    def test_rejection_carries_same_request_id_as_header(self):
        resp = self.client.post(
            "/api/chat",
            json={"question": "测试"},
            headers={"Cookie": "kb_token=abc", "Origin": "https://evil.example"},
        )
        self.assertEqual(resp.json()["request_id"], resp.headers["x-request-id"])


class LoginRedirectTests(unittest.TestCase):
    """登录后跳转目标：只允许站内相对路径。"""

    def test_accepts_internal_path(self):
        from logistics_agent.app import _safe_next

        self.assertEqual(_safe_next("/admin"), "/admin")
        self.assertEqual(_safe_next("/chat?x=1"), "/chat?x=1")

    def test_rejects_absolute_and_protocol_relative(self):
        from logistics_agent.app import _safe_next

        for raw in ("https://evil.example", "//evil.example", "javascript:alert(1)"):
            with self.subTest(raw=raw):
                self.assertIsNone(_safe_next(raw))

    def test_rejects_backslash_bypass(self):
        """
        浏览器把反斜杠当斜杠：`/\\evil.com`、`/%5Cevil.com` 都会跳到外站。

        只判断 `startswith("//")` 会漏掉这两种，所以要先解码再检查形状。
        """
        from logistics_agent.app import _safe_next

        for raw in ("/\\evil.example", "/%5Cevil.example", "/%2F%2Fevil.example"):
            with self.subTest(raw=raw):
                self.assertIsNone(_safe_next(raw))

    def test_rejects_control_characters(self):
        from logistics_agent.app import _safe_next

        for raw in ("/\tevil.example", "/\nevil.example", "/\r\nX: y"):
            with self.subTest(raw=raw):
                self.assertIsNone(_safe_next(raw))

    def test_empty_returns_none(self):
        from logistics_agent.app import _safe_next

        self.assertIsNone(_safe_next(""))


class HealthEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health_requires_login(self):
        """知识库切片数、文档清单与模型名属于内部信息，不该匿名可见。"""
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["code"], 1006)

    def test_provider_info_has_no_base_url(self):
        """base_url 可能指向内网模型网关，属于基础设施信息。"""
        from logistics_agent import llm

        info = llm.provider_info()
        self.assertNotIn("base_url", info)
        self.assertNotIn("api_key", "".join(info.keys()).lower())


class PaginationTests(unittest.TestCase):
    """负数 limit 在 SQLite 里等于「不限制」，必须先夹紧。"""

    def setUp(self):
        self.client = TestClient(app)
        self.token = auth.create_token({
            "id": "u-test", "username": "tester",
            "display_name": "测试员", "role": "employee",
        })

    def test_negative_limit_rejected(self):
        resp = self.client.get("/api/agent/runs?limit=-1",
                               headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(resp.status_code, 422)

    def test_zero_and_huge_limits_rejected(self):
        for value in ("0", "101", "999999"):
            with self.subTest(value=value):
                resp = self.client.get(f"/api/agent/runs?limit={value}",
                                       headers={"Authorization": f"Bearer {self.token}"})
                self.assertEqual(resp.status_code, 422)


class UploadGuardTests(unittest.TestCase):
    """上传接口不能被用来打爆内存。"""

    class _StubUpload:
        """分块上传时 file.size 为 None，只能靠有界读取兜住。"""

        def __init__(self, payload: bytes):
            self.filename = "制度.md"
            self.size = None
            self.payload = payload
            self.read_sizes: list[int] = []

        async def read(self, n: int = -1) -> bytes:
            self.read_sizes.append(n)
            return self.payload

    def test_read_is_bounded_when_size_unknown(self):
        import asyncio

        from logistics_agent.app import ApiError, admin_upload_document
        from logistics_agent.rag_service import MAX_UPLOAD_BYTES

        stub = self._StubUpload(b"x" * (MAX_UPLOAD_BYTES + 10))
        with self.assertRaises(ApiError) as ctx:
            asyncio.run(admin_upload_document(file=stub, user={"id": "u", "role": "admin"}))
        self.assertEqual(ctx.exception.code, 1008)
        # 关键：只读上限 +1，而不是整个文件
        self.assertEqual(stub.read_sizes, [MAX_UPLOAD_BYTES + 1])

    def test_delete_rejects_traversal_names(self):
        """
        `Path("..").name` 仍然是 `..`，直接拼到 DOCS_DIR 后 unlink 会抛
        IsADirectoryError，在接口层表现为 500。必须先做形状校验。
        """
        import asyncio

        from logistics_agent.rag_service import kb_service

        for name in ("..", ".", "", "win.exe"):
            with self.subTest(name=name):
                with self.assertRaises(FileNotFoundError):
                    asyncio.run(kb_service.delete_document(name))


if __name__ == "__main__":
    unittest.main()
