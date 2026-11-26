"""Append-only history of scorecard verdicts.

``scripts/run_weekly_scorecard.sh`` publishes exactly one verdict at a time:
``results/scorecards/latest_status.env`` (and ``latest_status.json``) are
overwritten on every run. That is enough for the fail-closed gate in
:mod:`src.scorecard_gate`, which only ever asks "is the current evidence a fresh
``GO``?", but it throws away the record the moment the next run lands. Two
operators questions cannot be answered with a single marker:

* was the last week's verdict also ``NO-GO``, i.e. is the tuning brake in
  ``tuning_policy.json`` (``if_verdict_no_go``) reacting to a streak or to a
  one-off?
* what did the gate see at the time it refused, after the marker was replaced?

This module keeps every verdict as an immutable JSONL record, following the same
storage contract as :mod:`src.news_item_store`: one record per line, canonical key
order, a stable ``record_id`` used to skip duplicates, an exclusive ``flock`` around
the append, ``fsync`` before the lock is released, and a bounded tail read. Nothing
here removes a record: pruning is :mod:`src.jsonl_retention` via
``scripts/prune_jsonl_stores.py``.

Readers fail closed like the rest of the repository: a missing, unreadable,
empty or malformed file yields an empty history rather than raising, and
:func:`resolve_scorecard_status` returns ``None`` when no marker or history
record can be trusted, so a caller must treat "no evidence" as a refusal.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .scorecard_gate import (
    DEFAULT_STATUS_ENV_FILE,
    DEFAULT_STATUS_JSON_FILE,
    GO_VERDICT,
    MAINTENANCE_RUN_MODE,
    read_scorecard_status,
    scorecard_status_age_seconds,
)

VERDICT_HISTORY_SCHEMA_VERSION = 1

HISTORY_PATH_ENV = "SCORECARD_VERDICT_HISTORY_FILE"
DEFAULT_VERDICT_HISTORY_FILE = "results/scorecards/verdict_history.jsonl"

# Values that switch the history off deliberately, e.g. for a throwaway run.
DISABLED_VALUES = frozenset({"", "off", "none", "false", "no", "disabled"})

# A weekly scorecard produces one record per week, so a few hundred records are
# several years of verdicts. The tail window only has to cover the streaks and
# the recent-NO-GO safety check, both of which look at single-digit counts.
DEFAULT_READ_LIMIT = 200
DEFAULT_DEDUPE_WINDOW = 256

_TAIL_CHUNK_BYTES = 65536

# Second bound on a tail read: even with a generous line limit, a reader must
# never scan an unbounded amount of disk. One megabyte covers thousands of
# verdict records, far past any streak or safety check that exists today.
_MAX_TAIL_BYTES = 1_048_576

VERDICT_GO = GO_VERDICT
VERDICT_HOLD = "HOLD"
VERDICT_NO_GO = "NO-GO"
VERDICT_ERROR = "ERROR"
KNOWN_VERDICTS = frozenset({VERDICT_GO, VERDICT_HOLD, VERDICT_NO_GO, VERDICT_ERROR})


def _iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_history_timestamp(value: Any) -> datetime | None:
    """Parse a history timestamp, or ``None`` when it is unusable."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def resolve_verdict_history_path(
    environ: Mapping[str, str] | None = None,
    *,
    default: str = DEFAULT_VERDICT_HISTORY_FILE,
) -> str:
    """Resolve the history path from the environment.

    An explicit disabled value returns ``""``, which every helper treats as
    "history off": the caller keeps working exactly as it did before this
    module existed.
    """
    env = os.environ if environ is None else environ
    raw = env.get(HISTORY_PATH_ENV)
    if raw is None:
        return default
    if raw.strip().lower() in DISABLED_VALUES:
        return ""
    return raw.strip()


def _record_identity(record: Mapping[str, Any]) -> str:
    """Stable identity so a re-published run is not stored twice.

    The scorecard report file name carries the run timestamp, which makes it the
    best available key; without it the timestamp plus verdict is used.
    """
    report_file = str(record.get("report_file") or "").strip()
    if report_file:
        return f"report:{os.path.basename(report_file)}"
    stamp = str(record.get("timestamp_utc") or "").strip()
    verdict = str(record.get("verdict") or "").strip().upper()
    mode = str(record.get("run_mode") or "").strip().lower()
    if not stamp or not verdict:
        return ""
    return f"run:{stamp}:{verdict}:{mode}"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def to_verdict_record(fields: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normalise one scorecard run into an immutable history record.

    A record without a usable verdict or timestamp is rejected (``None``) -
    storing an unattributable verdict would make the history untrustworthy.
    """
    verdict = str(fields.get("verdict") or "").strip().upper()
    if verdict not in KNOWN_VERDICTS:
        return None
    stamp = parse_history_timestamp(fields.get("timestamp_utc"))
    if stamp is None:
        return None
    run_mode = str(fields.get("run_mode") or "").strip().lower() or "standard"
    record: dict[str, Any] = {
        "schema_version": VERDICT_HISTORY_SCHEMA_VERSION,
        "timestamp_utc": _iso_utc(stamp),
        "verdict": verdict,
        "run_mode": run_mode,
    }
    for key in ("underlying_exit_code", "final_exit_code"):
        number = _as_int(fields.get(key))
        if number is not None:
            record[key] = number
    for key in ("primary_reason", "report_file", "profile"):
        text = str(fields.get(key) or "").strip()
        if text:
            record[key] = text
    metrics = fields.get("metrics")
    if isinstance(metrics, Mapping) and metrics:
        record["metrics"] = dict(metrics)
    record_id = _record_identity(record)
    if not record_id:
        return None
    record["record_id"] = record_id
    return record


def _iter_tail_lines(path: str, limit: int) -> list[bytes]:
    """Read at most ``limit`` trailing lines without loading the whole file."""
    if limit <= 0:
        return []
    try:
        with open(path, "rb") as raw:
            raw.seek(0, os.SEEK_END)
            position = raw.tell()
            if position == 0:
                return []
            collected: list[bytes] = []
            partial = b""
            scanned = 0
            while position > 0 and len(collected) < limit and scanned < _MAX_TAIL_BYTES:
                step = min(_TAIL_CHUNK_BYTES, position)
                position -= step
                raw.seek(position)
                block = raw.read(step) + partial
                scanned += step
                parts = block.split(b"\n")
                partial = parts[0]
                collected[0:0] = [part for part in parts[1:] if part.strip()]
            if position == 0 and partial:
                collected.insert(0, partial)
            return collected[-limit:]
    except OSError:
        return []


def _iter_records(lines: Iterable[bytes | str]) -> Iterator[dict[str, Any]]:
    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        if not line or not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("record_id"):
            yield record


def _ensure_trailing_newline(path: str) -> bool:
    """Repair a torn final line before appending. Returns True when repaired."""
    try:
        with open(path, "rb") as raw:
            raw.seek(0, os.SEEK_END)
            if raw.tell() == 0:
                return False
            raw.seek(-1, os.SEEK_END)
            if raw.read(1) == b"\n":
                return False
    except OSError:
        return False
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n")
    return True


def append_verdict_record(
    path: str,
    fields: Mapping[str, Any],
    *,
    dedupe_window: int = DEFAULT_DEDUPE_WINDOW,
) -> str:
    """Append one verdict record. Returns ``appended``, ``duplicate`` or ``invalid``.

    Never rewrites existing lines and never raises for an I/O problem - the
    scorecard run must not fail because its history could not be written.
    """
    record = to_verdict_record(fields)
    if record is None:
        return "invalid"
    if not path:
        return "disabled"
    try:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        with open(path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                _ensure_trailing_newline(path)
                known = {
                    str(existing.get("record_id"))
                    for existing in _iter_records(_iter_tail_lines(path, dedupe_window))
                }
                record_id = str(record["record_id"])
                if record_id in known:
                    return "duplicate"
                handle.write(json.dumps(record, sort_keys=True, ensure_ascii=True))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return "error"
    return "appended"


def read_verdict_history(
    path: str, *, limit: int = DEFAULT_READ_LIMIT
) -> list[dict[str, Any]]:
    """Return the most recent records, oldest first, skipping unreadable lines."""
    if not path or not os.path.exists(path):
        return []
    lines = _iter_tail_lines(path, limit if limit > 0 else DEFAULT_READ_LIMIT)
    return list(_iter_records(lines))


def latest_verdict_record(path: str) -> dict[str, Any] | None:
    """The newest usable history record, or ``None``."""
    records = read_verdict_history(path, limit=1)
    return records[-1] if records else None


def verdict_streak(path: str, verdict: str) -> int:
    """How many consecutive runs at the end of the history carry ``verdict``."""
    wanted = str(verdict).strip().upper()
    streak = 0
    for record in reversed(read_verdict_history(path)):
        if str(record.get("verdict", "")).strip().upper() != wanted:
            break
        streak += 1
    return streak


def history_stats(path: str) -> dict[str, Any]:
    """Small summary for logging and monitoring of the history itself."""
    stats: dict[str, Any] = {
        "path": path,
        "enabled": bool(path),
        "exists": bool(path) and os.path.exists(path),
        "records": 0,
        "newest_timestamp_utc": None,
        "newest_verdict": None,
        "go_streak": 0,
        "no_go_streak": 0,
    }
    if not stats["enabled"]:
        stats["reason"] = "disabled"
        return stats
    if not stats["exists"]:
        stats["reason"] = "missing"
        return stats
    records = read_verdict_history(path)
    stats["records"] = len(records)
    if records:
        newest = records[-1]
        stats["newest_timestamp_utc"] = str(newest.get("timestamp_utc") or "") or None
        stats["newest_verdict"] = str(newest.get("verdict") or "") or None
    stats["go_streak"] = verdict_streak(path, VERDICT_GO)
    stats["no_go_streak"] = verdict_streak(path, VERDICT_NO_GO)
    return stats


def history_age_seconds(path: str, now: datetime | None = None) -> float | None:
    """Seconds since the newest history record, or ``None`` when unusable."""
    newest = latest_verdict_record(path)
    if newest is None:
        return None
    return scorecard_status_age_seconds(
        {"TIMESTAMP_UTC": str(newest.get("timestamp_utc", ""))}, now
    )


def resolve_scorecard_status(
    *,
    status_json_file: Path | str | None = DEFAULT_STATUS_JSON_FILE,
    status_env_file: Path | str | None = DEFAULT_STATUS_ENV_FILE,
    history_file: str = DEFAULT_VERDICT_HISTORY_FILE,
) -> dict[str, Any] | None:
    """Newest scorecard evidence as a JSON-shaped status dict, or ``None``.

    Markers win over the history because they are written last and carry the
    metrics; the history is the fallback for a deployment that only keeps the
    uppercase ``.env`` marker, or whose marker was replaced by a partial run.
    The returned dict always has ``verdict``, ``run_mode`` and
    ``timestamp_utc`` as strings plus ``metrics`` as a (possibly empty) dict, so
    callers never have to handle two marker shapes.
    """
    for source, candidate in (("json", status_json_file), ("env", status_env_file)):
        if candidate is None or not str(candidate).strip():
            continue
        status = _read_full_status(Path(str(candidate)))
        if status is not None:
            status["source"] = source
            return status
    record = latest_verdict_record(history_file)
    if record is None:
        return None
    status = _normalise_record(record)
    status["source"] = "verdict_history"
    return status


def _read_full_status(candidate: Path) -> dict[str, Any] | None:
    """Read a marker without losing the fields the gate does not need.

    :func:`src.scorecard_gate.read_scorecard_status` projects a JSON marker onto
    the four keys a gate decision needs. The tuning cycle also needs the
    metrics block, so this reader keeps the whole payload.
    """
    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.strip():
        return None
    if text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return _normalise_json_payload(payload)
    marker = read_scorecard_status(candidate)
    if not marker:
        return None
    return _normalise_marker(marker)


def _normalise_json_payload(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    verdict = str(payload.get("verdict") or "").strip().upper()
    stamp = str(payload.get("timestamp_utc") or "").strip()
    if not verdict or not stamp:
        return None
    metrics = payload.get("metrics")
    result: dict[str, Any] = {
        "verdict": verdict,
        "run_mode": str(payload.get("run_mode") or "").strip().lower() or "standard",
        "timestamp_utc": stamp,
        "metrics": dict(metrics) if isinstance(metrics, Mapping) else {},
    }
    for key in (
        "underlying_exit_code",
        "final_exit_code",
        "primary_reason",
        "report_file",
        "profile",
    ):
        if payload.get(key) is not None:
            result[key] = payload[key]
    return result


def _normalise_marker(status: Mapping[str, str]) -> dict[str, Any]:
    verdict = str(status.get("VERDICT", "")).strip().upper()
    payload: dict[str, Any] = {
        "verdict": verdict or "ERROR",
        "run_mode": str(status.get("RUN_MODE", "")).strip().lower() or "standard",
        "timestamp_utc": str(status.get("TIMESTAMP_UTC", "")).strip(),
        "metrics": {},
    }
    underlying = _as_int(status.get("UNDERLYING_EXIT_CODE"))
    if underlying is not None:
        payload["underlying_exit_code"] = underlying
    final = _as_int(status.get("FINAL_EXIT_CODE"))
    if final is not None:
        payload["final_exit_code"] = final
    reason = str(status.get("PRIMARY_REASON", "")).strip()
    if reason:
        payload["primary_reason"] = reason
    report_file = str(status.get("REPORT_FILE", "")).strip()
    if report_file:
        payload["report_file"] = report_file
    profile = str(status.get("PROFILE", "")).strip()
    if profile:
        payload["profile"] = profile
    return payload


def _normalise_record(record: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "verdict": str(record.get("verdict", "")).strip().upper() or "ERROR",
        "run_mode": str(record.get("run_mode", "")).strip().lower() or "standard",
        "timestamp_utc": str(record.get("timestamp_utc", "")).strip(),
        "metrics": dict(record.get("metrics") or {}),
    }
    for key in (
        "underlying_exit_code",
        "final_exit_code",
        "primary_reason",
        "report_file",
        "profile",
    ):
        if record.get(key) is not None:
            payload[key] = record[key]
    return payload


def status_age_seconds(
    status: Mapping[str, Any] | None, now: datetime | None = None
) -> float | None:
    """Age of a resolved status dict, keyed the way the marker readers expect."""
    if not status:
        return None
    return scorecard_status_age_seconds(
        {"TIMESTAMP_UTC": str(status.get("timestamp_utc", ""))}, now
    )


def is_maintenance_override(status: Mapping[str, Any] | None) -> bool:
    """Whether a resolved status came from a maintenance-override run."""
    if not status:
        return False
    return str(status.get("run_mode", "")).strip().lower() == MAINTENANCE_RUN_MODE


def recent_no_go_records(history_file: str, limit: int) -> list[dict[str, Any]]:
    """Recent ``NO-GO`` records from the history, newest first."""
    if limit <= 0:
        return []
    records = read_verdict_history(history_file, limit=limit)
    return [
        record
        for record in reversed(records)
        if str(record.get("verdict", "")).strip().upper() == VERDICT_NO_GO
    ]
