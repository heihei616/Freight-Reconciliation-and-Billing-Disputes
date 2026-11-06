# 物流费对账与计费争议智能体

> 一个把「制度问答」和「账务核对」放在同一条链路上的物流结算助手。
> 模型负责决策，服务端负责事实、计算与权限闸门；任何写操作都必须由人点确认。

[![CI](https://github.com/example/logistics-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/example/logistics-agent/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 这个项目解决什么问题

物流结算岗每天要干三件互相纠缠的事：

1. **查规则**：这票该按体积重还是实重？燃油费率这个月是多少？争议要在几天内提出？
2. **核账**：承运商账单上的钱，跟按合同算出来的钱，差在哪里、差多少？
3. **走流程**：差异确实存在时，起草争议单、附证据、提交复核。

难点不在「回答一个问题」，而在**回答完之后敢不敢让它动钱**。大模型可以流畅地编造一个看起来合理的差额，也可以在被催促时跳过校验直接建单。所以这个项目的设计重心不是把提示词写得更长，而是：

- 金额只能来自确定性代码的计算结果，模型没有乘法权限；
- 校验结论只能来自服务端校验器，模型不能说「我看过了，没问题」；
- 建单前强制中断，草稿正文流式推给员工，人点了按钮才继续；
- 每一步工具调用都落库，事后可复盘模型到底做了什么。

## 能力一览

| 能力 | 说明 | 由谁保证 |
|---|---|---|
| 制度问答 | FAISS + BM25 混合检索，答案附原文片段与章节；未命中明确说「未找到」 | 检索层 + 提示词 |
| 运费复算 | 体积重、抛比、阶梯价、燃油附加费、最低收费完整推导 | `core/domain.py` 纯函数 |
| 账单核对 | 逐行比对账单金额与应收金额，输出差异项与合计差额 | `validate_bill()` |
| 凭证校验 | 称重单能否计入计费重量（偏差阈值、签章完整性） | `check_weight_ticket()` |
| 争议起草 | 生成草稿 → 人工确认 → 提交复核队列 | 服务端闸门 + 人工确认 |
| 确认可恢复 | 确认状态存库，刷新页面、切换会话、多 worker 都能接着点；超过 180 秒自动失效 | `pending_confirms` + 原子更新 |
| 执行可观测 | 每次运行、每轮决策、每次工具调用全部落库 | `agent_runs` / `agent_steps` |
| 对话审计 | 按员工、状态、关键词、时间筛选执行轨迹，展开看每轮工具与耗时 | `/admin/audit` |
| 知识库运维 | 管理员上传、删除、重建 md / txt / pdf 索引 | `/admin/knowledge` |
| 提示词模板 | 后台维护补充要求，保存即生效；每次保存留快照，可回滚 | `/admin/prompt` + `prompt_revisions` |
| 日志保留 | 按天清理运行日志，业务单据不受影响；可预演、可手动执行 | `LOG_RETENTION_DAYS` |
| 限流与配额 | 滑动窗口限流、每日配额、全局并发闸门 | `ratelimit.py` |
| 登录防爆破 | 账号 / IP 双维度失败计数，超限 429 + Retry-After；账号不存在也走等价哈希，避免计时枚举 | `ratelimit.LoginGuard` |
| 日志脱敏 | 密钥、手机号、身份证在落库前统一脱敏 | `utils/masking.py` |
| 启动自检 | 配置类型/范围校验；生产模式强制密钥与 Secure Cookie，不合规拒绝启动 | `utils/config.py` |
| 健康探针 | `/healthz` 存活、`/readyz` 就绪（数据库/索引/模型三项），失败带明细 | 内置路由 |
| 运行指标 | Prometheus 文本格式：请求量、耗时、SSE 活跃数、确认积压、库大小 | `/metrics` |
| 备份恢复 | 在线一致性快照、完整性校验、单文件可独立使用、恢复前自动另存 | `scripts/backup_db.py` |
| 凭证轮换 | 列出仍在用演示口令的账号；交互改密 / 随机生成 / 自动化输入；写库后回读校验 | `scripts/set_password.py` |
| 首次引导 | 开发环境播种演示账号，生产不播种；空库用 `ADMIN_INITIAL_PASSWORD` 建首个管理员，避免「起了但没人能登录」 | `auth.ensure_initial_accounts()` |
| 容器交付 | 多阶段镜像、非 root 运行、内置健康检查、compose 编排 | `Dockerfile` |

## 快速开始

### 环境要求

- Python 3.11 及以上（3.12 / 3.13 均已通过 CI）
- 无需数据库服务，SQLite 随进程启动自动建表
- 可选：任意 OpenAI 兼容的模型与向量接口（不配也能用 mock 跑通界面）

### 六步启动

```bash
# 1. 取代码并进入目录
git clone <your-repo-url> logistics-agent
cd logistics-agent

# 2. 建虚拟环境（Windows Git Bash；macOS/Linux 把 Scripts 换成 bin）
python -m venv .venv
source .venv/Scripts/activate

# 3. 装依赖与项目本体
pip install -r requirements.txt
pip install -e .

# 4. 准备配置：不填密钥也能启动，但回答会走 mock
cp .env.example .env

# 5. 上线预检：环境、依赖、目录权限、数据库、密钥强度逐项过一遍
PYTHONPATH=src python scripts/preflight.py

# 6. 启动
logistics-agent
```

浏览器打开 <http://127.0.0.1:8000>。开发环境首次启动会自动创建两个演示账号：

| 账号 | 初始口令 | 角色 | 能做什么 |
|---|---|---|---|
| `employee` | `emp123` | 员工 | 提问、核对账单、提交自己名下的争议草稿 |
| `admin` | `admin123` | 管理员 | 额外可进 `/admin` 维护知识库、提示词与审计 |

> 这两个口令随代码发版，**只能用于本地**。`APP_ENV=production` 时不会播种，
> 首次启动改用 `ADMIN_INITIAL_PASSWORD` 建第一个管理员（见
> [运维手册 6.1](docs/operations.md)）。若这个实例已经轮换过口令，
> 初始值在 `data/runtime/credentials.local.txt`（忽略项，不入库）；
> 随时可用 `python scripts/set_password.py --list` 查状态，
> `--user admin --generate` 重置一条。

### 离线（装不上依赖）时的启动方式

有些环境没有网络也没有 setuptools，`pip install -e .` 会卡在隔离构建阶段。这时直接把 `src` 加进模块路径即可：

```bash
PYTHONPATH=src python -m logistics_agent.main
# Windows CMD: set PYTHONPATH=src && python -m logistics_agent.main
```

仓库自带的脚本与测试都做了同样的回退处理，不安装也能跑。

### Docker 启动（交付环境推荐）

```bash
cp .env.example .env            # 填模型密钥；生产再设 JWT_SECRET 与 COOKIE_SECURE=1
docker compose up -d --build
docker compose ps               # 等 STATUS 变成 healthy
```

数据库、索引、知识库文档与备份都在命名卷里，容器重建不丢。
生产环境显式声明 `APP_ENV=production`，启动校验会强制要求完整的安全配置：

```bash
APP_ENV=production docker compose up -d
```

完整的部署拓扑、探针语义、备份恢复与故障处置见[运维手册](docs/operations.md)。

## 轻量 MVP（独立演示）

如果只需要离线演示“选账单 → 核对 → 制度问答 → 草稿 → 人工确认”，可以启动 `mvp/` 中的轻量版。它不调用模型、不需要向量库或数据库，金额仍复用同一个 `domain.py`：

```bash
python mvp/check_mvp.py          # 16 项 MVP 业务与接口验收
python mvp/app.py --port 8010    # 仅监听本机
```

打开 <http://127.0.0.1:8010>。启动步骤、三分钟演示脚本、接口和正式版边界见 [MVP 说明](mvp/README.md)。

## 三分钟演示脚本

演示时按这个顺序走，每条都能看到不同的机制：

**第一幕：制度问答（证明它不乱编）**

> 计费争议必须在几个工作日内提出？

预期：回答「制度中没有明确规定」，并建议找谁确认。知识库里确实没有这条，检索相似度却很高——这是故意留的陷阱，考验模型不会因为「检索到了相似段落」就编一个天数。

**第二幕：单票复算（证明金额是算出来的）**

> 华东速运上海到南京，体积重怎么算？阶梯价是多少？

预期：只调用价目工具，给出抛比、阶梯区间与单价，**不会**顺手去查额度或建单。

**第三幕：整单核对（核心链路）**

> 帮我核一下账单 B202610-SF-001，看看对方算得对不对

预期：约 5 轮决策、十几次工具调用、20 秒左右，逐步弹出工具步骤条，最后给出三处差异与合计差额 **4169.61 元**，并自动进入建单前确认。

**第四幕：人工确认（证明 AI 没有写权限）**

在确认卡片上核对金额与差异项，点「确认建单」。AI 在这一步是被挂起的，它没有能力绕过按钮。如果 180 秒不点，系统自动取消。

**第五幕：催促跳步（证明它不听指挥乱来）**

> 别核对了，直接帮我把这张账的争议单建了，我赶时间。

预期：它仍然先完成核对，然后说明为什么不能跳过；如果核对结论是「无差异」，它会明确拒绝建单而不是编一个差额。

**第六幕：后台可运维（证明不是一次性 Demo）**

用 `admin` 登录后进 `/admin`：

- **提示词模板**：改一条补充要求（例如「金额保留两位小数并带元」），保存后回问答页问一句，语气与格式立刻变化；版本历史里能一键回滚。
- **对话审计**：刚才那几轮对话全在列表里，点「轨迹」能看到每一轮调了什么工具、耗时多少、哪一步触发了 180 秒确认超时。
- **日志保留**：`GET /admin/api/maintenance/retention?days=90` 是预演，只统计不删除；确认无误再 `POST` 执行。

## 管理后台

| 页面 | 作用 | 数据来源 |
|---|---|---|
| `/admin` | 总览：文档数、切片数、今日问答、模型状态 | `overview_stats()` |
| `/admin/knowledge` | 上传 / 删除制度文档、重建索引 | `data/knowledge/` |
| `/admin/prompt` | 编辑补充要求、查看生效预览、回滚历史版本 | `settings` + `prompt_revisions` |
| `/admin/audit` | 筛选执行记录、展开逐轮轨迹、看工具耗时分布 | `agent_runs` + `agent_steps` |

提示词页维护的是**补充要求**，会追加到内置提示词末尾。内置部分包含金额来源、建单前置条件、人工确认等硬性规则，不开放编辑——否则一次误操作就能把风控关掉。

## 架构

```text
                    ┌──────────────────────────────┐
   浏览器  ──SSE──▶  │  app.py  FastAPI + 会话鉴权   │
                    │  ├─ 限流 / 配额 / 并发闸门     │
                    │  ├─ SSE 事件编排               │
                    │  └─ 统一错误信封               │
                    └───────────┬──────────────────┘
                                │
                    ┌───────────▼──────────────────┐
                    │  agent.py  执行循环（≤5 轮）   │
                    │  决策 → 执行工具 → 结果回填    │
                    │  建单前挂起，等人工确认        │
                    └───┬───────────┬──────────────┘
                        │           │
          ┌─────────────▼──┐   ┌────▼─────────────┐
          │ tools.py       │   │ skills.py        │
          │ 14 个工具      │   │ 能力包 + 路由     │
          │ Schema + 闸门  │   │ 工具白名单分层    │
          └───┬────────┬───┘   └──────────────────┘
              │        │
    ┌─────────▼──┐  ┌──▼──────────────┐
    │ core/      │  │ rag_service.py  │
    │ domain.py  │  │ FAISS + BM25    │
    │ 确定性计算 │  │ 混合检索        │
    └────────────┘  └─────────────────┘
              │
    ┌─────────▼──────────────────────┐
    │ db.py  SQLite                  │
    │ sessions / messages            │
    │ agent_runs / agent_steps       │
    │ tool_calls / validations       │
    │ drafts                         │
    └────────────────────────────────┘
```

三层职责的边界很硬：

- **模型**只能决定「下一步调哪个工具、参数填什么」；
- **工具层**只信自己记账的调用轨迹，不信模型的自述；
- **领域层**是纯函数，不碰网络与数据库，可以拿单元测试锁死金额。

更细的设计取舍见[设计说明](docs/design.md)。

## 校验与测试

所有检查都不依赖网络与模型，可在离线环境跑完。

```bash
# 256 条离线单元测试（约 6 秒）
python -m unittest discover -s tests -v

# 上线预检：配置、依赖、数据库、密钥、磁盘、备份新鲜度
PYTHONPATH=src python scripts/preflight.py

# 仓库规范：编码、换行、行尾空白、密钥扫描、必备文件
python scripts/check_repo.py

# 文档一致性：链接、路径、接口、命令是否真实存在
python scripts/check_docs.py

# 30 条评测用例的数据与工具名校验
python scripts/check_cases.py

# 不经过模型，直接验证服务端权限闸门（8 个场景 16 项断言）
python scripts/check_gates.py

# 需要服务已启动：验证确认接口读写同一份状态（11 项断言，不调用模型）
PYTHONPATH=src python scripts/check_confirm_api.py

# 在线备份：服务运行中也能取一致性快照，备份后自动校验
PYTHONPATH=src python scripts/backup_db.py

# 初始凭证轮换：先看谁还在用演示口令，再逐个改密
PYTHONPATH=src python scripts/set_password.py --list
PYTHONPATH=src python scripts/set_password.py --user admin

# 离线业务示例：运费推导 + 三张账单结论
python examples/demo.py

# 需要真实模型：回放三条 Agent 轨迹
python examples/demo_agent.py
```

装了 make 可以一条命令全跑：

```bash
make check
```

没有 make 的 Windows 环境，等价写法：

```bash
python scripts/check_repo.py && python scripts/check_docs.py && python scripts/check_cases.py && python -m unittest discover -s tests
```

测试覆盖的重点不是行数，而是**那些一旦破了就会动到钱的路径**：

| 被测对象 | 关键断言 |
|---|---|
| `core/domain.py` | 三张账单的结论与差额固定为 fail 4169.61 / pass 0 / fail 540.80 |
| `tools.py` | 没有 `validation_id` 时建单必被拒；非法参数不会进入人工确认 |
| `skills.py` | 纯制度提问不进入争议流程；有单号才进；倒装口语不漏判 |
| `ratelimit.py` | 窗口内第 N+1 次被拒；配额用尽抛专用异常；并发位一定归还 |
| `utils/masking.py` | 密钥整段抹除；金额保留（审计需要）；`os.getenv` 不被误报 |

## 目录结构

```text
├── src/logistics_agent/
│   ├── main.py                  # 命令行入口
│   ├── app.py                   # FastAPI 路由、SSE 编排、错误码
│   ├── agent.py                 # 执行循环：决策 → 工具 → 回填 → 再决策
│   ├── tools.py                 # 工具 Schema、前置闸门、执行分发
│   ├── skills.py                # 能力包定义、路由、系统提示词
│   ├── rag_service.py           # 混合检索与索引管理
│   ├── reimburse_agent_mvp.py   # 检索内核（切片、向量、BM25）
│   ├── llm.py                   # 模型客户端与流式解析
│   ├── db.py                    # SQLite 数据层
│   ├── auth.py                  # JWT + HttpOnly Cookie 鉴权
│   ├── metrics.py               # Prometheus 文本格式指标（零依赖）
│   ├── ratelimit.py             # 限流、配额、并发闸门
│   ├── core/domain.py           # 业务事实与确定性计算
│   ├── utils/paths.py           # 统一路径
│   ├── utils/config.py          # 配置读取与启动校验（范围、枚举、生产守卫）
│   ├── utils/text.py            # 文本规范化与截断
│   ├── utils/masking.py         # 日志脱敏
│   ├── static/                  # 员工端页面与样式
│   └── templates/               # 管理端 Jinja2 模板
├── data/knowledge/              # 唯一参与 RAG 索引的制度语料
├── data/runtime/                # 本地 SQLite（已忽略，不上传）
├── docs/                        # 设计、API、运维手册、上线清单、实测轨迹
├── tests/                       # 256 条离线测试 + 评测数据
├── examples/                    # 离线示例与轨迹回放
├── scripts/                     # 自检、预检、备份、改密、构建、部署
├── config/default.yaml          # 非机密默认配置
├── Makefile                     # 常用任务入口
└── .github/                     # CI 与协作模板
```

## 配置项

机密只从环境变量或 `.env` 读，代码里不留默认密钥。完整的变量清单见 `.env.example`，常用的几个：

| 变量 | 默认 | 作用 |
|---|---|---|
| `LLM_API_KEY` | 空 | 不填则降级 mock，界面可跑但回答是预置文本 |
| `LLM_MODEL` | `gpt-4o-mini` | 聊天模型名 |
| `EMBEDDING_PROVIDER` | `local` | `local` 完全离线；`api` 走远程向量 |
| `RATE_LIMIT_MAX_REQUESTS` | `10` | 每用户每分钟提问上限 |
| `LLM_DAILY_QUOTA` | `200` | 每用户每日模型调用上限 |
| `AGENT_MAX_CONCURRENT` | `4` | 全局同时执行的 Agent 循环数 |
| `LOG_MASKING` | `1` | 落库前脱敏，关掉仅供本地调试 |
| `LOG_RETENTION_DAYS` | `90` | 运行日志保留天数，`0` 关闭自动清理 |
| `COOKIE_SECURE` | 自动 | 留空按请求协议判断，反向代理下可强制 `1` |
| `JWT_SECRET` | 自动生成 | 生产必须显式设置，否则重启即全员掉线 |

## 常见问题

**启动报 `Address already in use`**
上一个实例还在跑。Windows 用 `netstat -ano | findstr :8000` 找到 PID 后 `taskkill /PID <pid> /F`；macOS/Linux 用 `lsof -i :8000` 后 `kill`。

**报 `ModuleNotFoundError: logistics_agent`**
没装成包也没设路径。执行 `export PYTHONPATH=src` 后重试，或用 `PYTHONPATH=src python -m logistics_agent.main`。

**页面能开，但回答像模板**
没读到模型密钥，降级成了 mock。检查 `.env` 是否在项目根目录、`LLM_API_KEY` 是否非空，然后重启。

**问制度问题时引用到了不相干的文档**
知识库会索引 `data/knowledge/` 下的所有文件。把项目文档放进 `docs/`（不参与索引），只把制度语料放在 `data/knowledge/`。

**点了确认建单却没反应**
确认状态存在数据库的 `pending_confirms` 表里，多 worker 或重启都不会丢；但**只有仍在 180 秒等待窗口内的确认能点**。超时后再点会返回「确认已超过等待时限」，重新问一次即可。如果日志里看到 `确认请求不存在`，先检查多个实例是否指向了同一个 `LOGISTICS_DB_PATH`。

## 工程约定

- **提交前**：`pre-commit install` 后可自动跑 ruff、仓库自检、文档校验与单元测试。
- **新增接口**：同步更新 [API 说明](docs/api.md)，否则 `scripts/check_docs.py` 会在 CI 里报错。
- **改动业务规则**：先改 `core/domain.py` 并补测试，再动提示词——规则写在代码里，不写在提示里。
- **新增环境变量**：同时更新 `.env.example`，否则仓库自检会警告。

更多约定见[贡献指南](CONTRIBUTING.md)。

## 安全与部署边界

当前定位是**单实例内部系统**，可以交付给一个小团队长期使用，但不是开箱即用的多租户 SaaS：

- 启动前会校验配置；`APP_ENV=production` 下缺模型密钥、`JWT_SECRET` 过短、Cookie 未加 Secure、账号仍用演示口令，都会**拒绝启动**，不会带着问题上线；
- 人工确认状态已存进 SQLite，多 worker 可以正确处理；但**限流计数仍在进程内存**，多实例会把每用户额度放大成「每实例额度」，横向扩展前要先把计数器换到 Redis；
- SQLite 适合 3~5 人的并发写入，人多了要换 PostgreSQL；
- 备份、探针、指标、日志保留都已内置，日常运维不需要额外组件；生产环境开启指标必须配 `METRICS_TOKEN`（同机反代下「仅本机」判断会失效），否则拒绝启动；
- 演示口令随代码发版，部署前必须更换：`scripts/set_password.py --list` 会标出还在用演示口令的账号，改完回读校验；改密不会让已签发的令牌立刻失效（最长 8 小时），要立即踢下线需轮换 `JWT_SECRET`。

部署拓扑、上线顺序、告警建议、备份恢复与故障处置，见[运维手册](docs/operations.md)。正式上线前请逐项完成[上线前检查清单](docs/prelaunch_checklist.md)。

## 文档

- [设计说明](docs/design.md)：分层理由、闸门取舍、已知限制
- [运维手册](docs/operations.md)：部署形态、上线流程、探针、指标、备份恢复、故障处置
- [API 说明](docs/api.md)：全部接口、错误码、SSE 事件
- [测试说明](docs/testing.md)：测试分层、如何新增用例
- [Agent 实测轨迹](docs/agent_loop_trajectories.md)：三条真实对话的逐轮记录
- [上线前检查清单](docs/prelaunch_checklist.md)：六个维度 37 项，含负责人与通过标准
- [贡献指南](CONTRIBUTING.md)
- [更新记录](CHANGELOG.md)

## 许可证

[MIT](LICENSE)
