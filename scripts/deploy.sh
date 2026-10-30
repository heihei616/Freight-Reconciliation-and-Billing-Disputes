#!/usr/bin/env bash
set -euo pipefail

# 内部单实例部署入口。多 worker 前需先把确认中断改为共享存储。
python -m pip install -e .
if [[ ! -f .env ]]; then
  printf 'Warning: .env not found; the app will use mock mode without an LLM key.\n' >&2
fi
exec python -m logistics_agent.main
