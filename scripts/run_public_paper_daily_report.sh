#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PAPER_DIR="${PUBLIC_PAPER_DIR:-$HOME/trading-hailo-public-paper}"
OUT_DIR="${PUBLIC_PAPER_REPORT_DIR:-$PAPER_DIR/reports}"
STATUS_FILE="${PUBLIC_PAPER_STATUS_FILE:-$OUT_DIR/latest_status.env}"
PYTHON="${PYTHON_BIN:-python3}"
DAY="$(date -u -d yesterday +%F)"
umask 077
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
  mv -f "$temporary_path" "$STATUS_FILE"
}

trap 'write_status $?' EXIT

args=(--journal "$PAPER_DIR/data/trade_journal.csv"
      --log "${PUBLIC_PAPER_LOG:-$PAPER_DIR/data/trading_bot.log}"
      --config "$PAPER_DIR/paper.env" --log-timezone "${PUBLIC_PAPER_LOG_TIMEZONE:-UTC}"
      --max-data-age-hours "${PUBLIC_PAPER_MAX_DATA_AGE_HOURS:-26}"
      --output "$OUT_DIR/report-$DAY.json")
if [[ -n "${PUBLIC_PAPER_COMPARE_JOURNAL:-}" ]]; then
  args+=(--compare-journal "$PUBLIC_PAPER_COMPARE_JOURNAL"
         --compare-feed "${PUBLIC_PAPER_COMPARE_FEED:-unknown}")
  if [[ -n "${PUBLIC_PAPER_COMPARE_CONTAINER:-}" ]]; then
    args+=(--compare-container "$PUBLIC_PAPER_COMPARE_CONTAINER")
  fi
  if [[ -n "${PUBLIC_PAPER_COMPARE_CONFIG:-}" ]]; then
    args+=(--compare-config "$PUBLIC_PAPER_COMPARE_CONFIG")
  fi
fi
"$PYTHON" "$ROOT_DIR/scripts/public_paper_daily_report.py" "${args[@]}"
cp "$OUT_DIR/report-$DAY.json" "$OUT_DIR/latest.json"
echo "Public paper daily report: $OUT_DIR/report-$DAY.json"
