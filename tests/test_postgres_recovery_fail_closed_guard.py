import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, mock_open

import pytest

from src.trading_bot import BotConfig, CryptoTradingBot, Portfolio


@pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
def test_required_recovery_config_rejects_start_before_side_effects(monkeypatch, value):
    monkeypatch.setattr("src.trading_bot.load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("DRY_RUN_REQUIRE_POSTGRES_RECOVERY", value)
    config = BotConfig()
    writer = Mock()
    monkeypatch.setattr("src.trading_bot.PostgresAnalyticsWriter", writer)
    with pytest.raises(RuntimeError, match="recovery is not implemented"):
        CryptoTradingBot(config)
    writer.assert_not_called()


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_required_recovery_can_be_disabled(monkeypatch, value):
    monkeypatch.setattr("src.trading_bot.load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("DRY_RUN_REQUIRE_POSTGRES_RECOVERY", value)
    assert BotConfig().dry_run_require_postgres_recovery is False


def test_invalid_recovery_switch_does_not_silently_disable_guard(monkeypatch):
    monkeypatch.setattr("src.trading_bot.load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("DRY_RUN_REQUIRE_POSTGRES_RECOVERY", "tru")
    with pytest.raises(ValueError, match="must be a boolean"):
        BotConfig()


@pytest.mark.parametrize(
    "inherited, dotenv",
    [("true", "false"), ("false", "true"), ("tru", "true")],
)
def test_dotenv_override_cannot_disable_or_hide_bad_requirement(monkeypatch, inherited, dotenv):
    monkeypatch.setenv("DRY_RUN_REQUIRE_POSTGRES_RECOVERY", inherited)
    monkeypatch.setattr(
        "src.trading_bot.load_dotenv",
        lambda **kwargs: monkeypatch.setenv("DRY_RUN_REQUIRE_POSTGRES_RECOVERY", dotenv),
    )
    if inherited == "tru":
        with pytest.raises(ValueError, match="must be a boolean"):
            BotConfig()
        with pytest.raises(ValueError, match="must be a boolean"):
            BotConfig()
    else:
        assert BotConfig().dry_run_require_postgres_recovery is True
        assert BotConfig().dry_run_require_postgres_recovery is True


@pytest.mark.parametrize("action", ["buy", "sell"])
def test_recovery_guard_blocks_trade_before_any_mutation(action):
    bot = CryptoTradingBot.__new__(CryptoTradingBot)
    bot.config = SimpleNamespace(dry_run=True, dry_run_require_postgres_recovery=True)
    bot.portfolio = Portfolio()
    bot.portfolio.cash = 100
    bot.portfolio.holdings = {"BTC": 1}
    bot.portfolio.add_trade("BTC", 100, 1, 100)
    before = (bot.portfolio.cash, dict(bot.portfolio.holdings), dict(bot.portfolio.open_trades))
    bot.analytics_writer = Mock()
    assert not bot._execute_trade("BTC", action, 100, 50)
    assert (bot.portfolio.cash, bot.portfolio.holdings, bot.portfolio.open_trades) == before
    bot.analytics_writer.assert_not_called()


def test_guard_blocks_protective_sell_and_keeps_position():
    bot = CryptoTradingBot.__new__(CryptoTradingBot)
    bot.config = SimpleNamespace(
        dry_run=True, dry_run_require_postgres_recovery=True,
        coin_max_hold_seconds={}, max_hold_seconds=0, atr_tp_mult=2,
        partial_take_profit_atr_mult=1, partial_take_profit_fraction=0.5,
        partial_take_profit_enabled=False, exit_on_downtrend=False,
    )
    bot.portfolio = Portfolio()
    bot.portfolio.holdings = {"BTC": 1}
    bot.portfolio.add_trade("BTC", 100, 1, 100)
    bot._get_atr_for_coin = Mock(return_value=2.0)
    bot._effective_stop_loss_level = Mock(return_value=95.0)
    bot._is_downtrend_reversal_trade = Mock(return_value=False)
    bot._is_rules_uptrend_trade = Mock(return_value=False)
    bot._manage_open_trades({"BTC": {"price": 90}})
    assert bot.portfolio.holdings == {"BTC": 1}
    assert bot.portfolio.open_trades["BTC"]["amount_coin"] == 1


def test_partial_take_profit_timestamp_restored_for_exit_monitoring(monkeypatch):
    now = datetime.now(UTC)
    state = {
        "cash": 10, "holdings": {"BTC": 0.5},
        "open_trades": {"BTC": {
            "buy_price": 100, "amount_coin": 0.5, "amount_base": 50,
            "timestamp": (now - timedelta(hours=2)).isoformat(),
            "partial_tp_taken": True,
            "partial_tp_timestamp": (now - timedelta(hours=1)).isoformat(),
        }},
    }
    monkeypatch.setattr("src.trading_bot.os.path.exists", lambda path: True)
    monkeypatch.setattr("builtins.open", mock_open(read_data=json.dumps(state)))
    portfolio = Portfolio()
    assert portfolio.load_state("portfolio.json")
    assert portfolio.open_trades["BTC"]["partial_tp_timestamp"] == now - timedelta(hours=1)
    assert portfolio.open_trades["BTC"]["timestamp"] == now - timedelta(hours=2)
    bot = CryptoTradingBot.__new__(CryptoTradingBot)
    bot.portfolio = portfolio
    bot.config = SimpleNamespace(
        coin_max_hold_seconds={}, max_hold_seconds=0, atr_tp_mult=2,
        partial_take_profit_atr_mult=1, partial_take_profit_fraction=0.5,
        partial_take_profit_enabled=False, exit_on_downtrend=False,
        partial_take_profit_remainder_max_hold_seconds=30,
    )
    bot._get_atr_for_coin = Mock(return_value=2.0)
    bot._effective_stop_loss_level = Mock(return_value=95.0)
    bot._is_downtrend_reversal_trade = Mock(return_value=False)
    bot._is_rules_uptrend_trade = Mock(return_value=False)
    bot._execute_trade = Mock(return_value=False)
    bot._manage_open_trades({"BTC": {"price": 100}})
    assert "PARTIAL-TP-REMAINDER-TIMEOUT" in bot._execute_trade.call_args.kwargs["reason"]


def test_invalid_partial_timestamp_does_not_partially_replace_state(monkeypatch):
    state = {
        "cash": 100, "holdings": {"ETH": 2},
        "open_trades": {"ETH": {"partial_tp_timestamp": "invalid"}},
    }
    monkeypatch.setattr("src.trading_bot.os.path.exists", lambda path: True)
    monkeypatch.setattr("builtins.open", mock_open(read_data=json.dumps(state)))
    portfolio = Portfolio()
    portfolio.cash = 5
    portfolio.holdings = {"BTC": 1}
    portfolio.open_trades = {"BTC": {"amount_coin": 1}}
    with pytest.raises(ValueError):
        portfolio.load_state("portfolio.json")
    assert portfolio.cash == 5
    assert portfolio.holdings == {"BTC": 1}
    assert portfolio.open_trades == {"BTC": {"amount_coin": 1}}
