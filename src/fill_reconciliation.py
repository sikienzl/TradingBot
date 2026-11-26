"""Reconcile journalled live orders against Kraken order history.

The bot journals every order it submits, including the deterministic
``client_order_id`` it passed to Kraken. Nothing used to compare those rows with
what Kraken actually filled: the startup checks in
:mod:`src.portfolio_bootstrap` only compare the persisted portfolio against
``fetch_balance`` and refuse to start with open orders, so a journal row that
never filled, or filled for a different quantity, stayed invisible and an
operator had to diff the CSV against the exchange by hand.

This module performs that comparison and reports it. It never mutates the
journal, the portfolio, or the exchange - the journal is an append-only audit
trail, and rewriting it would destroy the very evidence the reconciliation is
meant to protect. The result is a machine-readable report that the bot logs,
the operator CLI prints, and the health exporter turns into gauges.

The three collaborators are deliberately separate layer-1 modules:

* :mod:`src.journal_order_reader` - the read-only journal tail.
* :mod:`src.exchange_order_history` - everything that talks to the exchange.
* :mod:`src.fill_reconciliation_report` - report shape, markers, and logging.

What stays here is the configuration, the per-row comparison, and the
orchestration, so the whole fail-closed policy is readable in one place.

Design rules taken from the repository constraints:

* Fail closed. Missing credentials, an unsupported or failing exchange call, or
  an unreadable journal produce ``complete=False`` plus a reason. An incomplete
  pass must never be read as "everything matches", and it must never stop the
  bot from starting.
* Match on the journalled ``client_order_id``. :mod:`src.order_submission`
  builds it from ``run_id`` + ``config_hash`` + iteration, and ``run_id`` is a
  random uuid per process, so the id is only reconstructible when
  ``TRADING_RUN_ID`` is pinned. The journal is the source of truth for the id;
  it is never recomputed here.
* Bound the exchange calls. Kraken's order endpoints are paginated and rate
  limited, so the reconciliation asks for one page per scope and resolves the
  remaining ids with an explicit ``params={"clientOrderId": ...}`` filter.
* Currency keys are Kraken's (``XBT``, ``XXBT``, ``ZUSD``) while the journal
  stores the bot's coin names, so both sides are normalised with
  :func:`src.portfolio_bootstrap.currency_aliases`.
"""

from __future__ import annotations

import csv
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.exchange_order_history import (
    DEFAULT_ORDER_PAGE_LIMIT,
    MAX_SYMBOL_SCOPES,
    ExchangeOrder,
    build_exchange,
    collect_exchange_orders,
    journal_symbols,
    lookup_order_by_id,
    order_currency_matches,
    order_side_matches,
    parse_exchange_order,
)
from src.fill_reconciliation_report import (
    STATUS_MATCHED,
    STATUS_NOT_CHECKABLE,
    STATUS_PARTIAL,
    STATUS_STATUSES,
    STATUS_UNMATCHED,
    OrderFinding,
    ReconciliationReport,
    iter_findings,
    log_report,
    marker_age_seconds,
    marker_int,
    read_report_marker,
    report_marker_lines,
    report_to_dict,
    write_report_json,
    write_report_marker,
)
from src.journal_order_reader import (
    DEFAULT_JOURNAL_LOOKBACK_DAYS,
    DEFAULT_JOURNAL_READ_LIMIT,
    JournalOrder,
    read_journal_orders,
)

logger = logging.getLogger(__name__)


# Rows written before the idempotency work carry run_id/config_hash = 'legacy'.
LEGACY_RUN_ID = "legacy"


# Fill quantities are compared with the same relative tolerance the balance
# comparison in src.portfolio_bootstrap uses.
FILL_REL_TOLERANCE = 1e-6

ENABLED_ENV = "TRADING_FILL_RECONCILIATION_ENABLED"
LOOKBACK_ENV = "TRADING_FILL_RECONCILIATION_LOOKBACK_DAYS"
PAGE_LIMIT_ENV = "TRADING_FILL_RECONCILIATION_PAGE_LIMIT"
REPORT_ENV = "TRADING_FILL_RECONCILIATION_REPORT"
SYMBOLS_ENV = "TRADING_FILL_RECONCILIATION_SYMBOLS"
JOURNAL_FILE_ENV = "TRADING_JOURNAL_FILE"
JOURNAL_READ_LIMIT_ENV = "TRADING_JOURNAL_READ_LIMIT"

# The bot itself journals to PERFORMANCE_LOG_FILE (src/bot_config.py), while the
# health exporter and the scorecard read TRADING_JOURNAL_FILE. Both names are
# accepted so a standard deployment needs no extra variable.
BOT_JOURNAL_FILE_ENV = "PERFORMANCE_LOG_FILE"


@dataclass(frozen=True)
class ReconciliationConfig:
    """What the operator wants reconciled, resolved from the environment.

    Deliberately independent of :class:`src.bot_config.BotConfig`: the bot
    already owns the run-mode settings, while the health exporter and the
    operator CLI need only these few, and ``bot_config.py`` is pinned at its
    size ceiling.
    """

    enabled: bool
    journal_file: str
    base_currency: str
    exchange_name: str
    lookback_days: int
    read_limit: int
    page_limit: int
    report_file: str
    api_key: str
    api_secret: str
    symbols: tuple[str, ...] = ()


@dataclass
class _Pass:
    """Mutable counters for one reconciliation pass."""

    counts: dict[str, int] = field(
        default_factory=lambda: {status: 0 for status in STATUS_STATUSES}
    )
    findings: list[OrderFinding] = field(default_factory=list)
    quantity_mismatch: int = 0
    fee_mismatch: int = 0
    calls: int = 0
    errors: int = 0
    journal_quantity: float = 0.0
    exchange_quantity: float = 0.0
    journal_fees: float = 0.0
    exchange_fees: float = 0.0

    def record(
        self,
        journal: JournalOrder,
        order: ExchangeOrder | None,
        status: str,
        reason: str,
    ) -> None:
        self.counts[status] = self.counts.get(status, 0) + 1
        self.findings.append(_finding(journal, order, status, reason))

    def add_totals(
        self, journal: JournalOrder, order: ExchangeOrder, include_exchange: bool
    ) -> None:
        self.journal_quantity += _journal_base(journal) or 0.0
        self.journal_fees += journal.fee_base or 0.0
        if include_exchange:
            self.exchange_quantity += _exchange_base(order, journal) or 0.0
            self.exchange_fees += order.fee_base or 0.0


def resolve_reconciliation_config(
    env: Mapping[str, str] | None = None,
) -> ReconciliationConfig:
    """Reads the reconciliation settings from the environment.

    The journal path and base currency default to the bot's own values, so a
    standard deployment needs no extra configuration. ``DRY_RUN`` is the default
    for the enable flag: a paper deployment must not need Kraken credentials,
    and a dry-run journal has no exchange side to compare against.
    """
    source = os.environ if env is None else env
    root = source.get("TRADING_DATA_DIR", ".").strip() or "."
    journal_default = os.path.join(root, "trade_journal.csv")
    journal_file = (
        source.get(JOURNAL_FILE_ENV, "").strip()
        or source.get(BOT_JOURNAL_FILE_ENV, "").strip()
        or journal_default
    )
    return ReconciliationConfig(
        enabled=_env_bool(source, ENABLED_ENV, not _env_bool(source, "DRY_RUN", True)),
        journal_file=journal_file,
        base_currency=source.get("BASE_CURRENCY", "EUR").strip() or "EUR",
        exchange_name=source.get("EXCHANGE_NAME", "kraken").strip().lower() or "kraken",
        lookback_days=_env_int(source, LOOKBACK_ENV, DEFAULT_JOURNAL_LOOKBACK_DAYS),
        read_limit=_env_int(source, JOURNAL_READ_LIMIT_ENV, DEFAULT_JOURNAL_READ_LIMIT),
        page_limit=_env_int(source, PAGE_LIMIT_ENV, DEFAULT_ORDER_PAGE_LIMIT),
        report_file=source.get(REPORT_ENV, "").strip(),
        api_key=source.get("KRAKEN_API_KEY", "").strip(),
        api_secret=source.get("KRAKEN_API_SECRET", "").strip(),
        symbols=_env_symbols(source.get(SYMBOLS_ENV, "")),
    )


def _env_bool(source: Mapping[str, str], name: str, default: bool) -> bool:
    raw = source.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(source: Mapping[str, str], name: str, default: int) -> int:
    raw = str(source.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_symbols(raw: str) -> tuple[str, ...]:
    values = (item.strip().upper() for item in str(raw or "").split(","))
    return tuple(item for item in values if item)[:MAX_SYMBOL_SCOPES]


def _expected_coin(order: JournalOrder) -> float | None:
    """The quantity the exchange was asked to fill, as journalled."""
    if order.requested_amount_coin is not None:
        return order.requested_amount_coin
    if order.amount_coin is not None:
        return order.amount_coin
    if order.amount_base is not None and order.price:
        return order.amount_base / order.price
    return None


def _journal_base(order: JournalOrder) -> float | None:
    if order.amount_base is not None:
        return order.amount_base
    if order.amount_coin is not None and order.price:
        return order.amount_coin * order.price
    return None


def _exchange_base(order: ExchangeOrder, journal: JournalOrder) -> float | None:
    if order.cost is not None:
        return order.cost
    filled = order.filled
    if filled is None and order.status in ("closed", "filled"):
        filled = order.amount
    if filled is not None and order.average:
        return filled * order.average
    if journal.amount_base is not None and order.filled is not None and journal.price:
        return order.filled * journal.price
    return None


def _close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(1e-8, abs(a) * FILL_REL_TOLERANCE)


def _finding(
    journal: JournalOrder,
    exchange_order: ExchangeOrder | None,
    status: str,
    reason: str,
) -> OrderFinding:
    return OrderFinding(
        client_order_id=journal.client_order_id,
        timestamp=journal.timestamp,
        coin=journal.coin,
        action=journal.action,
        status=status,
        reason=reason,
        journal_amount_coin=journal.amount_coin,
        exchange_filled_coin=None if exchange_order is None else exchange_order.filled,
        journal_amount_base=_journal_base(journal),
        exchange_cost_base=(
            None if exchange_order is None else _exchange_base(exchange_order, journal)
        ),
        journal_fee_base=journal.fee_base,
        exchange_fee_base=None if exchange_order is None else exchange_order.fee_base,
        exchange_order_id="" if exchange_order is None else exchange_order.order_id,
        exchange_status="" if exchange_order is None else exchange_order.status,
    )


def _compare(journal: JournalOrder, order: ExchangeOrder) -> tuple[str, str, bool, bool]:
    """Return ``(status, reason, quantity_mismatch, fee_mismatch)``.

    A cancelled exchange order is only a mismatch when the journal booked a fill
    for it: a cancelled order with an empty position is exactly what a rejected
    signal should look like, and is reported as matched.
    """
    if order.status in ("canceled", "cancelled", "expired", "rejected"):
        if (journal.amount_coin or 0.0) > 0:
            return (
                STATUS_UNMATCHED,
                f"exchange order is {order.status} but the journal booked a fill",
                True,
                False,
            )
        return (
            STATUS_MATCHED,
            f"exchange order is {order.status}, journal booked no fill",
            False,
            False,
        )

    expected = _expected_coin(journal)
    filled = order.filled
    if filled is None and order.status in ("closed", "filled"):
        filled = order.amount
    if expected is None or filled is None:
        return STATUS_NOT_CHECKABLE, "fill quantity is not reported on both sides", False, False

    if not _close(expected, filled):
        status = STATUS_PARTIAL if filled < expected else STATUS_UNMATCHED
        return (
            status,
            f"journal booked {expected:.8f} {journal.coin}, exchange filled {filled:.8f}",
            True,
            False,
        )

    if journal.fee_base is not None and order.fee_base is not None and not _close(
        journal.fee_base,
        order.fee_base,
    ):
        return (
            STATUS_MATCHED,
            "fill matches; the journalled fee differs from the exchange fee record",
            False,
            True,
        )
    return STATUS_MATCHED, "fill matches the exchange order", False, False


def _resolve_order(
    config: ReconciliationConfig,
    exchange: Any,
    journal: JournalOrder,
    by_id: Mapping[str, list[Mapping[str, Any]]],
    state: _Pass,
    log: logging.Logger,
) -> ExchangeOrder | None:
    """Find the exchange order for one journal row, or ``None``.

    A lookup error is counted on ``state`` and recorded as ``not_checkable``;
    only a confirmed absence counts as a mismatch.
    """
    candidates = [
        parsed
        for parsed in (
            parse_exchange_order(
                raw,
                base_currency=config.base_currency,
                traded_asset=journal.coin,
            )
            for raw in by_id.get(journal.client_order_id, [])
        )
        if parsed is not None
    ]
    order = next(
        (
            parsed
            for parsed in candidates
            if order_currency_matches(parsed, journal.coin)
            and order_side_matches(parsed, journal.action)
        ),
        candidates[0] if candidates else None,
    )
    if order is not None:
        return order

    state.calls += 1
    order, lookup_error = lookup_order_by_id(exchange, journal, config.base_currency, log)
    if lookup_error:
        state.errors += 1
        state.record(
            journal,
            None,
            STATUS_NOT_CHECKABLE,
            f"exchange order lookup failed, the row is unverified: {lookup_error}",
        )
        return None
    if order is None:
        state.record(
            journal,
            None,
            STATUS_UNMATCHED,
            "no matching exchange order in the lookback window",
        )
        state.quantity_mismatch += 1
    return order


def _apply_missing_id(journal: JournalOrder, state: _Pass) -> None:
    """Count a row that cannot be matched because it has no journalled id."""
    if journal.client_order_id:
        return
    reason = (
        "journal row predates client order ids"
        if journal.run_id in ("", LEGACY_RUN_ID)
        else "journal row has no client order id"
    )
    state.record(journal, None, STATUS_NOT_CHECKABLE, reason)
    state.journal_quantity += _journal_base(journal) or 0.0
    state.journal_fees += journal.fee_base or 0.0


def _apply_verdict(journal: JournalOrder, order: ExchangeOrder, state: _Pass) -> None:
    status, reason, quantity_bad, fee_bad = _compare(journal, order)
    state.counts[status] = state.counts.get(status, 0) + 1
    state.findings.append(_finding(journal, order, status, reason))
    state.quantity_mismatch += int(quantity_bad)
    state.fee_mismatch += int(fee_bad)
    state.add_totals(journal, order, include_exchange=status != STATUS_UNMATCHED)


def reconcile_journal_fills(
    config: ReconciliationConfig,
    *,
    exchange: Any = None,
    now: datetime | None = None,
    log: logging.Logger = logger,
) -> ReconciliationReport:
    """Compare the journalled live orders with the exchange order history.

    Never raises: every failure mode produces an incomplete report with a
    reason, because a reconciliation problem must be visible without being able
    to stop the bot or the health export.
    """
    checked_at = now or datetime.now(UTC)
    if not config.enabled:
        return _report(False, "disabled", checked_at, config, _Pass(), 0)

    try:
        journal_orders = read_journal_orders(
            config.journal_file,
            lookback_days=config.lookback_days,
            read_limit=config.read_limit,
            now=checked_at,
        )
    except (OSError, UnicodeError, csv.Error) as exc:
        log.warning("Fill reconciliation could not read %s: %s", config.journal_file, exc)
        return _report(False, "journal unreadable", checked_at, config, _Pass(), 0)

    if not journal_orders:
        return _report(
            True, "no live orders in the lookback window", checked_at, config, _Pass(), 0
        )

    if exchange is None:
        exchange = build_exchange(config.exchange_name, config.api_key, config.api_secret)
    if exchange is None:
        reason = (
            "no authenticated exchange"
            if not config.api_key or not config.api_secret
            else "exchange client unavailable"
        )
        return _report(False, reason, checked_at, config, _Pass(), len(journal_orders))

    state = _Pass()
    symbols = journal_symbols(journal_orders, config.base_currency, config.symbols)
    since_ms = int((checked_at.timestamp() - config.lookback_days * 86400) * 1000)
    raw_orders, calls, errors = collect_exchange_orders(
        exchange,
        since_ms,
        page_limit=config.page_limit,
        symbols=symbols,
        log=log,
    )
    state.calls += calls
    state.errors += errors
    by_id: dict[str, list[Mapping[str, Any]]] = {}
    for raw in raw_orders:
        client_order_id = str(raw.get("clientOrderId") or "").strip()
        if client_order_id:
            by_id.setdefault(client_order_id, []).append(raw)

    for journal in journal_orders:
        if journal.client_order_id:
            order = _resolve_order(config, exchange, journal, by_id, state, log)
            if order is not None:
                _apply_verdict(journal, order, state)
        else:
            _apply_missing_id(journal, state)

    complete = state.errors == 0
    reason = "reconciled" if complete else f"{state.errors} exchange order lookup(s) failed"
    return _report(complete, reason, checked_at, config, state, len(journal_orders))


def _report(
    complete: bool,
    reason: str,
    checked_at: datetime,
    config: ReconciliationConfig,
    state: _Pass,
    journal_rows: int,
) -> ReconciliationReport:
    return ReconciliationReport(
        complete=complete,
        reason=reason,
        checked_at=checked_at,
        lookback_days=config.lookback_days,
        base_currency=config.base_currency,
        journal_rows=journal_rows,
        orders=len(state.findings),
        counts=dict(state.counts),
        findings=list(state.findings),
        quantity_mismatch=state.quantity_mismatch,
        fee_mismatch=state.fee_mismatch,
        exchange_calls=state.calls,
        exchange_errors=state.errors,
        journal_quantity_base=state.journal_quantity,
        exchange_quantity_base=state.exchange_quantity,
        journal_fee_base=state.journal_fees,
        exchange_fee_base=state.exchange_fees,
    )


def reconcile_and_report(
    config: ReconciliationConfig,
    *,
    exchange: Any = None,
    now: datetime | None = None,
    log: logging.Logger = logger,
) -> ReconciliationReport:
    """Reconcile, log, and persist the markers. Never raises."""
    report = reconcile_journal_fills(config, exchange=exchange, now=now, log=log)
    log_report(report, log=log)
    if config.report_file:
        write_report_marker(config.report_file, report)
        write_report_json(f"{config.report_file}.json", report)
    return report


__all__ = [
    "DEFAULT_JOURNAL_LOOKBACK_DAYS",
    "DEFAULT_JOURNAL_READ_LIMIT",
    "DEFAULT_ORDER_PAGE_LIMIT",
    "MAX_SYMBOL_SCOPES",
    "STATUS_MATCHED",
    "STATUS_NOT_CHECKABLE",
    "STATUS_PARTIAL",
    "STATUS_STATUSES",
    "STATUS_UNMATCHED",
    "ExchangeOrder",
    "JournalOrder",
    "OrderFinding",
    "ReconciliationConfig",
    "ReconciliationReport",
    "build_exchange",
    "collect_exchange_orders",
    "iter_findings",
    "journal_symbols",
    "log_report",
    "marker_age_seconds",
    "marker_int",
    "parse_exchange_order",
    "read_journal_orders",
    "read_report_marker",
    "reconcile_and_report",
    "reconcile_journal_fills",
    "report_marker_lines",
    "report_to_dict",
    "resolve_reconciliation_config",
    "write_report_json",
    "write_report_marker",
]
