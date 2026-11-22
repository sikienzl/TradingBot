"""Deterministic simulated OHLCV market data for simulation mode.

Extracted from ``src.trading_bot`` so the market simulator can be tested and
reused without importing the full trading bot.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd


class SimulationEngine:
    """Seeded, regime-shaped OHLCV generator used when SIMULATE_DATA is enabled.

    The bot's current iteration counter is provided via ``iteration_provider``
    so the generated series stay in sync with the bot loop.
    """

    def __init__(self, config, iteration_provider: Callable[[], int] | None = None):
        self.config = config
        self._iteration_provider = iteration_provider or (lambda: 0)
        self._cache_iteration: int | None = None
        self._ohlcv_cache: dict[tuple[int, str, str, int], pd.DataFrame] = {}

    @property
    def iteration(self) -> int:
        return int(self._iteration_provider())

    def series_seed(self, symbol: str, timeframe: str) -> int:
        token = f"{symbol}|{timeframe}|{self.config.simulation_seed}"
        checksum = sum((index + 1) * ord(char)
                       for index, char in enumerate(token))
        return checksum % (2 ** 32)

    def time_delta(self, timeframe: str) -> timedelta:
        unit = timeframe[-1].lower()
        value = int(timeframe[:-1]) if timeframe[:-1].isdigit() else 1
        if unit == 'm':
            return timedelta(minutes=value)
        if unit == 'h':
            return timedelta(hours=value)
        if unit == 'd':
            return timedelta(days=value)
        return timedelta(hours=1)

    def base_price(self, coin: str) -> float:
        defaults = {
            'BTC': 74000.0,
            'ETH': 2500.0,
            'SOL': 160.0,
            'XRP': 0.55,
            'ADA': 0.45,
            'DOGE': 0.16,
            'TRX': 0.12,
            'CHZ': 0.11,
            'VVV': 5.5,
        }
        if coin in defaults:
            return defaults[coin]
        checksum = sum(ord(char) for char in coin)
        return 10.0 + (checksum % 190)

    def base_volume(self, coin: str) -> float:
        defaults = {
            'BTC': 2_000_000.0,
            'ETH': 1_400_000.0,
            'SOL': 900_000.0,
            'XRP': 700_000.0,
            'ADA': 550_000.0,
            'DOGE': 600_000.0,
            'TRX': 450_000.0,
            'CHZ': 280_000.0,
            'VVV': 180_000.0,
        }
        if coin in defaults:
            return defaults[coin]
        checksum = sum((index + 3) * ord(char)
                       for index, char in enumerate(coin))
        return float(120_000 + (checksum % 500_000))

    def regime_for_coin(self, coin: str) -> str:
        regime = (self.config.simulation_regime or 'neutral').lower()
        if regime != 'mixed':
            return regime

        regimes = ['uptrend', 'sideways', 'downtrend', 'recovery']
        checksum = sum((index + 5) * ord(char)
                       for index, char in enumerate(coin))
        return regimes[checksum % len(regimes)]

    def regime_shape(self, regime: str, bucket_index: int, phase: float) -> tuple[float, float, float]:
        relative = bucket_index - self.iteration

        if regime == 'uptrend':
            price_bias = 0.16 * math.tanh((relative + 36.0) / 18.0)
            volume_multiplier = 1.00 + 0.12 * \
                math.sin(bucket_index / 4.0 + phase)
            wave_scale = 0.85
        elif regime == 'downtrend':
            price_bias = -0.16 * math.tanh((relative + 36.0) / 18.0)
            volume_multiplier = 1.05 + 0.16 * \
                abs(math.sin(bucket_index / 5.0 + phase))
            wave_scale = 0.95
        elif regime == 'sideways':
            price_bias = 0.02 * math.sin(relative / 6.0 + phase)
            volume_multiplier = 0.78 + 0.06 * \
                math.cos(bucket_index / 7.0 + phase)
            wave_scale = 0.35
        elif regime == 'crash':
            crash_step = 1.0 / (1.0 + math.exp(-(relative + 6.0) / 1.2))
            price_bias = -0.30 * crash_step + 0.01 * \
                math.sin(bucket_index / 4.0 + phase)
            volume_multiplier = 1.20 + 0.70 * crash_step
            wave_scale = 1.35
        elif regime == 'recovery':
            dip = -0.18 * math.exp(-((relative + 18.0) ** 2) / 90.0)
            bounce = 0.16 / (1.0 + math.exp(-(relative + 6.0) / 1.6))
            price_bias = dip + bounce + 0.02 * \
                math.sin(bucket_index / 6.0 + phase)
            volume_multiplier = 1.00 + 0.22 * \
                abs(math.sin(bucket_index / 4.0 + phase))
            wave_scale = 0.90
        else:
            price_bias = 0.03 * math.sin(relative / 8.0 + phase)
            volume_multiplier = 0.95 + 0.10 * \
                math.sin(bucket_index / 3.0 + phase * 0.5)
            wave_scale = 0.70

        return price_bias, max(0.25, volume_multiplier), wave_scale

    def build_ohlcv(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> pd.DataFrame:
        iteration = self.iteration
        if self._cache_iteration != iteration:
            self._ohlcv_cache = {}
            self._cache_iteration = iteration

        cache_key = (iteration, symbol, timeframe, limit)
        cached = self._ohlcv_cache.get(cache_key)
        if cached is not None:
            return cached.copy()

        coin = symbol.split('/')[0]
        regime = self.regime_for_coin(coin)
        rng = np.random.default_rng(self.series_seed(symbol, timeframe))
        interval = self.time_delta(timeframe)
        end_time = datetime.now(UTC).replace(second=0, microsecond=0)
        base_price = self.base_price(coin)
        base_volume = self.base_volume(coin)

        phase = rng.uniform(0.0, 2.0 * math.pi)
        volatility = rng.uniform(0.0025, 0.0120)
        trend_strength = rng.uniform(0.01, 0.04)

        def close_for_bucket(bucket_index: int) -> float:
            regime_bias, _, wave_scale = self.regime_shape(
                regime, bucket_index, phase)
            macro = 0.10 * math.sin(bucket_index / 18.0 + phase)
            micro = 0.03 * math.cos(bucket_index / 5.0 + phase * 0.7)
            local_trend = trend_strength * \
                math.sin(bucket_index / 9.0 + phase * 0.3)
            regime_cycle = 0.02 * \
                math.sin((iteration + bucket_index) / 7.0 + phase * 1.3)
            price_factor = max(0.25, 1.0 + regime_bias + wave_scale * (
                macro + micro + local_trend + regime_cycle))
            return base_price * price_factor

        rows = []
        for idx in range(limit):
            timestamp = end_time - interval * (limit - 1 - idx)
            bucket_index = iteration - (limit - 1 - idx)
            close_price = close_for_bucket(bucket_index)
            open_price = close_for_bucket(bucket_index - 1)
            _, volume_multiplier, _ = self.regime_shape(
                regime, bucket_index, phase)
            wick_up = abs(math.sin(bucket_index * 0.91 + phase)
                          ) * (0.002 + volatility * 0.5)
            wick_down = abs(math.cos(bucket_index * 1.07 + phase)
                            ) * (0.002 + volatility * 0.5)
            high_price = max(open_price, close_price) * (1.0 + wick_up)
            low_price = min(open_price, close_price) * \
                max(0.2, 1.0 - wick_down)
            price_move = abs(close_price - open_price) / max(open_price, 1e-9)
            volume = max(base_volume * 0.08,
                         base_volume * volume_multiplier * (1.0 + price_move * 8.0))

            rows.append([
                timestamp,
                float(open_price),
                float(high_price),
                float(low_price),
                float(close_price),
                float(volume),
            ])

        df = pd.DataFrame(
            rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        self._ohlcv_cache[cache_key] = df
        return df.copy()
