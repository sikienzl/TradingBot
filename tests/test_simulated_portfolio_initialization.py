from types import SimpleNamespace

import pytest

from src.trading_bot import CryptoTradingBot, Portfolio


@pytest.fixture(params=[True, False], ids=["simulated-data", "no-exchange"])
def bot(request, tmp_path):
    bot = object.__new__(CryptoTradingBot)
    bot.config = SimpleNamespace(
        simulation_initial_balance_from_kraken=False,
        simulate_data=request.param,
        base_currency="EUR",
    )
    bot.exchange = object() if request.param else None
    bot.portfolio = Portfolio()
    bot.portfolio.state_file = str(tmp_path / "portfolio.json")
    return bot


def test_initializes_only_once_when_portfolio_becomes_empty(bot):
    bot._update_portfolio_balance()
    assert bot.portfolio.cash == pytest.approx(1000)
    assert bot.portfolio.initial_portfolio_value == pytest.approx(1000)
    assert bot.portfolio.last_update is not None

    bot.portfolio.cash = 0.0
    bot._update_portfolio_balance()
    assert bot.portfolio.cash == pytest.approx(0)


def test_restored_zero_balance_is_not_reinitialized(bot):
    bot.portfolio.cash = 0.0
    bot.portfolio.initial_portfolio_value = 100.0
    assert bot.portfolio.save_state()

    bot._update_portfolio_balance()
    bot._update_portfolio_balance()

    assert bot.portfolio.cash == pytest.approx(0)
    assert bot.portfolio.initial_portfolio_value == pytest.approx(100)


@pytest.mark.parametrize("cash", [1e-10, 25.0, -25.0])
def test_existing_nonzero_cash_is_preserved(bot, cash):
    bot.portfolio.cash = cash
    bot._update_portfolio_balance()
    assert bot.portfolio.cash == pytest.approx(cash, abs=1e-15)


@pytest.mark.parametrize("cash", [-1e-12, 1e-12])
def test_uninitialized_cash_within_zero_tolerance_is_initialized(bot, cash):
    bot.portfolio.cash = cash
    bot._update_portfolio_balance()
    assert bot.portfolio.cash == pytest.approx(1000)


def test_existing_holdings_are_preserved(bot):
    bot.portfolio.holdings = {"BTC": 1.0}
    bot._update_portfolio_balance()
    assert bot.portfolio.cash == pytest.approx(0)
    assert bot.portfolio.holdings == {"BTC": 1.0}


def test_preinitialized_empty_portfolio_is_preserved(bot):
    bot.portfolio.initial_portfolio_value = 0.0
    bot._update_portfolio_balance()
    assert bot.portfolio.cash == pytest.approx(0)
