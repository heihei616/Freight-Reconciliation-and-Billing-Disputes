"""读取非机密服务配置；机密仍只从环境变量或 .env 获取。"""

from pathlib import Path
from typing import Any

import yaml

from .paths import PROJECT_ROOT

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return {}
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件顶层必须是对象：{config_path}")
    return data
