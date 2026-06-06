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
import html
import json
import os
import re
import urllib.request
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
            ts = datetime.strptime(
                ts_text, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
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
    empty = {
        "available": False,
        "model": "",
        "monthly_calls": 0,
        "monthly_spend_usd": 0.0,
        "daily_calls": 0,
        "consecutive_errors": 0,
        "last_run_at": "",
        "last_applied_at": "",
        "last_error": "",
        "last_error_at": "",
        "last_estimated_cost_usd": 0.0,
        "last_prompt_tokens": 0,
        "last_completion_tokens": 0,
        "last_suggestion": {},
        "last_applied_changes": {},
    }
    candidates = [path, f"{path}.bak"]
    state = None
    for candidate in candidates:
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                state = json.load(f)
            break
        except (OSError, json.JSONDecodeError):
            continue
    if state is None:
        return empty

    return {
        "available": True,
        "model": str(state.get("model", "") or ""),
        "monthly_calls": int(state.get("monthly_calls", 0) or 0),
        "monthly_spend_usd": float(state.get("monthly_spend_usd", 0.0) or 0.0),
        "daily_calls": int(state.get("daily_calls", 0) or 0),
        "consecutive_errors": int(state.get("consecutive_errors", 0) or 0),
        "last_run_at": str(state.get("last_run_at", "") or ""),
        "last_applied_at": str(state.get("last_applied_at", "") or ""),
        "last_error": str(state.get("last_error", "") or ""),
        "last_error_at": str(state.get("last_error_at", "") or ""),
        "last_estimated_cost_usd": float(state.get("last_estimated_cost_usd", 0.0) or 0.0),
        "last_prompt_tokens": int(state.get("last_prompt_tokens", 0) or 0),
        "last_completion_tokens": int(state.get("last_completion_tokens", 0) or 0),
        "last_suggestion": state.get("last_suggestion", {}) if isinstance(state.get("last_suggestion", {}), dict) else {},
        "last_applied_changes": state.get("last_applied_changes", {}) if isinstance(state.get("last_applied_changes", {}), dict) else {},
    }


def _read_env_value(path: str, env_key: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() != env_key:
                    continue
                return value.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def _read_ai_spend_from_api(env_path: str) -> Dict[str, Any]:
    api_key = _read_env_value(env_path, "MAMMOUTH_API_KEY")
    api_url = _read_env_value(
        env_path, "AI_COPILOT_API_URL") or "https://api.mammouth.ai/v1/chat/completions"
    if not api_key:
        return {}

    api_root = re.sub(r"/v1/chat/completions$", "", api_url).rstrip("/")
    req = urllib.request.Request(
        f"{api_root}/key/info",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "trading-bot-daily-review/1.0",
        },
        method="GET",
    )

    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))

    info = payload.get("info", {}) if isinstance(payload, dict) else {}
    result = {}
    if info.get("spend") is not None:
        result["monthly_spend_usd"] = float(info.get("spend"))
    if info.get("max_budget") is not None:
        result["budget_cap_usd"] = float(info.get("max_budget"))
    if info.get("budget_reset_at") is not None:
        result["budget_reset_at"] = str(info.get("budget_reset_at") or "")
    return result


def _build_text(report: Dict[str, Any]) -> str:
    t = report["trades"]
    log_activity = report["log_activity"]
    a = report["ai_copilot"]
    b = report["ai_benchmark"]

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
        f"model: {a['model']}",
        f"available: {a['available']}",
        f"daily_calls: {a['daily_calls']}",
        f"monthly_calls: {a['monthly_calls']}",
        f"monthly_spend_usd: {a['monthly_spend_usd']}",
        f"consecutive_errors: {a['consecutive_errors']}",
        f"last_run_at: {a['last_run_at']}",
        f"last_applied_at: {a['last_applied_at']}",
        f"last_estimated_cost_usd: {a['last_estimated_cost_usd']}",
        f"last_error: {a['last_error']}",
        "",
        "[AI Benchmark]",
        f"model: {b['model']}",
        f"available: {b['available']}",
        f"daily_calls: {b['daily_calls']}",
        f"monthly_calls: {b['monthly_calls']}",
        f"monthly_spend_usd: {b['monthly_spend_usd']}",
        f"consecutive_errors: {b['consecutive_errors']}",
        f"last_run_at: {b['last_run_at']}",
        f"last_applied_at: {b['last_applied_at']}",
        f"last_estimated_cost_usd: {b['last_estimated_cost_usd']}",
        f"last_error: {b['last_error']}",
    ]
    return "\n".join(lines) + "\n"


def _fmt_number(value: Any, digits: int = 4) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{number:.{digits}f}"


def _fmt_timestamp(value: str) -> str:
    if not value:
        return "-"
    return html.escape(str(value))


def _fmt_change_map(changes: Dict[str, Any]) -> str:
    if not changes:
        return "No changes recorded"
    parts = []
    for key, value in changes.items():
        if isinstance(value, dict) and "old" in value and "new" in value:
            parts.append(f"{key}: {value['old']} -> {value['new']}")
        else:
            parts.append(f"{key}: {value}")
    return ", ".join(parts)


def _fmt_suggestion(suggestion: Dict[str, Any]) -> str:
    if not suggestion:
        return "No suggestion recorded"
    changes = suggestion.get("changes", {})
    reason = str(suggestion.get("reason", "") or "")
    confidence = suggestion.get("confidence")
    mode = str(suggestion.get("mode", "") or "unknown")
    risk_level = str(suggestion.get("risk_level", "") or "unknown")
    parts = [
        f"mode={mode}",
        f"risk={risk_level}",
        f"confidence={confidence if confidence is not None else '-'}",
        f"changes={_fmt_change_map(changes)}",
    ]
    if reason:
        parts.append(f"reason={reason}")
    return " | ".join(parts)


def _build_ai_summary_rows(label: str, state: Dict[str, Any]) -> List[str]:
    return [
        f"<tr><th>{html.escape(label)} model</th><td>{html.escape(state.get('model', '') or '-')}</td></tr>",
        f"<tr><th>{html.escape(label)} available</th><td>{html.escape(str(bool(state.get('available', False))))}</td></tr>",
        f"<tr><th>{html.escape(label)} daily calls</th><td>{int(state.get('daily_calls', 0) or 0)}</td></tr>",
        f"<tr><th>{html.escape(label)} monthly calls</th><td>{int(state.get('monthly_calls', 0) or 0)}</td></tr>",
        f"<tr><th>{html.escape(label)} monthly spend (USD)</th><td>{_fmt_number(state.get('monthly_spend_usd', 0.0), 4)}</td></tr>",
        f"<tr><th>{html.escape(label)} last estimated cost (USD)</th><td>{_fmt_number(state.get('last_estimated_cost_usd', 0.0), 6)}</td></tr>",
        f"<tr><th>{html.escape(label)} consecutive errors</th><td>{int(state.get('consecutive_errors', 0) or 0)}</td></tr>",
        f"<tr><th>{html.escape(label)} last run</th><td>{_fmt_timestamp(str(state.get('last_run_at', '') or ''))}</td></tr>",
        f"<tr><th>{html.escape(label)} last applied</th><td>{_fmt_timestamp(str(state.get('last_applied_at', '') or ''))}</td></tr>",
        f"<tr><th>{html.escape(label)} last suggestion</th><td>{html.escape(_fmt_suggestion(state.get('last_suggestion', {})))}</td></tr>",
        f"<tr><th>{html.escape(label)} last applied changes</th><td>{html.escape(_fmt_change_map(state.get('last_applied_changes', {})))}</td></tr>",
        f"<tr><th>{html.escape(label)} last error</th><td>{html.escape(str(state.get('last_error', '') or '-'))}</td></tr>",
    ]


def _build_html(report: Dict[str, Any]) -> str:
    trades = report["trades"]
    log_activity = report["log_activity"]
    ai_copilot = report["ai_copilot"]
    ai_benchmark = report["ai_benchmark"]
    summary_cards = [
        ("Closed Trades", str(trades["closed_trades"])),
        ("Win Rate", f"{_fmt_number(trades['win_rate_pct'], 2)}%"),
        ("Realized PnL", _fmt_number(trades["realized_pnl"], 6)),
        ("Buy Attempts", str(log_activity["buy_attempts"])),
        ("Reentry Blocks", str(log_activity["reentry_blocks"])),
        ("AI Monthly Spend",
         f"${_fmt_number(ai_copilot['monthly_spend_usd'], 4)}"),
    ]

    ai_compare_rows = "\n".join(_build_ai_summary_rows(
        "Primary", ai_copilot) + _build_ai_summary_rows("Benchmark", ai_benchmark))

    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Daily Dry-Run Review</title>
    <style>
        :root {{
            color-scheme: light;
            --bg: #f5f1e8;
            --panel: rgba(255, 252, 246, 0.88);
            --panel-strong: #fffaf0;
            --text: #1f2933;
            --muted: #52606d;
            --line: rgba(31, 41, 51, 0.12);
            --accent: #166534;
            --accent-soft: rgba(22, 101, 52, 0.10);
            --warn: #b45309;
            --shadow: 0 20px 40px rgba(78, 59, 28, 0.10);
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            font-family: Georgia, \"Times New Roman\", serif;
            color: var(--text);
            background:
                radial-gradient(circle at top left, rgba(255,255,255,0.75), transparent 35%),
                linear-gradient(135deg, #ede2cc 0%, #f9f6ef 45%, #e8efe5 100%);
            min-height: 100vh;
        }}
        .wrap {{ max-width: 1200px; margin: 0 auto; padding: 32px 20px 48px; }}
        .hero {{
            background: linear-gradient(135deg, rgba(22,101,52,0.95), rgba(28,25,23,0.92));
            color: #fdfbf6;
            border-radius: 24px;
            padding: 28px;
            box-shadow: var(--shadow);
        }}
        .hero h1 {{ margin: 0 0 8px; font-size: clamp(2rem, 4vw, 3.5rem); }}
        .hero p {{ margin: 6px 0; color: rgba(253, 251, 246, 0.82); }}
        .cards {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 14px;
            margin: 22px 0 28px;
        }}
        .card, .panel {{
            background: var(--panel);
            backdrop-filter: blur(12px);
            border: 1px solid var(--line);
            border-radius: 20px;
            box-shadow: var(--shadow);
        }}
        .card {{ padding: 18px; }}
        .card .label {{ font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }}
        .card .value {{ margin-top: 8px; font-size: 1.8rem; font-weight: 700; }}
        .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
        .panel {{ padding: 22px; }}
        h2 {{ margin: 0 0 14px; font-size: 1.2rem; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ text-align: left; padding: 10px 0; vertical-align: top; border-bottom: 1px solid var(--line); }}
        th {{ width: 40%; color: var(--muted); font-weight: 600; padding-right: 14px; }}
        .note {{ margin-top: 14px; padding: 14px 16px; background: var(--accent-soft); border-radius: 14px; }}
        .warn {{ color: var(--warn); }}
        @media (max-width: 860px) {{
            .grid {{ grid-template-columns: 1fr; }}
            th {{ width: 46%; }}
        }}
    </style>
</head>
<body>
    <div class=\"wrap\">
        <section class=\"hero\">
            <h1>Daily Dry-Run Review</h1>
            <p>Generated: {html.escape(report['generated_at_utc'])}</p>
            <p>Lookback window: {int(report['lookback_hours'])} hours</p>
        </section>

        <section class=\"cards\">
            {''.join(f'<article class="card"><div class="label">{html.escape(label)}</div><div class="value">{html.escape(value)}</div></article>' for label, value in summary_cards)}
        </section>

        <section class=\"grid\">
            <article class=\"panel\">
                <h2>Trading Window</h2>
                <table>
                    <tr><th>Buys</th><td>{trades['buys']}</td></tr>
                    <tr><th>Closed trades</th><td>{trades['closed_trades']}</td></tr>
                    <tr><th>Wins</th><td>{trades['wins']}</td></tr>
                    <tr><th>Losses</th><td>{trades['losses']}</td></tr>
                    <tr><th>Win rate</th><td>{_fmt_number(trades['win_rate_pct'], 2)}%</td></tr>
                    <tr><th>Realized PnL</th><td>{_fmt_number(trades['realized_pnl'], 8)}</td></tr>
                    <tr><th>Avg. PnL per trade</th><td>{_fmt_number(trades['avg_pnl_per_trade'], 8)}</td></tr>
                </table>
            </article>

            <article class=\"panel\">
                <h2>Execution Activity</h2>
                <table>
                    <tr><th>Buy attempts</th><td>{log_activity['buy_attempts']}</td></tr>
                    <tr><th>Reentry blocks</th><td>{log_activity['reentry_blocks']}</td></tr>
                    <tr><th>Momentum blocks</th><td>{log_activity['momentum_blocks']}</td></tr>
                    <tr><th>Reentry block ratio</th><td>{_fmt_number(log_activity['reentry_block_ratio'], 4)}</td></tr>
                    <tr><th>Error lines</th><td class=\"{'warn' if int(log_activity['error_lines']) > 0 else ''}\">{log_activity['error_lines']}</td></tr>
                </table>
                <div class=\"note\">This report stays file-based and lightweight so it can run on the Pi without a browser runtime or external template engine.</div>
            </article>
        </section>

        <section class=\"panel\" style=\"margin-top: 18px;\">
            <h2>AI Copilot Comparison</h2>
            <table>
                {ai_compare_rows}
            </table>
        </section>
    </div>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate daily dry-run review report")
    parser.add_argument("--journal", default="trade_journal.csv")
    parser.add_argument("--bot-log", default="logs/bot.log")
    parser.add_argument("--ai-state", default="ai_copilot_state.json")
    parser.add_argument(
        "--ai-benchmark-state", default="ai_copilot_benchmark_state.json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument(
        "--output-json", default="results/daily_review/latest_review.json")
    parser.add_argument(
        "--output-txt", default="results/daily_review/latest_review.txt")
    parser.add_argument(
        "--output-html", default="results/daily_review/latest_review.html")
    args = parser.parse_args()

    now_utc = datetime.now(timezone.utc)
    cutoff_utc = now_utc - timedelta(hours=max(1, args.lookback_hours))

    rows = _read_trade_rows(args.journal)
    trades = _review_trades(rows, cutoff_utc)
    log_activity = _scan_bot_log(args.bot_log, cutoff_utc)
    ai_copilot = _read_ai_state(args.ai_state)
    ai_benchmark = _read_ai_state(args.ai_benchmark_state)
    try:
        api_usage = _read_ai_spend_from_api(args.env_file)
        if api_usage:
            ai_copilot.update(api_usage)
            ai_copilot["available"] = True
    except Exception:
        pass

    ai_copilot["model"] = ai_copilot.get("model") or _read_env_value(
        args.env_file, "AI_COPILOT_MODEL")
    ai_benchmark["model"] = ai_benchmark.get("model") or _read_env_value(
        args.env_file, "AI_COPILOT_BENCHMARK_MODEL")

    report = {
        "generated_at_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "lookback_hours": int(max(1, args.lookback_hours)),
        "trades": trades,
        "log_activity": log_activity,
        "ai_copilot": ai_copilot,
        "ai_benchmark": ai_benchmark,
    }

    os.makedirs(os.path.dirname(args.output_json), exist_ok=True)
    os.makedirs(os.path.dirname(args.output_txt), exist_ok=True)
    os.makedirs(os.path.dirname(args.output_html), exist_ok=True)

    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=True, indent=2)
        f.write("\n")

    with open(args.output_txt, "w", encoding="utf-8") as f:
        f.write(_build_text(report))

    with open(args.output_html, "w", encoding="utf-8") as f:
        f.write(_build_html(report))

    print(f"Daily dry-run review written: {args.output_json}")
    print(f"Daily dry-run review written: {args.output_txt}")
    print(f"Daily dry-run review written: {args.output_html}")


if __name__ == "__main__":
    main()
