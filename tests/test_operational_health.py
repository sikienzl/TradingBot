from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.error import URLError

from scripts.export_operational_health import main, render_metrics, write_metrics


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
    monkeypatch.setenv("TRADING_HEALTH_METRICS_FILE", str(output))

    assert main() == 0

    body = output.read_text(encoding="utf-8")
    # Defaults resolve under TRADING_DATA_DIR, and an unconfigured recorder
    # publishes no market metric at all rather than a false absence.
    assert "trading_journal_present 0" in body
    assert "trading_bot_log_present 0" in body
    assert "trading_market_data" not in body
