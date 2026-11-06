"""知识层：制度知识库（语料加载、关键词检索、条款引用）。

这是 MVP 的「知识库」模块。它把制度文本（policies.md）解析为可检索的
P-01～P-08 条款，提供可解释的关键词检索与稳定引用；不调用模型、不依赖
向量库，保证结果可复现、可测试、可追溯。

职责边界：
- 只负责「检索与引用」，不下结算结论；
- 未命中时返回空列表，绝不编造未登记的规则。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CLAUSE_HEADING = re.compile(r"^##\s+(P-\d{2})\s+(.+?)\s*$", re.MULTILINE)

# 每条制度条款的检索关键词表（与条款正文保持一致）
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "P-01": ("体积重", "体积重量", "抛比", "实重", "实际重量", "计费重量", "泡货", "6000", "5000"),
    "P-02": ("阶梯", "单价", "基础运费", "最低收费", "最低一票", "落档", "价目"),
    "P-03": ("燃油", "附加费", "费率", "上一季度", "油价"),
    "P-04": ("重复计费", "重复", "核对", "逐行", "账单金额", "应收金额", "冲回"),
    "P-05": ("争议额度", "额度", "月度上限", "审批", "结算经理", "争议金额"),
    "P-06": ("证据", "凭证", "称重单", "签收回执", "签章", "盖章", "材料", "偏差"),
    "P-07": ("人工确认", "确认", "草稿", "争议单", "提交", "取消", "绕过"),
    "P-08": ("审计", "留痕", "差异解释", "计费明细", "复盘", "价目版本"),
}

# 默认语料位置：mvp/policies.md（相对本文件上一级）
DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "policies.md"


@dataclass(frozen=True)
class Clause:
    """一条不可变的制度条款。"""

    id: str
    title: str
    text: str


class KnowledgeBase:
    """制度知识库：加载 P-01～P-08，支持关键词检索与稳定引用。"""

    def __init__(self, policy_path: str | Path = DEFAULT_POLICY_PATH) -> None:
        self.policy_path = Path(policy_path)
        self._clauses = self._load()

    def _load(self) -> list[Clause]:
        text = self.policy_path.read_text(encoding="utf-8")
        matches = list(_CLAUSE_HEADING.finditer(text))
        clauses: list[Clause] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[match.end():end].strip()
            clauses.append(Clause(id=match.group(1), title=match.group(2), text=body))
        ids = [item.id for item in clauses]
        expected = [f"P-{number:02d}" for number in range(1, 9)]
        if ids != expected or any(not item.text for item in clauses):
            raise RuntimeError("MVP 制度语料必须完整包含 P-01～P-08，且顺序稳定")
        return clauses

    def count(self) -> int:
        """条款数量。"""
        return len(self._clauses)

    def clauses(self) -> list[Clause]:
        """返回全部条款（按 P-01～P-08 顺序）。"""
        return list(self._clauses)

    def cite(self, clause_id: str) -> Clause:
        """按编号返回条款；不存在则抛 KeyError。"""
        for clause in self._clauses:
            if clause.id == clause_id:
                return clause
        raise KeyError(clause_id)

    def search(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        """可解释的关键词检索；未命中返回空列表而不是编造条款。"""
        question = (query or "").strip().lower()
        limit = max(1, min(int(limit), 5))
        ranked: list[tuple[int, str, Clause]] = []
        for clause in self._clauses:
            score = 0
            for keyword in _KEYWORDS[clause.id]:
                if keyword.lower() in question:
                    score += 2 if keyword in clause.title else 1
            if score:
                ranked.append((score, clause.id, clause))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [
            {"id": clause.id, "title": clause.title, "text": clause.text, "score": score}
            for score, _cid, clause in ranked[:limit]
        ]
