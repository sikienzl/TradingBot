#!/usr/bin/env python3
"""Run pytest with the heavy optional deps hidden, matching the slim CI job.

The CI unit-test job installs only numpy/pandas/scikit-learn/catboost/ccxt/
python-dotenv/pydantic/fastapi/uvicorn/httpx -- no TA-Lib, torch, transformers
or onnx. Run this instead of plain pytest to reproduce CI locally:

    PYTHONPATH="$PWD:$PWD/src" .venv/bin/python \
        .github/skills/extract-module-refactor/scripts/ci_env_sim.py \
        tests/ -q --no-header -p no:cacheprovider
"""

from __future__ import annotations

import sys

BLOCKED = {"talib", "torch", "transformers", "onnx"}


class _Blocker:
    def find_spec(self, name, path=None, target=None):
        root = name.split(".")[0]
        if root in BLOCKED:
            raise ModuleNotFoundError(f"No module named {root!r}", name=root)


for _mod in list(sys.modules):
    if _mod.split(".")[0] in BLOCKED:
        del sys.modules[_mod]

sys.meta_path.insert(0, _Blocker())

import pytest

raise SystemExit(pytest.main(sys.argv[1:]))
