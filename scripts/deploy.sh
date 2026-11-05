#!/usr/bin/env bash
set -euo pipefail

# 内部单实例部署入口。
# 确认中断的状态已存进 SQLite（pending_confirms），多 worker 不会再丢确认；
# 但限流计数仍在进程内存，多实例会把每用户额度放大成「每实例额度」。
# 要横向扩展，先把 ratelimit 的计数器换到 Redis，再调 --workers。
python -m pip install -e .
if [[ ! -f .env ]]; then
  printf 'Warning: .env not found; the app will use mock mode without an LLM key.\n' >&2
fi
exec python -m logistics_agent.main
