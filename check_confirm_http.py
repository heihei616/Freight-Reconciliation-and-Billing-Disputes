"""
check_confirm_http.py —— 验证「建单前强制中断」在真实 HTTP 链路上的行为

为什么要单独写这个脚本
----------------------
demo_trajectories.py 是在进程内直接调 agent.run()，它证明不了两件最关键的事：
  1. 中断发生时，SSE 连接是不是真的**挂着不断**（而不是偷偷结束再让前端重连）；
  2. 员工通过 POST /api/agent/confirm 点确认后，**同一条流**能否继续把建单结果推回来。

所以这里用标准库 http.client 流式读 SSE：读到 confirm_open 先不发确认，
停 3 秒观察服务端有没有继续吐帧（停住 = 中断生效），再 POST 确认，继续读到最后。
不用 requests/httpx，避免为了一个自检脚本再装依赖。

前提：服务已启动在 127.0.0.1:8000
运行：python check_confirm_http.py
"""

from __future__ import annotations

import http.client
import json
import time

HOST, PORT = "127.0.0.1", 8000
QUESTION = "帮我核上个月华东速运的争议运费"


def login() -> str:
    """登录并返回 Cookie 头。"""
    conn = http.client.HTTPConnection(HOST, PORT, timeout=30)
    body = "username=employee&password=emp123"
    conn.request("POST", "/auth/login", body=body,
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp = conn.getresponse()
    cookie = resp.getheader("Set-Cookie") or ""
    resp.read()
    conn.close()
    if not cookie:
        raise RuntimeError(f"登录失败：{resp.status}")
    return cookie.split(";")[0]


def confirm(cookie: str, confirm_id: str, action: str = "confirm") -> tuple[int, str]:
    conn = http.client.HTTPConnection(HOST, PORT, timeout=30)
    body = json.dumps({"confirm_id": confirm_id, "action": action})
    conn.request("POST", "/api/agent/confirm", body=body,
                 headers={"Content-Type": "application/json", "Cookie": cookie})
    resp = conn.getresponse()
    text = resp.read().decode("utf-8", "ignore")
    conn.close()
    return resp.status, text


def parse_frame(lines: list[str]) -> tuple[str, dict] | None:
    event, data_lines = "message", []
    for line in lines:
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if not data_lines:
        return None
    try:
        return event, json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None


def main() -> int:
    cookie = login()
    conn = http.client.HTTPConnection(HOST, PORT, timeout=300)
    conn.request("POST", "/api/chat",
                 body=json.dumps({"question": QUESTION}),
                 headers={"Content-Type": "application/json", "Cookie": cookie})
    resp = conn.getresponse()
    if resp.status != 200:
        print(f"提问失败：{resp.status} {resp.read()[:200]}")
        return 1

    buffer: list[str] = []
    events: list[str] = []
    confirm_id = draft_id = None
    interrupted_at = confirmed_at = None
    executed_before_confirm = False

    while True:
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "ignore").rstrip("\r\n")
        if line:
            buffer.append(line)
            continue

        parsed = parse_frame(buffer)
        buffer = []
        if not parsed:
            continue
        event, data = parsed
        events.append(event)

        # 真正的证据：建单工具的结果如果出现在「我们点确认之前」，
        # 就说明中断没拦住执行，不管提示词怎么写都是错的。
        if event == "tool_result" and data.get("name") == "create_dispute_draft" \
                and not confirmed_at:
            executed_before_confirm = True
        if event == "draft" and not confirmed_at:
            executed_before_confirm = True

        if event == "confirm_open":
            confirm_id = data["confirm_id"]
            interrupted_at = time.time()
            print(f"[中断] confirm_open：账单 {data['bill_no']}，金额 {data['amount']}，"
                  f"编号 {confirm_id}")
            print("       先不发确认，观察 3 秒……")
            time.sleep(3)
            print(f"[中断] 已停 {time.time() - interrupted_at:.1f} 秒，尚无建单结果")

        elif event == "confirm_delta":
            pass          # 正文在流式下发，属于预期帧

        elif event == "confirm_ready":
            print("[中断] confirm_ready：草稿正文已全部推完，等待人工点击")
            status, text = confirm(cookie, confirm_id)
            confirmed_at = time.time()
            print(f"[确认] POST /api/agent/confirm → {status} {text}")

        elif event == "confirm_closed":
            print(f"[恢复] confirm_closed：decision={data['decision']}，"
                  f"服务端记录等待 {data['waited_ms']}ms")

        elif event == "tool_result" and data.get("name") == "create_dispute_draft":
            print(f"[恢复] 建单工具返回：ok={data.get('ok')} · {data.get('summary')}")

        elif event == "draft":
            draft_id = data.get("draft_id")
            print(f"[恢复] 草稿已生成：{draft_id}")

        elif event == "error":
            print(f"[错误] {data}")

    conn.close()

    print(f"\n事件序列：{' → '.join(dict.fromkeys(events))}")
    if executed_before_confirm:
        print("✗ 违规：建单结果出现在人工确认之前，中断没拦住执行")
    ok = bool(confirm_id and confirmed_at and draft_id and not executed_before_confirm)
    print(f"\n结论：{'通过' if ok else '未通过'} —— "
          f"{'挂起等待 → 人工确认 → 同一条流恢复建单' if ok else '流程未跑通'}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
