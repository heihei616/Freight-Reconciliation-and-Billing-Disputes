# 贡献指南

## 开发环境

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash；macOS/Linux 用 bin/activate

pip install -r requirements-dev.txt
pip install -e .
pre-commit install              # 可选，装完提交时自动检查
```

不要提交 `.env`、API Key、真实业务数据、`data/runtime/` 或索引缓存。示例数据必须保持虚构且可公开。

## 提交前检查

最小集合（离线，约 1 秒）：

```bash
python -m unittest discover -s tests -v
python scripts/check_repo.py
python scripts/check_docs.py
```

装了 make 的话：

```bash
make check
```

改动风险不同，需要追加的验证也不同：

| 你改了什么 | 必须额外跑 |
|---|---|
| 工具前置条件、金额计算、建单权限 | `python scripts/check_gates.py` |
| 系统提示词或能力包 | `python examples/demo_agent.py`（真实模型） |
| 新增接口 | `python scripts/check_docs.py`（会核对路由与文档） |
| 新增环境变量 | `python scripts/check_repo.py`（会核对 `.env.example`） |
| 新增或删除文档、脚本 | `python scripts/check_docs.py` |

涉及模型行为时，补充 `tests/data/eval_cases.json` 用例并记录真实轨迹；**不要把 mock 结果写成真实模型验证结果**。

## 代码约定

- Python 3.11+，行宽 100，缩进 4 空格，换行 LF（`.editorconfig` 已约定）。
- 业务事实与算术放在 `core/`，不得由模型自行推算。
- 包内使用相对导入；脚本和示例使用 `logistics_agent` 绝对导入。
- 工具失败返回可恢复错误码与 `next_hint`，不要把内部堆栈直接展示给员工。
- 注释解释「为什么这样设计」，避免复述代码字面行为。
- 制度语料只放 `data/knowledge/`；项目说明文档放 `docs/`，避免污染检索。
- 新增日志字段或落库内容时，确认已过 `utils/masking.mask_for_log()`。

## 加一个新的工具

1. 在 `tools.py` 的 `SCHEMAS` 里写 Schema，描述要说明「什么时候该用」而不只是「它做什么」——模型主要靠这段描述决策。
2. 写 handler 并注册到 `_HANDLERS`，参数校验用 `_as_str` / `_as_number` 等辅助函数，不要自己拼错误文本。
3. 如果这个工具有前置条件，在 handler 里查 `db` 的调用轨迹，**不要信任模型的自述**。
4. 把工具名加进对应 skill 的 `tools` 白名单；只读场景不要给写工具。
5. 补 `tests/test_tools.py`：成功路径、参数非法、前置条件不满足三类。
6. 如果它是新接口，同步更新 [API 说明](docs/api.md)。

## 改系统提示词

提示词是最后一道防线，不是唯一防线。改之前先确认：

- 这条规则能不能用代码强制？能就不要只写在提示里。
- 改动会不会让模型开始复述自己的执行过程？`ThinkingFilter` 与提示词的「只写要说给用户的话」共同负责这一点。
- 语气类改动（更自然、更简短）请用 `examples/demo_agent.py` 实跑三条轨迹确认没有退化。

## 分支与提交

从最新主分支创建功能分支，一个 PR 只解决一个主题。提交信息使用简洁中文或英文动词开头，例如 `Fix: 阻止 pass 账单建单`、`Feat: 新增限流与配额`。PR 中说明变更、验证命令、影响范围，并关联 Issue。

## 文档约定

- README 面向第一次接触项目的人，包含能跑通的完整步骤。
- `docs/` 下每篇文档只讲一件事，标题层级不超过三级。
- 文档里出现的路径、接口、命令必须真实存在——`scripts/check_docs.py` 会校验，CI 会拦。

## 安全报告

发现越权建单、提示词泄露、密钥泄露或敏感数据暴露时，请私下联系维护者，不要在公开 Issue 中附攻击细节或真实数据。
