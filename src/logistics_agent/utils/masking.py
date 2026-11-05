"""日志与审计脱敏。

背景
----
agent_steps / tool_calls 会把工具参数与结果摘要落库，里面可能出现：
  - 模型密钥、JWT、数据库连接串（来自异常信息或用户误贴）
  - 员工姓名、手机号、邮箱（来自制度文档或用户陈述）
  - 争议草稿正文（业务敏感，但不能抹掉金额，否则审计失去意义）

设计原则
--------
1. **默认脱敏**：落库前统一过一遍 mask_text()，不依赖每个调用点自觉。
2. **可逆性不需要**：脱敏就是为了不可逆，绝不在日志里保存原值。
3. **可配置关闭**：本地调试可设 LOG_MASKING=0，但生产默认开启。
4. **宁可误伤**：误把一个普通单词遮住的代价，远小于泄露一把密钥。
"""

from __future__ import annotations

import os
import re
from typing import Any

# --- 密钥类：一律整段替换，连首尾都不留 ---
# 每项 = (正则, 替换文本, 类别名)。类别名显式给出，
# 不要从替换文本反推——带分组引用的模板会推出 \1\2\3 这种垃圾名字。
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    # sk- 开头的 OpenAI 兼容密钥，含 dashscope 的 sk-xxxx
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"), "[REDACTED_API_KEY]", "api_key"),
    # JWT：三段 base64url，中间用点连接
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\b"),
     "[REDACTED_JWT]", "jwt"),
    # Authorization: Bearer xxx（不区分大小写）
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.=+/]{8,}"),
     r"\1[REDACTED_TOKEN]", "bearer_token"),
    # key=value 形式的敏感配置：password=、api_key=、secret=、token=
    # 值不允许出现括号与逗号，否则 os.getenv( / 函数调用 / 类型注解都会被误判
    (re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret|access[_-]?token)"
                r"(\s*=\s*)(['\"]?)[^\s,'\";&()]{4,}\3"),
     r"\1\2\3[REDACTED]\3", "credential_assignment"),
)

# 明显的占位符 / 示例值，扫描时不算泄露
_PLACEHOLDER_RE = re.compile(
    r"(?i)(your|example|sample|dummy|placeholder|test|demo|changeme|"
    r"xxx+|\*\*\*|<[^>]*>|redacted|getenv|environ|none|null)"
)

# --- 个人信息类：保留可辨识的结构，遮掉关键位 ---
_PHONE_RE = re.compile(r"\b(1[3-9]\d)\d{4}(\d{4})\b")
_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+\-]{1,3})[A-Za-z0-9._%+\-]*(@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
_IDCARD_RE = re.compile(r"\b(\d{6})\d{8}(\d{3}[\dXx])\b")


def _same_name_assignment(match: re.Match[str]) -> bool:
    """判断是否是 `api_key=api_key` 这种「把变量透传下去」的写法。

    关键字参数传递同名变量非常常见（client(api_key=api_key)），
    它不含任何秘密，扫描器必须放过，否则真正的硬编码会被噪音淹没。
    """
    # 分组顺序：1=键名，2=赋值符，3=引号；值由反向引用匹配，不单独成组，
    # 所以要按已匹配的前缀长度从整段里切出来。
    groups = match.groups()
    if len(groups) < 3 or not match.group(0):
        return False
    prefix_len = sum(len(part or "") for part in groups)
    value = match.group(0)[prefix_len:]
    key = (groups[0] or "").lower().replace("-", "").replace("_", "")
    token = (value or "").lower().replace("-", "").replace("_", "").strip("\"'")
    return bool(token) and key == token


def mask_text(text: str) -> str:
    """对任意文本做脱敏，返回可安全落库的字符串。"""
    if not text:
        return text
    for pattern, repl, _category in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    text = _PHONE_RE.sub(r"\1****\2", text)
    text = _EMAIL_RE.sub(r"\1***\2", text)
    text = _IDCARD_RE.sub(r"\1********\2", text)
    return text


def masking_enabled() -> bool:
    """LOG_MASKING=0/false/off 时关闭，其余情况默认开启。"""
    return os.getenv("LOG_MASKING", "1").strip().lower() not in {"0", "false", "off", "no"}


def mask_value(value: Any, depth: int = 0) -> Any:
    """递归脱敏任意结构：dict / list / str / 标量。

    工具参数是嵌套 dict（例如 {"args": {"statement": "..."}}），
    只在顶层做字符串替换会漏掉嵌套层，所以这里递归到底。
    depth 限制是防御性写法，避免异常对象里有自引用导致死循环。
    """
    if depth > 8:
        return "[MAX_DEPTH]"
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {str(k): mask_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        seq = [mask_value(v, depth + 1) for v in value]
        return type(value)(seq) if not isinstance(value, list) else seq
    return value


def mask_for_log(value: Any) -> Any:
    """落库前的统一入口：尊重开关，未开启时原样返回。"""
    if not masking_enabled():
        return value
    return mask_value(value)


def scan_for_secrets(text: str) -> list[str]:
    """返回文本里命中的敏感类型，供 CI / 上线前扫描使用。

    只返回类别名，不返回命中的原文——扫描报告本身也不能泄密。
    """
    hits: list[str] = []
    for pattern, _repl, category in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            # 占位符与「从环境变量读取」的写法不算泄露：
            # api_key = os.getenv(...) 是正确做法，不该被扫描器告警。
            if _PLACEHOLDER_RE.search(match.group(0)):
                continue
            if _same_name_assignment(match):
                continue
            if category not in hits:
                hits.append(category)
            break
    if _PHONE_RE.search(text):
        hits.append("phone")
    if _EMAIL_RE.search(text):
        hits.append("email")
    if _IDCARD_RE.search(text):
        hits.append("id_card")
    return hits
