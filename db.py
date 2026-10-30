"""
db.py —— SQLite 数据层（沿用阶段 1 的表结构）

设计要点
--------
1. 只依赖标准库 sqlite3，不引入额外 ORM，方便单文件部署与教学演示。
2. sqlite3 是同步阻塞 API，所有读写都通过 `asyncio.to_thread` 丢到线程里执行，
   避免拖慢 FastAPI 的事件循环（一个慢查询会卡住所有 SSE 连接）。
3. 全局只开一个连接，并用 threading.Lock 串行化写入：
   单机演示场景下这样最简单，也不会出现 "database is locked"。
4. messages.citations / token_usage 用 JSON 文本存储，结构随检索策略变化，
   拆表属于过度设计（与阶段 1 的判断保持一致）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 数据库文件就放在本脚本同目录，随项目一起复制即可带走历史会话。
DB_PATH = Path(__file__).parent / "chat.db"

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

# 建表语句：sessions 与 messages 两张表，字段与接口设计文档一致。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id          TEXT PRIMARY KEY,          -- s_20261030_abc123
  user_id     TEXT NOT NULL,             -- 来自 X-User 请求头
  title       TEXT,                      -- 取首个问题前 20 字，便于列表展示
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id          TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  role        TEXT NOT NULL,             -- user / assistant
  content     TEXT NOT NULL,
  citations   TEXT,                      -- JSON 数组，仅 assistant 消息有值
  token_usage TEXT,                      -- JSON {prompt, completion}
  status      TEXT NOT NULL DEFAULT 'ok',-- ok / error
  created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_session_user ON sessions(user_id, updated_at);

-- 用户表：登录鉴权用。role 只有 employee / admin 两种。
-- password_hash 存 pbkdf2_sha256$迭代次数$盐$哈希，不存明文。
CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'employee',
  is_active     INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL
);

-- 配置表：系统提示词等可变项存这里，改完立即生效，不写死在代码里。
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_by TEXT,
  updated_at TEXT NOT NULL
);

-- 工具调用轨迹：智能体的每一步都留痕，审计页据此回放「它为什么这么做」。
-- result 存完整 JSON，便于事后核对模型当时看到的到底是什么。
CREATE TABLE IF NOT EXISTS tool_calls (
  id          TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL,
  user_id     TEXT,
  step        INTEGER,                  -- 第几轮工具调用（从 1 开始）
  name        TEXT NOT NULL,
  args        TEXT,                     -- JSON
  result      TEXT,                     -- JSON
  ok          INTEGER NOT NULL DEFAULT 1,
  error_code  TEXT,
  duration_ms INTEGER,
  created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_session ON tool_calls(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tool_name ON tool_calls(session_id, name);

-- 校验凭证：validate_invoice 的产物，是建单的唯一门票。
-- 单独建表而不是放在内存里，是为了重启后仍能判定「这张票是不是真的」。
CREATE TABLE IF NOT EXISTS validations (
  id           TEXT PRIMARY KEY,
  session_id   TEXT NOT NULL,
  user_id      TEXT,
  bill_no      TEXT NOT NULL,
  rule_version TEXT,
  verdict      TEXT NOT NULL,           -- pass / fail / warn
  total_delta  REAL NOT NULL DEFAULT 0,
  issues       TEXT,                    -- JSON 数组
  citation_ids TEXT,                    -- JSON 数组，来自 search_policy
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL,           -- 过期即作废，防止拿旧结论建新单
  draft_id     TEXT                     -- 已被哪张草稿消费（一次一票）
);

CREATE INDEX IF NOT EXISTS idx_val_session ON validations(session_id, created_at);

-- 争议草稿：模型能产出的最终形态。
-- status=draft 时才允许人工提交；提交入口不在模型的工具集里。
CREATE TABLE IF NOT EXISTS drafts (
  id            TEXT PRIMARY KEY,
  session_id    TEXT NOT NULL,
  user_id       TEXT NOT NULL,
  validation_id TEXT NOT NULL,
  bill_no       TEXT,
  dispute_type  TEXT,
  amount        REAL NOT NULL DEFAULT 0,
  statement     TEXT,
  evidence      TEXT,                   -- JSON 数组
  status        TEXT NOT NULL DEFAULT 'draft',   -- draft / submitted / void
  created_at    TEXT NOT NULL,
  submitted_at  TEXT,
  submitted_by  TEXT
);

CREATE INDEX IF NOT EXISTS idx_draft_user ON drafts(user_id, created_at);
"""

# 为已有数据库补列：CREATE TABLE IF NOT EXISTS 不会给老表加字段，
# 所以审计相关字段单独用 ALTER 补齐，并把「列已存在」的异常吞掉（幂等）。
_MIGRATIONS = [
    "ALTER TABLE messages ADD COLUMN ai_risk TEXT DEFAULT 'none'",       # none/low/high
    "ALTER TABLE messages ADD COLUMN ai_risk_reason TEXT",
    "ALTER TABLE messages ADD COLUMN ai_scored_at TEXT",
    "ALTER TABLE messages ADD COLUMN flag_status TEXT DEFAULT 'none'",   # none/suspect/cleared
    "ALTER TABLE messages ADD COLUMN flag_reason TEXT",
    "ALTER TABLE messages ADD COLUMN flagged_by TEXT",
    "ALTER TABLE messages ADD COLUMN flagged_at TEXT",
]


def _now() -> str:
    """
    统一使用带时区的 ISO 时间串，方便前端直接展示。

    精度取到微秒：同一秒内会连续写入 user 与 assistant 两条消息，
    只精确到秒会导致时间戳相同、排序退化成随机。
    """
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")


def _get_conn() -> sqlite3.Connection:
    """懒加载单例连接；首次调用时建表。"""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row          # 让查询结果可以按列名取值
        _conn.execute("PRAGMA journal_mode=WAL")  # 读写并发更友好
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(_SCHEMA)
        # 老库补列：每列单独执行，失败说明该列已存在，忽略即可。
        for statement in _MIGRATIONS:
            try:
                _conn.execute(statement)
            except sqlite3.OperationalError:
                pass
        _conn.commit()
    return _conn


def _gen_id(prefix: str) -> str:
    """生成可读的业务 ID，例如 s_20261030_4f2a1c。"""
    stamp = datetime.now().strftime("%Y%m%d")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------------------
# 同步实现：只在工作线程里被调用，不要在 async 函数中直接调用
# ---------------------------------------------------------------------------

def _init_sync() -> None:
    _get_conn()


def _create_session_sync(user_id: str, title: str | None = None) -> dict[str, Any]:
    conn = _get_conn()
    session_id = _gen_id("s")
    now = _now()
    with _lock:
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, user_id, title, now, now),
        )
        conn.commit()
    return {"id": session_id, "user_id": user_id, "title": title,
            "created_at": now, "updated_at": now}


def _get_session_sync(session_id: str) -> dict[str, Any] | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def _list_sessions_sync(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM sessions WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _touch_session_sync(session_id: str, title: str | None = None) -> None:
    """更新会话的活跃时间；首次提问时顺便写入标题。"""
    conn = _get_conn()
    with _lock:
        if title:
            conn.execute(
                "UPDATE sessions SET updated_at = ?, title = COALESCE(title, ?) WHERE id = ?",
                (_now(), title, session_id),
            )
        else:
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id)
            )
        conn.commit()


def _add_message_sync(session_id: str, role: str, content: str,
                      citations: list[dict[str, Any]] | None = None,
                      token_usage: dict[str, Any] | None = None,
                      status: str = "ok") -> str:
    conn = _get_conn()
    message_id = _gen_id("m")
    with _lock:
        conn.execute(
            "INSERT INTO messages "
            "(id, session_id, role, content, citations, token_usage, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                session_id,
                role,
                content,
                json.dumps(citations, ensure_ascii=False) if citations else None,
                json.dumps(token_usage, ensure_ascii=False) if token_usage else None,
                status,
                _now(),
            ),
        )
        conn.commit()
    return message_id


def _get_messages_sync(session_id: str, limit: int | None = None,
                       before_id: str | None = None) -> list[dict[str, Any]]:
    """
    按插入顺序返回消息。

    排序统一用 SQLite 隐式 rowid，而不是 created_at：
    同一秒内 user 与 assistant 两条消息的时间戳可能极其接近甚至相同，
    用 rowid 才是真正的写入顺序，历史上下文才不会错位。

    - limit 为空：返回全部（刷新页面恢复历史用）。
    - limit 有值且未指定 before_id：取“最近 limit 条”，再反转为正序。
    - before_id 有值：取该消息之前（不含）的最近 limit 条，用于排除本次提问。
    """
    conn = _get_conn()
    if before_id:
        row = conn.execute("SELECT rowid FROM messages WHERE id = ?",
                           (before_id,)).fetchone()
        if row is None:
            return []
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? AND rowid < ? "
            "ORDER BY rowid DESC LIMIT ?",
            (session_id, row["rowid"], limit or 12),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    if limit:
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY rowid DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY rowid ASC",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _to_api(row: dict[str, Any]) -> dict[str, Any]:
    """把数据库行转成前端友好的结构：JSON 字段反序列化，隐藏内部字段。"""
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "citations": json.loads(row["citations"]) if row.get("citations") else [],
        "status": row.get("status", "ok"),
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# 对外 async 接口：上层只调用这些函数
# ---------------------------------------------------------------------------

async def init_db() -> None:
    await asyncio.to_thread(_init_sync)


async def create_session(user_id: str, title: str | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(_create_session_sync, user_id, title)


async def get_session(session_id: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(_get_session_sync, session_id)


async def list_sessions(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_list_sessions_sync, user_id, limit)


async def touch_session(session_id: str, title: str | None = None) -> None:
    await asyncio.to_thread(_touch_session_sync, session_id, title)


async def add_message(session_id: str, role: str, content: str,
                      citations: list[dict[str, Any]] | None = None,
                      token_usage: dict[str, Any] | None = None,
                      status: str = "ok") -> str:
    return await asyncio.to_thread(
        _add_message_sync, session_id, role, content, citations, token_usage, status
    )


async def get_messages(session_id: str, limit: int | None = None,
                       before_id: str | None = None) -> list[dict[str, Any]]:
    rows = await asyncio.to_thread(_get_messages_sync, session_id, limit, before_id)
    return [_to_api(row) for row in rows]


async def get_recent_history(session_id: str, before_id: str, rounds: int = 6) -> list[dict[str, Any]]:
    """
    取最近 N 轮问答作为上下文。

    一轮 = 一条 user + 一条 assistant，所以按 2*rounds 条消息取，排除本次提问。
    """
    return await get_messages(session_id, limit=rounds * 2, before_id=before_id)


def add_message_nowait(session_id: str, role: str, content: str,
                       citations: list[dict[str, Any]] | None = None,
                       status: str = "error") -> None:
    """
    不阻塞地写一条消息，专供「客户端已断开」的场景使用。

    此时协程正在被取消，任何 await 都会被立刻再次打断，消息就会丢失。
    所以改用守护线程直接写库：SQLite 侧有全局锁，线程安全。
    """
    def worker() -> None:
        try:
            _add_message_sync(session_id, role, content, citations, None, status)
        except Exception as exc:  # 兜底写入失败只记录日志，不再抛出
            print(f"[落库失败] {exc!r}")

    threading.Thread(target=worker, daemon=True).start()


# ===========================================================================
# 用户与配置：登录鉴权、提示词模板
# ===========================================================================

def _get_user_by_username_sync(username: str) -> dict[str, Any] | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    return dict(row) if row else None


def _get_user_by_id_sync(user_id: str) -> dict[str, Any] | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def _create_user_sync(username: str, password_hash: str, display_name: str,
                      role: str) -> dict[str, Any]:
    conn = _get_conn()
    user_id = _gen_id("u")
    now = _now()
    with _lock:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, display_name, role, "
            "is_active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (user_id, username, password_hash, display_name, role, now),
        )
        conn.commit()
    return {"id": user_id, "username": username, "display_name": display_name,
            "role": role, "is_active": 1, "created_at": now}


def _count_users_sync() -> int:
    conn = _get_conn()
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def _update_password_sync(user_id: str, password_hash: str) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (password_hash, user_id))
        conn.commit()


def _list_users_sync() -> list[dict[str, Any]]:
    """审计页要按员工筛选，需要用户名列表；只返回非敏感字段。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, username, display_name, role FROM users ORDER BY role, username"
    ).fetchall()
    return [dict(r) for r in rows]


def _get_setting_sync(key: str) -> str | None:
    conn = _get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_setting_sync(key: str, value: str, updated_by: str | None) -> None:
    conn = _get_conn()
    now = _now()
    with _lock:
        # upsert：SQLite 3.24+ 支持 ON CONFLICT
        conn.execute(
            "INSERT INTO settings (key, value, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (key, value, updated_by, now),
        )
        conn.commit()


def _get_setting_meta_sync(key: str) -> dict[str, Any] | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT key, value, updated_by, updated_at FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return dict(row) if row else None


async def get_user_by_username(username: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(_get_user_by_username_sync, username)


async def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(_get_user_by_id_sync, user_id)


async def create_user(username: str, password_hash: str, display_name: str,
                      role: str) -> dict[str, Any]:
    return await asyncio.to_thread(
        _create_user_sync, username, password_hash, display_name, role
    )


async def count_users() -> int:
    return await asyncio.to_thread(_count_users_sync)


async def update_password(user_id: str, password_hash: str) -> None:
    await asyncio.to_thread(_update_password_sync, user_id, password_hash)


async def list_users() -> list[dict[str, Any]]:
    return await asyncio.to_thread(_list_users_sync)


async def get_setting(key: str) -> str | None:
    return await asyncio.to_thread(_get_setting_sync, key)


async def get_setting_meta(key: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(_get_setting_meta_sync, key)


async def set_setting(key: str, value: str, updated_by: str | None = None) -> None:
    await asyncio.to_thread(_set_setting_sync, key, value, updated_by)


# ---------------------------------------------------------------------------
# 后台统计：总览页与审计页共用
# ---------------------------------------------------------------------------

def _overview_stats_sync() -> dict[str, Any]:
    conn = _get_conn()
    # 今日范围：按本地时区的自然日算，直接比较 ISO 字符串前缀即可
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    today_messages = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE role = 'user' AND created_at LIKE ?",
        (f"{today}%",),
    ).fetchone()[0]
    total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    flagged = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE flag_status = 'suspect'"
    ).fetchone()[0]
    ai_suspect = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE ai_risk = 'high' AND "
        "(flag_status IS NULL OR flag_status = 'none')"
    ).fetchone()[0]
    return {
        "today_messages": today_messages,
        "total_sessions": total_sessions,
        "flagged": flagged,
        "ai_suspect": ai_suspect,
    }


async def overview_stats() -> dict[str, Any]:
    return await asyncio.to_thread(_overview_stats_sync)


# ===========================================================================
# 智能体：工具调用轨迹 / 校验凭证 / 争议草稿
# ===========================================================================

def _loads(text: str | None) -> Any:
    """读库时统一反序列化，避免调用方到处写 try/except。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _log_tool_call_sync(session_id: str, user_id: str | None, step: int, name: str,
                        args: dict[str, Any] | None, result: dict[str, Any] | None,
                        ok: bool, error_code: str | None,
                        duration_ms: int) -> str:
    conn = _get_conn()
    call_id = _gen_id("t")
    with _lock:
        conn.execute(
            "INSERT INTO tool_calls (id, session_id, user_id, step, name, args, result, "
            "ok, error_code, duration_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (call_id, session_id, user_id, step, name,
             json.dumps(args, ensure_ascii=False) if args is not None else None,
             json.dumps(result, ensure_ascii=False) if result is not None else None,
             1 if ok else 0, error_code, duration_ms, _now()),
        )
        conn.commit()
    return call_id


def _list_tool_calls_sync(session_id: str, name: str | None = None,
                          limit: int = 200) -> list[dict[str, Any]]:
    conn = _get_conn()
    if name:
        rows = conn.execute(
            "SELECT * FROM tool_calls WHERE session_id = ? AND name = ? "
            "ORDER BY rowid DESC LIMIT ?", (session_id, name, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tool_calls WHERE session_id = ? ORDER BY rowid DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["args"] = _loads(item.get("args"))
        item["result"] = _loads(item.get("result"))
        out.append(item)
    return out


def _create_validation_sync(session_id: str, user_id: str | None, bill_no: str,
                            rule_version: str, verdict: str, total_delta: float,
                            issues: list[dict[str, Any]],
                            citation_ids: list[str], ttl_minutes: int = 30) -> dict[str, Any]:
    conn = _get_conn()
    validation_id = _gen_id("v")
    now_dt = datetime.now(timezone.utc).astimezone()
    created_at = now_dt.isoformat(timespec="microseconds")
    expires_at = (now_dt + timedelta(minutes=ttl_minutes)).isoformat(timespec="microseconds")
    with _lock:
        conn.execute(
            "INSERT INTO validations (id, session_id, user_id, bill_no, rule_version, "
            "verdict, total_delta, issues, citation_ids, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (validation_id, session_id, user_id, bill_no, rule_version, verdict,
             total_delta, json.dumps(issues, ensure_ascii=False),
             json.dumps(citation_ids, ensure_ascii=False), created_at, expires_at),
        )
        conn.commit()
    return {
        "id": validation_id, "session_id": session_id, "bill_no": bill_no,
        "rule_version": rule_version, "verdict": verdict, "total_delta": total_delta,
        "issues": issues, "citation_ids": citation_ids,
        "created_at": created_at, "expires_at": expires_at, "draft_id": None,
    }


def _get_validation_sync(validation_id: str) -> dict[str, Any] | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM validations WHERE id = ?", (validation_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["issues"] = _loads(item.get("issues")) or []
    item["citation_ids"] = _loads(item.get("citation_ids")) or []
    return item


def _mark_validation_drafted_sync(validation_id: str, draft_id: str) -> None:
    conn = _get_conn()
    with _lock:
        conn.execute("UPDATE validations SET draft_id = ? WHERE id = ?",
                     (draft_id, validation_id))
        conn.commit()


def _create_draft_sync(session_id: str, user_id: str, validation_id: str, bill_no: str,
                       dispute_type: str, amount: float, statement: str,
                       evidence: list[str]) -> dict[str, Any]:
    conn = _get_conn()
    draft_id = _gen_id("d")
    now = _now()
    with _lock:
        conn.execute(
            "INSERT INTO drafts (id, session_id, user_id, validation_id, bill_no, "
            "dispute_type, amount, statement, evidence, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'draft',?)",
            (draft_id, session_id, user_id, validation_id, bill_no, dispute_type,
             amount, statement, json.dumps(evidence, ensure_ascii=False), now),
        )
        conn.commit()
    return {
        "id": draft_id, "session_id": session_id, "user_id": user_id,
        "validation_id": validation_id, "bill_no": bill_no,
        "dispute_type": dispute_type, "amount": amount, "statement": statement,
        "evidence": evidence, "status": "draft", "created_at": now,
        "submitted_at": None,
    }


def _get_draft_sync(draft_id: str) -> dict[str, Any] | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["evidence"] = _loads(item.get("evidence")) or []
    return item


def _list_drafts_sync(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM drafts WHERE user_id = ? ORDER BY rowid DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["evidence"] = _loads(item.get("evidence")) or []
        items.append(item)
    return items


def _latest_validation_sync(session_id: str) -> dict[str, Any] | None:
    """取本会话最近一次校验；状态注入与前端恢复都要用。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM validations WHERE session_id = ? ORDER BY rowid DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["issues"] = _loads(item.get("issues")) or []
    item["citation_ids"] = _loads(item.get("citation_ids")) or []
    return item


def _latest_draft_sync(session_id: str) -> dict[str, Any] | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM drafts WHERE session_id = ? ORDER BY rowid DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["evidence"] = _loads(item.get("evidence")) or []
    return item


def _submit_draft_sync(draft_id: str, user_id: str) -> tuple[bool, str]:
    """
    人工确认提交。只有 draft 状态能提交，重复点击返回明确原因而不是静默成功。

    返回 (是否成功, 原因)；原因用于前端提示。
    """
    conn = _get_conn()
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        return False, "草稿不存在"
    if row["user_id"] != user_id:
        return False, "无权提交他人的草稿"
    if row["status"] != "draft":
        return False, f"草稿当前状态为 {row['status']}，不能重复提交"
    with _lock:
        conn.execute(
            "UPDATE drafts SET status = 'submitted', submitted_at = ?, submitted_by = ? "
            "WHERE id = ? AND status = 'draft'",
            (_now(), user_id, draft_id),
        )
        conn.commit()
    return True, "已提交"


async def log_tool_call(session_id: str, user_id: str | None, step: int, name: str,
                        args: dict[str, Any] | None, result: dict[str, Any] | None,
                        ok: bool, error_code: str | None = None,
                        duration_ms: int = 0) -> str:
    return await asyncio.to_thread(
        _log_tool_call_sync, session_id, user_id, step, name, args, result,
        ok, error_code, duration_ms,
    )


async def list_tool_calls(session_id: str, name: str | None = None,
                          limit: int = 200) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_list_tool_calls_sync, session_id, name, limit)


async def create_validation(session_id: str, user_id: str | None, bill_no: str,
                            rule_version: str, verdict: str, total_delta: float,
                            issues: list[dict[str, Any]], citation_ids: list[str],
                            ttl_minutes: int = 30) -> dict[str, Any]:
    return await asyncio.to_thread(
        _create_validation_sync, session_id, user_id, bill_no, rule_version, verdict,
        total_delta, issues, citation_ids, ttl_minutes,
    )


async def get_validation(validation_id: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(_get_validation_sync, validation_id)


async def mark_validation_drafted(validation_id: str, draft_id: str) -> None:
    await asyncio.to_thread(_mark_validation_drafted_sync, validation_id, draft_id)


async def create_draft(session_id: str, user_id: str, validation_id: str, bill_no: str,
                       dispute_type: str, amount: float, statement: str,
                       evidence: list[str]) -> dict[str, Any]:
    return await asyncio.to_thread(
        _create_draft_sync, session_id, user_id, validation_id, bill_no,
        dispute_type, amount, statement, evidence,
    )


async def get_draft(draft_id: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(_get_draft_sync, draft_id)


async def latest_validation(session_id: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(_latest_validation_sync, session_id)


async def latest_draft(session_id: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(_latest_draft_sync, session_id)


async def list_drafts(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_list_drafts_sync, user_id, limit)


async def submit_draft(draft_id: str, user_id: str) -> tuple[bool, str]:
    return await asyncio.to_thread(_submit_draft_sync, draft_id, user_id)

