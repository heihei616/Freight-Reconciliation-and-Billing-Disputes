"""
agent.py —— 智能体执行循环（Harness）

为什么自己写 Harness，而不是用 LangChain / LangGraph / AutoGen
--------------------------------------------------------------
1. 本项目的硬需求是「执行前强制中断」：create_dispute_draft 被调用时，
   必须先把草稿内容流给员工、等真人点确认，才允许真正落库。
   这要求循环能在「工具执行前」挂起并原样恢复——框架里要么没有这个钩子，
   要么得把状态机、SQLite 审计、SSE 全部改写成它的 checkpointer 格式，成本反而更高。
2. 每一轮决策都要落库（工具名/参数/耗时/摘要），供阶段 5 做效果分析。
   自建循环里，这只是一次 INSERT；用框架则要接 callback 体系再对齐字段。
3. 工具总共 12 个、单进程、单模型，框架提供的链路编排、多智能体、记忆后端全用不上。

所以本文件就是一个约 400 行的 Harness，职责只有四件事：
    决策（问模型） → 执行（跑工具） → 回填（把结果塞回上下文） → 再决策
外加三道硬约束：轮次上限、重复调用上限、写操作强制人工确认。

与「提示词约束」的分工
----------------------
提示词只负责让模型「不想」违规；真正的边界在这里：
    - 轮次、重复调用、工具白名单：代码里的 if；
    - 建单必须先有 validation_id：tools.py 的前置条件；
    - 建单前必须人工确认：本文件的 interrupt 分支，和提示词无关。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import db
import llm
import skills
import tools

# 一次提问最多走几轮「决策 → 执行」。
# 用户要求 5 轮：够跑完「查规则 → 查额度 → 要凭证 → 校验」这段探索，
# 又不至于让模型在信息不足时无限试错。
MAX_ROUNDS = 5

# 建单收尾阶段：5 轮用尽但已经拿到校验结论时，额外给一轮「只能建单」的机会。
# 为什么这不算第 6 轮决策：建不建单在拿到 verdict 那一刻就定了，不是新决策；
# 而人工确认本身是人的动作。不给这一轮的话，只要模型把校验压到第 5 轮，
# 用户就永远拿不到争议单——那不是安全，是流程没走完。
DRAFT_ROUND_TOOLS = ["create_dispute_draft", "escalate_to_human"]

# 单轮里模型可能一次要多个工具（例如同时取 4 张运单），
# 所以「轮数」之外还要有「总调用数」这道更硬的闸。
MAX_TOOL_CALLS = 12

# 同一工具 + 同一参数重复超过这个次数，一定是模型陷进去了，直接拒绝。
MAX_SAME_CALL = 2

# 等待员工确认的时限。超时按「取消」处理，绝不默认同意。
CONFIRM_TIMEOUT = 180

TOOL_TITLES = {
    "search_policy": "检索制度条款",
    "get_waybill": "核对运单事实",
    "get_bill_lines": "读取账单明细",
    "list_bills": "查询账单列表",
    "get_rate_card": "查询合同价目",
    "get_dispute_quota": "查询争议额度",
    "request_evidence": "核对凭证齐备",
    "check_weight_ticket": "校验称重单",
    "calc_expected_freight": "计算应收运费",
    "validate_invoice": "逐行核对账单",
    "create_dispute_draft": "起草争议单",
    "escalate_to_human": "转人工处理",
    "load_skill": "切换能力包",
}


# ---------------------------------------------------------------------------
# 人工确认：执行前中断的「信号量」
# ---------------------------------------------------------------------------

@dataclass
class PendingConfirm:
    """
    一次待确认的写操作。

    它活在进程内存里而不是数据库里，是刻意的：
    确认只对「当前这条还挂着的 SSE 连接」有意义——连接断了这次确认就该失效，
    不存在「三天后再点确认把单建出来」这种事。
    """
    id: str
    session_id: str
    user_id: str
    tool: str
    args: dict[str, Any]
    event: asyncio.Event = field(default_factory=asyncio.Event)
    decision: str | None = None          # confirm / cancel
    statement: str | None = None         # 员工可能改过的正文
    created_at: float = field(default_factory=time.time)


_CONFIRMS: dict[str, PendingConfirm] = {}


def submit_confirmation(confirm_id: str, user_id: str, action: str,
                        statement: str | None = None) -> tuple[bool, str]:
    """
    由 HTTP 接口调用：员工点了「确认建单」或「取消」。

    这里只负责「唤醒循环」，真正的建单动作仍由循环在挂起处继续执行——
    这样工具调用、日志、事件流都还在同一条链路上，不会出现两份状态。
    """
    item = _CONFIRMS.get(confirm_id)
    if item is None:
        return False, "确认请求不存在或已超时，请重新发起"
    if item.user_id != user_id:
        return False, "无权确认他人发起的草稿"
    if item.decision is not None:
        return False, "该确认已经处理过了"
    if action not in ("confirm", "cancel"):
        return False, "action 只能是 confirm 或 cancel"

    item.decision = action
    item.statement = statement
    item.event.set()          # 唤醒 agent.run 里挂起的 await
    return True, "ok"


async def _await_confirmation(item: PendingConfirm) -> str:
    """等员工点按钮；超时按取消处理，绝不默认通过。"""
    try:
        await asyncio.wait_for(item.event.wait(), timeout=CONFIRM_TIMEOUT)
        return item.decision or "cancel"
    except asyncio.TimeoutError:
        return "timeout"


# ---------------------------------------------------------------------------
# 状态推导：全部来自数据库里已有的痕迹，不靠模型「记住自己走到哪」
# ---------------------------------------------------------------------------

async def derive_state(session_id: str) -> dict[str, Any]:
    """把工具轨迹 + 凭证 + 草稿折叠成一句「现在到哪一步了」。"""
    calls = await db.list_tool_calls(session_id, limit=200)

    bill_no: str | None = None
    waybills: list[str] = []
    computed: list[str] = []

    for call in reversed(calls):        # list 是倒序的，反转成正序取最后一次
        data = (call.get("result") or {}).get("data")
        if not data:
            continue
        if call["name"] == "get_bill_lines" and data.get("bill_no"):
            bill_no = data["bill_no"]
        elif call["name"] == "get_waybill" and data.get("waybill_no"):
            if data["waybill_no"] not in waybills:
                waybills.append(data["waybill_no"])
        elif call["name"] == "calc_expected_freight":
            for item in data.get("results", []):
                if item.get("waybill_no") and item["waybill_no"] not in computed:
                    computed.append(item["waybill_no"])

    validation = await db.latest_validation(session_id)
    draft = await db.latest_draft(session_id)

    if draft:
        state = "drafted"
    elif validation:
        state = "validated"
    elif computed:
        state = "computed"
    elif bill_no:
        state = "verified"
    elif waybills:
        state = "collecting"
    else:
        state = "idle"

    return {
        "state": state,
        "bill_no": bill_no,
        "waybills": waybills,
        "computed": computed,
        "verdict": validation["verdict"] if validation else None,
        "total_delta": validation["total_delta"] if validation else None,
        "validation_id": validation["id"] if validation else None,
        "draft_id": draft["id"] if draft else None,
        "draft_status": draft["status"] if draft else None,
    }


# ---------------------------------------------------------------------------
# 结果摘要：前端步骤条与日志表都只显示这一行，不能让用户读 JSON
# ---------------------------------------------------------------------------

def summarize(name: str, payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        error = payload.get("error") or {}
        return f"{error.get('code', 'ERROR')}：{error.get('message', '执行失败')}"

    data = payload.get("data") or {}
    if name == "search_policy":
        return f"命中 {len(data.get('citations', []))} 条条款"
    if name == "get_waybill":
        return (f"{data.get('waybill_no')} · {data.get('carrier')} {data.get('lane')} · "
                f"实重 {data.get('weight_kg')}kg / 体积 {data.get('volume_m3')}m³")
    if name == "get_bill_lines":
        return (f"{data.get('bill_no')} · {len(data.get('lines', []))} 行 · "
                f"合计 {data.get('total_amount')} 元")
    if name == "list_bills":
        bills = data.get("bills", [])
        if not bills:
            return "没有符合条件的账单"
        return f"{len(bills)} 张：" + "、".join(b["bill_no"] for b in bills[:3])
    if name == "get_rate_card":
        rate = data.get("rate_card") or {}
        tiers = rate.get("tiers") or []
        if tiers:
            ladder = " / ".join(f"{t['label']} {t['price_per_kg']}元" for t in tiers)
            return f"{rate.get('version')} · 阶梯价 {ladder} · 抛比 {rate.get('volume_ratio')}"
        return f"{rate.get('version')} · {rate.get('price_per_kg')} 元/kg · 抛比 {rate.get('volume_ratio')}"
    if name == "get_dispute_quota":
        quota = data.get("quota") or {}
        return (f"{quota.get('carrier')} {quota.get('month')} · 剩余额度 "
                f"{quota.get('remaining')} / {quota.get('monthly_limit')} 元")
    if name == "request_evidence":
        if data.get("complete"):
            return "三类凭证齐备"
        return "缺：" + "、".join(data.get("missing", []))
    if name == "check_weight_ticket":
        return f"{data.get('conclusion')}（{data.get('waybill_no')}）"
    if name == "calc_expected_freight":
        results = data.get("results", [])
        total = round(sum(r.get("expected_amount", 0) for r in results), 2)
        return f"{len(results)} 张运单，应收合计 {total} 元"
    if name == "validate_invoice":
        return (f"结论 {data.get('verdict')} · {len(data.get('issues', []))} 处问题 · "
                f"差额 {data.get('total_delta')} 元")
    if name == "create_dispute_draft":
        return f"草稿 {data.get('draft_id')} · 金额 {data.get('amount')} 元 · 待人工提交"
    if name == "escalate_to_human":
        return f"已登记人工介入：{data.get('reason')}"
    if name == "load_skill":
        return f"切换到 {data.get('title')}（{data.get('skill')}）"
    return "完成"


def _tool_content(payload: dict[str, Any]) -> str:
    """回灌给模型的工具消息：完整 JSON，模型才能看到 next_hint 自我纠正。"""
    return json.dumps(payload, ensure_ascii=False)


class ThinkingFilter:
    """
    过滤模型写在正文里的「[Thinking] … [回复]」段落。

    部分混合推理模型在开启工具调用时会把思考过程直接写进 content。
    这里做流式过滤：标记可能被切成两半，所以未命中的尾巴留在缓冲区等下一片。
    放在服务端而不是前端——落库内容也必须是干净的，否则审计页里会留着思维过程。
    """

    OPEN = "[Thinking]"
    CLOSE = "[回复]"

    def __init__(self) -> None:
        self._buffer = ""
        self._skipping = False

    def feed(self, text: str) -> str:
        self._buffer += text
        out: list[str] = []
        while True:
            if self._skipping:
                index = self._buffer.find(self.CLOSE)
                if index == -1:
                    if len(self._buffer) > len(self.CLOSE):
                        self._buffer = self._buffer[-len(self.CLOSE):]
                    return "".join(out)
                self._buffer = self._buffer[index + len(self.CLOSE):]
                self._skipping = False
                continue

            index = self._buffer.find(self.OPEN)
            if index != -1:
                out.append(self._buffer[:index])
                self._buffer = self._buffer[index + len(self.OPEN):]
                self._skipping = True
                continue

            keep = len(self.OPEN) - 1
            if len(self._buffer) > keep:
                out.append(self._buffer[:-keep])
                self._buffer = self._buffer[-keep:]
            return "".join(out)

    def flush(self) -> str:
        rest = "" if self._skipping else self._buffer
        self._buffer = ""
        return rest


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

async def run(user_id: str, session_id: str, question: str,
              history: list[dict[str, Any]], mock: bool = False
              ) -> AsyncIterator[dict[str, Any]]:
    """
    执行一轮智能体对话。事件顺序大致是：

        state → (status → delta/tool_call/tool_result → sources)* → done

    写操作会多出：

        confirm_open → confirm_delta* → confirm_ready →（等员工点）→ confirm_closed → tool_result

    每个工具调用、每次人工确认都会写一条 agent_steps 日志，供阶段 5 分析。
    """
    started = time.perf_counter()
    state = await derive_state(session_id)
    active_case = bool(state["validation_id"] or state["draft_id"])
    skill_name = skills.route(question, active_case=active_case)

    # 一次提问 = 一个 run，先落库拿到 run_id，后面每一步都挂在它下面
    run_row = await db.create_run(session_id, user_id, question, skill_name)
    run_id = run_row["id"]

    system_prompt = skills.build_system_prompt(skill_name, state)
    messages: list[dict[str, Any]] = (
        [{"role": "system", "content": system_prompt}]
        + [{"role": m["role"], "content": m["content"]}
           for m in history if m.get("role") in ("user", "assistant") and m.get("content")]
        + [{"role": "user", "content": question}]
    )

    yield {"event": "state", "data": {
        "run_id": run_id,
        "skill": skill_name,
        "skill_title": skills.SKILLS[skill_name]["title"],
        "stage": state["state"],
        "tools": skills.SKILLS[skill_name]["tools"],
        "mock": mock or llm.is_mock_mode(),
        "max_rounds": MAX_ROUNDS,
    }}

    total_text: list[str] = []
    tool_call_count = 0
    round_no = 0
    call_fingerprints: dict[str, int] = {}
    seen_citations: dict[str, dict[str, Any]] = {}
    hit_limit = False
    status = "done"
    thinking = ThinkingFilter()

    for round_no in range(1, MAX_ROUNDS + 2):
        # 第 6 次循环是「建单收尾阶段」：只有已经拿到校验结论、且还没建过单时才进。
        is_draft_round = round_no > MAX_ROUNDS
        if is_draft_round:
            settle = await derive_state(session_id)
            if not settle["validation_id"] or settle["draft_id"]:
                # 5 轮用完，且没有「已校验未建单」这种可收尾的事 → 就是真的到上限了
                hit_limit = True
                status = "max_rounds"
                round_no -= 1
                break
            allowed_tools = DRAFT_ROUND_TOOLS
            yield {"event": "status", "data": {
                "stage": "drafting",
                "round": round_no,
                "text": "5 轮决策已用完，进入建单收尾阶段…",
            }}
        else:
            allowed_tools = skills.SKILLS[skill_name]["tools"]
            yield {"event": "status", "data": {
                "stage": "thinking",
                "round": round_no,
                "text": "正在分析问题…" if round_no == 1
                        else f"已获得新结果，继续决策（第 {round_no}/{MAX_ROUNDS} 轮）",
            }}

        step_text: list[str] = []
        pending: list[dict[str, Any]] = []

        # ---- 决策：问模型这一轮要做什么 ----
        try:
            async for chunk in llm.stream_with_tools(
                messages, tools.openai_tools(allowed_tools), mock=mock
            ):
                if chunk["type"] == "delta":
                    piece = thinking.feed(chunk["text"])
                    if piece:
                        step_text.append(piece)
                        total_text.append(piece)
                        yield {"event": "delta", "data": {"text": piece}}
                elif chunk["type"] == "tool_calls":
                    pending = chunk["calls"]
        except llm.LLMError as exc:
            await db.finish_run(run_id, "error",
                                int((time.perf_counter() - started) * 1000), str(exc))
            yield {"event": "error", "data": {"code": 3002, "message": str(exc)}}
            return

        decision_text = "".join(step_text)

        # 没有工具请求 = 这一轮就是最终回答，循环自然结束
        if not pending:
            break

        # 把「这一轮说的话 + 工具请求」记进上下文，否则下一轮它会重复要同样的工具
        messages.append({
            "role": "assistant",
            "content": decision_text or None,
            "tool_calls": [
                {"id": call["id"], "type": "function",
                 "function": {"name": call["name"], "arguments": call["arguments"] or "{}"}}
                for call in pending
            ],
        })

        for call in pending:
            name = call["name"]
            title = TOOL_TITLES.get(name, name)

            # 参数是模型生成的 JSON 字符串，一律当不可信输入解析
            try:
                args = json.loads(call["arguments"] or "{}")
                if not isinstance(args, dict):
                    raise ValueError("参数必须是 JSON 对象")
            except (json.JSONDecodeError, ValueError) as exc:
                payload = {"ok": False, "error": {
                    "code": "BAD_ARGS", "message": f"工具参数不是合法 JSON：{exc}",
                    "next_hint": "请重新生成参数，确保是合法 JSON 对象"}}
                yield {"event": "tool_result", "data": {
                    "name": name, "title": title, "ok": False,
                    "summary": payload["error"]["message"]}}
                await db.log_step(run_id, session_id, round_no, "tool", name, {}, 0,
                                  False, "BAD_ARGS", payload["error"]["message"], decision_text)
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": _tool_content(payload)})
                continue

            # 重复调用保护
            fingerprint = name + json.dumps(args, ensure_ascii=False, sort_keys=True)
            call_fingerprints[fingerprint] = call_fingerprints.get(fingerprint, 0) + 1
            if call_fingerprints[fingerprint] > MAX_SAME_CALL:
                payload = {"ok": False, "error": {
                    "code": "TOOL_LOOP", "message": "同样的调用已经重复多次，结果不会变化",
                    "next_hint": "请换一个思路：要么换参数，要么调用 escalate_to_human"}}
                yield {"event": "tool_result", "data": {
                    "name": name, "title": title, "ok": False,
                    "summary": payload["error"]["message"]}}
                await db.log_step(run_id, session_id, round_no, "tool", name, args, 0,
                                  False, "TOOL_LOOP", payload["error"]["message"], decision_text)
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": _tool_content(payload)})
                continue

            tool_call_count += 1
            if tool_call_count > MAX_TOOL_CALLS:
                await db.finish_run(run_id, "capped",
                                    int((time.perf_counter() - started) * 1000))
                yield {"event": "error", "data": {
                    "code": 3004, "message": "本轮工具调用次数已达上限，请把问题拆小一些再问"}}
                return

            yield {"event": "tool_call", "data": {
                "name": name, "title": title, "args": args,
                "round": round_no, "step": tool_call_count}}

            # ==============================================================
            # 强制中断：写操作必须先拿到真人确认，才能执行。
            # 这段是整个进程里「唯一」能执行 create_dispute_draft 的地方——
            # 模型无论怎么催、提示词怎么写，都绕不过下面这个 await。
            # ==============================================================
            if name == "create_dispute_draft":
                # 先做纯参数预检：不合规的草稿不要拿去打扰人。
                # 员工的时间比一次重试贵得多，而且这时确认完也会被工具拒绝。
                bad = tools.precheck(name, args)
                if bad:
                    summary = summarize(name, bad)
                    await db.log_step(run_id, session_id, round_no, "tool", name, args,
                                      0, False, bad["error"]["code"], summary, decision_text)
                    yield {"event": "tool_result", "data": {
                        "name": name, "title": title, "ok": False, "summary": summary}}
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": _tool_content(bad)})
                    continue

                confirm_id = f"cf_{run_id}_{int(time.time() * 1000) % 10**7}"
                item = PendingConfirm(
                    id=confirm_id, session_id=session_id, user_id=user_id,
                    tool=name, args=args,
                )
                _CONFIRMS[confirm_id] = item
                await db.set_run_status(run_id, "awaiting_confirm")

                validation = await db.get_validation(str(args.get("validation_id", "")))

                yield {"event": "confirm_open", "data": {
                    "confirm_id": confirm_id,
                    "run_id": run_id,
                    "tool": name,
                    "bill_no": (validation or {}).get("bill_no") or args.get("bill_no"),
                    "dispute_type": args.get("dispute_type"),
                    "amount": args.get("amount"),
                    "evidence": args.get("evidence") or [],
                    "issues": (validation or {}).get("issues") or [],
                    "total_delta": (validation or {}).get("total_delta"),
                    "timeout_sec": CONFIRM_TIMEOUT,
                }}

                # 「流式发给员工确认」：正文按小片推，卡片像打字机一样展开，
                # 员工能边看边判断，而不是一次性糊一大段再问要不要确认。
                statement = str(args.get("statement") or "")
                for offset in range(0, len(statement), 10):
                    yield {"event": "confirm_delta", "data": {
                        "confirm_id": confirm_id, "text": statement[offset:offset + 10]}}
                    await asyncio.sleep(0.012)
                yield {"event": "confirm_ready", "data": {
                    "confirm_id": confirm_id, "char_count": len(statement)}}

                began = time.perf_counter()
                try:
                    decision = await _await_confirmation(item)
                finally:
                    # 连接被断开（用户关页面/点停止）时协程会被取消，
                    # 这里同步清掉登记项，避免留下一个永远等不到的确认请求。
                    _CONFIRMS.pop(confirm_id, None)
                waited_ms = int((time.perf_counter() - began) * 1000)

                if decision == "confirm":
                    merged = dict(args)
                    if item.statement:                  # 员工改过正文就用改后的
                        merged["statement"] = item.statement
                    payload = await tools.execute(name, merged, ctx={
                        "session_id": session_id, "user_id": user_id,
                        "step": round_no,
                    })
                    summary = summarize(name, payload)
                    ok = bool(payload.get("ok"))
                    err = (payload.get("error") or {}).get("code")
                elif decision == "timeout":
                    payload = {"ok": False, "error": {
                        "code": "CONFIRM_TIMEOUT",
                        "message": f"等待人工确认超时（{CONFIRM_TIMEOUT} 秒），草稿未创建",
                        "next_hint": "告知用户草稿未创建；如仍需建单，请让用户重新发起"}}
                    summary, ok, err = "等待确认超时，未建单", False, "CONFIRM_TIMEOUT"
                else:
                    payload = {"ok": False, "error": {
                        "code": "HUMAN_CANCELLED", "message": "员工取消了本次建单",
                        "next_hint": "不要再次尝试建单，改为询问用户需要调整什么"}}
                    summary, ok, err = "员工取消建单", False, "HUMAN_CANCELLED"

                _CONFIRMS.pop(confirm_id, None)
                await db.set_run_status(run_id, "running")
                # 人工等待本身也是一条可分析的数据：谁在犹豫、犹豫多久
                await db.log_step(run_id, session_id, round_no, "confirm",
                                  "human_confirm",
                                  {"confirm_id": confirm_id, "decision": decision},
                                  waited_ms, decision == "confirm", err,
                                  f"{summary}（人工确认耗时 {waited_ms}ms）", decision_text)

                yield {"event": "confirm_closed", "data": {
                    "confirm_id": confirm_id, "decision": decision,
                    "waited_ms": waited_ms}}

                yield {"event": "tool_result", "data": {
                    "name": name, "title": title, "ok": ok,
                    "summary": summary, "duration_ms": waited_ms}}

                if ok:
                    draft = payload["data"]
                    yield {"event": "draft", "data": {
                        "draft_id": draft.get("draft_id"),
                        "bill_no": draft.get("bill_no"),
                        "amount": draft.get("amount"),
                        "dispute_type": draft.get("dispute_type"),
                        "statement": draft.get("statement"),
                        "status": draft.get("status"),
                    }}

                await db.log_step(run_id, session_id, round_no, "tool", name, args,
                                  waited_ms, ok, err, summary, decision_text)
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": _tool_content(payload)})
                continue

            # ---- 执行：普通只读工具，直接跑 ----
            begin = time.perf_counter()
            # 轨迹由 tools.execute 自己落库（谁执行谁记账），这里只计时与写步骤日志
            payload = await tools.execute(name, args, {
                "session_id": session_id, "user_id": user_id, "step": round_no,
            })
            duration_ms = int((time.perf_counter() - begin) * 1000)
            summary = summarize(name, payload)

            await db.log_step(
                run_id, session_id, round_no, "tool", name, args, duration_ms,
                bool(payload.get("ok")), (payload.get("error") or {}).get("code"),
                summary, decision_text,
            )

            yield {"event": "tool_result", "data": {
                "name": name, "title": title, "ok": bool(payload.get("ok")),
                "summary": summary, "duration_ms": duration_ms}}

            # 检索结果顺手推给前端做「引用来源」
            if name == "search_policy" and payload.get("ok"):
                for citation in payload["data"].get("citations", []):
                    seen_citations[citation["citation_id"]] = citation
                if seen_citations:
                    yield {"event": "sources", "data": {
                        "citations": list(seen_citations.values())}}

            # ---- 回填：把工具结果塞回上下文，模型据此进入下一轮决策 ----
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": _tool_content(payload)})

            # 模型可以中途换能力包：换了就刷新工具白名单与系统提示
            if name == "load_skill" and payload.get("ok"):
                new_skill = payload["data"]["skill"]
                if new_skill != skill_name:
                    skill_name = new_skill
                    messages[0]["content"] = skills.build_system_prompt(skill_name, state)
                    yield {"event": "state", "data": {
                        "run_id": run_id,
                        "skill": skill_name,
                        "skill_title": skills.SKILLS[skill_name]["title"],
                        "stage": state["state"],
                        "tools": skills.SKILLS[skill_name]["tools"],
                    }}
    else:
        # 走完了所有轮（含建单收尾轮）仍想要工具
        if round_no > MAX_ROUNDS:
            # 收尾轮已执行完建单，只是模型还想继续折腾：正常结束，让下面补一段总结
            hit_limit = False
            status = "done"
        else:
            hit_limit = True
            status = "max_rounds"

    # 两种收尾：只调工具没说话 / 轮次用尽（流程没走完）
    need_summary = hit_limit or not "".join(total_text).strip()
    if need_summary and not (mock or llm.is_mock_mode()):
        hint = ("请基于以上工具结果，用中文给出结论、依据与下一步。"
                if not hit_limit else
                f"本轮已达 {MAX_ROUNDS} 轮上限。请基于目前已获得的结果给出阶段性结论："
                "说明已经查到什么、还差哪一步、建议用户下一步怎么做，不要编造未取得的数据。")
        messages.append({"role": "user", "content": hint})
        try:
            async for chunk in llm.stream_with_tools(messages, tools=None, mock=False):
                if chunk["type"] == "delta":
                    piece = thinking.feed(chunk["text"])
                    if piece:
                        total_text.append(piece)
                        yield {"event": "delta", "data": {"text": piece}}
        except llm.LLMError as exc:
            await db.finish_run(run_id, "error",
                                int((time.perf_counter() - started) * 1000), str(exc))
            yield {"event": "error", "data": {"code": 3002, "message": str(exc)}}
            return

    tail = thinking.flush()
    if tail.strip():
        total_text.append(tail)
        yield {"event": "delta", "data": {"text": tail}}

    final_state = await derive_state(session_id)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    await db.finish_run(run_id, status, elapsed_ms)

    yield {"event": "done", "data": {
        "run_id": run_id,
        "elapsed_ms": elapsed_ms,
        "rounds": round_no,
        "tool_calls": tool_call_count,
        "skill": skill_name,
        "stage": final_state["state"],
        "citation_count": len(seen_citations),
        "draft_id": final_state["draft_id"],
        "status": status,
    }}
