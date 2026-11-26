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
from src.env_file import env_bool
from src.exporter_paths import ENV_PATH
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
        lines.extend(
            _scorecard_gate_lines(
                scorecard_gate, deployment_env_file or Path(ENV_PATH), now,
            )
        )
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
    )
    output = Path(os.getenv(
        "TRADING_HEALTH_METRICS_FILE",
        "results/scorecards/textfile/trading_health.prom",
    ))
    write_metrics(output, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
