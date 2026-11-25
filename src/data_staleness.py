"""Bounded staleness probes for the bot's own data sources.

The operational-health exporter needs one answer per input: *when did this file
last see activity?* A dead collector or a stopped bot process is otherwise
invisible, because a report over an empty window still succeeds.

Every probe reads only a bounded tail of the file, so the periodic health timer
never rescans a multi-megabyte journal, and every probe fails closed to ``None``
on a missing, unreadable, or unparseable file rather than raising.
"""

from __future__ import annotations

import csv
import os
import re
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# A staleness probe only needs the newest rows, so it reads far less than the
# dashboard's market loader (``market_observations.MARKET_DATA_READ_LIMIT``).
STALENESS_READ_LIMIT = 1024 * 1024

# ``logging.Formatter`` default asctime: ``2026-11-25 21:40:03,118``.
LOG_TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:[.,](\d{1,6}))?")

JOURNAL_TIMESTAMP_COLUMN = "timestamp"
MARKET_TIMESTAMP_COLUMN = "timestamp_utc"


def _parse_iso_timestamp(value: str) -> datetime | None:
    """Parse an explicit UTC ISO timestamp, as written by the bot itself."""
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        # Journal rows are written with an aware UTC clock; a naive value is
        # malformed for this schema, so treat it as UTC rather than guessing.
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def resolve_log_timezone(name: str) -> tzinfo:
    """Resolve a log timestamp zone, falling back to UTC like the daily report."""
    if not name.strip():
        return UTC
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _parse_log_timestamp(value: str, zone: tzinfo) -> datetime | None:
    """Parse a ``logging`` asctime stamp, which carries no offset.

    ``logging.Formatter`` writes naive local time, so the zone has to be
    supplied rather than guessed. UTC is the documented default and matches
    ``scripts/public_paper_daily_report.py --log-timezone``.
    """
    match = LOG_TIMESTAMP.match(value.strip())
    if match is None:
        return None
    try:
        stamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    fraction = match.group(2)
    if fraction:
        stamp = stamp.replace(microsecond=int(fraction.ljust(6, "0")))
    return stamp.replace(tzinfo=zone).astimezone(UTC)


def _read_tail_lines(path: Path, read_limit: int) -> list[str]:
    """Return the complete lines in the last ``read_limit`` bytes."""
    with path.open("rb") as source:
        size = os.fstat(source.fileno()).st_size
        if size > read_limit:
            source.seek(-read_limit, os.SEEK_END)
            content = source.read(read_limit)
            first_newline = content.find(b"\n")
            if first_newline < 0:
                return []
            content = content[first_newline + 1:]
        else:
            source.seek(0)
            content = source.read(read_limit)
    if content and not content.endswith(b"\n"):
        last_newline = content.rfind(b"\n")
        content = content[:last_newline + 1] if last_newline >= 0 else b""
    return content.decode("utf-8", errors="replace").splitlines()


def _latest_csv_timestamp(
    path: Path, column: str, parse, read_limit: int = STALENESS_READ_LIMIT
) -> datetime | None:
    """Newest value of ``column`` using the file's own header for the index."""
    if not path.is_file():
        return None
    try:
        with path.open("rb") as source:
            header = source.readline(8192).decode("utf-8", errors="replace")
            fields = next(csv.reader([header]), [])
            if column not in fields:
                return None
            index = fields.index(column)
            lines = _read_tail_lines(path, read_limit)
    except (OSError, UnicodeError):
        return None
    latest: datetime | None = None
    for line in lines:
        values = next(csv.reader([line]), [])
        if len(values) <= index:
            continue
        stamp = parse(values[index])
        if stamp is not None and (latest is None or stamp > latest):
            latest = stamp
    return latest


def latest_journal_timestamp(path: Path) -> datetime | None:
    """Newest ``timestamp`` in a trade journal, or ``None`` if unusable."""
    return _latest_csv_timestamp(path, JOURNAL_TIMESTAMP_COLUMN, _parse_iso_timestamp)


def latest_market_timestamp(market_dir: Path) -> datetime | None:
    """Newest recorder row across ``tickers.csv`` and ``candles_1m.csv``."""
    latest: datetime | None = None
    for name in ("tickers.csv", "candles_1m.csv"):
        stamp = _latest_csv_timestamp(
            market_dir / name, MARKET_TIMESTAMP_COLUMN, _parse_iso_timestamp
        )
        if stamp is not None and (latest is None or stamp > latest):
            latest = stamp
    return latest


def latest_log_timestamp(
    path: Path, zone: tzinfo = UTC
) -> datetime | None:
    """Newest ``asctime`` in a bot log file, or ``None`` if unusable."""
    if not path.is_file():
        return None
    try:
        lines = _read_tail_lines(path, STALENESS_READ_LIMIT)
    except (OSError, UnicodeError):
        return None
    latest: datetime | None = None
    for line in lines:
        stamp = _parse_log_timestamp(line, zone)
        if stamp is not None and (latest is None or stamp > latest):
            latest = stamp
    return latest


def _age(latest: datetime | None, now: datetime) -> float | None:
    if latest is None:
        return None
    return max(0.0, (now - latest).total_seconds())


def measure_staleness(
    journal_file: Path,
    bot_log_file: Path,
    market_data_dir: Path | None,
    *,
    log_timezone: tzinfo = UTC,
    now: datetime | None = None,
) -> dict[str, float | None]:
    """Flat Prometheus-ready ages for the journal, bot log, and market data.

    A source that is missing or unreadable reports ``*_present`` 0 and no age,
    which keeps "not collected" distinguishable from "stale" in alert rules.
    ``market_data_dir`` is ``None`` on hosts that do not run the recorder, so no
    market metric is reported at all instead of a false absence.
    """
    moment = now or datetime.now(UTC)
    journal = latest_journal_timestamp(journal_file)
    bot_log = latest_log_timestamp(bot_log_file, log_timezone)
    market = latest_market_timestamp(market_data_dir) if market_data_dir else None
    return {
        "journal_present": float(journal is not None),
        "journal_age_seconds": _age(journal, moment),
        "bot_log_present": float(bot_log is not None),
        "bot_log_age_seconds": _age(bot_log, moment),
        "market_data_present": float(market is not None),
        "market_data_age_seconds": _age(market, moment),
    }
