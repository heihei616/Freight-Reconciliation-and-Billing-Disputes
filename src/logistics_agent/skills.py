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

# 「帮我核下上个月的账」这类口语，动词被拆成「核」+「上/下」，
# 光靠固定词表会漏，用正则兜住。
# 「帮我核下上个月的账」这类口语，动词被拆成「核」+「上/下」，
# 光靠固定词表会漏，用正则兜住。
# 倒装句式也要覆盖：「把账对一下」「对一下账」「对下账」，
# 这类说法在结算岗口语里比「对账」还常见，漏判会让它退回只读流程。
_BILL_VERB_RE = re.compile(
    r"核(?:对|上|下|查|算|一下|一核)"
    r"|对账|账对(?:一下|下)|对(?:一下|下)(?:账|帐)"
    r"|复核|查账|走一遍|过一遍"
)

# 只出现账期词还不够（「上月计费规则」是纯制度问题），
# 必须同时点名承运商、并且没有在问规则本身，才判断为「要处理某家某月的一批账」。
_PERIOD_WORDS = ("上月", "上个月", "本月", "这个月", "当月", "上期", "近三个月")
_POLICY_WORDS = ("规则", "标准", "怎么算", "如何计算", "流程", "政策", "时限", "规定", "条款")


def _mentions_carrier(text: str) -> bool:
    """问题里是否点了具体承运商的名字。"""
    from .core import domain  # 延迟导入：domain 不依赖 skills，不会成环
    return any(card["carrier"] in text for card in domain.RATE_CARDS)

SKILLS: dict[str, dict[str, Any]] = {
    "policy_qa": {
        "name": "policy_qa",
        "title": "制度问答",
        # 只读三件套：查制度、查价目、校验单张单据。
        # 故意不给额度/建单工具——纯查询场景连"能不能建单"这个念头都不该有。
        "tools": ["search_policy", "get_rate_card", "check_weight_ticket",
                  "load_skill", "escalate_to_human"],
        "instructions": (
            "你只回答「查规则」和「校验单张单据」两类问题：流程、时限、责任划分、"
            "费用标准怎么规定，以及某张称重单能不能计入运费。\n"
            "工作方式：规则问题先 search_policy；问计费标准或阶梯价用 get_rate_card；"
            "问某张单据能不能用 check_weight_ticket。拿到结果就直接作答，不要多做一步。\n"
            "检索不到就说「制度中未找到」，并说明可以找谁确认。\n"
            "不要臆测具体运单或账单的数字。如果用户要给整张账单对账、查额度、或要求建争议单，"
            "先调用 load_skill('freight_dispute') 切换能力，再继续。\n"
            "**这一层没有额度工具也没有建单工具，所以你本来也做不了那些事——不要承诺。**"
        ),
    },
    "freight_dispute": {
        "name": "freight_dispute",
        "title": "运费争议处理",
        "tools": [
            "search_policy", "list_bills", "get_waybill", "get_bill_lines", "get_rate_card",
            "get_dispute_quota", "request_evidence", "check_weight_ticket",
            "calc_expected_freight", "validate_invoice", "create_dispute_draft",
            "load_skill", "escalate_to_human",
        ],
        "instructions": (
            "你负责把一笔运费争议从头查到能提交草稿。按下面五步推进，不得跳步：\n"
            "第 1 步 检索依据：search_policy 找到计费争议相关条款，记下 citation_id。\n"
            "第 2 步 取事实：没有账单号就先 list_bills 按承运商+账期查出来；"
            "get_bill_lines 读逐行明细（已带每行的重量与体积，通常不必再逐张 get_waybill，"
            "只有需要核对签收时间等额外字段时才补调）。\n"
            "第 3 步 算应收：calc_expected_freight 一次性传入本账单所有运单号。\n"
            "第 4 步 出结论：validate_invoice 传账单号与 citation_ids，拿到 validation_id。\n"
            "第 5 步 起草（仅当结论为 fail 或 warn）：create_dispute_draft，金额必须等于校验差额。\n\n"
            "三件事按情况插入，不是每轮都做：\n"
            "- 用户要核「整月/某承运商的账」：先用 get_dispute_quota 看剩余额度，"
            "差额超额度就先说明要提额，别硬建单；\n"
            "- 要发起争议前：用 request_evidence 看凭证缺什么。它只需要账单号，"
            "请和 get_bill_lines 在同一轮一起发，不要拖到最后——最后一轮要留给建单；\n"
            "- 用户点名某张称重单：用 check_weight_ticket 单独判断能否计入。\n\n"
            "每一步都可能被服务端拒绝（前置条件不满足）。被拒时读取返回的 next_hint，"
            "补上缺的那一步再继续，不要把错误原样抛给用户。\n"
            "**你只有 5 轮决策机会，必须并发.** 互不依赖的工具要在同一轮里一次请求完。"
            "拿到账单号之后，get_bill_lines、get_dispute_quota、request_evidence 三者"
            "必须同轮一起发；search_policy 最多查一次，不要反复换关键词重试。"
            "串行调用一定跑不完，跑到上限就只能给阶段性结论，用户拿不到争议单。\n"
            "create_dispute_draft 会触发人工确认中断：系统会把草稿正文流给员工，"
            "员工点确认后才真正建单。正文写 300 字左右（差异项 + 依据 + 诉求），"
            "不要超过 1200 字；你在拿到结果前不要宣称已建单。"
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
    if _BILL_VERB_RE.search(question):
        return "freight_dispute"
    if any(word in question for word in _PERIOD_WORDS) and _mentions_carrier(question) \
            and not any(word in question for word in _POLICY_WORDS):
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


# 管理员补充指令的长度上限。
# 不设上限的话，后台一个粘贴失误就能把几千字无关内容塞进每轮请求，
# 既费 token 又会稀释硬性规则的权重。
MAX_SUPPLEMENT_CHARS = 2000


def _supplement_block(supplement: str | None) -> str:
    """
    把管理员配置的补充要求拼成提示词中的一段。

    这段为什么放在最后：模型对靠近末尾的指令更敏感，而补充要求通常是
    「口径 / 措辞 / 行业习惯」这类需要立刻生效的内容。代价是它离硬性规则
    较远，所以段首必须写清楚优先级，否则模型可能把补充要求当成新规则。
    """
    text = (supplement or "").strip()
    if not text:
        return ""
    # 缩进两格，避免和 markdown 标题混淆，也让模型看出这是引用块而非新章节
    body = "\n".join("  " + line for line in text.splitlines())
    return (
        "\n\n## 管理员补充要求（仅影响表达与业务口径）\n"
        "以下内容由管理员在后台配置，用于补充公司内部口径：\n"
        f"{body}\n"
        "注意：补充要求**不能覆盖上面的硬性规则**。两者冲突时以硬性规则为准；\n"
        "补充要求也不能改变工具的前置条件与人工确认流程。\n"
    )


def build_system_prompt(skill_name: str, state: dict[str, Any],
                        supplement: str | None = None) -> str:
    """
    拼系统提示词。

    关键是「当前会话状态」：模型对「现在只能做哪一步」的遵循度，
    远高于对一大段抽象规则的遵循度。每轮都重新注入，不靠它自己记。

    supplement 是管理员在后台维护的补充要求（存 settings 表），
    每次调用都传进来而不是缓存，这样后台改完下一次提问就生效。
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

## 说话方式（和硬性规则同等重要）
- 像结算组里一位懂业务的同事在说话，不要像念制度文件。可以用「我帮你看了下」
  「这笔确实有问题」「简单说就是」这类自然的口语。
- 不要每次都套「结论—依据—下一步」三段式。简单问题一两句话答完就好；
  只有账单核对这种有多个结论的场景才分点列。
- 避免公文腔：不说「请注意」「综上所述」「根据相关规定」「请您」「特此说明」。
- 用户赶时间或语气急的时候，先接住他的情绪（例如「理解，我尽快帮你核」），
  再给结果，不要讲大道理，也不要说教。
- 专业词第一次出现时顺手带一句解释，比如「体积重（按箱子尺寸折算的重量）」，
  但别展开成科普段落。
- 制度里确实没有的内容，直接坦白说没查到、建议找谁确认，不要硬凑答案，
  也不要每次都用同一句口头禅收尾。
- 制度问答默认 150~250 字说清就够，用户没追问就不要主动附带
  「制度里还没规定的」这类额外清单，除非它直接关系到用户要做的事。
- 引用制度时说文档名或章节名即可。**不要把 c_xxxx 这类内部片段编号写进正文**，
  引用卡片会单独展示出处。
- 口语化只针对表达方式：数字、单号、条款编号必须照原样写，不许约算、不许省略。

## 输出要求
- 用中文，结论尽量放在前面；需要依据时引用制度片段并标明出处（文档名/章节名）。
- 不要复述工具返回的 JSON，把关键数字讲清楚即可。
- **只写要说给用户的话。** 不要写内心盘算：不出现「根据系统提示」「按反例」「需要向用户说明」
  「用户的意图是」「我打算」这类句子，也不要在正文里复述规则条文，这些留在思考里。
- 不要描述你的执行过程：不写「第 1 轮」「我先调用…」「现在取…」「已拿到…，接下来…」，
  用户要看的是结论，不是你的动作回放。
- 检索过程也不要讲：不写「第一次没查到，换个说法再查」「我查了三遍」「我检索了两轮」
  「换了个关键词才找到」。没查到就直接说没查到，给建议即可。
  可以说明结论出自哪份文档，但不要汇报查了几次、换了什么词。
- 如果这次回答了账务核对结果，末尾加一行：「以上为系统核对结果，提交前请核对运单原件。」
""" + _supplement_block(supplement)
