"""Unit tests for the layer-1 execution service.

The service receives the exchange, the config, the portfolio and the bot-owned
callbacks explicitly, so these tests drive the real code paths with stubs and
never import :mod:`src.trading_bot`.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any

import ccxt
import pytest

from src.execution_service import (
    TERMINAL_ORDER_STATUSES,
    EntryHooks,
    RealTradeHooks,
    confirm_order_fill,
    execute_real_trade,
    open_new_positions,
    order_fill_details,
)
from src.portfolio import Portfolio


@dataclass
class _Config:
    base_currency: str = "EUR"
    min_trade_amount: float = 10.0
    allow_partial_trades: bool = True
    reentry_cooldown_seconds: int = 3600
    max_entry_spread_bps: float = 0.0
    max_entry_cost_bps: float = 0.0
    live_taker_fee_rate: float | None = None
    live_slippage_rate: float | None = None
    kill_switch_file: str = ".trading_kill_switch"


class _ExchangeStub:
    """Minimal ccxt stand-in recording every call the service makes."""

    def __init__(
        self,
        *,
        min_amount: float | None = None,
        buy_order: dict[str, Any] | None = None,
        sell_order: dict[str, Any] | None = None,
        open_orders: list[dict[str, Any]] | None = None,
        closed_orders: list[dict[str, Any]] | None = None,
        order_error: Exception | None = None,
    ) -> None:
        self.min_amount = min_amount
        self.buy_order = {"id": "buy-1", "status": "closed"} if buy_order is None else buy_order
        self.sell_order = {"id": "sell-1", "status": "closed"} if sell_order is None else sell_order
        self.open_orders = open_orders or []
        self.closed_orders = closed_orders
        self.order_error = order_error
        self.calls: list[tuple[Any, ...]] = []

    def market(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("market", symbol))
        return {"limits": {"amount": {"min": self.min_amount}}}

    def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        self.calls.append(("open_orders", symbol))
        return self.open_orders

    def fetch_closed_orders(self, symbol: str) -> list[dict[str, Any]]:
        self.calls.append(("closed_orders", symbol))
        if self.closed_orders is None:
            raise ccxt.NotSupported("closed orders unavailable")
        return self.closed_orders

    def create_market_buy_order(
        self, symbol: str, amount: float, params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("buy", symbol, amount)
                          if not params else ("buy", symbol, amount, params))
        if self.order_error:
            raise self.order_error
        return self.buy_order

    def create_market_sell_order(
        self, symbol: str, amount: float, params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("sell", symbol, amount)
                          if not params else ("sell", symbol, amount, params))
        if self.order_error:
            raise self.order_error
        return self.sell_order


class _Recorder:
    """Records every hook invocation the service performs."""

    def __init__(self, *, fill: tuple[float, float, float] | None = None,
                 fill_error: Exception | None = None) -> None:
        self.fill = fill
        self.fill_error = fill_error
        self.events: list[Any] = []
        self.journal_rows: list[dict[str, Any]] = []

    def confirm_fill(self, order, symbol, requested_amount, fallback_price):
        self.events.append(("confirm_fill", order.get("id")))
        if self.fill_error:
            raise self.fill_error
        assert self.fill is not None
        return self.fill

    def record_close_performance(self, entry_trade, average, sold_amount, **kwargs):
        self.events.append(("close_performance", average, sold_amount, kwargs))
        return 5.0, 10.0, 60.0

    def trade_excursion_metrics(self, entry_trade):
        self.events.append(("excursion",))
        return 1.5, -0.5

    def append_trade_journal(self, row):
        self.journal_rows.append(row)
        self.events.append(("journal", row["action"], row["price"]))
        return row

    def register_buy_timestamp(self):
        self.events.append(("buy_ts",))

    def register_sell_timestamp(self, coin):
        self.events.append(("sell_ts", coin))

    def update_portfolio_balance(self):
        self.events.append(("balance",))

    def real_hooks(self) -> RealTradeHooks:
        return RealTradeHooks(
            confirm_fill=self.confirm_fill,
            record_close_performance=self.record_close_performance,
            trade_excursion_metrics=self.trade_excursion_metrics,
            append_trade_journal=self.append_trade_journal,
            register_buy_timestamp=self.register_buy_timestamp,
            register_sell_timestamp=self.register_sell_timestamp,
            update_portfolio_balance=self.update_portfolio_balance,
        )


def _performance() -> dict[str, Any]:
    return {
        "buys": 0,
        "sells": 0,
        "by_source": defaultdict(
            lambda: {"buys": 0, "sells": 0, "pnl": 0.0}),
    }


def _portfolio(cash: float = 1000.0) -> Portfolio:
    portfolio = Portfolio("EUR")
    portfolio.cash = cash
    portfolio.save_state = lambda *args, **kwargs: True  # type: ignore[method-assign]
    return portfolio


# ---------------------------------------------------------------------------
# Fill details / confirmation
# ---------------------------------------------------------------------------
def test_terminal_order_statuses_are_shared_with_the_confirmation_state_machine():
    assert "closed" in TERMINAL_ORDER_STATUSES
    assert "rejected" in TERMINAL_ORDER_STATUSES


def test_order_fill_details_prefers_reported_partial_fill():
    filled, average, cost = order_fill_details(
        {"status": "closed", "amount": 1.0, "filled": 0.4,
         "average": 102.0, "cost": 40.8},
        requested_amount=1.0,
        fallback_price=100.0,
    )
    assert (filled, average, cost) == (0.4, 102.0, 40.8)


def test_confirm_order_fill_refreshes_a_non_terminal_order():
    exchange = _ExchangeStub()
    exchange.fetch_order = lambda order_id, symbol: {  # type: ignore[method-assign]
        "id": order_id,
        "status": "closed",
        "filled": 0.75,
        "average": 101.0,
        "cost": 75.75,
    }
    exchange.cancel_order = lambda order_id, symbol: {}  # type: ignore[method-assign]

    details = confirm_order_fill(
        exchange,
        {"id": "order-1", "status": "open", "amount": 1.0},
        "BTC/EUR",
        requested_amount=1.0,
        fallback_price=100.0,
    )
    assert details == (0.75, 101.0, 75.75)


def test_confirm_order_fill_rejects_an_order_that_stays_non_terminal():
    exchange = _ExchangeStub()
    exchange.cancel_order = lambda order_id, symbol: {}  # type: ignore[method-assign]
    exchange.fetch_order = lambda order_id, symbol: {  # type: ignore[method-assign]
        "id": order_id, "status": "open", "filled": 0.5, "amount": 1.0,
    }

    with pytest.raises(ValueError, match="non-terminal"):
        confirm_order_fill(
            exchange,
            {"id": "order-2", "status": "open", "amount": 1.0},
            "BTC/EUR",
            requested_amount=1.0,
            fallback_price=100.0,
        )


def test_confirm_order_fill_needs_an_order_id_for_an_unconfirmed_response():
    with pytest.raises(ValueError, match="no usable order ID"):
        confirm_order_fill(
            _ExchangeStub(),
            {"status": "open", "amount": 1.0},
            "BTC/EUR",
            requested_amount=1.0,
            fallback_price=100.0,
        )


# ---------------------------------------------------------------------------
# Real trade execution
# ---------------------------------------------------------------------------
def test_real_buy_accounts_the_confirmed_fill():
    config = _Config()
    portfolio = _portfolio()
    performance = _performance()
    recorder = _Recorder(fill=(0.5, 100.0, 50.0))
    exchange = _ExchangeStub()

    executed = execute_real_trade(
        exchange, config, portfolio, performance, recorder.real_hooks(),
        "BTC", "buy", 100.0, 50.0,
        signal_source="ai_copilot", signal_confidence=0.8,
        recommendation="BUY", reason="ENTRY",
    )

    assert executed is True
    assert exchange.calls == [
        ("market", "BTC/EUR"), ("open_orders", "BTC/EUR"),
        ("buy", "BTC/EUR", 0.5),
    ]
    trade = portfolio.open_trades["BTC"]
    assert trade["amount_coin"] == 0.5
    assert trade["amount_base"] == 50.0
    assert trade["signal_source"] == "ai_copilot"
    assert performance["buys"] == 1
    assert performance["by_source"]["ai_copilot"]["buys"] == 1
    assert recorder.events == [
        ("confirm_fill", "buy-1"),
        ("buy_ts",),
        ("journal", "buy", 100.0),
        ("balance",),
    ]


def test_real_buy_includes_reported_quote_fee_in_entry_basis():
    config = _Config()
    portfolio = _portfolio()
    recorder = _Recorder(fill=(0.5, 100.0, 50.0))
    exchange = _ExchangeStub(buy_order={
        "id": "buy-fee", "status": "closed",
        "fees": [{"cost": 0.2, "currency": "EUR"}],
    })

    executed = execute_real_trade(
        exchange, config, portfolio, _performance(), recorder.real_hooks(),
        "BTC", "buy", 100.0, 50.0,
    )

    assert executed is True
    assert portfolio.open_trades["BTC"]["amount_base"] == pytest.approx(50.2)
    assert portfolio.open_trades["BTC"]["entry_fee_known"] is True
    assert recorder.journal_rows[0]["fee_base"] == pytest.approx(0.2)
    assert recorder.journal_rows[0]["amount_base"] == pytest.approx(50.2)
    assert recorder.journal_rows[0]["execution_cost_mode"] == "live_exchange"


def test_real_buy_without_confirmable_fill_latches_kill_switch(tmp_path):
    config = _Config()
    config.kill_switch_file = str(tmp_path / "STOP")
    portfolio = _portfolio()
    performance = _performance()
    recorder = _Recorder(fill_error=ValueError("remains non-terminal"))
    exchange = _ExchangeStub()

    executed = execute_real_trade(
        exchange, config, portfolio, performance, recorder.real_hooks(),
        "BTC", "buy", 100.0, 50.0,
    )

    assert executed is False
    assert portfolio.open_trades == {}
    assert performance["buys"] == 0
    assert ("balance",) not in recorder.events
    assert "uncertain_order_outcome" in (tmp_path / "STOP").read_text()


def test_real_buy_submission_timeout_latches_kill_switch(tmp_path):
    config = _Config()
    config.kill_switch_file = str(tmp_path / "STOP")
    exchange = _ExchangeStub(order_error=ccxt.NetworkError("submission timeout"))
    recorder = _Recorder(fill=(0.5, 100.0, 50.0))

    executed = execute_real_trade(
        exchange, config, _portfolio(), _performance(), recorder.real_hooks(),
        "BTC", "buy", 100.0, 50.0,
    )

    assert executed is False
    assert "uncertain_order_outcome" in (tmp_path / "STOP").read_text()
    assert recorder.events == []


def test_real_sell_updates_the_remaining_position_on_a_partial_fill():
    config = _Config()
    portfolio = _portfolio()
    portfolio.holdings["BTC"] = 1.0
    portfolio.open_trades["BTC"] = {
        "buy_price": 90.0, "amount_coin": 1.0, "amount_base": 90.0,
        "signal_source": "rules", "signal_confidence": None,
        "recommendation": "HOLD",
    }
    performance = _performance()
    recorder = _Recorder(fill=(0.4, 110.0, 44.0))
    exchange = _ExchangeStub()

    executed = execute_real_trade(
        exchange, config, portfolio, performance, recorder.real_hooks(),
        "BTC", "sell", 110.0, 44.0, reason="TAKE_PROFIT",
    )

    assert executed is True
    assert portfolio.open_trades["BTC"]["amount_coin"] == pytest.approx(0.6)
    assert portfolio.open_trades["BTC"]["amount_base"] == pytest.approx(54.0)
    assert ("sell_ts", "BTC") in recorder.events
    assert ("balance",) in recorder.events


def test_real_sell_closes_the_position_completely():
    config = _Config()
    portfolio = _portfolio()
    portfolio.holdings["BTC"] = 1.0
    portfolio.open_trades["BTC"] = {
        "buy_price": 90.0, "amount_coin": 1.0, "amount_base": 90.0,
    }
    recorder = _Recorder(fill=(1.0, 110.0, 110.0))
    exchange = _ExchangeStub()

    executed = execute_real_trade(
        exchange, config, portfolio, performance=_performance(),
        hooks=recorder.real_hooks(), coin="BTC", action="sell",
        price=110.0, amount_in_base_currency=110.0,
    )

    assert executed is True
    assert "BTC" not in portfolio.open_trades


def test_real_sell_uses_reported_quote_fee_for_net_proceeds():
    portfolio = _portfolio()
    portfolio.holdings["BTC"] = 1.0
    portfolio.open_trades["BTC"] = {
        "buy_price": 90.0, "amount_coin": 1.0, "amount_base": 90.2,
        "entry_fee_known": True,
    }
    recorder = _Recorder(fill=(1.0, 110.0, 110.0))
    exchange = _ExchangeStub(sell_order={
        "id": "sell-fee", "status": "closed",
        "fees": [{"cost": 0.22, "currency": "EUR"}],
    })

    executed = execute_real_trade(
        exchange, _Config(), portfolio, _performance(), recorder.real_hooks(),
        "BTC", "sell", 110.0, 110.0,
    )

    assert executed is True
    close_call = next(event for event in recorder.events if event[0] == "close_performance")
    assert close_call[3]["net_proceeds"] == pytest.approx(109.78)
    assert recorder.journal_rows[0]["net_pnl_base"] == pytest.approx(5.0)
    assert recorder.journal_rows[0]["fee_base"] == pytest.approx(0.22)


def test_real_sell_does_not_label_unknown_fee_pnl_as_net():
    portfolio = _portfolio()
    portfolio.holdings["BTC"] = 1.0
    portfolio.open_trades["BTC"] = {
        "buy_price": 90.0, "amount_coin": 1.0, "amount_base": 90.0,
    }
    recorder = _Recorder(fill=(1.0, 110.0, 110.0))
    exchange = _ExchangeStub(sell_order={
        "id": "sell-fee-unknown", "status": "closed",
        "fees": [{"cost": 0.01, "currency": "KCS"}],
    })

    executed = execute_real_trade(
        exchange, _Config(), portfolio, _performance(), recorder.real_hooks(),
        "BTC", "sell", 110.0, 110.0,
    )

    assert executed is True
    row = recorder.journal_rows[0]
    assert row["pnl_base"] == ""
    assert row["net_pnl_base"] == ""
    assert row["execution_cost_mode"] == "live_fee_unknown"
    assert '"currency": "KCS"' in row["exchange_fees"]


def test_real_sell_without_holdings_is_refused_before_the_order():
    recorder = _Recorder(fill=(1.0, 110.0, 110.0))
    exchange = _ExchangeStub()

    executed = execute_real_trade(
        exchange, _Config(), _portfolio(), _performance(), recorder.real_hooks(),
        "BTC", "sell", 110.0, 110.0,
    )

    assert executed is False
    assert exchange.calls == [
        ("market", "BTC/EUR"), ("open_orders", "BTC/EUR")]
    assert recorder.events == []


def test_real_trade_refuses_to_order_when_exchange_has_open_orders():
    exchange = _ExchangeStub(open_orders=[{"id": "pending"}])
    recorder = _Recorder(fill=(0.5, 100.0, 50.0))

    executed = execute_real_trade(
        exchange, _Config(), _portfolio(), _performance(), recorder.real_hooks(),
        "BTC", "buy", 100.0, 50.0,
    )

    assert executed is False
    assert exchange.calls == [("market", "BTC/EUR"), ("open_orders", "BTC/EUR")]
    assert recorder.events == []


def test_real_trade_checks_execution_lease_before_order():
    exchange = _ExchangeStub()
    recorder = _Recorder(fill=(0.5, 100.0, 50.0))
    hooks = replace(
        recorder.real_hooks(),
        assert_execution_owner=lambda: (_ for _ in ()).throw(
            RuntimeError("cluster lease lost")),
    )

    with pytest.raises(RuntimeError, match="cluster lease lost"):
        execute_real_trade(
            exchange, _Config(), _portfolio(), _performance(), hooks,
            "BTC", "buy", 100.0, 50.0,
        )

    assert exchange.calls == [("market", "BTC/EUR"), ("open_orders", "BTC/EUR")]
    assert recorder.events == []


def test_trade_below_the_exchange_minimum_is_not_sent():
    recorder = _Recorder(fill=(0.5, 100.0, 50.0))
    exchange = _ExchangeStub(min_amount=1.0)

    executed = execute_real_trade(
        exchange, _Config(), _portfolio(), _performance(), recorder.real_hooks(),
        "BTC", "buy", 100.0, 50.0,
    )

    assert executed is False
    assert exchange.calls == [("market", "BTC/EUR")]
    assert recorder.events == []


@pytest.mark.parametrize(
    "error",
    [
        ccxt.InsufficientFunds("no funds"),
        ccxt.InvalidOrder("bad order"),
        ccxt.NetworkError("timeout"),
        ccxt.ExchangeError("rejected"),
        ccxt.BaseError("exchange problem"),
    ],
)
def test_exchange_errors_are_reported_as_a_failed_trade(error, caplog, tmp_path):
    exchange = _ExchangeStub(order_error=error)
    recorder = _Recorder(fill=(0.5, 100.0, 50.0))
    config = _Config()
    config.kill_switch_file = str(tmp_path / "STOP")

    with caplog.at_level(logging.ERROR, logger="src.execution_service"):
        executed = execute_real_trade(
            exchange, config, _portfolio(), _performance(),
            recorder.real_hooks(), "BTC", "buy", 100.0, 50.0,
        )

    assert executed is False
    assert recorder.events == []
    assert "buy BTC" in caplog.text or "Insufficient funds" in caplog.text


def test_real_trade_requires_an_exchange(caplog):
    recorder = _Recorder(fill=(0.5, 100.0, 50.0))

    with caplog.at_level(logging.ERROR, logger="src.execution_service"):
        executed = execute_real_trade(
            None, _Config(), _portfolio(), _performance(),
            recorder.real_hooks(), "BTC", "buy", 100.0, 50.0,
        )

    assert executed is False
    assert "without an exchange" in caplog.text


@pytest.mark.parametrize("action", ["buy", "sell"])
def test_kill_switch_blocks_live_orders_before_exchange_call(tmp_path, action):
    kill_switch = tmp_path / "STOP_TRADING"
    kill_switch.touch()
    config = _Config()
    config.kill_switch_file = str(kill_switch)
    portfolio = _portfolio()
    portfolio.holdings["BTC"] = 1.0
    exchange = _ExchangeStub()
    recorder = _Recorder(fill=(0.5, 100.0, 50.0))

    executed = execute_real_trade(
        exchange, config, portfolio, _performance(), recorder.real_hooks(),
        "BTC", action, 100.0, 50.0,
    )

    assert executed is False
    assert exchange.calls == []
    assert recorder.events == []


# ---------------------------------------------------------------------------
# Idempotent submission (audit point 5)
# ---------------------------------------------------------------------------
def test_client_order_id_is_sent_with_the_live_order(tmp_path):
    config = _Config()
    config.kill_switch_file = str(tmp_path / "STOP")
    exchange = _ExchangeStub()
    recorder = _Recorder(fill=(0.5, 100.0, 50.0))

    executed = execute_real_trade(
        exchange, config, _portfolio(), _performance(), recorder.real_hooks(),
        "BTC", "buy", 100.0, 50.0, client_order_id="tb-abcd1234-run0001-7-BTC-buy",
    )

    assert executed is True
    assert exchange.calls == [
        ("market", "BTC/EUR"), ("open_orders", "BTC/EUR"),
        ("buy", "BTC/EUR", 0.5, {"clientOrderId": "tb-abcd1234-run0001-7-BTC-buy"}),
    ]
    row = recorder.journal_rows[0]
    assert row["client_order_id"] == "tb-abcd1234-run0001-7-BTC-buy"


def test_submission_timeout_recovers_the_original_order_without_resubmitting(tmp_path, caplog):
    """A visible order after a network error is accounted, never sent twice."""
    config = _Config()
    config.kill_switch_file = str(tmp_path / "STOP")
    exchange = _ExchangeStub(
        order_error=ccxt.NetworkError("submission timeout"),
        closed_orders=[{
            "id": "filled-1", "clientOrderId": "tb-1-buy", "status": "closed",
            "amount": 0.5, "filled": 0.5, "average": 100.0, "cost": 50.0,
        }],
    )
    recorder = _Recorder(fill=(0.5, 100.0, 50.0))
    portfolio = _portfolio()

    with caplog.at_level(logging.WARNING, logger="src.order_submission"):
        executed = execute_real_trade(
            exchange, config, portfolio, _performance(), recorder.real_hooks(),
            "BTC", "buy", 100.0, 50.0, client_order_id="tb-1-buy",
        )

    assert executed is True
    assert not (tmp_path / "STOP").exists()
    assert [call for call in exchange.calls if call[0] == "buy"] == [
        ("buy", "BTC/EUR", 0.5, {"clientOrderId": "tb-1-buy"})]
    assert portfolio.open_trades["BTC"]["amount_coin"] == 0.5
    assert "accounting for the original order instead of resubmitting" in caplog.text


def test_unrecoverable_submission_timeout_latches_kill_switch_and_stops(tmp_path, caplog):
    config = _Config()
    config.kill_switch_file = str(tmp_path / "STOP")
    exchange = _ExchangeStub(
        order_error=ccxt.NetworkError("submission timeout"), closed_orders=[])
    recorder = _Recorder(fill=(0.5, 100.0, 50.0))

    with caplog.at_level(logging.ERROR, logger="src.execution_service"):
        executed = execute_real_trade(
            exchange, config, _portfolio(), _performance(), recorder.real_hooks(),
            "BTC", "buy", 100.0, 50.0, client_order_id="tb-1-buy",
        )

    assert executed is False
    assert "it is not resubmitted" in caplog.text
    assert "uncertain_order_outcome: BUY BTC" in (tmp_path / "STOP").read_text()
    assert [call for call in exchange.calls if call[0] == "buy"] == [
        ("buy", "BTC/EUR", 0.5, {"clientOrderId": "tb-1-buy"})]
    assert recorder.events == []


def test_live_journal_keeps_requested_amount_and_partial_fill(tmp_path):
    config = _Config()
    config.kill_switch_file = str(tmp_path / "STOP")
    recorder = _Recorder(fill=(0.4, 101.0, 40.4))
    exchange = _ExchangeStub()

    executed = execute_real_trade(
        exchange, config, _portfolio(), _performance(), recorder.real_hooks(),
        "BTC", "buy", 100.0, 50.0, client_order_id="tb-1-buy",
    )

    assert executed is True
    row = recorder.journal_rows[0]
    assert row["requested_amount_coin"] == pytest.approx(0.5)
    assert row["partial_fill"] == 1


def test_live_journal_records_the_pre_trade_cost_estimate():
    config = _Config(live_taker_fee_rate=0.004, live_slippage_rate=0.001)
    recorder = _Recorder(fill=(0.5, 100.0, 50.0))

    executed = execute_real_trade(
        _ExchangeStub(), config, _portfolio(), _performance(),
        recorder.real_hooks(), "BTC", "buy", 100.0, 50.0,
    )

    assert executed is True
    row = recorder.journal_rows[0]
    assert row["est_taker_fee_rate"] == pytest.approx(0.004)
    assert row["est_slippage_rate"] == pytest.approx(0.001)
    assert row["est_fee_base"] == pytest.approx(0.2)
    assert row["est_slippage_base"] == pytest.approx(0.05)
    assert row["est_total_cost_base"] == pytest.approx(0.25)
    assert row["est_cost_pct"] == pytest.approx(0.5)
    # No order book is available for a live order, so the spread stays blank.
    assert row["est_spread_base"] == ""
    assert row["est_spread_bps"] == ""


# ---------------------------------------------------------------------------
# Pre-trade cost budget (audit point 4)
# ---------------------------------------------------------------------------
def _cost_gate_hooks(recorder: _EntryRecorder) -> tuple[EntryHooks, list[str]]:
    base = recorder.hooks()
    news_calls: list[str] = []
    return EntryHooks(
        get_atr=base.get_atr,
        compute_position_size=base.compute_position_size,
        passes_momentum_filter=base.passes_momentum_filter,
        resolve_cooldown_seconds=base.resolve_cooldown_seconds,
        in_reentry_cooldown=base.in_reentry_cooldown,
        execute_trade=base.execute_trade,
        log_blocked_attempts=base.log_blocked_attempts,
        news_veto=lambda coin, candidate: news_calls.append(coin) or (False, "clear"),
    ), news_calls


def test_cost_budget_blocks_before_the_news_veto(caplog):
    recorder = _EntryRecorder()
    hooks, news_calls = _cost_gate_hooks(recorder)
    config = _Config(max_entry_cost_bps=40.0, live_taker_fee_rate=0.0026,
                     live_slippage_rate=0.001)

    with caplog.at_level(logging.INFO, logger="src.execution_service"):
        open_new_positions(
            config, _portfolio(), hooks,
            {"BTC": {"price": 100.0}},
            {"BTC": {"score": 80, "recommendation": "BUY",
                     "bid": 99.5, "ask": 100.5}},
            ["BTC"], 1000.0,
            effective_trade_amount=100.0, effective_max_open_trades=3,
            can_open_positions=True, block_reason="",
        )

    assert news_calls == []
    assert recorder.executed == []
    assert recorder.blocked[0]["reason"].startswith("estimated_cost_above_limit")
    assert "Estimated execution cost blocked entry" in caplog.text


def test_cost_budget_allows_an_order_inside_the_budget():
    recorder = _EntryRecorder()
    hooks, news_calls = _cost_gate_hooks(recorder)
    config = _Config(max_entry_cost_bps=500.0, live_taker_fee_rate=0.0026,
                     live_slippage_rate=0.001)

    open_new_positions(
        config, _portfolio(), hooks,
        {"BTC": {"price": 100.0}},
        {"BTC": {"score": 80, "recommendation": "BUY",
                 "bid": 99.9, "ask": 100.1}},
        ["BTC"], 1000.0,
        effective_trade_amount=100.0, effective_max_open_trades=3,
        can_open_positions=True, block_reason="",
    )

    assert news_calls == ["BTC"]
    assert recorder.executed == ["BTC"]


def test_cost_budget_is_disabled_below_one_basis_point():
    recorder = _EntryRecorder()
    hooks, _news_calls = _cost_gate_hooks(recorder)
    config = _Config(max_entry_cost_bps=0.5, live_taker_fee_rate=0.5,
                     live_slippage_rate=0.5)

    open_new_positions(
        config, _portfolio(), hooks,
        {"BTC": {"price": 100.0}},
        {"BTC": {"score": 80, "recommendation": "BUY"}},
        ["BTC"], 1000.0,
        effective_trade_amount=100.0, effective_max_open_trades=3,
        can_open_positions=True, block_reason="",
    )

    assert recorder.executed == ["BTC"]


def test_unmatchable_quote_does_not_add_a_spread_cost():
    recorder = _EntryRecorder()
    hooks, _news_calls = _cost_gate_hooks(recorder)
    config = _Config(max_entry_cost_bps=40.0, live_taker_fee_rate=0.0026,
                     live_slippage_rate=0.001)

    open_new_positions(
        config, _portfolio(), hooks,
        {"BTC": {"price": 100.0}},
        {"BTC": {"score": 80, "recommendation": "BUY",
                 "bid": 80.0, "ask": 82.0, "spread_bps": 250.0}},
        ["BTC"], 1000.0,
        effective_trade_amount=100.0, effective_max_open_trades=3,
        can_open_positions=True, block_reason="",
    )

    assert recorder.executed == ["BTC"]
    assert recorder.blocked == []


# ---------------------------------------------------------------------------
# Entry loop
# ---------------------------------------------------------------------------
@dataclass
class _EntryRecorder:
    atr: float = 5.0
    position_size: float = 100.0
    momentum: tuple[bool, str] = (True, "")
    cooldown_seconds: tuple[int, str] = (3600, "static")
    in_cooldown: tuple[bool, int] = (False, 0)
    succeed: bool = True
    events: list[Any] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)

    def hooks(self) -> EntryHooks:
        return EntryHooks(
            get_atr=lambda coin, period=14: self.atr,
            compute_position_size=self._position_size,
            passes_momentum_filter=lambda coin_data: self.momentum,
            resolve_cooldown_seconds=lambda price, atr: self.cooldown_seconds,
            in_reentry_cooldown=lambda coin, cooldown_seconds=None: self.in_cooldown,
            execute_trade=self._execute_trade,
            log_blocked_attempts=self._log_blocked,
        )

    def _position_size(self, coin, price, atr, portfolio_value, trade_amount, attempt_meta):
        self.events.append(("size", coin, price, atr))
        attempt_meta["position_size_text"] = f"{self.position_size:.2f}"
        return self.position_size

    def _execute_trade(self, coin, action, price, amount, **kwargs):
        self.executed.append(coin)
        if self.succeed:
            self.events.append(("trade", coin, action, price, amount))
            return True
        return False

    def _log_blocked(self, blocked_attempts):
        self.blocked = list(blocked_attempts)


def _entry_args(recorder: _EntryRecorder, coins: list[str], market_data: dict[str, Any]):
    return (
        _Config(),
        _portfolio(),
        recorder.hooks(),
        market_data,
        {coin: {"score": 1, "recommendation": "BUY"} for coin in coins},
        coins,
        1000.0,
    )


def test_risk_guardrail_blocks_every_new_position(caplog):
    recorder = _EntryRecorder()

    with caplog.at_level(logging.WARNING, logger="src.execution_service"):
        open_new_positions(
            *_entry_args(recorder, ["BTC"], {"BTC": {"price": 100.0}}),
            effective_trade_amount=100.0,
            effective_max_open_trades=3,
            can_open_positions=False,
            block_reason="Daily-Loss-Limit reached",
        )

    assert "Daily-Loss-Limit reached" in caplog.text
    assert recorder.executed == []
    assert recorder.blocked == []


def test_candidate_blocked_reasons_are_reported_in_order():
    recorder = _EntryRecorder(position_size=100.0, succeed=False)
    market_data = {
        "BTC": {"price": 100.0},
        "ETH": {"price": 50.0},
    }

    open_new_positions(
        *_entry_args(recorder, ["BTC", "ETH", "ADA"], market_data),
        effective_trade_amount=100.0,
        effective_max_open_trades=3,
        can_open_positions=True,
        block_reason="",
    )

    assert recorder.executed == ["BTC", "ETH"]
    reasons = [row["reason"] for row in recorder.blocked]
    assert reasons == [
        "execute_trade_failed",
        "execute_trade_failed",
        "missing_price_data",
    ]
    assert recorder.blocked[2]["coin"] == "ADA"


def test_momentum_filter_and_cooldown_block_before_the_order():
    recorder = _EntryRecorder(momentum=(False, "rsi_too_high"))
    open_new_positions(
        *_entry_args(recorder, ["BTC"], {"BTC": {"price": 100.0}}),
        effective_trade_amount=100.0,
        effective_max_open_trades=3,
        can_open_positions=True,
        block_reason="",
    )
    assert recorder.executed == []
    assert recorder.blocked[0]["reason"] == "rsi_too_high"

    recorder = _EntryRecorder(in_cooldown=(True, 120),
                              cooldown_seconds=(900, "volatility-scaled"))
    open_new_positions(
        *_entry_args(recorder, ["BTC"], {"BTC": {"price": 100.0}}),
        effective_trade_amount=100.0,
        effective_max_open_trades=3,
        can_open_positions=True,
        block_reason="",
    )
    assert recorder.executed == []
    assert recorder.blocked[0]["reason"] == "reentry_cooldown_active"
    assert recorder.blocked[0]["cooldown_text"] == (
        "120s remaining (effective=900s, volatility-scaled)")


def test_local_entry_gates_skip_news_veto_call():
    news_calls = []
    recorder = _EntryRecorder(momentum=(False, "local_gate"))
    hooks = recorder.hooks()
    hooks = EntryHooks(
        get_atr=hooks.get_atr,
        compute_position_size=hooks.compute_position_size,
        passes_momentum_filter=hooks.passes_momentum_filter,
        resolve_cooldown_seconds=hooks.resolve_cooldown_seconds,
        in_reentry_cooldown=hooks.in_reentry_cooldown,
        execute_trade=hooks.execute_trade,
        log_blocked_attempts=hooks.log_blocked_attempts,
        news_veto=lambda coin, candidate: news_calls.append(coin) or (False, "clear"),
    )

    open_new_positions(
        _Config(), _portfolio(), hooks, {"BTC": {"price": 100.0}},
        {"BTC": {"score": 80, "recommendation": "BUY"}}, ["BTC"], 1000.0,
        effective_trade_amount=100.0, effective_max_open_trades=3,
        can_open_positions=True, block_reason="",
    )

    assert news_calls == []
    assert recorder.executed == []


def test_news_veto_blocks_candidate_before_order():
    recorder = _EntryRecorder()
    hooks = recorder.hooks()
    hooks = EntryHooks(
        get_atr=hooks.get_atr,
        compute_position_size=hooks.compute_position_size,
        passes_momentum_filter=hooks.passes_momentum_filter,
        resolve_cooldown_seconds=hooks.resolve_cooldown_seconds,
        in_reentry_cooldown=hooks.in_reentry_cooldown,
        execute_trade=hooks.execute_trade,
        log_blocked_attempts=hooks.log_blocked_attempts,
        news_veto=lambda _coin, _candidate: (True, "exchange announcement risk"),
    )

    open_new_positions(
        _Config(), _portfolio(), hooks, {"BTC": {"price": 100.0}},
        {"BTC": {"score": 80, "recommendation": "BUY"}}, ["BTC"], 1000.0,
        effective_trade_amount=100.0, effective_max_open_trades=3,
        can_open_positions=True, block_reason="",
    )

    assert recorder.executed == []
    assert recorder.blocked[0]["reason"] == "news_veto:exchange announcement risk"


def test_excessive_spread_blocks_before_news_and_order():
    recorder = _EntryRecorder()
    news_calls = []
    base_hooks = recorder.hooks()
    hooks = EntryHooks(
        get_atr=base_hooks.get_atr,
        compute_position_size=base_hooks.compute_position_size,
        passes_momentum_filter=base_hooks.passes_momentum_filter,
        resolve_cooldown_seconds=base_hooks.resolve_cooldown_seconds,
        in_reentry_cooldown=base_hooks.in_reentry_cooldown,
        execute_trade=base_hooks.execute_trade,
        log_blocked_attempts=base_hooks.log_blocked_attempts,
        news_veto=lambda coin, candidate: news_calls.append(coin) or (False, "clear"),
    )
    config = _Config(max_entry_spread_bps=75.0)

    open_new_positions(
        config, _portfolio(), hooks,
        {"BTC": {"price": 100.0}},
        {"BTC": {"score": 80, "recommendation": "BUY", "spread_bps": 100.0}},
        ["BTC"], 1000.0,
        effective_trade_amount=100.0, effective_max_open_trades=3,
        can_open_positions=True, block_reason="",
    )

    assert news_calls == []
    assert recorder.executed == []
    assert recorder.blocked[0]["reason"] == "spread_unavailable_or_above_limit"


def test_position_below_the_minimum_trade_amount_is_skipped():
    recorder = _EntryRecorder(position_size=5.0)

    open_new_positions(
        *_entry_args(recorder, ["BTC"], {"BTC": {"price": 100.0}}),
        effective_trade_amount=100.0,
        effective_max_open_trades=3,
        can_open_positions=True,
        block_reason="",
    )

    assert recorder.executed == []
    assert recorder.blocked[0]["reason"].startswith("below_min_trade_amount")


def test_candidate_loop_stops_once_the_max_open_trades_is_reached():
    recorder = _EntryRecorder()
    portfolio = _portfolio()
    portfolio.open_trades["XMR"] = {"amount_coin": 1.0}

    def _add_position(coin, action, price, amount, **kwargs):
        recorder.executed.append(coin)
        portfolio.open_trades[coin] = {"amount_coin": 1.0}
        return True

    recorder.succeed = True
    hooks = recorder.hooks()

    open_new_positions(
        _Config(),
        portfolio,
        EntryHooks(
            get_atr=hooks.get_atr,
            compute_position_size=hooks.compute_position_size,
            passes_momentum_filter=hooks.passes_momentum_filter,
            resolve_cooldown_seconds=hooks.resolve_cooldown_seconds,
            in_reentry_cooldown=hooks.in_reentry_cooldown,
            execute_trade=_add_position,
            log_blocked_attempts=hooks.log_blocked_attempts,
        ),
        {"BTC": {"price": 100.0}, "ETH": {"price": 50.0}},
        {"BTC": {"score": 2}, "ETH": {"score": 1}},
        ["BTC", "ETH"],
        1000.0,
        effective_trade_amount=100.0,
        effective_max_open_trades=2,
        can_open_positions=True,
        block_reason="",
    )

    assert recorder.executed == ["BTC"]
    assert set(portfolio.open_trades) == {"XMR", "BTC"}


def test_existing_positions_are_not_bought_again(caplog):
    recorder = _EntryRecorder()
    portfolio = _portfolio()
    portfolio.open_trades["BTC"] = {"amount_coin": 1.0}
    portfolio.holdings["ETH"] = 1.0

    with caplog.at_level(logging.DEBUG, logger="src.execution_service"):
        open_new_positions(
            _Config(), portfolio, recorder.hooks(),
            {"BTC": {"price": 100.0}, "ETH": {"price": 50.0}},
            {"BTC": {"score": 2}, "ETH": {"score": 1}},
            ["BTC", "ETH"],
            1000.0,
            effective_trade_amount=100.0,
            effective_max_open_trades=3,
            can_open_positions=True,
            block_reason="",
        )

    assert recorder.executed == []
    assert "already in portfolio or open position" in caplog.text


def test_max_open_trades_reached_logs_instead_of_scanning(caplog):
    recorder = _EntryRecorder()
    portfolio = _portfolio()
    portfolio.open_trades["XMR"] = {"amount_coin": 1.0}

    with caplog.at_level(logging.INFO, logger="src.execution_service"):
        open_new_positions(
            _Config(), portfolio, recorder.hooks(),
            {"BTC": {"price": 100.0}}, {"BTC": {"score": 1}}, ["BTC"], 1000.0,
            effective_trade_amount=100.0,
            effective_max_open_trades=1,
            can_open_positions=True,
            block_reason="",
        )

    assert "Maximum number of open trades (1) reached" in caplog.text
    assert recorder.events == []


def test_not_enough_cash_for_the_minimum_trade_logs_a_note(caplog):
    recorder = _EntryRecorder()
    config = _Config(allow_partial_trades=False, min_trade_amount=10.0)
    portfolio = _portfolio(cash=20.0)

    with caplog.at_level(logging.INFO, logger="src.execution_service"):
        open_new_positions(
            config, portfolio, recorder.hooks(),
            {"BTC": {"price": 100.0}}, {"BTC": {"score": 1}}, ["BTC"], 1000.0,
            effective_trade_amount=50.0,
            effective_max_open_trades=3,
            can_open_positions=True,
            block_reason="",
        )

    assert "Insufficient available funds (20.00 EUR) for minimum trade (50.00 EUR)" in caplog.text
    assert recorder.events == []


def test_candidate_without_cash_records_the_blocked_reason(caplog):
    recorder = _EntryRecorder(position_size=5000.0)

    with caplog.at_level(logging.WARNING, logger="src.execution_service"):
        open_new_positions(
            *_entry_args(recorder, ["BTC"], {"BTC": {"price": 100.0}}),
            effective_trade_amount=100.0,
            effective_max_open_trades=3,
            can_open_positions=True,
            block_reason="",
        )

    assert "Insufficient available funds (1000.00 EUR) for a trade of 5000.00 EUR" in caplog.text
    assert recorder.executed == []
    assert recorder.blocked[0]["reason"].startswith("insufficient_available_funds")
