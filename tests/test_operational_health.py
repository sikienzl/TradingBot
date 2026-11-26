from __future__ import annotations

import importlib
import json
import shutil
import sys
from datetime import UTC, datetime, timedelta
from urllib.error import URLError

from scripts.export_operational_health import (
    main,
    query_database_size,
    render_metrics,
    write_metrics,
)


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_render_metrics_reports_kill_switch_hailo_and_fresh_signal(tmp_path):
    marker = tmp_path / "STOP"
    marker.touch()
    now = datetime.now(UTC)
    signal_path = tmp_path / "signal.json"
    signal_path.write_text(json.dumps({
        "timestamp_utc": (now - timedelta(seconds=4)).isoformat(),
        "news_items": [{"fetched_at": (now - timedelta(seconds=2)).isoformat()}],
    }), encoding="utf-8")

    metrics = render_metrics(
        marker, "http://localhost:18001/health", signal_path,
        tmp_path / "trade_journal.csv", tmp_path / "trading_bot.log",
        now=now, health_probe=lambda *_args, **_kwargs: _Response(),
    )

    assert "trading_kill_switch_active 1" in metrics
    assert "trading_hailo_health_check_enabled 1" in metrics
    assert "trading_hailo_service_up 1" in metrics
    assert "trading_research_signal_present 1" in metrics
    assert "trading_research_signal_age_seconds 2.000" in metrics


def test_render_metrics_reports_hailo_failure_and_missing_signal(tmp_path):
    def fail_probe(*_args, **_kwargs):
        raise URLError("unavailable")

    metrics = render_metrics(
        tmp_path / "missing-stop", "http://localhost:18001/health",
        tmp_path / "missing.json", tmp_path / "missing_journal.csv",
        tmp_path / "missing_bot.log", health_probe=fail_probe,
    )

    assert "trading_kill_switch_active 0" in metrics
    assert "trading_hailo_service_up 0" in metrics
    assert "trading_research_signal_present 0" in metrics
    assert "trading_research_signal_age_seconds" not in metrics
    assert "trading_journal_present 0" in metrics
    assert "trading_journal_age_seconds" not in metrics
    assert "trading_bot_log_present 0" in metrics
    assert "trading_bot_log_age_seconds" not in metrics
    assert "trading_market_data_present" not in metrics


def test_render_metrics_reports_data_staleness(tmp_path):
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    journal = tmp_path / "trade_journal.csv"
    journal.write_text(
        "timestamp,iteration,coin,action,price\n"
        "2026-11-25T11:50:00+00:00,7,BTC/EUR,buy,10\n", encoding="utf-8")
    bot_log = tmp_path / "trading_bot.log"
    bot_log.write_text("2026-11-25 11:59:00,500 - INFO - hello\n", encoding="utf-8")
    market = tmp_path / "market"
    market.mkdir()
    (market / "tickers.csv").write_text(
        "timestamp_utc,symbol,price\n"
        "2026-11-25T11:59:30+00:00,BTC/EUR,10\n", encoding="utf-8")

    metrics = render_metrics(
        tmp_path / "STOP", "", tmp_path / "missing.json", journal, bot_log, market,
        now=now, health_probe=lambda *_args, **_kwargs: _Response(),
    )

    assert "trading_journal_present 1" in metrics
    assert "trading_journal_age_seconds 600.000" in metrics
    assert "trading_bot_log_present 1" in metrics
    assert "trading_bot_log_age_seconds 59.500" in metrics
    assert "trading_market_data_present 1" in metrics
    assert "trading_market_data_age_seconds 30.000" in metrics


def test_render_metrics_shifts_log_age_with_configured_timezone(tmp_path):
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    bot_log = tmp_path / "trading_bot.log"
    bot_log.write_text("2026-11-25 11:59:00 - INFO - hello\n", encoding="utf-8")

    metrics = render_metrics(
        tmp_path / "STOP", "", tmp_path / "missing.json",
        tmp_path / "missing_journal.csv", bot_log,
        log_timezone="Europe/Berlin", now=now,
        health_probe=lambda *_args, **_kwargs: _Response(),
    )

    assert "trading_bot_log_age_seconds 3660.000" in metrics


def test_write_metrics_replaces_output_atomically(tmp_path):
    target = tmp_path / "nested" / "health.prom"
    write_metrics(target, "trading_kill_switch_active 0\n")
    assert target.read_text(encoding="utf-8") == "trading_kill_switch_active 0\n"
    assert list(target.parent.iterdir()) == [target]


def test_main_uses_configured_staleness_paths(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    journal = data_dir / "trade_journal.csv"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        "timestamp,iteration,coin,action,price\n"
        "2026-11-25T11:50:00+00:00,7,BTC/EUR,buy,10\n", encoding="utf-8")
    bot_log = tmp_path / "logs" / "bot.log"
    bot_log.parent.mkdir()
    bot_log.write_text("2026-11-25 11:59:00 - INFO - hello\n", encoding="utf-8")
    market = tmp_path / "market"
    market.mkdir()
    (market / "tickers.csv").write_text(
        "timestamp_utc,symbol,price\n2026-11-25T11:59:30+00:00,BTC/EUR,10\n",
        encoding="utf-8")
    output = tmp_path / "textfile" / "trading_health.prom"

    monkeypatch.setenv("TRADING_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TRADING_JOURNAL_FILE", str(journal))
    monkeypatch.setenv("TRADING_BOT_LOG_FILE", str(bot_log))
    monkeypatch.setenv("TRADING_MARKET_DATA_DIR", str(market))
    monkeypatch.setenv("TRADING_LOG_TIMEZONE", "UTC")
    monkeypatch.setenv("TRADING_KILL_SWITCH_FILE", str(tmp_path / "STOP"))
    monkeypatch.setenv("RESEARCH_SIGNAL_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("HAILO_HEALTH_URL", "")
    monkeypatch.delenv("TRADING_TASK_STATUS_FILES", raising=False)
    monkeypatch.delenv("TRADING_DISK_PATHS", raising=False)
    monkeypatch.delenv("ANALYTICS_DB_URL", raising=False)
    monkeypatch.setenv("TRADING_HEALTH_METRICS_FILE", str(output))

    assert main() == 0

    body = output.read_text(encoding="utf-8")
    assert "trading_journal_present 1" in body
    assert "trading_bot_log_present 1" in body
    assert "trading_market_data_present 1" in body
    assert "trading_market_data_age_seconds" in body


def test_main_omits_market_metrics_when_unset(tmp_path, monkeypatch):
    output = tmp_path / "textfile" / "trading_health.prom"
    monkeypatch.setenv("TRADING_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADING_KILL_SWITCH_FILE", str(tmp_path / "STOP"))
    monkeypatch.setenv("RESEARCH_SIGNAL_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("HAILO_HEALTH_URL", "")
    monkeypatch.delenv("TRADING_MARKET_DATA_DIR", raising=False)
    monkeypatch.delenv("TRADING_JOURNAL_FILE", raising=False)
    monkeypatch.delenv("TRADING_BOT_LOG_FILE", raising=False)
    monkeypatch.delenv("TRADING_LOG_TIMEZONE", raising=False)
    monkeypatch.delenv("TRADING_TASK_STATUS_FILES", raising=False)
    monkeypatch.delenv("TRADING_DISK_PATHS", raising=False)
    monkeypatch.delenv("ANALYTICS_DB_URL", raising=False)
    monkeypatch.setenv("TRADING_HEALTH_METRICS_FILE", str(output))

    assert main() == 0

    body = output.read_text(encoding="utf-8")
    # Defaults resolve under TRADING_DATA_DIR, and an unconfigured recorder
    # publishes no market metric at all rather than a false absence.
    assert "trading_journal_present 0" in body
    assert "trading_bot_log_present 0" in body
    assert "trading_market_data" not in body


def _base_kwargs(tmp_path, now):
    return {
        "research_signal_file": tmp_path / "missing.json",
        "journal_file": tmp_path / "missing_journal.csv",
        "bot_log_file": tmp_path / "missing_bot.log",
        "now": now,
        "health_probe": lambda *_args, **_kwargs: _Response(),
    }


def test_render_metrics_exports_failed_task_marker(tmp_path):
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    marker = tmp_path / "latest_status.env"
    marker.write_text(
        f"exit_code=3\nstarted_unixtime={now.timestamp() - 90:.0f}\n"
        f"finished_unixtime={now.timestamp() - 60:.0f}\n",
        encoding="utf-8",
    )

    metrics = render_metrics(
        tmp_path / "STOP", "",
        task_status_files={"daily_review": marker, "public_paper": tmp_path / "gone.env"},
        **_base_kwargs(tmp_path, now),
    )

    assert 'trading_task_status_present{task="daily_review"} 1' in metrics
    assert 'trading_task_last_exit_code{task="daily_review"} 3' in metrics
    assert 'trading_task_last_run_age_seconds{task="daily_review"} 60.000' in metrics
    # A listed task whose marker is missing is reported as absent, and no
    # exit-code or age gauge is invented for it.
    assert 'trading_task_status_present{task="public_paper"} 0' in metrics
    assert 'task="public_paper"' in metrics
    assert metrics.count('trading_task_last_exit_code{task="public_paper"}') == 0
    assert "trading_task_last_run_age_seconds" in metrics


def test_render_metrics_ignores_unlisted_tasks_and_broken_markers(tmp_path):
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    broken = tmp_path / "broken.env"
    broken.write_text("exit_code=not-a-number\n", encoding="utf-8")

    metrics = render_metrics(
        tmp_path / "STOP", "",
        task_status_files={"broken": broken},
        **_base_kwargs(tmp_path, now),
    )

    assert 'trading_task_status_present{task="broken"} 1' in metrics
    # An unparseable exit code is exported as 0 so the failure alert cannot fire
    # on garbage, while the missing finish time suppresses the age gauge.
    assert 'trading_task_last_exit_code{task="broken"} 0' in metrics
    assert "trading_task_last_run_age_seconds" not in metrics

    unlisted = render_metrics(tmp_path / "STOP", "", **_base_kwargs(tmp_path, now))
    assert "trading_task_status_present" not in unlisted


def test_render_metrics_exports_disk_capacity(tmp_path):
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    usage = shutil.disk_usage(str(tmp_path))
    expected_percent = 100.0 * usage.used / usage.total

    metrics = render_metrics(
        tmp_path / "STOP", "",
        disk_paths=[str(tmp_path), " ", str(tmp_path / "definitely-missing")],
        **_base_kwargs(tmp_path, now),
    )

    label = f'{{path="{tmp_path}"}}'
    assert f"trading_disk_total_bytes{label} {usage.total}" in metrics
    assert f"trading_disk_free_bytes{label} {usage.free}" in metrics
    assert f"trading_disk_used_percent{label} {expected_percent:.4f}" in metrics
    # Blank and unreadable paths publish nothing instead of a false zero.
    assert metrics.count("trading_disk_total_bytes") == 3  # HELP, TYPE, sample
    assert 'path=" "' not in metrics
    assert "definitely-missing" not in metrics


def test_render_metrics_exports_analytics_database_size(tmp_path):
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    probes = []

    def probe(url):
        probes.append(url)
        return 2048

    metrics = render_metrics(
        tmp_path / "STOP", "", analytics_db_url="postgresql://localhost/analytics",
        database_probe=probe, **_base_kwargs(tmp_path, now),
    )

    assert probes == ["postgresql://localhost/analytics"]
    assert "trading_analytics_database_enabled 1" in metrics
    assert "trading_analytics_database_up 1" in metrics
    assert "trading_analytics_database_size_bytes 2048" in metrics


def test_render_metrics_reports_unreachable_analytics_database(tmp_path):
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    metrics = render_metrics(
        tmp_path / "STOP", "", analytics_db_url="postgresql://localhost/analytics",
        database_probe=lambda _url: None, **_base_kwargs(tmp_path, now),
    )

    assert "trading_analytics_database_enabled 1" in metrics
    assert "trading_analytics_database_up 0" in metrics
    # No size gauge at all: absence is not the same as a zero-byte database.
    assert "trading_analytics_database_size_bytes" not in metrics


def test_render_metrics_omits_database_metrics_without_url(tmp_path):
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)

    def fail_probe(_url):
        raise AssertionError("must not be called without a configured URL")

    metrics = render_metrics(
        tmp_path / "STOP", "", analytics_db_url="   ", database_probe=fail_probe,
        **_base_kwargs(tmp_path, now),
    )

    assert "trading_analytics_database" not in metrics


def test_query_database_size_without_psycopg(monkeypatch):
    real_import = importlib.import_module

    def blocked(name, *args, **kwargs):
        if name == "psycopg":
            raise ImportError("psycopg is not a Pi dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", blocked)
    assert query_database_size("postgresql://localhost/analytics") is None


def test_query_database_size_reports_server_errors(monkeypatch):
    class _FailingConnection:
        def __init__(self, *_args, **_kwargs):
            raise OSError("connection refused")

        def __enter__(self):
            raise AssertionError("unreachable")

    class _Module:
        connect = staticmethod(_FailingConnection)

    monkeypatch.setitem(sys.modules, "psycopg", _Module())
    assert query_database_size("postgresql://localhost/analytics") is None


def test_query_database_size_reads_the_query_result(monkeypatch):
    class _Result:
        @staticmethod
        def fetchone():
            return (4096,)

    class _Connection:
        @staticmethod
        def execute(_sql):
            return _Result()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class _Module:
        @staticmethod
        def connect(_url, **_kwargs):
            return _Connection()

    monkeypatch.setitem(sys.modules, "psycopg", _Module())
    assert query_database_size("postgresql://localhost/analytics") == 4096


def test_query_database_size_rejects_empty_result(monkeypatch):
    class _Result:
        @staticmethod
        def fetchone():
            return None

    class _Connection:
        @staticmethod
        def execute(_sql):
            return _Result()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class _Module:
        @staticmethod
        def connect(_url, **_kwargs):
            return _Connection()

    monkeypatch.setitem(sys.modules, "psycopg", _Module())
    assert query_database_size("postgresql://localhost/analytics") is None


def test_main_exports_task_status_disk_and_database(tmp_path, monkeypatch):
    marker = tmp_path / "results" / "daily_review" / "latest_status.env"
    marker.parent.mkdir(parents=True)
    marker.write_text("exit_code=1\nfinished_unixtime=1764000000\n", encoding="utf-8")
    output = tmp_path / "textfile" / "trading_health.prom"

    monkeypatch.setenv("TRADING_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADING_KILL_SWITCH_FILE", str(tmp_path / "STOP"))
    monkeypatch.setenv("RESEARCH_SIGNAL_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("HAILO_HEALTH_URL", "")
    monkeypatch.delenv("TRADING_MARKET_DATA_DIR", raising=False)
    monkeypatch.setenv(
        "TRADING_TASK_STATUS_FILES", f"daily_review={marker}, broken_entry")
    monkeypatch.setenv("TRADING_DISK_PATHS", f"{tmp_path},missing-mount")
    monkeypatch.setenv("ANALYTICS_DB_URL", "")
    monkeypatch.setenv("TRADING_HEALTH_METRICS_FILE", str(output))

    assert main() == 0

    body = output.read_text(encoding="utf-8")
    assert 'trading_task_status_present{task="daily_review"} 1' in body
    assert 'trading_task_last_exit_code{task="daily_review"} 1' in body
    assert 'trading_task_last_run_age_seconds{task="daily_review"}' in body
    assert "trading_disk_total_bytes" in body
    assert "missing-mount" not in body
    assert "trading_analytics_database" not in body


def test_main_defaults_disk_capacity_to_data_dir(tmp_path, monkeypatch):
    output = tmp_path / "textfile" / "trading_health.prom"
    monkeypatch.setenv("TRADING_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADING_KILL_SWITCH_FILE", str(tmp_path / "STOP"))
    monkeypatch.setenv("RESEARCH_SIGNAL_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setenv("HAILO_HEALTH_URL", "")
    monkeypatch.delenv("TRADING_MARKET_DATA_DIR", raising=False)
    monkeypatch.delenv("TRADING_TASK_STATUS_FILES", raising=False)
    monkeypatch.delenv("TRADING_DISK_PATHS", raising=False)
    monkeypatch.delenv("ANALYTICS_DB_URL", raising=False)
    monkeypatch.setenv("TRADING_HEALTH_METRICS_FILE", str(output))

    assert main() == 0

    body = output.read_text(encoding="utf-8")
    # The Pi profile gets capacity coverage without extra configuration.
    assert f'trading_disk_total_bytes{{path="{tmp_path}"}}' in body
    assert "trading_task_status_present" not in body
