#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-.env.simulation.example}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/sim_bot.log}"
PID_FILE="${PID_FILE:-$LOG_DIR/sim_bot.pid}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: Env file not found: $ENV_FILE" >&2
  exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "ERROR: Python binary not executable: $PYTHON_BIN" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Simulation bot already running (PID $OLD_PID)."
    echo "Log: $LOG_FILE"
    exit 0
  fi
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

nohup "$PYTHON_BIN" "$ROOT_DIR/trading_bot.py" > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

echo "Simulation bot started."
echo "PID: $NEW_PID"
echo "Log: $LOG_FILE"
