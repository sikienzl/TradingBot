"""Guard the Prometheus alert rules against the metrics we actually export.

An alert that references a metric name no exporter emits never fires, which is
worse than no alert at all because it looks like coverage. These tests parse
``deploy/trading-alerts.yml`` with a minimal line parser (no YAML dependency)
and check the scheduled-task and capacity rules against the real output of
``scripts/export_operational_health.py``.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.export_operational_health import render_metrics
from src.scorecard_gate import scorecard_gate_settings

ROOT = Path(__file__).resolve().parents[1]
ALERTS_FILE = ROOT / "deploy" / "trading-alerts.yml"
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)

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


def render(
    tmp_path: Path,
    gate_env: dict[str, str],
    *,
    dry_run: bool,
    model_release_root: str | None = None,
) -> str:
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
        now=NOW,
        health_probe=lambda *_args, **_kwargs: _Ok(),
        scorecard_gate=scorecard_gate_settings(gate_env),
        deployment_env_file=write_deployment_env(tmp_path, dry_run=dry_run),
        fill_report_file=write_fill_marker(tmp_path),
        retention_report_file=write_retention_marker(tmp_path),
        inference_stats_file=write_inference_stats_marker(tmp_path),
        model_release_root=(
            str(write_release_root(tmp_path))
            if model_release_root is None else model_release_root
        ),
    )


def write_release_root(
    tmp_path: Path, *, release_id: str = "release-1", tamper: str | None = None
) -> Path:
    """Promote a paired model release into a throwaway root and return that root.

    Built with the real promotion command so the exporter is tested against a
    manifest the runtime would actually accept, not a hand-written one.
    """
    from src.model_release import promote_release

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "cpu.cbm").write_bytes(b"cpu model")
    (artifacts / "model.hef").write_bytes(b"hailo model")
    validation = {
        "rows": 250,
        "accuracy": 0.8,
        "macro_f1": 0.7,
        "threshold_macro_f1": 0.68,
        "threshold_coverage": 0.62,
    }
    label_map = {"verkaufen": 0, "halten": 1, "kaufen": 2}
    (artifacts / "cpu.json").write_text(json.dumps({
        "feature_contract": "hailo",
        "features": ["rsi", "macd"],
        "label_map": label_map,
        "training_data_sha256": "a" * 64,
        "label_horizon": 6,
        "train_rows": 1000,
        "validation_rows": 250,
        "validation": validation,
    }), encoding="utf-8")
    (artifacts / "hailo.json").write_text(json.dumps({
        "feature_contract": "hailo",
        "features": ["rsi", "macd"],
        "label_map": label_map,
        "data_provenance": {
            "training_data_sha256": "a" * 64,
            "label_map": label_map,
            "label_horizon": 6,
            "train_rows": 1000,
            "validation_rows": 250,
        },
        "validation": validation,
    }), encoding="utf-8")
    root = tmp_path / "models"
    release = promote_release(
        root, release_id,
        artifacts / "cpu.cbm", artifacts / "cpu.json",
        artifacts / "model.hef", artifacts / "hailo.json",
    )
    if tamper == "checksum":
        (release / "cpu" / "catboost_model.cbm").write_bytes(b"swapped")
    elif tamper == "missing":
        (release / "hailo" / "model.hef").unlink()
    return root


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


def write_retention_marker(
    tmp_path: Path, *, age: timedelta = timedelta(hours=9)
) -> Path:
    """Retention run summary as ``scripts/prune_jsonl_stores.py`` writes it.

    The store paths point at real files in ``tmp_path`` because the exporter
    reports the current size of each store, not a size from the marker. The
    ``news_bytes``/``verdict_bytes`` counters in the marker deliberately say 20
    while the files are 19 bytes, so a test can tell the two apart.
    """
    stamp = datetime(2026, 11, 25, 12, 0, tzinfo=UTC) - age
    news_store = tmp_path / "research_news_items.jsonl"
    news_store.write_text('{"record_id":"n1"}\n', encoding="utf-8")
    verdict_store = tmp_path / "verdict_history.jsonl"
    verdict_store.write_text('{"record_id":"v1"}\n', encoding="utf-8")
    marker = tmp_path / "retention.env"
    marker.write_text(
        "schema_version=1\nstatus=complete\nreason=ok\nexit_code=0\n"
        f"started_unixtime={int(stamp.timestamp())}\n"
        f"finished_unixtime={int(stamp.timestamp())}\nbackups_kept=1\n"
        f"news_enabled=1\nnews_path={news_store}\nnews_days=180\n"
        "news_min_records=8192\nnews_scanned=1\nnews_pruned=0\nnews_kept=1\n"
        "news_undated=0\nnews_malformed=0\nnews_records=1\nnews_bytes=20\n"
        "news_error=\n"
        f"verdict_enabled=1\nverdict_path={verdict_store}\nverdict_days=730\n"
        "verdict_min_records=512\nverdict_scanned=1\nverdict_pruned=0\n"
        "verdict_kept=1\nverdict_undated=0\nverdict_malformed=0\n"
        "verdict_records=1\nverdict_bytes=20\nverdict_error=\n",
        encoding="utf-8",
    )
    return marker


def write_inference_stats_marker(
    tmp_path: Path, *, age: timedelta = timedelta(minutes=2)
) -> Path:
    """Inference backend counters as ``src.inference_decision_stats`` writes them."""
    stamp = datetime(2026, 11, 25, 12, 0, tzinfo=UTC) - age
    path = tmp_path / "backend_stats.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_utc": stamp.isoformat(),
                "last_backend": "hailo",
                "decisions": {"hailo": 8, "cpu": 2},
            }
        ),
        encoding="utf-8",
    )
    return path


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
    "TradingJsonlStoreOverRetentionGuard": "trading_jsonl_store_bytes > 268435456",
    "TradingJsonlStoreGrowing": "trading_jsonl_store_bytes > 16777216",
    "TradingInferenceBackendCountersMissing": (
        "trading_inference_model_enabled == 1 "
        "and trading_inference_stats_present == 0"
    ),
    "TradingInferenceBackendFallback": (
        'trading_inference_expected_backend_hailo == 1 '
        'and (trading_inference_backend_share{backend="cpu"} + '
        'trading_inference_backend_share{backend="rules_only"}) > 0.5'
    ),
    "TradingInferenceRulesOnlyDecisions": (
        'trading_inference_backend_share{backend="rules_only"} > 0.05'
    ),
    "TradingModelReleaseMissing": (
        "trading_model_release_enabled == 1 "
        "and trading_model_release_present == 0"
    ),
    "TradingModelReleaseArtifactMismatch": "trading_model_release_artifacts_ok == 0",
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


def test_jsonl_store_gauges_report_current_store_sizes(tmp_path) -> None:
    """The size gauges come from the real files, not from the marker counters."""
    body = render(
        tmp_path,
        {"SCORECARD_STATUS_JSON": str(tmp_path / "absent.json")},
        dry_run=False,
    )
    values = gauge_values(body)
    assert values['trading_jsonl_store_bytes{store="news"}'] == 19
    assert values['trading_jsonl_store_bytes{store="verdict"}'] == 19
    assert values['trading_jsonl_store_records{store="news"}'] == 1
    assert values['trading_jsonl_store_pruned{store="verdict"}'] == 0


def test_jsonl_store_gauges_are_absent_without_a_retention_marker(tmp_path) -> None:
    """A profile that never runs retention publishes no store gauge at all."""
    body = render_metrics(
        tmp_path / "STOP", "", tmp_path / "signal.json", tmp_path / "journal.csv",
        tmp_path / "bot.log",
        now=datetime(2026, 11, 25, 12, 0, tzinfo=UTC),
        scorecard_gate=scorecard_gate_settings(gate_env(tmp_path, None)),
        deployment_env_file=write_deployment_env(tmp_path, dry_run=True),
        retention_report_file=tmp_path / "absent.env",
    )
    assert "trading_jsonl_store_bytes" not in body


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


# ---------------------------------------------------------------------------
# Inference backend mix: the cluster fallback must be observable
# ---------------------------------------------------------------------------

def inference_render(
    tmp_path: Path,
    *,
    backend: str,
    use_tabular_model: bool,
    stats: dict[str, int] | None,
    age: timedelta = timedelta(minutes=2),
) -> dict[str, float]:
    """Gauge values for one deployment profile plus its backend counters."""
    env_file = tmp_path / "deployment.env"
    env_file.write_text(
        "DRY_RUN=true\n"
        f"USE_TABULAR_MODEL={'true' if use_tabular_model else 'false'}\n"
        f"TABULAR_MODEL_BACKEND={backend}\n",
        encoding="utf-8",
    )
    stats_file = tmp_path / "backend_stats.json"
    if stats is not None:
        stamp = datetime(2026, 11, 25, 12, 0, tzinfo=UTC) - age
        stats_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "updated_utc": stamp.isoformat(),
                    "last_backend": max(stats),
                    "decisions": stats,
                }
            ),
            encoding="utf-8",
        )
    body = render_metrics(
        tmp_path / "STOP", "", tmp_path / "signal.json", tmp_path / "journal.csv",
        tmp_path / "bot.log",
        now=datetime(2026, 11, 25, 12, 0, tzinfo=UTC),
        scorecard_gate=scorecard_gate_settings(gate_env(tmp_path, None)),
        deployment_env_file=env_file,
        inference_stats_file=stats_file,
    )
    return gauge_values(body)


def test_hailo_profile_publishes_the_backend_mix(tmp_path) -> None:
    values = inference_render(
        tmp_path, backend="hailo", use_tabular_model=True,
        stats={"hailo": 8, "cpu": 2},
    )
    assert values["trading_inference_model_enabled"] == 1
    assert values["trading_inference_stats_present"] == 1
    assert values["trading_inference_expected_backend_hailo"] == 1
    assert 100 < values["trading_inference_stats_age_seconds"] < 140
    assert values['trading_inference_decisions_total{backend="hailo"}'] == 8
    assert values['trading_inference_decisions_total{backend="cpu"}'] == 2
    assert values['trading_inference_backend_share{backend="hailo"}'] == 0.8
    assert values['trading_inference_backend_share{backend="cpu"}'] == 0.2
    # No rules_only counter means no gauge, not a zero that dilutes the mix.
    assert 'trading_inference_backend_share{backend="rules_only"}' not in (
        "\n".join(values))


def test_catboost_profile_cannot_match_the_hailo_fallback_alert(tmp_path) -> None:
    """A single-node Pi legitimately runs on CPU, so it must stay silent."""
    values = inference_render(
        tmp_path, backend="catboost", use_tabular_model=True,
        stats={"cpu": 10},
    )
    assert values["trading_inference_expected_backend_hailo"] == 0
    assert values['trading_inference_backend_share{backend="cpu"}'] == 1.0


def test_rules_only_fallback_is_reported_as_a_share(tmp_path) -> None:
    values = inference_render(
        tmp_path, backend="hailo", use_tabular_model=True,
        stats={"hailo": 18, "cpu": 1, "rules_only": 1},
    )
    assert values['trading_inference_backend_share{backend="rules_only"}'] == 0.05


def test_tabular_model_disabled_publishes_no_counters_and_no_alert(tmp_path) -> None:
    """A rules-only deployment has no backend mix to monitor."""
    values = inference_render(
        tmp_path, backend="catboost", use_tabular_model=False, stats=None)
    assert values["trading_inference_model_enabled"] == 0
    assert values["trading_inference_stats_present"] == 0
    assert "trading_inference_backend_share" not in str(values)


def test_corrupt_counter_file_is_reported_as_absent(tmp_path) -> None:
    stats_file = tmp_path / "backend_stats.json"
    stats_file.write_text("{truncated", encoding="utf-8")
    env_file = tmp_path / "deployment.env"
    env_file.write_text(
        "DRY_RUN=true\nUSE_TABULAR_MODEL=true\nTABULAR_MODEL_BACKEND=hailo\n",
        encoding="utf-8",
    )
    body = render_metrics(
        tmp_path / "STOP", "", tmp_path / "signal.json", tmp_path / "journal.csv",
        tmp_path / "bot.log",
        now=datetime(2026, 11, 25, 12, 0, tzinfo=UTC),
        scorecard_gate=scorecard_gate_settings(gate_env(tmp_path, None)),
        deployment_env_file=env_file,
        inference_stats_file=stats_file,
    )
    values = gauge_values(body)
    assert values["trading_inference_stats_present"] == 0
    assert "trading_inference_stats_age_seconds" not in values


def test_inference_gauges_are_absent_without_a_stats_file(tmp_path) -> None:
    body = render_metrics(
        tmp_path / "STOP", "", tmp_path / "signal.json", tmp_path / "journal.csv",
        tmp_path / "bot.log",
        now=datetime(2026, 11, 25, 12, 0, tzinfo=UTC),
        scorecard_gate=scorecard_gate_settings(gate_env(tmp_path, None)),
        deployment_env_file=write_deployment_env(tmp_path, dry_run=True),
    )
    assert "trading_inference_" not in body


# ---------------------------------------------------------------------------
# Active model release: which paired release this host is trading on
# ---------------------------------------------------------------------------

def release_render(tmp_path: Path, release_root: str) -> dict[str, float]:
    """Gauge values for one model release root state."""
    body = render(
        tmp_path,
        {"SCORECARD_STATUS_JSON": str(tmp_path / "absent.json")},
        dry_run=True,
        model_release_root=release_root,
    )
    return gauge_values(body)


def test_promoted_release_publishes_verified_gauges(tmp_path) -> None:
    root = write_release_root(tmp_path)
    values = release_render(tmp_path, str(root))
    assert values["trading_model_release_enabled"] == 1
    assert values["trading_model_release_present"] == 1
    assert values["trading_model_release_artifacts_ok"] == 1
    assert values["trading_model_release_features"] == 2
    assert values["trading_model_release_age_seconds"] >= 0


def test_unset_release_root_cannot_match_the_missing_release_alert(tmp_path) -> None:
    """A host that never promotes releases publishes enabled 0, so it stays quiet."""
    values = release_render(tmp_path, "")
    assert values["trading_model_release_enabled"] == 0
    assert "trading_model_release_present" not in values
    assert "trading_model_release_artifacts_ok" not in values


def test_release_root_without_active_link_matches_the_missing_alert(tmp_path) -> None:
    (tmp_path / "models").mkdir()
    values = release_render(tmp_path, str(tmp_path / "models"))
    assert values["trading_model_release_enabled"] == 1
    assert values["trading_model_release_present"] == 0
    assert "trading_model_release_artifacts_ok" not in values


def test_tampered_release_artifact_matches_the_mismatch_alert(tmp_path) -> None:
    """The release is still live, so presence stays 1 while verification drops."""
    root = write_release_root(tmp_path, tamper="checksum")
    values = release_render(tmp_path, str(root))
    assert values["trading_model_release_present"] == 1
    assert values["trading_model_release_artifacts_ok"] == 0


def test_missing_release_artifact_matches_the_mismatch_alert(tmp_path) -> None:
    root = write_release_root(tmp_path, tamper="missing")
    values = release_render(tmp_path, str(root))
    assert values["trading_model_release_present"] == 1
    assert values["trading_model_release_artifacts_ok"] == 0
