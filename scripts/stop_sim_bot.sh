#!/usr/bin/env bash
set -euo pipefail

# Error handling
trap 'echo "ERROR: Script failed at line $LINENO with exit code $?" >&2; exit 1' ERR

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
PID_FILE="${PID_FILE:-$LOG_DIR/sim_bot.pid}"

# Try to stop via PID file
if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    echo "INFO: Stopping simulation bot (PID $PID)..."
    kill "$PID" 2>/dev/null || true
    sleep 1
    if kill -0 "$PID" 2>/dev/null; then
      echo "WARNING: Process $PID still running, forcing kill..."
      kill -9 "$PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "INFO: Simulation bot stopped successfully."
    exit 0
  fi
fi

# Fallback if no PID file exists or is stale
if pgrep -f "python.*trading_bot.py" >/dev/null 2>&1; then
  echo "INFO: Found trading bot process via process match, stopping..."
  pkill -f "python.*trading_bot.py" || true
  rm -f "$PID_FILE"
  sleep 1
  echo "INFO: Simulation bot stopped via process match."
  exit 0
fi

echo "INFO: Simulation bot is not running."
exit 0
