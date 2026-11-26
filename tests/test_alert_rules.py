"""Guard the Prometheus alert rules against the metrics we actually export.

An alert that references a metric name no exporter emits never fires, which is
worse than no alert at all because it looks like coverage. These tests parse
``deploy/trading-alerts.yml`` with a minimal line parser (no YAML dependency)
and check the scheduled-task and capacity rules against the real output of
``scripts/export_operational_health.py``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.export_operational_health import render_metrics
from src.scorecard_gate import scorecard_gate_settings

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


def render(tmp_path: Path, gate_env: dict[str, str], *, dry_run: bool) -> str:
    """Operational-health exporter output for one deployment configuration."""
    marker = tmp_path / "latest_status.env"
    marker.write_text(
        "exit_code=0\nfinished_unixtime=1764000000\n", encoding="utf-8")
    return render_metrics(
        tmp_path / "STOP", "http://localhost:18001/health",
        tmp_path / "signal.json", tmp_path / "journal.csv", tmp_path / "bot.log",
        tmp_path / "market",
        task_status_files={"daily_review": marker},
        disk_paths=[str(tmp_path)],
        analytics_db_url="postgresql://localhost/analytics",
        database_probe=lambda _url: 1024,
        now=datetime(2026, 11, 25, 12, 0, tzinfo=UTC),
        health_probe=lambda *_args, **_kwargs: _Ok(),
        scorecard_gate=scorecard_gate_settings(gate_env),
        deployment_env_file=write_deployment_env(tmp_path, dry_run=dry_run),
        fill_report_file=write_fill_marker(tmp_path),
    )


def write_fill_marker(tmp_path: Path, *, age: timedelta = timedelta(minutes=10)) -> Path:
    """Fill reconciliation marker as ``scripts/reconcile_fills.py`` writes it."""
    stamp = datetime(2026, 11, 25, 12, 0, tzinfo=UTC) - age
    marker = tmp_path / "fill_reconciliation.env"
    marker.write_text(
        "SCHEMA_VERSION=1\nSTATUS=complete\nREASON=reconciled\n"
        f"CHECKED_AT_UNIXTIME={int(stamp.timestamp())}\nLOOKBACK_DAYS=30\n"
        "JOURNAL_ROWS=4\nORDERS=4\nMATCHED=3\nPARTIAL_FILLS=1\nUNMATCHED=0\n"
        "NOT_CHECKABLE=0\nQUANTITY_MISMATCH=0\nFEE_MISMATCH=0\nEXCHANGE_CALLS=2\n"
        "EXCHANGE_ERRORS=0\nJOURNAL_QUANTITY_BASE=1.00000000\n"
        "EXCHANGE_QUANTITY_BASE=1.00000000\nJOURNAL_FEE_BASE=2.00000000\n"
        "EXCHANGE_FEE_BASE=2.00000000\n",
        encoding="utf-8",
    )
    return marker


def exported_metric_names(tmp_path: Path) -> set[str]:
    """Metric families the operational-health exporter really emits."""
    body = render(
        tmp_path,
        {"SCORECARD_STATUS_JSON": str(tmp_path / "absent.json")},
        dry_run=False,
    )
    return set(re.findall(r"^# TYPE (\S+) gauge$", body, re.MULTILINE))


def gauge_values(body: str) -> dict[str, float]:
    """Sample values emitted by the exporter, keyed by metric name."""
    return {
        match.group(1): float(match.group(2))
        for match in re.finditer(
            r"^(trading_\S+)(?: \S+)? ([0-9.eE+-]+)$", body, re.MULTILINE
        )
    }


def write_deployment_env(tmp_path: Path, *, dry_run: bool) -> Path:
    """The deployment ``.env`` the exporter reads ``DRY_RUN`` from."""
    env_file = tmp_path / "deployment.env"
    env_file.write_text(
        f"DRY_RUN={'true' if dry_run else 'false'}\n", encoding="utf-8")
    return env_file


def write_scorecard_marker(tmp_path: Path, *, verdict: str, age: timedelta) -> Path:
    """A weekly scorecard marker as ``scripts/run_weekly_scorecard.sh`` writes it."""
    stamp = datetime(2026, 11, 25, 12, 0, tzinfo=UTC) - age
    marker = tmp_path / "latest_scorecard.env"
    marker.write_text(
        f"TIMESTAMP_UTC={stamp.isoformat()}\nRUN_MODE=standard\nVERDICT={verdict}\n",
        encoding="utf-8",
    )
    return marker


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
    "TradingTuningCycleApplyBlocked": (
        'trading_task_last_exit_code{task="weekly_tuning"} == 3'
    ),
    "TradingDiskSpaceLow": "trading_disk_free_bytes < 1073741824",
    "TradingDiskUsedPercentHigh": "trading_disk_used_percent > 90",
    "TradingAnalyticsDatabaseUnavailable": (
        "trading_analytics_database_enabled == 1 "
        "and trading_analytics_database_up == 0"
    ),
    "TradingLiveModeScorecardGateBlocked": (
        "trading_scorecard_gate_enabled == 1 "
        "and trading_scorecard_gate_live_enabled == 1 "
        "and trading_scorecard_gate_allowed == 0"
    ),
    "TradingLiveModeWithoutScorecardGate": (
        "trading_scorecard_gate_enabled == 0 "
        "and trading_scorecard_gate_live_enabled == 1"
    ),
    "TradingFillReconciliationMismatch": "trading_fill_reconciliation_mismatches > 0",
    "TradingFillReconciliationIncomplete": (
        "trading_fill_reconciliation_enabled == 1 "
        "and trading_fill_reconciliation_complete == 0"
    ),
    "TradingFillReconciliationStale": (
        "trading_fill_reconciliation_enabled == 1 "
        "and trading_fill_reconciliation_age_seconds > 10800"
    ),
    "TradingFillReconciliationMarkerMissing": (
        "trading_fill_reconciliation_enabled == 1 "
        "and trading_fill_reconciliation_present == 0"
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


def gate_env(tmp_path: Path, marker: Path | None) -> dict[str, str]:
    """Gate settings pointing at the marker under test, nothing else."""
    absent = str(tmp_path / "absent.json")
    return {
        "SCORECARD_STATUS_JSON": marker if marker is not None else absent,
        "SCORECARD_STATUS_ENV": absent,
    }


def test_live_gate_gauges_report_blocked_when_marker_missing(tmp_path) -> None:
    body = render(tmp_path, gate_env(tmp_path, None), dry_run=False)
    values = gauge_values(body)
    assert values["trading_scorecard_gate_enabled"] == 1
    assert values["trading_scorecard_gate_live_enabled"] == 1
    assert values["trading_scorecard_gate_allowed"] == 0
    assert "trading_scorecard_gate_age_seconds" not in values


def test_live_gate_gauges_report_blocked_for_stale_go(tmp_path) -> None:
    marker = write_scorecard_marker(
        tmp_path, verdict="GO", age=timedelta(days=10))
    values = gauge_values(render(tmp_path, gate_env(tmp_path, str(marker)),
                                 dry_run=False))
    assert values["trading_scorecard_gate_allowed"] == 0
    assert values["trading_scorecard_gate_age_seconds"] >= 864000


def test_live_gate_gauges_report_allowed_for_fresh_go(tmp_path) -> None:
    marker = write_scorecard_marker(
        tmp_path, verdict="GO", age=timedelta(hours=2))
    values = gauge_values(render(tmp_path, gate_env(tmp_path, str(marker)),
                                 dry_run=False))
    assert values["trading_scorecard_gate_allowed"] == 1
    assert 7000 < values["trading_scorecard_gate_age_seconds"] < 7300


def test_dry_run_profile_cannot_match_live_alerts(tmp_path) -> None:
    """A simulated deployment publishes live_enabled 0, so both alerts stay off."""
    body = render(tmp_path, gate_env(tmp_path, None), dry_run=True)
    values = gauge_values(body)
    assert values["trading_scorecard_gate_live_enabled"] == 0
    assert values["trading_scorecard_gate_allowed"] == 0


def test_disabled_gate_is_visible_as_gate_enabled_zero(tmp_path) -> None:
    env = gate_env(tmp_path, None)
    env["SCORECARD_GATE_ENABLED"] = "false"
    values = gauge_values(render(tmp_path, env, dry_run=False))
    assert values["trading_scorecard_gate_enabled"] == 0
    assert values["trading_scorecard_gate_live_enabled"] == 1


def fill_render(tmp_path: Path, marker: Path | None, *, dry_run: bool) -> dict[str, float]:
    """Gauge values for one fill reconciliation marker state."""
    body = render_metrics(
        tmp_path / "STOP", "", tmp_path / "signal.json", tmp_path / "journal.csv",
        tmp_path / "bot.log",
        now=datetime(2026, 11, 25, 12, 0, tzinfo=UTC),
        scorecard_gate=scorecard_gate_settings(gate_env(tmp_path, None)),
        deployment_env_file=write_deployment_env(tmp_path, dry_run=dry_run),
        fill_report_file=marker,
    )
    return gauge_values(body)


def test_fill_reconciliation_gauges_track_a_clean_recent_pass(tmp_path) -> None:
    marker = write_fill_marker(tmp_path)
    values = fill_render(tmp_path, marker, dry_run=False)
    assert values["trading_fill_reconciliation_enabled"] == 1
    assert values["trading_fill_reconciliation_present"] == 1
    assert values["trading_fill_reconciliation_complete"] == 1
    assert values["trading_fill_reconciliation_mismatches"] == 0
    assert 500 < values["trading_fill_reconciliation_age_seconds"] < 700


def test_fill_reconciliation_gauges_count_mismatches(tmp_path) -> None:
    """``quantity_mismatch`` already includes every unmatched order.

    ``src.fill_reconciliation`` increments it for a partial fill, an overfill, a
    cancelled order the journal booked, and an order the exchange does not know
    at all, so adding ``UNMATCHED`` again would alert on one order twice.
    """
    marker = tmp_path / "fill.env"
    marker.write_text(
        "STATUS=complete\nCHECKED_AT_UNIXTIME=1764079200\n"
        "QUANTITY_MISMATCH=3\nFEE_MISMATCH=2\nUNMATCHED=1\n",
        encoding="utf-8",
    )
    values = fill_render(tmp_path, marker, dry_run=False)
    assert values["trading_fill_reconciliation_mismatches"] == 5


def test_fill_reconciliation_incomplete_pass_is_reported(tmp_path) -> None:
    marker = tmp_path / "fill.env"
    marker.write_text(
        "STATUS=incomplete\nREASON=no authenticated exchange\n"
        "CHECKED_AT_UNIXTIME=1764079200\n",
        encoding="utf-8",
    )
    values = fill_render(tmp_path, marker, dry_run=False)
    assert values["trading_fill_reconciliation_complete"] == 0
    assert "trading_fill_reconciliation_mismatches" not in values


def test_paper_profile_cannot_match_fill_reconciliation_alerts(tmp_path) -> None:
    """A dry-run deployment has no exchange side, so it publishes enabled 0."""
    values = fill_render(tmp_path, None, dry_run=True)
    assert values["trading_fill_reconciliation_enabled"] == 0
    assert values["trading_fill_reconciliation_present"] == 0
    assert "trading_fill_reconciliation_complete" not in values


def test_missing_fill_marker_only_publishes_presence(tmp_path) -> None:
    values = fill_render(tmp_path, tmp_path / "absent.env", dry_run=False)
    assert values["trading_fill_reconciliation_present"] == 0
    assert "trading_fill_reconciliation_age_seconds" not in values


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
