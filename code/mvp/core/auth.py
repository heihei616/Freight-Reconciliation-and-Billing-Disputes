"""认证与权限层：基于《六人访谈记录》六角色的登录账号与权限。

从六人访谈提炼出的职责分离边界，映射为六类账号与三处人工审核点：
- 结算经理（顾明岚）：争议终审（审核点③）、全局查看
- 合同审计专员（傅景）：证据审核（审核点②）、全局查看、可否决上线
- 对账专员（唐映秋）：差异复核（审核点①）、生成争议草稿
- 运单录入员（林亦舟）：只读（账单核对 / 制度问答）
- 计费系统工程师（秦芮）：只读 + 全局查看（系统口径视角）
- 承运商结算代表（叶书）：只读（账单核对 / 制度问答）

对应访谈事实（f04/f22/f32/f41/f52）：
- f22 对账专员可整理、校核、发起复核，不能代替合同审计专员完成专业签核；
- f04 立项由结算经理推动，合同审计专员可否决上线；
- f32/f52 接口只读、不自动批准结算或向承运商扣费；
- f41 只有授权业务岗位可查看合同价目等四类资料，不能借用他人账号。

职责分离落点：三处人工审核点（①差异复核 / ②证据审核 / ③争议终审）
分别由对账专员、合同审计专员、结算经理三个不同角色承担。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from dataclasses import dataclass
from typing import Any

from .engine import EngineError


# —— 权限能力（对应 MVP 的接口能力） ——
PERMISSION_LABELS: dict[str, str] = {
    "bills:read": "查看账单、核对与制度问答",
    "review:audit": "审核点① 差异复核",
    "review:evidence": "审核点② 证据审核",
    "draft:create": "生成争议草稿",
    "draft:decide": "审核点③ 争议终审",
    "oversight": "查看全部审核记录、草稿与 Agent 能力",
}


@dataclass(frozen=True)
class User:
    """登录用户：账号、角色、姓名与权限集合。"""

    username: str
    role_key: str
    role_label: str
    name: str
    permissions: frozenset[str]

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def public(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "role_key": self.role_key,
            "role_label": self.role_label,
            "name": self.name,
            "permissions": sorted(self.permissions),
        }


# —— 六角色账号（与《六人访谈记录》一一对应） ——
_ROLE_DEFS: dict[str, dict[str, Any]] = {
    "guminglan": {
        "role_key": "settlement_manager",
        "role_label": "结算经理",
        "name": "顾明岚",
        "permissions": ("bills:read", "draft:decide", "oversight"),
    },
    "fujing": {
        "role_key": "contract_auditor",
        "role_label": "合同审计专员",
        "name": "傅景",
        "permissions": ("bills:read", "review:evidence", "oversight"),
    },
    "tangyingqiu": {
        "role_key": "reconciliation_specialist",
        "role_label": "对账专员",
        "name": "唐映秋",
        "permissions": ("bills:read", "review:audit", "draft:create"),
    },
    "linyizhou": {
        "role_key": "waybill_entry_clerk",
        "role_label": "运单录入员",
        "name": "林亦舟",
        "permissions": ("bills:read",),
    },
    "qinrui": {
        "role_key": "billing_engineer",
        "role_label": "计费系统工程师",
        "name": "秦芮",
        "permissions": ("bills:read", "oversight"),
    },
    "yeshu": {
        "role_key": "carrier_settlement_rep",
        "role_label": "承运商结算代表",
        "name": "叶书",
        "permissions": ("bills:read",),
    },
}

# 演示口令（MVP 演示专用；正式部署须更换为强口令并关闭演示账号提示）
DEMO_PASSWORD = "demo123456"

_PBKDF2_ITERATIONS = 120_000
_PASSWORD_SALT = b"logistics-mvp-auth-v1"


def _hash_password(password: str) -> str:
    """口令摘要（标准库 PBKDF2-HMAC-SHA256，无第三方依赖）。"""
    digest = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), _PASSWORD_SALT, _PBKDF2_ITERATIONS
    )
    return digest.hex()


# 演示口令哈希（所有演示账号共用同一演示口令）
_PASSWORD_HASH = _hash_password(DEMO_PASSWORD)

_USERS: dict[str, User] = {
    username: User(
        username=username,
        role_key=definition["role_key"],
        role_label=definition["role_label"],
        name=definition["name"],
        permissions=frozenset(definition["permissions"]),
    )
    for username, definition in _ROLE_DEFS.items()
}

_SESSIONS: dict[str, str] = {}  # token -> username
_LOCK = threading.RLock()


def accounts() -> list[dict[str, Any]]:
    """返回全部账号（含角色、姓名、权限），供登录页与验收展示。"""
    return [
        {
            "username": user.username,
            "role_key": user.role_key,
            "role_label": user.role_label,
            "name": user.name,
            "permissions": sorted(user.permissions),
        }
        for user in _USERS.values()
    ]


def login(username: str, password: str) -> dict[str, Any]:
    """校验账号口令，成功则签发会话令牌。"""
    username = (username or "").strip()
    user = _USERS.get(username)
    supplied = _hash_password(password or "")
    # 账号不存在也做一次等价哈希，避免通过响应时长枚举用户名
    if user is None or supplied != _PASSWORD_HASH:
        raise EngineError("账号或口令错误", 401, "INVALID_CREDENTIALS")
    token = secrets.token_urlsafe(24)
    with _LOCK:
        _SESSIONS[token] = username
    return {"token": token, "user": user.public()}


def logout(token: str) -> bool:
    """注销会话令牌。"""
    with _LOCK:
        return _SESSIONS.pop((token or "").strip(), None) is not None


def authenticate(token: str) -> User:
    """按会话令牌取用户；无效则抛 401。"""
    token = (token or "").strip()
    with _LOCK:
        username = _SESSIONS.get(token)
    user = _USERS.get(username) if username else None
    if user is None:
        raise EngineError("未登录或会话已失效", 401, "UNAUTHORIZED")
    return user


def require_permission(user: User, permission: str) -> User:
    """校验用户是否拥有某权限；无则抛 403。"""
    if permission not in user.permissions:
        raise EngineError(
            f"当前角色「{user.role_label}」无此操作权限"
            f"（需要「{PERMISSION_LABELS.get(permission, permission)}」）",
            403,
            "FORBIDDEN",
        )
    return user
