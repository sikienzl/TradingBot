#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/trading_2"
SYSTEMD_DIR="/etc/systemd/system"
PROMETHEUS_DIR="/etc/prometheus"
PROMETHEUS_RULES_DIR="${PROMETHEUS_DIR}/rules"

_die() { echo "ERROR: $*" >&2; exit 1; }
_info() { echo "INFO:  $*"; }
_ok() { echo "OK:    $*"; }

if [[ "$(id -u)" -ne 0 ]]; then
  _die "Run as root (sudo)."
fi

[[ -d "$INSTALL_DIR" ]] || _die "Install directory not found: $INSTALL_DIR"

_info "Installing monitoring dependencies..."
apt-get update -q
apt-get install -y -q prometheus prometheus-node-exporter apt-transport-https software-properties-common wget gpg

# Add Grafana APT repo if not already present
if [[ ! -f /etc/apt/sources.list.d/grafana.list ]]; then
  _info "Adding Grafana APT repository..."
  mkdir -p /etc/apt/keyrings
  wget -q -O /etc/apt/keyrings/grafana.gpg https://apt.grafana.com/gpg.key
    wget -q -O /tmp/grafana.gpg.asc https://apt.grafana.com/gpg.key
    gpg --batch --yes --dearmor -o /etc/apt/keyrings/grafana.gpg /tmp/grafana.gpg.asc
    rm -f /tmp/grafana.gpg.asc
    echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
    > /etc/apt/sources.list.d/grafana.list
  apt-get update -q
fi

apt-get install -y -q grafana
_ok "Packages installed."

mkdir -p /opt/trading_2/results/scorecards/textfile
chown -R trading:trading /opt/trading_2/results

for unit in scorecard-status.service scorecard-status.timer node-exporter-textfile.service; do
  src="${INSTALL_DIR}/deploy/${unit}"
  dst="${SYSTEMD_DIR}/${unit}"
  [[ -f "$src" ]] || _die "Missing unit file: $src"
  cp "$src" "$dst"
  _ok "Installed ${unit}"
done

mkdir -p "$PROMETHEUS_DIR" "$PROMETHEUS_RULES_DIR"
cp "${INSTALL_DIR}/deploy/prometheus.yml" "${PROMETHEUS_DIR}/prometheus.yml"
cp "${INSTALL_DIR}/deploy/trading-alerts.yml" "${PROMETHEUS_RULES_DIR}/trading-alerts.yml"
_ok "Prometheus config installed."

systemctl daemon-reload
systemctl enable --now node-exporter-textfile.service
systemctl enable --now scorecard-status.timer
systemctl restart prometheus

# ── Grafana provisioning ───────────────────────────────────────────────────────
_info "Configuring Grafana..."
mkdir -p /etc/grafana/provisioning/datasources
mkdir -p /etc/grafana/provisioning/dashboards
mkdir -p /etc/grafana/dashboards

cp "${INSTALL_DIR}/deploy/grafana-datasource.yml"              /etc/grafana/provisioning/datasources/trading.yml
cp "${INSTALL_DIR}/deploy/grafana-dashboard-provisioning.yml"  /etc/grafana/provisioning/dashboards/trading.yml
cp "${INSTALL_DIR}/deploy/grafana-dashboard.json"              /etc/grafana/dashboards/trading-bot.json

systemctl enable --now grafana-server
_ok "Grafana enabled. Dashboard will be available at http://<raspi-ip>:3000"
# Default login: admin / admin  (change on first login)

_ok "Monitoring services enabled."

echo
echo "Browser endpoints (LAN):"
echo "  Grafana:       http://<raspi-ip>:3000   (admin / admin)"
echo "  Prometheus:    http://<raspi-ip>:9090"
echo "  Node Exporter: http://<raspi-ip>:9100/metrics"
