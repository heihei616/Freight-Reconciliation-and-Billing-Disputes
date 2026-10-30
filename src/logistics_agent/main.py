"""程序入口：python -m logistics_agent.main 或 logistics-agent。"""

from __future__ import annotations

import uvicorn

from .utils.config import load_config


def main() -> None:
    config = load_config()
    server = config.get("server", {})
    uvicorn.run(
        "logistics_agent.app:app",
        host=str(server.get("host", "127.0.0.1")),
        port=int(server.get("port", 8000)),
        reload=bool(server.get("reload", False)),
    )


if __name__ == "__main__":
    main()
