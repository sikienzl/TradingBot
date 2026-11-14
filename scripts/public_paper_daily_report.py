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
    for row in rows:
        timestamp = _parse_ts(row.get("timestamp") or "")
        if timestamp is None:
            invalid_timestamps += 1
        elif start <= timestamp < end:
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
    }


LOG_TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[,.]\d+)\s+-\s+\w+\s+-\s+(.*)$")
DECISION = re.compile(r"HAILO for (\S+): (KAUFEN|HALTEN|VERKAUFEN)\b")


def log_report(text: str, start: datetime, end: datetime, timezone: ZoneInfo) -> dict:
    decisions: Counter[str] = Counter()
    blocked: Counter[str] = Counter()
    hailo_events: Counter[str] = Counter()
    matched_lines = 0
    for line in text.splitlines():
        match = LOG_TIMESTAMP.match(line)
        if not match:
            continue
        try:
            stamp = datetime.strptime(match[1].replace(".", ","), "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone).astimezone(UTC)
        except ValueError:
            continue
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
    }


def load_log(log: str | None, container: str | None, start: datetime, end: datetime) -> str | None:
    if log:
        return Path(log).read_text(encoding="utf-8", errors="replace")
    if container:
        result = subprocess.run(
            ["docker", "logs", "--since", start.isoformat(), "--until", end.isoformat(), container],
            capture_output=True, text=True, check=True,
        )
        return result.stdout + "\n" + result.stderr
    return None


def observation(journal: str, log: str | None, container: str | None, config: str | None,
                feed: str, start: datetime, end: datetime, timezone: ZoneInfo) -> dict:
    text = load_log(log, container, start, end)
    return {
        "feed_description_operator_supplied": feed,
        "declared_environment_allowlist": read_config(config),
        "journal": journal_report(journal, start, end),
        "log": log_report(text, start, end, timezone) if text is not None else None,
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
    parser.add_argument("--output", help="Write JSON to this file (otherwise stdout)")
    args = parser.parse_args()
    if not args.compare_journal and any((args.compare_log, args.compare_container, args.compare_config)):
        parser.error("comparison inputs require --compare-journal")
    day = args.date or (datetime.now(UTC).date() - timedelta(days=1))
    start = datetime.combine(day, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    try:
        timezone = ZoneInfo(args.log_timezone)
        primary = observation(args.journal, args.log, args.container, args.config, args.feed, start, end, timezone)
        report = {
            "window_utc": {"start_inclusive": start.isoformat(), "end_exclusive": end.isoformat()},
            "log_timezone": args.log_timezone,
            "primary": primary,
            "limitations": [
                "Costs are per fill in the window, not the cost of the closed trades; net PnL already includes costs.",
                "Sell events may be partial exits. Net PnL excludes unrealized PnL and is unavailable for legacy/mixed cost basis.",
                "Log counts are observed events, not all blocked entries; missing or rotated logs cannot establish zero activity.",
                "Configuration is an operator-supplied snapshot; defaults, runtime overrides and changes are unknown.",
            ],
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
                "observation": comparison,
            }
        output = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if args.output:
            Path(args.output).write_text(output, encoding="utf-8")
        else:
            print(output, end="")
    except (OSError, ValueError, ZoneInfoNotFoundError, subprocess.SubprocessError) as exc:
        print(f"report failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
