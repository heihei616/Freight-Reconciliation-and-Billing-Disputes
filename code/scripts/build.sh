#!/usr/bin/env bash
set -euo pipefail

# 在已激活的虚拟环境中执行；不访问真实 LLM。
python -m pip install -e .
python -m compileall -q src tests examples scripts
python -m unittest discover -s tests -v
python scripts/check_cases.py
printf '\nBuild checks passed.\n'
