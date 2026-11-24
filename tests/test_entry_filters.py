"""Unit tests for the layer-1 entry gates.

Every gate receives the config explicitly, so these tests drive the real code
paths with a plain stub and never import :mod:`src.trading_bot`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from src.entry_filters import (
    entry_volatility_guard,
    passes_downtrend_reversal_filter,
    passes_entry_momentum_filter,
    passes_fallback_entry_filter,
    passes_uptrend_entry_filter,
)


@dataclass
class _Config:
    # momentum filter
    entry_momentum_filter_enabled: bool = True
    entry_min_ret_3: float = -0.01
    entry_min_ret_3_by_coin: dict[str, float] = field(default_factory=dict)
    entry_require_price_above_ema20: bool = False
    entry_sharp_pump_filter_enabled: bool = True
    entry_max_ret_1: float = 0.04
    entry_max_ret_3: float = 0.08
    # up-trend gate
    uptrend_entry_gate_enabled: bool = True
    uptrend_entry_max_rsi: float = 72.0
    uptrend_entry_max_rsi_by_coin: dict[str, float] = field(default_factory=dict)
    uptrend_entry_min_buy_proba: float = 0.24
    uptrend_entry_min_buy_proba_by_coin: dict[str, float] = field(default_factory=dict)
    uptrend_entry_max_sell_proba: float = 0.34
    uptrend_entry_max_sell_proba_by_coin: dict[str, float] = field(default_factory=dict)
    uptrend_entry_min_proba_edge: float = -0.05
    uptrend_entry_min_proba_edge_by_coin: dict[str, float] = field(default_factory=dict)
    # fallback gate
    fallback_max_rsi: float = 68.0
    fallback_max_rsi_by_coin: dict[str, float] = field(default_factory=dict)
    # volatility guard
    min_entry_atr_pct: float = 0.012
    # downtrend reversal gate
    downtrend_reversal_entry_enabled: bool = True
    downtrend_reversal_max_rsi: float = 30.0
    downtrend_reversal_max_rsi_by_coin: dict[str, float] = field(default_factory=dict)
    downtrend_reversal_min_buy_proba: float = 0.20
    downtrend_reversal_min_buy_proba_by_coin: dict[str, float] = field(default_factory=dict)
    downtrend_reversal_max_sell_proba: float = 0.35
    downtrend_reversal_max_sell_proba_by_coin: dict[str, float] = field(default_factory=dict)
    downtrend_reversal_min_proba_edge: float = 0.02
    downtrend_reversal_min_proba_edge_by_coin: dict[str, float] = field(default_factory=dict)
    downtrend_reversal_min_ret_1: float = 0.0
    downtrend_reversal_min_ret_3: float = -0.01
    downtrend_reversal_min_macd_hist: float = 0.0
    downtrend_reversal_allowed_coins: set[str] = field(
        default_factory=lambda: {"ETH"})


def _healthy_reversal_coin(**overrides: Any) -> dict[str, Any]:
    coin_data: dict[str, Any] = {
        "coin": "ETH",
        "recommendation": "HOLD (Down-Trend)",
        "rsi": 25.0,
        "tabular_buy_proba": 0.40,
        "tabular_sell_proba": 0.20,
        "ret_1": 0.005,
        "ret_3": 0.0,
        "macd_hist": 0.01,
    }
    coin_data.update(overrides)
    return coin_data


# ---------------------------------------------------------------------------
# momentum filter
# ---------------------------------------------------------------------------

def test_momentum_filter_disabled_short_circuits() -> None:
    config = _Config(entry_momentum_filter_enabled=False)
    assert passes_entry_momentum_filter(config, {"coin": "BTC", "ret_3": -9.0}) == (
        True,
        "disabled",
    )


@pytest.mark.parametrize("recommendation", ["SELL", "WEAK SELL", "HOLD", ""])
def test_momentum_filter_ignores_non_buy_signals(recommendation: str) -> None:
    config = _Config()
    assert passes_entry_momentum_filter(
        config, {"coin": "BTC", "recommendation": recommendation, "ret_3": -9.0}
    ) == (True, "not_buy_signal")


@pytest.mark.parametrize("recommendation", ["BUY", "STRONG BUY", "HOLD (Up-Trend)"])
def test_momentum_filter_accepts_healthy_buy_candidate(recommendation: str) -> None:
    config = _Config()
    assert passes_entry_momentum_filter(
        config, {"coin": "BTC", "recommendation": recommendation, "ret_3": 0.02}
    ) == (True, "ok")


def test_momentum_filter_rejects_slow_ret_3() -> None:
    config = _Config()
    passes, reason = passes_entry_momentum_filter(
        config, {"coin": "BTC", "recommendation": "BUY", "ret_3": -0.02}
    )
    assert passes is False
    assert reason == "ret_3_below_min (-0.0200 < -0.0100)"


def test_momentum_filter_uses_per_coin_ret_3_minimum() -> None:
    config = _Config(entry_min_ret_3_by_coin={"XDC": -0.05})
    assert passes_entry_momentum_filter(
        config, {"coin": "xdc", "recommendation": "BUY", "ret_3": -0.02}
    ) == (True, "ok")
    passes, reason = passes_entry_momentum_filter(
        config, {"coin": "XDC", "recommendation": "BUY", "ret_3": -0.06}
    )
    assert passes is False
    assert reason == "ret_3_below_min (-0.0600 < -0.0500)"


@pytest.mark.parametrize("ret_3", [None, float("nan")])
def test_momentum_filter_skips_missing_ret_3(ret_3: Any) -> None:
    config = _Config()
    assert passes_entry_momentum_filter(
        config, {"coin": "BTC", "recommendation": "BUY", "ret_3": ret_3}
    ) == (True, "ok")


def test_momentum_filter_requires_price_above_ema20_when_enabled() -> None:
    config = _Config(entry_require_price_above_ema20=True)
    passes, reason = passes_entry_momentum_filter(
        config, {"coin": "BTC", "recommendation": "BUY", "price": 99.0, "ema_20": 100.0}
    )
    assert passes is False
    assert reason == "price_below_ema20 (99.0000 < 100.0000)"
    assert passes_entry_momentum_filter(
        config, {"coin": "BTC", "recommendation": "BUY", "price": 101.0, "ema_20": 100.0}
    ) == (True, "ok")


@pytest.mark.parametrize("price, ema20", [(None, 100.0), (100.0, None), (None, None)])
def test_momentum_filter_skips_ema20_check_without_data(price: Any, ema20: Any) -> None:
    config = _Config(entry_require_price_above_ema20=True)
    assert passes_entry_momentum_filter(
        config, {"coin": "BTC", "recommendation": "BUY", "price": price, "ema_20": ema20}
    ) == (True, "ok")


def test_momentum_filter_blocks_sharp_pump_ret_1() -> None:
    config = _Config()
    passes, reason = passes_entry_momentum_filter(
        config, {"coin": "BTC", "recommendation": "BUY", "ret_1": 0.05, "ret_3": 0.02}
    )
    assert passes is False
    assert reason == "sharp_pump_ret_1 (0.0500 > 0.0400)"


def test_momentum_filter_blocks_sharp_pump_ret_3() -> None:
    config = _Config()
    passes, reason = passes_entry_momentum_filter(
        config, {"coin": "BTC", "recommendation": "BUY", "ret_1": 0.01, "ret_3": 0.09}
    )
    assert passes is False
    assert reason == "sharp_pump_ret_3 (0.0900 > 0.0800)"


def test_momentum_filter_sharp_pump_disabled() -> None:
    config = _Config(entry_sharp_pump_filter_enabled=False)
    assert passes_entry_momentum_filter(
        config, {"coin": "BTC", "recommendation": "BUY", "ret_1": 0.9, "ret_3": 0.9}
    ) == (True, "ok")


# ---------------------------------------------------------------------------
# up-trend gate
# ---------------------------------------------------------------------------

def test_uptrend_gate_ignores_other_recommendations() -> None:
    config = _Config()
    assert passes_uptrend_entry_filter(
        config, {"coin": "BTC", "recommendation": "BUY", "rsi": 99.0}
    ) == (True, "not_uptrend")


def test_uptrend_gate_disabled() -> None:
    config = _Config(uptrend_entry_gate_enabled=False)
    assert passes_uptrend_entry_filter(
        config, {"coin": "BTC", "recommendation": "HOLD (Up-Trend)", "rsi": 99.0}
    ) == (True, "uptrend_gate_disabled")


@pytest.mark.parametrize("rsi", [None, float("nan")])
def test_uptrend_gate_requires_rsi(rsi: Any) -> None:
    config = _Config()
    assert passes_uptrend_entry_filter(
        config, {"coin": "BTC", "recommendation": "HOLD (Up-Trend)", "rsi": rsi}
    ) == (False, "missing_rsi")


def test_uptrend_gate_rejects_overheated_rsi() -> None:
    config = _Config()
    passes, reason = passes_uptrend_entry_filter(
        config, {"coin": "BTC", "recommendation": "HOLD (Up-Trend)", "rsi": 75.5}
    )
    assert passes is False
    assert reason == "rsi_above_uptrend_max (75.50 > 72.00)"


def test_uptrend_gate_uses_per_coin_rsi_ceiling() -> None:
    config = _Config(uptrend_entry_max_rsi_by_coin={"XDC": 78.0})
    assert passes_uptrend_entry_filter(
        config,
        {
            "coin": "xdc",
            "recommendation": "HOLD (Up-Trend)",
            "rsi": 75.0,
            "tabular_buy_proba": 0.30,
            "tabular_sell_proba": 0.30,
        },
    ) == (True, "ok")


@pytest.mark.parametrize(
    "coin_data, reason",
    [
        ({"tabular_sell_proba": 0.3}, "missing_buy_proba"),
        ({"tabular_buy_proba": 0.3}, "missing_sell_proba"),
        ({"tabular_buy_proba": float("nan"), "tabular_sell_proba": 0.3}, "missing_buy_proba"),
        ({"tabular_buy_proba": 0.3, "tabular_sell_proba": float("nan")}, "missing_sell_proba"),
    ],
)
def test_uptrend_gate_requires_tabular_probabilities(coin_data: dict, reason: str) -> None:
    config = _Config()
    payload = {"coin": "BTC", "recommendation": "HOLD (Up-Trend)", "rsi": 60.0}
    payload.update(coin_data)
    assert passes_uptrend_entry_filter(config, payload) == (False, reason)


def test_uptrend_gate_rejects_weak_buy_proba() -> None:
    config = _Config()
    passes, reason = passes_uptrend_entry_filter(
        config,
        {
            "coin": "BTC",
            "recommendation": "HOLD (Up-Trend)",
            "rsi": 60.0,
            "tabular_buy_proba": 0.20,
            "tabular_sell_proba": 0.10,
        },
    )
    assert passes is False
    assert reason == "buy_proba_below_uptrend_min (0.200 < 0.240)"


def test_uptrend_gate_rejects_high_sell_proba() -> None:
    config = _Config()
    passes, reason = passes_uptrend_entry_filter(
        config,
        {
            "coin": "BTC",
            "recommendation": "HOLD (Up-Trend)",
            "rsi": 60.0,
            "tabular_buy_proba": 0.40,
            "tabular_sell_proba": 0.45,
        },
    )
    assert passes is False
    assert reason == "sell_proba_above_uptrend_max (0.450 > 0.340)"


def test_uptrend_gate_rejects_thin_proba_edge() -> None:
    config = _Config(uptrend_entry_min_proba_edge=0.05)
    passes, reason = passes_uptrend_entry_filter(
        config,
        {
            "coin": "BTC",
            "recommendation": "HOLD (Up-Trend)",
            "rsi": 60.0,
            "tabular_buy_proba": 0.30,
            "tabular_sell_proba": 0.28,
        },
    )
    assert passes is False
    assert reason == "proba_edge_below_uptrend_min (0.020 < 0.050)"


def test_uptrend_gate_per_coin_overrides_win() -> None:
    config = _Config(
        uptrend_entry_min_buy_proba_by_coin={"TRX": 0.15},
        uptrend_entry_min_proba_edge_by_coin={"TRX": -0.13},
    )
    assert passes_uptrend_entry_filter(
        config,
        {
            "coin": "trx",
            "recommendation": "HOLD (Up-Trend)",
            "rsi": 60.0,
            "tabular_buy_proba": 0.16,
            "tabular_sell_proba": 0.28,
        },
    ) == (True, "ok")


def test_uptrend_gate_accepts_balanced_candidate() -> None:
    config = _Config()
    assert passes_uptrend_entry_filter(
        config,
        {
            "coin": "BTC",
            "recommendation": "HOLD (Up-Trend)",
            "rsi": 60.0,
            "tabular_buy_proba": 0.30,
            "tabular_sell_proba": 0.28,
        },
    ) == (True, "ok")


# ---------------------------------------------------------------------------
# fallback gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rsi", [None, float("nan")])
def test_fallback_gate_requires_rsi(rsi: Any) -> None:
    config = _Config()
    assert passes_fallback_entry_filter(config, "TRX", {"rsi": rsi}) == (False, "missing_rsi")


def test_fallback_gate_rejects_hot_rsi() -> None:
    config = _Config()
    passes, reason = passes_fallback_entry_filter(config, "BTC", {"rsi": 70.0})
    assert passes is False
    assert reason == "rsi_above_fallback_max (70.00 > 68.00)"


def test_fallback_gate_uses_per_coin_ceiling() -> None:
    config = _Config(fallback_max_rsi_by_coin={"TRX": 72.0})
    assert passes_fallback_entry_filter(config, "trx", {"rsi": 70.0}) == (True, "ok")
    passes, reason = passes_fallback_entry_filter(config, "TRX", {"rsi": 73.0})
    assert passes is False
    assert reason == "rsi_above_fallback_max (73.00 > 72.00)"


def test_fallback_gate_accepts_cool_rsi() -> None:
    config = _Config()
    assert passes_fallback_entry_filter(config, "ETH", {"rsi": 55.0}) == (True, "ok")


# ---------------------------------------------------------------------------
# volatility guard
# ---------------------------------------------------------------------------

def test_volatility_guard_disabled_without_minimum() -> None:
    config = _Config(min_entry_atr_pct=0.0)
    assert entry_volatility_guard(config, {}) == (True, "disabled")


@pytest.mark.parametrize(
    "coin_data",
    [
        {},
        {"atr_pct": None},
        {"atr_pct": "abc"},
        {"atr_pct": float("nan")},
        {"atr_pct": float("inf")},
    ],
)
def test_volatility_guard_needs_a_finite_atr(coin_data: dict) -> None:
    config = _Config()
    assert entry_volatility_guard(config, coin_data) == (False, "atr_pct_unavailable")


def test_volatility_guard_rejects_quiet_market() -> None:
    config = _Config()
    passes, reason = entry_volatility_guard(config, {"atr_pct": 0.0119})
    assert passes is False
    assert reason == "atr_pct_below_minimum (0.01190 < 0.01200)"


def test_volatility_guard_accepts_enough_volatility() -> None:
    config = _Config()
    assert entry_volatility_guard(config, {"atr_pct": 0.012}) == (True, "ok")


# ---------------------------------------------------------------------------
# downtrend reversal gate
# ---------------------------------------------------------------------------

def test_reversal_gate_ignores_other_recommendations() -> None:
    config = _Config()
    assert passes_downtrend_reversal_filter(
        config, {"coin": "ETH", "recommendation": "BUY"}
    ) == (True, "not_downtrend")


def test_reversal_gate_blocks_coins_not_on_the_allow_list() -> None:
    config = _Config()
    passes, reason = passes_downtrend_reversal_filter(
        config, _healthy_reversal_coin(coin="XDC")
    )
    assert passes is False
    assert reason == "coin_not_allowed_for_reversal (XDC)"


def test_reversal_gate_empty_allow_list_means_no_coin_filter() -> None:
    config = _Config(downtrend_reversal_allowed_coins=set())
    assert passes_downtrend_reversal_filter(
        config, _healthy_reversal_coin(coin="XDC")
    ) == (True, "downtrend_reversal_ok")


def test_reversal_gate_disabled_after_allow_list() -> None:
    config = _Config(downtrend_reversal_entry_enabled=False)
    assert passes_downtrend_reversal_filter(config, _healthy_reversal_coin()) == (
        False,
        "downtrend_reversal_disabled",
    )
    # A coin that is not allowed is reported before the disabled flag.
    blocked = passes_downtrend_reversal_filter(
        config, _healthy_reversal_coin(coin="DOGE")
    )
    assert blocked == (False, "coin_not_allowed_for_reversal (DOGE)")


@pytest.mark.parametrize("rsi", [None, float("nan")])
def test_reversal_gate_requires_rsi(rsi: Any) -> None:
    config = _Config()
    assert passes_downtrend_reversal_filter(
        config, _healthy_reversal_coin(rsi=rsi)
    ) == (False, "missing_rsi")


def test_reversal_gate_rejects_rsi_that_is_not_oversold() -> None:
    config = _Config()
    passes, reason = passes_downtrend_reversal_filter(
        config, _healthy_reversal_coin(rsi=45.0)
    )
    assert passes is False
    assert reason == "rsi_above_reversal_max (45.00 > 30.00)"


def test_reversal_gate_uses_per_coin_rsi_ceiling() -> None:
    config = _Config(downtrend_reversal_max_rsi_by_coin={"ETH": 50.0})
    assert passes_downtrend_reversal_filter(
        config, _healthy_reversal_coin(rsi=45.0)
    ) == (True, "downtrend_reversal_ok")


@pytest.mark.parametrize(
    "coin_data",
    [
        {"tabular_buy_proba": None},
        {"tabular_sell_proba": None},
        {"tabular_buy_proba": None, "tabular_sell_proba": None},
    ],
)
def test_reversal_gate_requires_tabular_probabilities(coin_data: dict) -> None:
    config = _Config()
    assert passes_downtrend_reversal_filter(
        config, _healthy_reversal_coin(**coin_data)
    ) == (False, "missing_tabular_proba")


def test_reversal_gate_rejects_weak_buy_proba() -> None:
    config = _Config()
    passes, reason = passes_downtrend_reversal_filter(
        config, _healthy_reversal_coin(tabular_buy_proba=0.15)
    )
    assert passes is False
    assert reason == "buy_proba_below_min (0.150 < 0.200)"


def test_reversal_gate_rejects_dominant_sell_proba() -> None:
    config = _Config()
    passes, reason = passes_downtrend_reversal_filter(
        config, _healthy_reversal_coin(tabular_sell_proba=0.45)
    )
    assert passes is False
    assert reason == "sell_proba_above_max (0.450 > 0.350)"


def test_reversal_gate_rejects_thin_buy_sell_edge() -> None:
    config = _Config()
    passes, reason = passes_downtrend_reversal_filter(
        config,
        _healthy_reversal_coin(tabular_buy_proba=0.30, tabular_sell_proba=0.29),
    )
    assert passes is False
    assert reason == "buy_sell_edge_too_small (0.010 < 0.020)"


def test_reversal_gate_uses_per_coin_proba_overrides() -> None:
    config = _Config(
        downtrend_reversal_min_buy_proba_by_coin={"ETH": 0.10},
        downtrend_reversal_max_sell_proba_by_coin={"ETH": 0.50},
        downtrend_reversal_min_proba_edge_by_coin={"ETH": -0.20},
    )
    assert passes_downtrend_reversal_filter(
        config,
        _healthy_reversal_coin(tabular_buy_proba=0.11, tabular_sell_proba=0.29),
    ) == (True, "downtrend_reversal_ok")


@pytest.mark.parametrize("ret_1", [None, float("nan")])
def test_reversal_gate_requires_ret_1(ret_1: Any) -> None:
    config = _Config()
    assert passes_downtrend_reversal_filter(
        config, _healthy_reversal_coin(ret_1=ret_1)
    ) == (False, "missing_ret_1")


def test_reversal_gate_rejects_still_falling_ret_1() -> None:
    config = _Config()
    passes, reason = passes_downtrend_reversal_filter(
        config, _healthy_reversal_coin(ret_1=-0.02)
    )
    assert passes is False
    assert reason == "ret_1_below_reversal_min (-0.0200 < 0.0000)"


@pytest.mark.parametrize("ret_3", [None, float("nan")])
def test_reversal_gate_requires_ret_3(ret_3: Any) -> None:
    config = _Config()
    assert passes_downtrend_reversal_filter(
        config, _healthy_reversal_coin(ret_3=ret_3)
    ) == (False, "missing_ret_3")


def test_reversal_gate_rejects_still_falling_ret_3() -> None:
    config = _Config()
    passes, reason = passes_downtrend_reversal_filter(
        config, _healthy_reversal_coin(ret_3=-0.05)
    )
    assert passes is False
    assert reason == "ret_3_below_reversal_min (-0.0500 < -0.0100)"


@pytest.mark.parametrize("macd_hist", [None, float("nan")])
def test_reversal_gate_requires_macd_hist(macd_hist: Any) -> None:
    config = _Config()
    assert passes_downtrend_reversal_filter(
        config, _healthy_reversal_coin(macd_hist=macd_hist)
    ) == (False, "missing_macd_hist")


def test_reversal_gate_rejects_negative_macd_hist() -> None:
    config = _Config()
    passes, reason = passes_downtrend_reversal_filter(
        config, _healthy_reversal_coin(macd_hist=-0.02)
    )
    assert passes is False
    assert reason == "macd_hist_below_reversal_min (-0.0200 < 0.0000)"


def test_reversal_gate_accepts_oversold_reversal_candidate() -> None:
    config = _Config()
    assert passes_downtrend_reversal_filter(config, _healthy_reversal_coin()) == (
        True,
        "downtrend_reversal_ok",
    )
