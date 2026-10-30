"""
tools.py —— 智能体的工具层（Function Calling 规范 + 服务端闸门）

三条铁律
--------
1. **事实与算术不在模型手里**：运单、账单、价目由 domain.py 提供，
   运费也只由 domain.compute_freight 算。模型只能调用、不能篡改。
2. **顺序由服务端保证**：每个有前置条件的工具都会回查本会话的调用轨迹，
   少一步就返回 409 语义的可恢复错误，并在 next_hint 里说清下一步做什么。
   提示词只是第三道保险，不是唯一防线。
3. **写入口只有一个**：create_dispute_draft 必须持有 validate_invoice 发的
   validation_id，且结论不能是 pass、金额必须等于校验差额。
   提交（submit）不在这里——它根本不在模型的工具集里。

工具返回统一信封
----------------
    成功: {"ok": true,  "data": {...}}
    失败: {"ok": false, "error": {"code": ..., "message": ..., "next_hint": ...}}

错误码设计成「模型看得懂、能自救」：NOT_FOUND 让它去问用户，
PRECONDITION 让它去补上一步，VERDICT_PASS 让它换结论而不是硬闯。
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

import db
import domain
from rag_service import kb_service, to_citations

MAX_TOOL_STEPS = 10         # 一次提问最多几轮工具调用，防止死循环
WAYBILL_PATTERN = re.compile(r"^YD\d{10}$")


class ToolError(Exception):
    """可恢复的工具错误：带错误码与下一步建议，模型据此自我纠正。"""

    def __init__(self, code: str, message: str, next_hint: str = ""):
        self.code = code
        self.message = message
        self.next_hint = next_hint
        super().__init__(message)


# ---------------------------------------------------------------------------
# 参数校验：手写而不是引 jsonschema，报错信息才能写成模型能懂的中文
# ---------------------------------------------------------------------------

def _as_str(args: dict[str, Any], key: str, *, required: bool = True,
            max_len: int | None = None, pattern: re.Pattern[str] | None = None,
            hint: str = "") -> str | None:
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ToolError("BAD_ARGS", f"缺少必填参数 {key}", hint or f"请补上 {key} 后重试")
        return None
    if not isinstance(value, str):
        raise ToolError("BAD_ARGS", f"参数 {key} 必须是字符串", f"请把 {key} 改成字符串")
    value = value.strip()
    if max_len and len(value) > max_len:
        raise ToolError("BAD_ARGS", f"参数 {key} 超过 {max_len} 字",
                        f"请把 {key} 压缩到 {max_len} 字以内")
    if pattern and not pattern.match(value):
        raise ToolError("BAD_ARGS", f"参数 {key} 格式不正确：{value}",
                        hint or "运单号形如 YD2026100101")
    return value


def _as_str_list(args: dict[str, Any], key: str, *, required: bool = True,
                 max_items: int = 20, hint: str = "") -> list[str]:
    value = args.get(key)
    if value is None:
        if required:
            raise ToolError("BAD_ARGS", f"缺少必填参数 {key}", hint)
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ToolError("BAD_ARGS", f"参数 {key} 必须是字符串数组", hint)
    cleaned = [v.strip() for v in value if v and v.strip()]
    if required and not cleaned:
        raise ToolError("BAD_ARGS", f"参数 {key} 不能为空", hint)
    if len(cleaned) > max_items:
        raise ToolError("BAD_ARGS", f"参数 {key} 最多 {max_items} 项", "请分批处理")
    return cleaned


def _as_number(args: dict[str, Any], key: str, *, required: bool = True) -> float | None:
    value = args.get(key)
    if value is None or value == "":
        if required:
            raise ToolError("BAD_ARGS", f"缺少必填参数 {key}", f"请补上 {key}")
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError) as exc:
        raise ToolError("BAD_ARGS", f"参数 {key} 必须是数字", "金额请传数字，例如 4169.61") from exc


# ---------------------------------------------------------------------------
# 调用轨迹查询：前置条件全靠它判定，所以必须先落库再判断
# ---------------------------------------------------------------------------

async def _calls(ctx: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return await db.list_tool_calls(ctx["session_id"], name=name, limit=100)


def _data_of(call: dict[str, Any]) -> dict[str, Any] | None:
    result = call.get("result") or {}
    return result.get("data") if result.get("ok") else None


async def _fetched_waybills(ctx: dict[str, Any]) -> set[str]:
    """本会话里已经成功取过的运单号。"""
    out: set[str] = set()
    for call in await _calls(ctx, "get_waybill"):
        data = _data_of(call)
        if data and data.get("waybill_no"):
            out.add(data["waybill_no"])
    return out


async def _computed_waybills(ctx: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """本会话里已经算过的运单 → 计算结果。"""
    out: dict[str, dict[str, Any]] = {}
    for call in await _calls(ctx, "calc_expected_freight"):
        data = _data_of(call)
        for item in (data or {}).get("results", []):
            if item.get("waybill_no"):
                out[item["waybill_no"]] = item
    return out


async def _session_citations(ctx: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """本会话检索到的制度片段 → citation_id 索引，用来验真。"""
    out: dict[str, dict[str, Any]] = {}
    for call in await _calls(ctx, "search_policy"):
        data = _data_of(call)
        for citation in (data or {}).get("citations", []):
            if citation.get("citation_id"):
                out[citation["citation_id"]] = citation
    return out


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

async def _tool_search_policy(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    query = _as_str(args, "query", max_len=200,
                   hint="用业务词检索，例如「计费争议提出时限」")
    top_k = int(args.get("top_k") or 3)
    top_k = max(1, min(top_k, 5))

    if not kb_service.ready:
        raise ToolError("KB_NOT_READY", "制度知识库尚未就绪",
                        "请告知用户稍后再试，不要凭印象回答")

    results = await kb_service.search(query, top_k=top_k)
    citations = to_citations(results)
    for citation in citations:
        # citation_id 由「文件 + 章节 + 偏移」哈希得到：同一片段永远同一个 id，
        # validate_invoice 据此验真（模型无法凭空编一个存在过的 id）。
        # 用 md5 而不是内置 hash()，后者带进程随机盐，重启后 id 会变。
        raw = f"{citation['file']}|{citation['section']}|{citation['offset']}"
        citation["citation_id"] = "c_" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]
    return {"query": query, "citations": citations}


async def _tool_get_waybill(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    waybill_no = _as_str(args, "waybill_no", pattern=WAYBILL_PATTERN,
                         hint="运单号形如 YD2026100101，请向用户确认后重试")
    waybill = domain.get_waybill(waybill_no)
    if waybill is None:
        raise ToolError("NOT_FOUND", f"系统中没有运单 {waybill_no}",
                        "请问用户核对运单号，或让用户提供运单截图；不要编造运单信息")
    return waybill


async def _tool_get_bill_lines(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    bill_no = _as_str(args, "bill_no", max_len=40,
                      hint="账单号形如 B202610-SF-001")
    bill = domain.get_bill(bill_no)
    if bill is None:
        raise ToolError(
            "NOT_FOUND", f"系统中没有账单 {bill_no}",
            "可向用户确认账单号；当前演示环境可用账单：" + "、".join(domain.BILLS),
        )
    return bill


async def _tool_get_rate_card(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    carrier = _as_str(args, "carrier", max_len=40)
    lane = _as_str(args, "lane", max_len=40, hint="线路写成「上海-成都」这种形式")
    on_date = _as_str(args, "on_date", required=False)
    rate, day, fallback = domain.resolve_rate_card(carrier, lane, on_date)
    if rate is None:
        raise ToolError("NOT_FOUND", f"{carrier} {lane} 没有可用的合同价目",
                        "请确认承运商与线路；如确实没有合同，应升级人工而不是自行估价")
    return {"rate_card": rate, "quoted_for": day, "fallback_version": fallback}


async def _tool_calc_expected_freight(args: dict[str, Any],
                                      ctx: dict[str, Any]) -> dict[str, Any]:
    waybill_nos = _as_str_list(args, "waybill_nos", max_items=20,
                               hint="传入运单号数组，例如 [\"YD2026100101\"]")

    fetched = await _fetched_waybills(ctx)
    missing = [no for no in waybill_nos if no.upper() not in fetched]
    if missing:
        raise ToolError(
            "PRECONDITION", "这些运单还没有取过事实数据："
            + "、".join(missing),
            "先调用 get_waybill 获取运单重量与体积，再计算期望运费",
        )

    results = []
    for no in waybill_nos:
        waybill = domain.get_waybill(no)
        if waybill is None:
            raise ToolError("NOT_FOUND", f"系统中没有运单 {no}", "请先核对运单号")
        rate, day, fallback = domain.resolve_rate_card(waybill["carrier"], waybill["lane"])
        if rate is None:
            raise ToolError("NOT_FOUND", f"{waybill['carrier']} {waybill['lane']} 无合同价目",
                            "缺少价目无法核定，应升级人工")
        computed = domain.compute_freight(waybill, rate)
        computed["rate_quoted_for"] = day
        computed["fallback_version"] = fallback
        results.append(computed)

    return {"results": results, "count": len(results)}


async def _tool_validate_invoice(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    bill_no = _as_str(args, "bill_no", max_len=40)
    citation_ids = _as_str_list(args, "citation_ids", max_items=10,
                                hint="citation_ids 只能取自 search_policy 的返回")

    # 前置 1：账单必须先查过
    if not any(_data_of(c) and _data_of(c).get("bill_no") == bill_no.upper()
               for c in await _calls(ctx, "get_bill_lines")):
        raise ToolError("PRECONDITION", f"还没有读取账单 {bill_no} 的明细",
                        "先调用 get_bill_lines 拿到逐行金额")

    bill = domain.get_bill(bill_no)
    if bill is None:
        raise ToolError("NOT_FOUND", f"系统中没有账单 {bill_no}", "请先确认账单号")

    # 前置 2：账单里每张运单都要先算过期望运费
    computed = await _computed_waybills(ctx)
    bill_waybills = sorted({line["waybill_no"] for line in bill["lines"]})
    not_computed = [no for no in bill_waybills if no not in computed]
    if not_computed:
        raise ToolError(
            "PRECONDITION",
            "这些运单还没有计算期望运费：" + "、".join(not_computed),
            "先调用 calc_expected_freight（可一次传多个运单号），再执行核对",
        )

    # 前置 3：引用的制度片段必须真实存在于本会话的检索结果里
    known = await _session_citations(ctx)
    unknown = [cid for cid in citation_ids if cid not in known]
    if unknown:
        raise ToolError(
            "PRECONDITION", "这些制度引用不存在于本会话的检索结果：" + "、".join(unknown),
            "先调用 search_policy 检索条款，再用它返回的 citation_id",
        )

    result = domain.validate_bill(bill["bill_no"])
    rule_version = computed[bill_waybills[0]].get("rule_version", "-")

    record = await db.create_validation(
        session_id=ctx["session_id"], user_id=ctx.get("user_id"),
        bill_no=result["bill_no"], rule_version=rule_version,
        verdict=result["verdict"], total_delta=result["total_delta"],
        issues=result["issues"], citation_ids=citation_ids, ttl_minutes=30,
    )

    return {
        "validation_id": record["id"],
        "bill_no": result["bill_no"],
        "verdict": result["verdict"],
        "total_delta": result["total_delta"],
        "issues": result["issues"],
        "checked_lines": result["checked_lines"],
        "citations": [known[cid] for cid in citation_ids],
        "rule_version": rule_version,
        "expires_at": record["expires_at"],
        "next_step": ("存在多收，可调用 create_dispute_draft 起草争议单"
                      if result["verdict"] == "fail"
                      else "无多收，不能发起争议单"),
    }


async def _tool_create_dispute_draft(args: dict[str, Any],
                                     ctx: dict[str, Any]) -> dict[str, Any]:
    validation_id = _as_str(args, "validation_id", max_len=40,
                            hint="必须是 validate_invoice 返回的 validation_id")
    dispute_type = _as_str(args, "dispute_type", max_len=20)
    allowed_types = ["计费差异", "重复计费", "时效违约", "货损", "其他"]
    if dispute_type not in allowed_types:
        raise ToolError("BAD_ARGS", f"争议类型不在允许范围：{dispute_type}",
                        "可选：" + "、".join(allowed_types))
    amount = _as_number(args, "amount")
    statement = _as_str(args, "statement", max_len=500,
                        hint="用一两句话说明争议事实与依据，不超过 500 字")
    evidence = _as_str_list(args, "evidence", required=False, max_items=10)

    validation = await db.get_validation(validation_id)
    if validation is None:
        raise ToolError("BAD_ARGS", "校验凭证不存在",
                        "请重新调用 validate_invoice 获取新的 validation_id")
    if validation["session_id"] != ctx["session_id"]:
        raise ToolError("FORBIDDEN", "该校验凭证不属于当前会话",
                        "请在本会话内重新核对账单")
    if validation["draft_id"]:
        raise ToolError("VALIDATION_CONSUMED", "该次校验已经生成过草稿",
                        "如需再次起草，请重新执行 validate_invoice")
    expires_at = datetime.fromisoformat(validation["expires_at"])
    if datetime.now(timezone.utc).astimezone() > expires_at:
        raise ToolError("VALIDATION_EXPIRED", "校验凭证已过期（有效期 30 分钟）",
                        "请重新执行 validate_invoice")
    if validation["verdict"] == "pass":
        raise ToolError(
            "VERDICT_PASS",
            "该账单核对结果为通过，没有可主张的差额，不能创建争议单",
            "请如实告知用户账单无误；若用户仍怀疑，建议升级人工复核，不要强行建单",
        )
    if abs(amount - float(validation["total_delta"])) > 0.01:
        raise ToolError(
            "AMOUNT_MISMATCH",
            f"起草金额 {amount} 与校验差额 {validation['total_delta']} 不一致",
            f"金额只能是 {validation['total_delta']}（校验差额），请按实际差额填写",
        )

    draft = await db.create_draft(
        session_id=ctx["session_id"], user_id=ctx["user_id"],
        validation_id=validation_id, bill_no=validation["bill_no"],
        dispute_type=dispute_type, amount=amount, statement=statement,
        evidence=evidence,
    )
    await db.mark_validation_drafted(validation_id, draft["id"])

    return {
        "draft_id": draft["id"],
        "status": draft["status"],
        "bill_no": draft["bill_no"],
        "amount": draft["amount"],
        "dispute_type": draft["dispute_type"],
        "statement": draft["statement"],
        "evidence": draft["evidence"],
        "requires_human_confirmation": True,
        "note": "草稿已生成，必须由员工在页面点击「确认提交」才会真正提交；"
                "你没有提交权限，也不得声称已提交",
    }


async def _tool_escalate_to_human(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    reason = _as_str(args, "reason", max_len=40)
    summary = _as_str(args, "summary", max_len=300,
                      hint="用一两句话说明为什么要人工介入")
    return {
        "escalated": True,
        "reason": reason,
        "summary": summary,
        "note": "已登记人工介入，请在回答中告知用户已转结算经理，不要继续尝试自动处理",
    }


async def _tool_load_skill(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    # 延迟导入避免循环依赖（skills 会引用工具名常量）
    import skills

    name = _as_str(args, "name", max_len=40)
    skill = skills.get_skill(name)
    if skill is None:
        raise ToolError("NOT_FOUND", f"没有名为 {name} 的 skill",
                        "可选：" + "、".join(skills.SKILLS))
    return {
        "skill": skill["name"],
        "title": skill["title"],
        "allowed_tools": skill["tools"],
        "instructions": skill["instructions"],
        "note": "下一轮对话起将按该 skill 的工具白名单执行",
    }


# ---------------------------------------------------------------------------
# 注册表：schema 给模型看，handler 给服务端执行
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, Callable[[dict[str, Any], dict[str, Any]],
                              Coroutine[Any, Any, dict[str, Any]]]] = {
    "search_policy": _tool_search_policy,
    "get_waybill": _tool_get_waybill,
    "get_bill_lines": _tool_get_bill_lines,
    "get_rate_card": _tool_get_rate_card,
    "calc_expected_freight": _tool_calc_expected_freight,
    "validate_invoice": _tool_validate_invoice,
    "create_dispute_draft": _tool_create_dispute_draft,
    "escalate_to_human": _tool_escalate_to_human,
    "load_skill": _tool_load_skill,
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "search_policy": {
        "name": "search_policy",
        "description": (
            "检索公司制度条款。任何涉及规则、时限、责任划分、费用标准的回答，"
            "都必须先调用本工具。返回的 citation_id 是后续校验与争议单的唯一合法引用来源。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索语句，用业务词，如「计费争议提出时限」"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 5, "description": "返回片段数，默认 3"},
            },
            "required": ["query"],
        },
    },
    "get_waybill": {
        "name": "get_waybill",
        "description": (
            "按运单号读取运单事实（承运商、线路、实重、体积、签收时间）。"
            "禁止凭记忆或推测填写任何运单字段；查不到就向用户要单号，不要编造。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "waybill_no": {"type": "string", "pattern": "^YD\\d{10}$",
                               "description": "运单号，形如 YD2026100101"},
            },
            "required": ["waybill_no"],
        },
    },
    "get_bill_lines": {
        "name": "get_bill_lines",
        "description": "读取承运商账单及其逐行计费明细（金额、备注）。核对账单前必须先调用。",
        "parameters": {
            "type": "object",
            "properties": {"bill_no": {"type": "string", "description": "账单号，形如 B202610-SF-001"}},
            "required": ["bill_no"],
        },
    },
    "get_rate_card": {
        "name": "get_rate_card",
        "description": (
            "读取某承运商某线路在指定日期生效的合同价目（单价、抛比、燃油费率、最低收费）。"
            "只有拿到价目才允许计算运费。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "carrier": {"type": "string", "description": "承运商名称，如 顺丰速运"},
                "lane": {"type": "string", "description": "线路，如 上海-成都"},
                "on_date": {"type": "string", "description": "计费发生日 YYYY-MM-DD，可省略"},
            },
            "required": ["carrier", "lane"],
        },
    },
    "calc_expected_freight": {
        "name": "calc_expected_freight",
        "description": (
            "按合同价目计算应收运费（含体积重、抛比、燃油附加费、最低收费的完整推导）。"
            "前置条件：这些运单必须已经用 get_waybill 取过事实。"
            "你不得自行乘算金额，所有数字都必须来自本工具的返回。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "waybill_nos": {"type": "array", "items": {"type": "string"},
                                "description": "运单号数组，可一次传多张"},
            },
            "required": ["waybill_nos"],
        },
    },
    "validate_invoice": {
        "name": "validate_invoice",
        "description": (
            "逐行核对账单并生成校验结论。前置条件（缺一不可）："
            "① 已用 get_bill_lines 读取该账单；② 账单涉及的每张运单都已 calc_expected_freight；"
            "③ citation_ids 必须原样取自本会话 search_policy 的返回。"
            "结论（pass/fail/warn）由服务端规则产生，你不能传入或修改结论。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "bill_no": {"type": "string"},
                "citation_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                                 "description": "支撑判定所依据的制度片段 id"},
                "rule_version": {"type": "string", "description": "采用的价目版本，可省略"},
            },
            "required": ["bill_no", "citation_ids"],
        },
    },
    "create_dispute_draft": {
        "name": "create_dispute_draft",
        "description": (
            "创建争议草稿。必须先持有 validate_invoice 返回的 validation_id（30 分钟内有效），"
            "且结论为 fail 或 warn；结论为 pass 时一律拒绝。"
            "amount 必须等于校验差额，不得自行调整。"
            "本工具只产出草稿，提交必须由员工在页面确认，你没有提交能力。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "validation_id": {"type": "string", "description": "只能原样使用 validate_invoice 的返回值"},
                "dispute_type": {"type": "string",
                                 "enum": ["计费差异", "重复计费", "时效违约", "货损", "其他"]},
                "amount": {"type": "number", "description": "争议金额，必须等于校验差额"},
                "statement": {"type": "string", "maxLength": 500,
                              "description": "争议事实与依据，含条款出处"},
                "evidence": {"type": "array", "items": {"type": "string"},
                             "description": "证据清单，如「运单 YD2026100101 磅单」"},
            },
            "required": ["validation_id", "dispute_type", "amount", "statement"],
        },
    },
    "escalate_to_human": {
        "name": "escalate_to_human",
        "description": (
            "转人工。证据不足、金额超出你的处理范围、规则之间冲突、工具连续失败、"
            "或用户要求人工时都必须调用，不要硬撑着给结论。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string",
                           "enum": ["证据不足", "金额超限", "规则冲突", "工具失败", "用户要求"]},
                "summary": {"type": "string", "description": "一两句话说明情况"},
            },
            "required": ["reason", "summary"],
        },
    },
    "load_skill": {
        "name": "load_skill",
        "description": (
            "切换到更适合当前任务的能力包。当用户在对话中提供了运单号/账单号，"
            "或问题从「查制度」变成「核对并处理具体账务」时，切换到 freight_dispute。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": ["policy_qa", "freight_dispute"]},
            },
            "required": ["name"],
        },
    },
}


def openai_tools(names: list[str]) -> list[dict[str, Any]]:
    """把工具名列表转成 OpenAI 兼容的 tools 参数结构。"""
    return [{"type": "function", "function": SCHEMAS[name]} for name in names if name in SCHEMAS]


async def execute(name: str, args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """
    执行一个工具，异常一律转成可恢复的错误信封。

    **先落库再返回**是有意为之：前置条件靠调用轨迹判定，
    如果由调用方（agent）负责记录，一旦漏记，闸门就会误判成「没做过」。
    谁执行谁记账，闸门与证据才是同一份数据。

    ctx 至少包含 session_id / user_id / step；没有 session_id 时只执行不记账
    （便于单测，但闸门相关工具会因此判定前置条件不满足，这正是正确行为）。
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": {"code": "UNKNOWN_TOOL",
                                       "message": f"没有名为 {name} 的工具",
                                       "next_hint": "请从工具列表中重新选择"}}

    begin = time.perf_counter()
    try:
        payload: dict[str, Any] = {"ok": True, "data": await handler(args or {}, ctx)}
    except ToolError as exc:
        payload = {"ok": False, "error": {"code": exc.code, "message": exc.message,
                                          "next_hint": exc.next_hint}}
    except Exception as exc:  # 工具内部 bug 也不该让整轮对话 500
        print(f"[工具异常] {name}: {exc!r}")
        payload = {"ok": False, "error": {"code": "TOOL_ERROR",
                                          "message": f"{name} 执行失败：{exc}",
                                          "next_hint": "可重试一次；连续失败请调用 escalate_to_human"}}
    duration_ms = int((time.perf_counter() - begin) * 1000)

    session_id = ctx.get("session_id")
    if session_id:
        try:
            await db.log_tool_call(
                session_id=session_id, user_id=ctx.get("user_id"),
                step=int(ctx.get("step") or 0), name=name, args=args or {},
                result=payload, ok=bool(payload.get("ok")),
                error_code=(payload.get("error") or {}).get("code"),
                duration_ms=duration_ms,
            )
        except Exception as exc:  # 记账失败不影响本次结果，但要留下日志
            print(f"[记账失败] {name}: {exc!r}")
    return payload
