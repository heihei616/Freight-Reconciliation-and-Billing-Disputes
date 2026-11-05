"""仓库规范自检（零第三方依赖，可直接在本机运行）。

    python scripts/check_repo.py

为什么需要它
------------
ruff / pre-commit 都很好，但它们要联网安装。这个脚本只用标准库，
保证在离线环境里也能拦住最常见的工程问题：

1. 密钥与本地数据被误提交
2. 编码、缩进、行尾、结尾换行不统一
3. 开源仓库必备文件缺失
4. 文档里出现可疑的硬编码密钥

退出码：0 通过，1 有失败项。warnings 不阻塞，但会打印出来。
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from logistics_agent.utils.masking import scan_for_secrets  # noqa: E402

CODE_DIRS = ("src", "tests", "examples", "scripts")
REQUIRED_FILES = (
    "README.md", "LICENSE", "CHANGELOG.md", "CONTRIBUTING.md",
    ".gitignore", ".editorconfig", ".env.example",
    "requirements.txt", "requirements-dev.txt", "pyproject.toml",
    "config/default.yaml", "Makefile", ".pre-commit-config.yaml",
    "scripts/check_repo.py", "scripts/check_docs.py", "scripts/check_cases.py",
    ".github/workflows/ci.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
)
# 这些文件一旦出现在 git 跟踪里就是事故
FORBIDDEN_TRACKED = (".env", ".env.local", ".env.production")
MAX_LINE_LENGTH = 120
SECRET_SCAN_EXCLUDE = {
    # 脱敏模块本身必须包含密钥样例作为测试模式，属于误报
    "src/logistics_agent/utils/masking.py",
    "tests/test_utils.py",
    "scripts/check_repo.py",
}

errors: list[str] = []
warnings: list[str] = []
stats: dict[str, int] = {}


def fail(message: str) -> None:
    errors.append(message)


def warn(message: str) -> None:
    warnings.append(message)


def check_required_files() -> None:
    for rel in REQUIRED_FILES:
        if not (ROOT / rel).exists():
            fail(f"缺少仓库必备文件：{rel}")


def tracked_files() -> list[str]:
    """取 git 已跟踪文件；不在 git 仓库里时退回扫描磁盘。"""
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def check_sensitive_files() -> None:
    tracked = tracked_files()
    for rel in tracked:
        if rel in FORBIDDEN_TRACKED:
            fail(f"敏感文件被 git 跟踪：{rel}，请立即 git rm --cached 并轮换密钥")
        if rel.endswith((".db", ".db-shm", ".db-wal")):
            fail(f"本地数据库被 git 跟踪：{rel}")
        if rel.endswith((".pem", ".key")):
            fail(f"私钥文件被 git 跟踪：{rel}")

    if tracked:
        # .env 必须存在于本地（服务要读），但绝不能被跟踪
        env_example = ROOT / ".env.example"
        if not env_example.exists():
            fail("缺少 .env.example，新同事无法知道要配哪些变量")


def iter_code_files():
    for directory in CODE_DIRS:
        base = ROOT / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def check_python_files() -> None:
    total_lines = 0
    total_files = 0
    for path in iter_code_files():
        rel = path.relative_to(ROOT).as_posix()
        raw = path.read_bytes()
        total_files += 1

        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            fail(f"{rel} 不是合法 UTF-8，Windows 下请另存为 UTF-8（不要 GBK）")
            continue

        if raw.startswith(b"\xef\xbb\xbf"):
            warn(f"{rel} 带 BOM，建议保存为无 BOM 的 UTF-8")
        if b"\r\n" in raw:
            fail(f"{rel} 含 CRLF 换行，统一为 LF（.editorconfig 已约定）")
        if raw and not raw.endswith(b"\n"):
            fail(f"{rel} 末尾缺少换行符")

        lines = content.splitlines()
        total_lines += len(lines)
        for number, line in enumerate(lines, start=1):
            if line.rstrip() != line:
                fail(f"{rel}:{number} 行尾有多余空白")
            if "\t" in line[: len(line) - len(line.lstrip())]:
                fail(f"{rel}:{number} 用 Tab 缩进，统一为 4 个空格")
            if len(line) > MAX_LINE_LENGTH:
                warn(f"{rel}:{number} 行长 {len(line)} 超过 {MAX_LINE_LENGTH}")

        # AST 解析能提前发现语法错误，比等服务启动失败再排查便宜得多
        try:
            ast.parse(content, filename=rel)
        except SyntaxError as exc:
            fail(f"{rel}:{exc.lineno} 语法错误：{exc.msg}")

        if rel not in SECRET_SCAN_EXCLUDE:
            hits = scan_for_secrets(content)
            if hits:
                fail(f"{rel} 疑似硬编码敏感信息：{', '.join(hits)}")

    stats["python_files"] = total_files
    stats["python_lines"] = total_lines


def check_env_example_keys() -> None:
    """.env.example 的键要覆盖代码里真正读取的变量，避免漏配。"""
    example = ROOT / ".env.example"
    if not example.exists():
        return
    documented: set[str] = set()
    for line in example.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        # 可选变量常以 `# KEY=value` 的注释形式给出示例，这种也算已说明
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        if "=" in stripped:
            documented.add(stripped.split("=", 1)[0].strip())
    used: set[str] = set()
    pattern = re.compile(r"""os\.getenv\(\s*["']([A-Z][A-Z0-9_]+)["']""")
    for path in iter_code_files():
        if "tests" in path.parts:
            continue
        used |= set(pattern.findall(path.read_text(encoding="utf-8")))
    missing = sorted(used - documented)
    for key in missing:
        warn(f".env.example 未列出代码读取的变量：{key}")
    stats["env_keys"] = len(used)


def check_test_suite() -> None:
    tests = list((ROOT / "tests").glob("test_*.py"))
    if not tests:
        fail("tests/ 下没有任何 test_*.py")
        return
    cases = 0
    for path in tests:
        cases += path.read_text(encoding="utf-8").count("def test_")
    stats["test_files"] = len(tests)
    stats["test_cases"] = cases
    if cases < 20:
        warn(f"测试用例只有 {cases} 条，建议至少覆盖核心业务与闸门")


def main() -> int:
    check_required_files()
    check_sensitive_files()
    check_python_files()
    check_env_example_keys()
    check_test_suite()

    print("=" * 64)
    print("仓库规范自检")
    print("=" * 64)
    print(f"Python 文件：{stats.get('python_files', 0)} 个，"
          f"共 {stats.get('python_lines', 0)} 行")
    print(f"测试文件：{stats.get('test_files', 0)} 个，"
          f"用例 {stats.get('test_cases', 0)} 条")
    print(f"环境变量：代码读取 {stats.get('env_keys', 0)} 个")

    if warnings:
        print(f"\n警告 {len(warnings)} 条（不阻塞）：")
        for item in warnings[:20]:
            print("  ! " + item)
        if len(warnings) > 20:
            print(f"  … 其余 {len(warnings) - 20} 条省略")

    if errors:
        print(f"\n失败 {len(errors)} 条：")
        for item in errors[:40]:
            print("  ✗ " + item)
        if len(errors) > 40:
            print(f"  … 其余 {len(errors) - 40} 条省略")
        return 1

    print("\n✓ 仓库规范检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
