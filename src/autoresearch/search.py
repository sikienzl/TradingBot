"""Simple grid search over strategy parameters."""
import itertools
from collections.abc import Iterable

import numpy as np

from .backtester import run_backtest
from .strategies.auto_generator import create as make_generator


def grid_search(param_grid: dict[str, Iterable], price_series: np.ndarray):
    keys = list(param_grid.keys())
    values = [list(param_grid[k]) for k in keys]
    best = None
    results = []
    for combo in itertools.product(*values):
        params = dict(zip(keys, combo))
        gen = make_generator(params)
        signals = gen.generate_signals({"close": price_series})
        metrics = run_backtest(signals, np.asarray(price_series))
        res = {"params": params, "signals": signals, "metrics": metrics}
        results.append(res)
        if best is None or res["metrics"]["return_pct"] > best["metrics"]["return_pct"]:
            best = res
    return {"best": best, "results": results}
