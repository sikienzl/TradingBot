"""Reads the live orders out of the CSV trade journal tail.

Layer-1 helper for :mod:`src.fill_reconciliation`. The journal is written by
:mod:`src.trade_journal` and :mod:`src.execution_service` and grows
continuously, so only the recent tail is parsed - a reconciliation must not load
a whole deployment's history into memory.

The journal contains paper, simulation, and live rows side by side. Only rows
whose ``dry_run`` column is an explicit false are returned: guessing from the
run mode or from the presence of an exchange id would turn a paper row into an
exchange claim, which is exactly the failure this reconciliation exists to catch.
"""

from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# Only live rows have an exchange side to reconcile against; dry-run and
# simulation rows are skipped through the ``dry_run`` column, not guessed at.
RECONCILABLE_ACTIONS = frozenset({"BUY", "SELL"})

# A journal tail is enough: rows older than the window are outside the
# closed-orders retention Kraken offers anyway.
DEFAULT_JOURNAL_LOOKBACK_DAYS = 30
DEFAULT_JOURNAL_READ_LIMIT = 4 * 1024 * 1024


@dataclass(frozen=True)
class JournalOrder:
    """One journalled live order."""

    client_order_id: str
    timestamp: str
    run_id: str
    coin: str
    action: str
    price: float | None
    amount_coin: float | None
    amount_base: float | None
    fee_base: float | None
    requested_amount_coin: float | None
    partial_fill: bool | None


def to_float(value: Any) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y"):
        return True
    if text in ("0", "false", "no", "n"):
        return False
    return None


def within_window(timestamp: str, cutoff: datetime) -> bool:
    raw = str(timestamp or "").strip()
    if not raw:
        return False
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC) >= cutoff


def read_tail_lines(path: str, read_limit: int) -> list[str]:
    """Return the complete lines in the last ``read_limit`` bytes of a file.

    The journal grows continuously and a reconciliation must not read it all
    into memory, so only the tail is parsed. A torn final line is dropped: the
    journal writer appends one row at a time, but a tail read can still land
    between a partial write and its completion.
    """
    with open(path, "rb") as source:
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


def read_journal_orders(
    path: str,
    *,
    lookback_days: int = DEFAULT_JOURNAL_LOOKBACK_DAYS,
    read_limit: int = DEFAULT_JOURNAL_READ_LIMIT,
    now: datetime | None = None,
) -> list[JournalOrder]:
    """Reads the live orders in the recent journal tail.

    Returns ``[]`` for a missing journal - a fresh install simply has nothing to
    reconcile. A journal that exists but cannot be parsed raises, so the caller
    reports an incomplete pass instead of a clean one.
    """
    if not os.path.exists(path):
        return []
    cutoff = (now or datetime.now(UTC)) - timedelta(days=lookback_days)
    lines = read_tail_lines(path, read_limit)
    if not lines:
        return []
    header = next(csv.reader([lines[0]]), [])
    if "action" not in header:
        return []
    orders: list[JournalOrder] = []
    for row in csv.DictReader(lines, fieldnames=header):
        if not row or str(row.get("action", "")).strip().upper() not in RECONCILABLE_ACTIONS:
            continue
        # Only an explicit false means a live row; an unparseable value must not
        # turn a paper row into an exchange claim.
        if to_bool(row.get("dry_run")) is not False:
            continue
        if not within_window(row.get("timestamp", ""), cutoff):
            continue
        orders.append(JournalOrder(
            client_order_id=str(row.get("client_order_id", "") or "").strip(),
            timestamp=str(row.get("timestamp", "") or "").strip(),
            run_id=str(row.get("run_id", "") or "").strip(),
            coin=str(row.get("coin", "") or "").strip().upper(),
            action=str(row.get("action", "")).strip().upper(),
            price=to_float(row.get("price")),
            amount_coin=to_float(row.get("amount_coin")),
            amount_base=to_float(row.get("amount_base")),
            fee_base=to_float(row.get("fee_base")),
            requested_amount_coin=to_float(row.get("requested_amount_coin")),
            partial_fill=to_bool(row.get("partial_fill")),
        ))
    return orders


__all__ = [
    "DEFAULT_JOURNAL_LOOKBACK_DAYS",
    "DEFAULT_JOURNAL_READ_LIMIT",
    "RECONCILABLE_ACTIONS",
    "JournalOrder",
    "read_journal_orders",
    "read_tail_lines",
    "to_bool",
    "to_float",
    "within_window",
]
