"""Scorecard GO gating for model promotion and live-mode execution.

A weekly ``GO`` verdict is the only evidence the repository produces that the
recent trading record is healthy. Until now that evidence was advisory: the
model release CLI promoted a paired CPU/Hailo artifact without looking at the
scorecard, and ``DRY_RUN=false`` started real orders with no scorecard
prerequisite at all. Both paths now consult the same fail-closed gate, so a
missing, stale, malformed, ``HOLD``, ``NO-GO`` or ``ERROR`` marker refuses the
action exactly like an explicit ``NO-GO`` would.

``scripts/run_weekly_scorecard.sh`` writes two markers for the same run:

* ``results/scorecards/latest_status.env`` — ``KEY=value`` lines with
  **uppercase** keys (``VERDICT``, ``TIMESTAMP_UTC``, ``RUN_MODE``, ...). This
  is the marker the weekly ``scorecard.service`` always writes, and it is the
  only one on a single-node Raspberry Pi release.
* ``results/scorecards/latest_status.json`` — lowercase JSON keys, written when
  ``STATUS_JSON_ENABLED=true`` (the cluster ``scorecard-status.service`` and the
  tuning cycle both use it).

Both readers are tolerant of either shape and neither ever raises: an
unusable marker yields ``None`` and the gate refuses, mirroring
:mod:`src.task_status` and :mod:`src.data_staleness`.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Uppercase ``KEY=value`` lines; unknown keys are kept, the reader only asks for
# what it needs. Deliberately distinct from ``src.task_status.STATUS_LINE``,
# which only matches the lowercase wrapper markers.
STATUS_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)=(.*)$")

VERDICT_KEY = "VERDICT"
TIMESTAMP_KEY = "TIMESTAMP_UTC"
RUN_MODE_KEY = "RUN_MODE"
UNDERLYING_EXIT_CODE_KEY = "UNDERLYING_EXIT_CODE"

GO_VERDICT = "GO"
MAINTENANCE_RUN_MODE = "maintenance_override"

DEFAULT_STATUS_JSON_FILE = "results/scorecards/latest_status.json"
DEFAULT_STATUS_ENV_FILE = "results/scorecards/latest_status.env"

# ``scorecard.service`` produces one verdict per week on the Pi profile, so a
# marker older than one full cycle plus a day of slack is no longer evidence
# about the current record. The cluster profile refreshes the marker every five
# minutes, which makes the same bound generous there.
DEFAULT_MAX_AGE_SECONDS = 8 * 24 * 60 * 60.0

ENABLE_ENV = "SCORECARD_GATE_ENABLED"
STATUS_JSON_ENV = "SCORECARD_STATUS_JSON"
STATUS_ENV_ENV = "SCORECARD_STATUS_ENV"
MAX_AGE_ENV = "SCORECARD_GATE_MAX_AGE_SECONDS"


@dataclass(frozen=True)
class ScorecardGateStatus:
    """Outcome of one gate evaluation.

    ``reason`` is a single stable phrase intended to be embedded in the
    operator-facing refusal message.
    """

    allowed: bool
    reason: str
    verdict: str | None = None
    run_mode: str | None = None
    age_seconds: float | None = None


def _parse_json_status(text: str) -> dict[str, str] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    status: dict[str, str] = {}
    for json_key, status_key in (
        ("verdict", VERDICT_KEY),
        ("run_mode", RUN_MODE_KEY),
        ("timestamp_utc", TIMESTAMP_KEY),
        ("underlying_exit_code", UNDERLYING_EXIT_CODE_KEY),
    ):
        value = payload.get(json_key)
        if isinstance(value, str):
            status[status_key] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            status[status_key] = str(value)
    return status or None


def _parse_env_status(text: str) -> dict[str, str] | None:
    status: dict[str, str] = {}
    for line in text.splitlines():
        match = STATUS_LINE.match(line.strip())
        if match:
            status[match.group(1).upper()] = match.group(2).strip()
    return status or None


def read_scorecard_status(status_file: Path) -> dict[str, str] | None:
    """Parse a scorecard status marker, or ``None`` when it is unusable."""
    try:
        text = status_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.strip():
        return None
    if text.lstrip().startswith("{"):
        return _parse_json_status(text)
    return _parse_env_status(text)


def scorecard_status_age_seconds(
    status: dict[str, str], now: datetime | None = None
) -> float | None:
    """Seconds since the scorecard run, or ``None`` when unparseable."""
    raw = status.get(TIMESTAMP_KEY, "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return max(0.0, (reference - stamp.astimezone(UTC)).total_seconds())


def _first_usable_marker(
    status_json_file: Path | None, status_env_file: Path | None
) -> tuple[dict[str, str] | None, bool]:
    """Return the first parseable marker plus whether any marker was present."""
    found_marker = False
    for candidate in (status_json_file, status_env_file):
        if candidate is None:
            continue
        status = read_scorecard_status(candidate)
        if status is not None:
            return status, True
        if candidate.exists():
            found_marker = True
    return None, found_marker


def evaluate_scorecard_gate(
    status_json_file: Path | None = None,
    status_env_file: Path | None = None,
    *,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    allow_maintenance_override: bool = False,
    now: datetime | None = None,
) -> ScorecardGateStatus:
    """Decide whether a fresh ``GO`` scorecard verdict backs the requested action.

    Fails closed: no marker, an unreadable marker, a missing verdict, anything
    other than ``GO``, a maintenance-override run, or a marker older than
    ``max_age_seconds`` all refuse.
    """
    status, found_marker = _first_usable_marker(status_json_file, status_env_file)
    if status is None:
        reason = (
            "scorecard status marker is unreadable"
            if found_marker else "no scorecard status marker was found"
        )
        return ScorecardGateStatus(allowed=False, reason=reason)

    verdict = status.get(VERDICT_KEY, "").strip().upper()
    run_mode = status.get(RUN_MODE_KEY, "").strip().lower() or None
    if not verdict:
        return ScorecardGateStatus(allowed=False, reason="scorecard verdict is missing")
    if verdict != GO_VERDICT:
        return ScorecardGateStatus(
            allowed=False, reason=f"scorecard verdict is {verdict}", verdict=verdict,
            run_mode=run_mode,
        )
    if run_mode == MAINTENANCE_RUN_MODE and not allow_maintenance_override:
        return ScorecardGateStatus(
            allowed=False,
            reason="scorecard run used a maintenance override",
            verdict=verdict, run_mode=run_mode,
        )
    age = scorecard_status_age_seconds(status, now)
    if age is None:
        return ScorecardGateStatus(
            allowed=False, reason="scorecard status has no usable timestamp",
            verdict=verdict, run_mode=run_mode,
        )
    if age > max_age_seconds:
        return ScorecardGateStatus(
            allowed=False,
            reason=(
                f"scorecard verdict is stale ({age:.0f}s old, "
                f"limit {max_age_seconds:.0f}s)"
            ),
            verdict=verdict, run_mode=run_mode, age_seconds=age,
        )
    return ScorecardGateStatus(
        allowed=True, reason="scorecard verdict is GO", verdict=verdict,
        run_mode=run_mode, age_seconds=age,
    )


def _env_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value <= 0.0:
        return default
    return value


@dataclass(frozen=True)
class ScorecardGateSettings:
    """Resolved gate configuration: which markers to read and how strict to be."""

    enabled: bool
    status_json_file: Path | None
    status_env_file: Path | None
    max_age_seconds: float

    def evaluate(
        self, *, now: datetime | None = None,
        allow_maintenance_override: bool = False,
    ) -> ScorecardGateStatus:
        return evaluate_scorecard_gate(
            self.status_json_file, self.status_env_file,
            max_age_seconds=self.max_age_seconds,
            allow_maintenance_override=allow_maintenance_override, now=now,
        )


def scorecard_gate_settings(
    environ: Mapping[str, str] | None = None,
    *,
    default_max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> ScorecardGateSettings:
    """Resolve the gate configuration from the process environment.

    Paths are relative to the working directory, which is the deployment root
    (``/opt/trading_2``) under both release profiles, so the defaults need no
    configuration on either profile.
    """
    env = os.environ if environ is None else environ
    json_raw = env.get(STATUS_JSON_ENV, "").strip() or DEFAULT_STATUS_JSON_FILE
    env_raw = env.get(STATUS_ENV_ENV, "").strip() or DEFAULT_STATUS_ENV_FILE
    return ScorecardGateSettings(
        enabled=_env_bool(env, ENABLE_ENV, True),
        status_json_file=Path(json_raw),
        status_env_file=Path(env_raw),
        max_age_seconds=_env_float(env, MAX_AGE_ENV, default_max_age_seconds),
    )


def scorecard_gate_refusal(
    action: str, status: ScorecardGateStatus, settings: ScorecardGateSettings
) -> str:
    """Build the operator-facing refusal message for a blocked action."""
    markers = ", ".join(
        str(path) for path in (settings.status_json_file, settings.status_env_file)
        if path is not None
    )
    return (
        f"{action} requires a fresh GO scorecard verdict: {status.reason}. "
        f"Run scripts/run_weekly_scorecard.sh and re-check, or point "
        f"{STATUS_JSON_ENV}/{STATUS_ENV_ENV} at the current marker ({markers}). "
        f"Set {ENABLE_ENV}=false to override this gate deliberately."
    )
