#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
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

[[ -d "$SOURCE_DIR" ]] || _die "Source directory not found: $SOURCE_DIR"

if [[ ! -f "${SOURCE_DIR}/deploy/prometheus.yml" ]]; then
  _die "Monitoring configs not found in source directory: ${SOURCE_DIR}"
fi

_info "Installing monitoring dependencies..."
apt-get update -q
apt-get install -y -q prometheus prometheus-node-exporter rsync python3-numpy python3-pandas python3-pydantic

mkdir -p "$INSTALL_DIR"

if [[ -d "$INSTALL_DIR" ]]; then
  _info "Syncing trading tree into ${INSTALL_DIR}..."
  rsync -a --delete \
    --exclude='.git/' \
    --exclude='.venv/' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='logs/' \
    --exclude='results/' \
    --exclude='data/' \
    "${SOURCE_DIR}/" "${INSTALL_DIR}/"
fi

if ! getent group trading >/dev/null; then
  groupadd --system trading
fi

if ! id trading >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin --gid trading trading
fi

chown -R trading:trading "$INSTALL_DIR"

if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  _info "No /opt/trading_2/.env found yet; monitoring will still install, but trading services may stay in mock mode."
fi

mkdir -p /opt/trading_2/results/scorecards/textfile
mkdir -p /opt/trading_2/logs

for unit in pnl-exporter.service scorecard-status.service scorecard-status.timer node-exporter-textfile.service; do
  if [[ "$unit" == "pnl-exporter.service" ]]; then
    src="${SOURCE_DIR}/pnl-exporter.service"
  else
    src="${SOURCE_DIR}/deploy/${unit}"
  fi
  dst="${SYSTEMD_DIR}/${unit}"
  [[ -f "$src" ]] || _die "Missing unit file: $src"
  cp "$src" "$dst"
  _ok "Installed ${unit}"
done

mkdir -p "$PROMETHEUS_DIR" "$PROMETHEUS_RULES_DIR"
cp "${SOURCE_DIR}/deploy/prometheus.yml" "${PROMETHEUS_DIR}/prometheus.yml"
cp "${SOURCE_DIR}/deploy/trading-alerts.yml" "${PROMETHEUS_RULES_DIR}/trading-alerts.yml"
_ok "Prometheus config installed."

systemctl daemon-reload
systemctl enable --now prometheus-node-exporter.service
systemctl enable --now pnl-exporter.service
systemctl enable --now node-exporter-textfile.service
systemctl enable --now scorecard-status.timer
systemctl restart prometheus

_ok "Monitoring services enabled."

echo
echo "Browser endpoints (LAN):"
echo "  Prometheus:    http://<host-ip>:9090"
echo "  Node Exporter: http://<host-ip>:9100/metrics"