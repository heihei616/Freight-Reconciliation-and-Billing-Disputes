"""
配置读取与启动前校验。

为什么不把 `os.getenv` 散落在各处
--------------------------------
运行时配置有两种失败方式：

1. **值非法**：`LLM_TIMEOUT=90s`、`RATE_LIMIT_MAX_REQUESTS=ten`。这类错误如果等到
   第一个请求进来才由 `int()` 抛出，表现是「页面能开、一问就 500」，排查成本很高；
2. **环境错配**：生产环境仍用演示口令、仍然没配模型密钥、Cookie 不是 Secure。
   这类问题不会报错，只会安静地不符合上线要求。

所以启动时统一过一遍：`validate_env()` 返回结构化报告，`lifespan` 里有 error 就拒绝启动，
把「带病上线」变成一个立刻失败的动作。warning 不阻断，但会打在启动日志第一屏。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml

from .paths import PROJECT_ROOT

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"

TRUE_WORDS = {"1", "true", "yes", "on"}
FALSE_WORDS = {"0", "false", "no", "off", ""}

# (变量名, 默认值, 最小值, 最大值)
_INT_RULES: tuple[tuple[str, int, int, int], ...] = (
    ("LLM_TIMEOUT", 90, 5, 600),
    ("EMBEDDING_BATCH_SIZE", 10, 1, 128),
    ("JWT_EXPIRE_HOURS", 8, 1, 720),
    ("RATE_LIMIT_WINDOW", 60, 5, 3600),
    ("RATE_LIMIT_MAX_REQUESTS", 10, 1, 5000),
    ("LLM_DAILY_QUOTA", 200, 1, 100000),
    ("AGENT_MAX_CONCURRENT", 4, 1, 128),
    ("AGENT_ACQUIRE_TIMEOUT", 15, 1, 600),
    ("LOG_RETENTION_DAYS", 90, 0, 3650),
    # 登录防爆破：阈值调得过大等于没有防护，调得过小会把正常手误锁死
    ("LOGIN_MAX_FAILURES", 5, 1, 100),
    ("LOGIN_WINDOW_SECONDS", 900, 60, 86400),
    ("LOGIN_IP_MAX_FAILURES", 20, 1, 1000),
    ("LOGIN_IP_WINDOW_SECONDS", 300, 60, 86400),
)

_BOOL_KEYS = ("RATE_LIMIT_ENABLED", "LOGIN_GUARD_ENABLED", "LOG_MASKING",
              "LLM_ENABLE_THINKING", "COOKIE_SECURE", "SEED_DEMO_USERS")

_ENUM_RULES: dict[str, set[str]] = {
    "LLM_PROVIDER": {"openai", "mock"},
    # local = 本地哈希向量（零成本、可离线）；其余值都走向量接口。
    # api 是文档里一贯的写法，openai 作为同义写法保留。
    "EMBEDDING_PROVIDER": {"local", "api", "openai"},
}

# 生产模式的标识值；只有这个值会触发强制检查
PRODUCTION = "production"


@dataclass(frozen=True)
class EnvIssue:
    """一条校验结论。level 只有 error / warning 两种。"""

    level: str
    key: str
    message: str
    hint: str = ""

    def render(self) -> str:
        tag = "错误" if self.level == "error" else "警告"
        text = f"[{tag}] {self.key}：{self.message}"
        return f"{text} → {self.hint}" if self.hint else text


@dataclass
class EnvReport:
    env: str
    issues: list[EnvIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[EnvIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[EnvIssue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, level: str, key: str, message: str, hint: str = "") -> None:
        self.issues.append(EnvIssue(level=level, key=key, message=message, hint=hint))


def load_config(path: Path | None = None) -> dict[str, Any]:
    """读取非机密服务配置；机密仍只从环境变量或 .env 获取。"""
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return {}
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件顶层必须是对象：{config_path}")
    return data


def current_env(environ: Mapping[str, str] | None = None) -> str:
    """运行环境标识，默认 development。只认 production 为生产。"""
    raw = (environ or os.environ).get("APP_ENV", "").strip().lower()
    return raw or "development"


def _read(environ: Mapping[str, str], key: str) -> str:
    return (environ.get(key) or "").strip()


def _check_int(report: EnvReport, environ: Mapping[str, str], key: str,
               default: int, low: int, high: int) -> None:
    raw = _read(environ, key)
    if not raw:
        return                       # 留空 = 用默认值，不告警
    try:
        value = int(raw)
    except ValueError:
        report.add("error", key, f"必须是整数，当前是 {raw!r}",
                   f"改成 {low}~{high} 之间的整数，或删掉这一行使用默认 {default}")
        return
    if not low <= value <= high:
        report.add("error", key, f"超出允许范围（{value} 不在 {low}~{high}）",
                   f"改成 {low}~{high} 之间的整数，默认 {default}")


def _check_float(report: EnvReport, environ: Mapping[str, str], key: str,
                 default: float, low: float, high: float) -> None:
    raw = _read(environ, key)
    if not raw:
        return
    try:
        value = float(raw)
    except ValueError:
        report.add("error", key, f"必须是数字，当前是 {raw!r}",
                   f"改成 {low}~{high} 之间的数字，或删掉这一行使用默认 {default}")
        return
    if not low <= value <= high:
        report.add("error", key, f"超出允许范围（{value} 不在 {low}~{high}）",
                   f"改成 {low}~{high} 之间的数字，默认 {default}")


def _check_bool(report: EnvReport, environ: Mapping[str, str], key: str) -> None:
    raw = _read(environ, key).lower()
    if raw in TRUE_WORDS or raw in FALSE_WORDS:
        return
    report.add("error", key, f"只接受 true/false 或 1/0，当前是 {raw!r}",
               "改成 1 或 0；留空表示使用默认值")


def _check_url(report: EnvReport, environ: Mapping[str, str], key: str,
               *, require_https: bool = False) -> None:
    raw = _read(environ, key)
    if not raw:
        return
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        report.add("error", key, f"不是合法的 http(s) 地址：{raw!r}",
                   "示例：https://api.example.com/v1")
        return
    if require_https and parts.scheme != "https":
        report.add("error", key, "生产环境必须使用 https，明文传输会泄露密钥与对话内容",
                   "把地址换成 https；仅内网自建模型可设 ALLOW_INSECURE_LLM=1 显式豁免")


def validate_env(config: dict[str, Any] | None = None,
                 environ: Mapping[str, str] | None = None) -> EnvReport:
    """
    校验运行时配置，返回可打印、可测试的报告。

    入参都允许注入，测试里不必污染真实环境变量。
    """
    env_map = os.environ if environ is None else environ
    env = current_env(env_map)
    report = EnvReport(env=env)
    is_prod = env == PRODUCTION

    # ---- 1. 类型与范围：非法值必须在启动时暴露 ----
    for key, default, low, high in _INT_RULES:
        _check_int(report, env_map, key, default, low, high)
    _check_float(report, env_map, "LLM_TEMPERATURE", 0.1, 0.0, 2.0)
    for key in _BOOL_KEYS:
        _check_bool(report, env_map, key)
    for key, allowed in _ENUM_RULES.items():
        raw = _read(env_map, key).lower()
        if raw and raw not in allowed:
            report.add("error", key, f"不支持 {raw!r}",
                       f"可选：{' / '.join(sorted(allowed))}")

    allow_insecure = _read(env_map, "ALLOW_INSECURE_LLM") in TRUE_WORDS
    _check_url(report, env_map, "LLM_BASE_URL",
               require_https=is_prod and not allow_insecure)
    _check_url(report, env_map, "EMBEDDING_BASE_URL",
               require_https=is_prod and not allow_insecure)

    # ---- 2. 生产模式：开发期的便利必须显式关掉 ----
    if is_prod:
        if _read(env_map, "LLM_PROVIDER").lower() != "mock" and not _read(env_map, "LLM_API_KEY"):
            report.add("error", "LLM_API_KEY", "生产环境没有配置模型密钥，会降级成 mock 回答",
                       "填写真实密钥；若只想演示，把 APP_ENV 改回 development")
        if not _read(env_map, "LLM_MODEL"):
            report.add("error", "LLM_MODEL", "生产环境没有指定模型名",
                       "填写供应商提供的模型标识")
        if _read(env_map, "LLM_PROVIDER").lower() == "mock":
            report.add("error", "LLM_PROVIDER", "生产环境禁止使用 mock 模型",
                       "改成 openai 并配置密钥")
        jwt_secret = _read(env_map, "JWT_SECRET")
        if len(jwt_secret) < 32:
            report.add(
                "error", "JWT_SECRET",
                f"生产环境必须显式设置至少 32 字符的随机密钥（当前 {len(jwt_secret)} 字符）",
                "生成方式：python -c \"import secrets;print(secrets.token_urlsafe(48))\"。"
                "不设置会退回主目录密钥文件，容器重启后所有人被踢下线",
            )
        if _read(env_map, "COOKIE_SECURE").lower() not in TRUE_WORDS:
            report.add("error", "COOKIE_SECURE", "生产环境必须强制 Secure Cookie",
                       "设 COOKIE_SECURE=1；确认前置代理已启用 HTTPS")
        # 指标默认「仅本机可读」，但同机反向代理会让所有外部请求都来自 127.0.0.1，
        # 这个判断在生产拓扑下等于失效。所以要么给令牌，要么明确关掉。
        metrics_raw = _read(env_map, "METRICS_ENABLED").lower()
        metrics_on = metrics_raw == "" or metrics_raw in TRUE_WORDS
        if metrics_on and not _read(env_map, "METRICS_TOKEN"):
            report.add(
                "error", "METRICS_TOKEN",
                "生产环境开启了指标但没有访问令牌；反向代理后所有请求都来自 127.0.0.1，"
                "「仅本机」判断会失效，等于公开",
                "设置 METRICS_TOKEN=<随机串>（抓取端带 Bearer 或 ?token=）；"
                "不需要指标则设 METRICS_ENABLED=0",
            )
        reload_flag = str((config or {}).get("server", {}).get("reload", "")).lower()
        if reload_flag == "true":
            report.add("error", "server.reload", "生产环境不能开启热重载",
                       "把 config/default.yaml 里的 reload 改为 false")
        # 演示口令随代码发版，生产播种等于后台默认开放。默认已经是关的，
        # 这条拦的是「有人为了省事手动打开」。
        if _read(env_map, "SEED_DEMO_USERS").lower() in TRUE_WORDS:
            report.add("error", "SEED_DEMO_USERS",
                       "生产环境禁止播种演示账号（口令随代码发版）",
                       "删掉这一行，或用 ADMIN_INITIAL_PASSWORD 完成首次引导")
    elif not _read(env_map, "JWT_SECRET"):
        report.add("warning", "JWT_SECRET", "未设置，将使用主目录密钥文件",
                   "多实例部署时必须显式设置，否则各实例签发的 token 互不认")

    # ---- 3. 常见错配：不致命，但值得在启动时提醒 ----
    if _read(env_map, "RATE_LIMIT_ENABLED") and \
            _read(env_map, "RATE_LIMIT_ENABLED").lower() in FALSE_WORDS:
        report.add("warning", "RATE_LIMIT_ENABLED", "限流已关闭，模型额度可能被单个账号打满",
                   "生产环境保持为 1")
    if _read(env_map, "LOG_MASKING") and _read(env_map, "LOG_MASKING").lower() in FALSE_WORDS:
        report.add("warning", "LOG_MASKING", "日志脱敏已关闭，工具参数会原样入库",
                   "生产环境保持为 1")
    if _read(env_map, "EMBEDDING_PROVIDER").lower() not in ("", "local") \
            and not (_read(env_map, "EMBEDDING_API_KEY") or _read(env_map, "LLM_API_KEY")):
        report.add("warning", "EMBEDDING_API_KEY", "向量接口没有密钥，重建索引会失败",
                   "补充 EMBEDDING_API_KEY（不填会复用 LLM_API_KEY），"
                   "或把 EMBEDDING_PROVIDER 改回 local")
    db_path = _read(env_map, "LOGISTICS_DB_PATH")
    if db_path and not Path(db_path).expanduser().is_absolute():
        report.add("warning", "LOGISTICS_DB_PATH", "建议使用绝对路径",
                   "相对路径会随启动目录变化，容器里很容易指错地方")

    return report


def format_report(report: EnvReport) -> str:
    """把报告压成多行文本，供启动日志使用。"""
    if not report.issues:
        return f"[配置] 环境 {report.env}，未发现配置问题"
    lines = [f"[配置] 环境 {report.env}，发现 {len(report.errors)} 个错误、"
             f"{len(report.warnings)} 个警告"]
    lines.extend("  " + issue.render() for issue in report.issues)
    return "\n".join(lines)
