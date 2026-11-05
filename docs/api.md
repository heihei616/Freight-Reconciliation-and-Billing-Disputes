# API 说明

服务启动后可访问 `/docs` 查看 FastAPI 自动生成的接口文档。除登录、健康检查和静态资源外，接口使用 JWT HttpOnly Cookie，也支持 `Authorization: Bearer <token>`。

**鉴权约定**：页面请求未登录会 303 跳转到登录页；接口请求返回 401 + JSON 信封。

## 统一错误信封

所有非 SSE 错误（以及 SSE 流内的 `error` 事件）都使用同一个结构：

```json
{"code": 1001, "message": "问题不能为空", "request_id": "f1784f5d9062"}
```

`request_id` 会同时写进服务端日志，报障时用它定位一次具体请求。

分两个阶段，因为 SSE 的响应头一旦发出，HTTP 状态码就改不了了：

| 阶段 | HTTP | 表现 |
|---|---|---|
| 建流前（参数、鉴权、知识库未就绪、限流） | 4xx / 5xx | JSON 信封 |
| 建流后（检索异常、模型超时） | 200 | `event: error` + 同样的信封 |

### 错误码表

| 码 | HTTP | 含义 | 常见原因 |
|---|---|---|---|
| 1001 | 400 | 问题不能为空 | 传了空白字符串 |
| 1002 | 400 | 问题超过 2000 字 | 一次贴了整份文件 |
| 1003 | 400 | 请求参数格式不正确 | JSON 字段类型错 |
| 1004 | 404 | 会话不存在 | session_id 拼错或已删除 |
| 1005 | 403 | 无权访问该会话 | 拿别人的 session_id |
| 1006 | 401 | 未登录或登录已过期 | Cookie 过期，需重新登录 |
| 1007 | 400 | 不支持的文件类型 | 上传了 .docx |
| 1008 | 413 | 上传文件过大 | 超过 5MB |
| 1009 | 401 | 用户名或密码不正确 | — |
| 1010 | 403 | 账号已停用 | — |
| 1011 | 403 | 需要管理员权限 | 员工访问 /admin |
| 1012 | 400 | 提示词不能为空 | — |
| 1013 | 400 | 提示词超过 4000 字 | — |
| 1014 | 400 | 文件无法解析或疑似扫描件 | 需要先 OCR |
| 1015 | 500 | 索引重建失败 | 看服务日志 |
| 1016 | 404 | 草稿不存在或无权操作 | 草稿属于别人 |
| 1017 | 409 | 该草稿已提交 | 重复提交 |
| 1018 | 409 | 确认请求已失效或无权确认 | 超过 180 秒或已处理 |
| 1019 | 400 | 草稿正文超过 1200 字 | 让模型压缩重写 |
| 1020 | 429 | 提问太频繁 | 触发滑动窗口限流 |
| 1021 | 429 | 今日问答次数已用完 | 触发每日配额 |
| 1022 | 429 | 当前排队人数较多 | 并发闸门排队超时 |
| 2001 | 503 | 知识库尚未就绪 | 索引还没建完 |
| 2002 | 503 | 尚未配置大模型 | 缺 LLM_API_KEY |
| 3001 | 200 | 检索失败 | 向量服务异常 |
| 3002 | 200 | 模型服务调用失败 | 超时或供应商 5xx |
| 3003 | 200 | 响应生成中断 | 内部异常 |
| 3004 | 200 | 本轮工具调用次数已达上限 | 问题太大，要拆 |
| 9999 | 500 | 服务内部错误 | 未分类异常 |

## 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/login` | 登录页；支持 `?next=` 回跳 |
| POST | `/auth/login` | 表单字段 `username`、`password`、`next`；成功返回 303 并设置 HttpOnly Cookie |
| POST | `/auth/logout` | 删除登录 Cookie，303 回登录页 |
| GET | `/api/auth/me` | 当前用户 `{id, username, display_name, role}` |

## 员工接口

### `POST /api/chat`

核心接口，返回 `text/event-stream`。

请求：

```json
{"session_id": null, "question": "核对 B202610-SF-001", "mock": false, "top_k": 3, "bm25_weight": 0.5}
```

`session_id` 为空会新建会话。`mock=true` 强制走离线模拟，便于演示。

响应事件按以下顺序出现（`*` 表示可重复）：

```text
meta → status* → [state] → (tool_call → tool_result)* → sources
     → delta* → [confirm_open → confirm_delta* → confirm_ready → confirm_closed]
     → [draft] → done
```

| 事件 | 数据字段 | 前端怎么用 |
|---|---|---|
| `meta` | `session_id`、`user_message_id`、`trace_id`、`mock` | 记住会话，刷新后恢复 |
| `status` | `stage`、`text` | 显示「正在检索制度…」 |
| `state` | `skill_title`、`state` | 提示已切换能力包 |
| `tool_call` | `name`、`title`、`args`、`round` | 步骤条插入「执行中」 |
| `tool_result` | `name`、`ok`、`summary`、`duration_ms` | 步骤条改成成功/失败 |
| `sources` | `citations[]` | 渲染引用卡片 |
| `delta` | `text` | 打字机输出正文 |
| `confirm_open` | `confirm_id`、`bill_no`、`amount`、`issues[]`、`timeout_sec` | 渲染确认卡，按钮先禁用 |
| `confirm_delta` | `confirm_id`、`text` | 草稿正文边流边显示 |
| `confirm_ready` | `confirm_id` | 按钮可用（这是「可以点了」的信号） |
| `confirm_closed` | `confirm_id`、`decision`、`waited_ms` | 显示已确认/已取消/超时 |
| `draft` | `draft_id`、`bill_no`、`amount`、`statement` | 渲染草稿卡片 |
| `done` | `message_id`、`elapsed_ms` | 收尾，解锁输入框 |
| `error` | `code`、`message` | 显示错误条与重试入口 |

### `POST /api/agent/confirm`

确认或取消一次挂起的建单。

```json
{"confirm_id": "confirm_open 返回的编号", "action": "confirm", "statement": null}
```

`action` 取 `confirm` 或 `cancel`；`statement` 可携带员工修改后的正文（不超过 1200 字）。确认等待 180 秒，超时按取消处理。

**重复调用是安全的**：服务端把第二次请求判成「已处理」并返回 1018，不会产生第二张草稿。

### 其他员工接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sessions` | 当前用户的会话列表，按更新时间倒序 |
| GET | `/api/sessions/{session_id}/messages` | 指定会话的消息与引用，校验归属 |
| GET | `/api/disputes` | 当前用户的争议草稿（含待提交与已提交） |
| POST | `/api/disputes/{draft_id}/submit` | 员工提交草稿；模型没有这个工具 |
| GET | `/api/agent/runs?limit=20` | 当前用户的 Agent 运行记录 |
| GET | `/api/agent/runs/{run_id}` | 单次运行的逐轮决策与工具轨迹 |
| GET | `/api/health` | 知识库与模型状态，不返回密钥 |

`GET /api/health` 返回示例：

```json
{
  "status": "ok",
  "knowledge_base": {"ready": true, "documents": ["discovery-report.md"], "document_count": 1, "chunk_count": 68},
  "llm": {"provider": "openai", "model": "qwen3.8-flash", "mock": false, "configured": true}
}
```

> `mock: true` 说明没读到密钥，回答是预置文本，不能用于验收。

## 管理接口（admin）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin` | 管理概览 |
| GET | `/admin/knowledge` | 知识库管理页 |
| GET | `/admin/api/documents` | 文档列表与切片统计 |
| POST | `/admin/api/documents` | 上传文档并重建索引；md / markdown / txt / pdf，最大 5MB |
| DELETE | `/admin/api/documents/{filename}` | 删除文档并重建索引 |
| POST | `/admin/api/documents/reindex` | 全量重建索引 |

上传扫描件或无法抽取文本的 PDF 会返回 1014，不会静默入库。

## 调用示例

用 curl 走一遍完整链路（Cookie 由登录接口下发）：

```bash
curl -c cookie.txt -X POST http://127.0.0.1:8000/auth/login \
  -d "username=employee&password=emp123"

curl -b cookie.txt -N -X POST http://127.0.0.1:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"question":"计费争议必须在几个工作日内提出？"}'
```

`-N` 关闭 curl 的缓冲，否则 SSE 内容会攒到结束才显示。

## 限流

默认每用户每分钟 10 次提问、每日 200 次模型调用、全局最多 4 个 Agent 循环并发。超限返回 429 与对应错误码，`message` 里带剩余等待秒数。

参数可通过环境变量调整（见 `.env.example`），`RATE_LIMIT_ENABLED=0` 可整体关闭，仅建议本地调试使用。
