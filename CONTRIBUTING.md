# 贡献指南

## 开发环境

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash
pip install -e .
```

不要提交 `.env`、API Key、真实业务数据、`data/runtime/` 或索引缓存。示例数据必须保持虚构且可公开。

## 提交前检查

```bash
python -m compileall -q src tests examples scripts
python -m unittest discover -s tests -v
python scripts/check_cases.py
```

修改工具前置条件、金额计算或建单权限时，还必须运行：

```bash
python scripts/check_gates.py
```

涉及模型行为时，补充 `tests/data/eval_cases.json` 用例并记录真实轨迹；不要把 mock 结果写成真实模型验证结果。

## 代码约定

- Python 3.11+，业务事实与算术放在 `core/`，不得由模型自行推算。
- 包内使用相对导入；脚本和示例使用 `logistics_agent` 绝对导入。
- 工具失败返回可恢复错误码与 `next_hint`，不要把内部堆栈直接展示给员工。
- 注释解释「为什么这样设计」，避免复述代码字面行为。
- 制度语料只放 `data/knowledge/`；项目说明文档放 `docs/`，避免污染检索。

## 分支与提交

从最新主分支创建功能分支，一个 PR 只解决一个主题。提交信息使用简洁中文或英文动词开头，例如 `Fix: 阻止 pass 账单建单`。PR 中说明变更、验证命令、影响范围，并关联 Issue。

## 安全报告

发现越权建单、提示词泄露、密钥泄露或敏感数据暴露时，请私下联系维护者，不要在公开 Issue 中附攻击细节或真实数据。
