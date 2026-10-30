# Agent 执行循环与三条典型轨迹

本文对应四件事的实现说明：框架选型、5 轮循环与日志、建单前强制中断、三条典型轨迹。

---

## 一、框架选型：自建 Harness，不引 LangChain / LangGraph / AutoGen

结论先说：**用 400 行的自建 Harness（`agent.py`）**，理由是三条硬需求都跟主流框架的抽象拧着：

| 需求 | 为什么框架不好用 |
|---|---|
| 工具执行前强制中断、等真人点确认后**原样恢复** | LangGraph 有 `interrupt()`，但要把状态机、SQLite 审计、SSE 流全部改写成它的 checkpointer 格式；改造量比自己写循环大 |
| 每轮决策落库（工具名/参数/耗时/摘要）供阶段 5 分析 | 自建：一次 INSERT。框架：接 callback 体系再对齐字段，还要处理异步上下文丢失 |
| 轮次、重复调用、工具白名单的硬边界 | 框架的路由/多智能体/记忆后端全用不上，反而多一层要读的源码 |

工具只有 13 个、单进程、单模型，所以"框架能省的那点样板代码"在这里不成立。

---

## 二、循环结构：决策 → 执行 → 回填 → 再决策（最多 5 轮）

```
agent.run()
├─ 推导状态（从数据库轨迹，不靠模型记忆）
├─ 路由 skill（决定这一轮模型能看见哪些工具）
├─ 建 agent_runs 行（run_id）
└─ for round in 1..5：                       ← MAX_ROUNDS = 5
     ├─ 决策：llm.stream_with_tools(messages, 白名单工具)
     │    ├─ 无工具请求 → 本轮就是最终回答，跳出
     │    └─ 有工具请求 → messages 追加 assistant(tool_calls)
     ├─ 执行：逐个 tools.execute()，每步写 agent_steps
     │    └─ create_dispute_draft → 强制中断（见第三节）
     ├─ 回填：tool 消息塞回 messages
     └─ 再决策（下一轮）
├─ 建单收尾轮（仅当 5 轮用尽、已拿到校验结论、还没建单）
│    工具白名单收窄到 [create_dispute_draft, escalate_to_human]
└─ 收尾：轮次用尽 / 只调工具没说话 → 补一次纯生成，给出阶段性结论
```

> 为什么单独有「建单收尾轮」：建不建单在拿到 verdict 那一刻就定了，不是新的决策；而人工确认是人的动作。
> 如果把这一轮也算进 5 轮，只要模型把校验压到第 5 轮，用户就永远拿不到争议单——那不是安全，是流程没走完。
> 这一轮的工具白名单被收窄成只剩建单，模型没法借机去干别的。

三道与提示词无关的硬约束：

| 约束 | 值 | 违反时 |
|---|---|---|
| 决策轮次 | `MAX_ROUNDS = 5` | 停止调工具，强制生成阶段性结论（`status=max_rounds`） |
| 工具调用总数 | `MAX_TOOL_CALLS = 12` | 直接 `error 3004` |
| 同工具同参数 | `MAX_SAME_CALL = 2` | 返回 `TOOL_LOOP`，提示换思路或转人工 |

另有一道「参数预检」：`create_dispute_draft` 的参数（正文长度、类型枚举、金额格式）**在中断之前**先校验一遍。
不合法就直接把 `BAD_ARGS` 回给模型让它改，绝不拿一份注定被拒的草稿去打扰员工。

### 日志表

**`agent_runs`**（一次提问一行）：`session_id / user_id / question / skill / status / rounds / tool_calls / confirm_count / elapsed_ms / started_at / finished_at`。
`status` 取值：`running` / `awaiting_confirm` / `done` / `max_rounds` / `error` / `capped`（工具调用总数超限）。

**`agent_steps`**（每次工具调用或人工确认一行）：

| 字段 | 说明 |
|---|---|
| `round` | 第几轮决策 |
| `kind` | `tool` / `confirm` |
| `tool_name` | 工具名；人工确认记为 `human_confirm` |
| `tool_args` | JSON 参数原文 |
| `duration_ms` | 耗时（人工确认记的是员工犹豫了多久） |
| `result_summary` | 一句话摘要，直接给人看 |
| `decision` | 模型这一轮说的话（截 200 字） |
| `ok` / `error_code` | 是否成功、失败码（`PRECONDITION` / `VERDICT_PASS` / `HUMAN_CANCELLED` …） |

阶段 5 想回答的问题都能直接查出来：

```sql
-- 哪个工具最慢
SELECT tool_name, COUNT(*), ROUND(AVG(duration_ms)) FROM agent_steps
WHERE kind='tool' GROUP BY tool_name ORDER BY 3 DESC;

-- 哪些调用最常被闸门拒绝
SELECT tool_name, error_code, COUNT(*) FROM agent_steps
WHERE ok=0 GROUP BY 1,2 ORDER BY 3 DESC;

-- 员工平均犹豫多久才点确认
SELECT ROUND(AVG(duration_ms)) FROM agent_steps
WHERE kind='confirm' AND tool_args LIKE '%"confirm"%';
```

读取接口：`GET /api/agent/runs`、`GET /api/agent/runs/{run_id}`（返回 run + steps）。

---

## 三、建单前强制中断

**它不是提示词，是代码里唯一一条能执行建单的路径。**

```
模型请求 create_dispute_draft
   ↓
① 登记 PendingConfirm（内存，不落库：连接断了这次确认就该失效）
② 写 agent_runs.status = awaiting_confirm
③ 推送 confirm_open（账单/类型/金额/问题清单）→ 前端画出确认卡
④ 推送 confirm_delta × N：草稿正文按 10 字一片流给员工（12ms/片）
⑤ 推送 confirm_ready：按钮解锁
⑥ await asyncio.wait_for(event.wait(), 180)     ← 真的停在这里
   ├─ 员工 POST /api/agent/confirm {confirm_id, action:"confirm"}
   │     → tools.execute("create_dispute_draft") → 草稿入库
   ├─ action:"cancel" → HUMAN_CANCELLED，回灌给模型让它改口
   └─ 超时 180s      → CONFIRM_TIMEOUT，按取消处理，**不默认同意**
⑦ 推送 confirm_closed（含员工等待毫秒数）→ 同一条 SSE 继续输出最终回答
```

几个刻意的选择：

- **确认请求不落库**。它只对当前这条挂着的连接有意义；落库会造出"三天后点确认把单建出来"的漏洞。
- **`/api/agent/confirm` 不建单**，只唤醒循环。建单仍在 SSE 链路内部完成，工具轨迹、日志、事件顺序才是连续的。
- **超时按取消**。默认通过是最危险的失败模式。
- **员工可以改正文**（`statement` 字段），改后的版本才入库。

---

## 四、三条典型轨迹

复现：`python demo_trajectories.py`（`a`/`b`/`c` 可单跑）；HTTP 链路另跑 `python check_confirm_http.py`。

### 示例 a：只查计费规则

> 用户：「华东速运 上海→南京 的体积重计费标准和阶梯价是多少」

预期路径：`policy_qa`（只带 5 个工具：search_policy / get_rate_card / check_weight_ticket / load_skill / escalate_to_human）→ 直接作答。

实测轨迹（9.1 秒，1 轮）：

| 轮 | 工具 | 参数 | 耗时 | 结果 |
|---|---|---|---|---|
| 1 | `get_rate_card` | carrier=华东速运, lane=上海-南京 | 2ms | RC-HD-SHNJ-2026Q3 · 阶梯价 ≤500kg 1.8 / 500-2000kg 1.6 / >2000kg 1.4 · 抛比 6000 |
| 1 | `search_policy` | query=体积重 计费标准 抛比 阶梯价 | 332ms | 命中 3 条条款 |

**约束**：全程不调用 `get_dispute_quota` / `request_evidence` / `validate_invoice` / `create_dispute_draft`。
**怎么保证**：不是靠提示词，是 `policy_qa` 的工具白名单里压根没有这四个工具，模型想调也调不到。
**校验**：`demo_trajectories.py` 断言 forbidden 列表一次都没出现 → 通过。

### 示例 b：整月争议核对（含建单）

> 用户：「帮我核上个月华东速运的争议运费」

预期路径：查制度 → 查额度 → 要凭证 → 校验 → 建草稿（中断确认）→ 作答，**5 轮决策 + 1 次人工确认**。

实测轨迹（约 32–40 秒；下例为 5 轮探索 + 建单收尾轮，8 次工具调用 + 1 次人工确认）：

| 轮 | 工具 | 关键参数 | 结果摘要 |
|---|---|---|---|
| 1 | `load_skill` / `list_bills` | carrier=华东速运, month=2026-09 | 切到 freight_dispute；1 张：B202609-HD-001 |
| 2–3 | `search_policy` + `get_bill_lines` + `get_dispute_quota` + `request_evidence` | 同上 | 命中条款；3 行 / 7533.76 元；剩余额度 38000 元；缺「附加费依据」 |
| 4 | `calc_expected_freight` | 3 张运单 | 应收合计 6992.96 元 |
| 5 | `validate_invoice` | + 2 个真实 citation_id | **fail · 1 处问题 · 差额 540.80 元** |
| 收尾 | `create_dispute_draft` | validation_id + amount=540.80 | **中断 → 人工确认 → 草稿 d_20261030_xxx** |

差额成因（服务端判定，非模型推断）：第 3 行 2600kg 应落「>2000kg」档 1.4 元/kg，账单按 1.6 元/kg 收 → 多收 540.80 元。

> 模型压缩得好时，第 5 轮就直接建单（实测两种都出现过）；收尾轮只是兜底，不是固定动作。

### 示例 c：只校验单张单据

> 用户：「这张称重单 CD2026090203 能计入运费吗」

预期路径：只调 `check_weight_ticket` 给结论，不查制度、不查额度、不建草稿。

实测轨迹（5.9 秒，1 轮）：

| 轮 | 工具 | 参数 | 结果 |
|---|---|---|---|
| 1 | `check_weight_ticket` | ticket_no=CD2026090203 | **不可计入运费** |

判定依据（四条硬规则，全在 `domain.check_weight_ticket` 里，不由模型判断）：
称重 2808kg 与运单申报 2600kg 偏差 8.0%，超过允许的 5%；且缺发货方签章。

**约束**：未调用 `get_dispute_quota` / `request_evidence` / `validate_invoice` / `create_dispute_draft` → 通过。

---

## 五、已知限制

1. **5 轮很紧**。依赖链决定最短序列是 `(search+list+quota) → (bill+evidence) → calc → validate → draft`，正好用满；模型一旦把某个工具拆成单独一轮，就要靠「建单收尾轮」兜底。如果用户中途补充信息（"再看看另一张账单"），后面几轮大概率被 `max_rounds` 截断，只能给阶段性结论。这是需求的硬约束，不是 bug——若要更宽松，可把 `MAX_ROUNDS` 做成按 skill 配置。
2. **中断只在单进程内有效**。`_CONFIRMS` 是进程内存字典，多 worker 部署时要换成 Redis 之类的共享状态，否则确认请求会打到没有挂起者的进程上。
3. **页面刷新会丢掉确认卡**。草稿卡片可以从 `/api/disputes` 重建（已在顶部"待确认草稿"栏实现），但**尚未执行的确认中断**无法重建——连接一断，那次中断就作废，用户需要重新问一次。这是"确认只对当前连接有效"的代价，是刻意的。
4. **人工确认耗时被算进工具耗时**。日志里 `create_dispute_draft` 的 `duration_ms` 含员工思考时间；如果阶段 5 要统计"工具性能"，记得按 `kind='confirm'` 单独剔除。
