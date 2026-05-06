#!/usr/bin/env python3
"""Create a compact daily dry-run review report.

The report is intentionally lightweight and robust for low-power systems.
It aggregates:
- trade_journal KPIs over a lookback window
- bot log activity counters (buy attempts, reentry cooldown blocks)
- AI copilot state snapshot
"""

from __future__ import annotations

import argparse
import json
import os
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


def _parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        # trade_journal timestamps are usually ISO; ignore malformed rows.
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _read_trade_rows(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []

    import csv

    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _review_trades(rows: List[Dict[str, Any]], cutoff_utc: datetime) -> Dict[str, Any]:
    sells = []
    buys = 0
    for row in rows:
        action = (row.get("action") or "").strip().lower()
        ts = _parse_ts(row.get("timestamp", ""))
        if ts is None or ts < cutoff_utc:
            continue
        if action == "buy":
            buys += 1
        elif action == "sell":
            sells.append(row)

    closed = len(sells)
    realized = 0.0
    wins = 0
    losses = 0
    pnl_values: List[float] = []
    for row in sells:
        pnl = _to_float(row.get("pnl_base"), 0.0)
        pnl_values.append(pnl)
        realized += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

    win_rate = (wins / closed * 100.0) if closed > 0 else 0.0
    avg_pnl = (realized / closed) if closed > 0 else 0.0

    return {
        "buys": buys,
        "closed_trades": closed,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 4),
        "realized_pnl": round(realized, 8),
        "avg_pnl_per_trade": round(avg_pnl, 8),
    }


def _scan_bot_log(path: str, cutoff_utc: datetime) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {
            "buy_attempts": 0,
            "reentry_blocks": 0,
            "momentum_blocks": 0,
            "reentry_block_ratio": 0.0,
            "error_lines": 0,
        }

    # Read only tail to stay light on Pi.
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        tail = list(deque(f, maxlen=5000))

    attempts = 0
    reentry_blocks = 0
    momentum_blocks = 0
    error_lines = 0

    for line in tail:
        # Lines start with: YYYY-mm-dd HH:MM:SS,mmm - LEVEL - ...
        if len(line) < 23:
            continue
        ts_text = line[:23]
        try:
            ts = datetime.strptime(ts_text, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts < cutoff_utc:
            continue

        msg_l = line.lower()
        if "attempting to buy" in msg_l:
            attempts += 1
        if "re-entry cooldown active" in msg_l:
            reentry_blocks += 1
        if "momentum filter blocked entry" in msg_l:
            momentum_blocks += 1
        if " - error - " in msg_l or "traceback" in msg_l:
            error_lines += 1

    block_ratio = (reentry_blocks / attempts) if attempts > 0 else 0.0

    return {
        "buy_attempts": attempts,
        "reentry_blocks": reentry_blocks,
        "momentum_blocks": momentum_blocks,
        "reentry_block_ratio": round(block_ratio, 4),
        "error_lines": error_lines,
    }


def _read_ai_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {
            "available": False,
            "monthly_calls": 0,
            "daily_calls": 0,
            "consecutive_errors": 0,
            "last_run_at": "",
            "last_applied_at": "",
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {
            "available": False,
            "monthly_calls": 0,
            "daily_calls": 0,
            "consecutive_errors": 0,
            "last_run_at": "",
            "last_applied_at": "",
        }

    return {
        "available": True,
        "monthly_calls": int(state.get("monthly_calls", 0) or 0),
        "daily_calls": int(state.get("daily_calls", 0) or 0),
        "consecutive_errors": int(state.get("consecutive_errors", 0) or 0),
        "last_run_at": str(state.get("last_run_at", "") or ""),
        "last_applied_at": str(state.get("last_applied_at", "") or ""),
    }


def _build_text(report: Dict[str, Any]) -> str:
    t = report["trades"]
    log_activity = report["log_activity"]
    a = report["ai_copilot"]

    lines = [
        "=== Daily Dry-Run Review ===",
        f"generated_at_utc: {report['generated_at_utc']}",
        f"lookback_hours: {report['lookback_hours']}",
        "",
        "[Trades]",
        f"buys: {t['buys']}",
        f"closed_trades: {t['closed_trades']}",
        f"win_rate_pct: {t['win_rate_pct']}",
        f"realized_pnl: {t['realized_pnl']}",
        f"avg_pnl_per_trade: {t['avg_pnl_per_trade']}",
        "",
        "[Execution Activity]",
        f"buy_attempts: {log_activity['buy_attempts']}",
        f"reentry_blocks: {log_activity['reentry_blocks']}",
        f"momentum_blocks: {log_activity['momentum_blocks']}",
        f"reentry_block_ratio: {log_activity['reentry_block_ratio']}",
        f"error_lines: {log_activity['error_lines']}",
        "",
        "[AI Copilot]",
        f"available: {a['available']}",
        f"daily_calls: {a['daily_calls']}",
        f"monthly_calls: {a['monthly_calls']}",
        f"consecutive_errors: {a['consecutive_errors']}",
        f"last_run_at: {a['last_run_at']}",
        f"last_applied_at: {a['last_applied_at']}",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate daily dry-run review report")
    parser.add_argument("--journal", default="trade_journal.csv")
    parser.add_argument("--bot-log", default="logs/bot.log")
    parser.add_argument("--ai-state", default="ai_copilot_state.json")
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--output-json", default="results/daily_review/latest_review.json")
    parser.add_argument("--output-txt", default="results/daily_review/latest_review.txt")
    args = parser.parse_args()

    now_utc = datetime.now(timezone.utc)
    cutoff_utc = now_utc - timedelta(hours=max(1, args.lookback_hours))

    rows = _read_trade_rows(args.journal)
    trades = _review_trades(rows, cutoff_utc)
    log_activity = _scan_bot_log(args.bot_log, cutoff_utc)
    ai_copilot = _read_ai_state(args.ai_state)

    report = {
        "generated_at_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "lookback_hours": int(max(1, args.lookback_hours)),
        "trades": trades,
        "log_activity": log_activity,
        "ai_copilot": ai_copilot,
    }

    os.makedirs(os.path.dirname(args.output_json), exist_ok=True)
    os.makedirs(os.path.dirname(args.output_txt), exist_ok=True)

    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=True, indent=2)
        f.write("\n")

    with open(args.output_txt, "w", encoding="utf-8") as f:
        f.write(_build_text(report))

    print(f"Daily dry-run review written: {args.output_json}")
    print(f"Daily dry-run review written: {args.output_txt}")


if __name__ == "__main__":
    main()
