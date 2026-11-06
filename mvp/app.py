"""MVP HTTP 入口：python mvp/app.py --port 8010"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from .engine import EngineError, answer, audit, bills, create_draft, decide, list_drafts, load_clauses
except ImportError:
    from engine import EngineError, answer, audit, bills, create_draft, decide, list_drafts, load_clauses

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


class DecisionRequest(BaseModel):
    action: Literal["confirm", "cancel"]


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
    }


@app.get("/api/bills")
def list_bills():
    return {"items": bills()}


@app.post("/api/audit")
def audit_bill(payload: BillRequest):
    return audit(payload.bill_no)


@app.post("/api/answer")
def answer_question(payload: AnswerRequest):
    return answer(payload.question, payload.bill_no)


@app.get("/api/drafts")
def get_drafts():
    return {"items": list_drafts()}


@app.post("/api/drafts", status_code=201)
def make_draft(payload: BillRequest):
    return create_draft(payload.bill_no)


@app.post("/api/drafts/{draft_id}/decision")
def decide_draft(draft_id: str, payload: DecisionRequest):
    return decide(draft_id, payload.action)


def main() -> None:
    parser = argparse.ArgumentParser(description="物流费对账与争议 MVP")
    parser.add_argument("--host", default="127.0.0.1", help="默认仅监听本机")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
