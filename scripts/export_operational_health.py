#!/usr/bin/env python3
"""Export bot safety, Hailo, research-feed, capacity and data-staleness health.

Writes a Prometheus textfile-collector fragment. The staleness gauges are what
make a silently stopped collector or bot process observable: without them a
report over an empty window still succeeds and every downstream metric simply
reads as zero. The task-status gauges close the other half of the same gap - a
scheduled report that exits non-zero, or never runs at all, is known to systemd
but was invisible to Prometheus until the wrappers started writing a status
marker (``src/task_status.py``).
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from src.data_staleness import measure_staleness, resolve_log_timezone
from src.env_file import env_bool, read_env_value
from src.exporter_paths import ENV_PATH
from src.fill_reconciliation_report import (
    marker_age_seconds,
    marker_int,
    read_report_marker,
)
from src.inference_decision_stats import (
    HAILO_BACKEND,
    KNOWN_BACKENDS,
    read_backend_stats,
)
from src.inference_decision_stats import (
    resolve_stats_path as resolve_inference_stats_path,
)
from src.jsonl_retention import (
    read_report_marker as read_retention_marker,
)
from src.jsonl_retention import (
    report_int,
    resolve_report_path,
)
from src.model_release import read_active_release, resolve_release_root
from src.pnl_prometheus_format import escape_label_value
from src.scorecard_gate import ScorecardGateSettings, scorecard_gate_settings
from src.task_status import (
    parse_task_status_files,
    read_task_status,
    task_exit_code,
    task_last_run_age_seconds,
)


def _research_age(path: Path, now: datetime) -> float | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    timestamps = [payload.get("timestamp_utc")]
    items = payload.get("news_items", [])
    if isinstance(items, list):
        timestamps.extend(
            item.get("fetched_at") for item in items if isinstance(item, dict))
    parsed = []
    for value in timestamps:
        if not isinstance(value, str):
            continue
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            continue
        if stamp.tzinfo is not None:
            parsed.append(stamp.astimezone(UTC))
    if not parsed:
        return None
    return max(0.0, (now - max(parsed)).total_seconds())


def _task_status_lines(
    task_status_files: dict[str, Path], now: datetime
) -> list[str]:
    """Gauges for the scheduled wrappers: exit code, marker presence, run age.

    A task is only reported when the operator listed it, so a profile that does
    not run a given timer publishes nothing for it instead of a false absence.
    """
    lines: list[str] = []
    for name in sorted(task_status_files):
        label = f'{{task="{escape_label_value(name)}"}}'
        status = read_task_status(task_status_files[name])
        lines.extend([
            ("# HELP trading_task_status_present Whether the scheduled task wrote a "
             "parseable status marker."),
            "# TYPE trading_task_status_present gauge",
            f"trading_task_status_present{label} {int(status is not None)}",
        ])
        if status is None:
            continue
        exit_code = task_exit_code(status)
        lines.extend([
            ("# HELP trading_task_last_exit_code Exit code of the last scheduled "
             "task run."),
            "# TYPE trading_task_last_exit_code gauge",
            f"trading_task_last_exit_code{label} {exit_code if exit_code is not None else 0}",
        ])
        age = task_last_run_age_seconds(status, now)
        if age is not None:
            lines.extend([
                ("# HELP trading_task_last_run_age_seconds Seconds since the last "
                 "scheduled task run finished."),
                "# TYPE trading_task_last_run_age_seconds gauge",
                f"trading_task_last_run_age_seconds{label} {age:.3f}",
            ])
    return lines


def _scorecard_gate_lines(
    gate: ScorecardGateSettings, env_file: Path, now: datetime
) -> list[str]:
    """Gauges for the live-trading scorecard gate in :mod:`src.scorecard_gate`.

    The gate refuses ``DRY_RUN=false`` without a fresh ``GO`` verdict, so the
    combination that matters operationally is "live trading is configured but
    the scorecard evidence is missing, stale, or not GO". ``DRY_RUN`` is read
    from the deployment ``.env`` the bot service loads, which is the same
    fail-closed ``.env`` reading the PnL exporters already use. A profile that
    never trades live publishes ``live_enabled 0`` and can never match the
    alert.
    """
    live_enabled = int(not env_bool("DRY_RUN", True, str(env_file), quiet=True))
    status = gate.evaluate(now=now)
    lines = [
        ("# HELP trading_scorecard_gate_enabled Whether the scorecard gate is "
         "enabled for this deployment."),
        "# TYPE trading_scorecard_gate_enabled gauge",
        f"trading_scorecard_gate_enabled {int(gate.enabled)}",
        ("# HELP trading_scorecard_gate_live_enabled Whether the deployment "
         "configuration places real orders (DRY_RUN=false)."),
        "# TYPE trading_scorecard_gate_live_enabled gauge",
        f"trading_scorecard_gate_live_enabled {live_enabled}",
        ("# HELP trading_scorecard_gate_allowed Whether a fresh GO scorecard "
         "verdict backs live trading right now."),
        "# TYPE trading_scorecard_gate_allowed gauge",
        f"trading_scorecard_gate_allowed {int(status.allowed)}",
    ]
    if status.age_seconds is not None:
        lines.extend([
            ("# HELP trading_scorecard_gate_age_seconds Age of the scorecard "
             "verdict backing the gate."),
            "# TYPE trading_scorecard_gate_age_seconds gauge",
            f"trading_scorecard_gate_age_seconds {status.age_seconds:.3f}",
        ])
    return lines


def _fill_reconciliation_lines(
    report_file: Path | None, env_file: Path, live_enabled: bool, now: datetime
) -> list[str]:
    """Gauges for the journal/exchange fill reconciliation of :mod:`src.fill_reconciliation`.

    The reconciliation is the only control that answers "did the exchange do what
    the journal claims", so a silent pass is the dangerous case: it either never
    ran, or it ran without credentials and verified nothing. ``enabled`` follows
    the reconciliation default (a live deployment reconciles, a paper deployment
    has no exchange side), so a simulated profile publishes ``enabled 0`` and can
    never match the alerts. The marker is written by the bot at startup and by
    ``deploy/fill-reconciliation.timer``; it is read here rather than re-deriving
    a verdict, so the exporter stays read-only and needs no credentials.
    """
    enabled = env_bool(
        "TRADING_FILL_RECONCILIATION_ENABLED", live_enabled, str(env_file), quiet=True,
    )
    lines = [
        ("# HELP trading_fill_reconciliation_enabled Whether journalled fills are "
         "expected to be reconciled against the exchange on this host."),
        "# TYPE trading_fill_reconciliation_enabled gauge",
        f"trading_fill_reconciliation_enabled {int(enabled)}",
    ]
    values = read_report_marker(str(report_file)) if report_file else None
    lines.extend([
        ("# HELP trading_fill_reconciliation_present Whether the last fill "
         "reconciliation wrote a parseable summary marker."),
        "# TYPE trading_fill_reconciliation_present gauge",
        f"trading_fill_reconciliation_present {int(values is not None)}",
    ])
    if values is None:
        return lines
    complete = str(values.get("status", "")).strip().lower() == "complete"
    lines.extend([
        ("# HELP trading_fill_reconciliation_complete Whether the last fill "
         "reconciliation verified every journalled live order."),
        "# TYPE trading_fill_reconciliation_complete gauge",
        f"trading_fill_reconciliation_complete {int(complete)}",
    ])
    # ``quantity_mismatch`` already counts every unmatched order as well, so the
    # two counters together are the number of bad orders, not the number of
    # findings - adding UNMATCHED again would report one order twice.
    counts = [marker_int(values, key) for key in ("quantity_mismatch", "fee_mismatch")]
    if all(count is not None for count in counts):
        mismatches = sum(count for count in counts if count is not None)
        lines.extend([
            ("# HELP trading_fill_reconciliation_mismatches Orders of the last "
             "complete pass that differ from the journal in filled quantity "
             "(orders the exchange does not know included) or in fees."),
            "# TYPE trading_fill_reconciliation_mismatches gauge",
            f"trading_fill_reconciliation_mismatches {mismatches}",
        ])
    age = marker_age_seconds(values, now)
    if age is not None:
        lines.extend([
            ("# HELP trading_fill_reconciliation_age_seconds Age of the last fill "
             "reconciliation pass."),
            "# TYPE trading_fill_reconciliation_age_seconds gauge",
            f"trading_fill_reconciliation_age_seconds {age:.3f}",
        ])
    return lines


def _jsonl_retention_lines(report_file: Path | None) -> list[str]:
    """Size gauges for the append-only JSONL stores, from the retention marker.

    The news store and the verdict history grow with every fetch and every
    scorecard run, and ``src.jsonl_retention.py`` refuses a store above
    ``MAX_STORE_BYTES`` rather than holding it in RAM. The byte gauge is the one
    that shows the failure mode: a store that keeps growing past the guard is
    no longer pruned at all. The marker is the single source of truth, so the
    exporter stays read-only and never counts a multi-megabyte file itself.
    """
    values = read_retention_marker(str(report_file)) if report_file else None
    if values is None:
        return []
    lines: list[str] = []
    for store in ("news", "verdict"):
        path = str(values.get(f"{store}_path", "")).strip()
        if not path:
            continue
        label = f'{{store="{escape_label_value(store)}"}}'
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        lines.extend([
            ("# HELP trading_jsonl_store_bytes Size of the append-only JSONL "
             "store as seen by the last retention pass."),
            "# TYPE trading_jsonl_store_bytes gauge",
            f"trading_jsonl_store_bytes{label} {size}",
        ])
        records = report_int(values, f"{store}_records")
        if records is not None:
            lines.extend([
                ("# HELP trading_jsonl_store_records Records left in the "
                 "append-only JSONL store by the last retention pass."),
                "# TYPE trading_jsonl_store_records gauge",
                f"trading_jsonl_store_records{label} {records}",
            ])
        pruned = report_int(values, f"{store}_pruned")
        if pruned is not None:
            lines.extend([
                ("# HELP trading_jsonl_store_pruned Records removed from the "
                 "append-only JSONL store by the last retention pass."),
                "# TYPE trading_jsonl_store_pruned gauge",
                f"trading_jsonl_store_pruned{label} {pruned}",
            ])
    return lines


def _inference_backend_lines(
    stats_file: Path | None, env_file: Path, now: datetime
) -> list[str]:
    """Backend-share gauges for the tabular inference cascade.

    The bot records which backend served each decision
    (:mod:`src.inference_decision_stats`), so a cluster deployment that quietly
    lost its Hailo endpoint keeps reporting decisions - only the backend mix
    changes. The share gauges are what make that visible; the age gauge covers
    the other failure mode, a bot that stopped recording at all. The expected
    backend is read from the deployment ``.env`` the bot service loads, so a
    single-node profile that legitimately runs ``catboost`` can never match the
    Hailo-fallback alert.
    """
    if stats_file is None:
        return []
    stats = read_backend_stats(stats_file)
    enabled = int(env_bool(
        "USE_TABULAR_MODEL", False, str(env_file), quiet=True))
    lines = [
        ("# HELP trading_inference_model_enabled Whether the deployment enables "
         "the optional tabular model."),
        "# TYPE trading_inference_model_enabled gauge",
        f"trading_inference_model_enabled {enabled}",
        ("# HELP trading_inference_stats_present Whether the bot published "
         "inference backend counters with at least one recorded decision."),
        "# TYPE trading_inference_stats_present gauge",
        f"trading_inference_stats_present {int(not stats.unreadable and stats.total > 0)}",
    ]
    if stats.unreadable or stats.total <= 0:
        return lines
    if stats.updated_utc is not None:
        lines.extend([
            ("# HELP trading_inference_stats_age_seconds Seconds since the bot last "
             "updated its inference backend counters."),
            "# TYPE trading_inference_stats_age_seconds gauge",
            (
                f"trading_inference_stats_age_seconds "
                f"{max(0.0, (now - stats.updated_utc).total_seconds()):.3f}"
            ),
        ])
    hailo_expected = int(
        read_env_value("TABULAR_MODEL_BACKEND", str(env_file), quiet=True)
        .strip().lower() == HAILO_BACKEND)
    lines.extend([
        ("# HELP trading_inference_expected_backend_hailo Whether the deployment "
         "configures the remote Hailo backend."),
        "# TYPE trading_inference_expected_backend_hailo gauge",
        f"trading_inference_expected_backend_hailo {hailo_expected}",
    ])
    for backend in sorted(KNOWN_BACKENDS):
        if backend not in stats.counts:
            continue
        label = f'{{backend="{escape_label_value(backend)}"}}'
        lines.extend([
            ("# HELP trading_inference_decisions_total Tabular decisions served by "
             "this inference backend since the counters were created."),
            "# TYPE trading_inference_decisions_total gauge",
            f"trading_inference_decisions_total{label} {stats.counts[backend]}",
            ("# HELP trading_inference_backend_share Share of recorded tabular "
             "decisions served by this inference backend."),
            "# TYPE trading_inference_backend_share gauge",
            f"trading_inference_backend_share{label} {stats.share(backend):.6f}",
        ])
    return lines


def _model_release_lines(release_root: str, now: datetime) -> list[str]:
    """Gauges for the paired model release this deployment is running.

    Promotion and rollback are explicit operator commands, so a release can be
    swapped, left dangling or edited on disk without anything in monitoring
    noticing which model is actually live. ``read_active_release`` is the
    read-only side of that same contract and never raises, so the exporter can
    distinguish the three states that matter: no release root configured
    (nothing published), a release root without a usable ``active`` link
    (``present 0``), and a release whose files no longer match its manifest
    (``artifacts_ok 0``).
    """
    root = release_root.strip()
    enabled = int(bool(root))
    lines = [
        ("# HELP trading_model_release_enabled Whether a paired model release "
         "root is configured for this host."),
        "# TYPE trading_model_release_enabled gauge",
        f"trading_model_release_enabled {enabled}",
    ]
    if not enabled:
        return lines
    active = read_active_release(root)
    lines.extend([
        ("# HELP trading_model_release_present Whether the release root has a "
         "readable active release manifest."),
        "# TYPE trading_model_release_present gauge",
        f"trading_model_release_present {int(active is not None)}",
    ])
    if active is None:
        return lines
    lines.extend([
        ("# HELP trading_model_release_artifacts_ok Whether every active release "
         "artifact still matches its manifest checksum."),
        "# TYPE trading_model_release_artifacts_ok gauge",
        f"trading_model_release_artifacts_ok {int(active.artifacts_verified)}",
        ("# HELP trading_model_release_features Number of features in the active "
         "release contract."),
        "# TYPE trading_model_release_features gauge",
        f"trading_model_release_features {len(active.features)}",
        ("# HELP trading_model_release_age_seconds Age of the active release "
         "manifest since promotion."),
        "# TYPE trading_model_release_age_seconds gauge",
        (
            f"trading_model_release_age_seconds "
            f"{max(0.0, (now - active.manifest_updated_utc).total_seconds()):.3f}"
        ),
    ])
    return lines


def _disk_lines(disk_paths: Sequence[str]) -> list[str]:
    """Capacity gauges for the filesystems the bot writes to.

    ``shutil.disk_usage`` is stdlib and needs no extra exporter, so the Pi
    profile gets capacity coverage from the same 30 s timer as everything else.
    An unreadable path is skipped: a missing gauge is distinguishable from a
    full disk, and a wrong path must not silence the other gauges.
    """
    lines: list[str] = []
    for raw in disk_paths:
        path = raw.strip()
        if not path:
            continue
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        label = f'{{path="{escape_label_value(path)}"}}'
        used_percent = 100.0 * usage.used / usage.total if usage.total else 0.0
        lines.extend([
            "# HELP trading_disk_total_bytes Total size of the filesystem in bytes.",
            "# TYPE trading_disk_total_bytes gauge",
            f"trading_disk_total_bytes{label} {usage.total}",
            (
                "# HELP trading_disk_free_bytes Space usable by the unprivileged bot "
                "user, excluding reserved blocks."
            ),
            "# TYPE trading_disk_free_bytes gauge",
            f"trading_disk_free_bytes{label} {usage.free}",
            "# HELP trading_disk_used_percent Used share of the filesystem.",
            "# TYPE trading_disk_used_percent gauge",
            f"trading_disk_used_percent{label} {used_percent:.4f}",
        ])
    return lines


def query_database_size(database_url: str) -> int | None:
    """Size of the analytics database in bytes, or ``None`` when unavailable.

    ``psycopg`` is not a Pi dependency, so the import happens here rather than
    at module scope and a missing driver is just another unavailable probe.
    """
    try:
        psycopg = importlib.import_module("psycopg")
    except ImportError:
        return None
    try:
        with psycopg.connect(database_url, connect_timeout=3) as connection:
            row = connection.execute(
                "SELECT pg_database_size(current_database())"
            ).fetchone()
    except Exception:  # noqa: BLE001 - any driver/server error means "unavailable"
        return None
    if not row or row[0] is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _database_lines(
    analytics_db_url: str, database_probe: Callable[[str], int | None]
) -> list[str]:
    """Analytics database capacity gauges, only when a URL is configured."""
    if not analytics_db_url.strip():
        return []
    lines = [
        ("# HELP trading_analytics_database_enabled Whether an analytics database "
         "URL is configured."),
        "# TYPE trading_analytics_database_enabled gauge",
        "trading_analytics_database_enabled 1",
    ]
    size = database_probe(analytics_db_url)
    lines.extend([
        ("# HELP trading_analytics_database_up Whether the analytics database "
         "answered a size query."),
        "# TYPE trading_analytics_database_up gauge",
        f"trading_analytics_database_up {int(size is not None)}",
    ])
    if size is not None:
        lines.extend([
            ("# HELP trading_analytics_database_size_bytes Size of the analytics "
             "database in bytes."),
            "# TYPE trading_analytics_database_size_bytes gauge",
            f"trading_analytics_database_size_bytes {size}",
        ])
    return lines


def render_metrics(
    kill_switch_file: Path,
    hailo_health_url: str,
    research_signal_file: Path,
    journal_file: Path,
    bot_log_file: Path,
    market_data_dir: Path | None = None,
    *,
    log_timezone: str = "UTC",
    now: datetime | None = None,
    health_probe=urlopen,
    task_status_files: dict[str, Path] | None = None,
    disk_paths: Sequence[str] = (),
    analytics_db_url: str = "",
    database_probe: Callable[[str], int | None] = query_database_size,
    scorecard_gate: ScorecardGateSettings | None = None,
    deployment_env_file: Path | None = None,
    fill_report_file: Path | None = None,
    retention_report_file: Path | None = None,
    inference_stats_file: Path | None = None,
    model_release_root: str = "",
) -> str:
    now = now or datetime.now(UTC)
    kill_switch_active = int(os.path.lexists(kill_switch_file))
    hailo_enabled = int(bool(hailo_health_url.strip()))
    hailo_up = 0 if hailo_enabled else 1
    if hailo_enabled:
        try:
            with health_probe(hailo_health_url, timeout=3) as response:
                hailo_up = int(200 <= response.status < 300)
        except (URLError, TimeoutError, OSError, ValueError):
            hailo_up = 0
    research_age = _research_age(research_signal_file, now)
    lines = [
        "# HELP trading_kill_switch_active Whether the live trading stop marker exists.",
        "# TYPE trading_kill_switch_active gauge",
        f"trading_kill_switch_active {kill_switch_active}",
        "# HELP trading_hailo_health_check_enabled Whether a Hailo health URL is configured.",
        "# TYPE trading_hailo_health_check_enabled gauge",
        f"trading_hailo_health_check_enabled {hailo_enabled}",
        "# HELP trading_hailo_service_up Whether the configured Hailo service passed its health check.",
        "# TYPE trading_hailo_service_up gauge",
        f"trading_hailo_service_up {hailo_up}",
        "# HELP trading_research_signal_present Whether a parseable timestamped research signal exists.",
        "# TYPE trading_research_signal_present gauge",
        f"trading_research_signal_present {int(research_age is not None)}",
    ]
    if research_age is not None:
        lines.extend([
            "# HELP trading_research_signal_age_seconds Age of the latest research signal.",
            "# TYPE trading_research_signal_age_seconds gauge",
            f"trading_research_signal_age_seconds {research_age:.3f}",
        ])

    staleness = measure_staleness(
        journal_file, bot_log_file, market_data_dir,
        log_timezone=resolve_log_timezone(log_timezone), now=now,
    )
    for key, help_text in (
        ("journal", "trade journal rows"),
        ("bot_log", "bot log lines"),
    ):
        lines.extend([
            (f"# HELP trading_{key}_present Whether {help_text} with a parseable "
             "timestamp exist."),
            f"# TYPE trading_{key}_present gauge",
            f"trading_{key}_present {staleness[f'{key}_present']:.0f}",
        ])
        age = staleness[f"{key}_age_seconds"]
        if age is not None:
            lines.extend([
                f"# HELP trading_{key}_age_seconds Age of the newest {help_text}.",
                f"# TYPE trading_{key}_age_seconds gauge",
                f"trading_{key}_age_seconds {age:.3f}",
            ])
    if market_data_dir is not None:
        lines.extend([
            ("# HELP trading_market_data_present Whether market recorder rows with a "
             "parseable timestamp exist."),
            "# TYPE trading_market_data_present gauge",
            f"trading_market_data_present {staleness['market_data_present']:.0f}",
        ])
        market_age = staleness["market_data_age_seconds"]
        if market_age is not None:
            lines.extend([
                ("# HELP trading_market_data_age_seconds Age of the newest market "
                 "recorder row."),
                "# TYPE trading_market_data_age_seconds gauge",
                f"trading_market_data_age_seconds {market_age:.3f}",
            ])
    lines.extend(_task_status_lines(task_status_files or {}, now))
    lines.extend(_disk_lines(disk_paths))
    lines.extend(_database_lines(analytics_db_url, database_probe))
    if scorecard_gate is not None:
        env_file = deployment_env_file or Path(ENV_PATH)
        lines.extend(_scorecard_gate_lines(scorecard_gate, env_file, now))
        live_enabled = not env_bool("DRY_RUN", True, str(env_file), quiet=True)
        lines.extend(
            _fill_reconciliation_lines(fill_report_file, env_file, live_enabled, now)
        )
    lines.extend(_jsonl_retention_lines(retention_report_file))
    lines.extend(_inference_backend_lines(
        inference_stats_file,
        deployment_env_file or Path(ENV_PATH),
        now,
    ))
    lines.extend(_model_release_lines(model_release_root, now))
    return "\n".join(lines) + "\n"


def write_metrics(output: Path, body: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent,
            prefix=f".{output.name}-", delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    root = Path(os.getenv("TRADING_DATA_DIR", "."))
    market_data = os.getenv("TRADING_MARKET_DATA_DIR", "").strip()
    # Capacity is watched on the volume the bot works on unless the operator
    # names the mount points explicitly, e.g. an NVMe data directory.
    disk_raw = os.getenv("TRADING_DISK_PATHS", "").strip() or str(root)
    # The reconciliation marker path is the same one the reconciliation unit and
    # the bot write; unset means "this host publishes no reconciliation report".
    fill_report = os.getenv("TRADING_FILL_RECONCILIATION_REPORT", "").strip()
    # Same resolution the retention unit writes with, including the default
    # path, so the exporter needs no extra configuration on a standard host.
    retention_report = resolve_report_path()
    # The bot writes the inference backend counters and the exporter only reads
    # them, so both sides use the same default path resolution.
    inference_stats = resolve_inference_stats_path()
    # Which paired model release is live is a property of the host, not of the
    # bot process, so the exporter resolves the release root itself. Unset means
    # "this host has no paired releases" and publishes no release gauges, which
    # also keeps the read-only sandbox from probing a path that is not there.
    model_release_root = resolve_release_root()
    body = render_metrics(
        Path(os.getenv("TRADING_KILL_SWITCH_FILE", ".trading_kill_switch")),
        os.getenv("HAILO_HEALTH_URL", ""),
        Path(os.getenv("RESEARCH_SIGNAL_PATH", str(root / "research_signal_latest.json"))),
        Path(os.getenv("TRADING_JOURNAL_FILE", str(root / "trade_journal.csv"))),
        Path(os.getenv("TRADING_BOT_LOG_FILE", str(root / "trading_bot.log"))),
        Path(market_data) if market_data else None,
        log_timezone=os.getenv("TRADING_LOG_TIMEZONE", "UTC"),
        task_status_files=parse_task_status_files(
            os.getenv("TRADING_TASK_STATUS_FILES", "")),
        disk_paths=[*disk_raw.split(",")],
        analytics_db_url=os.getenv("ANALYTICS_DB_URL", ""),
        scorecard_gate=scorecard_gate_settings(),
        deployment_env_file=Path(
            os.getenv("TRADING_DEPLOYMENT_ENV_FILE", ENV_PATH)),
        fill_report_file=Path(fill_report) if fill_report else None,
        retention_report_file=Path(retention_report) if retention_report else None,
        inference_stats_file=(
            Path(inference_stats) if inference_stats else None),
        model_release_root=model_release_root,
    )
    output = Path(os.getenv(
        "TRADING_HEALTH_METRICS_FILE",
        "results/scorecards/textfile/trading_health.prom",
    ))
    write_metrics(output, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
