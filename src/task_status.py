"""Status markers written by the scheduled wrapper scripts.

A scheduled report that exits non-zero - or never runs at all - is invisible to
Prometheus: systemd knows the unit failed, but no exported metric does. Each
wrapper therefore writes a small ``KEY=value`` marker after **every** attempt,
success or failure, and the operational-health exporter turns those markers
into gauges so an alert can fire on a failed or missing run.

The format is deliberately shell-writable (the same convention as
``results/scorecards/latest_status.env``), so the wrappers need no Python
helper of their own. ``tests/test_daily_dryrun_review.py`` and
``tests/test_public_paper_daily_report.py`` run the real wrappers and parse the
result with this module, which is what keeps the two languages in sync.

Every reader fails closed: a missing, unreadable, empty or malformed marker
yields ``None`` rather than raising, so one broken file cannot take the whole
health export down.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

# ``key=value`` lines; unknown keys are kept, the reader only asks for what it needs.
STATUS_LINE = re.compile(r"^([a-z][a-z0-9_]*)=(.*)$")

EXIT_CODE_KEY = "exit_code"
FINISHED_KEY = "finished_unixtime"


def read_task_status(status_file: Path) -> dict[str, str] | None:
    """Parse a wrapper status marker, or ``None`` when it is unusable."""
    try:
        text = status_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = STATUS_LINE.match(line.strip())
        if match:
            values[match.group(1)] = match.group(2)
    return values or None


def task_exit_code(status: dict[str, str]) -> int | None:
    """Exit code of the last run, or ``None`` when the marker lacks one."""
    try:
        return int(status.get(EXIT_CODE_KEY, "").strip())
    except ValueError:
        return None


def task_last_run_age_seconds(
    status: dict[str, str], now: datetime | None = None
) -> float | None:
    """Seconds since the last run finished, or ``None`` when unparseable."""
    raw = status.get(FINISHED_KEY, "").strip()
    try:
        finished = float(raw)
    except ValueError:
        return None
    if finished <= 0.0:
        return None
    reference = now or datetime.now(UTC)
    return max(0.0, (reference - datetime.fromtimestamp(finished, UTC)).total_seconds())


def parse_task_status_files(raw: str) -> dict[str, Path]:
    """Parse a ``name=path,name=path`` list, skipping malformed entries.

    Task names become Prometheus label values, so they are restricted to a
    label-safe alphabet; a path with no name (or a name that is not label-safe)
    is dropped rather than guessed at.
    """
    tasks: dict[str, Path] = {}
    for entry in raw.split(","):
        name, separator, path = entry.strip().partition("=")
        if not separator or not name or not path:
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
            continue
        tasks[name] = Path(path)
    return tasks
