"""Durable per-decision counters for the tabular inference backend.

Audit point 2 asks that a cluster fallback to CPU or rules-only be visible in
monitoring, not only in the bot log. :mod:`src.inference_fallback` already tags
every prediction with the backend that served it, but that label reached only
the log line and the ``signal_source`` column of the analysis result. This
module turns the label into a durable counter file that
``scripts/export_operational_health.py`` publishes as gauges, so a deployment
that quietly lost its Hailo endpoint shows up as a CPU share instead of a
green dashboard.

The store is deliberately cheap and process-free: the bot writes it from the
analysis path, the exporter only ever reads it, and every read merges into the
next write, so a restart continues the same counters and two writers cannot
lose a decision by keeping separate in-memory totals. Writes are throttled by
the timestamp stored in the marker itself - a steady backend is recorded at
most once per :data:`DEFAULT_MIN_WRITE_INTERVAL_SECONDS`, while a *change* of
backend is written immediately, because that transition is the event the alert
watches for.

Reads never raise: a missing file means "no decision recorded yet", an
unreadable or incompatible file means "counters unknown" and is reported as
such rather than silently restarting from zero. Writes swallow storage errors
after logging them once - losing a metric must never stop a trading decision.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Schema version of the counter file; incompatible versions fail closed.
BACKEND_STATS_VERSION = 1
#: Timestamps are stored in UTC ISO-8601 with an explicit offset.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S+00:00"

#: The three backends the decision pyramid can serve a tabular prediction with.
HAILO_BACKEND = "hailo"
CPU_BACKEND = "cpu"
RULES_ONLY_BACKEND = "rules_only"
#: Anything else is a configuration drift, kept in one bucket instead of a
#: growing key space.
OTHER_BACKEND = "other"
KNOWN_BACKENDS: frozenset[str] = frozenset(
    {HAILO_BACKEND, CPU_BACKEND, RULES_ONLY_BACKEND, OTHER_BACKEND})

#: Environment override for the counter file; ``off``/``none`` disables it.
STATS_PATH_ENV = "TRADING_INFERENCE_STATS_FILE"
DEFAULT_STATS_PATH = "results/inference/backend_stats.json"
#: Minimum delay between two writes while the backend stays the same.
MIN_WRITE_INTERVAL_ENV = "TRADING_INFERENCE_STATS_MIN_WRITE_SECONDS"
DEFAULT_MIN_WRITE_INTERVAL_SECONDS = 15.0

# Values that mean "this deployment does not publish inference counters".
_DISABLED_VALUES = frozenset({"", "off", "none", "false", "no", "disabled"})


@dataclass
class BackendStats:
    """Cumulative per-backend decision counters read from disk."""

    #: Decisions per normalised backend name.
    counts: dict[str, int] = field(default_factory=dict)
    #: Backend that served the most recent decision, ``None`` when unknown.
    last_backend: str | None = None
    #: When the counter file was last written.
    updated_utc: datetime | None = None
    #: True when the file exists but cannot be trusted.
    unreadable: bool = False

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def share(self, backend: str) -> float:
        """Share of all recorded decisions served by ``backend``."""
        total = self.total
        if total <= 0:
            return 0.0
        return self.counts.get(backend, 0) / total


def normalize_backend(value: Any) -> str:
    """Map a backend label from a prediction result onto a known bucket."""
    name = str(value or "").strip().lower()
    if name in KNOWN_BACKENDS:
        return name
    if "hailo" in name:
        return HAILO_BACKEND
    if name in {"catboost", "cpu", "onnx"}:
        return CPU_BACKEND
    if name in {"rules", "rules-only", "rules_only_fallback", "none"}:
        return RULES_ONLY_BACKEND
    return OTHER_BACKEND


def resolve_stats_path(environ: Mapping[str, str] | None = None) -> str:
    """Where the counter file lives; an empty string disables the counters."""
    raw = (os.environ if environ is None else environ).get(STATS_PATH_ENV)
    if raw is None:
        return DEFAULT_STATS_PATH
    if raw.strip().lower() in _DISABLED_VALUES:
        return ""
    return raw.strip()


def resolve_min_write_interval_seconds(
    environ: Mapping[str, str] | None = None,
) -> float:
    """Throttle for steady-state writes; ``0`` writes every decision."""
    raw = (os.environ if environ is None else environ).get(MIN_WRITE_INTERVAL_ENV)
    if raw is None:
        return DEFAULT_MIN_WRITE_INTERVAL_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_WRITE_INTERVAL_SECONDS
    if not math.isfinite(value) or value < 0.0:
        return DEFAULT_MIN_WRITE_INTERVAL_SECONDS
    return value


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Backward-compatible: naive timestamps were always written as UTC.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_counts(payload: Any) -> dict[str, int] | None:
    """Decode the counter object, or ``None`` when its shape is not trustworthy."""
    if not isinstance(payload, dict):
        return None
    counts: dict[str, int] = {}
    for backend, value in payload.items():
        if not isinstance(backend, str) or not backend.strip():
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            # A negative or fractional counter means the file was edited or
            # truncated: report the whole document as untrusted instead.
            return None
        counts[normalize_backend(backend)] = counts.get(
            normalize_backend(backend), 0) + int(value)
    return counts


def read_backend_stats(path: str | Path) -> BackendStats:
    """Read the counter file. Never raises: a missing file yields an empty store."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return BackendStats()
    except (OSError, UnicodeError, json.JSONDecodeError):
        return BackendStats(unreadable=True)
    if not isinstance(payload, dict) or payload.get("version") != BACKEND_STATS_VERSION:
        return BackendStats(unreadable=True)
    counts = _parse_counts(payload.get("decisions"))
    if counts is None:
        return BackendStats(unreadable=True)
    last = payload.get("last_backend")
    return BackendStats(
        counts=counts,
        last_backend=normalize_backend(last) if isinstance(last, str) else None,
        updated_utc=_parse_timestamp(payload.get("updated_utc")),
    )


def backend_stats_to_payload(
    stats: BackendStats, *, now_utc: datetime | None = None
) -> dict[str, Any]:
    """Serialises ``stats`` into the on-disk JSON document."""
    now = now_utc or datetime.now(UTC)
    return {
        "version": BACKEND_STATS_VERSION,
        "updated_utc": now.strftime(TIMESTAMP_FORMAT),
        "last_backend": stats.last_backend,
        "decisions": {
            backend: int(stats.counts.get(backend, 0))
            for backend in sorted(KNOWN_BACKENDS)
            if backend in stats.counts
        },
    }


def write_backend_stats(
    path: str | Path, stats: BackendStats, *, now_utc: datetime | None = None
) -> None:
    """Write the counter file atomically so a reader never sees a partial file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        backend_stats_to_payload(stats, now_utc=now_utc),
        indent=2,
        sort_keys=True,
    ) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent,
            prefix=f".{target.name}-", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _should_write(
    stats: BackendStats, backend: str, now: datetime, min_interval: float
) -> bool:
    """Throttle steady-state writes without delaying a backend change."""
    if stats.unreadable or stats.updated_utc is None or stats.last_backend != backend:
        return True
    age = (now - stats.updated_utc).total_seconds()
    return not age < min_interval


_write_error_logged = False


def record_backend_selection(
    backend: Any,
    *,
    path: str | Path | None = None,
    now: datetime | None = None,
    min_write_interval_seconds: float | None = None,
) -> bool:
    """Add one decision for ``backend`` to the counter file.

    Returns ``True`` when the marker was written. Storage problems are logged
    once per process and otherwise ignored: the counters are monitoring, and a
    failed metric must not change a trading decision.
    """
    global _write_error_logged
    resolved = resolve_stats_path() if path is None else str(path)
    if not resolved:
        return False
    moment = now or datetime.now(UTC)
    interval = (
        resolve_min_write_interval_seconds()
        if min_write_interval_seconds is None
        else float(min_write_interval_seconds)
    )
    name = normalize_backend(backend)
    try:
        stats = read_backend_stats(resolved)
        if not _should_write(stats, name, moment, interval):
            return False
        counts = dict(stats.counts)
        counts[name] = counts.get(name, 0) + 1
        write_backend_stats(
            resolved,
            BackendStats(counts=counts, last_backend=name, updated_utc=moment),
            now_utc=moment,
        )
    except OSError as exc:
        if not _write_error_logged:
            _write_error_logged = True
            logger.warning(
                "Inference backend counters could not be written to %s: %s",
                resolved, exc,
            )
        return False
    return True


class BackendStatsRecorder:
    """Callable wrapper that resolves the counter path from the environment.

    Stateless by design: the previous counters come from the file, so any
    number of processes may record decisions without coordinating in memory.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = path

    @property
    def path(self) -> str:
        return resolve_stats_path() if self._path is None else str(self._path)

    def record(self, backend: Any, *, now: datetime | None = None) -> bool:
        return record_backend_selection(backend, path=self.path, now=now)
