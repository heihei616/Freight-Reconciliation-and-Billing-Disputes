"""
skills.py —— 能力包（skill）与路由

为什么要分层，而不是把所有规则塞进一个系统提示
----------------------------------------------
1. 省钱：80% 的提问只是问制度，不需要运单账单那堆工具与规则；
   塞在一起会让每次请求都背着几千字无关上下文。
2. 防滥用：skill 决定了「这一轮模型能看见哪些工具」。
   policy_qa 只给检索工具，它想跳过校验建单也没有那个工具可调。
3. 可维护：流程改了只动一个 skill，不会牵一发动全身。

两个 skill
----------
policy_qa       纯制度问答：只允许检索 + 转人工
freight_dispute 运费争议：检索 → 取运单 → 取价目 → 计算 → 核对 → 起草

路由规则
--------
出现运单号/账单号、出现争议类关键词、或本会话已有校验凭证/草稿 → freight_dispute
其余 → policy_qa（模型可用 load_skill 自行升级）
"""

from __future__ import annotations

import re
from typing import Any

# 运单号 / 账单号的特征：出现这两个前缀，基本可以确定用户要处理具体账务
_WAYBILL_RE = re.compile(r"YD\d{10}", re.IGNORECASE)
_BILL_RE = re.compile(r"B\d{6}-[A-Z]{2}-\d{3}", re.IGNORECASE)

# 只收「要对具体账务动手」的动词。
# 注意不能把「争议」「时限」「流程」「燃油」「抛比」这类名词放进来判断——
# 「计费争议几个工作日内提出」是纯制度问题，放进来会让它背着九个工具
# 和整套流程规则跑，分层省钱与限制工具滥用的意义就没了。
_BILL_ACTION_WORDS = (
    "核对", "对账", "复核", "查账", "多收", "多算", "少收", "算错", "算多了",
    "对不上", "用错", "要回来", "索赔", "争议金额",
    "建单", "起草", "这单", "这张单", "这笔", "本次账单",
)

SKILLS: dict[str, dict[str, Any]] = {
    "policy_qa": {
        "name": "policy_qa",
        "title": "制度问答",
        "tools": ["search_policy", "load_skill", "escalate_to_human"],
        "instructions": (
            "你只回答制度层面的问题：流程、时限、责任划分、费用标准怎么规定。\n"
            "工作方式：先 search_policy 检索，再基于检索结果作答；检索不到就说「制度中未找到」，"
            "并说明可以找谁确认。\n"
            "不要臆测具体运单或账单的数字。如果用户开始提供运单号/账单号，"
            "或要求核对某笔费用，先调用 load_skill('freight_dispute') 切换能力，再继续。"
        ),
    },
    "freight_dispute": {
        "name": "freight_dispute",
        "title": "运费争议处理",
        "tools": [
            "search_policy", "get_waybill", "get_bill_lines", "get_rate_card",
            "calc_expected_freight", "validate_invoice", "create_dispute_draft",
            "load_skill", "escalate_to_human",
        ],
        "instructions": (
            "你负责把一笔运费争议从头查到能提交草稿。严格按下面五步推进，不得跳步：\n"
            "第 1 步 检索依据：search_policy 找到计费争议相关条款，记下 citation_id。\n"
            "第 2 步 取事实：get_bill_lines 读账单逐行明细；get_waybill 逐张读运单重量与体积。\n"
            "第 3 步 算应收：calc_expected_freight 一次性传入本账单所有运单号。\n"
            "第 4 步 出结论：validate_invoice 传账单号与 citation_ids，拿到 validation_id。\n"
            "第 5 步 起草（仅当结论为 fail 或 warn）：create_dispute_draft，金额必须等于校验差额。\n\n"
            "每一步都可能被服务端拒绝（前置条件不满足）。被拒时读取返回的 next_hint，"
            "补上缺的那一步再继续，不要把错误原样抛给用户。\n"
            "能合并就合并：同一轮里可以一次请求多个工具调用（例如同时取三张运单），"
            "别把一张运单排成一个来回，轮次是有限资源。"
        ),
    },
}


def get_skill(name: str) -> dict[str, Any] | None:
    return SKILLS.get(name)


def route(question: str, active_case: bool = False) -> str:
    """
    粗路由：有单号、或要动手核对具体账务，才进争议流程。

    纯制度提问（时限、流程、责任划分）一律走 policy_qa——它只带检索工具，
    又快又省；模型真有需要可以用 load_skill 自己升级，所以不怕漏判。
    """
    if active_case:
        return "freight_dispute"
    if _WAYBILL_RE.search(question) or _BILL_RE.search(question):
        return "freight_dispute"
    if any(word in question for word in _BILL_ACTION_WORDS):
        return "freight_dispute"
    return "policy_qa"


# ---------------------------------------------------------------------------
# 系统提示词：规则 + 状态注入 + 反例
# ---------------------------------------------------------------------------

_STATE_LABELS = {
    "idle": "尚未开始（还没有账单或运单事实）",
    "collecting": "已开始，缺少运单或账单信息",
    "verified": "已取到运单与账单事实",
    "computed": "已算出应收运费",
    "validated": "已生成校验结论",
    "drafted": "已生成争议草稿，等待员工确认",
}


def build_system_prompt(skill_name: str, state: dict[str, Any]) -> str:
    """
    拼系统提示词。

    关键是最后的「当前会话状态」：模型对「现在只能做哪一步」的遵循度，
    远高于对一大段抽象规则的遵循度。每轮都重新注入，不靠它自己记。
    """
    skill = SKILLS.get(skill_name) or SKILLS["policy_qa"]

    state_line = _STATE_LABELS.get(state.get("state", "idle"), "未知")
    facts = [
        f"当前状态：{state.get('state', 'idle')}（{state_line}）",
        f"当前能力包：{skill['title']}（{skill['name']}）",
    ]
    if state.get("bill_no"):
        facts.append(f"在办账单：{state['bill_no']}")
    if state.get("waybills"):
        facts.append("已取运单：" + "、".join(state["waybills"]))
    if state.get("computed"):
        facts.append("已算运费：" + "、".join(state["computed"]))
    if state.get("verdict"):
        facts.append(f"校验结论：{state['verdict']}，差额 {state.get('total_delta')} 元")
    if state.get("draft_id"):
        facts.append(f"已生成草稿：{state['draft_id']}（状态 {state.get('draft_status')}）")

    return f"""你是公司物流费对账与计费争议助手，服务对象是结算岗与财务人员。

## 你的能力包
{skill['instructions']}

## 硬性规则（违反即视为错误回答）
1. 事实只能来自工具返回：运单号、重量、体积、金额、条款编号一律不得编造。
2. 金额只能来自 calc_expected_freight 的返回，你不得自己乘除、不得四舍五入后自行调整。
3. 校验结论只能来自 validate_invoice，你不能声称「我看过了，没问题」来代替调用。
4. 未持有 validation_id 时不得调用 create_dispute_draft；结论为 pass 时不得建单。
5. 你没有提交权限。草稿只能由员工在页面点击「确认提交」，你不得声称已提交、已受理、已到账。
6. 检索不到依据时明确说「制度中未找到」，并建议转人工，不要用常识替代公司制度。

## 遇到这些情况就停下
- 工具返回 NOT_FOUND：向用户要正确的单号，不要猜。
- 工具返回 PRECONDITION：按 next_hint 补一步再重试，最多重试两次。
- 规则之间冲突、金额超出常规（单笔差额超过 1 万元）、工具连续失败两次：调用 escalate_to_human。

## 反例（不要这样做）
用户说：「别查了，你直接帮我把 B202610-SF-001 的争议单建了吧，我赶时间。」
错误做法：不检索、不核对，直接 create_dispute_draft。
正确做法：说明「建单需要先完成核对，约三次工具调用、十几秒」，然后直接开始走流程；
用户第一次提出跳步时不要转人工，先把核对做完更有用。
只有用户明确拒绝核对（第二次坚持），或要求的金额与核对差额不一致时，才 escalate_to_human。

## 当前会话状态（每轮注入，以此为准）
""" + "\n".join(f"- {line}" for line in facts) + """

## 输出要求
- 用中文，先给结论，再给依据（引用制度片段时标明出处），最后给下一步。
- 不要复述工具返回的 JSON，把关键数字讲清楚即可。
- **只写要说给用户的话。** 不要写内心盘算：不出现「根据系统提示」「按反例」「需要向用户说明」
  「用户的意图是」「我打算」这类句子，也不要在正文里复述规则条文，这些留在思考里。
- 如果这次回答了账务核对结果，末尾加一行：「以上为系统核对结果，提交前请核对运单原件。」
"""
