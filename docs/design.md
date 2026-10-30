# 架构与设计

## 1. 设计目标

系统面向结算岗与财务人员，处理制度查询、单票运费计算、整月账单核对与争议草稿。核心原则是：**模型负责选择下一步，业务系统负责事实、算术、权限和最终结论。**

## 2. 分层

| 层 | 模块 | 职责 |
|---|---|---|
| Web | `app.py` | JWT 鉴权、Jinja2 页面、SSE、知识库与草稿接口 |
| Agent | `agent.py` | 模型决策 → 工具执行 → 结果回填 → 再决策，最多 5 轮探索 |
| 能力包 | `skills.py` | 制度问答与争议处理的工具白名单、流程指令和路由 |
| 工具 | `tools.py` | 13 个 Function Calling schema、参数校验、前置条件闸门 |
| 业务核心 | `core/domain.py` | 运单、账单、价目、额度、凭证和确定性运费计算 |
| 检索 | `rag_service.py`、`reimburse_agent_mvp.py` | 文档解析、切片、FAISS + BM25 混合召回 |
| 数据 | `db.py` | SQLite 会话、消息、运行轨迹、校验与草稿 |
| 模型 | `llm.py` | OpenAI 兼容流式调用、超时与 mock 降级 |

## 3. 为什么计算不交给模型

模型容易把体积重、阶梯档和燃油费率算错。`compute_freight()` 输出完整推导，`validate_bill()` 独立产生 pass / warn / fail 与差额。模型只能读取结果，不能传 verdict，也不能把争议金额改成其他数字。

## 4. 工具顺序约束

1. `search_policy` 取得真实 citation_id。
2. 无账单号时用 `list_bills`；`get_bill_lines` 读取逐行事实。
3. `calc_expected_freight` 计算账单涉及的全部运单。
4. `validate_invoice` 校验事实轨迹、制度引用并发放 validation_id。
5. 仅 fail / warn 可调用 `create_dispute_draft`，金额必须等于差额。
6. 建单前触发人工确认；提交接口只暴露给员工，不在模型工具集中。

前置条件由服务端回查会话轨迹强制执行，提示词不是唯一防线。

## 5. 轮次与人工中断

探索轮上限 5 轮，工具调用上限 12 次，同指纹调用上限 2 次。若已有校验结论但还没建单，追加一轮只含建单/转人工的收尾轮。建单时通过 `asyncio.Event` 暂停，SSE 推送草稿；180 秒未确认按取消处理。

当前确认事件在单进程内存中，多 worker 部署需替换为 Redis 等共享状态。

## 6. 知识与数据目录

- `data/knowledge/`：唯一会被索引的目录。
- `docs/`：设计和项目文档，不参与检索，避免示例回答污染制度引用。
- `data/runtime/`：本地数据库与 WAL，已被 `.gitignore` 排除。
- `~/.logistics_rag_web_index/`：默认索引缓存，可用 `RAG_INDEX_DIR` 覆盖。

## 7. 验证策略

`tests/test_core.py` 验证金额与路由；`scripts/check_gates.py` 绕过模型直接攻击工具闸门；`tests/data/eval_cases.json` 覆盖制度、工具、违规意图和多轮上下文；`examples/demo_agent.py` 用真实模型回放典型轨迹。
