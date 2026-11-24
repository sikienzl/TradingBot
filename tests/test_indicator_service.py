"""Unit tests for the pure indicator helpers in ``src.indicator_service``."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from src.indicator_service import (
    calculate_atr,
    calculate_macd,
    calculate_rsi,
    calculate_sma,
)


def _series(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype="float64")


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

def test_rsi_returns_nan_for_short_input() -> None:
    assert math.isnan(calculate_rsi(_series([1.0, 2.0, 3.0]), period=14))


def test_rsi_is_50_when_gains_equal_losses() -> None:
    # Alternating +1 / -1 steps: with period=2 every window holds exactly one
    # gain and one loss, so avg gain == avg loss -> RSI 50.
    data = _series([100.0 + (1 if i % 2 else 0) for i in range(12)])
    rsi = calculate_rsi(data, period=2)
    assert rsi == pytest.approx(50.0)


def test_rsi_is_100_for_pure_uptrend() -> None:
    data = _series([float(i) for i in range(1, 31)])
    rsi = calculate_rsi(data, period=14)
    assert rsi == pytest.approx(100.0)


def test_rsi_is_0_for_pure_downtrend() -> None:
    data = _series([float(30 - i) for i in range(30)])
    rsi = calculate_rsi(data, period=14)
    assert rsi == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# SMA
# ---------------------------------------------------------------------------

def test_sma_returns_nan_for_short_input() -> None:
    assert math.isnan(calculate_sma(_series([1.0, 2.0]), period=5))


def test_sma_averages_last_window_only() -> None:
    data = _series([1000.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    assert calculate_sma(data, period=5) == pytest.approx(3.0)


def test_sma_of_flat_series_is_the_level() -> None:
    data = _series([7.0] * 10)
    assert calculate_sma(data, period=10) == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

def test_macd_returns_nan_for_short_input() -> None:
    assert math.isnan(calculate_macd(_series([1.0] * 25)))


def test_macd_is_zero_for_flat_series() -> None:
    data = _series([50.0] * 60)
    assert calculate_macd(data) == pytest.approx(0.0)


def test_macd_is_positive_in_uptrend_and_negative_in_downtrend() -> None:
    up = _series([float(i) for i in range(1, 101)])
    down = _series([float(100 - i) for i in range(100)])
    assert calculate_macd(up) > 0.0
    assert calculate_macd(down) < 0.0


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def _ohlcv(highs: list[float], lows: list[float], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


def test_atr_returns_nan_for_short_input() -> None:
    df = _ohlcv([2.0] * 14, [1.0] * 14, [1.5] * 14)
    assert math.isnan(calculate_atr(df, period=14))


def test_atr_of_constant_range_bars_equals_the_range() -> None:
    # Every bar spans exactly 2.0 and closes mid-bar, so TR == 2.0 everywhere.
    bars = 30
    df = _ohlcv([2.0] * bars, [0.0] * bars, [1.0] * bars)
    assert calculate_atr(df, period=14) == pytest.approx(2.0)


def test_atr_accounts_for_gaps_between_bars() -> None:
    closes = [10.0] * 15 + [20.0]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    df = _ohlcv(highs, lows, closes)
    atr = calculate_atr(df, period=14)
    # The gap bar's true range (high - prev close = 10.5) dominates the mean.
    assert atr > 1.0
