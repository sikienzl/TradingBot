"""Unit tests for the layer-1 dry-run execution service.

The service receives the config, the portfolio, the performance counters and
the bot-owned callbacks explicitly, so these tests drive the real code paths
with stubs and never import :mod:`src.trading_bot`.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pytest

from src.dry_run_execution import (
    DryRunHooks,
    apply_dry_run_sell_to_holdings,
    dry_run_buy,
    dry_run_sell,
    dry_run_sell_gross_net_pnl,
    dry_run_sell_proceeds,
    execute_dry_run_trade,
    plan_dry_run_fill,
    record_dry_run_sell_close,
    resolve_dry_run_sell_entry,
    update_open_trades_after_dry_run_sell,
)
from src.portfolio import Portfolio


@dataclass
class _Config:
    base_currency: str = "EUR"
    dry_run_costs_enabled: bool = True
    dry_run_slippage_rate: float = 0.001
    dry_run_fee_rate: float = 0.001


def _performance() -> dict[str, Any]:
    return {
        "buys": 0,
        "sells": 0,
        "closed_trades": 0,
        "realized_pnl_base": 0.0,
        "wins": 0,
        "losses": 0,
        "break_even": 0,
        "by_source": defaultdict(
            lambda: {"buys": 0, "sells": 0, "pnl": 0.0}),
    }


def _portfolio(cash: float = 1000.0) -> Portfolio:
    portfolio = Portfolio("EUR")
    portfolio.cash = cash
    portfolio.save_state = lambda *args, **kwargs: True  # type: ignore[method-assign]
    return portfolio


class _Recorder:
    """Records every hook invocation the service performs, in order."""

    def __init__(
        self,
        *,
        public_paper: bool = False,
        close_result: tuple[float, float, float] = (5.0, 10.0, 60.0),
        excursion: tuple[float, float] = (1.5, -0.5),
    ) -> None:
        self.public_paper = public_paper
        self.close_result = close_result
        self.excursion = excursion
        self.events: list[Any] = []
        self.journal_rows: list[dict[str, Any]] = []
        self.paper_rows: list[dict[str, Any]] = []

    def record_close_performance(
        self, entry_trade, sell_price, sell_amount, **kwargs
    ) -> tuple[float, float, float]:
        self.events.append(
            ("close_performance", sell_price, sell_amount, kwargs))
        return self.close_result

    def trade_excursion_metrics(self, entry_trade) -> tuple[float, float]:
        self.events.append(("excursion",))
        return self.excursion

    def entry_cost_basis(self, entry_trade, buy_price, buy_amount_coin) -> float:
        self.events.append(
            ("cost_basis", buy_price, buy_amount_coin))
        return float(entry_trade.get("amount_base",
                                     buy_price * buy_amount_coin))

    def append_trade_journal(self, row) -> dict[str, Any]:
        self.events.append(("journal", row["action"], row["price"]))
        self.journal_rows.append(row)
        return row

    def commit_paper_trade(self, row) -> dict[str, Any]:
        self.events.append(("paper_commit", row["action"], row["price"]))
        self.paper_rows.append(row)
        return row

    def public_paper_mode(self) -> bool:
        self.events.append(("paper_mode",))
        return self.public_paper

    def register_buy_timestamp(self) -> None:
        self.events.append(("buy_ts",))

    def register_sell_timestamp(self, coin) -> None:
        self.events.append(("sell_ts", coin))

    def now(self) -> datetime:
        self.events.append(("now",))
        return datetime(2024, 5, 4, 12, 0, 0)

    def hooks(self) -> DryRunHooks:
        return DryRunHooks(
            record_close_performance=self.record_close_performance,
            trade_excursion_metrics=self.trade_excursion_metrics,
            entry_cost_basis=self.entry_cost_basis,
            append_trade_journal=self.append_trade_journal,
            commit_paper_trade=self.commit_paper_trade,
            public_paper_mode=self.public_paper_mode,
            register_buy_timestamp=self.register_buy_timestamp,
            register_sell_timestamp=self.register_sell_timestamp,
            now=self.now,
        )


def _capture(
    monkeypatch: pytest.MonkeyPatch, logger_name: str = "src.dry_run_execution"
) -> list[tuple[int, str]]:
    """Collect the records the service logs, formatted exactly as emitted."""
    records: list[tuple[int, str]] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append((record.levelno, record.getMessage()))

    handler = _Handler()
    target = logging.getLogger(logger_name)
    monkeypatch.setattr(target, "handlers", [handler])
    monkeypatch.setattr(target, "level", logging.DEBUG)
    return records


# ---------------------------------------------------------------------------
# plan_dry_run_fill
# ---------------------------------------------------------------------------
def test_plan_fill_rejects_an_invalid_request_when_costs_are_tracked(
    monkeypatch: pytest.MonkeyPatch,
):
    records = _capture(monkeypatch)
    assert plan_dry_run_fill(
        _Config(), "BTC", "hold", float("nan"), 0.0, True, False) is None
    assert records == [(logging.WARNING, "Invalid DRY-RUN trade request for BTC")]


def test_plan_fill_ignores_an_invalid_request_in_gross_legacy_mode():
    plan = plan_dry_run_fill(_Config(), "BTC", "hold", -1.0, 5.0, False, False)
    assert plan == (-1.0, -5.0)


def test_plan_fill_applies_slippage_and_fee_to_a_costed_buy():
    config = _Config(dry_run_slippage_rate=0.02, dry_run_fee_rate=0.01)
    fill_price, amount_coin = plan_dry_run_fill(
        config, "BTC", "buy", 100.0, 100.0, True, False)
    assert fill_price == pytest.approx(102.0)
    assert amount_coin == pytest.approx(100.0 / (102.0 * 1.01))


def test_plan_fill_applies_slippage_only_to_a_costed_sell():
    config = _Config(dry_run_slippage_rate=0.02, dry_run_fee_rate=0.01)
    assert plan_dry_run_fill(
        config, "BTC", "sell", 100.0, 100.0, True, False) == (98.0, 1.0)


def test_plan_fill_ignores_costs_when_disabled():
    config = _Config(dry_run_slippage_rate=0.02, dry_run_fee_rate=0.01)
    assert plan_dry_run_fill(
        config, "BTC", "buy", 100.0, 50.0, False, False) == (100.0, 0.5)


def test_plan_fill_rejects_a_broken_calculation(monkeypatch: pytest.MonkeyPatch):
    config = _Config(dry_run_slippage_rate=-2.0)
    records = _capture(monkeypatch)
    assert plan_dry_run_fill(config, "BTC", "buy", 100.0, 100.0, True, False) is None
    assert records == [(logging.WARNING, "DRY-RUN trade calculation invalid for BTC")]


# ---------------------------------------------------------------------------
# dry_run_buy
# ---------------------------------------------------------------------------
def test_dry_run_buy_skips_an_existing_position(monkeypatch: pytest.MonkeyPatch):
    config = _Config()
    portfolio = _portfolio()
    portfolio.holdings["BTC"] = 0.5
    records = _capture(monkeypatch)
    performance = _performance()
    recorder = _Recorder()

    assert dry_run_buy(
        config, portfolio, performance, recorder.hooks(),
        "BTC", 100.0, 100.0, 1.0, 100.1, True, "rules", None, "HOLD", "why",
    ) is False
    assert records == [(logging.WARNING, "DRY-RUN buy skipped for existing BTC position")]
    assert portfolio.cash == 1000.0
    assert performance["buys"] == 0
    assert recorder.events == []


def test_dry_run_buy_allows_an_existing_position_in_gross_legacy_mode():
    config = _Config(dry_run_costs_enabled=False)
    portfolio = _portfolio()
    portfolio.holdings["BTC"] = 0.5
    recorder = _Recorder()

    assert dry_run_buy(
        config, portfolio, _performance(), recorder.hooks(),
        "BTC", 100.0, 100.0, 1.0, 100.0, False, "rules", None, "HOLD", "why",
    ) is True
    assert portfolio.holdings["BTC"] == pytest.approx(1.5)


def test_dry_run_buy_requires_enough_cash(monkeypatch: pytest.MonkeyPatch):
    config = _Config()
    portfolio = _portfolio(cash=10.0)
    records = _capture(monkeypatch)

    assert dry_run_buy(
        config, portfolio, _performance(), _Recorder().hooks(),
        "BTC", 100.0, 100.0, 1.0, 100.1, True, "rules", None, "HOLD", "why",
    ) is False
    assert records == [(
        logging.WARNING,
        "Insufficient cash for DRY-RUN buy of BTC: 10.00 < 100.00 EUR",
    )]


def test_dry_run_buy_accounts_cash_holdings_and_journal():
    config = _Config(dry_run_slippage_rate=0.02, dry_run_fee_rate=0.01)
    portfolio = _portfolio()
    performance = _performance()
    recorder = _Recorder()

    assert dry_run_buy(
        config, portfolio, performance, recorder.hooks(),
        "BTC", 100.0, 100.0, 0.9708737864077669, 102.0, True,
        "ai_copilot", 0.8, "HOLD (Up-Trend)", "momentum",
    ) is True
    assert portfolio.cash == pytest.approx(900.0)
    assert portfolio.holdings["BTC"] == pytest.approx(0.9708737864077669)
    trade = portfolio.open_trades["BTC"]
    assert trade["buy_price"] == pytest.approx(102.0)
    assert trade["execution_cost_mode"] == "net_costs"
    assert trade["entry_reference_price"] == 100.0
    assert performance["buys"] == 1
    assert performance["by_source"]["ai_copilot"]["buys"] == 1
    assert recorder.events == [
        ("buy_ts",), ("paper_mode",), ("journal", "buy", pytest.approx(102.0))]
    row = recorder.journal_rows[0]
    assert row["action"] == "buy"
    assert row["price"] == pytest.approx(102.0)
    assert row["execution_cost_mode"] == "net_costs"
    assert row["fee_base"] == pytest.approx(0.9708737864077669 * 102.0 * 0.01)
    assert row["slippage_base"] == pytest.approx(0.9708737864077669 * 2.0)


def test_dry_run_buy_records_gross_legacy_cost_mode_when_disabled():
    config = _Config(dry_run_costs_enabled=False)
    portfolio = _portfolio()
    recorder = _Recorder()

    assert dry_run_buy(
        config, portfolio, _performance(), recorder.hooks(),
        "BTC", 100.0, 100.0, 1.0, 100.0, False, "rules", None, "HOLD", "why",
    ) is True
    assert "execution_cost_mode" not in portfolio.open_trades["BTC"]
    row = recorder.journal_rows[0]
    assert row["execution_cost_mode"] == "gross_legacy"
    assert row["fee_base"] == 0.0
    assert row["slippage_base"] == 0.0


def test_dry_run_buy_commits_a_paper_trade_instead_of_journaling():
    portfolio = _portfolio()
    recorder = _Recorder(public_paper=True)

    assert dry_run_buy(
        _Config(), portfolio, _performance(), recorder.hooks(),
        "BTC", 100.0, 100.0, 1.0, 100.1, True, "rules", None, "HOLD", "why",
    ) is True
    assert recorder.journal_rows == []
    assert len(recorder.paper_rows) == 1
    assert recorder.events == [("buy_ts",), ("paper_mode",),
                               ("paper_commit", "buy", 100.1)]


# ---------------------------------------------------------------------------
# resolve_dry_run_sell_entry
# ---------------------------------------------------------------------------
def test_resolve_sell_entry_needs_a_tracked_entry_when_costed(
    monkeypatch: pytest.MonkeyPatch,
):
    records = _capture(monkeypatch)
    basis: Callable[[dict, float, float], float] = lambda *args: 1.0
    assert resolve_dry_run_sell_entry("BTC", {}, 1.0, 1.0, True, False, basis) is None
    assert records == [(
        logging.WARNING,
        "DRY-RUN sell skipped for BTC: no tracked entry cost basis",
    )]


def test_resolve_sell_entry_caps_the_sell_to_the_entry_quantity():
    entry = {"amount_coin": 0.4, "buy_price": 100.0, "amount_base": 40.0}
    basis: Callable[[dict, float, float], float] = lambda *args: 40.0
    mode, value, sell_amount = resolve_dry_run_sell_entry(
        "BTC", entry, 1.0, 1.0, True, False, basis)
    assert mode == "gross_legacy"
    assert value == 40.0
    assert sell_amount == 0.4


def test_resolve_sell_entry_reports_an_invalid_cost_basis(
    monkeypatch: pytest.MonkeyPatch,
):
    records = _capture(monkeypatch)
    basis: Callable[[dict, float, float], float] = lambda *args: 40.0
    assert resolve_dry_run_sell_entry(
        "BTC", {"amount_coin": 0.0, "buy_price": 100.0}, 1.0, 1.0, True, False,
        basis) is None
    message = records[0][1]
    assert message.startswith(
        "DRY-RUN sell skipped for BTC: invalid entry cost basis or position: ")
    assert "Invalid entry price, quantity or holdings" in message


def test_resolve_sell_entry_keeps_gross_legacy_entry_unvalidated():
    mode, value, sell_amount = resolve_dry_run_sell_entry(
        "BTC", {}, 1.0, 0.5, False, False, lambda *args: 1.0)
    assert (mode, value, sell_amount) == ("gross_legacy", None, 0.5)


# ---------------------------------------------------------------------------
# dry_run_sell_proceeds
# ---------------------------------------------------------------------------
def test_sell_proceeds_deducts_the_fee():
    config = _Config(dry_run_fee_rate=0.02)
    proceeds, fee = dry_run_sell_proceeds(config, "BTC", 1.0, 100.0, True, False)
    assert fee == pytest.approx(2.0)
    assert proceeds == pytest.approx(98.0)


def test_sell_proceeds_rejects_a_non_positive_result(
    monkeypatch: pytest.MonkeyPatch,
):
    records = _capture(monkeypatch)
    assert dry_run_sell_proceeds(
        _Config(), "BTC", 1.0, 0.0, True, False) is None
    assert records == [(logging.WARNING, "DRY-RUN proceeds invalid for BTC")]


def test_sell_proceeds_allows_zero_in_gross_legacy_mode():
    assert dry_run_sell_proceeds(
        _Config(), "BTC", 1.0, 0.0, False, False) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# apply_dry_run_sell_to_holdings / update_open_trades_after_dry_run_sell
# ---------------------------------------------------------------------------
def test_sell_reduces_a_partial_holding():
    portfolio = _portfolio()
    portfolio.holdings["BTC"] = 1.0
    apply_dry_run_sell_to_holdings(portfolio, "BTC", 1.0, 0.4)
    assert portfolio.holdings["BTC"] == pytest.approx(0.6)


def test_sell_removes_a_fully_drained_holding():
    portfolio = _portfolio()
    portfolio.holdings["BTC"] = 1.0
    apply_dry_run_sell_to_holdings(portfolio, "BTC", 1.0, 1.0)
    assert "BTC" not in portfolio.holdings


def test_update_open_trades_removes_a_fully_closed_position():
    portfolio = _portfolio()
    portfolio.open_trades["BTC"] = {"amount_coin": 1.0, "amount_base": 100.0}
    update_open_trades_after_dry_run_sell(
        portfolio, lambda: False, datetime.now,
        "BTC", {"amount_base": 100.0}, 0.0, 0.0, 100.0, True, "net_costs", False)
    assert "BTC" not in portfolio.open_trades


def test_update_open_trades_scales_a_partial_sell_with_tracked_basis():
    portfolio = _portfolio()
    portfolio.open_trades["BTC"] = {"amount_coin": 2.0, "amount_base": 200.0}
    update_open_trades_after_dry_run_sell(
        portfolio, lambda: False, datetime.now,
        "BTC", {"amount_base": 200.0}, 1.0, 0.5, 200.0, True, "net_costs", False)
    assert portfolio.open_trades["BTC"]["amount_coin"] == 1.0
    assert portfolio.open_trades["BTC"]["amount_base"] == pytest.approx(100.0)


def test_update_open_trades_marks_a_public_paper_partial_take_profit():
    portfolio = _portfolio()
    portfolio.open_trades["BTC"] = {"amount_coin": 2.0, "amount_base": 200.0}
    update_open_trades_after_dry_run_sell(
        portfolio, lambda: True, lambda: datetime(2024, 5, 4),
        "BTC", {"amount_base": 200.0}, 1.0, 0.5, 200.0, True, "net_costs", True)
    trade = portfolio.open_trades["BTC"]
    assert trade["partial_tp_taken"] is True
    assert trade["partial_tp_timestamp"] == datetime(2024, 5, 4)


def test_update_open_trades_uses_the_legacy_basis_when_costs_are_off():
    portfolio = _portfolio()
    portfolio.open_trades["BTC"] = {"amount_coin": 2.0, "amount_base": 200.0}
    update_open_trades_after_dry_run_sell(
        portfolio, lambda: False, datetime.now,
        "BTC", {"amount_base": 120.0}, 1.0, 0.5, None, False, "gross_legacy",
        False)
    assert portfolio.open_trades["BTC"]["amount_base"] == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# record_dry_run_sell_close / dry_run_sell_gross_net_pnl
# ---------------------------------------------------------------------------
def test_record_sell_close_passes_net_arguments_when_costs_are_tracked():
    seen: list[tuple] = []

    def record_close(entry_trade, sell_price, sell_amount, **kwargs):
        seen.append((entry_trade, sell_price, sell_amount, kwargs))
        return (1.0, 2.0, 3.0)

    result = record_dry_run_sell_close(
        record_close, {"buy_price": 100.0}, 101.0, 0.5, 50.0, 49.0, True,
        "net_costs")
    assert result == (1.0, 2.0, 3.0)
    assert seen[0][3] == {"net_proceeds": 50.0, "entry_basis": 49.0}


def test_record_sell_close_stays_gross_for_a_legacy_entry():
    seen: list[tuple] = []

    def record_close(entry_trade, sell_price, sell_amount, **kwargs):
        seen.append((entry_trade, sell_price, sell_amount, kwargs))
        return (1.0, 2.0, 3.0)

    record_dry_run_sell_close(
        record_close, {"buy_price": 100.0}, 101.0, 0.5, 50.0, 49.0, False,
        "gross_legacy")
    assert seen[0][3] == {}


def test_gross_net_pnl_pair_is_empty_without_a_net_entry():
    assert dry_run_sell_gross_net_pnl({}, 100.0, 1.0, 5.0, "gross_legacy") == ('', '')


def test_gross_net_pnl_pair_reports_both_legs_for_a_net_entry():
    entry = {"entry_reference_price": 90.0}
    gross, net = dry_run_sell_gross_net_pnl(entry, 100.0, 2.0, 15.0, "net_costs")
    assert gross == pytest.approx(20.0)
    assert net == 15.0


def test_gross_net_pnl_pair_tolerates_a_corrupt_reference_price():
    entry = {"entry_reference_price": "n/a"}
    assert dry_run_sell_gross_net_pnl(entry, 100.0, 2.0, 15.0, "net_costs") == ('', '')


# ---------------------------------------------------------------------------
# dry_run_sell
# ---------------------------------------------------------------------------
def _open_portfolio(quantity: float = 1.0, cash: float = 0.0,
                    cost_mode: str = "net_costs") -> Portfolio:
    portfolio = _portfolio(cash=cash)
    portfolio.holdings["BTC"] = quantity
    portfolio.open_trades["BTC"] = {
        "buy_price": 100.0,
        "amount_coin": quantity,
        "amount_base": 100.0 * quantity,
        "signal_source": "rules",
        "signal_confidence": None,
        "recommendation": "HOLD",
        "execution_cost_mode": cost_mode,
        "entry_reference_price": 100.0,
    }
    return portfolio


def test_dry_run_sell_needs_a_tracked_entry(monkeypatch: pytest.MonkeyPatch):
    portfolio = _portfolio()
    portfolio.holdings["BTC"] = 1.0
    records = _capture(monkeypatch)

    assert dry_run_sell(
        _Config(), portfolio, _Recorder().hooks(),
        "BTC", 100.0, 1.0, 99.9, True, False, {}, "take profit", False,
    ) is False
    assert records == [(
        logging.WARNING,
        "DRY-RUN sell skipped for BTC: no tracked entry cost basis",
    )]
    assert portfolio.cash == 1000.0


def test_dry_run_sell_skips_without_holdings(monkeypatch: pytest.MonkeyPatch):
    portfolio = _portfolio()
    portfolio.open_trades["BTC"] = {"amount_coin": 1.0, "buy_price": 100.0,
                                    "amount_base": 100.0}
    records = _capture(monkeypatch)

    assert dry_run_sell(
        _Config(dry_run_costs_enabled=False), portfolio, _Recorder().hooks(),
        "BTC", 100.0, 1.0, 99.9, False, False,
        portfolio.open_trades["BTC"], "exit", False,
    ) is False
    assert records == [(logging.WARNING, "DRY-RUN sell skipped, no holdings for BTC.")]


def test_dry_run_sell_closes_the_position_and_journals():
    config = _Config(dry_run_slippage_rate=0.0, dry_run_fee_rate=0.01)
    portfolio = _open_portfolio(cash=0.0)
    recorder = _Recorder(close_result=(12.0, 12.0, 90.0))

    assert dry_run_sell(
        config, portfolio, recorder.hooks(),
        "BTC", 100.0, 1.0, 100.0, True, True,
        portfolio.open_trades["BTC"], "take profit", False,
    ) is True
    assert portfolio.cash == pytest.approx(99.0)
    assert "BTC" not in portfolio.holdings
    assert "BTC" not in portfolio.open_trades
    assert recorder.events[:4] == [
        ("cost_basis", 100.0, 1.0),
        ("close_performance", 100.0, 1.0,
         {"net_proceeds": pytest.approx(99.0), "entry_basis": 100.0}),
        ("excursion",),
        ("paper_mode",),
    ]
    assert recorder.events[-2:] == [("sell_ts", "BTC"), ("paper_mode",)]
    row = recorder.journal_rows[0]
    assert row["action"] == "sell"
    assert row["pnl_base"] == 12.0
    assert row["mfe_pct"] == 1.5
    assert row["entry_cost_mode"] == "net_costs"
    assert row["fee_base"] == pytest.approx(1.0)
    assert row["gross_pnl_base"] == pytest.approx(0.0)
    assert row["net_pnl_base"] == 12.0


def test_dry_run_sell_scales_a_partial_take_profit():
    config = _Config(dry_run_slippage_rate=0.0, dry_run_fee_rate=0.0)
    portfolio = _open_portfolio(quantity=2.0, cash=0.0)
    recorder = _Recorder(public_paper=True)

    assert dry_run_sell(
        config, portfolio, recorder.hooks(),
        "BTC", 100.0, 1.0, 100.0, True, True,
        portfolio.open_trades["BTC"], "partial take profit", True,
    ) is True
    assert portfolio.holdings["BTC"] == pytest.approx(1.0)
    trade = portfolio.open_trades["BTC"]
    assert trade["amount_coin"] == 1.0
    assert trade["amount_base"] == pytest.approx(100.0)
    assert trade["partial_tp_taken"] is True
    assert trade["partial_tp_timestamp"] == datetime(2024, 5, 4, 12, 0, 0)
    assert recorder.journal_rows == []
    assert len(recorder.paper_rows) == 1


def test_dry_run_sell_keeps_a_legacy_entry_gross():
    config = _Config(dry_run_costs_enabled=False)
    portfolio = _open_portfolio(cost_mode="gross_legacy")
    recorder = _Recorder()

    assert dry_run_sell(
        config, portfolio, recorder.hooks(),
        "BTC", 100.0, 1.0, 100.0, False, False,
        portfolio.open_trades["BTC"], "exit", False,
    ) is True
    assert ("cost_basis", 100.0, 1.0) not in recorder.events
    assert recorder.events[0] == (
        "close_performance", 100.0, 1.0, {})
    row = recorder.journal_rows[0]
    assert row["entry_cost_mode"] == "gross_legacy"
    assert row["gross_pnl_base"] == ''


# ---------------------------------------------------------------------------
# execute_dry_run_trade dispatch
# ---------------------------------------------------------------------------
def test_execute_dry_run_trade_buys_with_costs(monkeypatch: pytest.MonkeyPatch):
    config = _Config(dry_run_slippage_rate=0.0, dry_run_fee_rate=0.0)
    portfolio = _portfolio()
    performance = _performance()
    recorder = _Recorder()
    records = _capture(monkeypatch)

    assert execute_dry_run_trade(
        config, portfolio, performance, recorder.hooks(),
        "BTC", "buy", 100.0, 100.0,
        signal_source="rules", signal_confidence=0.7,
        recommendation="HOLD", reason="entry",
    ) is True
    assert portfolio.cash == pytest.approx(900.0)
    assert performance["buys"] == 1
    assert records == [(
        logging.INFO,
        ("💰 DRY-RUN: BUY 1.00000000 BTC reference=100.00000000 fill=100.00000000 "
         "EUR requested_base=100.00000000 cost_mode=net_costs"),
    )]


def test_execute_dry_run_trade_sells_a_tracked_position(monkeypatch: pytest.MonkeyPatch):
    config = _Config(dry_run_slippage_rate=0.0, dry_run_fee_rate=0.0)
    portfolio = _open_portfolio(cash=0.0)
    recorder = _Recorder()
    records = _capture(monkeypatch)

    assert execute_dry_run_trade(
        config, portfolio, _performance(), recorder.hooks(),
        "BTC", "sell", 100.0, 100.0, reason="exit",
    ) is True
    assert portfolio.cash == pytest.approx(100.0)
    assert "BTC" not in portfolio.open_trades
    assert records[0][1].startswith("💰 DRY-RUN: SELL")


def test_execute_dry_run_trade_stops_when_the_plan_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
):
    records = _capture(monkeypatch)
    assert execute_dry_run_trade(
        _Config(), _portfolio(), _performance(), _Recorder().hooks(),
        "BTC", "hold", float("nan"), 100.0,
    ) is False
    assert records == [(logging.WARNING, "Invalid DRY-RUN trade request for BTC")]


def test_execute_dry_run_trade_reports_success_for_an_unknown_action(
    monkeypatch: pytest.MonkeyPatch,
):
    config = _Config(dry_run_costs_enabled=False)
    portfolio = _portfolio()
    recorder = _Recorder()
    _capture(monkeypatch)

    assert execute_dry_run_trade(
        config, portfolio, _performance(), recorder.hooks(),
        "BTC", "hold", 100.0, 100.0,
    ) is True
    assert recorder.events == []
    assert portfolio.cash == 1000.0
