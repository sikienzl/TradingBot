#!/usr/bin/env bash
set -euo pipefail

# Error handling with context-aware messages
trap 'echo "ERROR: Script failed at line $LINENO with exit code $?" >&2; exit 1' ERR

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Configuration with safe defaults
ENV_FILE="${ENV_FILE:-.env.simulation.example}"
PYTHON_BIN="${PYTHON_BIN:-}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/sim_bot.log}"
PID_FILE="${PID_FILE:-$LOG_DIR/sim_bot.pid}"

# Validate environment file
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: Environment file not found: $ENV_FILE" >&2
  exit 1
fi

# Resolve Python binary with fallbacks
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    echo "ERROR: No Python found. Install Python 3 or set PYTHON_BIN." >&2
    exit 1
  fi
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "ERROR: Python binary not executable: $PYTHON_BIN" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

# Check if bot already running
if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "INFO: Simulation bot already running (PID $OLD_PID)."
    echo "INFO: Log: $LOG_FILE"
    exit 0
  fi
fi

# Load environment and start bot
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

nohup "$PYTHON_BIN" "$ROOT_DIR/trading_bot.py" > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

echo "INFO: Simulation bot started."
echo "INFO: PID: $NEW_PID"
echo "INFO: Log: $LOG_FILE"
echo "INFO: To stop, run: bash scripts/stop_sim_bot.sh"
