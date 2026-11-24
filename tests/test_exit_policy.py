"""Unit tests for the layer-1 exit policy.

The policy receives the config, the portfolio and the bot-owned callbacks
explicitly, so these tests drive the real code paths with stubs and never
import :mod:`src.trading_bot`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.exit_policy import (
    core_exit_reason,
    downtrend_reversal_exit_reason,
    effective_stop_loss_level,
    is_downtrend_reversal_trade,
    is_rules_uptrend_trade,
    maybe_partial_take_profit,
    partial_tp_remainder_exit_reason,
    uptrend_rules_exit_reason,
)


@dataclass
class _Config:
    atr_stop_mult: float = 2.0
    trailing_stop_enabled: bool = False
    trailing_stop_atr_mult: float = 1.0
    break_even_enabled: bool = False
    break_even_trigger_pct: float = 1.0
    break_even_buffer_pct: float = 0.2
    dry_run: bool = True
    partial_take_profit_enabled: bool = False
    partial_take_profit_remainder_max_hold_seconds: int = 0
    partial_take_profit_exit_on_weak_signal: bool = False
    exit_on_downtrend: bool = False
    downtrend_reversal_fast_exit_enabled: bool = False
    downtrend_reversal_max_hold_seconds: int = 0
    downtrend_reversal_fast_exit_seconds: int = 0
    downtrend_reversal_flat_max_profit_pct: float = 0.0
    downtrend_reversal_weak_signal_exit_seconds: int = 0
    downtrend_reversal_weak_signal_max_profit_pct: float = 0.0
    uptrend_rules_fast_exit_enabled: bool = False
    uptrend_rules_fast_exit_seconds: int = 0
    uptrend_rules_fast_exit_seconds_by_coin: dict[str, int] = field(
        default_factory=dict)
    uptrend_rules_flat_max_profit_pct: float = 0.0
    uptrend_rules_flat_max_profit_pct_by_coin: dict[str, float] = field(
        default_factory=dict)
    uptrend_rules_max_hold_seconds: int = 0
    uptrend_rules_max_hold_seconds_by_coin: dict[str, int] = field(
        default_factory=dict)


class _PortfolioStub:
    def __init__(self, open_trades: dict[str, dict[str, Any]] | None = None) -> None:
        self.open_trades: dict[str, dict[str, Any]] = open_trades or {}
        self.save_state_calls = 0

    def save_state(self) -> None:
        self.save_state_calls += 1


class _ExecutorStub:
    def __init__(self, *, result: bool = True) -> None:
        self.result = result
        self.calls: list[tuple[Any, ...]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append(args)
        self.kwargs.append(kwargs)
        return self.result


def _fixed_now(moment: datetime):
    return lambda: moment


# ---------------------------------------------------------------------------
# Dynamic stop levels
# ---------------------------------------------------------------------------

def test_effective_stop_loss_returns_price_for_unpriced_trade() -> None:
    assert effective_stop_loss_level(_Config(), {"buy_price": 0.0}, 42.0, 1.0) == 42.0


def test_effective_stop_loss_records_new_peak() -> None:
    config = _Config(atr_stop_mult=1.5)
    trade_info = {"buy_price": 100.0, "peak_price": 100.0}

    stop = effective_stop_loss_level(config, trade_info, 106.0, 2.0)

    assert stop == 97.0
    assert trade_info["peak_price"] == 106.0


def test_effective_stop_loss_trailing_stop_follows_peak() -> None:
    config = _Config(
        atr_stop_mult=1.5,
        trailing_stop_enabled=True,
        trailing_stop_atr_mult=1.0,
    )
    trade_info = {"buy_price": 100.0, "peak_price": 100.0}

    stop = effective_stop_loss_level(config, trade_info, 106.0, 2.0)

    assert stop == 104.0
    assert trade_info["peak_price"] == 106.0


def test_effective_stop_loss_break_even_protection() -> None:
    config = _Config(
        atr_stop_mult=1.5,
        break_even_enabled=True,
        break_even_trigger_pct=1.0,
        break_even_buffer_pct=0.2,
    )
    trade_info = {"buy_price": 100.0, "peak_price": 101.5}

    # Base ATR stop would be 94.0, but break-even protection lifts it.
    assert effective_stop_loss_level(config, trade_info, 101.5, 4.0) == 100.2


def test_effective_stop_loss_break_even_not_triggered_below_runup() -> None:
    config = _Config(
        atr_stop_mult=1.5,
        break_even_enabled=True,
        break_even_trigger_pct=5.0,
        break_even_buffer_pct=0.2,
    )
    trade_info = {"buy_price": 100.0, "peak_price": 101.5}

    assert effective_stop_loss_level(config, trade_info, 101.5, 4.0) == 94.0


# ---------------------------------------------------------------------------
# Trade-class predicates
# ---------------------------------------------------------------------------

def test_is_rules_uptrend_trade_requires_rules_source() -> None:
    assert is_rules_uptrend_trade({
        "recommendation": "HOLD (Up-Trend)",
        "signal_source": "rules",
    }) is True
    assert is_rules_uptrend_trade({
        "recommendation": "HOLD (Up-Trend)",
        "signal_source": "catboost",
    }) is False
    assert is_rules_uptrend_trade({
        "recommendation": "BUY",
        "signal_source": "rules",
    }) is False


def test_is_downtrend_reversal_trade_only_checks_recommendation() -> None:
    assert is_downtrend_reversal_trade({"recommendation": "HOLD (Down-Trend)"}) is True
    assert is_downtrend_reversal_trade({"recommendation": "SELL"}) is False
    assert is_downtrend_reversal_trade({}) is False


# ---------------------------------------------------------------------------
# Partial take-profit
# ---------------------------------------------------------------------------

def test_partial_take_profit_disabled_is_noop() -> None:
    executor = _ExecutorStub()
    handled = maybe_partial_take_profit(
        _Config(partial_take_profit_enabled=False),
        _PortfolioStub(),
        executor,
        lambda: False,
        "SUI",
        {"buy_price": 100.0},
        partial_tp_taken=False,
        amount_coin=1.0,
        current_price=120.0,
        partial_take_profit_level=110.0,
        partial_fraction=0.5,
        atr=2.0,
    )

    assert handled is False
    assert executor.calls == []


def test_partial_take_profit_skips_when_already_taken() -> None:
    executor = _ExecutorStub()
    handled = maybe_partial_take_profit(
        _Config(partial_take_profit_enabled=True),
        _PortfolioStub({"SUI": {"buy_price": 100.0}}),
        executor,
        lambda: False,
        "SUI",
        {"buy_price": 100.0},
        partial_tp_taken=True,
        amount_coin=1.0,
        current_price=120.0,
        partial_take_profit_level=110.0,
        partial_fraction=0.5,
        atr=2.0,
    )

    assert handled is False
    assert executor.calls == []


def test_partial_take_profit_ignores_price_below_level() -> None:
    executor = _ExecutorStub()
    handled = maybe_partial_take_profit(
        _Config(partial_take_profit_enabled=True),
        _PortfolioStub({"SUI": {"buy_price": 100.0}}),
        executor,
        lambda: False,
        "SUI",
        {"buy_price": 100.0},
        partial_tp_taken=False,
        amount_coin=1.0,
        current_price=109.9,
        partial_take_profit_level=110.0,
        partial_fraction=0.5,
        atr=2.0,
    )

    assert handled is False
    assert executor.calls == []


def test_partial_take_profit_sells_fraction_and_stamps_trade() -> None:
    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    executor = _ExecutorStub()
    portfolio = _PortfolioStub({"SUI": {"buy_price": 100.0}})

    handled = maybe_partial_take_profit(
        _Config(partial_take_profit_enabled=True, dry_run=True),
        portfolio,
        executor,
        lambda: False,
        "SUI",
        {"buy_price": 100.0},
        partial_tp_taken=False,
        amount_coin=2.0,
        current_price=110.0,
        partial_take_profit_level=110.0,
        partial_fraction=0.25,
        atr=2.0,
        now=_fixed_now(now),
    )

    assert handled is True
    assert executor.calls == [("SUI", "sell", 110.0, 55.0)]
    assert executor.kwargs[0]["atr"] == 2.0
    assert executor.kwargs[0]["partial_take_profit"] is True
    assert executor.kwargs[0]["reason"] == (
        "💸 PARTIAL-TAKE-PROFIT (25% @ 110.0000)"
    )
    assert portfolio.open_trades["SUI"]["partial_tp_taken"] is True
    assert portfolio.open_trades["SUI"]["partial_tp_timestamp"] == now
    assert portfolio.save_state_calls == 1


def test_partial_take_profit_public_paper_mode_does_not_stamp_trade() -> None:
    executor = _ExecutorStub()
    portfolio = _PortfolioStub({"SUI": {"buy_price": 100.0}})

    handled = maybe_partial_take_profit(
        _Config(partial_take_profit_enabled=True, dry_run=True),
        portfolio,
        executor,
        lambda: True,
        "SUI",
        {"buy_price": 100.0},
        partial_tp_taken=False,
        amount_coin=2.0,
        current_price=110.0,
        partial_take_profit_level=110.0,
        partial_fraction=0.5,
        atr=2.0,
    )

    assert handled is True
    assert "partial_tp_taken" not in portfolio.open_trades["SUI"]
    assert portfolio.save_state_calls == 0


def test_partial_take_profit_live_run_does_not_save_state() -> None:
    portfolio = _PortfolioStub({"SUI": {"buy_price": 100.0}})

    handled = maybe_partial_take_profit(
        _Config(partial_take_profit_enabled=True, dry_run=False),
        portfolio,
        _ExecutorStub(),
        lambda: False,
        "SUI",
        {"buy_price": 100.0},
        partial_tp_taken=False,
        amount_coin=2.0,
        current_price=110.0,
        partial_take_profit_level=110.0,
        partial_fraction=0.5,
        atr=2.0,
    )

    assert handled is True
    assert portfolio.open_trades["SUI"]["partial_tp_taken"] is True
    assert portfolio.save_state_calls == 0


def test_partial_take_profit_sell_failure_falls_through(caplog) -> None:
    portfolio = _PortfolioStub({"SUI": {"buy_price": 100.0}})

    with caplog.at_level(logging.WARNING, logger="src.exit_policy"):
        handled = maybe_partial_take_profit(
            _Config(partial_take_profit_enabled=True),
            portfolio,
            _ExecutorStub(result=False),
            lambda: False,
            "SUI",
            {"buy_price": 100.0},
            partial_tp_taken=False,
            amount_coin=2.0,
            current_price=110.0,
            partial_take_profit_level=110.0,
            partial_fraction=0.5,
            atr=2.0,
        )

    assert handled is False
    assert "Partial take-profit sell failed for SUI" in caplog.text
    assert "partial_tp_taken" not in portfolio.open_trades["SUI"]


def test_partial_take_profit_ignores_full_and_zero_fractions() -> None:
    for fraction in (0.0, 1.0):
        executor = _ExecutorStub()
        handled = maybe_partial_take_profit(
            _Config(partial_take_profit_enabled=True),
            _PortfolioStub({"SUI": {"buy_price": 100.0}}),
            executor,
            lambda: False,
            "SUI",
            {"buy_price": 100.0},
            partial_tp_taken=False,
            amount_coin=2.0,
            current_price=110.0,
            partial_take_profit_level=110.0,
            partial_fraction=fraction,
            atr=2.0,
        )
        assert handled is False
        assert executor.calls == []


# ---------------------------------------------------------------------------
# Partial take-profit remainder
# ---------------------------------------------------------------------------

def test_partial_tp_remainder_timeout_reason() -> None:
    now = datetime(2026, 5, 20, 12, 10, 0, tzinfo=UTC)
    reason = partial_tp_remainder_exit_reason(
        _Config(partial_take_profit_remainder_max_hold_seconds=300),
        None,
        "SUI",
        partial_tp_taken=True,
        partial_tp_timestamp=now - timedelta(seconds=420),
        market_analysis=None,
        now=_fixed_now(now),
    )

    assert reason == "⏱️ PARTIAL-TP-REMAINDER-TIMEOUT (420s >= 300s)"


def test_partial_tp_remainder_weak_signal_reason() -> None:
    now = datetime(2026, 5, 20, 12, 10, 0, tzinfo=UTC)
    reason = partial_tp_remainder_exit_reason(
        _Config(
            partial_take_profit_remainder_max_hold_seconds=0,
            partial_take_profit_exit_on_weak_signal=True,
        ),
        None,
        "SUI",
        partial_tp_taken=True,
        partial_tp_timestamp=now - timedelta(seconds=60),
        market_analysis={"SUI": {"recommendation": "WEAK SELL"}},
        now=_fixed_now(now),
    )

    assert reason == (
        "📉 PARTIAL-TP-REMAINDER-WEAK-SIGNAL (recommendation: WEAK SELL)"
    )


def test_partial_tp_remainder_timeout_replaces_inherited_reason() -> None:
    # Matches the original ordering: this check runs first in the chain and its
    # timeout branch assigns unconditionally.
    now = datetime(2026, 5, 20, 12, 10, 0, tzinfo=UTC)
    reason = partial_tp_remainder_exit_reason(
        _Config(
            partial_take_profit_remainder_max_hold_seconds=1,
            partial_take_profit_exit_on_weak_signal=True,
        ),
        "🚨 ATR-STOP-LOSS (Stop: 90.0000)",
        "SUI",
        partial_tp_taken=True,
        partial_tp_timestamp=now - timedelta(seconds=600),
        market_analysis={"SUI": {"recommendation": "SELL"}},
        now=_fixed_now(now),
    )

    assert reason == "⏱️ PARTIAL-TP-REMAINDER-TIMEOUT (600s >= 1s)"


def test_partial_tp_remainder_requires_stamp() -> None:
    assert partial_tp_remainder_exit_reason(
        _Config(partial_take_profit_remainder_max_hold_seconds=1),
        None,
        "SUI",
        partial_tp_taken=True,
        partial_tp_timestamp=None,
        market_analysis={"SUI": {"recommendation": "SELL"}},
    ) is None


# ---------------------------------------------------------------------------
# Downtrend reversal exits
# ---------------------------------------------------------------------------

_DOWNTREND_TRADE = {"recommendation": "HOLD (Down-Trend)"}


def test_downtrend_reversal_max_hold() -> None:
    config = _Config(
        downtrend_reversal_fast_exit_enabled=True,
        downtrend_reversal_max_hold_seconds=420,
    )
    reason = downtrend_reversal_exit_reason(
        config, None, "SUI", _DOWNTREND_TRADE, 500.0, -1.5,
        {"SUI": {"recommendation": "SELL"}},
    )

    assert reason == "⏱️ DOWNTREND-REVERSAL-MAX-HOLD (500s >= 420s)"


def test_downtrend_reversal_fast_exit() -> None:
    config = _Config(
        downtrend_reversal_fast_exit_enabled=True,
        downtrend_reversal_fast_exit_seconds=120,
        downtrend_reversal_flat_max_profit_pct=0.5,
    )
    reason = downtrend_reversal_exit_reason(
        config, None, "SUI", _DOWNTREND_TRADE, 180.0, 0.2,
        {"SUI": {"recommendation": "HOLD (Down-Trend)"}},
    )

    assert reason == (
        "📉 DOWNTREND-REVERSAL-FAST-EXIT (+0.20% <= 0.50% after 180s, "
        "signal: HOLD (Down-Trend))"
    )


def test_downtrend_reversal_weak_signal_exit() -> None:
    config = _Config(
        downtrend_reversal_fast_exit_enabled=True,
        downtrend_reversal_fast_exit_seconds=0,
        downtrend_reversal_weak_signal_exit_seconds=300,
        downtrend_reversal_weak_signal_max_profit_pct=1.0,
    )
    reason = downtrend_reversal_exit_reason(
        config, None, "SUI", _DOWNTREND_TRADE, 360.0, 0.5,
        {"SUI": {"recommendation": "WEAK SELL"}},
    )

    assert reason == (
        "📉 DOWNTREND-REVERSAL-WEAK-SIGNAL-EXIT (+0.50% <= 1.00% after 360s, "
        "signal: WEAK SELL)"
    )


def test_downtrend_reversal_ignores_other_trades() -> None:
    config = _Config(
        downtrend_reversal_fast_exit_enabled=True,
        downtrend_reversal_max_hold_seconds=1,
    )
    assert downtrend_reversal_exit_reason(
        config, None, "SUI", {"recommendation": "BUY"}, 500.0, -1.0,
        {"SUI": {"recommendation": "SELL"}},
    ) is None


def test_downtrend_reversal_requires_signal_data() -> None:
    config = _Config(
        downtrend_reversal_fast_exit_enabled=True,
        downtrend_reversal_max_hold_seconds=1,
    )
    assert downtrend_reversal_exit_reason(
        config, None, "SUI", _DOWNTREND_TRADE, 500.0, -1.0, None,
    ) is None


def test_downtrend_reversal_disabled() -> None:
    config = _Config(
        downtrend_reversal_fast_exit_enabled=False,
        downtrend_reversal_max_hold_seconds=1,
    )
    assert downtrend_reversal_exit_reason(
        config, None, "SUI", _DOWNTREND_TRADE, 500.0, -1.0,
        {"SUI": {"recommendation": "SELL"}},
    ) is None


# ---------------------------------------------------------------------------
# Rules uptrend exits
# ---------------------------------------------------------------------------

_UPTREND_TRADE = {"recommendation": "HOLD (Up-Trend)", "signal_source": "rules"}


def test_uptrend_rules_max_hold() -> None:
    config = _Config(
        uptrend_rules_fast_exit_enabled=True,
        uptrend_rules_max_hold_seconds=900,
    )
    reason = uptrend_rules_exit_reason(
        config, None, "SUI", _UPTREND_TRADE, 1000.0, 3.0,
        {"SUI": {"recommendation": "HOLD (Up-Trend)"}},
    )

    assert reason == "⏱️ UPTREND-RULES-MAX-HOLD (1000s >= 900s)"


def test_uptrend_rules_fast_exit() -> None:
    config = _Config(
        uptrend_rules_fast_exit_enabled=True,
        uptrend_rules_fast_exit_seconds=120,
        uptrend_rules_flat_max_profit_pct=0.5,
    )
    reason = uptrend_rules_exit_reason(
        config, None, "SUI", _UPTREND_TRADE, 200.0, 0.1,
        {"SUI": {"recommendation": "HOLD (Up-Trend)"}},
    )

    assert reason == (
        "📉 UPTREND-RULES-FAST-EXIT (+0.10% <= 0.50% after 200s, "
        "signal: HOLD (Up-Trend))"
    )


def test_uptrend_rules_per_coin_overrides() -> None:
    config = _Config(
        uptrend_rules_fast_exit_enabled=True,
        uptrend_rules_fast_exit_seconds=0,
        uptrend_rules_fast_exit_seconds_by_coin={"SUI": 60},
        uptrend_rules_flat_max_profit_pct=0.0,
        uptrend_rules_flat_max_profit_pct_by_coin={"SUI": 2.0},
        uptrend_rules_max_hold_seconds=0,
        uptrend_rules_max_hold_seconds_by_coin={"SUI": 600},
    )
    reason = uptrend_rules_exit_reason(
        config, None, "SUI", _UPTREND_TRADE, 120.0, 1.5,
        {"SUI": {"recommendation": "WEAK SELL"}},
    )

    assert reason == (
        "📉 UPTREND-RULES-FAST-EXIT (+1.50% <= 2.00% after 120s, "
        "signal: WEAK SELL)"
    )


def test_uptrend_rules_keeps_strong_signals() -> None:
    config = _Config(
        uptrend_rules_fast_exit_enabled=True,
        uptrend_rules_fast_exit_seconds=60,
        uptrend_rules_flat_max_profit_pct=5.0,
    )
    assert uptrend_rules_exit_reason(
        config, None, "SUI", _UPTREND_TRADE, 300.0, 0.0,
        {"SUI": {"recommendation": "STRONG BUY"}},
    ) is None


def test_uptrend_rules_ignores_model_trades() -> None:
    config = _Config(
        uptrend_rules_fast_exit_enabled=True,
        uptrend_rules_max_hold_seconds=1,
    )
    assert uptrend_rules_exit_reason(
        config, None, "SUI",
        {"recommendation": "HOLD (Up-Trend)", "signal_source": "catboost"},
        300.0, 0.0, {"SUI": {"recommendation": "SELL"}},
    ) is None


def test_uptrend_rules_keeps_existing_reason() -> None:
    config = _Config(
        uptrend_rules_fast_exit_enabled=True,
        uptrend_rules_max_hold_seconds=1,
    )
    assert uptrend_rules_exit_reason(
        config, "🚨 ATR-STOP-LOSS (Stop: 90.0000)", "SUI", _UPTREND_TRADE,
        300.0, 0.0, {"SUI": {"recommendation": "SELL"}},
    ) == "🚨 ATR-STOP-LOSS (Stop: 90.0000)"


# ---------------------------------------------------------------------------
# Core stop / take-profit / hold / signal checks
# ---------------------------------------------------------------------------

def test_core_exit_stop_loss_wins() -> None:
    reason = core_exit_reason(
        _Config(), None, "SUI", current_price=89.0, stop_loss_level=90.0,
        take_profit_level=110.0, max_hold_seconds=0, hold_seconds=10.0,
        pnl_pct=-11.0, market_analysis={"SUI": {"recommendation": "SELL"}},
    )

    assert reason == "🚨 ATR-STOP-LOSS (Stop: 90.0000)"


def test_core_exit_take_profit_only_without_partial_tp() -> None:
    reason = core_exit_reason(
        _Config(partial_take_profit_enabled=False), None, "SUI",
        current_price=110.0, stop_loss_level=90.0, take_profit_level=110.0,
        max_hold_seconds=0, hold_seconds=10.0, pnl_pct=10.0, market_analysis=None,
    )
    assert reason == "🎉 ATR-TAKE-PROFIT (TP: 110.0000)"

    reason = core_exit_reason(
        _Config(partial_take_profit_enabled=True), None, "SUI",
        current_price=110.0, stop_loss_level=90.0, take_profit_level=110.0,
        max_hold_seconds=0, hold_seconds=10.0, pnl_pct=10.0, market_analysis=None,
    )
    assert reason is None


def test_core_exit_max_hold_time() -> None:
    reason = core_exit_reason(
        _Config(), None, "SUI", current_price=100.0, stop_loss_level=90.0,
        take_profit_level=110.0, max_hold_seconds=420, hold_seconds=500.0,
        pnl_pct=0.0, market_analysis=None,
    )

    assert reason == "⏰ MAX-HOLD-TIME reached (500s >= 420s)"


def test_core_exit_downtrend_signal() -> None:
    reason = core_exit_reason(
        _Config(exit_on_downtrend=True), None, "SUI", current_price=100.0,
        stop_loss_level=90.0, take_profit_level=110.0, max_hold_seconds=0,
        hold_seconds=10.0, pnl_pct=0.5,
        market_analysis={"SUI": {"recommendation": "HOLD (Down-Trend)"}},
    )

    assert reason == "📉 Down-trend signal detected (recommendation: HOLD (Down-Trend))"


def test_core_exit_negative_pnl_weak_signal() -> None:
    reason = core_exit_reason(
        _Config(), None, "SUI", current_price=99.0, stop_loss_level=90.0,
        take_profit_level=110.0, max_hold_seconds=0, hold_seconds=10.0,
        pnl_pct=-1.0, market_analysis={"SUI": {"recommendation": "WEAK SELL"}},
    )

    assert reason == "🔻 PnL-NEG-WEAK-SIGNAL (-1.00% | signal: WEAK SELL)"


def test_core_exit_keeps_positive_pnl_with_neutral_signal() -> None:
    assert core_exit_reason(
        _Config(), None, "SUI", current_price=101.0, stop_loss_level=90.0,
        take_profit_level=110.0, max_hold_seconds=0, hold_seconds=10.0,
        pnl_pct=1.0, market_analysis={"SUI": {"recommendation": "WEAK SELL"}},
    ) is None


def test_core_exit_preserves_inherited_reason() -> None:
    reason = core_exit_reason(
        _Config(), "📉 UPTREND-RULES-FAST-EXIT", "SUI", current_price=101.0,
        stop_loss_level=90.0, take_profit_level=110.0, max_hold_seconds=0,
        hold_seconds=10.0, pnl_pct=1.0, market_analysis=None,
    )

    assert reason == "📉 UPTREND-RULES-FAST-EXIT"


@pytest.mark.parametrize("signal", ["BUY", "STRONG BUY", "HOLD (Up-Trend)"])
def test_core_exit_ignores_favourable_signals_for_losing_trades(signal: str) -> None:
    assert core_exit_reason(
        _Config(), None, "SUI", current_price=99.0, stop_loss_level=90.0,
        take_profit_level=110.0, max_hold_seconds=0, hold_seconds=10.0,
        pnl_pct=-1.0, market_analysis={"SUI": {"recommendation": signal}},
    ) is None
