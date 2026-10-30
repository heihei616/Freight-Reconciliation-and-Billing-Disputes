# API 说明

服务启动后可访问 `/docs` 查看 FastAPI 自动文档。除登录、健康检查和静态资源外，接口使用 JWT HttpOnly Cookie，也支持 `Authorization: Bearer <token>`。

错误信封：`{"code": 1001, "message": "问题不能为空", "request_id": "..."}`。SSE 开始后的错误通过 `event: error` 下发，HTTP 状态保持 200。

## 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/login` | 登录页 |
| POST | `/auth/login` | 表单字段 `username`、`password`、`next` |
| POST | `/auth/logout` | 删除登录 Cookie |
| GET | `/api/auth/me` | 当前用户与角色 |

## 员工接口

### `POST /api/chat`

请求：

```json
{"session_id": null, "question": "核对 B202610-SF-001", "mock": false}
```

`session_id` 为空会新建会话。响应为 `text/event-stream`，主要事件包括 `state`、`tool_call`、`tool_result`、`confirm_open`、`confirm_delta`、`confirm_ready`、`confirm_closed`、`draft`、`delta`、`done`、`error`。

### `POST /api/agent/confirm`

```json
{"confirm_id": "confirm_open 返回的编号", "action": "confirm", "statement": null}
```

`action` 可为 `confirm` 或 `cancel`；`statement` 可携带员工修改后的正文。确认等待 180 秒，超时按取消处理。

### 其他

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sessions` | 当前用户的会话 |
| GET | `/api/sessions/{session_id}/messages` | 指定会话的消息，校验归属 |
| GET | `/api/disputes` | 当前用户的争议草稿 |
| POST | `/api/disputes/{draft_id}/submit` | 员工提交草稿，模型无此工具 |
| GET | `/api/agent/runs?limit=20` | 当前用户的 Agent 运行记录 |
| GET | `/api/agent/runs/{run_id}` | 单次运行的逐轮决策与工具轨迹 |
| GET | `/api/health` | 知识库与模型配置状态，不返回密钥 |

## 管理接口（admin）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin` | 管理概览 |
| GET | `/admin/knowledge` | 知识库管理页 |
| GET | `/admin/api/documents` | 文档与切片统计 |
| POST | `/admin/api/documents` | 上传文档并重建索引；md/markdown/txt/pdf，最大 5MB |
| DELETE | `/admin/api/documents/{filename}` | 删除文档并重建索引 |
| POST | `/admin/api/documents/reindex` | 全量重建索引 |

上传扫描件或无法抽取文本的 PDF 会返回明确错误，不会静默入库。
