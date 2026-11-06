# MVP 交付说明

> 系统：物流费对账与计费争议 MVP（轻量演示版）
> 代码位置：项目根目录 `mvp/` · 独立可部署 · 验收 30/30 通过

## 1. 定位

MVP 是主系统的轻量化独立演示版，去掉模型、向量库、登录与数据库，复用主系统的确定性金额引擎，提供「账单核对 → 制度问答 → 争议草稿 → 人工确认」完整闭环。金额结论与正式版完全一致（同一 `domain.py`）。

## 2. 工程架构（四层分包，依赖单向）

```
mvp/
├── app.py              接口层（FastAPI 传输层，业务留在 engine）
├── core/
│   ├── agent.py        编排层（Agent 框架：工具白名单 + effect 分级 + 审计）
│   ├── engine.py       领域层（核对引擎 + 审核状态机）
│   ├── knowledge.py    知识层（知识库：语料加载 + 关键词检索 + 条款引用）
│   └── domain.py       金额引擎（内置，来自主项目，纯标准库自包含）
├── static/index.html   三栏单页前端
├── policies.md         制度语料 P-01~P-08
├── check_mvp.py        30 项离线 + HTTP 验收
└── requirements.txt    仅 fastapi + uvicorn
```

依赖方向：`接口层 → 编排层 → 领域层 → 知识层 → domain`，只允许向下。

## 3. Agent 框架

`core/agent.py` 提供确定性 Agent 编排（不接大模型）：

- **工具白名单**：8 个受控工具，未注册工具调用返回 `UNKNOWN_TOOL`（400）。
- **effect 副作用分级**：`read`（只读）/ `review`（需人工审核）/ `write`（不可逆写，MVP 内不暴露）。
- **调用审计**：每次 `invoke()` 写入审计轨迹（tool / effect / 参数 / 时间），可追踪。

| 工具 | effect | 说明 |
|---|---|---|
| list_bills | read | 列出可核对账单 |
| audit_bill | read | 确定性核对账单 |
| search_policy | read | 检索制度条款 |
| answer | read | 制度与账单问答 |
| review_audit | **review** | 人工复核差异（审核点①） |
| confirm_evidence | **review** | 人工审核证据（审核点②） |
| decide_draft | **review** | 争议终审（审核点③） |
| create_draft | review | 生成争议草稿（需通过前置审核点） |

## 4. 知识库

`core/knowledge.py` 的 `KnowledgeBase`：

- 加载 `policies.md` 制度语料（P-01~P-08：抛比、阶梯价、燃油费率、重复计费、争议额度、证据、草稿、审计）；
- 关键词检索 + 稳定条款引用；
- 未命中返回空、不编造（问"员工年终奖如何计算"明确返回"未找到"）。

## 5. 三处人工确认/审核点

```
核对(只读) → ①差异复核(acknowledge/reject) → ②证据审核(confirm/abort)
           → 生成草稿 → ③争议终审(confirm/cancel)
```

- `create_draft` 有前置门：必须先通过①②，否则返回 409（`REVIEW_REQUIRED`）。
- 驳回差异后无法继续；每处审核记录审核人 + 决策 + 时间。
- 前端「人工审核流程（三级确认）」三步卡片逐步解锁。

## 6. 权限说明

**MVP 本身不设登录鉴权**（轻量演示定位，无账号体系）。

「至少两个权限」的能力由**主项目**承担：主项目已具备 `admin`（管理员）/ `employee`（员工）双角色，含 JWT 认证、角色授权（员工越权返回 403）、CSRF、登录防爆破等完整鉴权。MVP 通过 3 处人工审核点的"审核人 + 决策 + 时间"留痕体现人工兜底边界，但角色鉴权不在 MVP 范围内——如需演示权限控制，请运行主项目（`python -m logistics_agent.main`）。

## 7. 运行方式

```bash
python mvp/check_mvp.py          # 先跑 30 项验收
python mvp/app.py --port 8010    # 启动后打开 http://127.0.0.1:8010
```

## 8. 验收

`check_mvp.py` 共 30 项离线 + HTTP 断言，覆盖：知识库检索、Agent 白名单/effect 分级/审核点、审核门（409 前置门）、完整审核链、金额精确断言（顺丰 4169.61 / 华东 540.80 / 中通 0）。
