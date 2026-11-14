import json
import logging

import ccxt
import pytest

from src.trading_bot import BotConfig, CryptoTradingBot, Portfolio


def make_bot(monkeypatch, tmp_path, *, key="test-key", secret="test-secret"):
    monkeypatch.setenv("SIMULATE_DATA", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("SIMULATION_INITIAL_BALANCE_FROM_KRAKEN", "true")
    monkeypatch.setenv("KRAKEN_API_KEY", key)
    monkeypatch.setenv("KRAKEN_API_SECRET", secret)
    monkeypatch.setenv("USE_TABULAR_MODEL", "false")
    monkeypatch.setenv("USE_ML_MODEL", "false")
    monkeypatch.setenv("ANALYTICS_DB_ENABLED", "false")
    bot = CryptoTradingBot(BotConfig())
    bot.portfolio.state_file = str(tmp_path / "portfolio.json")
    return bot


def test_initial_balance_once_then_restore_without_credentials(monkeypatch, tmp_path, caplog):
    calls = []

    class Exchange:
        def fetch_balance(self):
            calls.append("fetch")
            return {"free": {"EUR": 19.25, "BTC": 3}}

    monkeypatch.setattr(ccxt, "kraken", lambda config: Exchange())
    bot = make_bot(monkeypatch, tmp_path)
    bot._update_portfolio_balance()
    assert bot.portfolio.cash == 19.25
    assert bot.portfolio.holdings == {}
    assert calls == ["fetch"]
    bot._update_portfolio_balance()
    assert calls == ["fetch"]
    bot.portfolio.cash = 11.5
    bot.portfolio.simulation_iteration = 42
    bot.portfolio.save_state()

    restarted = make_bot(monkeypatch, tmp_path, key="", secret="")
    with caplog.at_level(logging.WARNING):
        restarted._update_portfolio_balance()
    assert restarted.portfolio.cash == 11.5
    assert restarted.iteration == 43
    assert "Simulation restarted" in caplog.text
    assert calls == ["fetch"]


def test_zero_balance_does_not_repeat_initialization(monkeypatch, tmp_path):
    calls = []

    class Exchange:
        def fetch_balance(self):
            calls.append(1)
            return {"free": {"EUR": 0.0}}

    monkeypatch.setattr(ccxt, "kraken", lambda config: Exchange())
    bot = make_bot(monkeypatch, tmp_path)
    bot._update_portfolio_balance()
    bot._update_portfolio_balance()
    assert calls == [1]
    assert json.loads((tmp_path / "portfolio.json").read_text())["cash"] == 0


def test_simulated_trade_is_restored_after_restart(monkeypatch, tmp_path):
    class Exchange:
        def fetch_balance(self):
            return {"free": {"EUR": 19.52}}

    monkeypatch.setattr(ccxt, "kraken", lambda config: Exchange())
    bot = make_bot(monkeypatch, tmp_path)
    bot._update_portfolio_balance()
    monkeypatch.setattr(bot, "_append_trade_journal", lambda row: None)
    assert bot._execute_trade("ETH", "buy", 10.0, 5.0)

    restarted = make_bot(monkeypatch, tmp_path, key="", secret="")
    restarted._update_portfolio_balance()
    assert restarted.portfolio.cash == pytest.approx(14.52)
    assert restarted.portfolio.holdings["ETH"] == pytest.approx(0.5)
    assert restarted.portfolio.open_trades["ETH"]["amount_coin"] == pytest.approx(0.5)


@pytest.mark.parametrize("balance", [{"free": {}}, {"free": {"EUR": -1}}, {"free": {"EUR": "NaN"}}])
def test_invalid_balance_never_initializes(monkeypatch, tmp_path, balance):
    class Exchange:
        def fetch_balance(self):
            return balance

    monkeypatch.setattr(ccxt, "kraken", lambda config: Exchange())
    bot = make_bot(monkeypatch, tmp_path)
    with pytest.raises((ValueError, RuntimeError)):
        bot._update_portfolio_balance()
    assert not (tmp_path / "portfolio.json").exists()


def test_exchange_error_does_not_fall_back_to_1000(monkeypatch, tmp_path):
    class Exchange:
        def fetch_balance(self):
            raise ccxt.NetworkError("offline")

    monkeypatch.setattr(ccxt, "kraken", lambda config: Exchange())
    bot = make_bot(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="Could not obtain"):
        bot._update_portfolio_balance()
    assert not (tmp_path / "portfolio.json").exists()


def test_corrupt_state_does_not_fetch_balance(monkeypatch, tmp_path):
    (tmp_path / "portfolio.json").write_text("{")
    monkeypatch.setattr(ccxt, "kraken", lambda config: pytest.fail("unexpected API access"))
    bot = make_bot(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="Cannot restore"):
        bot._update_portfolio_balance()


def test_persistence_failure_preserves_previous_file(monkeypatch, tmp_path):
    state = tmp_path / "portfolio.json"
    state.write_text("original")
    portfolio = Portfolio()
    portfolio.strict_persistence = True
    monkeypatch.setattr("src.trading_bot.os.replace", lambda *args: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(RuntimeError, match="Cannot persist"):
        portfolio.save_state(str(state))
    assert state.read_text() == "original"
    assert list(tmp_path.iterdir()) == [state]


@pytest.mark.parametrize("name,value", [
    ("DRY_RUN", "false"),
    ("SIMULATE_DATA", "false"),
    ("BASE_CURRENCY", "USD"),
    ("EXCHANGE_NAME", "other"),
])
def test_initialization_option_requires_safe_mode(monkeypatch, tmp_path, name, value):
    make_bot(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="requires DRY_RUN"):
        CryptoTradingBot(BotConfig())
