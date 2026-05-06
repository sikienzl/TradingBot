#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_CMD="$PYTHON_BIN"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_CMD="$ROOT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD="$(command -v python3)"
else
  echo "ERROR: No Python found. Set PYTHON_BIN or install python3." >&2
  exit 1
fi

LOOKBACK_HOURS="${DAILY_REVIEW_LOOKBACK_HOURS:-24}"
OUT_DIR="${DAILY_REVIEW_OUT_DIR:-$ROOT_DIR/results/daily_review}"
TS="$(date +%Y%m%d_%H%M%S)"
OUT_JSON="$OUT_DIR/review_${TS}.json"
OUT_TXT="$OUT_DIR/review_${TS}.txt"
LATEST_JSON="$OUT_DIR/latest_review.json"
LATEST_TXT="$OUT_DIR/latest_review.txt"

mkdir -p "$OUT_DIR"

"$PYTHON_CMD" "$ROOT_DIR/scripts/daily_dryrun_review.py" \
  --journal "$ROOT_DIR/trade_journal.csv" \
  --bot-log "$ROOT_DIR/logs/bot.log" \
  --ai-state "$ROOT_DIR/ai_copilot_state.json" \
  --lookback-hours "$LOOKBACK_HOURS" \
  --output-json "$OUT_JSON" \
  --output-txt "$OUT_TXT"

cp "$OUT_JSON" "$LATEST_JSON"
cp "$OUT_TXT" "$LATEST_TXT"

echo "Daily review ready: $LATEST_TXT"
