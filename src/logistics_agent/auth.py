"""
auth.py —— 登录鉴权（JWT + 两种角色）

为什么用 HttpOnly Cookie 存 JWT 而不是 localStorage
----------------------------------------------------
后台是服务端渲染：页面能不能看，由服务端在渲染前就判定，前端不需要读 token。
放 Cookie 并设 HttpOnly，页面即使被注入脚本也读不到 token；
localStorage 方案在 XSS 面前等于把令牌拱手让人。

密码哈希
--------
用标准库 pbkdf2_hmac（FIPS 认可、够用），不引 passlib。
存储格式：pbkdf2_sha256$迭代次数$盐(base64)$哈希(base64)
带迭代次数是为了以后能平滑升级强度——校验时按存的那次数算。

角色
----
employee：只能问答、看自己的会话
admin  ：额外可进 /admin/*（知识库、提示词、审计）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import jwt
from fastapi import Request

from . import db
from .utils.config import PRODUCTION, TRUE_WORDS, current_env

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

COOKIE_NAME = "access_token"
ROLE_EMPLOYEE = "employee"
ROLE_ADMIN = "admin"

# 后台可编辑的「补充要求」（存 settings 表，键名沿用 system_prompt）。
#
# 注意它只是补充，不是整段系统提示词：硬性规则与流程约束由
# skills.build_system_prompt 生成，这段文本会被追加到提示词末尾，
# 并在段首声明「与硬性规则冲突时以硬性规则为准」。
#
# 这样设计的原因：如果把整段提示词都交给后台编辑，管理员一次误操作
# 就可能把「不得编造金额」「建单必须人工确认」一起删掉，而这些是
# 系统性约束，不应该成为可配置项。
DEFAULT_SYSTEM_PROMPT = (
    "- 称呼统一用「你」，不用「您」。\n"
    "- 金额保留两位小数并带「元」，例如 540.80 元。\n"
    "- 提到账期时，先说明是哪个月的账，再给结论。"
)
SYSTEM_PROMPT_KEY = "system_prompt"

# 早期版本存的是「整段提示词」的一句话版本。升级后如果发现库里还是这句，
# 说明管理员没改过，直接替换成新的补充要求；改过的不动，避免覆盖用户配置。
_LEGACY_SYSTEM_PROMPTS = {
    "你是公司物流费对账与计费争议助手，只根据提供的制度条文回答，"
    "不确定时明确说'制度中未找到'，回答末尾附引用来源，不得编造报销标准。",
}

# PBKDF2 迭代次数。20 万次在普通笔记本上约 100ms，足够拖慢离线爆破。
_PBKDF2_ROUNDS = 200_000

# 令牌有效期：8 小时，覆盖一个工作日。
_TOKEN_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "8"))

# 对外只读别名，供脚本在提示里说明「改密后旧令牌还能用多久」。
TOKEN_HOURS = _TOKEN_HOURS

# 密钥文件放在用户主目录（纯 ASCII 路径），避免落进项目目录被误提交。
_SECRET_FILE = Path.home() / ".logistics_jwt_secret"


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """生成加盐哈希。每次调用盐都不同，同一密码两次哈希结果不一样。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """
    校验密码。

    用 hmac.compare_digest 做定长比较，避免根据响应时间差逐字符猜哈希。
    """
    try:
        algorithm, rounds, salt_b64, digest_b64 = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(rounds)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


_DUMMY_HASH: str | None = None


def verify_dummy(password: str) -> bool:
    """
    用户不存在时也走一遍 PBKDF2，然后返回 False。

    为什么需要：`user is not None and verify_password(...)` 这种短路写法，
    在账号不存在时会立刻返回，而密码错误要等 20 万次哈希（几十毫秒）。
    攻击者据此就能判断哪些用户名真实存在，把枚举成本降到几乎为零。

    代价是不存在的账号也要烧一次 CPU。这正好被登录限流器覆盖：
    没有限流时，这个函数本身会成为 CPU 放大的入口。
    """
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        # 首次调用时生成，避免导入阶段就多花几十毫秒。
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _DUMMY_HASH)
    return False


# ---------------------------------------------------------------------------
# JWT 签发与校验
# ---------------------------------------------------------------------------

def _load_secret() -> str:
    """
    读取签名密钥。

    优先环境变量 JWT_SECRET（容器/生产用）；
    没有则从主目录的密钥文件读，文件不存在就生成一次并写入。
    这样重启服务后已签发的 token 仍然有效，不会把所有人踢下线。
    """
    env_secret = os.getenv("JWT_SECRET", "").strip()
    if env_secret:
        return env_secret
    if _SECRET_FILE.exists():
        content = _SECRET_FILE.read_text(encoding="utf-8").strip()
        if content:
            return content
    generated = secrets.token_urlsafe(48)
    _SECRET_FILE.write_text(generated, encoding="utf-8")
    try:
        _SECRET_FILE.chmod(0o600)   # 仅当前用户可读写（Windows 上是尽力而为）
    except OSError:
        pass
    return generated


_SECRET = _load_secret()
_ALGORITHM = "HS256"


def create_token(user: dict[str, Any]) -> str:
    """签发令牌。payload 里带角色，省去每次请求查库。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user["id"],
        "username": user["username"],
        "name": user["display_name"],
        "role": user["role"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=_TOKEN_HOURS)).timestamp()),
    }
    return jwt.encode(payload, _SECRET, algorithm=_ALGORITHM)


def decode_token(token: str) -> dict[str, Any] | None:
    """校验签名与有效期，失败一律返回 None（调用方统一按未登录处理）。"""
    try:
        return jwt.decode(token, _SECRET, algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return None


# ---------------------------------------------------------------------------
# 从请求中取当前用户
# ---------------------------------------------------------------------------

def user_from_request(request: Request) -> dict[str, Any] | None:
    """
    先看 Cookie（浏览器页面/同源 fetch 自动带），再看 Authorization 头
    （方便用 curl 或外部系统调接口）。
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    if not token:
        return None
    claims = decode_token(token)
    if not claims:
        return None
    return {
        "id": claims.get("sub"),
        "username": claims.get("username"),
        "display_name": claims.get("name"),
        "role": claims.get("role"),
    }


def is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user) and user.get("role") == ROLE_ADMIN


# ---------------------------------------------------------------------------
# 口令策略
# ---------------------------------------------------------------------------

PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128

# 常见弱口令片段。只拦明显凑数的，不做词典比对——那需要引入额外依赖，
# 而真正的强度保证来自长度与随机生成。
WEAK_FRAGMENTS = ("password", "123456", "qwerty", "admin888", "letmein", "iloveyou")


def password_problems(password: str) -> list[str]:
    """
    返回不合规原因；空列表表示通过。

    策略放在这里而不是脚本里：启动引导（ADMIN_INITIAL_PASSWORD）和
    命令行改密必须用同一把尺子，否则会出现「引导能起、事后改不了」的错位。

    故意不检查「大小写数字符号各至少一个」：那类规则把用户推向
    `Abcd1234!` 这种结构固定、字典可枚举的口令，实际强度低于一个长随机串。
    """
    problems: list[str] = []
    if len(password) < PASSWORD_MIN_LENGTH:
        problems.append(f"至少 {PASSWORD_MIN_LENGTH} 个字符（当前 {len(password)}）")
    if len(password) > PASSWORD_MAX_LENGTH:
        problems.append(f"不超过 {PASSWORD_MAX_LENGTH} 个字符")
    if password.strip() != password:
        problems.append("首尾不要有空白字符（登录时容易输错）")
    if password in DEMO_PASSWORDS.values():
        problems.append("不能与代码里写死的演示口令相同")
    lowered = password.lower()
    for fragment in WEAK_FRAGMENTS:
        if fragment in lowered:
            problems.append(f"包含常见弱口令片段「{fragment}」")
            break
    if len(set(password)) == 1:
        problems.append("不能是同一个字符重复")
    return problems


# ---------------------------------------------------------------------------
# 初始账号
# ---------------------------------------------------------------------------

# 演示账号。口令随代码发版，因此只允许出现在本地环境。
_DEMO_USERS = [
    # (用户名, 密码, 显示名, 角色)
    ("admin", "admin123", "系统管理员", ROLE_ADMIN),
    ("employee", "emp123", "张员工", ROLE_EMPLOYEE),
]

# 只读映射，供 scripts/preflight.py 与 scripts/set_password.py 判断
# 「这个账号是不是还在用代码里写死的口令」。不要在业务逻辑里引用。
DEMO_PASSWORDS = {username: password for username, password, _, _ in _DEMO_USERS}


def demo_seeding_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """
    是否播种演示账号。

    显式设置 SEED_DEMO_USERS 时以它为准，否则开发环境播、生产环境不播。
    """
    raw = ((environ or os.environ).get("SEED_DEMO_USERS") or "").strip().lower()
    if not raw:
        return current_env(environ) != PRODUCTION
    return raw in TRUE_WORDS


async def ensure_initial_accounts() -> list[str]:
    """
    首次启动准备账号，只在用户表为空时动作。

    开发/演示环境播两个演示账号，双击就能登录；生产环境不播——随代码发版的
    口令一旦进了生产，等于后台对所有见过仓库的人开放。生产首次启动改用
    `ADMIN_INITIAL_PASSWORD` 建一个管理员，之后由管理员继续加人，
    或再用 `scripts/set_password.py` 改密。

    这里同时修掉一个死锁：早先的实现无条件播种演示账号，紧接着又因
    「仍在使用演示口令」拒绝启动，全新生产部署永远起不来，而报错里建议的
    「登录后台改密码」也进不去。

    返回新建的账号名，供启动日志提示（不打印口令本身）。
    """
    if await db.count_users() > 0:
        if (os.getenv("ADMIN_INITIAL_PASSWORD") or "").strip():
            print("[安全] ADMIN_INITIAL_PASSWORD 已存在账号，本次忽略；"
                  "引导完成后请从环境变量中移除该值")
        return []

    if demo_seeding_enabled():
        created = []
        for username, password, display_name, role in _DEMO_USERS:
            await db.create_user(username, hash_password(password), display_name, role)
            created.append(username)
        return created

    initial = (os.getenv("ADMIN_INITIAL_PASSWORD") or "").strip()
    if not initial:
        raise RuntimeError(
            "当前环境不播种演示账号（APP_ENV=production 或 SEED_DEMO_USERS=0），"
            "而用户表为空，没有任何账号可以登录。二选一：\n"
            "  1) 设置 ADMIN_INITIAL_PASSWORD=<至少 12 位的初始口令> 后重启，"
            "用 admin 登录并立即改密，然后从环境变量中移除该值；\n"
            "  2) 临时以 APP_ENV=development 启动一次完成建库，执行 "
            "`python scripts/set_password.py --user admin` 设置口令，再切回 production。")
    problems = password_problems(initial)
    if problems:
        raise RuntimeError(
            "ADMIN_INITIAL_PASSWORD 不合规：" + "；".join(problems) + "。修正后重启。")

    await db.create_user("admin", hash_password(initial), "系统管理员", ROLE_ADMIN)
    print("[启动] 已按 ADMIN_INITIAL_PASSWORD 创建管理员 admin；"
          "请登录后立即改密，并从环境变量中移除该值")
    return []


async def insecure_demo_accounts() -> list[str]:
    """
    检查还有哪些账号在用代码里写死的演示口令。

    开发环境只告警（课程验收就用这两个账号）；生产环境由 lifespan 据此
    拒绝启动——但那时已被 `ensure_initial_accounts()` 断掉播种，正常部署
    不会命中。留着这道网是为了接住「先以开发模式建库、再切生产」的库。
    """
    risky = []
    for username, password, _, _ in _DEMO_USERS:
        user = await db.get_user_by_username(username)
        if user and user.get("is_active") and verify_password(password, user["password_hash"]):
            risky.append(username)
    return risky


async def demo_account_hints() -> list[dict[str, str]]:
    """
    登录页要展示的演示账号。

    只在「当前环境允许演示」且「账号确实还在用演示口令」时返回。两个条件缺一不可：
    轮换过口令的实例不该继续在页面上印旧口令（照着输一定登不上），
    生产环境更不该把后台入口的口令贴在门口。
    """
    if not demo_seeding_enabled():
        return []
    risky = set(await insecure_demo_accounts())
    return [
        {
            "username": username,
            "password": password,
            "role": "管理员" if role == ROLE_ADMIN else "员工",
        }
        for username, password, _display, role in _DEMO_USERS
        if username in risky
    ]


async def ensure_default_prompt() -> None:
    """
    补充要求没存过就写一份默认的，之后以数据库为准。

    两种情况会写：库里没有；库里还是早期那句「整段提示词」的默认值。
    后者说明管理员从未动过，替换成新口径不会覆盖任何人工配置。
    """
    current = await db.get_setting(SYSTEM_PROMPT_KEY)
    if current is None:
        await db.set_setting(SYSTEM_PROMPT_KEY, DEFAULT_SYSTEM_PROMPT, updated_by="system")
        await db.save_prompt_revision(DEFAULT_SYSTEM_PROMPT, action="system", operator="system")
        return
    if current in _LEGACY_SYSTEM_PROMPTS:
        await db.set_setting(SYSTEM_PROMPT_KEY, DEFAULT_SYSTEM_PROMPT, updated_by="system")
        await db.save_prompt_revision(DEFAULT_SYSTEM_PROMPT, action="migrate", operator="system")
