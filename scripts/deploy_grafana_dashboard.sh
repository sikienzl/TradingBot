#!/usr/bin/env bash
set -eu

# Usage:
# 1) Deploy locally on the Grafana host: ./deploy_grafana_dashboard.sh /path/to/trading-portfolio-dashboard.json
# 2) Deploy remotely from this machine: ./deploy_grafana_dashboard.sh /path/to/trading-portfolio-dashboard.json user@pi-host

LOCAL_DASH="$1"
REMOTE_HOST="${2:-}"
DASH_NAME="$(basename "$LOCAL_DASH")"

if [ -n "$REMOTE_HOST" ]; then
  echo "Copying $LOCAL_DASH to $REMOTE_HOST:/tmp/ and installing on remote Grafana"
  scp "$LOCAL_DASH" "$REMOTE_HOST":/tmp/
  ssh "$REMOTE_HOST" "sudo mv /tmp/$DASH_NAME /etc/grafana/dashboards/ && sudo chown root:root /etc/grafana/dashboards/$DASH_NAME && sudo systemctl restart grafana-server"
  echo "Deployed to $REMOTE_HOST:/etc/grafana/dashboards/$DASH_NAME"
  exit 0
fi

# Local deploy (run on the Grafana host)
if [ "$EUID" -ne 0 ]; then
  echo "Running local deploy: requires sudo to move file into /etc/grafana/dashboards and restart Grafana"
  echo "Run: sudo $0 $LOCAL_DASH"
  exit 1
fi

mkdir -p /etc/grafana/dashboards
mv "$LOCAL_DASH" /etc/grafana/dashboards/ || { echo "mv failed"; exit 2; }
chown root:root "/etc/grafana/dashboards/$DASH_NAME"
systemctl restart grafana-server

echo "Installed /etc/grafana/dashboards/$DASH_NAME and restarted grafana-server"
