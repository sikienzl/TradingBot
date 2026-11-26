"""Guard the Prometheus alert rules against the metrics we actually export.

An alert that references a metric name no exporter emits never fires, which is
worse than no alert at all because it looks like coverage. These tests parse
``deploy/trading-alerts.yml`` with a minimal line parser (no YAML dependency)
and check the scheduled-task and capacity rules against the real output of
``scripts/export_operational_health.py``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from scripts.export_operational_health import render_metrics

ROOT = Path(__file__).resolve().parents[1]
ALERTS_FILE = ROOT / "deploy" / "trading-alerts.yml"

_ALERT_RE = re.compile(r"^\s*-\s+alert:\s+(\S+)\s*$", re.MULTILINE)
_EXPR_RE = re.compile(r"^\s*expr:\s+(.*?)\s*$")
_IDENTIFIER_RE = re.compile(r"\btrading_[a-z0-9_]+\b")


def parse_alerts(path: Path = ALERTS_FILE) -> dict[str, str]:
    """Map each alert name to its PromQL expression."""
    alerts: dict[str, str] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        alert_match = _ALERT_RE.match(line)
        if alert_match:
            current = alert_match.group(1)
            continue
        expr_match = _EXPR_RE.match(line)
        if expr_match and current is not None:
            alerts[current] = expr_match.group(1)
            current = None
    return alerts


def exported_metric_names(tmp_path: Path) -> set[str]:
    """Metric families the operational-health exporter really emits."""
    marker = tmp_path / "latest_status.env"
    marker.write_text(
        "exit_code=0\nfinished_unixtime=1764000000\n", encoding="utf-8")
    body = render_metrics(
        tmp_path / "STOP", "http://localhost:18001/health",
        tmp_path / "signal.json", tmp_path / "journal.csv", tmp_path / "bot.log",
        tmp_path / "market",
        task_status_files={"daily_review": marker},
        disk_paths=[str(tmp_path)],
        analytics_db_url="postgresql://localhost/analytics",
        database_probe=lambda _url: 1024,
        now=datetime(2026, 11, 25, 12, 0, tzinfo=UTC),
        health_probe=lambda *_args, **_kwargs: _Ok(),
    )
    return set(re.findall(r"^# TYPE (\S+) gauge$", body, re.MULTILINE))


class _Ok:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


NEW_ALERTS = {
    "TradingScheduledTaskFailed": "trading_task_last_exit_code != 0",
    "TradingScheduledTaskMarkerMissing": "trading_task_status_present == 0",
    "TradingScheduledTaskStale": "trading_task_last_run_age_seconds > 172800",
    "TradingDiskSpaceLow": "trading_disk_free_bytes < 1073741824",
    "TradingDiskUsedPercentHigh": "trading_disk_used_percent > 90",
    "TradingAnalyticsDatabaseUnavailable": (
        "trading_analytics_database_enabled == 1 "
        "and trading_analytics_database_up == 0"
    ),
}


def test_new_alerts_exist_with_expected_expressions() -> None:
    alerts = parse_alerts()
    for name, expr in NEW_ALERTS.items():
        assert alerts[name] == expr


def test_new_alerts_only_reference_exported_metrics(tmp_path) -> None:
    exported = exported_metric_names(tmp_path)
    alerts = parse_alerts()
    for name in NEW_ALERTS:
        used = set(_IDENTIFIER_RE.findall(alerts[name]))
        assert used, f"{name} references no trading metric"
        unknown = used - exported
        assert not unknown, f"{name} references unexported metrics: {sorted(unknown)}"


def test_alert_rules_stay_parsable_and_unique() -> None:
    alerts = parse_alerts()
    text = ALERTS_FILE.read_text(encoding="utf-8")
    declared = _ALERT_RE.findall(text)
    assert len(declared) >= 20
    # Every declared alert has exactly one expression and no name is reused.
    assert sorted(alerts) == sorted(declared)
    assert len(set(declared)) == len(declared)
    for name, expr in alerts.items():
        assert expr, f"{name} has no expression"
        assert expr.count("(") == expr.count(")"), f"{name} has unbalanced parentheses"
