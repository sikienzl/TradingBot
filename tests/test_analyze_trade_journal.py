import pandas as pd

from src.analyze_trade_journal import (
    optimize_confidence_threshold,
    report_daily,
    report_exit_excursions,
    report_journal_freshness,
    report_run_config_breakdown,
    summarize,
)


def test_journal_freshness_warns_for_stale_events(capsys):
    report_journal_freshness(
        pd.DataFrame({"timestamp": ["2026-11-12T07:00:00Z"]}),
        as_of=pd.Timestamp("2026-11-14T07:00:00Z"),
    )

    output = capsys.readouterr().out
    assert "Journal age:          48.0 hours" in output
    assert "do not treat the latest trade metrics as current" in output


def test_confidence_sweep_reports_chronological_holdout(capsys):
    trades = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=10, freq="h"),
            "signal_confidence": [0.9] * 7 + [0.6, 0.9, 0.9],
            "pnl_base": [0.01] * 7 + [-0.02, -0.02, -0.02],
        }
    )

    optimize_confidence_threshold(
        trades,
        base_currency="EUR",
        threshold_min=0.5,
        threshold_max=0.8,
        threshold_step=0.3,
        min_trades=2,
    )

    output = capsys.readouterr().out
    assert "first 70%" in output
    assert "last 30%" in output
    assert "not a deployment recommendation" in output


def test_run_and_exit_reports_segment_journal_rows(capsys):
    sells = pd.DataFrame(
        [
            {
                "run_id": "run-a",
                "config_hash": "abcdef1234567890",
                "reason": "MAX-HOLD-TIME",
                "pnl_base": -0.02,
                "pnl_pct": -1.0,
                "mfe_pct": 0.4,
                "mae_pct": -1.5,
            },
            {
                "run_id": "run-b",
                "config_hash": "123456abcdef7890",
                "reason": "ATR-STOP",
                "pnl_base": -0.01,
                "pnl_pct": -0.5,
                "mfe_pct": 0.2,
                "mae_pct": -0.8,
            },
        ]
    )

    report_run_config_breakdown(sells, "EUR")
    report_exit_excursions(sells)

    output = capsys.readouterr().out
    assert "run_id=run-a config_hash=abcdef123456" in output
    assert "MAX-HOLD-TIME" in output
    assert "avg_mfe_pct" in output


def test_reports_count_break_evens_without_inflating_win_rate(capsys):
    sells = pd.DataFrame(
        [
            {
                "action": "sell", "timestamp": pd.Timestamp("2026-11-14"),
                "pnl_base": 1.0, "hold_seconds": 0.0,
            },
            {
                "action": "sell", "timestamp": pd.Timestamp("2026-11-14"),
                "pnl_base": -1.0, "hold_seconds": 0.0,
            },
            {
                "action": "sell", "timestamp": pd.Timestamp("2026-11-14"),
                "pnl_base": 0.0, "hold_seconds": 0.0,
            },
        ]
    )

    summarize(sells.copy(), "EUR")
    report_daily(sells, "EUR")

    output = capsys.readouterr().out
    assert "Wins / Losses / BE:   1 / 1 / 1" in output
    assert "Win rate:            33.33%" in output
    assert "break_even" in output
