"""
app.py —— FastAPI 员工问答入口

接口
----
POST   /api/chat                        提问（智能体：工具调用 + SSE 流式）
GET    /api/sessions                    我的会话列表
GET    /api/sessions/{id}/messages      会话历史（刷新页面恢复用）
GET    /api/disputes                    我名下的争议草稿
POST   /api/disputes/{id}/submit        人工确认提交（模型没有这个能力）
GET    /api/health                      健康检查（知识库 / 模型状态）

后台接口（仅管理员）
--------------------
GET    /admin/knowledge                 知识库管理页
GET    /admin/api/documents             文档列表 + 切片统计
POST   /admin/api/documents             上传文档（md/txt/pdf）并重建索引
DELETE /admin/api/documents/{name}      删除文档并重建索引
POST   /admin/api/documents/reindex     手动全量重建索引

错误处理约定
------------
1. 建流之前就失败的（参数错、会话不存在、无权限、知识库未就绪）
   → 直接返回 HTTP 4xx/5xx + JSON 信封 {code, message, request_id}。
2. 流开始之后才失败的（检索异常、模型超时）
   → HTTP 仍是 200，通过 `event: error` 下发同样的信封。
   这是 SSE 的固有限制：响应头发出后状态码就改不了了。

鉴权
----
JWT + HttpOnly Cookie。两种角色：
  employee：问答、看自己的会话
  admin   ：额外可进 /admin（知识库、提示词、审计）
页面请求未登录会跳 /login，接口请求返回 401 + JSON 信封。
不再使用 X-User 请求头 —— 那只要求知道用户名就能冒充。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import agent, auth, db, llm, ratelimit, skills
from .rag_service import (
    MAX_UPLOAD_BYTES,
    kb_service,
    seed_default_document,
)
from .utils.paths import PACKAGE_DIR, PROJECT_ROOT, KNOWLEDGE_DIR

# 启动时从同目录的 .env 读取模型配置（密钥不写进代码）。
# override=False：真实环境变量优先，方便容器/CI 覆盖。
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:  # 没装 python-dotenv 时退化为只读系统环境变量
    print("[启动] 未安装 python-dotenv，跳过 .env 加载")

STATIC_DIR = PACKAGE_DIR / "static"
TEMPLATE_DIR = PACKAGE_DIR / "templates"
SEED_DOC = KNOWLEDGE_DIR / "discovery-report.md"

# Jinja2 模板：后台页面全部服务端渲染，不依赖前端框架
TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# ---------------------------------------------------------------------------
# 错误码表：code 与中文信息的对应关系集中在这里，前后端都以它为准
# ---------------------------------------------------------------------------
# 1001~1099 请求与鉴权；2001~2099 依赖不可用；3001~3099 流内执行异常
ERRORS: dict[int, str] = {
    1001: "问题不能为空",
    1002: "问题长度超过 2000 字，请分段提问",
    1003: "请求参数格式不正确",
    1004: "会话不存在或已被删除",
    1005: "无权访问该会话",
    1006: "未登录或登录已过期，请重新登录",
    1007: "不支持的文件类型，仅接受 .md / .markdown / .txt / .pdf",
    1008: "上传文件过大，请控制在 5MB 以内",
    1009: "用户名或密码不正确",
    1010: "该账号已被停用，请联系管理员",
    1011: "需要管理员权限",
    1012: "提示词不能为空",
    1013: "提示词过长，请控制在 2000 字以内",
    1014: "文件无法解析或疑似扫描件",
    1015: "索引重建失败，请查看服务日志",
    1016: "草稿不存在或无权操作",
    1017: "该草稿已提交，不能重复提交",
    1018: "确认请求已失效或无权确认",
    1019: "草稿正文不能超过 1200 字",
    1020: "提问太频繁，请稍后再试",
    1021: "今日问答次数已用完，请明天再来",
    1022: "当前排队人数较多，请稍后再试",
    1023: "提示词版本不存在，可能已被清理",
    1024: "保留天数需在 1~3650 之间",
    1025: "审计记录不存在",
    2001: "知识库尚未就绪，请稍后再试",
    2002: "尚未配置大模型，请联系管理员",
    3001: "检索失败，请重试",
    3002: "模型服务调用失败，请稍后重试",
    3003: "响应生成中断，请重新提问",
    3004: "本轮工具调用次数已达上限，请把问题拆小一些再问",
    9999: "服务内部错误，请稍后重试",
}


class ApiError(Exception):
    """业务异常：带错误码与 HTTP 状态，由统一处理器转成 JSON 信封。"""

    def __init__(self, code: int, message: str | None = None, status: int = 400):
        self.code = code
        self.message = message or ERRORS.get(code, "未知错误")
        self.status = status
        super().__init__(self.message)


def _envelope(code: int, message: str, request_id: str) -> dict[str, Any]:
    """统一的错误响应体，保证前端只需要处理一种结构。"""
    return {"code": code, "message": message, "request_id": request_id}


async def _retention_worker() -> None:
    """
    日志保留的后台任务：启动先清一次，之后每 24 小时一次。

    为什么要放在后台而不是请求里做：清理会锁表，遇到几十万条时要跑几秒，
    放在请求线程里会卡住所有 SSE 连接。启动时先跑一次，是因为长期停机的
    实例重新拉起来时，往往正是日志最需要清的时候。
    """
    while True:
        settings = await db.get_retention_settings()
        if settings["enabled"]:
            try:
                result = await db.purge_logs(settings["days"])
                deleted = sum(result["deleted"].values())
                if deleted:
                    print(f"[清理] 已删除 {deleted} 条超过 {settings['days']} 天的运行日志")
            except Exception as exc:  # 清理失败不能拖垮服务启动
                print(f"[清理] 失败：{exc!r}")
        # 顺手把过期确认收成 timeout：谁也没点、进程也重启过的那种记录，
        # 留在 pending 里会让审计页看起来像「一直有人在等」。
        try:
            expired = await db.expire_stale_confirms()
            if expired:
                print(f"[清理] 已标记 {expired} 条超时确认")
        except Exception as exc:  # noqa: BLE001
            print(f"[清理] 确认超时标记失败：{exc!r}")
        await asyncio.sleep(24 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时：建表 → 演示账号 → 默认提示词 → 准备文档 → 加载索引 → 清理旧日志。"""
    await db.init_db()
    # 限流参数从环境变量读取，默认开启；RATE_LIMIT_ENABLED=0 仅供本地调试
    ratelimit.limiter.apply_config(ratelimit.RateLimitConfig.from_env())
    created = await auth.ensure_demo_users()
    await auth.ensure_default_prompt()
    seed_default_document(SEED_DOC)     # 首次启动把阶段 1 的文档放进 docs/
    await kb_service.ensure_ready()
    stats = kb_service.stats()
    print(f"[启动] 知识库就绪={stats['ready']} 文档={stats['document_count']} "
          f"切片={stats['chunk_count']} 模型={llm.provider_info()['model']} "
          f"mock={llm.provider_info()['mock']}")
    if created:
        print(f"[启动] 已创建演示账号：{'、'.join(created)}")
    # 演示口令随代码发版，一旦暴露到内网就等于后台全开。启动时明确告警，
    # 比写在文档里等运维自己想起来更可靠。
    insecure = await auth.insecure_demo_accounts()
    if insecure:
        print(f"[安全] 以下账号仍在使用演示口令，共享环境部署前必须修改：{'、'.join(insecure)}")

    retention_task = asyncio.create_task(_retention_worker())
    # 停机期间到点的确认先收掉：它们已经没有等待者，不该再出现在待确认列表里。
    stale = await db.expire_stale_confirms()
    if stale:
        print(f"[启动] 已标记 {stale} 条停机期间超时的确认")
    yield
    # 关停时先取消后台任务，避免 uvicorn 等它睡完 24 小时才退出
    retention_task.cancel()
    with suppress(asyncio.CancelledError):
        await retention_task


app = FastAPI(title="物流费制度问答 · 员工入口", lifespan=lifespan)

# 静态资源（前端页面）
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# 全局异常处理：任何异常都转成中文错误信封，不把堆栈暴露给前端
# ---------------------------------------------------------------------------

def _is_api_request(request: Request) -> bool:
    """
    判断这个请求是不是接口调用（而不是页面浏览）。

    注意后台接口的路径是 /admin/api/...，不是 /api/...，
    所以不能只用 startswith("/api") 判断，否则接口报错时会给前端回一整页 HTML。
    """
    path = request.url.path
    return path.startswith("/api") or path.startswith("/admin/api")


@app.exception_handler(ApiError)
async def handle_api_error(request: Request, exc: ApiError):
    request_id = getattr(request.state, "request_id", "-")
    is_api = _is_api_request(request)

    # 页面请求的错误分流：
    #   401 未登录  → 跳登录页（带上返回地址）
    #   403 无权限  → 渲染说明页，而不是跳登录。
    #               否则已登录的员工会被送去登录页，登录后又被弹回来，白白让人困惑。
    if not is_api and exc.status == 401:
        target = quote(request.url.path)
        return RedirectResponse(f"/login?next={target}", status_code=303)
    if not is_api and exc.status == 403:
        return templates.TemplateResponse(
            request, "forbidden.html", {"message": exc.message}, status_code=403
        )

    return JSONResponse(
        status_code=exc.status,
        content=_envelope(exc.code, exc.message, request_id),
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(request: Request, exc: StarletteHTTPException):
    """FastAPI 内建异常（404、405 等）也统一成中文信封，保持接口一致。"""
    request_id = getattr(request.state, "request_id", "-")
    if _is_api_request(request):
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.status_code, _http_message(exc), request_id),
        )
    return await http_exception_handler(request, exc)


def _http_message(exc: StarletteHTTPException) -> str:
    mapping = {404: "接口或资源不存在", 405: "请求方法不被允许"}
    return mapping.get(exc.status_code, str(exc.detail))


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "-")
    print(f"[未捕获异常] {request_id}: {exc!r}")
    return JSONResponse(
        status_code=500,
        content=_envelope(9999, ERRORS[9999], request_id),
    )


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    """给每个请求打上 trace ID，出错时能对上日志。"""
    request.state.request_id = uuid.uuid4().hex[:12]
    response = await call_next(request)
    response.headers["X-Request-Id"] = request.state.request_id
    return response


# ---------------------------------------------------------------------------
# 鉴权：JWT（Cookie 或 Authorization: Bearer）
# ---------------------------------------------------------------------------

def require_api_user(request: Request) -> dict[str, Any]:
    """
    接口级鉴权：未登录返回 401 + 中文信封。

    不再信任 X-User 请求头 —— 那个只要知道用户名就能冒充，等于没有鉴权。
    """
    user = auth.user_from_request(request)
    if not user:
        raise ApiError(1006, "未登录或登录已过期，请重新登录", status=401)
    return user


def require_api_admin(request: Request) -> dict[str, Any]:
    """管理员专用接口：员工访问返回 403。"""
    user = require_api_user(request)
    if not auth.is_admin(user):
        raise ApiError(1011, status=403)
    return user


def require_page_admin(request: Request) -> dict[str, Any]:
    """
    后台页面鉴权。

    未登录/非管理员都会抛 401/403，由 handle_api_error 转成跳登录页——
    浏览器看到的是登录界面，而不是一段报错 JSON。
    """
    user = auth.user_from_request(request)
    if not user:
        raise ApiError(1006, "未登录或登录已过期，请重新登录", status=401)
    if not auth.is_admin(user):
        raise ApiError(1011, status=403)
    return user


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    session_id: str | None = Field(default=None, description="为空则新建会话")
    question: str = Field(default="", description="员工问题")
    # top_k / bm25_weight 保留兼容旧前端：检索现在发生在 search_policy 工具内部，
    # 走的是同一套默认参数，不再由请求体控制。
    top_k: int = Field(default=3, ge=1, le=10)
    bm25_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    mock: bool = Field(default=False, description="强制离线模拟，便于演示")


class ConfirmRequest(BaseModel):
    """员工对「建单前中断」的回应。"""
    confirm_id: str = Field(description="confirm_open 事件里给的确认编号")
    action: str = Field(default="confirm", description="confirm 或 cancel")
    statement: str | None = Field(default=None, description="员工改过的草稿正文，可省略")


class PromptRequest(BaseModel):
    """后台补充要求。允许空串，表示不追加任何补充。"""
    content: str = Field(default="", description="补充要求正文，纯文本")


# ---------------------------------------------------------------------------
# SSE 工具
# ---------------------------------------------------------------------------

def sse(event: str, data: dict[str, Any]) -> str:
    """按 SSE 规范拼一帧：event 行 + data 行 + 空行结尾。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# 核心接口：POST /api/chat
# ---------------------------------------------------------------------------

@app.post("/api/chat")
async def chat(
    payload: ChatRequest,
    request: Request,
    user: dict[str, Any] = Depends(require_api_user),
):
    """接收问题并返回 SSE 流。建流前的所有校验都在这里完成。"""
    user_id = user["id"]

    question = payload.question.strip()
    if not question:
        raise ApiError(1001, status=400)
    if len(question) > 2000:
        raise ApiError(1002, status=400)
    if not kb_service.ready:
        raise ApiError(2001, status=503)

    # 限流与配额：必须在「建会话、写消息」之前拦截，
    # 否则被限流的请求也会在数据库里留下一堆空会话。
    try:
        await ratelimit.limiter.check(user_id)
    except ratelimit.RateLimitExceeded as exc:
        raise ApiError(1020, exc.message, status=429) from exc
    except ratelimit.QuotaExceeded as exc:
        raise ApiError(1021, exc.message, status=429) from exc

    # 会话：没有就新建，有就校验归属
    if payload.session_id:
        session = await db.get_session(payload.session_id)
        if session is None:
            raise ApiError(1004, status=404)
        if session["user_id"] != user_id:
            raise ApiError(1005, status=403)
        session_id = session["id"]
    else:
        created = await db.create_session(user_id, title=question[:20])
        session_id = created["id"]

    # 先落库用户消息，再取历史——用 before_id 把它排除在上下文之外
    user_message_id = await db.add_message(session_id, "user", question)
    await db.touch_session(session_id, title=question[:20])

    history = await db.get_recent_history(session_id, before_id=user_message_id, rounds=6)

    async def event_stream() -> AsyncIterator[str]:
        """
        智能体事件流。

        先发 meta/status，再把 agent.run() 产出的事件逐条转成 SSE 帧。
        新增的 state / tool_call / tool_result / draft 四类事件，前端用来画步骤条与草稿卡片；
        老的 delta / sources / done / error 保持不变，打字机与引用卡片照旧工作。
        """
        started = asyncio.get_event_loop().time()
        collected: list[str] = []
        citations: list[dict[str, Any]] = []
        draft_id: str | None = None
        agent_stats: dict[str, Any] = {}

        yield sse("meta", {
            "session_id": session_id,
            "user_message_id": user_message_id,
            "trace_id": request.state.request_id,
            "mock": payload.mock or llm.is_mock_mode(),
        })
        yield sse("status", {"stage": "retrieving", "text": "正在理解问题…"})

        try:
            # 并发闸门：全局同时执行的 Agent 循环数有限，满了排队，超时明确报错。
            # 放在流内而不是入口处，是因为占位的时长就是整个执行周期。
            async with ratelimit.limiter.slot():
                ratelimit.limiter.consume(user_id)
                async for event in agent.run(
                    user_id=user_id, session_id=session_id, question=question,
                    history=history, mock=payload.mock,
                ):
                    name = event["event"]
                    data = event["data"]
                    if name == "delta":
                        collected.append(data.get("text", ""))
                    elif name == "sources":
                        citations = data.get("citations", [])
                    elif name == "draft":
                        draft_id = data.get("draft_id")
                    elif name == "done":
                        # agent 的 done 只用于内部统计，对外的 done 由这里统一发，
                        # 否则前端会收到两帧 done（一帧没有 message_id）。
                        agent_stats = data
                        continue
                    yield sse(name, data)
        except ratelimit.RateLimitExceeded as exc:
            # 并发排队超时：此时还没开始执行，不需要落错误消息
            yield sse("error", {"code": 1022, "message": exc.message})
            return
        except llm.LLMError as exc:
            print(f"[模型失败] {exc}")
            await db.add_message(session_id, "assistant", "".join(collected) or "（生成失败）",
                                 citations=citations, status="error")
            yield sse("error", {"code": 3002, "message": str(exc) or ERRORS[3002]})
            return
        except (asyncio.CancelledError, GeneratorExit):
            # 用户关页面 / 点停止：协程正在取消，不能 await，用守护线程尽力存下已生成内容
            pending = "".join(collected)
            if pending:
                db.add_message_nowait(session_id, "assistant", pending, citations)
            raise
        except Exception as exc:
            print(f"[智能体异常] {exc!r}")
            await db.add_message(session_id, "assistant", "".join(collected) or "（生成中断）",
                                 citations=citations, status="error")
            yield sse("error", {"code": 3003, "message": ERRORS[3003]})
            return

        answer = "".join(collected)
        assistant_id = await db.add_message(
            session_id, "assistant", answer, citations=citations,
            token_usage={"completion_chars": len(answer)},
        )
        elapsed_ms = int((asyncio.get_event_loop().time() - started) * 1000)
        yield sse("done", {
            "message_id": assistant_id,
            "elapsed_ms": elapsed_ms,
            "citation_count": len(citations),
            "draft_id": draft_id,
            "tool_calls": agent_stats.get("tool_calls", 0),
            "rounds": agent_stats.get("rounds", 0),
            "run_id": agent_stats.get("run_id"),
            "status": agent_stats.get("status"),
            "skill": agent_stats.get("skill"),
            "stage": agent_stats.get("stage"),
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # 反向代理别缓冲，否则流式失效
        },
    )


# ---------------------------------------------------------------------------
# 人工确认：智能体在 create_dispute_draft 之前挂起，等这个接口放行
# ---------------------------------------------------------------------------

@app.post("/api/agent/confirm")
async def confirm_agent_action(payload: ConfirmRequest,
                               user: dict[str, Any] = Depends(require_api_user)):
    """
    员工点了「确认建单」或「取消」。

    注意：这个接口本身不建单。它只是把 agent.run 里挂起的 await 唤醒，
    真正的建单动作仍然发生在那条 SSE 链路内部，所以工具轨迹、日志、
    事件顺序都是连续的——不会出现「接口建了单但对话流不知道」的撕裂状态。
    """
    action = payload.action.strip().lower()
    if action not in ("confirm", "cancel"):
        raise ApiError(1018, "action 只能是 confirm 或 cancel", status=400)

    statement = (payload.statement or "").strip() or None
    # 与 tools.STATEMENT_MAX 保持一致；这里多一道是为了不让超长文本进内存
    if statement and len(statement) > 1200:
        raise ApiError(1019, status=400)

    ok, reason = await agent.submit_confirmation(payload.confirm_id, user["id"], action, statement)
    if not ok:
        raise ApiError(1018, reason, status=409)
    return {
        "confirm_id": payload.confirm_id,
        "action": action,
        "message": "已确认，正在建单" if action == "confirm" else "已取消，未建单",
    }


@app.get("/api/agent/confirm/pending")
async def pending_confirms(session_id: str,
                           user: dict[str, Any] = Depends(require_api_user)):
    """
    这个会话里还没被处理的确认，供页面刷新后重建确认卡。

    以前确认只活在内存里，刷新页面就等于把这次中断作废；现在草稿正文和
    等待时限都在库里，重建出来的卡片仍然可以点——但点了之后是否还算数，
    仍由服务端按 expires_at 判定，前端拿到的 deadline 只是用于倒计时展示。
    """
    session = await db.get_session(session_id)
    if session is None:
        raise ApiError(1004, status=404)
    if session["user_id"] != user["id"]:
        raise ApiError(1005, status=403)

    rows = await agent.list_pending_confirms(session_id)
    now = time.time()
    items = []
    for row in rows:
        args = row.get("args") or {}
        validation = await db.get_validation(str(args.get("validation_id", "")))
        items.append({
            "confirm_id": row["id"],
            "run_id": row["run_id"],
            "tool": row["tool"],
            "bill_no": (validation or {}).get("bill_no") or args.get("bill_no"),
            "dispute_type": args.get("dispute_type"),
            "amount": args.get("amount"),
            "evidence": args.get("evidence") or [],
            "issues": (validation or {}).get("issues") or [],
            "total_delta": (validation or {}).get("total_delta"),
            "statement": row.get("statement") or args.get("statement") or "",
            "created_at": row["created_at"],
            "remain_sec": max(0, int(float(row["expires_at"]) - now)),
        })
    return {"confirms": items}


# ---------------------------------------------------------------------------
# 争议草稿：模型的产出到此为止，提交必须由人点
# ---------------------------------------------------------------------------
# 注意这里刻意不提供任何「提交草稿」的工具给模型 —— 它没有这个能力，
# 所以「AI 自己把钱要回来」在架构上就不可能发生，而不是靠提示词求它别做。

@app.get("/api/disputes")
async def my_disputes(user: dict[str, Any] = Depends(require_api_user)):
    """员工只能看到自己名下（自己会话里产生）的草稿。"""
    return {"drafts": await db.list_drafts(user["id"])}


@app.post("/api/disputes/{draft_id}/submit")
async def submit_dispute(draft_id: str, user: dict[str, Any] = Depends(require_api_user)):
    """
    人工确认提交。只有会话所属员工本人、且草稿仍是 draft 状态才能提交。

    这是整个系统里唯一能改变争议单状态的地方，而它只接受浏览器点击产生的请求。
    """
    ok, reason = await db.submit_draft(draft_id, user["id"])
    if not ok:
        draft = await db.get_draft(draft_id)
        if draft is None or draft["user_id"] != user["id"]:
            raise ApiError(1016, status=403)
        raise ApiError(1017, reason, status=409)
    draft = await db.get_draft(draft_id)
    return {"message": "已提交，争议单已进入结算复核队列", "draft": draft}


# ---------------------------------------------------------------------------
# 智能体运行轨迹：阶段 5（效果分析）的数据出口
# 现在先用它自证「每轮决策都记下来了」，后面做分析页直接读这两个接口。
# ---------------------------------------------------------------------------

@app.get("/api/agent/runs")
async def list_agent_runs(limit: int = 20, user: dict[str, Any] = Depends(require_api_user)):
    """我自己的执行记录。管理员可以另开一个视图看全员。"""
    return {"runs": await db.list_runs(user["id"], limit=min(limit, 100))}


@app.get("/api/agent/runs/{run_id}")
async def get_agent_run(run_id: str, user: dict[str, Any] = Depends(require_api_user)):
    """单次执行的逐轮明细：第几轮、调了什么、耗时多久、结果摘要。"""
    runs = await db.list_runs(user["id"], limit=100)
    hit = next((r for r in runs if r["id"] == run_id), None)
    if hit is None:
        raise ApiError(1004, "执行记录不存在", status=404)
    return {"run": hit, "steps": await db.list_steps(run_id)}


# ---------------------------------------------------------------------------
# 会话与历史
# ---------------------------------------------------------------------------

@app.get("/api/sessions")
async def get_sessions(user: dict[str, Any] = Depends(require_api_user)):
    return {"sessions": await db.list_sessions(user["id"])}


@app.get("/api/sessions/{session_id}/messages")
async def get_messages(session_id: str,
                       user: dict[str, Any] = Depends(require_api_user)):
    session = await db.get_session(session_id)
    if session is None:
        raise ApiError(1004, status=404)
    if session["user_id"] != user["id"]:
        raise ApiError(1005, status=403)
    return {
        "session_id": session_id,
        "title": session["title"],
        "messages": await db.get_messages(session_id),
    }


# ---------------------------------------------------------------------------
# 知识库管理：页面 + 接口（仅管理员）
# ---------------------------------------------------------------------------
# 上传/删除都会同步触发索引重建，因此这三个接口都比较慢（秒级），
# 前端必须给出「处理中」的反馈，否则用户会以为按钮没反应。

def _document_error(exc: ValueError) -> ApiError:
    """
    把知识库服务抛出的 ValueError 翻译成带错误码的 ApiError。

    服务层只管「哪里不对」，错误码在这里统一分配，
    避免服务层被迫依赖 HTTP 概念，也避免前端只能看到一段模糊的 400。
    """
    message = str(exc)
    if "过大" in message:
        return ApiError(1008, message, status=400)
    if "不支持的文件类型" in message:
        return ApiError(1007, message, status=400)
    if any(key in message for key in ("疑似扫描件", "无法解析", "加密", "未提取到", "pypdf")):
        return ApiError(1014, message, status=400)
    return ApiError(1003, message, status=400)


def _kb_payload() -> dict[str, Any]:
    """知识库列表接口的返回体：文档明细 + 汇总，前端一次拿全。"""
    stats = kb_service.stats()
    return {
        "documents": kb_service.documents_detail(),
        "stats": {
            "ready": stats["ready"],
            "document_count": stats["document_count"],
            "chunk_count": stats["chunk_count"],
            "error": stats["error"],
        },
    }


@app.get("/admin/knowledge")
async def admin_knowledge(request: Request,
                          user: dict[str, Any] = Depends(require_page_admin)):
    """知识库管理页：上传、删除、重建索引。"""
    ctx = _layout_ctx(request, user, "knowledge")
    ctx.update(_kb_payload())
    ctx["max_size_mb"] = 5
    ctx["accepted"] = ".md, .markdown, .txt, .pdf"
    return templates.TemplateResponse(request, "admin/knowledge.html", ctx)


@app.get("/admin/api/documents")
async def admin_list_documents(user: dict[str, Any] = Depends(require_api_admin)):
    return _kb_payload()


@app.post("/admin/api/documents")
async def admin_upload_document(
    file: UploadFile = File(...),
    user: dict[str, Any] = Depends(require_api_admin),
):
    """
    上传制度文档并重建索引。

    先看 file.size 再读内容：几百 MB 的文件如果整个读进内存，
    服务的内存曲线会直接顶到天花板。超限的连读都不读，直接拒。
    """
    filename = file.filename or ""
    if file.size and file.size > MAX_UPLOAD_BYTES:
        raise ApiError(1008, "上传文件过大，请控制在 5MB 以内", status=400)

    content = await file.read()
    if not content:
        raise ApiError(1003, "文件内容为空，请重新选择", status=400)

    try:
        result = await kb_service.save_document(filename, content)
    except ValueError as exc:
        raise _document_error(exc) from exc

    message = "已覆盖同名文档，索引已重建" if result["replaced"] else "上传成功，索引已重建"
    return {"message": message, **result, "stats": _kb_payload()["stats"]}


@app.delete("/admin/api/documents/{filename}")
async def admin_delete_document(
    filename: str,
    user: dict[str, Any] = Depends(require_api_admin),
):
    try:
        result = await kb_service.delete_document(filename)
    except FileNotFoundError as exc:
        raise ApiError(1004, str(exc), status=404) from exc
    except ValueError as exc:
        raise _document_error(exc) from exc
    return {"message": "已删除，索引已重建", **result, "stats": _kb_payload()["stats"]}


@app.post("/admin/api/documents/reindex")
async def admin_reindex(user: dict[str, Any] = Depends(require_api_admin)):
    """手动重建索引：换了向量模型、或怀疑索引与文件不一致时使用。"""
    try:
        stats = await kb_service.reindex()
    except Exception as exc:
        print(f"[重建失败] {exc!r}")
        raise ApiError(1015, status=500) from exc
    return {
        "message": f"重建完成，共 {stats['chunk_count']} 个切片",
        "stats": {
            "ready": stats["ready"],
            "document_count": stats["document_count"],
            "chunk_count": stats["chunk_count"],
            "error": stats["error"],
        },
        "documents": kb_service.documents_detail(),
    }


# ---------------------------------------------------------------------------
# 提示词模板：页面 + 接口（仅管理员）
# ---------------------------------------------------------------------------
# 这里维护的是「补充要求」，不是整段系统提示词。
# 硬性规则（金额只能来自工具、无凭证不建单等）写在 skills.py 里，不可配置——
# 让后台能改这些，等于给了管理员一个关掉风控的开关。

# 预览用的示例状态：让管理员看到补充要求拼进提示词后的真实位置。
# 用真实账单号，比 dummy 数据更容易看出效果。
_PROMPT_PREVIEW_STATE: dict[str, Any] = {
    "state": "validated",
    "bill_no": "B202610-SF-001",
    "waybills": ["YD2026100101"],
    "computed": ["YD2026100101"],
    "verdict": "fail",
    "total_delta": 4169.61,
}


def _prompt_payload(supplement: str, meta: dict[str, Any] | None) -> dict[str, Any]:
    """提示词页与接口共用的返回体，避免两处字段不一致。"""
    base = skills.build_system_prompt("freight_dispute", _PROMPT_PREVIEW_STATE, "")
    final = skills.build_system_prompt("freight_dispute", _PROMPT_PREVIEW_STATE, supplement)
    return {
        "content": supplement,
        "meta": meta,
        "default": auth.DEFAULT_SYSTEM_PROMPT,
        "max_chars": skills.MAX_SUPPLEMENT_CHARS,
        "base_preview": base,
        "final_preview": final,
        "delta_chars": len(final) - len(base),
    }


@app.get("/admin/prompt")
async def admin_prompt_page(request: Request,
                            user: dict[str, Any] = Depends(require_page_admin)):
    """提示词模板页：编辑补充要求、查看版本历史、恢复旧版本。"""
    meta = await db.get_setting_meta(auth.SYSTEM_PROMPT_KEY)
    supplement = (meta or {}).get("value") or ""
    ctx = _layout_ctx(request, user, "prompt")
    ctx.update(_prompt_payload(supplement, meta))
    ctx["revisions"] = await db.list_prompt_revisions(10)
    return templates.TemplateResponse(request, "admin/prompt.html", ctx)


@app.get("/admin/api/prompt")
async def admin_get_prompt(user: dict[str, Any] = Depends(require_api_admin)):
    meta = await db.get_setting_meta(auth.SYSTEM_PROMPT_KEY)
    return _prompt_payload((meta or {}).get("value") or "", meta)


@app.put("/admin/api/prompt")
async def admin_save_prompt(payload: PromptRequest,
                            user: dict[str, Any] = Depends(require_api_admin)):
    """
    保存补充要求。

    允许保存空串——那表示「不加任何补充」，而不是错误。
    真正需要拦的是超长：每轮请求都会带上这段文本，太长会稀释硬性规则。
    """
    content = payload.content.strip()
    if len(content) > skills.MAX_SUPPLEMENT_CHARS:
        raise ApiError(1013, status=400)

    await db.set_setting(auth.SYSTEM_PROMPT_KEY, content, updated_by=user["username"])
    revision_id = await db.save_prompt_revision(content, "save", user["username"])
    meta = await db.get_setting_meta(auth.SYSTEM_PROMPT_KEY)
    return {
        "message": "已保存，下一次提问立即生效",
        "revision_id": revision_id,
        **_prompt_payload(content, meta),
    }


@app.post("/admin/api/prompt/restore/{revision_id}")
async def admin_restore_prompt(revision_id: str,
                               user: dict[str, Any] = Depends(require_api_admin)):
    """
    恢复到某个历史版本。

    恢复本身也是一次「保存」，会再记一条版本——否则回滚后历史就断了，
    下次再想回到回滚前的内容就找不到了。
    """
    revision = await db.get_prompt_revision(revision_id)
    if not revision:
        raise ApiError(1023, status=404)

    content = revision["content"] or ""
    await db.set_setting(auth.SYSTEM_PROMPT_KEY, content, updated_by=user["username"])
    new_id = await db.save_prompt_revision(content, "restore", user["username"])
    meta = await db.get_setting_meta(auth.SYSTEM_PROMPT_KEY)
    return {
        "message": f"已恢复到 {revision['created_at'][:19].replace('T', ' ')} 的版本",
        "revision_id": new_id,
        **_prompt_payload(content, meta),
    }


@app.delete("/admin/api/prompt")
async def admin_reset_prompt(user: dict[str, Any] = Depends(require_api_admin)):
    """恢复成内置默认补充要求（不是清空）。"""
    content = auth.DEFAULT_SYSTEM_PROMPT
    await db.set_setting(auth.SYSTEM_PROMPT_KEY, content, updated_by=user["username"])
    revision_id = await db.save_prompt_revision(content, "reset", user["username"])
    meta = await db.get_setting_meta(auth.SYSTEM_PROMPT_KEY)
    return {
        "message": "已恢复默认补充要求",
        "revision_id": revision_id,
        **_prompt_payload(content, meta),
    }


# ---------------------------------------------------------------------------
# 对话审计：页面 + 接口（仅管理员）
# ---------------------------------------------------------------------------

@app.get("/admin/audit")
async def admin_audit_page(
    request: Request,
    user: dict[str, Any] = Depends(require_page_admin),
    user_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    days: int = Query(default=7, ge=0, le=3650),
):
    """对话审计页：按员工/状态/关键词/时间筛选，展开看每一轮工具调用。"""
    # 状态值来自地址栏，白名单校验后再下传，避免拼出无意义查询
    status = status if status in db.AUDIT_STATUSES else None
    runs = await db.list_audit_runs(user_id, status, q, days, limit=50)
    ctx = _layout_ctx(request, user, "audit")
    ctx.update({
        "runs": runs,
        "total": await db.count_audit_runs(user_id, status, q, days),
        "stats": await db.audit_stats(days),
        "users": await db.list_users(),
        "statuses": db.AUDIT_STATUSES,
        "filters": {"user_id": user_id or "", "status": status or "",
                    "q": q or "", "days": days},
    })
    return templates.TemplateResponse(request, "admin/audit.html", ctx)


@app.get("/admin/api/audit/runs")
async def admin_audit_runs(
    user: dict[str, Any] = Depends(require_api_admin),
    user_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    days: int = Query(default=7, ge=0, le=3650),
    limit: int = Query(default=50, ge=1, le=200),
):
    status = status if status in db.AUDIT_STATUSES else None
    return {
        "runs": await db.list_audit_runs(user_id, status, q, days, limit),
        "total": await db.count_audit_runs(user_id, status, q, days),
        "stats": await db.audit_stats(days),
    }


@app.get("/admin/api/audit/runs/{run_id}")
async def admin_audit_run_detail(run_id: str,
                                 user: dict[str, Any] = Depends(require_api_admin)):
    """单条执行详情：run 概要 + 每轮 steps（含参数、耗时、摘要）。"""
    run = await db.get_audit_run(run_id)
    if not run:
        raise ApiError(1025, status=404)
    return {"run": run, "steps": await db.list_steps(run_id)}


# ---------------------------------------------------------------------------
# 日志保留：查看占用、手动清理
# ---------------------------------------------------------------------------

@app.get("/admin/api/maintenance/retention")
async def admin_retention_preview(
    user: dict[str, Any] = Depends(require_api_admin),
    days: int = Query(default=90, ge=1, le=3650),
):
    """只统计不删除：让管理员先看清会清掉多少，再决定是否执行。"""
    settings = await db.get_retention_settings()
    return {**await db.purge_logs(days, dry_run=True), "settings": settings}


@app.post("/admin/api/maintenance/purge")
async def admin_purge_logs(
    user: dict[str, Any] = Depends(require_api_admin),
    days: int = Query(default=90, ge=1, le=3650),
):
    """
    清理超过保留期的运行日志。

    只动 agent_runs / agent_steps / tool_calls 三张日志表；
    drafts / validations / messages 属于业务留痕，不在这里删。
    """
    result = await db.purge_logs(days, dry_run=False)
    deleted = sum(result["deleted"].values())
    return {
        "message": f"已清理 {deleted} 条早于 {days} 天的运行日志",
        **result,
    }


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    """不需要鉴权，便于运维探活；但不返回任何密钥信息。"""
    stats = kb_service.stats()
    return {
        "status": "ok" if stats["ready"] else "degraded",
        "knowledge_base": stats,
        "llm": llm.provider_info(),
    }


# ---------------------------------------------------------------------------
# 登录 / 登出
# ---------------------------------------------------------------------------

def _cookie_secure(request: Request) -> bool:
    """
    决定登录 Cookie 是否加 Secure 标记。

    默认按请求协议自动判断：HTTPS 下自动开启，本地 http 演示不开启，
    否则浏览器根本不会回传 Cookie，登录会「成功但立刻掉线」。
    反向代理后面时 request.url.scheme 往往是 http，所以要读
    X-Forwarded-Proto；需要强制开启/关闭时用 COOKIE_SECURE 覆盖。
    """
    flag = os.getenv("COOKIE_SECURE", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip().lower() == "https"

def _layout_ctx(request: Request, user: dict[str, Any], active: str) -> dict[str, Any]:
    """后台页面公共上下文：侧边栏、顶栏和底部状态都要用。"""
    info = llm.provider_info()
    return {
        "user": user,
        "active": active,
        "model_name": info["model"],
        "mock_mode": info["mock"],
    }


@app.api_route("/login", methods=["GET", "HEAD"])
async def login_page(request: Request, next: str = ""):
    """已登录的人再访问登录页，直接送回各自首页。"""
    user = auth.user_from_request(request)
    if user:
        return RedirectResponse("/admin" if auth.is_admin(user) else "/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "username": "", "next": next}
    )


@app.post("/auth/login")
async def login_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    next: str = Form(default=""),
):
    username = username.strip()

    def _failed(message: str):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": message, "username": username, "next": next},
            status_code=400,
        )

    if not username or not password:
        return _failed("请输入用户名和密码")

    user = await db.get_user_by_username(username)
    # 密码校验是 CPU 密集的（20 万次 PBKDF2），放线程池里避免卡住事件循环
    ok = user is not None and await asyncio.to_thread(
        auth.verify_password, password, user["password_hash"]
    )
    if not ok:
        # 不区分「用户不存在」和「密码错误」，避免被用来枚举账号
        return _failed("用户名或密码不正确")
    if not user.get("is_active"):
        return _failed("该账号已被停用，请联系管理员")

    token = auth.create_token(user)
    # next 只接受站内相对路径，防止被构造成开放重定向
    target = next if next.startswith("/") and not next.startswith("//") else None
    if not target:
        target = "/admin" if user["role"] == auth.ROLE_ADMIN else "/"

    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=3600 * 8,
        httponly=True,      # JS 读不到，XSS 也偷不走
        samesite="lax",     # 阻断跨站表单提交携带
        secure=_cookie_secure(request),
        path="/",
    )
    return response


@app.post("/auth/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


@app.get("/api/auth/me")
async def whoami(user: dict[str, Any] = Depends(require_api_user)):
    """给前端用：判断当前登录身份，决定是否显示后台入口。"""
    return user


# ---------------------------------------------------------------------------
# 后台页面（服务端渲染）
# ---------------------------------------------------------------------------

@app.get("/admin")
async def admin_dashboard(request: Request,
                          user: dict[str, Any] = Depends(require_page_admin)):
    stats = kb_service.stats()
    overview = await db.overview_stats()
    prompt_text = await db.get_setting(auth.SYSTEM_PROMPT_KEY) or ""
    ctx = _layout_ctx(request, user, "dashboard")
    ctx.update({
        "doc_count": stats["document_count"],
        "chunk_count": stats["chunk_count"],
        "kb_ready": stats["ready"],
        "kb_error": stats.get("error"),
        "embedding_model": (stats.get("embedding") or {}).get("model", "-"),
        "stats": overview,
        "prompt_len": len(prompt_text),
    })
    return templates.TemplateResponse(request, "admin/dashboard.html", ctx)


# ---------------------------------------------------------------------------
# 员工问答页
# ---------------------------------------------------------------------------

@app.api_route("/", methods=["GET", "HEAD"])
async def index(request: Request):
    """
    员工问答页。

    需要登录：未登录跳登录页。
    HEAD 与 GET 同等对待 —— 预览面板/反向代理常用 HEAD 做连通性探测。
    """
    user = auth.user_from_request(request)
    if not user:
        return RedirectResponse("/login?next=/", status_code=303)

    page = STATIC_DIR / "index.html"
    if not page.exists():
        raise ApiError(9999, "前端页面缺失，请确认 static/index.html 存在", status=500)
    return FileResponse(page)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("logistics_agent.app:app", host="127.0.0.1", port=8000, reload=False)
