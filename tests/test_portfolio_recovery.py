from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.trading_bot import CryptoTradingBot, Portfolio


def test_portfolio_state_roundtrip(tmp_path):
    path = tmp_path / "portfolio.json"
    original = Portfolio()
    original.cash = 125.0
    original.holdings = {"BTC": 0.1}
    original.add_trade("BTC", 100.0, 0.1, 10.0)
    assert original.save_state(str(path))
    restored = Portfolio()
    assert restored.load_state(str(path))
    assert restored.cash == original.cash
    assert restored.holdings == original.holdings
    assert restored.open_trades == original.open_trades


def test_interrupted_portfolio_replace_preserves_previous_state(tmp_path, monkeypatch, caplog):
    path = tmp_path / "portfolio.json"
    path.write_text('{"cash": 10}', encoding="utf-8")
    portfolio = Portfolio()
    portfolio.cash = 20

    def fail_replace(source, destination):
        raise OSError("simulated interruption")

    monkeypatch.setattr("src.trading_bot.os.replace", fail_replace)
    assert not portfolio.save_state(str(path))
    assert path.read_text(encoding="utf-8") == '{"cash": 10}'
    assert "simulated interruption" in caplog.text
    assert list(tmp_path.iterdir()) == [path]


def test_corrupt_portfolio_state_cannot_silently_reset(tmp_path, caplog):
    path = tmp_path / "portfolio.json"
    path.write_text('{"cash":', encoding="utf-8")
    with pytest.raises(ValueError):
        Portfolio().load_state(str(path))
    assert "Failed to load portfolio state" in caplog.text


def test_invalid_trade_timestamp_cannot_silently_reset(tmp_path, caplog):
    path = tmp_path / "portfolio.json"
    path.write_text('{"open_trades": {"BTC": {"timestamp": "invalid"}}}', encoding="utf-8")
    with pytest.raises(ValueError):
        Portfolio().load_state(str(path))
    assert "Failed to load portfolio state" in caplog.text


def test_failed_protective_sell_preserves_open_position():
    bot = CryptoTradingBot.__new__(CryptoTradingBot)
    bot.portfolio = Portfolio()
    bot.portfolio.add_trade("BTC", 100.0, 1.0, 100.0)
    bot.portfolio.open_trades["BTC"]["timestamp"] = datetime.now(UTC)
    bot.config = SimpleNamespace(
        coin_max_hold_seconds={}, max_hold_seconds=0, atr_tp_mult=2,
        partial_take_profit_atr_mult=1, partial_take_profit_fraction=0.5,
        partial_take_profit_enabled=False, exit_on_downtrend=False,
    )
    bot._get_atr_for_coin = Mock(return_value=2.0)
    bot._effective_stop_loss_level = Mock(return_value=95.0)
    bot._is_downtrend_reversal_trade = Mock(return_value=False)
    bot._is_rules_uptrend_trade = Mock(return_value=False)
    bot._execute_trade = Mock(return_value=False)
    bot._manage_open_trades({"BTC": {"price": 90.0}})
    bot._execute_trade.assert_called_once()
    assert "BTC" in bot.portfolio.open_trades


@pytest.mark.parametrize("action", ["buy", "sell"])
def test_live_orders_blocked_until_exchange_recovery_is_available(action, caplog):
    bot = CryptoTradingBot.__new__(CryptoTradingBot)
    bot.config = SimpleNamespace(dry_run=False)
    bot.exchange = Mock()
    assert bot._execute_trade("BTC", action, 100.0, 10.0) is False
    bot.exchange.market.assert_not_called()
    bot.exchange.create_market_buy_order.assert_not_called()
    bot.exchange.create_market_sell_order.assert_not_called()
    assert "LIVE order blocked" in caplog.text
