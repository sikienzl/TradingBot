"""Minimal backtester to evaluate strategies against synthetic price series."""
from typing import Any

import numpy as np


def run_backtest(signals: dict[str, Any], prices: np.ndarray) -> dict[str, Any]:
    """Run a toy backtest: apply a single final signal to compute return.

    signals: output from strategy (dict containing 'signal')
    prices: numpy array of close prices (latest last)
    Returns basic metrics: entry_price, exit_price, return_pct
    """
    if prices.size < 2:
        return {"return_pct": 0.0}

    entry = prices[-2]
    exit = prices[-1]
    sig = signals.get("signal", "hold")
    if sig == "buy":
        ret = (exit - entry) / entry
    elif sig == "sell":
        ret = (entry - exit) / entry
    else:
        ret = 0.0
    return {"entry": float(entry), "exit": float(exit), "return_pct": float(ret)}
