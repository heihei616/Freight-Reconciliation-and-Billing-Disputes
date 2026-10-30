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
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile
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

import agent
import auth
import db
import llm
from rag_service import (
    BASE_DIR,
    MAX_UPLOAD_BYTES,
    kb_service,
    seed_default_document,
)

# 启动时从同目录的 .env 读取模型配置（密钥不写进代码）。
# override=False：真实环境变量优先，方便容器/CI 覆盖。
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env", override=False)
except ImportError:  # 没装 python-dotenv 时退化为只读系统环境变量
    print("[启动] 未安装 python-dotenv，跳过 .env 加载")

STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"
SEED_DOC = BASE_DIR / "discovery-report.md"

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
    1013: "提示词过长，请控制在 4000 字以内",
    1014: "文件无法解析或疑似扫描件",
    1015: "索引重建失败，请查看服务日志",
    1016: "草稿不存在或无权操作",
    1017: "该草稿已提交，不能重复提交",
    1018: "确认请求已失效或无权确认",
    1019: "草稿正文不能超过 1200 字",
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时：建表 → 演示账号 → 默认提示词 → 准备文档 → 加载索引。"""
    await db.init_db()
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
    yield


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

    ok, reason = agent.submit_confirmation(payload.confirm_id, user["id"], action, statement)
    if not ok:
        raise ApiError(1018, reason, status=409)
    return {
        "confirm_id": payload.confirm_id,
        "action": action,
        "message": "已确认，正在建单" if action == "confirm" else "已取消，未建单",
    }


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

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
