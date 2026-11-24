#!/usr/bin/env python3
"""Read-only UTC daily observation of paper journals and bot logs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from daily_dryrun_review import _parse_ts

# Explicit allowlist: never copy credentials, tokens, service URLs or arbitrary env values.
CONFIG_KEYS = (
    "DRY_RUN", "SIMULATE_DATA", "SIMULATION_REGIME", "BASE_CURRENCY",
    "EXCLUDED_COINS", "MIN_ENTRY_ATR_PCT",
    "DRY_RUN_COSTS_ENABLED", "DRY_RUN_FEE_RATE", "DRY_RUN_SLIPPAGE_RATE",
    "CHECK_INTERVAL", "TOP_N_FOR_ANALYSIS", "USE_TABULAR_MODEL",
    "TABULAR_MODEL_BACKEND", "TABULAR_MIN_CONFIDENCE",
    "TABULAR_BUY_MIN_CONFIDENCE", "TABULAR_BLOCK_ON_SELL",
    "TABULAR_SELL_BLOCK_MIN_PROBA", "AUTO_TUNE_TABULAR_CONFIDENCE",
    "MIN_ENTRY_SCORE", "MAX_HOLD_SECONDS", "TRADE_AMOUNT", "MAX_OPEN_TRADES",
)


def read_config(path: str | None) -> dict[str, str]:
    result = {key: "not explicitly set (default/override unknown)" for key in CONFIG_KEYS}
    if path:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            if key.strip() in result:
                # Reject values that could be expansions or inline comments, rather than
                # guessing shell semantics or accidentally leaking another variable.
                value = value.strip().strip("\"'")
                if re.fullmatch(r"[a-zA-Z0-9_.,+-]+", value):
                    result[key.strip()] = value
    return result


def number(value: object) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def sum_field(rows: list[dict], field: str, available: bool) -> float | None:
    values = [number(row.get(field)) for row in rows]
    return round(sum(values), 8) if available and all(v is not None for v in values) else None


def journal_report(path: str, start: datetime, end: datetime) -> dict:
    with Path(path).open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        columns = set(reader.fieldnames or [])
        if not {"timestamp", "action"}.issubset(columns):
            raise ValueError(f"{path}: journal lacks timestamp/action columns")
        rows = list(reader)
    selected = []
    invalid_timestamps = 0
    last_row_timestamp = None
    for row in rows:
        timestamp = _parse_ts(row.get("timestamp") or "")
        if timestamp is None:
            invalid_timestamps += 1
            continue
        if last_row_timestamp is None or timestamp > last_row_timestamp:
            last_row_timestamp = timestamp
        if start <= timestamp < end:
            selected.append(row)
    buys = [row for row in selected if (row.get("action") or "").lower() == "buy"]
    sells = [row for row in selected if (row.get("action") or "").lower() == "sell"]
    net_known = {"net_pnl_base", "execution_cost_mode", "entry_cost_mode"}.issubset(columns)
    net_known = net_known and all(
        row.get("execution_cost_mode") == "net_costs"
        and row.get("entry_cost_mode") == "net_costs" for row in sells
    )
    durations = [number(row.get("hold_seconds")) for row in sells]
    valid_durations = [v for v in durations if v is not None and v >= 0]
    return {
        "buys": len(buys),
        "sell_events": len(sells),
        "net_realized_pnl_base": sum_field(sells, "net_pnl_base", net_known),
        "fees_base_all_fills": sum_field(selected, "fee_base", "fee_base" in columns),
        "slippage_base_all_fills": sum_field(selected, "slippage_base", "slippage_base" in columns),
        "average_sell_hold_seconds": round(sum(valid_durations) / len(valid_durations), 2) if valid_durations else None,
        "hold_duration_samples": len(valid_durations),
        "invalid_journal_timestamps": invalid_timestamps,
        "run_ids": sorted({row["run_id"] for row in selected if row.get("run_id")}),
        "config_hashes": sorted({row["config_hash"] for row in selected if row.get("config_hash")}),
        "rows_total": len(rows),
        "last_row_timestamp": last_row_timestamp.isoformat() if last_row_timestamp else None,
    }


LOG_TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,.]\d++)\s++-\s++\w++\s++-\s++(.*)$")
DECISION = re.compile(r"HAILO for (\S+): (KAUFEN|HALTEN|VERKAUFEN)\b")


def log_report(text: str, start: datetime, end: datetime, timezone: ZoneInfo,
               window_limited: bool = False) -> dict:
    decisions: Counter[str] = Counter()
    blocked: Counter[str] = Counter()
    hailo_events: Counter[str] = Counter()
    matched_lines = 0
    last_line_timestamp = None
    for line in text.splitlines():
        match = LOG_TIMESTAMP.match(line)
        if not match:
            continue
        try:
            stamp = datetime.strptime(match[1].replace(".", ","), "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone).astimezone(UTC)
        except ValueError:
            continue
        if last_line_timestamp is None or stamp > last_line_timestamp:
            last_line_timestamp = stamp
        if not start <= stamp < end:
            continue
        matched_lines += 1
        message = match[2]
        decision = DECISION.search(message)
        if decision:
            decisions[decision[2].lower()] += 1
        if "HAILO sell-filter active" in message:
            hailo_events["sell_filter"] += 1
        if "HAILO gated for" in message:
            hailo_events["gated"] += 1
        if "Hailo prediction for" in message and "failed; suppressing" in message:
            hailo_events["prediction_failed"] += 1
        for marker, name in (
            ("Momentum filter blocked entry", "momentum"),
            ("Re-entry cooldown active", "reentry_cooldown"),
            ("Risk guardrail blocking new positions", "risk_guardrail_iterations"),
            (" is below MIN_TRADE_AMOUNT", "below_min_trade_amount"),
        ):
            if marker in message:
                blocked[name] += 1
    return {
        "parsed_lines_in_window": matched_lines,
        "blocked_entry_log_events": dict(blocked),
        "hailo_decisions": dict(decisions),
        "hailo_other_events": dict(hailo_events),
        # A window-limited source (docker logs --since/--until) cannot show activity
        # outside the window, so staleness stays unknown instead of misleading.
        "last_line_timestamp": None if window_limited else (
            last_line_timestamp.isoformat() if last_line_timestamp else None
        ),
        "source_window_limited": window_limited,
    }


def load_log(log: str | None, container: str | None, start: datetime,
             end: datetime) -> tuple[str | None, bool]:
    if log:
        return Path(log).read_text(encoding="utf-8", errors="replace"), False
    if container:
        result = subprocess.run(
            ["docker", "logs", "--since", start.isoformat(), "--until", end.isoformat(), container],
            capture_output=True, text=True, check=True,
        )
        return result.stdout + "\n" + result.stderr, True
    return None, False


def observation(journal: str, log: str | None, container: str | None, config: str | None,
                feed: str, start: datetime, end: datetime, timezone: ZoneInfo) -> dict:
    text, window_limited = load_log(log, container, start, end)
    return {
        "feed_description_operator_supplied": feed,
        "declared_environment_allowlist": read_config(config),
        "journal": journal_report(journal, start, end),
        "log": log_report(text, start, end, timezone, window_limited) if text is not None else None,
    }


def staleness(journal: dict, log: dict | None, now: datetime, max_age_hours: float | None) -> dict:
    """Newest observed activity, independent of the report window.

    Journal rows only appear on fills, so the bot log is the primary activity
    signal; the journal is a fallback for log-less or rotated-log setups.
    """
    candidates = [value for value in (_parse_ts(journal.get("last_row_timestamp") or ""),
                                      _parse_ts((log or {}).get("last_line_timestamp") or ""))
                  if value is not None]
    latest = max(candidates, default=None)
    age_hours = round((now - latest).total_seconds() / 3600.0, 3) if latest else None
    if latest is None:
        status = "unknown"
    elif max_age_hours is None:
        status = "unchecked"
    elif age_hours > max_age_hours:
        status = "stale"
    else:
        status = "fresh"
    return {
        "checked_at_utc": now.isoformat(),
        "max_data_age_hours": max_age_hours,
        "last_activity_timestamp": latest.isoformat() if latest else None,
        "age_hours": age_hours,
        "status": status,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True)
    logs = parser.add_mutually_exclusive_group()
    logs.add_argument("--log")
    logs.add_argument("--container", help="Read docker logs (requires local Docker access)")
    parser.add_argument("--config", help="Non-secret values are read from this env file, not the live process")
    parser.add_argument("--feed", default="public Kraken; separate real-time polling")
    parser.add_argument("--compare-journal")
    compare_logs = parser.add_mutually_exclusive_group()
    compare_logs.add_argument("--compare-log")
    compare_logs.add_argument("--compare-container")
    parser.add_argument("--compare-config")
    parser.add_argument("--compare-feed", default="unknown")
    parser.add_argument("--date", type=date.fromisoformat, help="UTC calendar date, default yesterday")
    parser.add_argument("--log-timezone", default="UTC", help="IANA timezone for naive bot log timestamps")
    parser.add_argument("--max-data-age-hours", type=float, default=None,
                        help="Fail (exit 1) if journal/log show no activity for this many hours; "
                             "0 or omitted disables the gate so backfills never false-alarm")
    parser.add_argument("--output", help="Write JSON to this file (otherwise stdout)")
    args = parser.parse_args()
    if not args.compare_journal and any((args.compare_log, args.compare_container, args.compare_config)):
        parser.error("comparison inputs require --compare-journal")
    now = datetime.now(UTC)
    day = args.date or (now.date() - timedelta(days=1))
    start = datetime.combine(day, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    max_age_hours = args.max_data_age_hours if args.max_data_age_hours and args.max_data_age_hours > 0 else None
    try:
        timezone = ZoneInfo(args.log_timezone)
        primary = observation(args.journal, args.log, args.container, args.config, args.feed, start, end, timezone)
        primary_staleness = staleness(primary["journal"], primary["log"], now, max_age_hours)
        report = {
            "window_utc": {"start_inclusive": start.isoformat(), "end_exclusive": end.isoformat()},
            "log_timezone": args.log_timezone,
            "staleness": primary_staleness,
            "primary": primary,
            "limitations": [
                "Costs are per fill in the window, not the cost of the closed trades; net PnL already includes costs.",
                "Sell events may be partial exits. Net PnL excludes unrealized PnL and is unavailable for legacy/mixed cost basis.",
                "Log counts are observed events, not all blocked entries; missing or rotated logs cannot establish zero activity.",
                "Configuration is an operator-supplied snapshot; defaults, runtime overrides and changes are unknown.",
                ("Zero activity in the window is not evidence of a healthy idle bot; see 'staleness' for the "
                 "newest journal/log activity outside the window."),
            ],
        }
        if args.date is None:
            current_start = datetime.combine(now.date(), time.min, tzinfo=UTC)
            report["current_day_to_now"] = {
                "window_utc": {
                    "start_inclusive": current_start.isoformat(),
                    "end_exclusive": now.isoformat(),
                },
                "observation": observation(
                    args.journal, args.log, args.container, args.config, args.feed,
                    current_start, now, timezone,
                ),
            }
        if args.compare_journal:
            comparison = observation(args.compare_journal, args.compare_log, args.compare_container,
                                     args.compare_config, args.compare_feed, start, end, timezone)
            differences = {
                key: {"primary": primary["declared_environment_allowlist"][key],
                      "comparison": comparison["declared_environment_allowlist"][key]}
                for key in CONFIG_KEYS
                if primary["declared_environment_allowlist"][key] != comparison["declared_environment_allowlist"][key]
            }
            report["comparison"] = {
                "status": "non-comparable",
                "reason": "Independent polling or unknown feeds are not identical; this is descriptive, not a controlled experiment.",
                "config_differences": differences,
                "staleness": staleness(comparison["journal"], comparison["log"], now, max_age_hours),
                "observation": comparison,
            }
        output = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if args.output:
            Path(args.output).write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        if primary_staleness["status"] == "stale":
            # The report is still written for analysis, but the non-zero exit keeps
            # 'latest.json' at the last healthy state and makes the timer/alert fire.
            print(
                f"report failed: no journal or bot log activity since "
                f"{primary_staleness['last_activity_timestamp']} "
                f"({primary_staleness['age_hours']}h > {max_age_hours}h): the collector is not running",
                file=sys.stderr,
            )
            return 1
    except (OSError, ValueError, ZoneInfoNotFoundError, subprocess.SubprocessError) as exc:
        print(f"report failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
