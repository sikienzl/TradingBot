#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-siegfried@192.168.62.87}"
COIN="${2:-${SIGNAL_COIN:-VVV}}"
LOG_PATH="${SIGNAL_LOG_PATH:-/opt/trading_2/logs/bot.log}"

WATCH_RSI_MAX="${SIGNAL_WATCH_RSI_MAX:-70}"
WATCH_SELL_MAX="${SIGNAL_WATCH_SELL_MAX:-0.36}"
WATCH_BUY_MIN="${SIGNAL_WATCH_BUY_MIN:-0.44}"
WATCH_EDGE_MIN="${SIGNAL_WATCH_EDGE_MIN:-0.05}"
HARD_RSI_MAX="${SIGNAL_HARD_RSI_MAX:-72}"

ssh "$HOST" \
  COIN="$COIN" \
  LOG_PATH="$LOG_PATH" \
  WATCH_RSI_MAX="$WATCH_RSI_MAX" \
  WATCH_SELL_MAX="$WATCH_SELL_MAX" \
  WATCH_BUY_MIN="$WATCH_BUY_MIN" \
  WATCH_EDGE_MIN="$WATCH_EDGE_MIN" \
  HARD_RSI_MAX="$HARD_RSI_MAX" \
  'python3 - <<"PY"
import os
import re
from pathlib import Path

coin = os.environ["COIN"]
log_path = Path(os.environ["LOG_PATH"])
watch_rsi_max = float(os.environ["WATCH_RSI_MAX"])
watch_sell_max = float(os.environ["WATCH_SELL_MAX"])
watch_buy_min = float(os.environ["WATCH_BUY_MIN"])
watch_edge_min = float(os.environ["WATCH_EDGE_MIN"])
hard_rsi_max = float(os.environ["HARD_RSI_MAX"])

if not log_path.exists():
    raise SystemExit(f"Log not found: {log_path}")

tail_lines = log_path.read_text(errors="ignore").splitlines()[-1200:]
reason_line = None
catboost_line = None
rec_line = None

for line in reversed(tail_lines):
    if reason_line is None and f"- {coin}: reason=" in line:
        reason_line = line
    if catboost_line is None and f"CatBoost for {coin}:" in line:
        catboost_line = line
    if rec_line is None and f"- {coin}: rec=" in line:
        rec_line = line
    if reason_line and catboost_line and rec_line:
        break

print(f"coin={coin}")
print(f"host={os.environ.get('SSH_CONNECTION', '').split()[0] or 'remote'}")

print(f"latest_catboost={catboost_line}" if catboost_line else "latest_catboost=<missing>")
print(f"latest_reason={reason_line}" if reason_line else "latest_reason=<missing>")
print(f"latest_rec={rec_line}" if rec_line else "latest_rec=<missing>")

if not reason_line:
    print("status=UNKNOWN")
    print("summary=No recent rejection line found")
    raise SystemExit(0)

def extract(pattern: str, text: str):
    match = re.search(pattern, text)
    return match.group(1) if match else None

reason = extract(r"reason=([^,]+)", reason_line) or "unknown"
rsi_raw = extract(r"RSI=([-0-9.]+)", reason_line)
buy_raw = extract(r"buy_proba=([-0-9.]+)", reason_line)
sell_raw = extract(r"sell_proba=([-0-9.]+)", reason_line)
edge_raw = extract(r"edge=([-0-9.]+)", reason_line)

rsi = float(rsi_raw) if rsi_raw is not None else None
buy = float(buy_raw) if buy_raw is not None else None
sell = float(sell_raw) if sell_raw is not None else None
edge = float(edge_raw) if edge_raw is not None else None

print(f"reason_code={reason}")
print(f"rsi={rsi if rsi is not None else 'n/a'}")
print(f"buy_proba={buy if buy is not None else 'n/a'}")
print(f"sell_proba={sell if sell is not None else 'n/a'}")
print(f"edge={edge if edge is not None else 'n/a'}")

is_recheck = (
    rsi is not None and rsi <= watch_rsi_max
    and buy is not None and buy >= watch_buy_min
    and sell is not None and sell <= watch_sell_max
    and edge is not None and edge >= watch_edge_min
)

if is_recheck:
    print("status=RECHECK")
    print(
        "summary="
        f"{coin} is close enough to the watch zone "
        f"(rsi<={watch_rsi_max}, sell<={watch_sell_max}, buy>={watch_buy_min}, edge>={watch_edge_min})"
    )
elif sell is not None and sell >= 0.40:
    print("status=IGNORE")
    print(f"summary={coin} still has clearly elevated sell_proba")
elif edge is not None and edge <= 0.0:
    print("status=IGNORE")
    print(f"summary={coin} has non-positive edge")
elif rsi is not None and rsi > hard_rsi_max:
    print("status=IGNORE")
    print(f"summary={coin} is still over the hard RSI gate")
else:
    print("status=WATCH")
    print(f"summary={coin} improved somewhat but is not yet close enough")
PY'