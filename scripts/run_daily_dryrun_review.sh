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
STATUS_FILE="${DAILY_REVIEW_STATUS_FILE:-$OUT_DIR/latest_status.env}"
TS="$(date +%Y%m%d_%H%M%S)"
OUT_JSON="$OUT_DIR/review_${TS}.json"
OUT_TXT="$OUT_DIR/review_${TS}.txt"
OUT_HTML="$OUT_DIR/review_${TS}.html"
LATEST_JSON="$OUT_DIR/latest_review.json"
LATEST_TXT="$OUT_DIR/latest_review.txt"
LATEST_HTML="$OUT_DIR/latest_review.html"

mkdir -p "$OUT_DIR"

STARTED_UNIXTIME="$(date -u +%s)"

# The health exporter reads this marker, so it is written for every attempt -
# success or failure - and never contains credentials or account data.
write_status() {
  local exit_code="$1"
  local finished_unixtime temporary_path
  finished_unixtime="$(date -u +%s)"
  temporary_path="${STATUS_FILE}.tmp.$$"
  mkdir -p "$(dirname "$STATUS_FILE")"
  {
    printf 'exit_code=%s\n' "$exit_code"
    printf 'started_unixtime=%s\n' "$STARTED_UNIXTIME"
    printf 'finished_unixtime=%s\n' "$finished_unixtime"
  } > "$temporary_path"
  chmod 0644 "$temporary_path" 2>/dev/null || true
  mv -f "$temporary_path" "$STATUS_FILE"
}

trap 'write_status $?' EXIT

PYTHONPATH="$ROOT_DIR" "$PYTHON_CMD" "$ROOT_DIR/scripts/daily_dryrun_review.py" \
  --journal "$ROOT_DIR/trade_journal.csv" \
  --bot-log "$ROOT_DIR/logs/bot.log" \
  --ai-state "$ROOT_DIR/ai_copilot_state.json" \
  --ai-benchmark-state "$ROOT_DIR/ai_copilot_benchmark_state.json" \
  --lookback-hours "$LOOKBACK_HOURS" \
  --output-json "$OUT_JSON" \
  --output-txt "$OUT_TXT" \
  --output-html "$OUT_HTML"

cp "$OUT_JSON" "$LATEST_JSON"
cp "$OUT_TXT" "$LATEST_TXT"
cp "$OUT_HTML" "$LATEST_HTML"

echo "Daily review ready: $LATEST_TXT"
echo "Daily review ready: $LATEST_HTML"
