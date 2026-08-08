#!/usr/bin/env bash
# Start TradingBot with correct PYTHONPATH and virtualenv Python
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$ROOT_DIR"
if [ -f "$ROOT_DIR/.env" ]; then
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env" || true
fi
VENV_PY="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "Virtualenv python not found at $VENV_PY" >&2
  exit 2
fi
nohup "$VENV_PY" -u "$ROOT_DIR/src/trading_bot.py" > "$ROOT_DIR/trading_bot.log" 2>&1 &
echo "Started trading_bot, logs: $ROOT_DIR/trading_bot.log"
