"""Prune the append-only JSONL stores to the configured retention policy.

The research news store and the scorecard verdict history are append-only by
design, so nothing else ever removes a record and both files grow with every
hourly fetch and every weekly scorecard run. This command is the retention
policy: it rewrites each store under the same append lock the writers use,
keeps a backup copy, and records what it did in a status marker.

It never touches the trade journal, the portfolio state, or the exchange, and it
never places an order.

Exit codes are stable so a systemd timer or a wrapper can act on them:

* ``0`` every enabled store was pruned or needed no change
* ``1`` at least one store could not be pruned, so it still grows
* ``2`` retention is switched off, so nothing was pruned
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import Any

from src.jsonl_retention import (
    MARKER_KEYS,
    RETENTION_SCHEMA_VERSION,
    StoreRetention,
    env_flag,
    prune_jsonl_store,
    resolve_backups,
    resolve_news_retention,
    resolve_report_path,
    resolve_verdict_retention,
    write_report_marker,
)

EXIT_PRUNED = 0
EXIT_STORE_FAILED = 1
EXIT_DISABLED = 2

# Prefixes are the store names as they appear in the marker keys.
STORES = (("news", resolve_news_retention), ("verdict", resolve_verdict_retention))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prune the append-only JSONL stores to the retention policy.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be removed without touching any store",
    )
    parser.add_argument(
        "--news-store",
        default=None,
        help="Override the news store path (default: $RESEARCH_NEWS_STORE_PATH)",
    )
    parser.add_argument(
        "--verdict-store",
        default=None,
        help="Override the verdict history path (default: "
        "$SCORECARD_VERDICT_HISTORY_FILE)",
    )
    parser.add_argument(
        "--news-days",
        type=int,
        default=None,
        help="News horizon in days (default: $TRADING_JSONL_RETENTION_NEWS_DAYS, "
        "0 disables the age limit)",
    )
    parser.add_argument(
        "--news-records",
        type=int,
        default=None,
        help="Minimum news records to keep (default: "
        "$TRADING_JSONL_RETENTION_NEWS_RECORDS, 0 disables the floor)",
    )
    parser.add_argument(
        "--verdict-days",
        type=int,
        default=None,
        help="Verdict horizon in days (default: "
        "$TRADING_JSONL_RETENTION_VERDICT_DAYS, 0 disables the age limit)",
    )
    parser.add_argument(
        "--verdict-records",
        type=int,
        default=None,
        help="Minimum verdict records to keep (default: "
        "$TRADING_JSONL_RETENTION_VERDICT_RECORDS, 0 disables the floor)",
    )
    parser.add_argument(
        "--backups",
        type=int,
        default=None,
        help="Pre-prune copies to keep next to each store (default: "
        "$TRADING_JSONL_RETENTION_BACKUPS)",
    )
    parser.add_argument(
        "--report-file",
        default=None,
        help="Where to write the run summary marker (default: "
        "$TRADING_JSONL_RETENTION_REPORT)",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Do not write a run summary marker",
    )
    return parser


def _apply_overrides(
    rule: StoreRetention, path: str | None, days: int | None, records: int | None
) -> StoreRetention:
    if path is None and days is None and records is None:
        return rule
    return StoreRetention(
        store=rule.store,
        path=rule.path if path is None else path.strip(),
        days=rule.days if days is None else max(days, 0),
        min_records=rule.min_records if records is None else max(records, 0),
    )


def _marker_values(
    started: datetime,
    finished: datetime,
    exit_code: int,
    rules: dict[str, StoreRetention],
    results: dict[str, dict[str, Any]],
    backups: int,
    dry_run: bool,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "schema_version": RETENTION_SCHEMA_VERSION,
        "status": "complete" if exit_code != EXIT_STORE_FAILED else "incomplete",
        "reason": (
            "dry_run"
            if dry_run
            else ("ok" if exit_code == EXIT_PRUNED else "store_failed")
        ),
        "exit_code": exit_code,
        "started_unixtime": int(started.timestamp()),
        "finished_unixtime": int(finished.timestamp()),
        "backups_kept": backups,
    }
    for prefix, rule in rules.items():
        # The effective policy belongs in the marker: an operator must be able
        # to tell a pruned store from a store the policy simply protects.
        values[f"{prefix}_enabled"] = 1 if rule.enabled else 0
        values[f"{prefix}_path"] = rule.path
        values[f"{prefix}_days"] = rule.days
        values[f"{prefix}_min_records"] = rule.min_records
    for prefix, result in results.items():
        for key, value in result.items():
            if key == "store":
                continue
            values[f"{prefix}_{key}"] = value
    return {key: values[key] for key in MARKER_KEYS if key in values}


def print_summary(results: dict[str, dict[str, Any]], *, dry_run: bool) -> None:
    verb = "would prune" if dry_run else "pruned"
    for prefix in ("news", "verdict"):
        result = results[prefix]
        print(
            f"  {prefix:<8} {result['reason']:<10} "
            f"scanned={result['scanned']} {verb}={result['pruned']} "
            f"kept={result['kept']} malformed={result['malformed']} "
            f"undated={result['undated']} bytes={result['bytes']}"
            + (f" error={result['error']}" if result["error"] else "")
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not env_flag("TRADING_JSONL_RETENTION_ENABLED", True):
        print(
            "Retention is disabled (TRADING_JSONL_RETENTION_ENABLED). Nothing "
            "was pruned; the stores keep growing.",
            file=sys.stderr,
        )
        return EXIT_DISABLED

    started = datetime.now(UTC)
    backups = resolve_backups() if args.backups is None else max(args.backups, 0)
    overrides = {
        "news": (args.news_store, args.news_days, args.news_records),
        "verdict": (args.verdict_store, args.verdict_days, args.verdict_records),
    }
    rules: dict[str, StoreRetention] = {}
    results: dict[str, dict[str, Any]] = {}
    for prefix, resolver in STORES:
        rule = _apply_overrides(resolver(), *overrides[prefix])
        rules[prefix] = rule
        results[prefix] = prune_jsonl_store(
            rule, dry_run=args.dry_run, backups=backups)

    exit_code = EXIT_STORE_FAILED if any(
        result["enabled"] and result["reason"] in ("error", "too_large")
        for result in results.values()
    ) else EXIT_PRUNED

    print_summary(results, dry_run=args.dry_run)

    report_path = "" if args.no_report else (
        args.report_file if args.report_file is not None else resolve_report_path())
    if report_path:
        finished = datetime.now(UTC)
        if not write_report_marker(
            report_path,
            _marker_values(
                started,
                finished,
                exit_code,
                rules,
                results,
                backups,
                args.dry_run,
            ),
        ):
            print(
                f"Retention run summary could not be written to {report_path}",
                file=sys.stderr,
            )
            exit_code = EXIT_STORE_FAILED
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
