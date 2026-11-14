import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.trading_bot import BotConfig, CryptoTradingBot, Portfolio


def make_bot(monkeypatch, path):
    monkeypatch.chdir(path)
    for name, value in {
        "DRY_RUN": "true",
        "SIMULATE_DATA": "false",
        "SIMULATION_INITIAL_BALANCE_FROM_KRAKEN": "true",
        "KRAKEN_API_KEY": "",
        "KRAKEN_API_SECRET": "",
        "USE_TABULAR_MODEL": "false",
        "USE_ML_MODEL": "false",
        "ANALYTICS_DB_ENABLED": "false",
        "PERFORMANCE_LOG_ENABLED": "true",
        "PERFORMANCE_LOG_FILE": "journal.csv",
        "DRY_RUN_COSTS_ENABLED": "true",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(CryptoTradingBot, "_initialize_exchange", lambda self: None)
    if not (path / ".portfolio_state.json").exists():
        portfolio = Portfolio()
        portfolio.cash = 100
        portfolio.save_state()
    bot = CryptoTradingBot(BotConfig())
    bot._update_portfolio_balance()
    return bot


def rows(path):
    with (path / "journal.csv").open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


@pytest.mark.parametrize("crash_point", ["intent", "partial_journal", "state", "unlink"])
@pytest.mark.parametrize("action", ["buy", "partial_sell", "full_sell"])
def test_restart_completes_interrupted_trade_once(monkeypatch, tmp_path, crash_point, action):
    bot = make_bot(monkeypatch, tmp_path)
    if action != "buy":
        assert bot._execute_trade("BTC", "buy", 100, 80)
    initial_count = len(rows(tmp_path))

    with monkeypatch.context() as patch:
        if crash_point in ("intent", "partial_journal"):
            def interrupt():
                if crash_point == "partial_journal":
                    intent = json.loads((tmp_path / ".portfolio_state.json.trade-transaction.json").read_text())
                    data = intent["payload"]["row"].encode()
                    with (tmp_path / "journal.csv").open("ab") as journal:
                        journal.write(data[:len(data) // 2])
                        journal.flush()
                        os.fsync(journal.fileno())
                raise RuntimeError("simulated crash")
            patch.setattr(bot, "_recover_paper_trade", interrupt)
        elif crash_point == "state":
            patch.setattr(bot.portfolio, "save_state", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("simulated crash")))
        else:
            real_unlink = os.unlink

            def interrupt_unlink(path, *args, **kwargs):
                if str(path).endswith(".trade-transaction.json"):
                    raise OSError("simulated crash")
                return real_unlink(path, *args, **kwargs)

            patch.setattr("src.trading_bot.os.unlink", interrupt_unlink)

        with pytest.raises(RuntimeError):
            if action == "buy":
                bot._execute_trade("BTC", "buy", 100, 80)
            else:
                quantity = bot.portfolio.holdings["BTC"]
                partial = action == "partial_sell"
                bot._execute_trade("BTC", "sell", 110, quantity * 110 / (2 if partial else 1),
                                   partial_take_profit=partial)

    restarted = make_bot(monkeypatch, tmp_path)
    assert len(rows(tmp_path)) == initial_count + 1
    assert rows(tmp_path)[-1]["action"] == ("buy" if action == "buy" else "sell")
    quantity = 80 / (100 * 1.001 * 1.002)
    if action == "buy":
        assert restarted.portfolio.cash == pytest.approx(20)
        assert restarted.portfolio.holdings["BTC"] == pytest.approx(quantity)
    elif action == "partial_sell":
        assert restarted.portfolio.holdings["BTC"] == pytest.approx(quantity / 2)
        assert restarted.portfolio.open_trades["BTC"]["amount_base"] == pytest.approx(40)
        assert restarted.portfolio.open_trades["BTC"]["partial_tp_taken"] is True
        assert restarted.portfolio.cash == pytest.approx(20 + quantity / 2 * 110 * 0.999 * 0.998)
    else:
        assert "BTC" not in restarted.portfolio.holdings
        assert "BTC" not in restarted.portfolio.open_trades
        assert restarted.portfolio.cash == pytest.approx(20 + quantity * 110 * 0.999 * 0.998)
    assert not (tmp_path / ".portfolio_state.json.trade-transaction.json").exists()
    make_bot(monkeypatch, tmp_path)
    assert len(rows(tmp_path)) == initial_count + 1


def test_crash_before_intent_leaves_trade_uncommitted(monkeypatch, tmp_path):
    bot = make_bot(monkeypatch, tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(bot, "_sync_directory", lambda path: (_ for _ in ()).throw(OSError("disk")))
        with pytest.raises(RuntimeError, match="Cannot commit"):
            bot._execute_trade("BTC", "buy", 100, 80)
        with pytest.raises(RuntimeError, match="restart to recover"):
            bot._execute_trade("BTC", "buy", 100, 10)
    restarted = make_bot(monkeypatch, tmp_path)
    assert restarted.portfolio.cash == 100
    assert restarted.portfolio.holdings == {}
    assert rows(tmp_path) == []


def test_recovery_can_be_interrupted_and_retried(monkeypatch, tmp_path):
    bot = make_bot(monkeypatch, tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(bot.portfolio, "save_state", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("crash")))
        with pytest.raises(RuntimeError):
            bot._execute_trade("BTC", "buy", 100, 80)
    with monkeypatch.context() as patch:
        patch.setattr(Portfolio, "save_state", lambda self, **kwargs: (_ for _ in ()).throw(RuntimeError("crash again")))
        with pytest.raises(RuntimeError, match="crash again"):
            make_bot(monkeypatch, tmp_path)
    restarted = make_bot(monkeypatch, tmp_path)
    assert restarted.portfolio.cash == pytest.approx(20)
    assert len(rows(tmp_path)) == 1


def test_process_exit_during_journal_write_recovers(monkeypatch, tmp_path):
    make_bot(monkeypatch, tmp_path)
    script = """
import json, os
from src.trading_bot import BotConfig, CryptoTradingBot
CryptoTradingBot._initialize_exchange = lambda self: None
bot = CryptoTradingBot(BotConfig())
bot._update_portfolio_balance()
def crash():
    with open('.portfolio_state.json.trade-transaction.json') as f:
        row = json.load(f)['payload']['row'].encode()
    with open('journal.csv', 'ab') as f:
        f.write(row[:len(row) // 2])
        f.flush()
        os.fsync(f.fileno())
    os._exit(73)
bot._recover_paper_trade = crash
bot._execute_trade('BTC', 'buy', 100, 80)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, check=False)
    assert result.returncode == 73
    restarted = make_bot(monkeypatch, tmp_path)
    assert restarted.portfolio.cash == pytest.approx(20)
    assert len(rows(tmp_path)) == 1


@pytest.mark.parametrize("damage", ["intent", "prefix", "tail", "missing_journal"])
def test_recovery_fails_closed_on_damage(monkeypatch, tmp_path, damage):
    bot = make_bot(monkeypatch, tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(bot, "_recover_paper_trade", lambda: (_ for _ in ()).throw(RuntimeError("crash")))
        with pytest.raises(RuntimeError):
            bot._execute_trade("BTC", "buy", 100, 80)
    intent = tmp_path / ".portfolio_state.json.trade-transaction.json"
    journal = tmp_path / "journal.csv"
    if damage == "intent":
        intent.write_text("{")
    elif damage == "prefix":
        journal.write_bytes(b"damaged\n")
    elif damage == "tail":
        with journal.open("ab") as f:
            f.write(b"unexpected")
    else:
        journal.unlink()
    with pytest.raises(RuntimeError, match="Cannot recover paper trade"):
        make_bot(monkeypatch, tmp_path)
    assert intent.exists()
    assert json.loads((tmp_path / ".portfolio_state.json").read_text())["cash"] == 100


def test_public_paper_requires_journal(monkeypatch, tmp_path):
    make_bot(monkeypatch, tmp_path)
    monkeypatch.setenv("PERFORMANCE_LOG_ENABLED", "false")
    with pytest.raises(RuntimeError, match="requires an enabled trade journal"):
        CryptoTradingBot(BotConfig())
