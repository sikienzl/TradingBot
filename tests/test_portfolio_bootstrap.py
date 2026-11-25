"""Unit tests for the portfolio bootstrap collaborator (layer 1).

Expectations are derived from the moved bodies in
``src/portfolio_bootstrap.py``: the dispatcher, the Kraken simulation
bootstrap, the simulated/offline seeding, the dry-run restore and the live
balance refresh.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import ccxt
import pytest

from src.portfolio import Portfolio
from src.portfolio_bootstrap import (
    PortfolioBootstrapContext,
    PortfolioBootstrapHooks,
    currency_aliases,
    extract_cash_and_holdings,
    initialize_dry_run_portfolio,
    initialize_simulated_portfolio,
    initialize_simulation_portfolio_from_kraken,
    refresh_live_portfolio_balance,
    update_portfolio_balance,
)


class FakeExchange:
    def __init__(self, payload=None, error=None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[str] = []

    def fetch_balance(self):
        self.calls.append("fetch_balance")
        if self.error is not None:
            raise self.error
        return self.payload


def _config(**overrides):
    base = {
        "simulation_initial_balance_from_kraken": False,
        "simulate_data": True,
        "dry_run": False,
        "base_currency": "EUR",
        "api_key": "test-key",
        "api_secret": "test-secret",
        "exchange_timeout_ms": 5000,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _portfolio(tmp_path, base_currency="EUR"):
    portfolio = Portfolio(base_currency)
    portfolio.state_file = str(tmp_path / "portfolio.json")
    return portfolio


def _hooks(**overrides):
    base = {
        "public_paper_mode": lambda: False,
        "recover_paper_trade": lambda: None,
        "set_iteration": lambda value: None,
    }
    base.update(overrides)
    return PortfolioBootstrapHooks(**base)


def _context(config, portfolio, exchange=None):
    return PortfolioBootstrapContext(
        config=config, portfolio=portfolio, exchange=exchange)


# --- currency helpers ---------------------------------------------------------

def test_currency_aliases_base_and_prefixed_forms():
    assert currency_aliases("eur") == ["EUR", "ZEUR", "XEUR"]


def test_currency_aliases_btc_adds_exchange_symbols_without_duplicates():
    assert currency_aliases("BTC") == ["BTC", "ZBTC", "XBTC", "XBT", "XXBT"]


def test_extract_cash_and_holdings_prefers_first_present_alias():
    cash, holdings = extract_cash_and_holdings(
        {"ZEUR": 120.5, "XBT": 1.5, "BTC": 0.0, "LUNA": -2.0, "ADA": 4.0}, "EUR")
    assert cash == 120.5
    assert holdings == {"XBT": 1.5, "ADA": 4.0}


def test_extract_cash_and_holdings_btc_base_uses_xbt_alias():
    cash, holdings = extract_cash_and_holdings(
        {"XBT": 1.25, "EUR": 10.0, "ETH": 2.0}, "BTC")
    assert cash == 1.25
    assert holdings == {"EUR": 10.0, "ETH": 2.0}


def test_extract_cash_and_holdings_without_base_balance_keeps_cash_zero():
    cash, holdings = extract_cash_and_holdings({"ADA": 1.0}, "EUR")
    assert cash == 0.0
    assert holdings == {"ADA": 1.0}


# --- dispatcher ---------------------------------------------------------------

def test_update_portfolio_balance_dispatches_to_kraken_simulation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "src.portfolio_bootstrap.initialize_simulation_portfolio_from_kraken",
        lambda context, hooks, *, log=None: calls.append("kraken"))
    context = _context(_config(simulation_initial_balance_from_kraken=True),
                       Portfolio("EUR"))
    update_portfolio_balance(context, _hooks())
    assert calls == ["kraken"]


def test_update_portfolio_balance_dispatches_to_simulated_without_exchange(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "src.portfolio_bootstrap.initialize_simulated_portfolio",
        lambda context, *, log=None: calls.append("simulated"))
    update_portfolio_balance(
        _context(_config(), Portfolio("EUR"), None), _hooks())
    assert calls == ["simulated"]


def test_update_portfolio_balance_dispatches_to_dry_run(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "src.portfolio_bootstrap.initialize_dry_run_portfolio",
        lambda context, *, log=None: calls.append("dry_run"))
    update_portfolio_balance(
        _context(_config(simulate_data=False, dry_run=True), Portfolio("EUR"),
                 FakeExchange()), _hooks())
    assert calls == ["dry_run"]


def test_update_portfolio_balance_dispatches_to_live_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "src.portfolio_bootstrap.refresh_live_portfolio_balance",
        lambda context, *, log=None: calls.append("live"))
    update_portfolio_balance(
        _context(_config(simulate_data=False), Portfolio("EUR"), FakeExchange()),
        _hooks())
    assert calls == ["live"]


# --- Kraken simulation bootstrap ---------------------------------------------

def test_kraken_bootstrap_skips_when_already_initialized(tmp_path, caplog):
    portfolio = _portfolio(tmp_path)
    portfolio.last_update = "seeded"
    exchange = FakeExchange({"free": {"EUR": 5}})
    with caplog.at_level(logging.INFO):
        initialize_simulation_portfolio_from_kraken(
            _context(_config(simulation_initial_balance_from_kraken=True),
                     portfolio, exchange), _hooks())
    assert portfolio.cash == 0.0
    assert exchange.calls == []
    assert caplog.records == []


def test_kraken_bootstrap_fetches_once_and_persists(tmp_path, monkeypatch, caplog):
    exchange = FakeExchange({"free": {"EUR": 19.25, "BTC": 3}})
    monkeypatch.setattr(ccxt, "kraken", lambda config: exchange)
    portfolio = _portfolio(tmp_path)
    portfolio.strict_persistence = True
    config = _config(simulation_initial_balance_from_kraken=True)
    with caplog.at_level(logging.INFO):
        initialize_simulation_portfolio_from_kraken(
            _context(config, portfolio, exchange), _hooks())
    assert portfolio.cash == 19.25
    assert portfolio.initial_portfolio_value == 19.25
    assert portfolio.last_update is not None
    assert exchange.calls == ["fetch_balance"]
    assert "Simulation initialized once from Kraken free EUR balance: 19.25 EUR" in caplog.text
    saved = json.loads((tmp_path / "portfolio.json").read_text())
    assert saved["cash"] == 19.25
    assert saved["base_currency"] == "EUR"


def test_kraken_bootstrap_requires_read_only_credentials(tmp_path, monkeypatch):
    def factory(config):  # pragma: no cover - must never run
        raise AssertionError("exchange client must not be created")

    monkeypatch.setattr(ccxt, "kraken", factory)
    portfolio = _portfolio(tmp_path)
    config = _config(simulation_initial_balance_from_kraken=True,
                     api_key="", api_secret="")
    with pytest.raises(RuntimeError,
                       match="Kraken read-only API credentials are required"):
        initialize_simulation_portfolio_from_kraken(
            _context(config, portfolio, None), _hooks())
    assert portfolio.last_update is None


def test_kraken_bootstrap_reuses_persisted_state_and_bumps_iteration(
        tmp_path, caplog):
    (tmp_path / "portfolio.json").write_text(json.dumps({
        "base_currency": "EUR", "cash": 11.5, "holdings": {},
        "open_trades": {}, "simulation_iteration": 42,
        "initial_portfolio_value": 19.25,
    }))
    portfolio = _portfolio(tmp_path)
    portfolio.strict_persistence = True
    exchange = FakeExchange({"free": {"EUR": 99}})
    iterations = []
    hooks = _hooks(set_iteration=iterations.append)
    with caplog.at_level(logging.WARNING):
        initialize_simulation_portfolio_from_kraken(
            _context(_config(simulation_initial_balance_from_kraken=True),
                     portfolio, exchange), hooks)
    assert portfolio.cash == 11.5
    assert iterations == [43]
    assert exchange.calls == []
    assert "Simulation restarted: restored the last saved portfolio" in caplog.text


def test_kraken_bootstrap_recovers_public_paper_trade_before_restore(tmp_path):
    (tmp_path / "portfolio.json").write_text(json.dumps({
        "base_currency": "EUR", "cash": 3.0, "holdings": {},
        "open_trades": {}, "simulation_iteration": 1,
    }))
    portfolio = _portfolio(tmp_path)
    portfolio.strict_persistence = True
    recovered = []
    hooks = _hooks(public_paper_mode=lambda: True,
                   recover_paper_trade=lambda: recovered.append(None))
    initialize_simulation_portfolio_from_kraken(
        _context(_config(simulation_initial_balance_from_kraken=True,
                         simulate_data=False), portfolio, None), hooks)
    assert recovered == [None]
    assert portfolio.cash == 3.0


def test_kraken_bootstrap_refuses_public_market_data_without_portfolio(tmp_path):
    portfolio = _portfolio(tmp_path)
    with pytest.raises(RuntimeError,
                       match="Initialize the paper portfolio separately"):
        initialize_simulation_portfolio_from_kraken(
            _context(_config(simulation_initial_balance_from_kraken=True,
                             simulate_data=False), portfolio, None), _hooks())


def test_kraken_bootstrap_fails_closed_on_corrupt_state(tmp_path, monkeypatch):
    (tmp_path / "portfolio.json").write_text("{")
    portfolio = _portfolio(tmp_path)
    portfolio.strict_persistence = True
    exchange = FakeExchange({"free": {"EUR": 5}})
    monkeypatch.setattr(ccxt, "kraken", lambda config: exchange)
    with pytest.raises(RuntimeError, match="Cannot restore simulation portfolio"):
        initialize_simulation_portfolio_from_kraken(
            _context(_config(simulation_initial_balance_from_kraken=True),
                     portfolio, exchange), _hooks())
    assert exchange.calls == []


def test_kraken_bootstrap_fails_closed_without_strict_persistence(tmp_path, monkeypatch):
    (tmp_path / "portfolio.json").write_text("{")
    portfolio = _portfolio(tmp_path)
    exchange = FakeExchange({"free": {"EUR": 5}})
    monkeypatch.setattr(ccxt, "kraken", lambda config: exchange)
    with pytest.raises(RuntimeError, match="Cannot restore existing simulation portfolio"):
        initialize_simulation_portfolio_from_kraken(
            _context(_config(simulation_initial_balance_from_kraken=True),
                     portfolio, exchange), _hooks())
    assert exchange.calls == []
    assert portfolio.last_update is None


def test_kraken_bootstrap_fails_closed_on_invalid_persisted_balances(tmp_path, monkeypatch):
    (tmp_path / "portfolio.json").write_text(json.dumps({
        "base_currency": "EUR", "cash": float("nan"), "holdings": {},
        "open_trades": {}, "simulation_iteration": 0,
    }))
    portfolio = _portfolio(tmp_path)
    portfolio.strict_persistence = True
    exchange = FakeExchange({"free": {"EUR": 5}})
    monkeypatch.setattr(ccxt, "kraken", lambda config: exchange)
    with pytest.raises(RuntimeError, match="Cannot restore simulation portfolio"):
        initialize_simulation_portfolio_from_kraken(
            _context(_config(simulation_initial_balance_from_kraken=True),
                     portfolio, exchange), _hooks())
    assert exchange.calls == []


@pytest.mark.parametrize("payload, message", [
    ({"free": {}}, "Could not obtain free EUR balance from Kraken"),
    ({"free": {"EUR": None}}, "Could not obtain free EUR balance from Kraken"),
    ({"free": {"EUR": "NaN"}}, "Kraken returned an invalid free EUR balance"),
    ({"free": {"EUR": -1}}, "Kraken returned an invalid free EUR balance"),
])
def test_kraken_bootstrap_rejects_invalid_balances(tmp_path, monkeypatch, caplog,
                                                   payload, message):
    exchange = FakeExchange(payload)
    monkeypatch.setattr(ccxt, "kraken", lambda config: exchange)
    portfolio = _portfolio(tmp_path)
    portfolio.strict_persistence = True
    with pytest.raises((RuntimeError, ValueError), match=message):
        initialize_simulation_portfolio_from_kraken(
            _context(_config(simulation_initial_balance_from_kraken=True),
                     portfolio, exchange), _hooks())
    assert portfolio.cash == 0.0
    assert portfolio.last_update is None
    assert not (tmp_path / "portfolio.json").exists()
    assert caplog.records == []


def test_kraken_bootstrap_maps_exchange_errors_to_runtime_error(tmp_path, monkeypatch):
    exchange = FakeExchange(error=ccxt.NetworkError("offline"))
    monkeypatch.setattr(ccxt, "kraken", lambda config: exchange)
    portfolio = _portfolio(tmp_path)
    with pytest.raises(RuntimeError, match="Could not obtain free EUR balance from Kraken"):
        initialize_simulation_portfolio_from_kraken(
            _context(_config(simulation_initial_balance_from_kraken=True),
                     portfolio, exchange), _hooks())
    assert not (tmp_path / "portfolio.json").exists()


def test_kraken_bootstrap_accepts_alias_and_zero_balance(tmp_path, monkeypatch):
    exchange = FakeExchange({"free": {"ZEUR": 0.0}})
    monkeypatch.setattr(ccxt, "kraken", lambda config: exchange)
    portfolio = _portfolio(tmp_path)
    initialize_simulation_portfolio_from_kraken(
        _context(_config(simulation_initial_balance_from_kraken=True),
                 portfolio, exchange), _hooks())
    assert portfolio.cash == 0.0
    assert portfolio.initial_portfolio_value == 0.0
    assert json.loads((tmp_path / "portfolio.json").read_text())["cash"] == 0


# --- simulated / offline seeding ---------------------------------------------

def test_simulated_seed_sets_default_capital(tmp_path, caplog):
    portfolio = _portfolio(tmp_path)
    with caplog.at_level(logging.INFO):
        initialize_simulated_portfolio(_context(_config(), portfolio), log=logging.getLogger())
    assert portfolio.cash == 1000.0
    assert portfolio.initial_portfolio_value == 1000.0
    assert portfolio.last_update is not None


def test_simulated_seed_logs_capital_in_base_currency(tmp_path, caplog):
    portfolio = _portfolio(tmp_path, "USD")
    with caplog.at_level(logging.INFO):
        initialize_simulated_portfolio(
            _context(_config(base_currency="USD"), portfolio))
    assert "Simulated starting capital: 1000.00 USD" in caplog.text


def test_simulated_seed_restores_persisted_state(tmp_path):
    (tmp_path / "portfolio.json").write_text(json.dumps({
        "base_currency": "EUR", "cash": 250.0, "holdings": {"BTC": 0.5},
        "open_trades": {}, "simulation_iteration": 3,
    }))
    portfolio = _portfolio(tmp_path)
    initialize_simulated_portfolio(_context(_config(), portfolio))
    assert portfolio.cash == 250.0
    assert portfolio.holdings == {"BTC": 0.5}
    assert portfolio.initial_portfolio_value is None


def test_simulated_seed_keeps_nonzero_cash_without_seeding(tmp_path):
    portfolio = _portfolio(tmp_path)
    portfolio.cash = 25.0
    initialize_simulated_portfolio(_context(_config(), portfolio))
    assert portfolio.cash == 25.0
    assert portfolio.initial_portfolio_value is None


def test_simulated_seed_keeps_existing_holdings(tmp_path):
    portfolio = _portfolio(tmp_path)
    portfolio.holdings["BTC"] = 1.0
    initialize_simulated_portfolio(_context(_config(), portfolio))
    assert portfolio.cash == 0.0
    assert portfolio.holdings == {"BTC": 1.0}


def test_simulated_seed_keeps_preinitialized_empty_portfolio(tmp_path):
    portfolio = _portfolio(tmp_path)
    portfolio.initial_portfolio_value = 0.0
    initialize_simulated_portfolio(_context(_config(), portfolio))
    assert portfolio.cash == 0.0
    assert portfolio.initial_portfolio_value == 0.0


def test_simulated_seed_runs_without_exchange(tmp_path, caplog):
    portfolio = _portfolio(tmp_path)
    initialize_simulated_portfolio(
        _context(_config(simulate_data=False), portfolio, None))
    assert portfolio.cash == 1000.0


# --- dry-run bootstrap --------------------------------------------------------

def test_dry_run_skips_when_already_initialized(tmp_path):
    portfolio = _portfolio(tmp_path)
    portfolio.last_update = "seeded"
    exchange = FakeExchange({"free": {"EUR": 5}})
    initialize_dry_run_portfolio(
        _context(_config(simulate_data=False, dry_run=True), portfolio, exchange))
    assert exchange.calls == []


def test_dry_run_restores_persisted_state(tmp_path):
    (tmp_path / "portfolio.json").write_text(json.dumps({
        "base_currency": "EUR", "cash": 7.5, "holdings": {},
        "open_trades": {}, "simulation_iteration": 0,
    }))
    portfolio = _portfolio(tmp_path)
    exchange = FakeExchange({"free": {"EUR": 99}})
    initialize_dry_run_portfolio(
        _context(_config(simulate_data=False, dry_run=True), portfolio, exchange))
    assert portfolio.cash == 7.5
    assert exchange.calls == []
    assert portfolio.last_update is not None


def test_dry_run_seeds_from_exchange(tmp_path, caplog):
    portfolio = _portfolio(tmp_path)
    exchange = FakeExchange({"free": {"EUR": 50.0, "BTC": 0.5, "LUNA": 0.0}})
    with caplog.at_level(logging.INFO):
        initialize_dry_run_portfolio(
            _context(_config(simulate_data=False, dry_run=True), portfolio, exchange))
    assert portfolio.cash == 50.0
    assert portfolio.holdings == {"BTC": 0.5}
    assert portfolio.initial_portfolio_value is None
    assert portfolio.last_update is not None
    assert "Portfolio initialized from exchange (dry-run mode)" in caplog.text


def test_dry_run_seeds_initial_value_when_no_holdings(tmp_path):
    portfolio = _portfolio(tmp_path)
    exchange = FakeExchange({"free": {"EUR": 42.0}})
    initialize_dry_run_portfolio(
        _context(_config(simulate_data=False, dry_run=True), portfolio, exchange))
    assert portfolio.initial_portfolio_value == 42.0


def test_dry_run_exchange_error_only_warns(tmp_path, caplog):
    portfolio = _portfolio(tmp_path)
    exchange = FakeExchange(error=ccxt.BaseError("boom"))
    with caplog.at_level(logging.WARNING):
        initialize_dry_run_portfolio(
            _context(_config(simulate_data=False, dry_run=True), portfolio, exchange))
    assert portfolio.cash == 0.0
    assert portfolio.last_update is None
    assert "Failed to load initial balance from exchange in dry-run mode: boom" in caplog.text


# --- live balance refresh -----------------------------------------------------

def test_live_refresh_updates_cash_and_holdings(tmp_path, caplog):
    portfolio = _portfolio(tmp_path)
    exchange = FakeExchange({"free": {"ZEUR": 120.5, "XBT": 1.5, "ADA": 4.0}})
    with caplog.at_level(logging.INFO):
        refresh_live_portfolio_balance(
            _context(_config(simulate_data=False), portfolio, exchange))
    assert portfolio.cash == 120.5
    assert portfolio.holdings == {"XBT": 1.5, "ADA": 4.0}
    assert portfolio.last_update is not None
    assert "Portfolio balance updated" in caplog.text


@pytest.mark.parametrize("error", [
    ccxt.NetworkError("down"), ccxt.ExchangeError("rejected"),
    ccxt.BaseError("other")])
def test_live_refresh_logs_errors_without_touching_portfolio(tmp_path, caplog, error):
    portfolio = _portfolio(tmp_path)
    portfolio.cash = 33.0
    exchange = FakeExchange(error=error)
    with caplog.at_level(logging.ERROR):
        refresh_live_portfolio_balance(
            _context(_config(simulate_data=False), portfolio, exchange))
    assert portfolio.cash == 33.0
    assert portfolio.last_update is None
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "ERROR"
    assert "Error fetching/updating account balance" in caplog.text
