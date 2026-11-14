import csv
from types import SimpleNamespace

import pandas as pd

from src.trading_bot import (
    CryptoTradingBot,
    _compute_recent_pnl_guard_state,
    _config_fingerprint,
)


def test_config_fingerprint_omits_secrets_but_tracks_strategy_values():
    first = SimpleNamespace(api_key="secret-one", min_entry_score=65)
    changed_secret = SimpleNamespace(api_key="secret-two", min_entry_score=65)
    changed_strategy = SimpleNamespace(api_key="secret-one", min_entry_score=66)

    assert _config_fingerprint(first) == _config_fingerprint(changed_secret)
    assert _config_fingerprint(first) != _config_fingerprint(changed_strategy)


def test_recent_guard_reports_stale_journal_without_disabling_guard():
    rows = [
        {
            "action": "sell",
            "pnl_base": -0.01,
            "timestamp": "2026-11-12T07:00:00Z",
        }
        for _ in range(60)
    ]

    state = _compute_recent_pnl_guard_state(
        pd.DataFrame(rows),
        window=120,
        min_trades=60,
        min_realized_pnl=0.0,
        max_profit_factor=1.0,
        stale_after_hours=24.0,
        now=pd.Timestamp("2026-11-14T07:00:00Z"),
    )

    assert state["active"] is True
    assert state["journal_stale"] is True
    assert state["journal_age_hours"] == 48.0
    assert state["latest_closed_trade_at"] == "2026-11-12T07:00:00+00:00"


def test_trade_excursion_metrics_are_lifetime_percentages():
    assert CryptoTradingBot._trade_excursion_metrics(
        {"buy_price": 100.0, "peak_price": 112.0, "trough_price": 95.0}
    ) == (12.0, -5.0)


def test_existing_journal_is_extended_and_new_rows_remain_aligned(tmp_path):
    journal_path = tmp_path / "trade_journal.csv"
    original_fields = [
        "timestamp", "iteration", "coin", "action", "price", "amount_coin",
        "amount_base", "pnl_base", "pnl_pct", "hold_seconds",
        "signal_source", "signal_confidence", "recommendation", "reason",
        "dry_run",
    ]
    with journal_path.open("w", newline="", encoding="utf-8") as journal:
        writer = csv.writer(journal)
        writer.writerow(original_fields)
        writer.writerow(
            ["2026-11-01T00:00:00Z", 1, "BTC", "sell", 100, 1, 100,
             -1, -1, 60, "rules", "", "SELL", "MAX-HOLD-TIME", True]
        )

    bot = CryptoTradingBot.__new__(CryptoTradingBot)
    bot.config = SimpleNamespace(
        performance_log_file=str(journal_path),
        performance_log_enabled=True,
        dry_run=True,
        base_currency="EUR",
    )
    bot.run_id = "run-123"
    bot.config_hash = "config-abc"
    bot.iteration = 2
    bot._journal_fields = []
    bot._init_trade_journal()

    class AnalyticsWriter:
        def write_trade(self, row, base_currency):
            return None

    bot.analytics_writer = AnalyticsWriter()
    bot._append_trade_journal({
        "timestamp": "2026-11-14T07:00:00Z",
        "coin": "ETH",
        "action": "sell",
        "mfe_pct": 2.5,
        "mae_pct": -1.0,
    })

    with journal_path.open(newline="", encoding="utf-8") as journal:
        rows = list(csv.DictReader(journal))

    assert rows[0]["run_id"] == "legacy"
    assert rows[0]["config_hash"] == "legacy"
    assert rows[1]["run_id"] == "run-123"
    assert rows[1]["config_hash"] == "config-abc"
    assert rows[1]["mfe_pct"] == "2.5"
    assert rows[1]["mae_pct"] == "-1.0"
