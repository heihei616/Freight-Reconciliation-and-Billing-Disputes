"""MVP HTTP 接口层：python mvp/app.py --port 8010

分层架构中的接口/传输层，负责参数校验、路由与错误映射；业务逻辑在 core/ 包内：
- core/knowledge.py  知识层（制度知识库）
- core/engine.py     领域层（核对引擎 + 人工审核状态机）
- core/agent.py      编排层（Agent 框架：工具白名单 + effect 分级 + 审计）
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from .core import (
        AGENT,
        EngineError,
        User,
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
    )
    from .core import auth
except ImportError:  # 以脚本方式运行（python mvp/app.py）
    from core import (
        AGENT,
        EngineError,
        User,
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
    )
    from core import auth

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="物流费对账与争议 MVP",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class BillRequest(BaseModel):
    bill_no: str = Field(min_length=1, max_length=40)


class AnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    bill_no: str | None = Field(default=None, max_length=40)


class ReviewRequest(BaseModel):
    bill_no: str = Field(min_length=1, max_length=40)
    reviewer: str | None = Field(default=None, max_length=40)
    decision: str = Field(min_length=1, max_length=40)


class DecisionRequest(BaseModel):
    action: Literal["confirm", "cancel"]
    reviewer: str | None = Field(default=None, max_length=40)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


def _bearer_token(authorization: str | None = Header(default=None)) -> str:
    """从 Authorization: Bearer <token> 头提取会话令牌。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise EngineError("未登录或缺少凭证", 401, "UNAUTHORIZED")
    return authorization[len("Bearer "):].strip()


def _require_auth(token: str = Depends(_bearer_token)) -> User:
    """任何已登录用户均可通过。"""
    return auth.authenticate(token)


def _require(permission: str):
    """构造「必须拥有某权限」的依赖。"""

    def _dependency(token: str = Depends(_bearer_token)) -> User:
        user = auth.authenticate(token)
        return auth.require_permission(user, permission)

    return _dependency


@app.middleware("http")
async def local_safety_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(EngineError)
async def handle_engine_error(_request: Request, exc: EngineError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "detail": str(exc), "message": str(exc)},
    )


@app.get("/", include_in_schema=False)
def homepage():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "service": "logistics-dispute-mvp",
        "mode": "deterministic-local",
        "policy_count": len(load_clauses()),
        "review_points": len(AGENT.review_points()),
    }


# —— 认证与权限 ——

@app.get("/api/accounts")
def list_accounts():
    """演示账号清单（含演示口令）。正式部署应移除该接口。"""
    return {"demo_password": auth.DEMO_PASSWORD, "accounts": auth.accounts()}


@app.post("/api/login")
def login(payload: LoginRequest):
    return auth.login(payload.username, payload.password)


@app.get("/api/me")
def me(user: User = Depends(_require_auth)):
    return user.public()


@app.post("/api/logout")
def logout(token: str = Depends(_bearer_token)):
    # 校验令牌有效后吊销
    auth.authenticate(token)
    auth.logout(token)
    return {"ok": True}


@app.get("/api/agent")
def agent_capability(user: User = Depends(_require("oversight"))):
    """Agent 框架能力清单：工具白名单、effect 分级、审核点、审计计数。"""
    return AGENT.capability()


@app.get("/api/bills")
def list_bills(user: User = Depends(_require("bills:read"))):
    return {"items": bills()}


@app.post("/api/audit")
def audit_bill(payload: BillRequest, user: User = Depends(_require("bills:read"))):
    return audit(payload.bill_no)


@app.post("/api/answer")
def answer_question(payload: AnswerRequest, user: User = Depends(_require("bills:read"))):
    return answer(payload.question, payload.bill_no)


# —— 人工审核点 ——

@app.get("/api/reviews")
def get_reviews(user: User = Depends(_require("oversight"))):
    return {"items": list_reviews()}


@app.get("/api/reviews/{bill_no}")
def get_review(bill_no: str, user: User = Depends(_require("bills:read"))):
    record = reviews(bill_no)
    if record is None:
        return {"bill_no": bill_no, "audit": None, "evidence": None}
    return record


@app.post("/api/reviews/audit")
def review_audit_point(payload: ReviewRequest, user: User = Depends(_require("review:audit"))):
    """审核点①：人工复核差异。decision = acknowledge | reject（签核人取自登录身份）。"""
    return review_audit(payload.bill_no, user.name, payload.decision)


@app.post("/api/reviews/evidence")
def review_evidence_point(payload: ReviewRequest, user: User = Depends(_require("review:evidence"))):
    """审核点②：人工审核证据。decision = confirm | abort（签核人取自登录身份）。"""
    return confirm_evidence(payload.bill_no, user.name, payload.decision)


# —— 争议草稿 ——

@app.get("/api/drafts")
def get_drafts(user: User = Depends(_require("bills:read"))):
    return {"items": list_drafts()}


@app.post("/api/drafts", status_code=201)
def make_draft(payload: BillRequest, user: User = Depends(_require("draft:create"))):
    return create_draft(payload.bill_no)


@app.post("/api/drafts/{draft_id}/decision")
def decide_draft(draft_id: str, payload: DecisionRequest, user: User = Depends(_require("draft:decide"))):
    """审核点③：争议终审。action = confirm | cancel（终审人取自登录身份）。"""
    return decide(draft_id, payload.action, user.name)


def main() -> None:
    parser = argparse.ArgumentParser(description="物流费对账与争议 MVP")
    parser.add_argument("--host", default="127.0.0.1", help="默认仅监听本机")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8010")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
