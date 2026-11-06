"""文档与代码一致性校验。

    python scripts/check_docs.py

为什么需要它
------------
文档腐化不会报错，只会让人踩坑：README 里写着已删除的脚本、
API 文档里写着一个根本不存在的路由、快速开始的第一步就是错的。
这个脚本把「文档里的承诺」逐条拉出来，去代码里核对：

1. Markdown 相对链接指向的文件必须存在
2. 反引号里出现的项目内路径必须存在
3. 文档里提到的 API 路由必须真的注册在 app.py
4. 文档里的 `python xxx.py` 命令必须指向真实文件
5. README 必须包含关键章节（安装、运行、测试、结构）

不校验内容正确性（那是人的事），只校验「说的东西还在不在」。
退出码：0 通过，1 有失败项。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

DOC_FILES = [
    "README.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "docs/design.md",
    "docs/api.md",
    "docs/testing.md",
    "docs/prelaunch_checklist.md",
    "docs/agent_loop_trajectories.md",
]

# README 必备章节（用 slug 模糊匹配，允许「快速开始 / Quick Start」这类写法）
REQUIRED_README_SECTIONS = [
    ("安装", ("安装", "install", "快速开始", "quick start")),
    ("运行", ("运行", "启动", "run", "usage")),
    ("测试", ("测试", "test")),
    ("目录结构", ("目录", "结构", "structure", "layout")),
]

# 出现在文档里、需要验证存在的路径前缀
PATH_PREFIXES = ("code/src/", "code/scripts/", "code/tests/", "code/examples/", "docs/", "config/", "data/")

# 反引号里可能是命令、变量名、路径，只有带前缀的才当路径校验
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
# 同时匹配三种写法：
#   GET /api/health            行内
#   | GET | `/api/health` |    表格单元格（路径常被反引号包住）
#   ### `POST /api/chat`       小标题
ROUTE_RE = re.compile(
    # 斜杠后必须跟至少一个路径字符，避免把「`POST` / `PUT`」这类方法枚举
    # 误读成「方法 + 根路径」。本项目没有注册根路由，`GET /` 不属于可校验引用。
    r"\b(GET|POST|PUT|DELETE|PATCH)\b\s*[|｜]?\s*`?\s*(/[\w{][\w\-{}/.]*)"
)
PY_CMD_RE = re.compile(r"python\s+((?:code/scripts|code/tests|code/examples)/[\w./-]+\.py)")

errors: list[str] = []
checked = {"links": 0, "paths": 0, "routes": 0, "commands": 0}


def fail(message: str) -> None:
    errors.append(message)


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        fail(f"{path.relative_to(ROOT)} 不是 UTF-8，无法校验")
        return ""


def registered_routes() -> set[tuple[str, str]]:
    """从 app.py 里抽出实际注册的路由，作为文档的比对基准。"""
    app_py = ROOT / "code/src" / "logistics_agent" / "app.py"
    if not app_py.exists():
        fail("找不到 src/logistics_agent/app.py，无法校验路由")
        return set()
    source = read(app_py)
    routes: set[tuple[str, str]] = set()

    # 形式一：@app.get("/api/health")
    for method, path in re.findall(
        r"@app\.(get|post|put|delete|patch)\(\s*[\"']([^\"']+)[\"']", source
    ):
        routes.add((method.upper(), path))

    # 形式二：@app.api_route("/login", methods=["GET", "HEAD"])
    # 登录页需要同时响应 GET 与 HEAD，用的就是这种写法，漏了会误报文档错误。
    for path, methods in re.findall(
        r"@app\.api_route\(\s*[\"']([^\"']+)[\"'][^)]*methods\s*=\s*\[([^\]]+)\]",
        source,
        re.S,
    ):
        for method in re.findall(r"[\"'](\w+)[\"']", methods):
            routes.add((method.upper(), path))
    return routes


def normalize_route(path: str) -> str:
    """把文档里的 {id} 与代码里的 {run_id} 统一成占位符再比较。"""
    return re.sub(r"\{[^}]*\}", "{}", path.rstrip(" .,;，。；"))


def check_links(doc_path: Path, content: str) -> None:
    for target in MD_LINK_RE.findall(content):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        clean = target.split("#", 1)[0].strip()
        if not clean:
            continue
        checked["links"] += 1
        resolved = (doc_path.parent / clean).resolve()
        if not resolved.exists():
            rel = doc_path.relative_to(ROOT).as_posix()
            fail(f"{rel} 的链接指向不存在的文件：{clean}")


def check_inline_paths(doc_path: Path, content: str) -> None:
    rel = doc_path.relative_to(ROOT).as_posix()
    for token in INLINE_CODE_RE.findall(content):
        token = token.strip().strip("'\"")
        # 去掉命令行前缀与参数，只留路径本身
        token = token.split()[0] if token.split() else token
        if not token.startswith(PATH_PREFIXES):
            continue
        if any(ch in token for ch in "*<>|") or token.endswith("/"):
            continue  # 通配符与目录说明不校验
        checked["paths"] += 1
        if not (ROOT / token).exists():
            fail(f"{rel} 提到的路径不存在：{token}")


def check_routes(doc_path: Path, content: str, routes: set[tuple[str, str]]) -> None:
    rel = doc_path.relative_to(ROOT).as_posix()
    normalized = {(m, normalize_route(p)) for m, p in routes}
    for method, path in ROUTE_RE.findall(content):
        checked["routes"] += 1
        if (method.upper(), normalize_route(path)) not in normalized:
            fail(f"{rel} 写的接口未在 app.py 注册：{method} {path}")


def check_python_commands(doc_path: Path, content: str) -> None:
    rel = doc_path.relative_to(ROOT).as_posix()
    for script in PY_CMD_RE.findall(content):
        checked["commands"] += 1
        if not (ROOT / script).exists():
            fail(f"{rel} 让读者执行一个不存在的脚本：python {script}")


def check_readme_sections(content: str) -> None:
    headings = [line.lstrip("# ").strip() for line in content.splitlines()
                if line.startswith("#")]
    joined = " | ".join(headings).lower()
    for label, keywords in REQUIRED_README_SECTIONS:
        if not any(kw.lower() in joined for kw in keywords):
            fail(f"README 缺少「{label}」相关章节标题")


def main() -> int:
    routes = registered_routes()
    for rel in DOC_FILES:
        path = ROOT / rel
        if not path.exists():
            fail(f"文档缺失：{rel}")
            continue
        content = read(path)
        check_links(path, content)
        check_inline_paths(path, content)
        check_routes(path, content, routes)
        check_python_commands(path, content)

    readme = ROOT / "README.md"
    if readme.exists():
        check_readme_sections(read(readme))

    print("=" * 64)
    print("文档一致性校验")
    print("=" * 64)
    print(f"文档 {len(DOC_FILES)} 份；链接 {checked['links']} 条，"
          f"路径 {checked['paths']} 处，接口 {checked['routes']} 个，"
          f"命令 {checked['commands']} 条")
    print(f"代码中已注册路由：{len(routes)} 个")

    if errors:
        print(f"\n失败 {len(errors)} 条：")
        for item in errors[:40]:
            print("  ✗ " + item)
        if len(errors) > 40:
            print(f"  … 其余 {len(errors) - 40} 条省略")
        return 1

    print("\n✓ 文档与代码一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
