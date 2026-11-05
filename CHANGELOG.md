# 更新记录

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，版本号采用语义化版本。

## [1.4.0] - 2026-11-05

这一版把「能演示」推到「能交付」：启动前会拒绝坏配置，运行中有探针与指标，
出事后有备份与恢复路径，部署有镜像与编排。

### 新增

- **启动配置校验**（`utils/config.py`）：对 9 个数字型变量、4 个开关、2 个枚举、2 个 URL 做类型与范围检查。`LLM_TIMEOUT=90s` 这类拼写错误会在启动时直接失败，而不是等第一个请求进来才 500。
- **生产模式守卫**：`APP_ENV=production` 时强制要求模型密钥、`JWT_SECRET`≥32 字符、`COOKIE_SECURE=1`、非 mock 模型、https 模型地址；仍在使用演示口令的账号会直接阻止启动。每一项都带修正提示，不是一句「配置错误」。
- **存活/就绪探针**：`GET /healthz` 只回答「进程还活着」，`GET /readyz` 检查数据库可读、索引已加载、模型已配置，失败返回 503 与逐项明细。分开的原因：索引重建中应该摘流量，而不是被重启。
- **安全响应头与 CSRF 校验**：所有响应补齐 `nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy`、`Permissions-Policy`、CSP；HTTPS 下自动加 HSTS。对带 Cookie 的写请求校验 `Origin`/`Referer` 同源，跨站提交返回 1026。
- **Prometheus 指标**（`/metrics`，零依赖）：请求量按路由模板与状态码统计、耗时 summary、SSE 活跃数、待确认积压、数据库大小、切片数。默认只允许本机访问，跨机抓取需 `METRICS_TOKEN`。
- **上线预检**（`scripts/preflight.py`）：一条命令检查 Python 版本、依赖、配置、目录权限、数据库完整性与可写性、演示口令、密钥强度、磁盘、端口、备份新鲜度、`.env` 权限；`--env production` 按生产标准执行，`--json` 供流水线解析，退出码可直接做发布门禁。
- **在线备份与恢复**（`scripts/backup_db.py`）：用 SQLite 官方 backup API 取一致性快照，备份后做完整性校验、核心表行数核对，并把 WAL 合并回主文件（保证单文件可独立使用）。支持 `--verify`、`--restore`、`--keep`，恢复前会拒绝在服务运行时执行、并自动另存当前库。
- **初始凭证轮换**（`scripts/set_password.py`）：`--list` 标出仍在用演示口令的账号，`--user` 交互改密（不回显、输两次），`--generate` 生成随机强口令，`--stdin` 供自动化。写库后立刻回读校验，确认新哈希能被登录逻辑认出来。这条补上了一个真实断点：此前预检报「仍在用演示口令」，但界面里没有改密入口，运维只能手写 PBKDF2 哈希。
- **登录防爆破**（`ratelimit.LoginGuard`）：账号维度 15 分钟 5 次、IP 维度 5 分钟 20 次，超限返回 429 + `Retry-After`，在验证口令**之前**拦截，不消耗 PBKDF2 的 CPU。账号不存在时也走一次等价哈希，消除「查无此人响应更快」的计时差，避免用户名枚举。
- **容器化交付**：多阶段 `Dockerfile`（非 root、内置健康检查、libgomp1 依赖）、`.dockerignore`、`docker-compose.yml`（命名卷、日志轮转、优雅停止、健康检查）。
- **首次引导与演示账号分离**：开发环境首次启动照旧播种 `admin` / `employee`，双击就能演示；生产环境**不再播种**，空库改用 `ADMIN_INITIAL_PASSWORD`（≥12 位，走与命令行改密同一套强度规则）建出第一个管理员，库非空时该变量被忽略并提示移除。`SEED_DEMO_USERS` 可显式覆盖环境默认值，生产环境打开会被配置校验拒绝。口令策略统一收敛到 `auth.password_problems()`，启动引导与 `set_password.py` 共用一把尺子。
- `SERVER_HOST` / `SERVER_PORT` / `FORWARDED_ALLOW_IPS` 环境变量，容器与反向代理部署不再需要改配置文件。
- 测试从 140 条增至 256 条，新增配置校验、HTTP 加固、备份恢复、指标、凭据轮换、登录防爆破、首次引导七组共 116 条。

### 变更

- `scripts/check_repo.py` 增加部署产物校验：Dockerfile 里 `COPY` 的路径必须存在、不能被 `.dockerignore` 排除，compose 引用的 env 文件缺失会告警。
- `scripts/check_repo.py` 的编码与换行检查从只扫 `*.py` 扩到文档与配置（Markdown、YAML、TOML、Dockerfile、compose 等），二进制后缀显式跳过。此前 Markdown 被写成 CRLF 不会报错，最终表现为整文件 diff。
- `scripts/deploy.sh` 的启动参数支持代理头，并在注释里写明横向扩展前要先处理限流计数。
- 后台总览新增「待确认建单」统计卡，与 `/metrics` 的 `logistics_pending_confirms` 同源。
- 新增[运维手册](docs/operations.md)：部署形态、上线流程、探针语义、告警建议、备份恢复、故障处置。

### 修复

- **生产环境可以带着 mock 模型上线**：以前只有一句启动告警，现在直接拒绝启动。
- **配置校验与实现不一致**：`EMBEDDING_PROVIDER` 实际支持 `local` / `api`，校验规则最初只认 `openai`，会把正常配置判成错误——现已按真实读取逻辑对齐。
- **文档校验器误报接口**：`check_docs.py` 会把「`POST` / `PUT` / `PATCH`」这类方法枚举读成「方法 + 根路径 `/`」，导致三处假失败。正则改为要求斜杠后必须有实际路径段，文档也改用顿号分隔。
- **预检的修复建议指向不存在的入口**：原来写「登录后台修改密码」，但系统并没有改密接口。现在改为指向 `scripts/set_password.py`，并补上了这个脚本。
- **生产首次启动必然失败（死锁）**：旧流程无条件播种 `admin/admin123`，紧接着又因「仍在使用演示口令」拒绝启动——全新的生产库永远起不来，而报错建议的「登录后台改密码」在服务未起来时也走不通。现在生产不播种，空库由 `ADMIN_INITIAL_PASSWORD` 引导；改密报错也改成「停服后执行 `scripts/set_password.py`」这条真实可用的离线路径。
- **`preflight` 看不见「空库 + 生产 + 无引导口令」**：这个组合的后果是容器起来就退出，日志里只有一句 `RuntimeError`。现在新增「首次引导」检查项，在部署前就给出结论。

### 安全修复

- **登录接口可无限重试**：没有次数限制，配合 20 万次 PBKDF2，既能让攻击者慢慢猜密码，也能被用来打满 CPU。现在按账号与 IP 双维度计数，超限 429 并给出等待时间。
- **用户名可被计时枚举**：`user is not None and verify_password(...)` 短路求值，账号不存在时立即返回，比密码错误快几十毫秒。现在两种情况耗时一致。
- **开放重定向绕过**：`next` 只检查了 `startswith("//")`，但 `/\evil.com`、`/%5Cevil.com` 会被浏览器当成协议相对 URL。现在先解码再校验，并拒绝反斜杠与控制字符。
- **上传可打爆内存**：只在 `file.size` 有值时才检查大小，分块传输时该字段为 `None`，整个文件仍会被读进内存。现在按「上限 + 1」字节有界读取。
- **`/api/health` 匿名暴露内部信息**：切片数、文档清单、模型名无需登录即可读取，且返回了 `base_url`（可能指向内网模型网关）。现在要求登录，并从 `provider_info()` 移除 `base_url`。
- **`limit=-1` 绕过条数上限**：SQLite 把负数 `LIMIT` 当作不限制，一次能拉全部历史。改用 `Query(ge=1, le=100)` 在参数层拒绝。
- **`/metrics` 的「仅本机」可被反向代理架空**：同机 nginx 转发时所有外部请求都来自 127.0.0.1，IP 判断形同虚设。现在带 `X-Forwarded-For` 的请求一律按非本机处理；`APP_ENV=production` 下开启指标却未配 `METRICS_TOKEN` 会拒绝启动，要求显式给令牌或显式关闭。
- **删除文档对 `..` 返回 500**：`Path("..").name` 仍是 `..`，拼进目录后 `unlink` 抛 `IsADirectoryError`。现在只允许删除本目录下扩展名受支持的文件。

## [1.3.0] - 2026-11-05

### 新增

- **确认状态持久化**：新增 `pending_confirms` 表，把「建单前中断」的状态从进程内存搬进 SQLite。多 worker 部署时确认请求打到哪个进程都能处理，重启服务也不会让还没超时的确认变成死按钮。唯一事实来源是数据库，本地 `asyncio.Event` 只用于同进程快速唤醒；跨进程由等待方每 0.4 秒读一次状态。
- **确认终态可审计**：`pending / confirmed / cancelled / timeout / abandoned` 五态，带 `decided_at`、`decided_by` 与员工最终采用的正文。页面关闭或连接中断会落 `abandoned`，超时落 `timeout`，不再有「记录留在 `awaiting_confirm` 但没人知道后来怎样」的悬空状态。
- **`GET /api/agent/confirm/pending`**：按会话查询仍在等待窗口内的确认，供页面刷新或切换会话后重建确认卡。过期的记录不会返回。
- **待办栏合并展示**：首页顶部现在同时列出「挂起的建单确认」和「待提交的草稿」，两类卡片都能在刷新后恢复。
- **确认正文可编辑**：员工在点确认前可以直接改草稿措辞，改后的版本随确认一起提交（服务端 `statement` 字段，上限 1200 字）；不传则沿用模型生成的原文。
- 测试从 123 条增至 140 条，新增 `tests/test_confirm.py`：越权确认、重复点击、过期拒绝、收尾不可伪造、跨进程读取决策五组。

### 修复

- **多 worker 下确认必然失败**：`_CONFIRMS` 是模块级字典，确认请求被负载均衡打到另一个 worker 时，那边没有这条记录，员工只会看到 409。现在改由 `WHERE status='pending'` 的原子更新抢占，只有一个请求能成功。
- **刷新页面等于放弃建单**：以前挂起的 `asyncio.Event` 无法重建，刷新后草稿卡片在、确认卡没了。现在正文与时限都在库里，重建的卡片仍然可点（是否有效由服务端的 `expires_at` 判定，不看前端倒计时）。
- **取消后残留 `awaiting_confirm`**：连接断开时只清理内存字典，确认记录和运行记录都停在等待态。现在确认落 `abandoned`，运行落新增的 `cancelled` 状态，审计页显示「连接中断」。
- **停机期间到点的确认无人处理**：启动时先批量把 `expires_at` 已过的记录标成 `timeout`，避免待办列表里出现点了必然失败的卡片。

### 变更

- `scripts/deploy.sh` 的注释更新：确认中断已支持多 worker，但限流计数仍在内存，横向扩展前要先换 Redis 计数器。
- 设计说明的「已知限制」重写：移除「确认在单进程内存」「刷新丢确认」「没有日志保留期」三条已解决项，补上跨进程 0.4 秒延迟、多标签页不自动同步、`pending_confirms` 不自动清理三条新边界。

## [1.2.0] - 2026-11-05

### 新增

- **提示词模板管理**：`/admin/prompt` 页面，配合 `GET /admin/api/prompt`、`PUT /admin/api/prompt`、`DELETE /admin/api/prompt` 与 `POST /admin/api/prompt/restore/{id}`。后台维护的是**补充要求**，追加到内置提示词末尾，硬性规则不可配置；每次保存写入 `prompt_revisions` 快照，支持查看历史与一键恢复。
- **对话审计**：`/admin/audit` 页面与 `GET /admin/api/audit/runs`、`GET /admin/api/audit/runs/{run_id}`。支持按员工、状态、关键词、时间范围筛选，展开可见每一轮工具名、参数、耗时、结果摘要与失败码；顶部汇总失败率、平均耗时与工具调用分布。
- **日志保留**：`db.purge_logs()` 按天清理 `agent_runs` / `agent_steps` / `tool_calls`，后台协程启动先清一次、之后每 24 小时一次；`LOG_RETENTION_DAYS=0` 可关闭。管理员可通过 `GET /admin/api/maintenance/retention` 预演、`POST /admin/api/maintenance/purge` 手动执行。
- **Cookie Secure**：登录 Cookie 按请求协议自动加 `Secure`（反向代理下读 `X-Forwarded-Proto`），可用 `COOKIE_SECURE` 强制开关。
- **演示口令告警**：启动时校验 `admin` / `employee` 是否仍在使用演示口令，在日志第一屏明确提示。
- 测试从 88 条增至 123 条，新增提示词拼接、审计/清理、执行预算与准备句过滤四组用例。

### 修复

- **提示词配置形同虚设**：`settings.system_prompt` 已写入数据库、总览页也显示其字数，但 `agent.py` 从未读取，改配置没有任何效果。现在每轮读取并注入提示词。
- **默认提示词语义冲突**：早期 `DEFAULT_SYSTEM_PROMPT` 是一句话的「整段提示词」，与 `skills.build_system_prompt()` 的内置规则重复且更弱。现改为补充要求，并在启动时自动迁移未修改过的旧值。
- **导航死链**：管理后台侧边栏早有「提示词模板」「对话审计」两项，但对应路由不存在，点击返回 404。
- **检索重复调用**：指纹去重只能拦住参数完全相同的调用，模型查不到时会不断换关键词重试（评测中同一题连查 6 次）。新增按工具名的专项预算，`search_policy` 最多 3 次，超出后强制基于已有片段下结论。
- **英文准备句泄漏**：模型偶尔在调用工具前输出 `I'll look up the policy...`，这句话会先于工具结果流到页面。新增 `PreambleFilter`，只丢弃「整句无中文且以 I'll / Let me 等开头」的句子；正常中文仍逐字流式输出，不会被缓存拖慢。

### 变更

- 错误码 1013 的上限由 4000 字改为 2000 字，与 `skills.MAX_SUPPLEMENT_CHARS` 对齐；新增 1023~1025。
- 总览页「系统提示词」改称「补充要求」，与页面语义一致。

## [1.1.0] - 2026-11-05

### 新增

- **限流与配额**（`ratelimit.py`）：滑动窗口限流、按自然日分桶的每日配额、Agent 循环并发闸门，参数全部可用环境变量覆盖。
- **日志脱敏**（`utils/masking.py`）：落库前统一抹除密钥、JWT、Bearer 令牌，遮罩手机号/邮箱/身份证，保留金额以满足审计需要；提供 `scan_for_secrets()` 供仓库扫描。
- **文本工具**（`utils/text.py`）：全角转半角（修复微信/Excel 复制的单号匹配不上）、控制字符清理、截断、单号抽取。
- **仓库规范自检**（`scripts/check_repo.py`）：零依赖检查编码、CRLF、行尾空白、语法、硬编码密钥、必备文件与环境变量覆盖度。
- **文档一致性校验**（`scripts/check_docs.py`）：校验文档中的相对链接、项目路径、API 路由、脚本命令是否真实存在，并检查 README 必备章节。
- **工程配置**：`ruff` 与覆盖率配置、`.editorconfig`、`.pre-commit-config.yaml`、`Makefile`、`requirements-dev.txt`。
- **测试从 7 条扩到 88 条**：新增工具闸门、能力包路由、限流并发、脱敏与文本处理四组用例。
- 新增[测试说明](docs/testing.md)，README 重写为完整的使用与排障手册。

### 修复

- **路由漏判**：「帮我把上个月的账对一下」这类倒装口语被错误路由到只读流程，导致模型看不到核对工具。
- **脱敏扫描误报**：`api_key = os.getenv(...)` 与 `api_key=api_key` 被误判为硬编码密钥，真问题会被噪音淹没。
- **文档校验盲区**：表格形式的 `| GET | /login |` 与 `@app.api_route(...)` 注册的路由无法被识别，导致 18 个接口只校验了 8 个。

### 变更

- 演示脚本的账号改为从环境变量读取（`DEMO_EMPLOYEE_USER` / `DEMO_EMPLOYEE_PASSWORD`），不再写死口令。
- `.env.example` 补齐限流、脱敏、存储路径等 20 个可选变量。
- CI 拆为「规范与文档」「测试（3.11/3.12/3.13）」「启动冒烟」三个 job，并产出覆盖率报告。
- 版本号升至 1.1.0。

### 安全

- 工具参数与结果摘要在落库前强制脱敏，`LOG_MASKING=0` 仅限本地调试。
- 限流在「建会话、写消息」之前拦截，被限流的请求不会留下空会话。

## [1.0.0] - 2026-10-30

### 新增

- FastAPI 员工问答与管理后台，JWT HttpOnly Cookie 鉴权。
- FAISS + BM25 混合检索，支持 md / markdown / txt / pdf 知识库管理。
- 最多 5 轮探索的 Agent 执行循环、工具结果回填与建单收尾轮。
- 13 个 Function Calling 工具、双能力包路由和服务端前置条件闸门。
- 创建争议草稿前的人工确认中断，员工确认后才能建单与提交。
- 决策日志、工具日志、校验记录、争议草稿及执行轨迹查询接口。
- 30 条评测用例、服务端闸门自检、HTTP 确认链路检查和轨迹回放脚本。
- 标准 `src/` 包结构、离线单元测试、GitHub Actions CI 与仓库协作文档。

### 修复

-  embedding 批次超供应商上限时自动二分重试。
- Windows 中文路径、CRLF 与 GBK 文档导致的索引问题。
- 管理接口异常误返回 HTML、刷新后草稿无法继续提交等问题。

### 安全

- 模型不能提交争议单，不能传入校验结论，也不能修改校验差额。
- `.env`、SQLite、索引缓存和本地运行数据默认不纳入版本控制。
