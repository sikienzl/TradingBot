"""Unit tests for the layer-1 open-position sweep.

The sweep receives the config and the portfolio through :class:`OpenTradeContext`
and the bot-owned helpers through :class:`OpenTradeHooks`, so these tests drive
the real code with stubs and never import :mod:`src.trading_bot`.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pytest

from src.open_trade_manager import (
    OpenTradeContext,
    OpenTradeHooks,
    manage_open_trades,
)


@dataclass
class _Config:
    dry_run: bool = True
    max_hold_seconds: int = 0
    coin_max_hold_seconds: dict[str, int] = field(default_factory=dict)
    atr_stop_mult: float = 2.0
    trailing_stop_enabled: bool = False
    trailing_stop_atr_mult: float = 1.0
    break_even_enabled: bool = False
    break_even_trigger_pct: float = 1.0
    break_even_buffer_pct: float = 0.2
    atr_tp_mult: float = 3.0
    partial_take_profit_enabled: bool = False
    partial_take_profit_atr_mult: float = 1.5
    partial_take_profit_fraction: float = 0.5
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
        self.removed: list[str] = []
        self.save_state_calls = 0

    def remove_trade(self, coin: str) -> None:
        self.removed.append(coin)
        self.open_trades.pop(coin, None)

    def save_state(self) -> None:
        self.save_state_calls += 1


class _ExecutorStub:
    def __init__(self, *, result: bool = True, side_effect: Any = None) -> None:
        self.result = result
        self.side_effect = side_effect
        self.calls: list[tuple[Any, ...]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append(args)
        self.kwargs.append(kwargs)
        if self.side_effect is not None:
            self.side_effect(*args, **kwargs)
        return self.result


class _AtrStub:
    def __init__(self, value: float = 2.0) -> None:
        self.value = value
        self.calls: list[tuple[Any, ...]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> float:
        self.calls.append(args)
        self.kwargs.append(kwargs)
        return self.value


def _trade(
    *,
    buy_price: float = 100.0,
    amount_coin: float = 1.0,
    age_seconds: float = 10.0,
    **extra: Any,
) -> dict[str, Any]:
    trade: dict[str, Any] = {
        "buy_price": buy_price,
        "amount_coin": amount_coin,
        "timestamp": datetime.now() - timedelta(seconds=age_seconds),
    }
    trade.update(extra)
    return trade


def _sweep(
    portfolio: _PortfolioStub,
    market_data: dict[str, dict],
    *,
    config: _Config | None = None,
    atr: float = 2.0,
    sell_result: bool = True,
    market_analysis: dict | None = None,
    public_paper_mode: bool = False,
) -> tuple[_ExecutorStub, _AtrStub]:
    config = config or _Config()
    executor = _ExecutorStub(result=sell_result)
    atr_stub = _AtrStub(atr)
    hooks = OpenTradeHooks(
        get_atr=atr_stub,
        execute_trade=executor,
        public_paper_mode=lambda: public_paper_mode,
    )
    manage_open_trades(
        OpenTradeContext(config=config, portfolio=portfolio),
        hooks,
        market_data,
        market_analysis,
    )
    return executor, atr_stub


# ---------------------------------------------------------------------------
# Market data guard
# ---------------------------------------------------------------------------

def test_missing_market_data_skips_trade_without_touching_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    trade = _trade()
    portfolio = _PortfolioStub({"BTCUSD": trade})
    with caplog.at_level(logging.WARNING):
        executor, atr_stub = _sweep(portfolio, {}, atr=99.0)

    assert "Current market data for BTCUSD not available, cannot monitor trade." in caplog.text
    assert executor.calls == []
    assert atr_stub.calls == []
    assert portfolio.removed == []
    assert portfolio.save_state_calls == 0
    assert "peak_price" not in trade


# ---------------------------------------------------------------------------
# Excursion bookkeeping
# ---------------------------------------------------------------------------

def test_peak_and_trough_are_recorded_and_saved_in_dry_run() -> None:
    trade = _trade(buy_price=100.0)
    portfolio = _PortfolioStub({"BTCUSD": trade})

    _sweep(portfolio, {"BTCUSD": {"price": 118.0}})

    assert trade["peak_price"] == 118.0
    assert trade["trough_price"] == 100.0
    assert portfolio.save_state_calls == 1


def test_existing_trough_is_lowered_and_saved() -> None:
    trade = _trade(buy_price=100.0, peak_price=105.0, trough_price=99.0)
    portfolio = _PortfolioStub({"BTCUSD": trade})

    _sweep(portfolio, {"BTCUSD": {"price": 97.5}})

    assert trade["peak_price"] == 105.0
    assert trade["trough_price"] == 97.5
    assert portfolio.save_state_calls == 1


def test_unchanged_excursions_do_not_save_state() -> None:
    trade = _trade(buy_price=100.0, peak_price=100.0, trough_price=100.0)
    portfolio = _PortfolioStub({"BTCUSD": trade})

    _sweep(portfolio, {"BTCUSD": {"price": 100.0}})

    assert trade["peak_price"] == 100.0
    assert trade["trough_price"] == 100.0
    assert portfolio.save_state_calls == 0


def test_excursion_update_without_dry_run_does_not_save_state() -> None:
    trade = _trade(buy_price=100.0)
    portfolio = _PortfolioStub({"BTCUSD": trade})

    _sweep(portfolio, {"BTCUSD": {"price": 111.0}}, config=_Config(dry_run=False))

    assert trade["peak_price"] == 111.0
    assert portfolio.save_state_calls == 0


# ---------------------------------------------------------------------------
# ATR handling
# ---------------------------------------------------------------------------

def test_atr_is_requested_with_period_14() -> None:
    portfolio = _PortfolioStub({"BTCUSD": _trade()})

    _, atr_stub = _sweep(portfolio, {"BTCUSD": {"price": 100.0}})

    assert atr_stub.calls == [("BTCUSD",)]
    assert atr_stub.kwargs == [{"period": 14}]


def test_nan_atr_falls_back_to_two_percent_of_buy_price() -> None:
    trade = _trade(buy_price=250.0)
    portfolio = _PortfolioStub({"BTCUSD": trade})

    _sweep(portfolio, {"BTCUSD": {"price": 250.0}}, atr=math.nan)

    # buy_price * 0.02 == 5.0 -> stop = 250 - 2 * 5, TP = 250 + 3 * 5
    assert trade["peak_price"] == 250.0
    assert portfolio.save_state_calls == 0


def test_zero_atr_falls_back_and_take_profit_uses_it() -> None:
    portfolio = _PortfolioStub({"BTCUSD": _trade(buy_price=200.0)})

    executor, _ = _sweep(portfolio, {"BTCUSD": {"price": 207.0}}, atr=0.0)

    # Fallback ATR is 2% of the buy price -> TP = 200 + 3 * 4 == 212, not reached.
    assert executor.calls == []
    assert portfolio.removed == []

    executor2, _ = _sweep(
        _PortfolioStub({"BTCUSD": _trade(buy_price=200.0)}),
        {"BTCUSD": {"price": 213.0}},
        atr=0.0,
    )
    assert executor2.kwargs[0]["reason"].startswith("\U0001f389 ATR-TAKE-PROFIT")


# ---------------------------------------------------------------------------
# Hold-limit warning
# ---------------------------------------------------------------------------

def test_hold_limit_warning_reports_overdue_and_atr_cost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    portfolio = _PortfolioStub({"BTCUSD": _trade(age_seconds=120.0)})

    with caplog.at_level(logging.WARNING):
        executor, _ = _sweep(
            portfolio,
            {"BTCUSD": {"price": 100.0}},
            config=_Config(max_hold_seconds=60),
        )

    message = caplog.text
    assert "Position hold limit reached: coin=BTCUSD" in message
    assert "limit_seconds=60" in message
    assert "exit is checked during the trading loop, not at an exact deadline" in message
    # The warning itself never exits: the core chain owns the decision.
    assert executor.kwargs[0]["reason"].startswith("\u23f0 MAX-HOLD-TIME reached")


def test_per_coin_hold_limit_overrides_global(
    caplog: pytest.LogCaptureFixture,
) -> None:
    portfolio = _PortfolioStub({"BTCUSD": _trade(age_seconds=120.0)})

    with caplog.at_level(logging.WARNING):
        _sweep(
            portfolio,
            {"BTCUSD": {"price": 100.0}},
            config=_Config(max_hold_seconds=3600, coin_max_hold_seconds={
                           "BTCUSD": 60}),
        )

    assert "limit_seconds=60" in caplog.text


# ---------------------------------------------------------------------------
# Partial take-profit short-circuit
# ---------------------------------------------------------------------------

def test_partial_take_profit_skips_the_exit_chain() -> None:
    config = _Config(
        partial_take_profit_enabled=True,
        partial_take_profit_atr_mult=1.0,
        partial_take_profit_fraction=0.4,
        atr_tp_mult=50.0,
    )
    trade = _trade(buy_price=100.0, amount_coin=2.0)
    portfolio = _PortfolioStub({"BTCUSD": trade})

    executor, _ = _sweep(portfolio, {"BTCUSD": {"price": 120.0}},
                         config=config, atr=2.0)

    assert len(executor.calls) == 1
    coin, action, price, quote_amount = executor.calls[0]
    assert (coin, action, price) == ("BTCUSD", "sell", 120.0)
    assert quote_amount == 0.8 * 120.0
    assert executor.kwargs[0]["partial_take_profit"] is True
    assert executor.kwargs[0]["reason"].startswith(
        "\U0001f4b8 PARTIAL-TAKE-PROFIT")
    # The position stays open and the exit chain never ran.
    assert portfolio.removed == []
    assert trade["partial_tp_taken"] is True
    assert isinstance(trade["partial_tp_timestamp"], datetime)
    # Once for the partial fill, once for the new peak written by the sweep.
    assert portfolio.save_state_calls == 2


def test_fraction_outside_open_interval_disables_partial_take_profit() -> None:
    config = _Config(
        partial_take_profit_enabled=True,
        partial_take_profit_atr_mult=1.0,
        partial_take_profit_fraction=1.0,
        atr_tp_mult=50.0,
    )
    portfolio = _PortfolioStub({"BTCUSD": _trade(buy_price=100.0)})

    executor, _ = _sweep(portfolio, {"BTCUSD": {"price": 102.0}},
                         config=config, atr=2.0)

    assert executor.calls == []
    assert portfolio.removed == []


# ---------------------------------------------------------------------------
# Exit chain and selling
# ---------------------------------------------------------------------------

def test_stop_loss_exit_sells_and_removes_trade() -> None:
    portfolio = _PortfolioStub({"BTCUSD": _trade(buy_price=100.0)})

    executor, _ = _sweep(portfolio, {"BTCUSD": {"price": 95.0}}, atr=2.0)

    assert executor.calls == [("BTCUSD", "sell", 95.0, 1.0 * 95.0)]
    assert executor.kwargs[0]["reason"].startswith("\U0001f6a8 ATR-STOP-LOSS")
    assert executor.kwargs[0]["atr"] == 2.0
    assert portfolio.removed == ["BTCUSD"]
    assert portfolio.open_trades == {}


def test_failed_sell_keeps_the_position(
    caplog: pytest.LogCaptureFixture,
) -> None:
    portfolio = _PortfolioStub({"BTCUSD": _trade(buy_price=100.0)})

    with caplog.at_level(logging.WARNING):
        executor, _ = _sweep(portfolio, {"BTCUSD": {"price": 95.0}},
                             atr=2.0, sell_result=False)

    assert len(executor.calls) == 1
    assert portfolio.removed == []
    assert "BTCUSD" in portfolio.open_trades
    assert "Exit sell failed for BTCUSD; retaining the position for retry." in caplog.text


def test_partial_fill_below_original_amount_is_retained() -> None:
    trade = _trade(buy_price=100.0, amount_coin=2.0)
    portfolio = _PortfolioStub({"BTCUSD": trade})

    def shrink_after_sell(*args: Any, **kwargs: Any) -> None:
        trade["amount_coin"] = 1.0

    executor = _ExecutorStub(result=True, side_effect=shrink_after_sell)
    hooks = OpenTradeHooks(
        get_atr=_AtrStub(2.0),
        execute_trade=executor,
        public_paper_mode=lambda: False,
    )
    manage_open_trades(
        OpenTradeContext(config=_Config(), portfolio=portfolio),
        hooks,
        {"BTCUSD": {"price": 95.0}},
        None,
    )

    assert portfolio.removed == []
    assert "BTCUSD" in portfolio.open_trades


def test_partial_fill_at_original_amount_is_removed() -> None:
    trade = _trade(buy_price=100.0, amount_coin=2.0)
    portfolio = _PortfolioStub({"BTCUSD": trade})

    executor = _ExecutorStub(result=True)
    hooks = OpenTradeHooks(
        get_atr=_AtrStub(2.0),
        execute_trade=executor,
        public_paper_mode=lambda: False,
    )
    manage_open_trades(
        OpenTradeContext(config=_Config(), portfolio=portfolio),
        hooks,
        {"BTCUSD": {"price": 95.0}},
        None,
    )

    assert portfolio.removed == ["BTCUSD"]


# ---------------------------------------------------------------------------
# Exit-reason precedence
# ---------------------------------------------------------------------------

def test_partial_tp_remainder_timeout_wins_over_core_checks() -> None:
    config = _Config(
        partial_take_profit_remainder_max_hold_seconds=30,
        atr_tp_mult=50.0,
    )
    trade = _trade(
        buy_price=100.0,
        partial_tp_taken=True,
        partial_tp_timestamp=datetime.now() - timedelta(seconds=90),
    )
    portfolio = _PortfolioStub({"BTCUSD": trade})

    executor, _ = _sweep(portfolio, {"BTCUSD": {"price": 100.0}},
                         config=config, atr=2.0)

    reason = executor.kwargs[0]["reason"]
    assert reason.startswith("\u23f1\ufe0f PARTIAL-TP-REMAINDER-TIMEOUT")


def test_downtrend_reversal_reason_wins_over_core_checks() -> None:
    config = _Config(
        downtrend_reversal_fast_exit_enabled=True,
        downtrend_reversal_max_hold_seconds=60,
        atr_tp_mult=50.0,
    )
    trade = _trade(buy_price=100.0, age_seconds=120.0,
                   recommendation="HOLD (Down-Trend)")
    portfolio = _PortfolioStub({"BTCUSD": trade})

    executor, _ = _sweep(
        portfolio,
        {"BTCUSD": {"price": 100.0}},
        config=config,
        atr=2.0,
        market_analysis={"BTCUSD": {"recommendation": "SELL"}},
    )

    reason = executor.kwargs[0]["reason"]
    assert reason.startswith("\u23f1\ufe0f DOWNTREND-REVERSAL-MAX-HOLD")


def test_uptrend_rules_reason_wins_over_core_checks() -> None:
    config = _Config(
        uptrend_rules_fast_exit_enabled=True,
        uptrend_rules_max_hold_seconds=60,
        atr_tp_mult=50.0,
    )
    trade = _trade(buy_price=100.0, age_seconds=120.0,
                   recommendation="HOLD (Up-Trend)", signal_source="rules")
    portfolio = _PortfolioStub({"BTCUSD": trade})

    executor, _ = _sweep(
        portfolio,
        {"BTCUSD": {"price": 100.0}},
        config=config,
        atr=2.0,
        market_analysis={"BTCUSD": {"recommendation": "HOLD"}},
    )

    reason = executor.kwargs[0]["reason"]
    assert reason.startswith("\u23f1\ufe0f UPTREND-RULES-MAX-HOLD")


def test_max_hold_exit_from_core_chain() -> None:
    config = _Config(max_hold_seconds=60)
    portfolio = _PortfolioStub({"BTCUSD": _trade(age_seconds=90.0)})

    executor, _ = _sweep(portfolio, {"BTCUSD": {"price": 100.0}}, config=config)

    assert executor.kwargs[0]["reason"].startswith(
        "\u23f0 MAX-HOLD-TIME reached (90s >= 60s)")
    assert portfolio.removed == ["BTCUSD"]


def test_negative_pnl_weak_signal_exit() -> None:
    config = _Config()
    portfolio = _PortfolioStub({"BTCUSD": _trade(buy_price=100.0)})

    executor, _ = _sweep(
        portfolio,
        {"BTCUSD": {"price": 99.0}},
        config=config,
        atr=10.0,
        market_analysis={"BTCUSD": {"recommendation": "WEAK SELL"}},
    )

    assert executor.kwargs[0]["reason"].startswith(
        "\U0001f53b PnL-NEG-WEAK-SIGNAL")


def test_profitable_position_with_neutral_signal_is_kept() -> None:
    portfolio = _PortfolioStub({"BTCUSD": _trade(buy_price=100.0)})

    executor, _ = _sweep(
        portfolio,
        {"BTCUSD": {"price": 103.0}},
        atr=2.0,
        market_analysis={"BTCUSD": {"recommendation": "HOLD"}},
    )

    assert executor.calls == []
    assert portfolio.removed == []
    assert portfolio.open_trades["BTCUSD"]["peak_price"] == 103.0


# ---------------------------------------------------------------------------
# Multi-position sweep
# ---------------------------------------------------------------------------

def test_sweep_visits_every_position_and_removes_only_exits() -> None:
    portfolio = _PortfolioStub({
        "BTCUSD": _trade(buy_price=100.0),
        "ETHUSD": _trade(buy_price=50.0),
        "SOLUSD": _trade(buy_price=10.0),
    })

    executor, atr_stub = _sweep(
        portfolio,
        {
            "BTCUSD": {"price": 95.0},   # stop loss
            "ETHUSD": {"price": 51.0},   # hold
            "SOLUSD": {"price": 5.0},    # stop loss
        },
        atr=2.0,
    )

    assert [call[0] for call in atr_stub.calls] == ["BTCUSD", "ETHUSD", "SOLUSD"]
    assert [call[0] for call in executor.calls] == ["BTCUSD", "SOLUSD"]
    assert portfolio.removed == ["BTCUSD", "SOLUSD"]
    assert list(portfolio.open_trades) == ["ETHUSD"]
    assert portfolio.save_state_calls == 1
