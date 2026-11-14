import csv
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import public_paper_daily_report as report

START = datetime(2026, 6, 6, tzinfo=UTC)
END = datetime(2026, 6, 7, tzinfo=UTC)
FIELDS = [
    "timestamp", "action", "net_pnl_base", "fee_base", "slippage_base",
    "hold_seconds", "execution_cost_mode", "entry_cost_mode", "config_hash", "run_id",
]


def write_journal(path, rows, fields=FIELDS):
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_journal_window_costs_partial_sells_and_missing_values(tmp_path):
    journal = tmp_path / "journal.csv"
    write_journal(journal, [
        {"timestamp": "2026-06-05T23:59:59Z", "action": "buy", "fee_base": "99"},
        {"timestamp": "2026-06-06T00:00:00Z", "action": "buy", "fee_base": ".2", "slippage_base": ".1"},
        {"timestamp": "2026-06-06T12:00:00Z", "action": "sell", "net_pnl_base": "-1",
         "fee_base": ".1", "slippage_base": ".05", "hold_seconds": "60",
         "execution_cost_mode": "net_costs", "entry_cost_mode": "net_costs", "config_hash": "abc"},
        {"timestamp": "2026-06-06T13:00:00Z", "action": "sell", "net_pnl_base": "2",
         "fee_base": ".1", "slippage_base": ".05", "hold_seconds": "120",
         "execution_cost_mode": "net_costs", "entry_cost_mode": "net_costs", "config_hash": "abc"},
        {"timestamp": "2026-06-07T00:00:00Z", "action": "sell", "net_pnl_base": "999"},
        {"timestamp": "bad", "action": "buy"},
    ])
    result = report.journal_report(str(journal), START, END)
    assert result["buys"] == 1
    assert result["sell_events"] == 2
    assert result["net_realized_pnl_base"] == 1
    assert result["fees_base_all_fills"] == 0.4
    assert result["slippage_base_all_fills"] == 0.2
    assert result["average_sell_hold_seconds"] == 90
    assert result["invalid_journal_timestamps"] == 1
    assert result["config_hashes"] == ["abc"]


@pytest.mark.parametrize("entry,pnl", [("gross_legacy", "1"), ("net_costs", ""), ("net_costs", "nan")])
def test_unknown_net_is_not_reported_as_zero(tmp_path, entry, pnl):
    journal = tmp_path / "journal.csv"
    write_journal(journal, [{"timestamp": "2026-06-06T00:00:00Z", "action": "sell",
                             "execution_cost_mode": "net_costs", "entry_cost_mode": entry,
                             "net_pnl_base": pnl}])
    result = report.journal_report(str(journal), START, END)
    assert result["net_realized_pnl_base"] is None
    assert result["fees_base_all_fills"] is None


def test_logs_count_individual_decisions_and_not_summary_duplicates():
    text = "\n".join([  # noqa: FLY002
        "2026-06-06 02:00:00,000 - INFO - 📊 HAILO for BTC: KAUFEN (Confidence: 80%)",
        "2026-06-06 02:01:00,000 - INFO - ⛔ Momentum filter blocked entry for BTC: RSI",
        "2026-06-06 02:02:00,000 - INFO - ⛔ Re-entry cooldown active for ETH",
        "2026-06-06 02:03:00,000 - INFO - 🚫 Buy attempt blocked 2 candidate(s): {'x': 2}",
        "2026-06-06 02:04:00,000 - ERROR - Hailo prediction for BTC failed; suppressing this coin's entries: error",
        "2026-06-07 02:00:00,000 - INFO - 📊 HAILO for BTC: HALTEN",
    ])
    result = report.log_report(text, START, END, ZoneInfo("Europe/Berlin"))
    assert result["hailo_decisions"] == {"kaufen": 1}
    assert result["blocked_entry_log_events"] == {"momentum": 1, "reentry_cooldown": 1}
    assert result["hailo_other_events"] == {"prediction_failed": 1}


def test_comparison_cli_reports_non_comparable_and_redacts_secrets(tmp_path):
    journal = tmp_path / "journal.csv"
    write_journal(journal, [])
    config = tmp_path / "paper.env"
    config.write_text("DRY_RUN=true\nSIMULATE_DATA=false\nHAILO_API_TOKEN=secret\n", encoding="utf-8")
    other = tmp_path / "other.env"
    other.write_text("DRY_RUN=true\nSIMULATE_DATA=true\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/public_paper_daily_report.py"),
         "--date", "2026-06-06", "--journal", str(journal), "--config", str(config),
         "--compare-journal", str(journal), "--compare-config", str(other),
         "--compare-feed", "synthetic"],
        text=True, capture_output=True, check=True,
    )
    result = json.loads(completed.stdout)
    assert result["comparison"]["status"] == "non-comparable"
    assert result["comparison"]["config_differences"]["SIMULATE_DATA"] == {
        "primary": "false", "comparison": "true"
    }
    assert result["comparison"]["observation"]["feed_description_operator_supplied"] == "synthetic"
    assert result["primary"]["log"] is None
    assert "secret" not in completed.stdout


def test_docker_log_errors_fail_report(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        report.load_log(None, "paper", START, END)


def test_scheduled_wrapper_writes_private_report(tmp_path):
    paper = tmp_path / "paper"
    (paper / "data").mkdir(parents=True)
    write_journal(paper / "data" / "trade_journal.csv", [])
    (paper / "data" / "trading_bot.log").write_text("", encoding="utf-8")
    (paper / "paper.env").write_text("DRY_RUN=true\n", encoding="utf-8")
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PUBLIC_PAPER_DIR": str(paper),
        "PYTHON_BIN": sys.executable,
    }
    subprocess.run(["bash", str(ROOT / "scripts/run_public_paper_daily_report.sh")],
                   env=env, check=True, capture_output=True, text=True)
    latest = paper / "reports" / "latest.json"
    result = json.loads(latest.read_text(encoding="utf-8"))
    assert result["primary"]["journal"]["buys"] == 0
    assert result["primary"]["log"]["parsed_lines_in_window"] == 0
    assert latest.stat().st_mode & 0o077 == 0
