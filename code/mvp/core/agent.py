"""编排层：Agent 框架（工具白名单、effect 分级、调用审计）。

这是 MVP 的「Agent 框架」。它不接大模型，而是用确定性的工具白名单来编排：
- 每个能力都是一个受控 Tool；
- Tool 按副作用分级：read（只读）/ review（需人工审核）/ write（不可逆写）；
- 每次 invoke 都写入审计轨迹（tool / effect / 参数 / 时间），可追踪。

不可逆动作（差异复核、证据审核、生成草稿、争议终审）全部落在 review 级，
由人工确认驱动，AI 只做只读校验与依据归集。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from . import engine
from .knowledge import KnowledgeBase


class Effect(str, Enum):
    READ = "read"        # 只读，无副作用
    REVIEW = "review"    # 需人工审核/确认
    WRITE = "write"      # 不可逆写操作（MVP 内不暴露）


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    effect: Effect
    func: Callable[..., Any]
    review_point: bool = False  # 是否为「人工确认/审核点」


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Agent:
    """确定性 Agent：注册工具白名单，编排并审计每次工具调用。"""

    def __init__(self, knowledge: KnowledgeBase) -> None:
        self.knowledge = knowledge
        self.tools: dict[str, Tool] = self._register()
        self.trail: list[dict[str, Any]] = []

    def _register(self) -> dict[str, Tool]:
        return {
            "list_bills": Tool("list_bills", "列出可核对账单", Effect.READ, engine.bills),
            "audit_bill": Tool("audit_bill", "确定性核对账单", Effect.READ, engine.audit),
            "search_policy": Tool("search_policy", "检索制度条款", Effect.READ, self.knowledge.search),
            "answer": Tool("answer", "制度与账单问答", Effect.READ, engine.answer),
            # —— 三处人工确认/审核点（review 级） ——
            "review_audit": Tool("review_audit", "人工复核差异（审核点①）", Effect.REVIEW, engine.review_audit, True),
            "confirm_evidence": Tool("confirm_evidence", "人工审核证据（审核点②）", Effect.REVIEW, engine.confirm_evidence, True),
            "decide_draft": Tool("decide_draft", "争议终审（审核点③）", Effect.REVIEW, engine.decide, True),
            # —— 受控生成（需通过前置审核点，本身非人工确认点） ——
            "create_draft": Tool("create_draft", "生成争议草稿", Effect.REVIEW, engine.create_draft),
        }

    def invoke(self, tool_name: str, **kwargs: Any) -> Any:
        """按白名单调用工具并记录审计轨迹。"""
        tool = self.tools.get(tool_name)
        if tool is None:
            raise engine.EngineError(f"未注册的工具：{tool_name}", 400, "UNKNOWN_TOOL")
        result = tool.func(**kwargs)
        self.trail.append({
            "tool": tool.name,
            "effect": tool.effect.value,
            "args": kwargs,
            "at": _now(),
        })
        return result

    def review_points(self) -> list[dict[str, str]]:
        """列出全部「人工确认/审核点」工具。"""
        return [
            {"name": tool.name, "description": tool.description}
            for tool in self.tools.values()
            if tool.review_point
        ]

    def capability(self) -> dict[str, Any]:
        """Agent 能力清单（工具白名单 + 审核点 + 审计计数），供接口与演示展示。"""
        return {
            "tools": [
                {"name": tool.name, "effect": tool.effect.value, "description": tool.description}
                for tool in self.tools.values()
            ],
            "review_points": self.review_points(),
            "invocations": len(self.trail),
        }
