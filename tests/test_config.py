"""
启动配置校验测试。

这组用例的价值不在于「函数返回了什么」，而在于把上线前最容易犯的配置错误
固化成可执行的断言：拼错的超时、越界的配额、生产环境忘记关掉的演示开关。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from logistics_agent.utils import config


def prod_env(**overrides: str) -> dict[str, str]:
    """一份合法的生产环境配置，用例只覆盖自己关心的字段。"""
    env = {
        "APP_ENV": "production",
        "LLM_PROVIDER": "openai",
        "LLM_API_KEY": "sk-test-key",
        "LLM_MODEL": "qwen3.8-flash",
        "LLM_BASE_URL": "https://api.example.com/v1",
        "JWT_SECRET": "s" * 48,
        "COOKIE_SECURE": "1",
        "EMBEDDING_PROVIDER": "local",
        "RATE_LIMIT_ENABLED": "1",
        "LOG_MASKING": "1",
        # 生产环境必须给指标接口配令牌，否则同机反代下「仅本机」判断会失效
        "METRICS_TOKEN": "m" * 32,
    }
    env.update(overrides)
    return env


class NumericValidationTests(unittest.TestCase):
    def test_empty_environment_has_no_errors(self):
        report = config.validate_env(environ={})
        self.assertTrue(report.ok, [i.render() for i in report.errors])

    def test_non_numeric_timeout_is_rejected(self):
        report = config.validate_env(environ={"LLM_TIMEOUT": "90s"})
        self.assertFalse(report.ok)
        self.assertIn("LLM_TIMEOUT", [i.key for i in report.errors])
        self.assertIn("整数", report.errors[0].message)

    def test_out_of_range_quota_is_rejected(self):
        report = config.validate_env(environ={"RATE_LIMIT_MAX_REQUESTS": "0"})
        self.assertFalse(report.ok)
        self.assertIn("1~5000", report.errors[0].message)

    def test_temperature_range(self):
        self.assertTrue(config.validate_env(environ={"LLM_TEMPERATURE": "0.2"}).ok)
        self.assertFalse(config.validate_env(environ={"LLM_TEMPERATURE": "3"}).ok)

    def test_blank_value_falls_back_to_default(self):
        """留空等于用默认值——这是 .env 里注释掉一行的常见形态，不能报错。"""
        report = config.validate_env(environ={"LLM_TIMEOUT": "", "LLM_DAILY_QUOTA": ""})
        self.assertTrue(report.ok)

    def test_boolean_and_enum(self):
        self.assertFalse(config.validate_env(environ={"LOG_MASKING": "maybe"}).ok)
        self.assertFalse(config.validate_env(environ={"LLM_PROVIDER": "azure"}).ok)
        self.assertTrue(config.validate_env(environ={"LLM_PROVIDER": "mock"}).ok)

    def test_invalid_url(self):
        report = config.validate_env(environ={"LLM_BASE_URL": "api.example.com"})
        self.assertFalse(report.ok)
        self.assertIn("LLM_BASE_URL", [i.key for i in report.errors])

    def test_seed_demo_users_accepts_switch_values_only(self):
        self.assertTrue(config.validate_env(environ={"SEED_DEMO_USERS": "1"}).ok)
        self.assertTrue(config.validate_env(environ={"SEED_DEMO_USERS": "false"}).ok)
        self.assertFalse(config.validate_env(environ={"SEED_DEMO_USERS": "maybe"}).ok)


class ProductionGuardTests(unittest.TestCase):
    def test_complete_production_config_passes(self):
        report = config.validate_env(environ=prod_env())
        self.assertTrue(report.ok, [i.render() for i in report.errors])
        self.assertEqual(report.issues, [])

    def test_missing_api_key_blocks_startup(self):
        report = config.validate_env(environ=prod_env(LLM_API_KEY=""))
        self.assertFalse(report.ok)
        self.assertIn("LLM_API_KEY", [i.key for i in report.errors])

    def test_mock_provider_is_rejected(self):
        report = config.validate_env(environ=prod_env(LLM_PROVIDER="mock", LLM_API_KEY=""))
        keys = [i.key for i in report.errors]
        self.assertIn("LLM_PROVIDER", keys)

    def test_short_jwt_secret_is_rejected(self):
        report = config.validate_env(environ=prod_env(JWT_SECRET="short"))
        self.assertFalse(report.ok)
        issue = next(i for i in report.errors if i.key == "JWT_SECRET")
        self.assertIn("32", issue.message)

    def test_cookie_must_be_secure(self):
        report = config.validate_env(environ=prod_env(COOKIE_SECURE="0"))
        self.assertFalse(report.ok)
        self.assertIn("COOKIE_SECURE", [i.key for i in report.errors])

    def test_reload_is_rejected(self):
        report = config.validate_env(
            environ=prod_env(), config={"server": {"reload": True}})
        self.assertFalse(report.ok)
        self.assertIn("server.reload", [i.key for i in report.errors])

    def test_plain_http_model_endpoint_is_rejected(self):
        report = config.validate_env(
            environ=prod_env(LLM_BASE_URL="http://api.example.com/v1"))
        self.assertFalse(report.ok)
        self.assertIn("https", next(i for i in report.errors
                                    if i.key == "LLM_BASE_URL").message)

    def test_insecure_llm_escape_hatch(self):
        """内网自建模型确实可能只有 http，给一个显式豁免开关，而不是逼人改代码。"""
        report = config.validate_env(environ=prod_env(
            LLM_BASE_URL="http://10.0.0.8:8000/v1", ALLOW_INSECURE_LLM="1"))
        self.assertTrue(report.ok, [i.render() for i in report.errors])

    def test_metrics_without_token_is_rejected(self):
        """
        同机反代下所有请求都来自 127.0.0.1，「仅本机可读」实际等于公开。

        所以生产环境要么给令牌，要么明确关闭指标——不允许「默认开着且无令牌」。
        """
        report = config.validate_env(environ=prod_env(METRICS_TOKEN=""))
        self.assertFalse(report.ok)
        self.assertIn("METRICS_TOKEN", [i.key for i in report.errors])

    def test_metrics_can_be_disabled_explicitly(self):
        report = config.validate_env(environ=prod_env(METRICS_TOKEN="", METRICS_ENABLED="0"))
        self.assertTrue(report.ok, [i.render() for i in report.errors])


class DevelopmentWarningTests(unittest.TestCase):
    def test_missing_jwt_secret_is_only_a_warning(self):
        report = config.validate_env(environ={})
        self.assertTrue(report.ok)
        self.assertIn("JWT_SECRET", [i.key for i in report.warnings])

    def test_disabling_guardrails_warns(self):
        report = config.validate_env(environ={"RATE_LIMIT_ENABLED": "0", "LOG_MASKING": "0"})
        keys = [i.key for i in report.warnings]
        self.assertIn("RATE_LIMIT_ENABLED", keys)
        self.assertIn("LOG_MASKING", keys)

    def test_embedding_api_without_key_warns(self):
        report = config.validate_env(environ={"EMBEDDING_PROVIDER": "openai"})
        self.assertIn("EMBEDDING_API_KEY", [i.key for i in report.warnings])

    def test_embedding_api_provider_is_valid(self):
        """`api` 是项目里一直用的写法，校验规则必须和实际读取逻辑一致。"""
        report = config.validate_env(environ={"EMBEDDING_PROVIDER": "api",
                                              "LLM_API_KEY": "sk-test"})
        self.assertTrue(report.ok, [i.render() for i in report.errors])
        self.assertNotIn("EMBEDDING_API_KEY", [i.key for i in report.warnings])

    def test_relative_db_path_warns(self):
        report = config.validate_env(environ={"LOGISTICS_DB_PATH": "chat.db"})
        self.assertIn("LOGISTICS_DB_PATH", [i.key for i in report.warnings])

    def test_seed_demo_users_is_forbidden_in_production(self):
        """默认已经是关的；这条拦的是「为了省事手动打开」。"""
        report = config.validate_env(environ=prod_env(SEED_DEMO_USERS="1"))
        self.assertFalse(report.ok)
        self.assertIn("SEED_DEMO_USERS", [i.key for i in report.errors])
        # 关掉时不应产生任何额外告警，避免正常部署被噪音淹没
        self.assertTrue(config.validate_env(environ=prod_env(SEED_DEMO_USERS="0")).ok)


class ReportFormatTests(unittest.TestCase):
    def test_format_mentions_counts_and_hints(self):
        report = config.validate_env(environ={"LLM_TIMEOUT": "abc"})
        text = config.format_report(report)
        self.assertIn("1 个错误", text)
        self.assertIn("LLM_TIMEOUT", text)
        self.assertIn("→", text)

    def test_clean_report_is_single_line(self):
        text = config.format_report(config.validate_env(environ=prod_env()))
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("未发现配置问题", text)


if __name__ == "__main__":
    unittest.main()
