"""MVP 内核包：知识层 / 领域层 / 编排层。

依赖方向（单向）：app（接口层）→ agent（编排层）→ engine（领域层）→ knowledge（知识层）→ domain（主项目）。
"""

from __future__ import annotations

from . import auth
from .agent import Agent, Effect, Tool
from .auth import PERMISSION_LABELS, User, authenticate, login, logout, require_permission
from .engine import (
    EngineError,
    answer,
    audit,
    bills,
    confirm_evidence,
    create_draft,
    decide,
    list_drafts,
    list_reviews,
    load_clauses,
    review_audit,
    reviews,
    search,
)
from .knowledge import Clause, KnowledgeBase

# 默认知识库单例 + Agent 单例（接口层复用）
KNOWLEDGE = KnowledgeBase()
AGENT = Agent(KNOWLEDGE)

__all__ = [
    "Agent",
    "Effect",
    "Tool",
    "Clause",
    "KnowledgeBase",
    "EngineError",
    "auth",
    "User",
    "PERMISSION_LABELS",
    "authenticate",
    "login",
    "logout",
    "require_permission",
    "KNOWLEDGE",
    "AGENT",
    "answer",
    "audit",
    "bills",
    "confirm_evidence",
    "create_draft",
    "decide",
    "list_drafts",
    "list_reviews",
    "load_clauses",
    "review_audit",
    "reviews",
    "search",
]
