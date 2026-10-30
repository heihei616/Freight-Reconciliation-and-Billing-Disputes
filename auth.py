"""
auth.py —— 登录鉴权（JWT + 两种角色）

为什么用 HttpOnly Cookie 存 JWT 而不是 localStorage
----------------------------------------------------
后台是服务端渲染：页面能不能看，由服务端在渲染前就判定，前端不需要读 token。
放 Cookie 并设 HttpOnly，页面即使被注入脚本也读不到 token；
localStorage 方案在 XSS 面前等于把令牌拱手让人。

密码哈希
--------
用标准库 pbkdf2_hmac（FIPS 认可、够用），不引 passlib。
存储格式：pbkdf2_sha256$迭代次数$盐(base64)$哈希(base64)
带迭代次数是为了以后能平滑升级强度——校验时按存的那次数算。

角色
----
employee：只能问答、看自己的会话
admin  ：额外可进 /admin/*（知识库、提示词、审计）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt
from fastapi import Request

import db

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

COOKIE_NAME = "access_token"
ROLE_EMPLOYEE = "employee"
ROLE_ADMIN = "admin"

# 默认系统提示词。管理员在后台改过之后，以数据库里的为准。
DEFAULT_SYSTEM_PROMPT = (
    "你是公司物流费对账与计费争议助手，只根据提供的制度条文回答，"
    "不确定时明确说'制度中未找到'，回答末尾附引用来源，不得编造报销标准。"
)
SYSTEM_PROMPT_KEY = "system_prompt"

# PBKDF2 迭代次数。20 万次在普通笔记本上约 100ms，足够拖慢离线爆破。
_PBKDF2_ROUNDS = 200_000

# 令牌有效期：8 小时，覆盖一个工作日。
_TOKEN_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "8"))

# 密钥文件放在用户主目录（纯 ASCII 路径），避免落进项目目录被误提交。
_SECRET_FILE = Path.home() / ".logistics_jwt_secret"


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """生成加盐哈希。每次调用盐都不同，同一密码两次哈希结果不一样。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """
    校验密码。

    用 hmac.compare_digest 做定长比较，避免根据响应时间差逐字符猜哈希。
    """
    try:
        algorithm, rounds, salt_b64, digest_b64 = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(rounds)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# JWT 签发与校验
# ---------------------------------------------------------------------------

def _load_secret() -> str:
    """
    读取签名密钥。

    优先环境变量 JWT_SECRET（容器/生产用）；
    没有则从主目录的密钥文件读，文件不存在就生成一次并写入。
    这样重启服务后已签发的 token 仍然有效，不会把所有人踢下线。
    """
    env_secret = os.getenv("JWT_SECRET", "").strip()
    if env_secret:
        return env_secret
    if _SECRET_FILE.exists():
        content = _SECRET_FILE.read_text(encoding="utf-8").strip()
        if content:
            return content
    generated = secrets.token_urlsafe(48)
    _SECRET_FILE.write_text(generated, encoding="utf-8")
    try:
        _SECRET_FILE.chmod(0o600)   # 仅当前用户可读写（Windows 上是尽力而为）
    except OSError:
        pass
    return generated


_SECRET = _load_secret()
_ALGORITHM = "HS256"


def create_token(user: dict[str, Any]) -> str:
    """签发令牌。payload 里带角色，省去每次请求查库。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user["id"],
        "username": user["username"],
        "name": user["display_name"],
        "role": user["role"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=_TOKEN_HOURS)).timestamp()),
    }
    return jwt.encode(payload, _SECRET, algorithm=_ALGORITHM)


def decode_token(token: str) -> dict[str, Any] | None:
    """校验签名与有效期，失败一律返回 None（调用方统一按未登录处理）。"""
    try:
        return jwt.decode(token, _SECRET, algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return None


# ---------------------------------------------------------------------------
# 从请求中取当前用户
# ---------------------------------------------------------------------------

def user_from_request(request: Request) -> dict[str, Any] | None:
    """
    先看 Cookie（浏览器页面/同源 fetch 自动带），再看 Authorization 头
    （方便用 curl 或外部系统调接口）。
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    if not token:
        return None
    claims = decode_token(token)
    if not claims:
        return None
    return {
        "id": claims.get("sub"),
        "username": claims.get("username"),
        "display_name": claims.get("name"),
        "role": claims.get("role"),
    }


def is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user) and user.get("role") == ROLE_ADMIN


# ---------------------------------------------------------------------------
# 演示账号
# ---------------------------------------------------------------------------

_DEMO_USERS = [
    # (用户名, 密码, 显示名, 角色)
    ("admin", "admin123", "系统管理员", ROLE_ADMIN),
    ("employee", "emp123", "张员工", ROLE_EMPLOYEE),
]


async def ensure_demo_users() -> list[str]:
    """
    首次启动建两个演示账号。

    只在用户表为空时创建，避免覆盖管理员后来改的密码。
    返回值用于启动日志提示。
    """
    if await db.count_users() > 0:
        return []
    created = []
    for username, password, display_name, role in _DEMO_USERS:
        await db.create_user(username, hash_password(password), display_name, role)
        created.append(f"{username}/{password}")
    return created


async def ensure_default_prompt() -> None:
    """系统提示词没存过就写一份默认的，之后以数据库为准。"""
    if await db.get_setting(SYSTEM_PROMPT_KEY) is None:
        await db.set_setting(SYSTEM_PROMPT_KEY, DEFAULT_SYSTEM_PROMPT, updated_by="system")
