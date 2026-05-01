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
apt-get install -y -q prometheus prometheus-node-exporter
_ok "Packages installed."

if ! id node-exp >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin node-exp
  _ok "Created system user node-exp"
fi

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

if systemctl list-unit-files | grep -q '^prometheus-node-exporter.service'; then
  systemctl disable --now prometheus-node-exporter.service || true
fi

systemctl daemon-reload
systemctl enable --now node-exporter-textfile.service
systemctl enable --now scorecard-status.timer
systemctl restart prometheus

_ok "Monitoring services enabled."

echo
echo "Browser endpoints (LAN):"
echo "  Node Exporter: http://<raspi-ip>:9100/metrics"
echo "  Prometheus:    http://<raspi-ip>:9090"
