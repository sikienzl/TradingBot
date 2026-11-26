#!/usr/bin/env python3
"""Inspect and maintain the scorecard verdict history.

``scripts/run_weekly_scorecard.sh`` calls ``append`` in its exit trap, so every
attempted run - ``GO``, ``HOLD``, ``NO-GO`` or ``ERROR`` - leaves one immutable
record behind. The subcommands exist for operators and for the tuning chain:

* ``append`` - publish the current marker as a history record (idempotent).
* ``show``   - print the recent records, oldest first.
* ``stats``  - print a ``KEY=value`` summary that shell scripts can consume.

Exit codes: ``0`` when the requested work succeeded, ``1`` when the history is
disabled or unusable. ``append`` never returns non-zero, because a history
problem must never change a scorecard verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from src.scorecard_verdict_history import (
    DEFAULT_STATUS_ENV_FILE,
    DEFAULT_STATUS_JSON_FILE,
    DEFAULT_VERDICT_HISTORY_FILE,
    HISTORY_PATH_ENV,
    append_verdict_record,
    history_stats,
    read_verdict_history,
    resolve_scorecard_status,
    resolve_verdict_history_path,
)

EXIT_OK = 0
EXIT_UNAVAILABLE = 1


def _history_path(args: argparse.Namespace) -> str:
    if args.history_file is not None:
        raw = args.history_file.strip()
        return "" if raw.lower() in {"off", "none", "false", "no", "disabled"} else raw
    return resolve_verdict_history_path()


def _metrics_from_report(metrics_json: str) -> dict[str, object]:
    """Metrics from the scorecard ``--metrics-json`` payload, empty when unusable."""
    if not metrics_json.strip():
        return {}
    try:
        with open(metrics_json, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    metrics = payload.get("metrics")
    return dict(metrics) if isinstance(metrics, dict) else {}


def _cmd_append(args: argparse.Namespace) -> int:
    history_file = _history_path(args)
    if not history_file:
        print("Verdict history disabled, nothing appended.")
        return EXIT_OK
    fields = resolve_scorecard_status(
        status_json_file=args.status_json,
        status_env_file=args.status_env,
        history_file="",
    )
    if fields is None:
        print(
            f"No usable scorecard marker at {args.status_json} or {args.status_env}; "
            "no history record written."
        )
        return EXIT_OK
    profile = os.environ.get("SCORECARD_HISTORY_PROFILE", "").strip()
    if profile:
        fields["profile"] = profile
    reason = os.environ.get("SCORECARD_HISTORY_PRIMARY_REASON", "").strip()
    if reason and "primary_reason" not in fields:
        fields["primary_reason"] = reason
    if not fields.get("metrics"):
        fields["metrics"] = _metrics_from_report(args.metrics_json)
    outcome = append_verdict_record(history_file, fields)
    print(
        f"Verdict history {outcome}: {fields['verdict']} "
        f"({fields['timestamp_utc']}) -> {history_file}"
    )
    return EXIT_OK


def _cmd_show(args: argparse.Namespace) -> int:
    history_file = _history_path(args)
    if not history_file:
        print("Verdict history is disabled.", file=sys.stderr)
        return EXIT_UNAVAILABLE
    records = read_verdict_history(history_file, limit=args.limit)
    if not records:
        print(f"No verdict records in {history_file}")
        return EXIT_UNAVAILABLE
    print(f"=== Scorecard verdict history: {history_file} ===")
    for record in records:
        reason = str(record.get("primary_reason") or "none")
        print(
            f"{record.get('timestamp_utc')}  {record.get('verdict'):<5} "
            f"mode={record.get('run_mode')}  reason={reason}"
        )
    return EXIT_OK


def _cmd_stats(args: argparse.Namespace) -> int:
    history_file = _history_path(args)
    if not history_file:
        print("history_enabled=0")
        return EXIT_UNAVAILABLE
    stats = history_stats(history_file)
    print("history_enabled=1")
    print(f"history_path={stats['path']}")
    print(f"history_exists={int(stats['exists'])}")
    print(f"history_records={stats['records']}")
    print(f"history_newest_timestamp_utc={stats['newest_timestamp_utc'] or ''}")
    print(f"history_newest_verdict={stats['newest_verdict'] or ''}")
    print(f"history_go_streak={stats['go_streak']}")
    print(f"history_no_go_streak={stats['no_go_streak']}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and maintain the scorecard verdict history."
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--history-file",
        default=None,
        help=(
            f"History JSONL path. Defaults to ${HISTORY_PATH_ENV} or "
            f"{DEFAULT_VERDICT_HISTORY_FILE}; 'off'/'none'/'false' disables it."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    append = sub.add_parser(
        "append", parents=[common], help="Publish the current marker as a record"
    )
    append.add_argument(
        "--status-json",
        default=DEFAULT_STATUS_JSON_FILE,
        help="Scorecard JSON status marker to read.",
    )
    append.add_argument(
        "--status-env",
        default=DEFAULT_STATUS_ENV_FILE,
        help="Scorecard KEY=value status marker to read.",
    )
    append.add_argument(
        "--metrics-json",
        default="",
        help="Scorecard --metrics-json payload, used when the marker has no metrics.",
    )
    append.set_defaults(func=_cmd_append)

    show = sub.add_parser(
        "show", parents=[common], help="Print recent records, oldest first"
    )
    show.add_argument(
        "--limit", type=int, default=20, help="How many records to print."
    )
    show.set_defaults(func=_cmd_show)

    stats = sub.add_parser(
        "stats", parents=[common], help="Print a KEY=value summary"
    )
    stats.set_defaults(func=_cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
