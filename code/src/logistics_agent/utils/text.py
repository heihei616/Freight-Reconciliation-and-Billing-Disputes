"""通用文本工具。

只放与业务无关的纯函数，方便在任何模块里复用，也方便离线单测。
这里的函数不允许访问数据库、网络或环境变量——保持「给输入就出输出」。
"""

from __future__ import annotations

import re
import unicodedata

# 连续空白（含全角空格、不换行空格），统一压成一个半角空格
_WS_RE = re.compile(r"[ \t 　]+")
# 只保留「可打印」字符，去掉方向控制符这类会导致日志错位的字符
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f  ​-‏‪-‮]")


def normalize_text(text: str) -> str:
    """规范化用户输入：NFKC + 去控制字符 + 收敛空白。

    NFKC 能把全角字母数字（Ｂ２０２６）折成半角，避免用户从微信/Excel
    复制过来的单号因为全角而匹配不上正则。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL_RE.sub("", text)
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    # 去掉首尾空行，但保留段内换行（多行陈述需要原始结构）
    return "\n".join(lines).strip("\n")


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """按字符截断，避免把超长工具结果直接塞进模型上下文。"""
    if limit <= 0:
        raise ValueError("limit 必须为正数")
    if len(text) <= limit:
        return text
    if len(suffix) >= limit:
        return text[:limit]
    return text[: limit - len(suffix)] + suffix


def one_line(text: str, limit: int = 120) -> str:
    """压成单行摘要，用于日志的 result_summary 字段。"""
    flat = " ".join(text.split())
    return truncate(flat, limit)


def redact_middle(value: str, head: int = 3, tail: int = 2, mask: str = "***") -> str:
    """保留首尾、遮住中段，用于在界面上展示单号/证件号这类半敏感信息。"""
    if head < 0 or tail < 0:
        raise ValueError("head/tail 不能为负数")
    if len(value) <= head + tail:
        return mask
    return f"{value[:head]}{mask}{value[len(value) - tail:] if tail else ''}"


def extract_identifiers(text: str, pattern: str) -> list[str]:
    """按正则抽取单号并去重，保持出现顺序。

    re.findall 在含分组时会只返回分组内容，这里统一处理，
    免得调用方因为多写一个括号拿到一堆空串。
    """
    seen: dict[str, None] = {}
    for match in re.finditer(pattern, text, re.IGNORECASE):
        token = match.group(1) if match.groups() else match.group(0)
        token = token.upper()
        if token not in seen:
            seen[token] = None
    return list(seen)


def is_blank(text: str | None) -> bool:
    """判断「空白输入」，None、空串、纯空白都算。"""
    return text is None or not text.strip()
