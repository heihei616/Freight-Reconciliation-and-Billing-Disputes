"""
check_confirm_api.py —— 人工确认链路的接口级自检（不调用模型）

为什么还要一个脚本
------------------
check_confirm_http.py 要真实模型一路跑到 create_dispute_draft 才能看到确认卡，
额度用完、模型抖动或换了供应商时，这条最关键的闸门就验不了。

这个脚本换一种做法：直接往库里放一条待确认记录，然后只用 HTTP 走完
    GET  待确认列表 → POST 确认（带改写正文）→ 再查应为空 → 重复确认应为 409
它验证的是「确认状态存库之后，接口是否真的读写同一份状态」，与模型无关，
所以可以放进 CI 的启动冒烟里。

前提：服务已在本机启动（默认 127.0.0.1:8000）。
运行：PYTHONPATH=src python scripts/check_confirm_api.py
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from logistics_agent import db  # noqa: E402

BASE = os.getenv("CHECK_BASE_URL", "http://127.0.0.1:8000")
USERNAME = os.getenv("DEMO_EMPLOYEE_USER", "employee")
PASSWORD = os.getenv("DEMO_EMPLOYEE_PASSWORD", "emp123")

CONFIRM_ID = f"cf_smoke_{int(time.time())}"
EDITED = "员工在确认前改过的争议说明正文。"


def call(method: str, path: str, *, body: dict | None = None,
         form: dict | None = None, cookie: str | None = None) -> tuple[int, dict]:
    """发一个请求，返回 (状态码, 解析后的 JSON 或空字典)。"""
    data = None
    headers: dict[str, str] = {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie

    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            parsed = json.loads(raw) if raw else {}
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, {}


def login() -> str:
    """
    表单登录，返回 Cookie 头。

    用 http.client 而不是 urllib：登录成功返回 303，
    urllib 会自动跟随跳转，Set-Cookie 留在中间响应上，最后就拿不到了。
    """
    parts = urllib.parse.urlsplit(BASE)
    conn = http.client.HTTPConnection(parts.hostname or "127.0.0.1",
                                      parts.port or 80, timeout=20)
    conn.request("POST", "/auth/login",
                 body=urllib.parse.urlencode({"username": USERNAME, "password": PASSWORD}),
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp = conn.getresponse()
    cookie = resp.getheader("Set-Cookie") or ""
    resp.read()
    conn.close()
    if not cookie:
        raise RuntimeError(f"登录失败：状态 {resp.status}，没有拿到 Set-Cookie")
    return cookie.split(";")[0]


def cleanup(session_id: str) -> None:
    """删掉自检造的会话与确认记录，不给演示库留垃圾。"""
    try:
        conn = db._get_conn()
        with db._lock:
            conn.execute("DELETE FROM pending_confirms WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
    except Exception as exc:  # noqa: BLE001 —— 清理失败不该让自检结论变成失败
        print(f"  [提示] 清理未完成：{exc!r}")


def main() -> int:
    passed, failed = 0, 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  [通过] {label}")
        else:
            failed += 1
            print(f"  [失败] {label}{('：' + detail) if detail else ''}")

    cookie = login()
    user = None
    for _ in range(3):
        user = asyncio.run(db.get_user_by_username(USERNAME))
        if user:
            break
        time.sleep(0.5)
    if not user:
        print("找不到演示员工账号，先启动一次服务让它自动创建")
        return 1

    session = asyncio.run(db.create_session(user["id"], "确认接口自检"))
    session_id = session["id"]

    asyncio.run(db.create_pending_confirm(
        CONFIRM_ID, "run_smoke", session_id, user["id"], "create_dispute_draft",
        {"bill_no": "B202610-SF-001", "dispute_type": "weight", "amount": 4169.61,
         "statement": "模型生成的原始正文。"},
        120,
    ))

    try:
        print(f"\n== 1. 查询待确认（会话 {session_id}）==")
        status, data = call("GET", f"/api/agent/confirm/pending?session_id={session_id}",
                            cookie=cookie)
        items = data.get("confirms") or []
        check(status == 200, "接口返回 200", str(status))
        check(len(items) == 1 and items[0]["confirm_id"] == CONFIRM_ID,
              "待确认列表里能看到刚插入的记录", json.dumps(data, ensure_ascii=False)[:200])
        check(bool(items) and items[0].get("amount") == 4169.61,
              "金额来自服务端记录", str(items[0].get("amount") if items else None))
        check(bool(items) and items[0].get("remain_sec", 0) > 0,
              "剩余秒数为正", str(items[0].get("remain_sec") if items else None))

        print("\n== 2. 点确认（带员工改写的正文）==")
        status, data = call("POST", "/api/agent/confirm",
                            body={"confirm_id": CONFIRM_ID, "action": "confirm",
                                  "statement": EDITED},
                            cookie=cookie)
        check(status == 200, "确认接口返回 200", f"{status} {data}")

        row = asyncio.run(db.get_pending_confirm(CONFIRM_ID))
        check(row is not None and row["status"] == "confirmed", "库里状态为 confirmed",
              str(row and row["status"]))
        check(row is not None and row["statement"] == EDITED, "改写后的正文被采用",
              str(row and row["statement"]))

        print("\n== 3. 再查待确认 ==")
        status, data = call("GET", f"/api/agent/confirm/pending?session_id={session_id}",
                            cookie=cookie)
        check(status == 200 and not (data.get("confirms") or []),
              "已确认的记录不再出现在待办里", json.dumps(data, ensure_ascii=False)[:200])

        print("\n== 4. 重复点击 ==")
        status, data = call("POST", "/api/agent/confirm",
                            body={"confirm_id": CONFIRM_ID, "action": "confirm"},
                            cookie=cookie)
        check(status == 409, "重复确认返回 409", str(status))
        check(data.get("code") == 1018, "错误码为 1018", str(data.get("code")))

        print("\n== 5. 越权与会话归属 ==")
        status, _ = call("GET", "/api/agent/confirm/pending?session_id=s_not_exist",
                         cookie=cookie)
        check(status == 404, "不存在的会话返回 404", str(status))
    finally:
        cleanup(session_id)

    print(f"\n结果：{passed} 项通过，{failed} 项失败")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
