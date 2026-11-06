# 物流费对账与争议 MVP

这是从主项目抽出的**可独立演示、可离线验证**的最小可行产品。它不依赖大模型、向量数据库或 SQLite，用三栏单页串起完整业务闭环：

> 选择账单 → 确定性核对 → 查看差异与复算步骤 → 制度问答 → **三级人工审核** → 争议草稿终审

MVP 复用主项目 `src/logistics_agent/core/domain.py` 的样例运单、价目与 `validate_bill()`。它不会另写一套运费公式，因此轻量版与正式版的金额结论保持一致。

## 工程架构（四层）

依赖方向严格单向：`接口层 → 编排层 → 领域层 → 知识层 → 主项目 domain`。

```text
mvp/
├── app.py               # ① 接口/传输层：路由、参数校验、错误映射、权限守卫
├── core/
│   ├── __init__.py      # 包导出 + 单例（KNOWLEDGE / AGENT）
│   ├── auth.py          # 认证/权限层：六角色账号、会话令牌、权限校验
│   ├── agent.py         # ② 编排层：Agent 框架（工具白名单 + effect 分级 + 审计）
│   ├── engine.py        # ③ 领域层：核对引擎 + 制度问答 + 人工审核状态机
│   └── knowledge.py     # ④ 知识层：KnowledgeBase（语料加载 / 检索 / 引用）
├── policies.md          # 制度知识库语料（P-01～P-08）
├── static/index.html    # 无构建步骤的三栏单页（含登录界面）
├── check_mvp.py         # 39 项离线业务 + 认证/权限 + HTTP 验收
└── README.md            # 本说明
```

- **知识层** `core/knowledge.py`：`KnowledgeBase` 解析 `policies.md` 为 P-01～P-08，提供可解释的关键词检索与稳定引用；未命中返回空列表，不编造条款。
- **领域层** `core/engine.py`：确定性核对（金额只来自 `domain.validate_bill()`）、意图路由问答、争议草稿，以及**三级人工审核状态机**。
- **编排层** `core/agent.py`：`Agent` 注册工具白名单，按副作用分级（read / review / write），每次调用写入审计轨迹。
- **认证/权限层** `core/auth.py`：六角色登录账号、会话令牌（`Authorization: Bearer`）、权限校验；三处审核点分别落到三个不同角色。
- **接口层** `app.py`：FastAPI 传输层，仅监听本机，暴露核对、问答、审核与 Agent 能力接口，并对受保护接口加权限守卫。

## Agent 框架

`core/agent.py` 提供轻量确定性 Agent，不接大模型，但具备完整框架语义：

| 能力 | 说明 |
|---|---|
| 工具白名单 | 8 个受控 Tool（`list_bills` / `audit_bill` / `search_policy` / `answer` / `review_audit` / `confirm_evidence` / `create_draft` / `decide_draft`），未注册工具调用被拒绝 |
| effect 分级 | `read`（只读，无副作用）· `review`（需人工审核）· `write`（不可逆写，MVP 不暴露） |
| 调用审计 | 每次 `invoke` 记录 tool / effect / 参数 / 时间，可追溯 |
| 审核点清单 | `review_points()` 明确列出人工确认/审核点，`/api/agent` 暴露能力清单 |

## 知识库

`core/knowledge.py` 的 `KnowledgeBase` 是 MVP 的知识库模块：

- **语料**：`policies.md` 的 P-01～P-08（抛比、阶梯价、燃油、重复计费、争议额度、证据、草稿确认、审计留痕）。
- **检索**：确定性关键词匹配，返回条款 + 相关度得分；回答必附条款引用（citations）。
- **边界**：未命中明确返回「未找到」，绝不猜测未登记的规则；不下结算结论。

## 登录账号与权限

基于《六人访谈记录》的六个岗位，设置六类登录账号，权限对应三处人工审核点（职责分离）：

| 账号 | 姓名 | 角色 | 权限 |
|---|---|---|---|
| `guminglan` | 顾明岚 | 结算经理 | 查看账单/问答 · **③争议终审** · 全局查看 |
| `fujing` | 傅景 | 合同审计专员 | 查看账单/问答 · **②证据审核** · 全局查看 |
| `tangyingqiu` | 唐映秋 | 对账专员 | 查看账单/问答 · **①差异复核** · 生成草稿 |
| `linyizhou` | 林亦舟 | 运单录入员 | 查看账单/问答（只读） |
| `qinrui` | 秦芮 | 计费系统工程师 | 查看账单/问答 · 全局查看 |
| `yeshu` | 叶书 | 承运商结算代表 | 查看账单/问答（只读） |

- **演示口令统一为 `demo123456`**（MVP 演示专用；正式部署须更换强口令并移除 `/api/accounts` 演示提示）。
- 权限能力：`bills:read`（查看/核对/问答）· `review:audit`（①差异复核）· `review:evidence`（②证据审核）· `draft:create`（生成草稿）· `draft:decide`（③终审）· `oversight`（全局查看审核记录与 Agent 能力）。
- 职责分离依据访谈事实：f22（对账专员不能代替审计签核）、f04（经理推动、审计可否决）、f32/f52（只读、不自动扣费）、f41（不借用他人账号）。三处审核点分别由唐映秋、傅景、顾明岚三个角色承担，任何单一角色都无法走完完整争议链路。
- 审核点签核人一律取自登录身份（`user.name`），忽略客户端传入的 `reviewer`，防止伪造签核。

## 三级人工确认/审核点

不可逆动作之前都必须经过人工确认（对应 Agent 框架的 `review` 级工具）：

| 审核点 | 工具 / 接口 | 动作 | 承担角色 | 说明 |
|---|---|---|---|---|
| ① 差异复核 | `review_audit` / `POST /api/reviews/audit` | acknowledge / reject | 对账专员（唐映秋） | 核对发现差异后，人工确认差异是否成立；驳回则无法继续 |
| ② 证据审核 | `confirm_evidence` / `POST /api/reviews/evidence` | confirm / abort | 合同审计专员（傅景） | 确认证据齐备，或已知缺证仍继续；须先通过① |
| ③ 争议终审 | `decide` / `POST /api/drafts/{id}/decision` | confirm / cancel | 结算经理（顾明岚） | 草稿最终确认或取消，记录终审人 |

`create_draft` 有前置门：必须先通过审核点①和②，否则返回 409（`REVIEW_REQUIRED` / `EVIDENCE_REVIEW_REQUIRED`）。金额只来自服务端核对结果，客户端无法传入或修改。

## 启动

### 环境要求

- Python 3.11 及以上。
- 主项目 `requirements.txt` 中的 FastAPI、Uvicorn 等依赖已安装。
- 不需要模型密钥，不需要联网，不需要初始化数据库。

在项目根目录执行：

```bash
# 可选：先跑 39 项离线验收
python mvp/check_mvp.py

# 启动，默认仅监听本机 8010 端口
python mvp/app.py --port 8010
```

浏览器打开 <http://127.0.0.1:8010>。健康检查为 <http://127.0.0.1:8010/api/health>，Agent 能力清单为 <http://127.0.0.1:8010/api/agent>。

## 三分钟演示脚本

### 0. 登录

打开页面先进入登录界面，点击演示账号直接登录（或手工输入账号 + `demo123456`）。不同角色登录后可见的操作不同：

- 用 **林亦舟**（运单录入员）登录：只能看账单与问答，审核按钮不出现；
- 用 **唐映秋**（对账专员）登录：可做「①差异复核」并「生成争议单」；
- 用 **傅景**（合同审计专员）登录：可做「②证据审核」；
- 用 **顾明岚**（结算经理）登录：可做「③争议终审」。

### 1. 找出三处真实差异

默认选中 `B202610-SF-001`。页面显示：

- 核对不通过，共 3 处差异；
- 合计多收 **4,169.61 元**；
- 第 1 行体积重抛比用错，多收 694.41 元；
- 第 2 行燃油费率沿用旧版本，多收 115.20 元；
- 第 4 行同一运单重复计费，多收 3,360.00 元。

展开「查看复算过程」，说明金额来自确定性代码而不是模型心算。

### 2. 走一遍三级人工审核（需三个角色接力）

在核对结论下方「人工审核流程（三级确认）」，用不同账号登录接力完成：

1. **① 差异复核**（对账专员 唐映秋）：点「确认成立」→ 状态变为「已确认差异成立」。
2. **② 证据审核**（合同审计专员 傅景）：点「确认」→ 状态变为「已确认」。
3. ①②通过后出现「生成争议单」（对账专员）→ 弹窗核对金额与依据。
4. **③ 争议终审**（结算经理 顾明岚）：点「确认提交复核」完成。

说明：每个角色只能做自己权限内的那一步——跳过①直接生成草稿会返回 409；用只读账号看不到审核按钮；签核人自动记为登录账号，无法伪造。

### 3. 用制度回答「为什么」

在右侧依次点击：「为什么这张账单多了钱？」「争议额度还剩多少？」「称重单要满足什么条件？」分别引用 P-01/P-03/P-04、P-05、P-06。再输入「员工年终奖如何计算」，应明确回答未找到，不编造条款。

### 4. 对照通过账单与阶梯错误

- 选择 `B202610-ZT-001`：差额为 0，不出现审核流程与「生成争议单」。
- 选择 `B202609-HD-001`：差额 **540.80 元**，差异类型「阶梯档位」，依据 P-02。

## 接口

| 方法 | 路径 | 作用 | 权限 |
|---|---|---|---|
| GET | `/api/health` | 服务、制度语料与审核点状态 | 公开 |
| GET | `/api/accounts` | 演示账号清单（含演示口令，正式部署应移除） | 公开 |
| POST | `/api/login` | 账号口令登录，返回 `token` 与用户信息 | 公开 |
| GET | `/api/me` | 当前登录用户信息 | 已登录 |
| POST | `/api/logout` | 注销当前会话 | 已登录 |
| GET | `/api/agent` | Agent 框架能力清单 | `oversight` |
| GET | `/api/bills` | 三张样例账单摘要 | `bills:read` |
| POST | `/api/audit` | 按 `bill_no` 返回核对、差异、步骤与条款 | `bills:read` |
| POST | `/api/answer` | 按 `question`、可选 `bill_no` 回答 | `bills:read` |
| GET | `/api/reviews` | 全部审核记录 | `oversight` |
| GET | `/api/reviews/{bill_no}` | 某账单审核记录（①差异复核、②证据审核） | `bills:read` |
| POST | `/api/reviews/audit` | 审核点①，`decision` = acknowledge / reject | `review:audit` |
| POST | `/api/reviews/evidence` | 审核点②，`decision` = confirm / abort | `review:evidence` |
| GET | `/api/drafts` | 当前进程内的争议草稿 | `bills:read` |
| POST | `/api/drafts` | 创建待确认草稿（须先过①和②），返回 201 | `draft:create` |
| POST | `/api/drafts/{draft_id}/decision` | 审核点③，`action` = confirm / cancel | `draft:decide` |

除标注「公开」的接口外，均须在请求头携带 `Authorization: Bearer <token>`；未登录返回 401，无权限返回 403。业务错误统一包含 `code`、`detail`、`message`，不会把关键业务失败伪装成 HTTP 200。

## 与正式版的边界

这个 MVP 用于验证业务闭环、三级审核与 Agent 框架，**不能直接暴露到公网或代替正式版上线**：

| 维度 | MVP | 正式版 |
|---|---|---|
| 检索 | 本地关键词匹配 | FAISS + BM25 混合检索 |
| Agent | 确定性工具白名单 + 意图路由 | 模型决策 + 工具白名单 + Agent 循环 |
| 审核 | 内存三级审核状态机 | 审核留痕落库、可审计、可恢复 |
| 金额 | 复用 `domain.validate_bill()` | 同一个确定性领域引擎 |
| 草稿存储 | 进程内存，重启清空 | SQLite 持久化、确认状态可恢复 |
| 身份与权限 | 六角色账号 + 会话令牌（内存），审核点职责分离 | JWT HttpOnly Cookie、角色与归属校验、登录限流 |
| 写操作 | 只改变演示草稿/审核状态 | 人工确认后进入正式复核流程 |
| 运维 | 健康检查与结构化错误 | 探针、指标、限流、审计、备份、预检 |

真实落地时，应优先把 `engine.py` 的展示层对接到正式版已有校验记录与草稿接口，而不是把 MVP 的内存状态机直接扩成生产实现。

## 验收

```bash
# MVP：39 项，覆盖知识库、核对、Agent 框架、三级审核、认证/权限与完整 HTTP 链路
python mvp/check_mvp.py

# 主项目回归：确保新增 MVP 未影响既有能力
python -m unittest discover -s tests
python scripts/check_repo.py
python scripts/check_docs.py
```

浏览器已实际检查：核对页、制度问答、确认弹窗与确认后状态更新。弹窗采用固定头尾、正文独立滚动，小视口下确认按钮仍可见。
