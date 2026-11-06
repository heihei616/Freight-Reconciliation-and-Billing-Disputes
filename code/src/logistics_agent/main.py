"""程序入口：python -m logistics_agent.main 或 logistics-agent。"""

from __future__ import annotations

import os
import sys

import uvicorn

from .utils.config import format_report, load_config, validate_env
from .utils.paths import PROJECT_ROOT


def _load_dotenv() -> None:
    """
    在读取配置之前加载 .env，否则这里校验的是系统环境变量、
    而真正跑起来的是 .env 里的值，两边对不上。
    override=False 保证容器/CI 注入的真实环境变量优先。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def main() -> None:
    _load_dotenv()
    config = load_config()

    # 在拉起 uvicorn 之前先校验一次：lifespan 里也会校验（防止有人直接
    # `uvicorn logistics_agent.app:app` 绕过这里），但那时的失败是一串 traceback。
    # 运维需要的是「哪一项错了、该改成什么」，所以入口处先给人话版本。
    report = validate_env(config)
    if not report.ok:
        print(format_report(report), file=sys.stderr)
        print("\n服务未启动：请修正上述配置后重试。", file=sys.stderr)
        raise SystemExit(2)
    # 正常启动时由 lifespan 统一打印一次报告（直接 uvicorn 启动也走那里），
    # 这里不重复打印，否则同一个警告会在日志里出现两遍。

    server = config.get("server", {})
    # 容器里必须监听 0.0.0.0 才能被端口映射访问，但默认值保持 127.0.0.1
    # （本地裸跑时不把端口暴露到局域网）。用环境变量覆盖，不改配置文件。
    host = os.getenv("SERVER_HOST") or str(server.get("host", "127.0.0.1"))
    port = int(os.getenv("SERVER_PORT") or server.get("port", 8000))
    uvicorn.run(
        "logistics_agent.app:app",
        host=host,
        port=port,
        reload=bool(server.get("reload", False)),
        proxy_headers=True,       # 反向代理下才能拿到真实协议与客户端 IP
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
