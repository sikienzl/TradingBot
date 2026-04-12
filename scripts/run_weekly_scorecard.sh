#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Python binary selection (override with PYTHON_BIN)
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

# Tunables (override via environment variables)
JOURNAL_FILE="${JOURNAL_FILE:-$ROOT_DIR/trade_journal.csv}"
LOOKBACK_DAYS="${LOOKBACK_DAYS:-7}"
STARTING_CAPITAL="${STARTING_CAPITAL:-20}"
MIN_CLOSED_TRADES="${MIN_CLOSED_TRADES:-200}"
MIN_WIN_RATE="${MIN_WIN_RATE:-45}"
MIN_PROFIT_FACTOR="${MIN_PROFIT_FACTOR:-1.2}"
MIN_AVG_PNL="${MIN_AVG_PNL:-0.0}"
MAX_DRAWDOWN_PCT="${MAX_DRAWDOWN_PCT:-10}"

TS="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/results/scorecards}"
OUT_FILE="$OUT_DIR/scorecard_${TS}.txt"
LATEST_LINK="$OUT_DIR/latest_scorecard.txt"

mkdir -p "$OUT_DIR"

echo "=== Weekly Go/No-Go Scorecard ===" | tee "$OUT_FILE"
echo "Timestamp: $(date -Iseconds)" | tee -a "$OUT_FILE"
echo "Python: $PYTHON_CMD" | tee -a "$OUT_FILE"
echo "Journal: $JOURNAL_FILE" | tee -a "$OUT_FILE"
echo | tee -a "$OUT_FILE"

set +e
"$PYTHON_CMD" "$ROOT_DIR/go_no_go_scorecard.py" \
  --file "$JOURNAL_FILE" \
  --lookback-days "$LOOKBACK_DAYS" \
  --starting-capital "$STARTING_CAPITAL" \
  --min-closed-trades "$MIN_CLOSED_TRADES" \
  --min-win-rate "$MIN_WIN_RATE" \
  --min-profit-factor "$MIN_PROFIT_FACTOR" \
  --min-avg-pnl "$MIN_AVG_PNL" \
  --max-drawdown-pct "$MAX_DRAWDOWN_PCT" \
  | tee -a "$OUT_FILE"
RC=$?
set -e

ln -sfn "$(basename "$OUT_FILE")" "$LATEST_LINK"

echo | tee -a "$OUT_FILE"
if [[ $RC -eq 0 ]]; then
  echo "Verdict summary: GO" | tee -a "$OUT_FILE"
elif [[ $RC -eq 2 ]]; then
  echo "Verdict summary: HOLD" | tee -a "$OUT_FILE"
elif [[ $RC -eq 3 ]]; then
  echo "Verdict summary: NO-GO" | tee -a "$OUT_FILE"
else
  echo "Verdict summary: ERROR (exit code $RC)" | tee -a "$OUT_FILE"
fi

echo "Saved scorecard report: $OUT_FILE"
exit $RC
