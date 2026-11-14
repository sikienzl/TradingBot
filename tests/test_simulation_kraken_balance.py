import json
import logging
import sys

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
    ("BASE_CURRENCY", "USD"),
    ("EXCHANGE_NAME", "other"),
])
def test_initialization_option_requires_safe_mode(monkeypatch, tmp_path, name, value):
    make_bot(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="requires DRY_RUN"):
        CryptoTradingBot(BotConfig())


def test_public_paper_restores_state_and_uses_unauthenticated_exchange(monkeypatch, tmp_path):
    initial = make_bot(monkeypatch, tmp_path)
    initial.portfolio.cash = 19.25
    initial.portfolio.save_state()
    options = []

    class Exchange:
        def load_markets(self):
            return {"BTC/EUR": {"quote": "EUR", "active": True, "base": "BTC"}}

        def fetch_balance(self):
            pytest.fail("public paper mode must not access private balances")

    monkeypatch.setattr(ccxt, "kraken", lambda config: options.append(config) or Exchange())
    monkeypatch.setenv("SIMULATE_DATA", "false")
    monkeypatch.setenv("KRAKEN_API_KEY", "")
    monkeypatch.setenv("KRAKEN_API_SECRET", "")
    bot = CryptoTradingBot(BotConfig())
    bot.portfolio.state_file = str(tmp_path / "portfolio.json")
    bot._update_portfolio_balance()
    assert bot.portfolio.cash == 19.25
    assert bot.all_symbols == ["BTC/EUR"]
    assert options[0]["apiKey"] == options[0]["secret"] == ""


def test_public_paper_requires_initialized_portfolio(monkeypatch, tmp_path):
    make_bot(monkeypatch, tmp_path)
    monkeypatch.setenv("SIMULATE_DATA", "false")
    monkeypatch.setenv("KRAKEN_API_KEY", "")
    monkeypatch.setenv("KRAKEN_API_SECRET", "")
    monkeypatch.setattr(CryptoTradingBot, "_load_market_info", lambda self: None)
    bot = CryptoTradingBot(BotConfig())
    bot.portfolio.state_file = str(tmp_path / "missing.json")
    with pytest.raises(RuntimeError, match="Initialize the paper portfolio separately"):
        bot._update_portfolio_balance()
    assert not (tmp_path / "missing.json").exists()


def test_public_paper_rejects_credentials(monkeypatch, tmp_path):
    make_bot(monkeypatch, tmp_path)
    monkeypatch.setenv("SIMULATE_DATA", "false")
    with pytest.raises(ValueError, match="no Kraken API credentials"):
        CryptoTradingBot(BotConfig())


def test_public_paper_exchange_error_does_not_enable_simulation(monkeypatch, tmp_path):
    make_bot(monkeypatch, tmp_path)
    monkeypatch.setenv("SIMULATE_DATA", "false")
    monkeypatch.setenv("KRAKEN_API_KEY", "")
    monkeypatch.setenv("KRAKEN_API_SECRET", "")

    def fail(config):
        raise ccxt.NetworkError("offline")

    monkeypatch.setattr(ccxt, "kraken", fail)
    config = BotConfig()
    with pytest.raises(RuntimeError, match="Cannot initialize public Kraken exchange"):
        CryptoTradingBot(config)
    assert config.simulate_data is False


def test_initialization_command_uses_only_free_eur_and_refuses_existing_state(monkeypatch, tmp_path):
    from src.init_kraken_paper import main

    credentials = tmp_path / "credentials.env"
    credentials.write_text("KRAKEN_API_KEY=key\nKRAKEN_API_SECRET=secret\nDRY_RUN=false\n")
    calls = []

    class Exchange:
        def fetch_balance(self):
            calls.append(1)
            return {"free": {"EUR": 25, "BTC": 10}}

    monkeypatch.setattr(ccxt, "kraken", lambda config: Exchange())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["init", "--credentials-file", str(credentials)])
    main()
    state = json.loads((tmp_path / ".portfolio_state.json").read_text())
    assert state["cash"] == 25
    assert state["holdings"] == {}
    with pytest.raises(SystemExit, match="already exists"):
        main()
    assert calls == [1]
