# 运维手册

这份文档面向「要把它跑起来并长期维护」的人，不是开发者。
内容按上线顺序组织：装之前做什么、怎么起、怎么确认起对了、出问题看哪里。

---

## 1. 部署形态

### 1.1 单机裸跑（验证与内网小规模）

适合 3~10 人的结算组。一个进程、一个 SQLite 文件，不需要额外中间件。

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows；macOS/Linux 用 source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # 填模型密钥
PYTHONPATH=src python scripts/preflight.py
PYTHONPATH=src python -m logistics_agent.main
```

### 1.2 Docker Compose（推荐的交付方式）

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps                 # 等 STATUS 出现 healthy
```

数据库、索引、知识库文档、备份都在命名卷 `logistics-agent-data` 里，
容器重建不会丢。要看卷里的内容：

```bash
docker compose exec app ls -lh /app/data/runtime
```

生产环境必须显式声明，否则启动校验会按开发标准放行：

```bash
APP_ENV=production docker compose up -d
```

### 1.3 反向代理要求

前置 Nginx / Caddy 时有三件事必须配，否则会出现「登录成功但立刻掉线」
或「回答一次性蹦出来而不是逐字显示」：

| 配置 | 值 | 原因 |
|---|---|---|
| `X-Forwarded-Proto` | `$scheme` | 应用据此判断是否给 Cookie 加 Secure、是否下发 HSTS |
| `proxy_buffering` | `off` | SSE 被缓冲后，前端要等整段生成完才看到字 |
| `proxy_read_timeout` | `300s` | 一次核对含人工确认可能挂 180 秒，默认 60 秒会被代理切断 |

Nginx 片段：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

并把 `FORWARDED_ALLOW_IPS` 设为代理地址，应用才会信任这些头。

---

## 2. 上线流程

按顺序做，每一步都有明确的通过标准：

| 步骤 | 命令 | 通过标准 |
|---|---|---|
| 1. 配置 | 编辑 `.env` | 模型密钥、`JWT_SECRET`（≥32 字符）、`COOKIE_SECURE=1` 齐备 |
| 2. 预检 | `PYTHONPATH=src python scripts/preflight.py --env production` | 无「失败」项 |
| 3. 启动 | `python -m logistics_agent.main` / `docker compose up -d` | 日志出现「知识库就绪=True」且无配置错误 |
| 4. 就绪 | `curl -fsS http://127.0.0.1:8000/readyz` | HTTP 200，三项检查全 true |
| 5. 冒烟 | `PYTHONPATH=src python scripts/check_confirm_api.py` | 11 项通过 |
| 6. 基线备份 | `PYTHONPATH=src python scripts/backup_db.py` | 生成 `chat-*.db` 并通过完整性校验 |
| 7. 首个管理员 | 启动前设 `ADMIN_INITIAL_PASSWORD`，或先建库后用脚本改密 | `set_password.py --list` 全部显示「已自定义」 |

第 2 步的 `--env production` 很关键：不带这个参数时，预检按开发标准执行，
`COOKIE_SECURE`、`METRICS_TOKEN` 这类只在真实部署里才成立的项会被跳过；
带上之后它们会参与判定，缺一项就拦住发布。

---

## 3. 探针：活着 ≠ 能接流量

| 端点 | 回答的问题 | 失败时该做什么 |
|---|---|---|
| `GET /healthz` | 进程还在响应吗 | 重启容器 |
| `GET /readyz` | 能接新流量吗 | 从负载均衡摘掉，**不要重启** |
| `GET /api/health` | 人看的详细状态（含模型名、切片数） | 排查用，**需登录** |

分工的原因：索引重建、数据库短暂锁等待都会让 `/readyz` 失败，但进程是好的；
把它们合成一个端点，编排就会误杀正在重建索引的实例。

Kubernetes 配置：

```yaml
livenessProbe:
  httpGet: { path: /healthz, port: 8000 }
  initialDelaySeconds: 30
  periodSeconds: 30
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }
  initialDelaySeconds: 10
  periodSeconds: 10
  failureThreshold: 3
```

`/readyz` 的三项检查都是本地状态（数据库可读、索引已加载、模型名已配置），
不发网络请求——探针自己不能成为故障源。

---

## 4. 指标与告警

`GET /metrics` 输出 Prometheus 文本格式。默认**只允许本机访问**；
跨机抓取需设置 `METRICS_TOKEN`，并在抓取端带 `Authorization: Bearer <token>`
（也接受 `?token=`，方便不支持自定义头的抓取器）。

「只允许本机」有一个容易踩空的前提：它看的是 `request.client.host`。
**同机反向代理下，所有外部请求都来自 127.0.0.1**，只看 IP 等于公开。
因此：

- 带 `X-Forwarded-For` 的请求一律按非本机处理（代理转进来的就是外部流量）；
- `APP_ENV=production` 时，开启指标却没配 `METRICS_TOKEN` 会**拒绝启动**。
  不需要指标就显式设 `METRICS_ENABLED=0`，不要靠「反正没人知道这个路径」。

| 指标 | 类型 | 说明 |
|---|---|---|
| `logistics_build_info` | gauge | 版本号，固定为 1 |
| `logistics_uptime_seconds` | gauge | 进程运行时长 |
| `logistics_sse_active` | gauge | 当前活跃的 SSE 流 |
| `logistics_http_requests_total` | counter | 按方法 / 路由模板 / 状态码 |
| `logistics_http_request_seconds` | summary | 请求耗时 |
| `logistics_pending_confirms` | gauge | 仍在等待窗口内的建单确认 |
| `logistics_db_size_bytes` | gauge | 数据库文件大小 |
| `logistics_knowledge_chunks` | gauge | 索引切片数 |

建议的四条告警（阈值按实际流量调整）：

| 告警 | 表达式 | 含义 |
|---|---|---|
| 错误率升高 | `sum(rate(logistics_http_requests_total{status=~"5.."}[5m])) / sum(rate(logistics_http_requests_total[5m])) > 0.05` | 依赖或代码出问题 |
| 确认积压 | `logistics_pending_confirms > 5` | 有人卡在闸门前，或确认超时在堆积 |
| 流卡住 | `logistics_sse_active > 20` 持续 10 分钟 | 模型变慢或客户端大量断开未回收 |
| 库膨胀 | `logistics_db_size_bytes > 2e9` | 该清理日志或评估换库 |

标签用的是**路由模板**（`/api/sessions/{session_id}`）而不是真实路径，
否则每个会话 ID 都会生成一条时间序列，抓取端会先崩。

---

## 5. 备份与恢复

### 5.1 备份

```bash
PYTHONPATH=src python scripts/backup_db.py
# 容器内：
docker compose exec app python scripts/backup_db.py
```

它用的是 SQLite 官方在线备份 API，服务运行中也能拿到一致性快照，
不需要停服。备份完成后会做三件事：`integrity_check`、核心表行数核对、
把 WAL 合并回主文件（保证「一个文件拿走就能用」）。

默认保留最近 14 份，改 `--keep`：

```bash
PYTHONPATH=src python scripts/backup_db.py --keep 30
```

建议放到每日定时任务里：

```cron
0 2 * * * cd /opt/logistics-agent && PYTHONPATH=src /opt/venv/bin/python scripts/backup_db.py --keep 30 >> /var/log/logistics-backup.log 2>&1
```

### 5.2 恢复

恢复必须停服——运行中的进程会把内存里的数据继续写回旧库。

```bash
# 1. 停服
docker compose stop app          # 或结束裸跑进程

# 2. 先确认备份可用
PYTHONPATH=src python scripts/backup_db.py --verify data/runtime/backups/chat-20261105-021500.db

# 3. 恢复（会先把当前库另存为 chat-before-restore-*.db）
PYTHONPATH=src python scripts/backup_db.py --restore data/runtime/backups/chat-20261105-021500.db --yes

# 4. 起服并确认
docker compose start app
curl -fsS http://127.0.0.1:8000/readyz
```

脚本会在恢复前主动检查 8000 端口是否仍在监听，并在恢复后清理过期的
`-wal` / `-shm`——残留的 WAL 会把刚恢复的数据又盖回去。

### 5.3 恢复演练

备份最怕「从来没试过恢复」。建议每月做一次：

1. 把最近备份恢复到一台临时实例；
2. 登录确认最近一周的会话与草稿都在；
3. 跑一次 `preflight`，确认 `integrity_check=ok`；
4. 删掉临时实例。

RPO 由备份频率决定（每日一次 = 最多丢一天）；RTO 约 5 分钟，
取决于备份体积与磁盘速度。

---

## 6. 凭证与密钥

### 6.1 演示账号只存在于开发环境

开发环境（`APP_ENV` 不是 `production`，且没有显式设 `SEED_DEMO_USERS=0`）首次启动会创建
`admin/admin123`、`employee/emp123` 两个演示账号，让「双击就能演示」成立。

**生产环境不播种。** 口令随代码发版，一旦进了生产，等于后台对所有见过仓库的人开放；
更糟的是它会在启动流程里制造一个死锁——先建出演示账号，紧接着又因为「仍在使用演示口令」
拒绝启动，而报错建议的「登录后台改密码」在服务未起来时根本走不通。

生产首次启动的账号有两个来源，任选其一：

1. **引导口令**（推荐，容器友好）：设 `ADMIN_INITIAL_PASSWORD`（≥12 位，走与命令行
   改密同一套强度规则）后启动，系统据此建一个 `admin`。登录后**立刻改密**，并把该
   变量从环境中删掉。留着它在每次启动时只打印一行提醒，不会覆盖已有口令。
2. **先建库再改密**：临时以 `APP_ENV=development` 起一次完成建表，执行
   `python scripts/set_password.py --user admin` 设好口令，再切回 `production`。

两条路都不会把明文口令写进日志或代码。若生产库为空又没配 `ADMIN_INITIAL_PASSWORD`，
启动会直接失败并给出上面两条路径——这比「起来了但没有任何人能登录」更容易排查。

登录页上的「演示账号」提示只在口令仍等于演示值时才渲染。轮换之后提示自动消失，
不会出现「照着页面上的口令输、却提示密码错误」这种最难自查的状态。

开发库首次启动后的样子：

```bash
python scripts/set_password.py --list
```

```
数据库：/app/data/runtime/chat.db
账号          姓名        角色      状态    口令
admin         系统管理员  admin     启用    演示口令（必须更换）
employee      张员工      employee  启用    演示口令（必须更换）

仍在用演示口令：admin、employee
更换：python scripts/set_password.py --user admin
```

改密有两种方式：

```bash
# 交互式：输入不回显，要求输两次（推荐人手操作）
python scripts/set_password.py --user admin

# 生成随机强口令并打印一次，抄进密码管理器
python scripts/set_password.py --user admin --generate

# 自动化：口令从标准输入进，不落 shell 历史
printf '%s\n' "$NEW_PASSWORD" | python scripts/set_password.py --user employee --stdin
```

口令规则：至少 12 字符、不能与演示口令相同、不含 `password` / `123456` 这类片段、
首尾不能有空白、不能是同一字符重复。**不强制「大小写数字符号各一个」**——
那类规则会把用户推向 `Abcd1234!` 这种结构固定、字典可枚举的口令，
实际强度低于一个长随机串。

脚本写库后会立刻回读，用 `auth.verify_password()` 确认新哈希真的能通过校验，
再打印结果。所以不会出现「提示改成功、登录却失败」这种最难排查的状态。

> **改密不等于踢下线。** 已签发的令牌在有效期内仍然可用（默认 8 小时，
> 由 `JWT_EXPIRE_HOURS` 控制）。要立即让所有会话失效，只能轮换 `JWT_SECRET`
> 并重启服务，代价是全部用户重新登录。怀疑令牌泄漏时按后者处理。

### 6.2 JWT_SECRET

不配也能跑：程序会在用户主目录生成 `~/.logistics_jwt_secret` 并复用，
重启后已签发的令牌仍然有效。这适合单机。

**容器与多实例必须显式配置**，否则每个实例各生成一份，令牌互不认账；
容器重建后文件消失，所有人被踢下线。生成方式：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

把结果写进 `.env` 的 `JWT_SECRET=`，或直接用编排平台的 secret 机制注入。
`preflight` 在未配置时给警告、长度不足 32 字符时也给警告。

### 6.3 轮换节奏与人员变动

| 对象 | 时机 | 影响 |
|---|---|---|
| 引导口令 `ADMIN_INITIAL_PASSWORD` | 首次启动后立即失效（改密并从环境删除） | 无（只用于建第一个管理员） |
| `JWT_SECRET` | 怀疑泄漏、人员离职、按季度 | 全部用户需重新登录 |
| 模型 `LLM_API_KEY` | 按供应商建议（通常 90 天） | 换完重启，进行中的回答会中断 |
| 员工账号 | 人员离职当天 | 改 `users.is_active=0`（**暂无界面入口**，需直接改库或后续补管理页） |

轮换顺序有个容易搞反的地方：**先改口令，再轮换 `JWT_SECRET`**。

反过来的话，密钥一轮换所有人都被踢下线，而这时口令还是旧的——
他们会拿旧口令重新登录，等于给旧口令续了一段命，改密的意义被抵消掉。
按「先改口令」的顺序，重新登录时就必须用新口令，最后一步轮换密钥
把残留的旧会话一次性清干净。

---

## 7. 日志与审计

- **应用日志**：stdout，交给容器运行时或 systemd 收集。每条错误都带
  `request_id`，与响应头 `X-Request-Id`、错误信封里的 `request_id` 一致。
  Docker 部署已在 compose 里限制单文件 10MB × 5 份，防止写满磁盘。
- **业务日志**：`agent_runs` / `agent_steps` / `tool_calls` 三张表，
  后台「对话审计」页可按员工、状态、关键词、时间筛选。
- **保留期**：`LOG_RETENTION_DAYS`，默认 90 天，后台协程每 24 小时清理一次。
  设为 0 表示只保留手动清理入口。
- **脱敏**：`LOG_MASKING=1` 时，密钥整段抹除、手机号/邮箱/身份证遮中间、
  金额保留（审计需要）。关闭只应出现在本地调试。

---

## 8. 容量与扩容边界

| 维度 | 当前能力 | 到顶的信号 | 下一步 |
|---|---|---|---|
| 并发用户 | 单实例 4 个 Agent 循环（`AGENT_MAX_CONCURRENT`） | 排队超时 `1022` 增多 | 调大并发；再往上换多实例 |
| 数据库 | SQLite 单写入锁，3~5 人舒适 | 写锁等待、`database is locked` | 迁移 PostgreSQL |
| 限流 | **进程内存**计数 | 多实例时额度被放大 N 倍 | 计数迁到 Redis |
| 索引 | 单机 FAISS + BM25，全量重建 | 文档上千份后重建变慢 | 换独立向量库 |
| 备份 | 单文件，秒级 | 库超过数 GB | 改为增量或物理备份 |

**确认状态已经可以跨实例**（存在 `pending_confirms` 表），所以多 worker
不会再出现「点确认报不存在」。但限流计数还在内存，横向扩展前要先换 Redis，
否则每个实例各算一份额度。

---

## 9. 故障处置

### 启动被配置校验拒绝

日志形如：

```
[配置] 环境 production，发现 2 个错误、0 个警告
  [错误] JWT_SECRET：生产环境必须显式设置至少 32 字符的随机密钥（当前 0 字符） → ...
```

按提示改 `.env` 即可。这类失败是**故意的**：带着坏配置启动，问题会在第一次
模型调用或第一次上线检查时才爆发，排查成本更高。

生成密钥：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 启动报「空库且没有账号」

```
RuntimeError: 当前环境不播种演示账号（APP_ENV=production 或 SEED_DEMO_USERS=0），
而用户表为空，没有任何账号可以登录。
```

生产环境不会创建 `admin/admin123`，所以全新库必须显式给一个管理员。两条路：

```bash
# 1) 引导口令：设好再启动，登录后立即改密，然后从环境变量里删掉
export ADMIN_INITIAL_PASSWORD='<至少 12 位>'
python -m logistics_agent.main

# 2) 已有空库：直接写库设口令，不需要服务在跑
python scripts/set_password.py --user admin
```

### 启动被「仍在使用演示口令」拒绝

```
RuntimeError: 生产环境仍在使用演示口令的账号：admin。服务已拒绝启动。
```

这是从开发模式切的库：账号还在用 `admin123`。停服后离线改密即可，脚本直接写库：

```bash
python scripts/set_password.py --user admin
```

改完 `python scripts/preflight.py --env production` 确认「演示口令」一项变绿，再启动。

### 登录返回 429：账号或 IP 被临时锁定

连续失败会触发保护：同一账号 15 分钟内失败 5 次，或同一 IP 5 分钟内失败 20 次。
响应带 `Retry-After`，等窗口过去自动恢复。

先判断是哪一类：

- **只有一个人登不上** → 账号维度。多半是自己记错密码。等 15 分钟，或让管理员
  直接重置口令（`python scripts/set_password.py --user <账号>`）。
- **整个办公室都登不上** → IP 维度，通常是反向代理没配好：所有请求被记成
  同一个代理 IP，一个人手误就把所有人锁住。检查 uvicorn 的
  `FORWARDED_ALLOW_IPS` 是否包含反代地址（不能是 `*`，那会让来源 IP 可伪造）。
- **有人明显在撞库** → 看访问日志里同一 IP 的高频 `POST /auth/login`，
  在反代层封 IP；这不属于应用层能解决的事。

计数在进程内存里，**重启服务会清空**——紧急情况下的临时解法，但别当成常规操作。

容器部署还有一层：反代转发进来的源地址通常是 Docker 网桥（如 `172.17.0.1`），
不是 `127.0.0.1`。若 `FORWARDED_ALLOW_IPS` 保持默认，uvicorn 会忽略
`X-Forwarded-For`，所有用户被记成同一个 IP——一个人手误就能触发 IP 维度锁定。
按 `docker network inspect` 的实际网关填写该变量，并且**不要写 `*`**。

### `/readyz` 返回 503
看返回体里的 `checks`，哪项 `ok=false` 就是原因：

- `database`：文件被占用、磁盘只读、路径指错。看 `db.DB_PATH` 是否与预期一致。
- `knowledge_base`：`data/knowledge` 为空，或索引重建失败。看 `detail` 里的错误。
- `model`：`LLM_MODEL` 没配。

### 模型调用 403 / 超时

- `403 insufficient_quota`：供应商额度用尽，与代码无关。
- 超时：`LLM_TIMEOUT` 默认 90 秒。调大前先确认是不是网络问题。
- 所有回答都是模板文本：`LLM_API_KEY` 为空，降级到了 mock。看启动日志的
  `mock=True`。

### 点了确认建单没反应

1. 看浏览器 Network 里 `POST /api/agent/confirm` 的状态码：
   - `409` + 1018：确认已超时或已被处理，重新发起；
   - `403` + 1026：跨站请求被拒，检查是否从非本站页面提交；
   - `200`：确认成功，看 SSE 是否继续输出。
2. 查库确认状态：

```sql
SELECT id, status, created_at, decided_at FROM pending_confirms ORDER BY created_at DESC LIMIT 5;
```

3. 多实例部署时，确认所有实例指向**同一个** `LOGISTICS_DB_PATH`。

### 磁盘写满

按影响排序处理：清理 `data/runtime/backups` 里的旧备份（保留最近 3 份）→
把 `LOG_RETENTION_DAYS` 调小并手动触发清理 → 扩容。
数据库文件本身不要手删。

### 数据库损坏

`preflight` 会报 `integrity_check` 失败。处置顺序：

1. 立即停服，避免继续写入；
2. 用最近备份恢复（见 5.2）；
3. 如果备份也损坏，用 `sqlite3 chat.db ".recover"` 尝试抢救，导出后导入新库。

---

## 10. 安全检查清单

上线前逐项确认，全部可在 `preflight --env production` 里看到结果：

- [ ] `admin` / `employee` 已改口令（`scripts/set_password.py --list` 里全部显示「已自定义」），或账号已停用
- [ ] `JWT_SECRET` 显式设置且 ≥32 字符（见 6.2）
- [ ] `COOKIE_SECURE=1`，且外部访问确实是 HTTPS
- [ ] 模型地址是 https（内网自建模型需显式 `ALLOW_INSECURE_LLM=1`）
- [ ] `.env` 权限 600（POSIX）
- [ ] `/metrics` 未被公网暴露，或已设 `METRICS_TOKEN`
- [ ] 反向代理已关闭 SSE 缓冲、放宽读超时
- [ ] `FORWARDED_ALLOW_IPS` 只填反代地址（不是 `*`），否则登录限流会把全员记成同一个 IP
- [ ] 登录防爆破阈值已按团队规模确认（`LOGIN_MAX_FAILURES` / `LOGIN_IP_MAX_FAILURES`）
- [ ] 首个基线备份已完成，并做过一次恢复演练
