#!/usr/bin/env bash
# Install AutoResearch systemd unit+timer using project .env
# Usage: sudo deploy/install_autoresearch.sh [/path/to/project]
set -euo pipefail
PROJECT_DIR=${1:-$(pwd)}
ENV_FILE="$PROJECT_DIR/.env"
SERVICE_SRC="$PROJECT_DIR/deploy/autoresearch.service"
TIMER_SRC="$PROJECT_DIR/deploy/autoresearch.timer"
INSTALL_DIR=/etc/systemd/system

if [[ ! -f "$SERVICE_SRC" || ! -f "$TIMER_SRC" ]]; then
  echo "service/timer sources not found in $PROJECT_DIR/deploy"
  exit 1
fi

# read interval from .env if present
INTERVAL_SEC=300
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -o allexport
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +o allexport
  if [[ -n "${AUTORESEARCH_INTERVAL_SEC:-}" ]]; then
    INTERVAL_SEC=${AUTORESEARCH_INTERVAL_SEC}
  fi
fi

# create a temporary timer with the desired interval
TMP_TIMER=$(mktemp)
awk -v sec="$INTERVAL_SEC" 'BEGIN{inTimer=0}
/\[Timer\]/{print; inTimer=1; next}
{ if(inTimer && /^OnUnitActiveSec=/) next; print }
END{ if(inTimer) print "OnUnitActiveSec=" sec "s" }' "$TIMER_SRC" > "$TMP_TIMER"

echo "Installing service and timer to $INSTALL_DIR"
install -m 644 "$SERVICE_SRC" "$INSTALL_DIR/autoresearch.service"
install -m 644 "$TMP_TIMER" "$INSTALL_DIR/autoresearch.timer"
rm -f "$TMP_TIMER"

echo "Reloading systemd daemon"
systemctl daemon-reload

echo "Enabling and starting autoresearch.timer"
systemctl enable --now autoresearch.timer

echo "Done. Check status with: systemctl status autoresearch.timer"
