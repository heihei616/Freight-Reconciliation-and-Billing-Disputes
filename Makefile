# ============================================================
# 常用任务入口。Windows 没装 make 时，直接用下面注释里的等价命令。
# ============================================================
.PHONY: help install install-dev run test coverage lint fmt check docs demo smoke-confirm clean

PY ?= python
export PYTHONPATH := src

help:  ## 显示所有可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## 安装运行依赖与项目本体
	$(PY) -m pip install -r requirements.txt
	$(PY) -m pip install -e . --no-build-isolation

install-dev:  ## 安装开发依赖（ruff / coverage / pre-commit）
	$(PY) -m pip install -r requirements-dev.txt
	pre-commit install

run:  ## 启动服务
	$(PY) -m logistics_agent.main
# 等价：set PYTHONPATH=src && python -m logistics_agent.main

test:  ## 跑离线单元测试（不访问网络与模型）
	$(PY) -m unittest discover -s tests -v
# 等价：python -m unittest discover -s tests -v

coverage:  ## 跑测试并输出覆盖率报告
	$(PY) -m coverage run -m unittest discover -s tests
	$(PY) -m coverage report -m
	@echo "HTML 报告：htmlcov/index.html"

lint:  ## 静态检查（未安装 ruff 时自动跳过）
	-$(PY) -m ruff check src tests examples scripts
	-$(PY) -m ruff format --check src tests examples scripts

fmt:  ## 自动格式化
	-$(PY) -m ruff format src tests examples scripts
	-$(PY) -m ruff check --fix src tests examples scripts

check:  ## 一次性跑完所有检查：规范 → 文档 → 用例 → 测试
	$(PY) scripts/check_repo.py
	$(PY) scripts/check_docs.py
	$(PY) scripts/check_cases.py
	$(PY) -m unittest discover -s tests
	$(PY) -m compileall -q src tests examples scripts
	@echo "全部检查通过"

docs:  ## 只校验文档与代码的一致性
	$(PY) scripts/check_docs.py

demo:  ## 离线业务示例（不调用模型）
	$(PY) examples/demo.py

smoke-confirm:  ## 服务已启动时验证确认接口（不调用模型）
	$(PY) scripts/check_confirm_api.py
# 等价：PYTHONPATH=src python scripts/check_confirm_api.py

clean:  ## 清理缓存与本地运行数据（不会碰 data/knowledge）
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .ruff_cache .mypy_cache htmlcov .coverage build dist *.egg-info
	@echo "已清理缓存；如需重置数据库，请删除 data/runtime/chat.db"
