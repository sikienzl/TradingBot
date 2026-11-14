#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PAPER_DIR="${PUBLIC_PAPER_DIR:-$HOME/trading-hailo-public-paper}"
OUT_DIR="${PUBLIC_PAPER_REPORT_DIR:-$PAPER_DIR/reports}"
PYTHON="${PYTHON_BIN:-python3}"
DAY="$(date -u -d yesterday +%F)"
umask 077
mkdir -p "$OUT_DIR"

args=(--date "$DAY" --journal "$PAPER_DIR/data/trade_journal.csv"
      --log "${PUBLIC_PAPER_LOG:-$PAPER_DIR/data/trading_bot.log}"
      --config "$PAPER_DIR/paper.env" --log-timezone "${PUBLIC_PAPER_LOG_TIMEZONE:-UTC}"
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
