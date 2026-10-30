# 物流费对账与计费争议智能体

基于 FastAPI、SSE 和 OpenAI 兼容 Function Calling 的物流对账助手。它不是固定流程问答：模型负责决策，服务端负责事实、计算和权限闸门；创建争议草稿前必须暂停并等待员工确认。

## 核心能力

- **制度问答**：FAISS + BM25 混合检索，答案带制度片段引用；未命中明确回答「制度中未找到」。
- **运费核对**：价目版本、体积重、阶梯价、燃油附加费和最低收费均由确定性代码计算。
- **争议处理**：账单读取 → 应收计算 → 服务端校验 → 草稿 → 人工确认提交，禁止跳步或篡改金额。
- **可观测**：每次运行、每轮决策和每次工具调用都写入 SQLite，可查询执行轨迹。
- **知识库管理**：管理员可上传、删除和重建 md / txt / pdf 文档索引。

## 快速开始

要求 Python 3.11+。

```bash
python -m venv .venv
# Windows Git Bash
source .venv/Scripts/activate
# macOS / Linux: source .venv/bin/activate

pip install -e .
cp .env.example .env        # Windows CMD 可用 copy .env.example .env
logistics-agent
```

打开 <http://127.0.0.1:8000>。未配置模型密钥时使用 mock 模式；真实模型请在 `.env` 中填写 OpenAI 兼容接口。

本地演示账号：`employee / emp123`、`admin / admin123`。**仅用于本地开发，部署前必须更换。**

也可以不使用命令行入口：

```bash
python -m logistics_agent.main
# 或
uvicorn logistics_agent.app:app --host 127.0.0.1 --port 8000
```

若当前环境无法安装（离线、无 setuptools），可把源码目录直接加入模块路径运行：

```bash
PYTHONPATH=src python -m logistics_agent.main
```

## 运行示例与测试

```bash
# 完全离线：查看运费推导与三处账单异常
python examples/demo.py

# 离线单元测试
python -m unittest discover -s tests -v

# 校验 30 条评测用例的数据与工具名
python scripts/check_cases.py

# 需要真实模型：回放三条 Agent 轨迹
python examples/demo_agent.py

# 不经过模型，直接验证服务端权限闸门
python scripts/check_gates.py
```

## 目录说明

```text
├── src/logistics_agent/    # 应用、Agent 循环、工具、鉴权与数据层
│   ├── core/               # 确定性业务规则与运费计算
│   ├── utils/              # 路径与非机密配置读取
│   ├── static/             # 员工端静态资源
│   └── templates/          # 管理端 Jinja2 模板
├── data/knowledge/         # 唯一参与 RAG 索引的制度语料
├── data/runtime/           # 本地 SQLite，已忽略，不上传
├── docs/                   # 设计、API、上线清单与研究资料，不参与检索
├── tests/                  # 离线单元测试与评测数据
├── examples/               # 离线示例与真实模型轨迹回放
├── scripts/                # 校验、构建和部署脚本
├── config/default.yaml     # 非机密服务配置
└── .github/                # CI 与协作模板
```

## 文档

- [架构与设计](docs/design.md)
- [API 说明](docs/api.md)
- [Agent 实测轨迹](docs/agent_loop_trajectories.md)
- [上线前检查清单](docs/prelaunch_checklist.md)
- [贡献指南](CONTRIBUTING.md)
- [更新记录](CHANGELOG.md)

## 安全与部署边界

当前定位是单实例内部系统。人工确认等待状态保存在进程内存中，多 worker 部署前必须改为共享存储。生产上线前请逐项完成[上线前检查清单](docs/prelaunch_checklist.md)，尤其是限流配额、日志保留、密钥更换和备份恢复。
