"""Report shape, persistence, and logging for fill reconciliation.

Layer-1 helper for :mod:`src.fill_reconciliation`. The comparison itself is
pure; this module is what makes the outcome observable: a JSON report for
tooling, a shell-readable ``KEY=value`` marker for the health exporter and
Prometheus alerts, and one log line per finding for the bot journal.

The marker follows the same convention as :mod:`src.task_status` and the weekly
scorecard verdict file, so a shell wrapper can read it and the exporter can
derive an age gauge from it. Writes are atomic: a reader must never see a
half-written marker and conclude the reconciliation was clean.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

STATUS_MATCHED = "matched"
STATUS_PARTIAL = "partial_fill"
STATUS_UNMATCHED = "unmatched"
STATUS_NOT_CHECKABLE = "not_checkable"
STATUS_STATUSES = (
    STATUS_MATCHED,
    STATUS_PARTIAL,
    STATUS_UNMATCHED,
    STATUS_NOT_CHECKABLE,
)

# ``KEY=value`` marker, the same convention as src.task_status and the weekly
# scorecard verdict file, so a shell wrapper can read it too.
MARKER_LINE = re.compile(r"^([a-z][a-z0-9_]*)=(.*)$")

MARKER_KEYS = (
    "schema_version",
    "status",
    "reason",
    "checked_at_unixtime",
    "lookback_days",
    "journal_rows",
    "orders",
    "matched",
    "partial_fills",
    "unmatched",
    "not_checkable",
    "quantity_mismatch",
    "fee_mismatch",
    "exchange_calls",
    "exchange_errors",
    "journal_quantity_base",
    "exchange_quantity_base",
    "journal_fee_base",
    "exchange_fee_base",
)


@dataclass(frozen=True)
class OrderFinding:
    """The verdict for one journalled order."""

    client_order_id: str
    timestamp: str
    coin: str
    action: str
    status: str
    reason: str
    journal_amount_coin: float | None
    exchange_filled_coin: float | None
    journal_amount_base: float | None
    exchange_cost_base: float | None
    journal_fee_base: float | None
    exchange_fee_base: float | None
    exchange_order_id: str = ""
    exchange_status: str = ""


@dataclass(frozen=True)
class ReconciliationReport:
    """Outcome of one reconciliation pass."""

    complete: bool
    reason: str
    checked_at: datetime
    lookback_days: int
    base_currency: str
    journal_rows: int
    orders: int
    counts: dict[str, int]
    findings: list[OrderFinding]
    quantity_mismatch: int
    fee_mismatch: int
    exchange_calls: int
    exchange_errors: int
    journal_quantity_base: float
    exchange_quantity_base: float
    journal_fee_base: float
    exchange_fee_base: float

    @property
    def has_mismatches(self) -> bool:
        """True when the pass completed and found something to act on."""
        return self.complete and (
            self.quantity_mismatch > 0
            or self.fee_mismatch > 0
            or self.counts.get(STATUS_UNMATCHED, 0) > 0
        )


def report_to_dict(report: ReconciliationReport) -> dict[str, Any]:
    """Machine-readable report for the CLI and the health exporter."""
    return {
        "complete": report.complete,
        "reason": report.reason,
        "checked_at": report.checked_at.isoformat(),
        "lookback_days": report.lookback_days,
        "base_currency": report.base_currency,
        "journal_rows": report.journal_rows,
        "orders": report.orders,
        "counts": dict(report.counts),
        "quantity_mismatch": report.quantity_mismatch,
        "fee_mismatch": report.fee_mismatch,
        "exchange_calls": report.exchange_calls,
        "exchange_errors": report.exchange_errors,
        "journal_quantity_base": round(report.journal_quantity_base, 8),
        "exchange_quantity_base": round(report.exchange_quantity_base, 8),
        "journal_fee_base": round(report.journal_fee_base, 8),
        "exchange_fee_base": round(report.exchange_fee_base, 8),
        "findings": [
            {
                "client_order_id": finding.client_order_id,
                "timestamp": finding.timestamp,
                "coin": finding.coin,
                "action": finding.action,
                "status": finding.status,
                "reason": finding.reason,
                "journal_amount_coin": finding.journal_amount_coin,
                "exchange_filled_coin": finding.exchange_filled_coin,
                "journal_amount_base": finding.journal_amount_base,
                "exchange_cost_base": finding.exchange_cost_base,
                "journal_fee_base": finding.journal_fee_base,
                "exchange_fee_base": finding.exchange_fee_base,
                "exchange_order_id": finding.exchange_order_id,
                "exchange_status": finding.exchange_status,
            }
            for finding in report.findings
        ],
    }


def atomic_write_text(path: str, body: str) -> None:
    """Write ``body`` to ``path`` atomically, creating the parent directory."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory,
            prefix=".fill-reconciliation-", suffix=".tmp", delete=False,
        ) as handle:
            temporary_path = handle.name
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def write_report_json(path: str, report: ReconciliationReport) -> None:
    """Atomically writes the detailed report; a failure is logged, never raised."""
    try:
        body = json.dumps(report_to_dict(report), sort_keys=True, indent=2) + "\n"
        atomic_write_text(path, body)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Fill reconciliation report could not be written to %s: %s", path, exc)


def report_marker_lines(report: ReconciliationReport) -> list[str]:
    """Shell-readable ``KEY=value`` summary, uppercase like the scorecard marker."""
    counts = report.counts
    values = {
        "schema_version": "1",
        "status": "complete" if report.complete else "incomplete",
        "reason": report.reason,
        "checked_at_unixtime": str(int(report.checked_at.timestamp())),
        "lookback_days": str(report.lookback_days),
        "journal_rows": str(report.journal_rows),
        "orders": str(report.orders),
        "matched": str(counts.get(STATUS_MATCHED, 0)),
        "partial_fills": str(counts.get(STATUS_PARTIAL, 0)),
        "unmatched": str(counts.get(STATUS_UNMATCHED, 0)),
        "not_checkable": str(counts.get(STATUS_NOT_CHECKABLE, 0)),
        "quantity_mismatch": str(report.quantity_mismatch),
        "fee_mismatch": str(report.fee_mismatch),
        "exchange_calls": str(report.exchange_calls),
        "exchange_errors": str(report.exchange_errors),
        "journal_quantity_base": f"{report.journal_quantity_base:.8f}",
        "exchange_quantity_base": f"{report.exchange_quantity_base:.8f}",
        "journal_fee_base": f"{report.journal_fee_base:.8f}",
        "exchange_fee_base": f"{report.exchange_fee_base:.8f}",
    }
    return [f"{key.upper()}={values[key]}" for key in MARKER_KEYS]


def write_report_marker(path: str, report: ReconciliationReport) -> None:
    """Atomically writes the summary marker; a failure is logged, never raised."""
    try:
        atomic_write_text(path, "\n".join(report_marker_lines(report)) + "\n")
    except OSError as exc:
        logger.warning("Fill reconciliation marker could not be written to %s: %s", path, exc)


def read_report_marker(path: str) -> dict[str, str] | None:
    """Parse the summary marker, or ``None`` when it is missing or unusable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as source:
            text = source.read()
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = MARKER_LINE.match(line.strip().lower())
        if match:
            values[match.group(1)] = match.group(2)
    return values or None


def marker_age_seconds(values: Mapping[str, str], now: datetime | None = None) -> float | None:
    """Seconds since the marker was written, or ``None`` when unparseable."""
    try:
        finished = float(str(values.get("checked_at_unixtime", "")).strip())
    except ValueError:
        return None
    if finished <= 0.0:
        return None
    reference = now or datetime.now(UTC)
    return max(0.0, (reference - datetime.fromtimestamp(finished, UTC)).total_seconds())


def marker_int(values: Mapping[str, str], key: str) -> int | None:
    """Read an integer marker field, or ``None`` when absent or malformed."""
    try:
        return int(str(values.get(key, "")).strip())
    except ValueError:
        return None


def log_report(report: ReconciliationReport, log: logging.Logger = logger) -> None:
    """One summary line plus one line per finding, so the bot log is auditable."""
    if not report.complete:
        log.warning(
            "Fill reconciliation incomplete (%s): %d journalled live order(s) were not verified.",
            report.reason, report.journal_rows,
        )
        return
    log.info(
        "Fill reconciliation: %d/%d journalled live order(s) matched the exchange "
        "(partial=%d unmatched=%d not_checkable=%d quantity_mismatch=%d fee_mismatch=%d).",
        report.counts.get(STATUS_MATCHED, 0), report.orders,
        report.counts.get(STATUS_PARTIAL, 0), report.counts.get(STATUS_UNMATCHED, 0),
        report.counts.get(STATUS_NOT_CHECKABLE, 0), report.quantity_mismatch,
        report.fee_mismatch,
    )
    for finding in report.findings:
        if finding.reason == "fill matches the exchange order":
            continue
        log.warning(
            "Fill reconciliation finding %s %s %s [%s]: %s",
            finding.timestamp, finding.action, finding.coin, finding.status, finding.reason,
        )


def iter_findings(report: ReconciliationReport, statuses: Iterable[str]) -> list[OrderFinding]:
    """Findings filtered by status, for the operator CLI."""
    wanted = set(statuses)
    return [finding for finding in report.findings if finding.status in wanted]


__all__ = [
    "MARKER_KEYS",
    "MARKER_LINE",
    "STATUS_MATCHED",
    "STATUS_NOT_CHECKABLE",
    "STATUS_PARTIAL",
    "STATUS_STATUSES",
    "STATUS_UNMATCHED",
    "OrderFinding",
    "ReconciliationReport",
    "atomic_write_text",
    "iter_findings",
    "log_report",
    "marker_age_seconds",
    "marker_int",
    "read_report_marker",
    "report_marker_lines",
    "report_to_dict",
    "write_report_json",
    "write_report_marker",
]
