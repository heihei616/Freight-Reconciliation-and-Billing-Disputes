"""项目路径定位测试。

代码迁入 ``code/`` 目录后，``PROJECT_ROOT`` 必须仍然指向仓库根（而不是 ``code/``），
否则 ``.env``、``config/``、``data/`` 会整体漂移，服务启动时找不到配置。
这组用例把「根目录锚点」锁死，防止未来再动目录结构时无声破坏。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "code/src"))

from logistics_agent.utils.paths import (  # noqa: E402
    DATA_DIR,
    KNOWLEDGE_DIR,
    PACKAGE_DIR,
    PROJECT_ROOT,
    RUNTIME_DIR,
)


class PathLayoutTests(unittest.TestCase):
    def test_project_root_is_repo_root(self) -> None:
        """PROJECT_ROOT 必须指向仓库根，而非 code/ 子目录。"""
        for anchor in ("pyproject.toml", "README.md", "config/default.yaml"):
            self.assertTrue((PROJECT_ROOT / anchor).exists(), f"仓库根缺少锚点 {anchor}")

    def test_package_dir_points_to_code_src_package(self) -> None:
        """PACKAGE_DIR 是 code/src 下的 logistics_agent 包。"""
        self.assertEqual(PACKAGE_DIR.name, "logistics_agent")
        self.assertTrue((PACKAGE_DIR / "__init__.py").exists())
        self.assertEqual(PACKAGE_DIR.parent.name, "src")

    def test_data_dirs_derive_from_project_root(self) -> None:
        """数据目录必须由 PROJECT_ROOT 派生，而非 code/ 下。"""
        self.assertEqual(DATA_DIR, PROJECT_ROOT / "data")
        self.assertEqual(KNOWLEDGE_DIR, PROJECT_ROOT / "data" / "knowledge")
        self.assertEqual(RUNTIME_DIR, PROJECT_ROOT / "data" / "runtime")


if __name__ == "__main__":
    unittest.main()
