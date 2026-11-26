import csv
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.task_status import read_task_status, task_exit_code, task_last_run_age_seconds

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


@pytest.mark.parametrize(
    "line,timestamp,message",
    [
        ("2026-06-06 02:00:00,000 - INFO - Hello", "2026-06-06 02:00:00,000", "Hello"),
        ("2026-06-06 02:00:00.123456\t-\tWARNING\t-\t📊 HAILO", "2026-06-06 02:00:00.123456", "📊 HAILO"),
        ("2026-06-06 02:00:00,0 - INFO -   ", "2026-06-06 02:00:00,0", ""),
    ],
)
def test_log_timestamp_accepts_valid_formats(line, timestamp, message):
    match = report.LOG_TIMESTAMP.match(line)
    assert match is not None
    assert match.groups() == (timestamp, message)


def test_log_timestamp_handles_long_malformed_line_without_excessive_backtracking():
    code = (
        "from scripts.public_paper_daily_report import LOG_TIMESTAMP\n"
        "line = '2026-06-06 02:00:00,000 - INFO - ' + ' ' * 100000 + 'x\\ny'\n"
        "assert LOG_TIMESTAMP.match(line) is None\n"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "scripts")},
        check=True,
        timeout=5,
    )


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
    assert "current_day_to_now" not in result
    assert result["comparison"]["status"] == "non-comparable"
    assert result["comparison"]["config_differences"]["SIMULATE_DATA"] == {
        "primary": "false", "comparison": "true"
    }
    assert result["comparison"]["observation"]["feed_description_operator_supplied"] == "synthetic"
    assert result["primary"]["log"] is None
    assert "secret" not in completed.stdout


def test_regular_report_includes_previous_day_and_current_day_to_now(
    monkeypatch, tmp_path, capsys
):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 6, 12, tzinfo=UTC)

    monkeypatch.setattr(report, "datetime", FixedDateTime)
    journal = tmp_path / "journal.csv"
    write_journal(journal, [
        {"timestamp": "2026-06-05T12:00:00Z", "action": "sell",
         "net_pnl_base": "1", "execution_cost_mode": "net_costs",
         "entry_cost_mode": "net_costs"},
        {"timestamp": "2026-06-06T10:00:00Z", "action": "sell",
         "net_pnl_base": "2", "execution_cost_mode": "net_costs",
         "entry_cost_mode": "net_costs"},
        {"timestamp": "2026-06-06T12:00:00Z", "action": "sell",
         "net_pnl_base": "3", "execution_cost_mode": "net_costs",
         "entry_cost_mode": "net_costs"},
    ])
    config = tmp_path / "paper.env"
    config.write_text(
        "DRY_RUN=true\nHAILO_API_TOKEN=do-not-disclose\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", [
        "public_paper_daily_report.py", "--journal", str(journal),
        "--config", str(config),
    ])

    assert report.main() == 0
    output = capsys.readouterr().out
    result = json.loads(output)

    assert result["primary"]["journal"]["sell_events"] == 1
    assert result["current_day_to_now"]["observation"]["journal"]["sell_events"] == 1
    assert result["current_day_to_now"]["window_utc"] == {
        "start_inclusive": "2026-06-06T00:00:00+00:00",
        "end_exclusive": "2026-06-06T12:00:00+00:00",
    }
    assert "do-not-disclose" not in output


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
    assert result["current_day_to_now"]["observation"]["journal"]["buys"] == 0
    assert result["current_day_to_now"]["observation"]["log"]["parsed_lines_in_window"] == 0
    assert latest.stat().st_mode & 0o077 == 0

    # The wrapper also records a status marker so a non-zero exit or a timer
    # that never starts becomes an alertable gauge.
    status = read_task_status(paper / "reports" / "latest_status.env")
    assert status is not None
    assert task_exit_code(status) == 0
    age = task_last_run_age_seconds(status)
    assert age is not None and 0.0 <= age < 120.0


def test_journal_and_log_report_newest_activity_outside_the_window(tmp_path):
    journal = tmp_path / "journal.csv"
    write_journal(journal, [
        {"timestamp": "2026-06-01T00:00:00Z", "action": "buy"},
        {"timestamp": "2026-06-03T05:00:00Z", "action": "sell"},
        {"timestamp": "bad", "action": "buy"},
    ])
    journal_result = report.journal_report(str(journal), START, END)
    assert journal_result["rows_total"] == 3
    assert journal_result["last_row_timestamp"] == "2026-06-03T05:00:00+00:00"

    log_result = report.log_report(
        "2026-06-04 06:07:08,000 - INFO - 📊 HAILO for BTC: HALTEN\n",
        START, END, ZoneInfo("UTC"),
    )
    assert log_result["parsed_lines_in_window"] == 0
    assert log_result["last_line_timestamp"] == "2026-06-04T06:07:08+00:00"
    assert log_result["source_window_limited"] is False


def test_window_limited_log_source_cannot_prove_staleness():
    result = report.log_report(
        "2026-06-06 02:00:00,000 - INFO - 📊 HAILO for BTC: KAUFEN\n",
        START, END, ZoneInfo("UTC"), window_limited=True,
    )
    assert result["parsed_lines_in_window"] == 1
    assert result["last_line_timestamp"] is None
    assert result["source_window_limited"] is True


@pytest.mark.parametrize(
    "journal_last,log_last,max_hours,expected_status,expected_age,expected_latest",
    [
        ("2026-06-01T00:00:00+00:00", "2026-06-06T10:00:00+00:00", 26, "fresh", 2.0,
         "2026-06-06T10:00:00+00:00"),
        ("2026-06-04T06:00:00+00:00", "2026-06-04T10:00:00+00:00", 26, "stale", 50.0,
         "2026-06-04T10:00:00+00:00"),
        ("2026-06-06T06:00:00+00:00", None, 26, "fresh", 6.0, "2026-06-06T06:00:00+00:00"),
        ("2026-06-04T06:00:00+00:00", "2026-06-04T10:00:00+00:00", None, "unchecked", 50.0,
         "2026-06-04T10:00:00+00:00"),
        (None, None, 26, "unknown", None, None),
    ],
)
def test_staleness_uses_newest_signal(journal_last, log_last, max_hours,
                                      expected_status, expected_age, expected_latest):
    now = datetime(2026, 6, 6, 12, tzinfo=UTC)
    result = report.staleness(
        {"last_row_timestamp": journal_last},
        {"last_line_timestamp": log_last} if log_last else None,
        now, max_hours,
    )
    assert result["status"] == expected_status
    assert result["age_hours"] == expected_age
    assert result["last_activity_timestamp"] == expected_latest


def run_cli(journal, log, extra_args, monkeypatch, now=None):
    if now is not None:
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        monkeypatch.setattr(report, "datetime", FixedDateTime)
    monkeypatch.setattr(sys, "argv", [
        "public_paper_daily_report.py", "--date", "2026-06-06",
        "--journal", str(journal), "--log", str(log), *extra_args,
    ])
    return report.main()


def test_stale_data_fails_the_report_with_exit_one(tmp_path, monkeypatch, capsys):
    journal = tmp_path / "journal.csv"
    write_journal(journal, [{"timestamp": "2026-06-01T00:00:00Z", "action": "buy"}])
    log = tmp_path / "trading_bot.log"
    log.write_text("2026-06-01 21:19:08,000 - INFO - 📊 HAILO for BTC: HALTEN\n", encoding="utf-8")
    now = datetime(2026, 6, 6, 12, tzinfo=UTC)

    assert run_cli(journal, log, ["--max-data-age-hours", "26"], monkeypatch, now) == 1
    captured = capsys.readouterr()
    assert "report failed: no journal or bot log activity since 2026-06-01T21:19:08+00:00" in captured.err
    assert "the collector is not running" in captured.err
    # The report itself stays available for analysis even though the run failed.
    result = json.loads(captured.out)
    assert result["staleness"]["status"] == "stale"
    assert result["staleness"]["age_hours"] == 110.681


def test_stale_report_written_to_output_still_exits_one(tmp_path, monkeypatch, capsys):
    journal = tmp_path / "journal.csv"
    write_journal(journal, [{"timestamp": "2026-06-01T00:00:00Z", "action": "buy"}])
    log = tmp_path / "trading_bot.log"
    log.write_text("2026-06-01 21:19:08,000 - INFO - 📊 HAILO for BTC: HALTEN\n", encoding="utf-8")
    out = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", [
        "public_paper_daily_report.py", "--date", "2026-06-06",
        "--journal", str(journal), "--log", str(log),
        "--max-data-age-hours", "26", "--output", str(out),
    ])

    assert report.main() == 1
    assert "the collector is not running" in capsys.readouterr().err
    assert json.loads(out.read_text(encoding="utf-8"))["staleness"]["status"] == "stale"


def test_fresh_data_and_disabled_gate_keep_exit_zero(tmp_path, monkeypatch, capsys):
    journal = tmp_path / "journal.csv"
    write_journal(journal, [{"timestamp": "2026-06-01T00:00:00Z", "action": "buy"}])
    log = tmp_path / "trading_bot.log"
    log.write_text("2026-06-06 11:00:00,000 - INFO - 📊 HAILO for BTC: HALTEN\n", encoding="utf-8")
    now = datetime(2026, 6, 6, 12, tzinfo=UTC)

    assert run_cli(journal, log, ["--max-data-age-hours", "26"], monkeypatch, now) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["staleness"]["status"] == "fresh"
    assert result["staleness"]["age_hours"] == 1.0
    assert result["staleness"]["last_activity_timestamp"] == "2026-06-06T11:00:00+00:00"

    # Without the flag a backfill of old data must never false-alarm.
    assert run_cli(journal, log, [], monkeypatch, now) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["staleness"]["status"] == "unchecked"
    assert result["staleness"]["max_data_age_hours"] is None


def test_empty_journal_and_log_are_unknown_not_stale(tmp_path, monkeypatch, capsys):
    journal = tmp_path / "journal.csv"
    write_journal(journal, [])
    log = tmp_path / "trading_bot.log"
    log.write_text("", encoding="utf-8")
    now = datetime(2026, 6, 6, 12, tzinfo=UTC)

    assert run_cli(journal, log, ["--max-data-age-hours", "26"], monkeypatch, now) == 0
    assert json.loads(capsys.readouterr().out)["staleness"]["status"] == "unknown"


def test_scheduled_wrapper_keeps_previous_report_when_data_is_stale(tmp_path):
    paper = tmp_path / "paper"
    (paper / "data").mkdir(parents=True)
    write_journal(paper / "data" / "trade_journal.csv",
                  [{"timestamp": "2026-06-01T00:00:00Z", "action": "buy"}])
    (paper / "data" / "trading_bot.log").write_text(
        "2026-06-01 21:19:08,000 - INFO - 📊 HAILO for BTC: HALTEN\n", encoding="utf-8",
    )
    (paper / "paper.env").write_text("DRY_RUN=true\n", encoding="utf-8")
    reports = paper / "reports"
    reports.mkdir()
    (reports / "latest.json").write_text("{\"marker\": \"previous\"}", encoding="utf-8")
    env = {
        **os.environ,
        "PUBLIC_PAPER_DIR": str(paper),
        "PYTHON_BIN": sys.executable,
        "PUBLIC_PAPER_MAX_DATA_AGE_HOURS": "26",
    }
    completed = subprocess.run(["bash", str(ROOT / "scripts/run_public_paper_daily_report.sh")],
                               env=env, check=False, capture_output=True, text=True)
    assert completed.returncode != 0
    assert "the collector is not running" in completed.stderr
    assert json.loads((reports / "latest.json").read_text(encoding="utf-8"))["marker"] == "previous"

    # A failed attempt still records its exit code, so the failure is alertable
    # even though the previous report is deliberately kept in place.
    status = read_task_status(reports / "latest_status.env")
    assert status is not None
    assert task_exit_code(status) == completed.returncode
    assert completed.returncode != 0
    assert (reports / "latest_status.env").stat().st_mode & 0o077 == 0
