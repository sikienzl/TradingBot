#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SIGNAL_COIN="${VVV_SIGNAL_COIN:-VVV}" \
SIGNAL_LOG_PATH="${VVV_SIGNAL_LOG_PATH:-/opt/trading_2/logs/bot.log}" \
SIGNAL_WATCH_RSI_MAX="${VVV_WATCH_RSI_MAX:-70}" \
SIGNAL_WATCH_SELL_MAX="${VVV_WATCH_SELL_MAX:-0.36}" \
SIGNAL_WATCH_BUY_MIN="${VVV_WATCH_BUY_MIN:-0.44}" \
SIGNAL_WATCH_EDGE_MIN="${VVV_WATCH_EDGE_MIN:-0.05}" \
exec "$SCRIPT_DIR/check_coin_signal.sh" "${1:-siegfried@192.168.62.87}" "${VVV_SIGNAL_COIN:-VVV}"