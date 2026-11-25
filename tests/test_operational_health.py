from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.error import URLError

from scripts.export_operational_health import render_metrics, write_metrics


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
        tmp_path / "missing.json", health_probe=fail_probe,
    )

    assert "trading_kill_switch_active 0" in metrics
    assert "trading_hailo_service_up 0" in metrics
    assert "trading_research_signal_present 0" in metrics
    assert "trading_research_signal_age_seconds" not in metrics


def test_write_metrics_replaces_output_atomically(tmp_path):
    target = tmp_path / "nested" / "health.prom"
    write_metrics(target, "trading_kill_switch_active 0\n")
    assert target.read_text(encoding="utf-8") == "trading_kill_switch_active 0\n"
    assert list(target.parent.iterdir()) == [target]
