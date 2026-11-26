#!/usr/bin/env python3
"""Compare the journalled live orders against the Kraken order history.

The bot journals every order it submits; this command checks what the exchange
actually did with them and prints the verdict. It never writes to the journal,
the portfolio, or the exchange, and it never places an order.

Exit codes are stable so a shell wrapper or timer can act on them:

* ``0`` the pass completed and every journalled live order matched
* ``1`` the pass completed and found a mismatch an operator must review
* ``2`` the pass is incomplete, so the journal is unverified (missing
  credentials, an unreadable journal, or a failing exchange call)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace

from src.fill_reconciliation import (
    STATUS_MATCHED,
    STATUS_NOT_CHECKABLE,
    STATUS_PARTIAL,
    STATUS_UNMATCHED,
    ReconciliationConfig,
    ReconciliationReport,
    iter_findings,
    reconcile_journal_fills,
    report_to_dict,
    resolve_reconciliation_config,
    write_report_json,
    write_report_marker,
)

EXIT_CLEAN = 0
EXIT_MISMATCH = 1
EXIT_INCOMPLETE = 2

DEFAULT_FINDINGS = (STATUS_UNMATCHED, STATUS_PARTIAL, STATUS_NOT_CHECKABLE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconcile the trade journal against exchange order history.",
    )
    parser.add_argument(
        "--journal",
        default=None,
        help="Journal CSV to read (default: $TRADING_JOURNAL_FILE or "
        "$PERFORMANCE_LOG_FILE)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="Only journal rows newer than this (default: "
        "$TRADING_FILL_RECONCILIATION_LOOKBACK_DAYS)",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=None,
        help="Orders requested per exchange history page (default: "
        "$TRADING_FILL_RECONCILIATION_PAGE_LIMIT)",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        default=None,
        help="Explicit unified symbol scope, repeatable (default: derived from the journal)",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Also write a KEY=value marker plus a .json report here (default: "
        "$TRADING_FILL_RECONCILIATION_REPORT)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the machine-readable report instead of the summary",
    )
    parser.add_argument(
        "--findings",
        default=",".join(DEFAULT_FINDINGS),
        help="Comma-separated statuses to list (default: unmatched,partial_fill,"
        "not_checkable)",
    )
    parser.add_argument(
        "--allow-dry-run",
        action="store_true",
        help="Run even when the deployment is a paper deployment; the journal then "
        "has no exchange side and the pass reports why",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> ReconciliationConfig:
    """Apply the CLI overrides on top of the environment-derived config.

    ``enabled`` is forced on: an operator who runs this command deliberately is
    asking for a pass, and the deployment flag only governs the automatic run.
    """
    config = resolve_reconciliation_config()
    overrides: dict[str, object] = {"enabled": True}
    if args.journal:
        overrides["journal_file"] = str(args.journal)
    if args.lookback_days:
        overrides["lookback_days"] = int(args.lookback_days)
    if args.page_limit:
        overrides["page_limit"] = int(args.page_limit)
    if args.symbol:
        overrides["symbols"] = tuple(
            item.strip().upper() for item in args.symbol if item.strip()
        )
    if args.report:
        overrides["report_file"] = str(args.report)
    return replace(config, **overrides)


def dry_run_requested() -> bool:
    raw = os.getenv("DRY_RUN", "true").strip().lower()
    return raw in ("1", "true", "yes", "y", "on")


def print_summary(report: ReconciliationReport, statuses: tuple[str, ...]) -> None:
    counts = report.counts
    print(
        f"Fill reconciliation {'complete' if report.complete else 'INCOMPLETE'}"
        f" ({report.reason})"
    )
    print(f"  journal rows:        {report.journal_rows}")
    print(f"  checked orders:      {report.orders}")
    print(f"  matched:             {counts.get(STATUS_MATCHED, 0)}")
    print(f"  partial fills:       {counts.get(STATUS_PARTIAL, 0)}")
    print(f"  unmatched:           {counts.get(STATUS_UNMATCHED, 0)}")
    print(f"  not checkable:       {counts.get(STATUS_NOT_CHECKABLE, 0)}")
    print(f"  quantity mismatch:   {report.quantity_mismatch}")
    print(f"  fee mismatch:        {report.fee_mismatch}")
    print(f"  exchange calls:      {report.exchange_calls}")
    print(f"  exchange errors:     {report.exchange_errors}")
    print(
        f"  journalled base:     {report.journal_quantity_base:.2f} "
        f"{report.base_currency} (fees {report.journal_fee_base:.2f})"
    )
    print(
        f"  exchange base:       {report.exchange_quantity_base:.2f} "
        f"{report.base_currency} (fees {report.exchange_fee_base:.2f})"
    )
    findings = iter_findings(report, statuses)
    if findings:
        print(f"  findings ({len(findings)}):")
        for finding in findings:
            print(
                f"    {finding.timestamp} {finding.action} {finding.coin} "
                f"[{finding.status}] id={finding.client_order_id}: {finding.reason}"
            )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.allow_dry_run and dry_run_requested():
        print(
            "DRY_RUN is enabled: the journal has no exchange side to compare "
            "against. Pass --allow-dry-run to run anyway.",
            file=sys.stderr,
        )
        return EXIT_INCOMPLETE

    config = config_from_args(args)
    report = reconcile_journal_fills(config)
    if config.report_file:
        write_report_marker(config.report_file, report)
        write_report_json(f"{config.report_file}.json", report)

    if args.json:
        print(json.dumps(report_to_dict(report), sort_keys=True, indent=2))
    else:
        statuses = tuple(
            item.strip() for item in str(args.findings).split(",") if item.strip()
        )
        print_summary(report, statuses or DEFAULT_FINDINGS)

    if not report.complete:
        return EXIT_INCOMPLETE
    return EXIT_MISMATCH if report.has_mismatches else EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
