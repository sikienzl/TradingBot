import csv

import pytest

from src.trading_bot import BotConfig, CryptoTradingBot


def make_bot(monkeypatch, path, *, enabled=True, dry_run=True, journal=True):
    for name in ("DRY_RUN_FEE_RATE", "DRY_RUN_SLIPPAGE_RATE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DRY_RUN", str(dry_run))
    monkeypatch.setenv("DRY_RUN_COSTS_ENABLED", str(enabled))
    monkeypatch.setenv("SIMULATE_DATA", "true")
    monkeypatch.setenv("SIMULATION_INITIAL_BALANCE_FROM_KRAKEN", "false")
    monkeypatch.setenv("USE_TABULAR_MODEL", "false")
    monkeypatch.setenv("USE_ML_MODEL", "false")
    monkeypatch.setenv("ANALYTICS_DB_ENABLED", "false")
    monkeypatch.setenv("PERFORMANCE_LOG_ENABLED", str(journal))
    monkeypatch.setenv("PERFORMANCE_LOG_FILE", str(path / "journal.csv"))
    bot = CryptoTradingBot(BotConfig())
    bot.portfolio.state_file = str(path / "portfolio.json")
    bot.portfolio.cash = 100.0
    return bot


def rows(path):
    with (path / "journal.csv").open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def test_default_off_preserves_gross_accounting(monkeypatch, tmp_path):
    bot = make_bot(monkeypatch, tmp_path, enabled=False)
    monkeypatch.delenv("DRY_RUN_COSTS_ENABLED")
    assert BotConfig().dry_run_costs_enabled is False
    assert bot._execute_trade("BTC", "buy", 100, 100)
    assert bot.portfolio.holdings["BTC"] == 1
    assert bot._execute_trade("BTC", "sell", 110, 110)
    assert bot.portfolio.cash == 110
    assert bot.performance["realized_pnl_base"] == 10
    assert [row["execution_cost_mode"] for row in rows(tmp_path)] == [
        "gross_legacy", "gross_legacy"
    ]


def test_custom_rates_are_used(monkeypatch, tmp_path):
    make_bot(monkeypatch, tmp_path)
    monkeypatch.setenv("DRY_RUN_FEE_RATE", "0.01")
    monkeypatch.setenv("DRY_RUN_SLIPPAGE_RATE", "0.02")
    bot = CryptoTradingBot(BotConfig())
    bot.portfolio.state_file = str(tmp_path / "portfolio.json")
    bot.portfolio.cash = 100
    assert bot._execute_trade("BTC", "buy", 100, 100)
    quantity = 100 / (102 * 1.01)
    assert bot.portfolio.holdings["BTC"] == pytest.approx(quantity)
    assert bot._execute_trade("BTC", "sell", 100, quantity * 100)
    assert bot.portfolio.cash == pytest.approx(quantity * 98 * 0.99)


def test_net_round_trip_and_journal(monkeypatch, tmp_path):
    bot = make_bot(monkeypatch, tmp_path)
    assert bot._execute_trade("BTC", "buy", 100, 100)
    quantity = 100 / (100 * 1.001 * 1.002)
    assert bot.portfolio.cash == 0
    assert bot.portfolio.holdings["BTC"] == pytest.approx(quantity)
    assert bot.portfolio.open_trades["BTC"]["amount_base"] == 100
    assert bot._execute_trade("BTC", "sell", 100, quantity * 100)
    proceeds = quantity * 100 * 0.999 * 0.998
    assert bot.portfolio.cash == pytest.approx(proceeds)
    assert bot.performance["realized_pnl_base"] == pytest.approx(proceeds - 100)
    buy, sell = rows(tmp_path)
    assert buy["execution_cost_mode"] == sell["execution_cost_mode"] == "net_costs"
    assert sell["entry_cost_mode"] == "net_costs"
    assert float(buy["fee_base"]) == pytest.approx(quantity * 100.1 * 0.002)
    assert float(sell["slippage_base"]) == pytest.approx(quantity * 0.1)
    assert float(sell["amount_base"]) == pytest.approx(proceeds)
    assert float(sell["pnl_pct"]) == pytest.approx(proceeds - 100)
    assert float(sell["gross_pnl_base"]) == 0
    assert float(sell["net_pnl_base"]) == pytest.approx(proceeds - 100)
    assert buy["gross_pnl_base"] == buy["net_pnl_base"] == ""


def test_partial_and_restart_preserve_basis(monkeypatch, tmp_path):
    bot = make_bot(monkeypatch, tmp_path)
    assert bot._execute_trade("ETH", "buy", 100, 80)
    quantity = bot.portfolio.holdings["ETH"]
    assert bot._execute_trade("ETH", "sell", 120, quantity * 120 / 4)
    assert bot.portfolio.holdings["ETH"] == pytest.approx(quantity * 0.75)
    assert bot.portfolio.open_trades["ETH"]["amount_base"] == pytest.approx(60)
    first_pnl = bot.performance["realized_pnl_base"]
    restarted = make_bot(monkeypatch, tmp_path)
    assert restarted.portfolio.load_state()
    assert restarted.portfolio.open_trades["ETH"]["execution_cost_mode"] == "net_costs"
    assert restarted.portfolio.open_trades["ETH"]["entry_reference_price"] == 100
    assert restarted._execute_trade("ETH", "sell", 120, quantity * 120 * 0.75)
    expected_proceeds = quantity * 120 * 0.999 * 0.998
    assert restarted.portfolio.cash == pytest.approx(20 + expected_proceeds)
    assert first_pnl + restarted.performance["realized_pnl_base"] == pytest.approx(expected_proceeds - 80)
    assert "ETH" not in restarted.portfolio.open_trades
    _, first_sell, second_sell = rows(tmp_path)
    assert float(first_sell["gross_pnl_base"]) == pytest.approx(quantity * 0.25 * 20)
    assert float(second_sell["gross_pnl_base"]) == pytest.approx(quantity * 0.75 * 20)
    assert float(first_sell["net_pnl_base"]) == pytest.approx(first_pnl)
    assert float(second_sell["net_pnl_base"]) == pytest.approx(
        restarted.performance["realized_pnl_base"])


@pytest.mark.parametrize("legacy_basis", [100, None])
def test_legacy_entry_sells_with_exit_cost_only(monkeypatch, tmp_path, caplog, legacy_basis):
    old = make_bot(monkeypatch, tmp_path, enabled=False)
    assert old._execute_trade("BTC", "buy", 100, 100)
    if legacy_basis is None:
        del old.portfolio.open_trades["BTC"]["amount_base"]
    else:
        old.portfolio.open_trades["BTC"]["amount_base"] = legacy_basis
    old.portfolio.save_state()
    bot = make_bot(monkeypatch, tmp_path, enabled=True)
    assert bot.portfolio.load_state()
    assert bot._execute_trade("BTC", "sell", 100, 50)
    assert bot.portfolio.open_trades["BTC"]["amount_base"] == 50
    assert bot.performance["realized_pnl_base"] == pytest.approx(50 * 0.999 * 0.998 - 50)
    assert rows(tmp_path)[-1]["entry_cost_mode"] == "gross_legacy"
    assert rows(tmp_path)[-1]["gross_pnl_base"] == rows(tmp_path)[-1]["net_pnl_base"] == ""
    if legacy_basis is None:
        assert "reconstructing from entry price" in caplog.text


def test_cost_enabled_sell_rejects_untracked_holdings(monkeypatch, tmp_path, caplog):
    bot = make_bot(monkeypatch, tmp_path)
    bot.portfolio.holdings["BTC"] = 1.0
    assert not bot._execute_trade("BTC", "sell", 100, 100)
    assert bot.portfolio.cash == 100
    assert bot.portfolio.holdings == {"BTC": 1.0}
    assert not bot.portfolio.open_trades
    assert bot.performance["closed_trades"] == 0
    assert rows(tmp_path) == []
    assert not (tmp_path / "portfolio.json").exists()
    assert "no tracked entry cost basis" in caplog.text


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("basis", [None, "invalid", 0, -1, float("nan"), float("inf")])
def test_net_entry_rejects_corrupt_basis_without_mutation(monkeypatch, tmp_path, caplog, enabled, basis):
    bot = make_bot(monkeypatch, tmp_path)
    assert bot._execute_trade("BTC", "buy", 100, 100)
    quantity = bot.portfolio.holdings["BTC"]
    if basis is None:
        del bot.portfolio.open_trades["BTC"]["amount_base"]
    else:
        bot.portfolio.open_trades["BTC"]["amount_base"] = basis
    bot.config.dry_run_costs_enabled = enabled
    saved = (tmp_path / "portfolio.json").read_bytes()
    journal = (tmp_path / "journal.csv").read_bytes()
    assert not bot._execute_trade("BTC", "sell", 100, quantity * 100)
    assert bot.portfolio.cash == 0
    assert bot.portfolio.holdings["BTC"] == quantity
    assert bot.portfolio.open_trades["BTC"]["amount_coin"] == quantity
    assert bot.performance["closed_trades"] == 0
    assert (tmp_path / "portfolio.json").read_bytes() == saved
    assert (tmp_path / "journal.csv").read_bytes() == journal
    assert "invalid entry cost basis" in caplog.text


@pytest.mark.parametrize("basis", ["invalid", 0, float("nan")])
def test_invalid_legacy_basis_is_not_reconstructed(monkeypatch, tmp_path, basis):
    bot = make_bot(monkeypatch, tmp_path, enabled=False)
    assert bot._execute_trade("BTC", "buy", 100, 100)
    bot.portfolio.open_trades["BTC"]["amount_base"] = basis
    bot.config.dry_run_costs_enabled = True
    assert not bot._execute_trade("BTC", "sell", 100, 100)
    assert bot.portfolio.cash == 0
    assert bot.portfolio.holdings["BTC"] == 1
    assert bot.performance["closed_trades"] == 0


def test_disabling_costs_preserves_previously_net_entry_basis(monkeypatch, tmp_path):
    bot = make_bot(monkeypatch, tmp_path)
    assert bot._execute_trade("BTC", "buy", 100, 100)
    quantity = bot.portfolio.holdings["BTC"]
    restarted = make_bot(monkeypatch, tmp_path, enabled=False)
    assert restarted.portfolio.load_state()
    assert restarted._execute_trade("BTC", "sell", 100, quantity * 100)
    assert restarted.performance["realized_pnl_base"] == pytest.approx(quantity * 100 - 100)
    assert rows(tmp_path)[-1]["entry_cost_mode"] == "net_costs"
    assert float(rows(tmp_path)[-1]["gross_pnl_base"]) == 0
    assert float(rows(tmp_path)[-1]["net_pnl_base"]) == pytest.approx(quantity * 100 - 100)


@pytest.mark.parametrize("reference", [None, "invalid", 0, float("nan")])
def test_missing_historical_reference_does_not_fabricate_pnl(monkeypatch, tmp_path, reference):
    bot = make_bot(monkeypatch, tmp_path)
    assert bot._execute_trade("BTC", "buy", 100, 100)
    quantity = bot.portfolio.holdings["BTC"]
    if reference is None:
        del bot.portfolio.open_trades["BTC"]["entry_reference_price"]
    else:
        bot.portfolio.open_trades["BTC"]["entry_reference_price"] = reference
    bot.portfolio.save_state()
    restarted = make_bot(monkeypatch, tmp_path, enabled=False)
    assert restarted.portfolio.load_state()
    assert restarted._execute_trade("BTC", "sell", 100, quantity * 100)
    sell = rows(tmp_path)[-1]
    assert sell["gross_pnl_base"] == sell["net_pnl_base"] == ""
    assert float(sell["pnl_base"]) == pytest.approx(quantity * 100 - 100)


@pytest.mark.parametrize("name", ["DRY_RUN_FEE_RATE", "DRY_RUN_SLIPPAGE_RATE"])
@pytest.mark.parametrize("value", ["-0.1", "1", "nan", "inf", "garbage"])
def test_invalid_rates_rejected_only_when_opted_in(monkeypatch, name, value):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DRY_RUN_COSTS_ENABLED", "true")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        BotConfig()
    monkeypatch.setenv("DRY_RUN_COSTS_ENABLED", "false")
    BotConfig()
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DRY_RUN_COSTS_ENABLED", "true")
    BotConfig()


def test_insufficient_cash_and_invalid_input_have_no_side_effects(monkeypatch, tmp_path):
    bot = make_bot(monkeypatch, tmp_path)
    for price, budget in [(100, 101), (0, 10), (100, -1), (float("nan"), 10)]:
        assert not bot._execute_trade("BTC", "buy", price, budget)
    assert bot.portfolio.cash == 100
    assert not bot.portfolio.holdings
    assert not bot.portfolio.open_trades
    assert rows(tmp_path) == []
    assert not (tmp_path / "portfolio.json").exists()


def test_live_trade_does_not_apply_costs(monkeypatch, tmp_path):
    bot = make_bot(monkeypatch, tmp_path, dry_run=False)
    class Exchange:
        def market(self, symbol):
            return {"limits": {"amount": {"min": 0}}}

        def create_market_buy_order(self, symbol, amount):
            assert symbol == "BTC/EUR"
            assert amount == 1
            return {"filled": amount, "average": 100, "cost": 100, "status": "closed"}

    bot.exchange = Exchange()
    monkeypatch.setattr(bot, "_update_portfolio_balance", lambda: None)
    assert bot._execute_trade("BTC", "buy", 100, 100)
    assert bot.portfolio.open_trades["BTC"]["amount_coin"] == 1
    assert bot.portfolio.open_trades["BTC"]["amount_base"] == 100
    assert rows(tmp_path)[0]["execution_cost_mode"] == "live_exchange"


def test_legacy_journal_migration_preserves_rows(monkeypatch, tmp_path):
    path = tmp_path / "journal.csv"
    path.write_text("coin,action,pnl_base\nBTC,sell,-1\n", encoding="utf-8")
    make_bot(monkeypatch, tmp_path)
    assert rows(tmp_path)[0]["execution_cost_mode"] == "legacy_unknown"
    assert rows(tmp_path)[0]["pnl_base"] == "-1"
