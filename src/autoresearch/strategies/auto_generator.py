"""Automated signal generator for autoresearch.

Produces simple signals based on moving-average crossover for demo/testing.
"""
from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass
class AutoGenerator:
    params: dict[str, Any]

    def generate_signals(self, market_data):
        """Generate signals using short/long SMA crossover.

        market_data: dict-like with 'close' sequence or numpy array.
        Returns: dict with 'signal' per latest index: 'buy'/'sell'/'hold'
        """
        closes = None
        if market_data is None:
            return {"signal": "hold", "reason": "no data"}

        # accept pandas Series/ND array or dict
        if hasattr(market_data, "get") and isinstance(market_data, dict):
            closes = market_data.get("close")
        else:
            closes = getattr(market_data, "close", None) or market_data

        arr = np.asarray(closes, dtype=float)
        if arr.size < 10:
            return {"signal": "hold", "reason": "insufficient data"}

        short = int(self.params.get("short", 5))
        long = int(self.params.get("long", 20))
        if arr.size < long:
            return {"signal": "hold", "reason": "insufficient long window"}

        sma_short = arr[-short:].mean()
        sma_long = arr[-long:].mean()

        if sma_short > sma_long:
            return {"signal": "buy", "reason": "sma crossover"}
        if sma_short < sma_long:
            return {"signal": "sell", "reason": "sma crossunder"}
        return {"signal": "hold", "reason": "neutral"}


def create(params: dict[str, Any]) -> AutoGenerator:
    return AutoGenerator(params=params)
