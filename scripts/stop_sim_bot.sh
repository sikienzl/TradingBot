#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
PID_FILE="${PID_FILE:-$LOG_DIR/sim_bot.pid}"

if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    sleep 1
    if kill -0 "$PID" 2>/dev/null; then
      kill -9 "$PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "Stopped simulation bot (PID $PID)."
    exit 0
  fi
fi

# Fallback if no pid file exists
if pgrep -f "python.*trading_bot.py" >/dev/null 2>&1; then
  pkill -f "python.*trading_bot.py" || true
  rm -f "$PID_FILE"
  echo "Stopped simulation bot via process match."
  exit 0
fi

echo "Simulation bot is not running."
