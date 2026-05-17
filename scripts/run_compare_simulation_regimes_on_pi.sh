#!/usr/bin/env bash
set -euo pipefail

PI_HOST=${PI_HOST:-siegfried@192.168.62.87}
PI_APP_DIR=${PI_APP_DIR:-/opt/trading_2}
PI_SERVICE_USER=${PI_SERVICE_USER:-trading}
PI_PYTHON=${PI_PYTHON:-$PI_APP_DIR/.venv/bin/python}
PI_SCRIPT=${PI_SCRIPT:-$PI_APP_DIR/scripts/compare_simulation_regimes.py}

run_local() {
    cd "$PI_APP_DIR"
    sudo -u "$PI_SERVICE_USER" "$PI_PYTHON" "$PI_SCRIPT" "$@"
}

if [[ "$PI_HOST" == "local" || "$PI_HOST" == "localhost" ]]; then
    run_local "$@"
    exit 0
fi

ssh "$PI_HOST" bash -s -- \
    "$PI_APP_DIR" \
    "$PI_SERVICE_USER" \
    "$PI_PYTHON" \
    "$PI_SCRIPT" \
    "$@" <<'EOF'
set -euo pipefail

pi_app_dir=$1
pi_service_user=$2
pi_python=$3
pi_script=$4
shift 4

cd "$pi_app_dir"
sudo -u "$pi_service_user" "$pi_python" "$pi_script" "$@"
EOF