#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMPARE_COINS=${COMPARE_COINS:-BTC,ETH,SOL,XRP,ADA,DOGE,TRX,CHZ,VVV}
COMPARE_REGIMES=${COMPARE_REGIMES:-uptrend,crash,mixed}
COMPARE_TOP=${COMPARE_TOP:-3}

exec "$SCRIPT_DIR/run_compare_simulation_regimes_on_pi.sh" \
    --coins "$COMPARE_COINS" \
    --regimes "$COMPARE_REGIMES" \
    --top "$COMPARE_TOP" \
    "$@"