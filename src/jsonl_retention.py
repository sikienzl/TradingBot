"""Retention (pruning) for the append-only JSONL stores.

``src.news_item_store`` and ``src.scorecard_verdict_history`` only ever append,
which is what makes a point-in-time replay trustworthy - but it also means the
two files grow forever. On a single-node Raspberry Pi that is a capacity
problem, and the audit called the missing policy an open gap on both profiles.

This module is the policy. It rewrites a store in place so that it keeps

* every record whose retention timestamp is within ``days`` of ``now``, and
* always the newest ``min_records`` usable records, even when they are older.

Contract:

* The rewrite happens while holding the same ``flock(LOCK_EX)`` the appenders
  take on the data file, and it reuses that file's inode (truncate + write).
  A blocked appender therefore waits for the prune and then appends to the
  pruned content instead of to a replaced inode, so no append is ever lost.
* A copy of the pre-prune content is written next to the store before the
  rewrite, and the newest ``backups`` copies are kept. An interrupted rewrite
  is recoverable from that copy.
* A record whose retention timestamp is missing or unparseable is kept. The
  policy fails closed: pruning may only delete records it can positively place
  outside the horizon.
* ``days`` and ``min_records`` are independent limits; ``0`` switches one off.
  ``min_records`` must stay at or above the store's dedupe window, otherwise a
  re-fetched item would be appended a second time.
* Nothing here is imported by the stores, so appending never depends on the
  retention policy. ``scripts/prune_jsonl_stores.py`` is the only caller that
  mutates a store.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .news_item_store import parse_store_timestamp
from .scorecard_verdict_history import parse_history_timestamp
from .task_status import read_task_status

RETENTION_SCHEMA_VERSION = 1

ENABLED_ENV = "TRADING_JSONL_RETENTION_ENABLED"
REPORT_PATH_ENV = "TRADING_JSONL_RETENTION_REPORT"
DEFAULT_REPORT_PATH = "results/jsonl_retention/latest_status.env"
BACKUPS_ENV = "TRADING_JSONL_RETENTION_BACKUPS"
DEFAULT_BACKUPS = 1

NEWS_DAYS_ENV = "TRADING_JSONL_RETENTION_NEWS_DAYS"
NEWS_RECORDS_ENV = "TRADING_JSONL_RETENTION_NEWS_RECORDS"
VERDICT_DAYS_ENV = "TRADING_JSONL_RETENTION_VERDICT_DAYS"
VERDICT_RECORDS_ENV = "TRADING_JSONL_RETENTION_VERDICT_RECORDS"

# The news store is the point-in-time replay source, so the horizon is also the
# longest replay window the deployment supports. 180 days is far past any
# backtest window the guides promise; 0 keeps every record.
DEFAULT_NEWS_DAYS = 180
# Floor for the news store. It has to stay at or above the append dedupe window
# (4096) and the veto read limit (1000), so a pruned store still dedupes.
DEFAULT_NEWS_RECORDS = 8192
# A weekly scorecard writes one record, so two years of verdicts is a small
# file. The floor stays above the history dedupe window (256) and the read
# limit (200), and far above the nine-day tuning evidence window.
DEFAULT_VERDICT_DAYS = 730
DEFAULT_VERDICT_RECORDS = 512

NEWS_STORE = "news"
VERDICT_STORE = "verdict"

# A prune reads the store into memory, so an absurdly large file is refused
# rather than allowed to exhaust a Raspberry Pi. The operator has to shrink such
# a store by hand once, which is also the point where a manual backup is wise.
MAX_STORE_BYTES = 268_435_456

# Backup files live next to the store: "<store>.bak-<UTC stamp>".
BACKUP_SUFFIX = ".bak-"

# Marker keys, in the shell-readable lowercase form used by src.task_status so
# the generic scheduled-task alerts cover this unit without extra rules.
MARKER_KEYS = (
    "schema_version",
    "status",
    "reason",
    "exit_code",
    "started_unixtime",
    "finished_unixtime",
    "backups_kept",
    "news_enabled",
    "news_path",
    "news_days",
    "news_min_records",
    "news_scanned",
    "news_pruned",
    "news_kept",
    "news_undated",
    "news_malformed",
    "news_records",
    "news_bytes",
    "news_error",
    "verdict_enabled",
    "verdict_path",
    "verdict_days",
    "verdict_min_records",
    "verdict_scanned",
    "verdict_pruned",
    "verdict_kept",
    "verdict_undated",
    "verdict_malformed",
    "verdict_records",
    "verdict_bytes",
    "verdict_error",
)

_TIMESTAMP_KEYS: dict[str, tuple[str, ...]] = {
    # ``fetched_at`` is the local visibility time, which is the conservative
    # choice: a record is only dropped once the bot could no longer have seen it.
    NEWS_STORE: ("fetched_at", "stored_at"),
    VERDICT_STORE: ("timestamp_utc",),
}

_PARSERS: dict[str, Callable[[Any], datetime | None]] = {
    NEWS_STORE: parse_store_timestamp,
    VERDICT_STORE: parse_history_timestamp,
}


@dataclass(frozen=True)
class StoreRetention:
    """One store plus the limits that apply to it."""

    store: str
    path: str
    days: int
    min_records: int

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    @property
    def timestamp_keys(self) -> tuple[str, ...]:
        return _TIMESTAMP_KEYS.get(self.store, ())

    def parse(self, value: Any) -> datetime | None:
        parser = _PARSERS.get(self.store, parse_history_timestamp)
        return parser(value)


def env_flag(name: str, default: bool, environ: Mapping[str, str] | None = None) -> bool:
    raw = (os.environ if environ is None else environ).get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def env_non_negative_int(
    name: str, default: int, environ: Mapping[str, str] | None = None
) -> int:
    """Read a non-negative int. A malformed value falls back to the default."""
    raw = (os.environ if environ is None else environ).get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def retention_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return env_flag(ENABLED_ENV, True, environ)


def resolve_report_path(environ: Mapping[str, str] | None = None) -> str:
    """Where the run summary marker is written; ``none`` disables the marker."""
    raw = (os.environ if environ is None else environ).get(REPORT_PATH_ENV)
    if raw is None:
        return DEFAULT_REPORT_PATH
    if raw.strip().lower() in ("", "off", "none", "false", "no", "disabled"):
        return ""
    return raw.strip()


def resolve_backups(environ: Mapping[str, str] | None = None) -> int:
    return env_non_negative_int(BACKUPS_ENV, DEFAULT_BACKUPS, environ)


def resolve_news_retention(environ: Mapping[str, str] | None = None) -> StoreRetention:
    """News store path plus its limits. Path resolution matches the store itself."""
    env = os.environ if environ is None else environ
    path = env.get("RESEARCH_NEWS_STORE_PATH")
    if path is not None and path.strip().lower() in ("", "off", "none", "false", "no", "disabled"):
        path = ""
    elif path is None:
        path = "data/research_news_items.jsonl"
    return StoreRetention(
        store=NEWS_STORE,
        path=path.strip(),
        days=env_non_negative_int(NEWS_DAYS_ENV, DEFAULT_NEWS_DAYS, environ),
        min_records=env_non_negative_int(NEWS_RECORDS_ENV, DEFAULT_NEWS_RECORDS, environ),
    )


def resolve_verdict_retention(environ: Mapping[str, str] | None = None) -> StoreRetention:
    """Verdict history path plus its limits, reusing the history's own rule."""
    env = os.environ if environ is None else environ
    raw = env.get("SCORECARD_VERDICT_HISTORY_FILE")
    path = "" if raw is not None and raw.strip().lower() in (
        "", "off", "none", "false", "no", "disabled") else (
        raw.strip() if raw is not None else "results/scorecards/verdict_history.jsonl")
    return StoreRetention(
        store=VERDICT_STORE,
        path=path,
        days=env_non_negative_int(VERDICT_DAYS_ENV, DEFAULT_VERDICT_DAYS, environ),
        min_records=env_non_negative_int(VERDICT_RECORDS_ENV, DEFAULT_VERDICT_RECORDS, environ),
    )


def _iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%d%H%M%S")


def _usable(line: str) -> dict[str, Any] | None:
    """Parse one raw line into a store record, or ``None`` when unusable.

    Mirrors what the store readers accept: a JSON object carrying a
    ``record_id``. Anything else is a torn or foreign line, and dropping it is
    what keeps a store readable after a killed writer.
    """
    if not line.strip():
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict) or not record.get("record_id"):
        return None
    return record


def _retention_stamp(
    rule: StoreRetention, record: Mapping[str, Any]
) -> datetime | None:
    """The retention timestamp of one record, or ``None`` when it has none."""
    for key in rule.timestamp_keys:
        moment = rule.parse(record.get(key))
        if moment is not None:
            return moment
    return None


def _select_kept(
    rule: StoreRetention, lines: list[str], cutoff: datetime | None
) -> tuple[list[str], dict[str, int]]:
    """Decide which lines survive. Blank and unreadable lines are dropped."""
    usable: list[tuple[str, datetime | None]] = []
    counters = {"malformed": 0, "undated": 0}
    for line in lines:
        if not line.strip():
            continue
        record = _usable(line)
        if record is None:
            counters["malformed"] += 1
            continue
        usable.append((line, _retention_stamp(rule, record)))

    total = len(usable)
    # The last ``min_records`` usable lines are always protected; a floor of
    # zero protects nothing, so every line is judged by its age.
    keep_from = max(0, total - rule.min_records)
    kept: list[str] = []
    pruned = 0
    for position, (line, moment) in enumerate(usable):
        if position >= keep_from:
            kept.append(line)
            continue
        if moment is None:
            # Fail closed: an undated record is never old enough to delete.
            counters["undated"] += 1
            kept.append(line)
            continue
        if cutoff is None or moment >= cutoff:
            kept.append(line)
            continue
        pruned += 1
    counters["pruned"] = pruned
    counters["kept"] = len(kept)
    counters["scanned"] = total
    return kept, counters


def _backup_paths(path: str) -> list[str]:
    directory = os.path.dirname(os.path.abspath(path))
    name = os.path.basename(path)
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    return sorted(
        os.path.join(directory, entry)
        for entry in entries
        if entry.startswith(name + BACKUP_SUFFIX)
    )


def _write_backup(path: str, body: str, now: datetime, keep: int) -> str:
    """Copy the pre-prune content next to the store and rotate old copies."""
    backup_path = f"{path}{BACKUP_SUFFIX}{_iso_utc(now)}"
    with open(backup_path, "w", encoding="utf-8", errors="surrogateescape") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    for stale in _backup_paths(path)[: -max(keep, 1)]:
        try:
            os.unlink(stale)
        except OSError:
            continue
    return backup_path


def prune_jsonl_store(
    rule: StoreRetention,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    backups: int = DEFAULT_BACKUPS,
) -> dict[str, Any]:
    """Prune one store in place under the append lock. Never raises for I/O.

    Returns counters describing the pass. ``error`` is set instead of raising so
    one unusable store cannot stop the other store from being pruned.
    """
    moment = now or datetime.now(UTC)
    result: dict[str, Any] = {
        "store": rule.store,
        "path": rule.path,
        "enabled": int(bool(rule.path)),
        "exists": 0,
        "scanned": 0,
        "kept": 0,
        "pruned": 0,
        "undated": 0,
        "malformed": 0,
        "unchanged": 1,
        "dry_run": int(dry_run),
        "records": 0,
        "bytes": 0,
        "backup": "",
        "error": "",
        "reason": "",
    }
    if not rule.path:
        result["reason"] = "disabled"
        return result
    if not os.path.exists(rule.path):
        result["reason"] = "missing"
        return result
    cutoff: datetime | None = None
    if rule.days > 0:
        cutoff = moment - timedelta(days=rule.days)
    try:
        if os.path.getsize(rule.path) > MAX_STORE_BYTES:
            result["error"] = f"store larger than {MAX_STORE_BYTES} bytes"
            result["reason"] = "too_large"
            return result
        # ``surrogateescape`` keeps undecodable bytes intact: a line that cannot
        # be parsed is dropped as malformed, and a line that can be is written
        # back byte for byte instead of with replacement characters.
        with open(rule.path, "r+", encoding="utf-8",
                  errors="surrogateescape") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                body = handle.read()
                lines = body.split("\n")
                kept, counters = _select_kept(rule, lines, cutoff)
                result.update(counters)
                result["exists"] = 1
                result["records"] = len(kept)
                new_body = "".join(f"{line}\n" for line in kept)
                result["bytes"] = len(new_body.encode("utf-8", errors="surrogateescape"))
                if not counters["scanned"] and not counters["malformed"]:
                    # Nothing usable and nothing to clean up: leave the file
                    # exactly as it is.
                    result["reason"] = "empty"
                    return result
                if new_body == body:
                    result["reason"] = "unchanged"
                    return result
                result["unchanged"] = 0
                if dry_run:
                    result["reason"] = "dry_run"
                    return result
                if backups > 0 and body:
                    result["backup"] = _write_backup(
                        rule.path, body, moment, backups)
                handle.seek(0)
                handle.truncate()
                handle.write(new_body)
                handle.flush()
                os.fsync(handle.fileno())
                result["reason"] = "pruned"
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        result["error"] = str(exc)
        result["reason"] = "error"
    return result


def report_marker_lines(values: Mapping[str, Any]) -> list[str]:
    """Shell-readable ``KEY=value`` summary in the src.task_status format."""
    lines = [f"schema_version={RETENTION_SCHEMA_VERSION}"]
    for key in MARKER_KEYS:
        if key == "schema_version" or key not in values:
            continue
        value = values[key]
        if value is None:
            continue
        lines.append(f"{key}={value}")
    return lines


def write_report_marker(path: str, values: Mapping[str, Any]) -> bool:
    """Atomically write the run summary. Returns ``False`` when it cannot be written."""
    if not path:
        return False
    body = "\n".join(report_marker_lines(values)) + "\n"
    directory = os.path.dirname(os.path.abspath(path))
    temporary_path = None
    try:
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory,
            prefix=".jsonl-retention-", suffix=".tmp", delete=False,
        ) as handle:
            temporary_path = handle.name
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError:
        return False
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return True


def read_report_marker(path: str) -> dict[str, str] | None:
    """Parse the run summary, or ``None`` when it is missing or unusable."""
    if not path:
        return None
    return read_task_status(Path(path))


def report_int(values: Mapping[str, str], key: str) -> int | None:
    try:
        return int(str(values.get(key, "")).strip())
    except (TypeError, ValueError):
        return None
