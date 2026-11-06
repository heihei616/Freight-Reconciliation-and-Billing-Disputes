"""统一项目路径，避免源码迁移后资源与数据目录漂移。"""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_DIR.parents[2]
DATA_DIR = PROJECT_ROOT / "data"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"
RUNTIME_DIR = DATA_DIR / "runtime"


def ensure_runtime_dirs() -> None:
    """创建本地运行目录；这些目录均被 .gitignore 排除。"""
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
