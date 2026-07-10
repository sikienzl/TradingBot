#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
INSTALL_DIR="/opt/trading_2"
SERVICE_USER="trading"
SERVICE_GROUP="trading"
SYSTEMD_DIR="/etc/systemd/system"
PROMETHEUS_DIR="/etc/prometheus"
PROM_RULES_DIR="${PROMETHEUS_DIR}/rules"
GRAFANA_DS_DIR="/etc/grafana/provisioning/datasources"
GRAFANA_DASH_DIR="/etc/grafana/provisioning/dashboards"
GRAFANA_JSON_DIR="/etc/grafana/dashboards"
NODE_EXPORTER_VERSION="1.9.1"
NODE_EXPORTER_BIN="/usr/local/bin/node_exporter"
NODE_EXPORTER_UNIT="${SYSTEMD_DIR}/node-exporter.service"
TEXTFILE_DIR="/var/lib/node-exporter/textfile"
BOT_VENV="${INSTALL_DIR}/.venv"
BOT_PYTHON="${BOT_VENV}/bin/python"
BOT_PIP="${BOT_VENV}/bin/pip"

_die() { echo "ERROR: $*" >&2; exit 1; }
_info() { echo "INFO:  $*"; }
_ok() { echo "OK:    $*"; }
_warn() { echo "WARN:  $*"; }

if [[ "$(id -u)" -ne 0 ]]; then
  _die "Run as root (sudo)."
fi

[[ -d "$SOURCE_DIR" ]] || _die "Source directory not found: $SOURCE_DIR"
[[ -d "$INSTALL_DIR" ]] || _die "Install directory not found: $INSTALL_DIR"

ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) NODE_EXPORTER_ARCH="arm64" ;;
  x86_64|amd64) NODE_EXPORTER_ARCH="amd64" ;;
  armv7l|armhf) NODE_EXPORTER_ARCH="armv7" ;;
  *) _die "Unsupported architecture for node_exporter: $ARCH" ;;
esac

install_packages() {
  _info "Installing system dependencies..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q \
    python3 python3-venv python3-pip python3-dev build-essential pkg-config \
    prometheus prometheus-node-exporter apt-transport-https software-properties-common \
    wget gpg curl rsync ca-certificates
  _ok "System dependencies installed."
}

ensure_service_user() {
  if ! getent group "$SERVICE_GROUP" >/dev/null; then
    groupadd --system "$SERVICE_GROUP"
  fi
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir /home/${SERVICE_USER} --create-home --shell /usr/sbin/nologin --gid "$SERVICE_GROUP" "$SERVICE_USER"
  fi
  mkdir -p /home/${SERVICE_USER}
  chown -R ${SERVICE_USER}:${SERVICE_GROUP} /home/${SERVICE_USER}
  _ok "Service user ensured."
}

sync_install_tree() {
  _info "Syncing source tree into ${INSTALL_DIR}..."
  rsync -a \
    --exclude='.git/' \
    --exclude='.venv/' \
    --exclude='model/hailo_prefilter/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.env' \
    "${SOURCE_DIR}/" "${INSTALL_DIR}/"
  mkdir -p "${INSTALL_DIR}/logs" "${INSTALL_DIR}/results/scorecards/textfile" "${INSTALL_DIR}/data"
  chown -R ${SERVICE_USER}:${SERVICE_GROUP} "${INSTALL_DIR}"
  _ok "Install tree synced."
}

ensure_venv_and_requirements() {
  _info "Creating/updating Python virtual environment..."
  sudo -u ${SERVICE_USER} -H bash -lc "
    set -euo pipefail
    cd '${INSTALL_DIR}'
    if [[ ! -x '${BOT_PYTHON}' ]]; then
      /usr/bin/python3 -m venv '${BOT_VENV}'
    fi
    '${BOT_PYTHON}' -m pip install --upgrade pip setuptools wheel
    '${BOT_PIP}' install -r requirements.txt
  "
  _ok "Python environment ready at ${BOT_VENV}."
}

install_node_exporter_if_missing() {
  if [[ -x "$NODE_EXPORTER_BIN" ]]; then
    _ok "node_exporter already installed."
    return
  fi

  _info "Installing node_exporter ${NODE_EXPORTER_VERSION} (${NODE_EXPORTER_ARCH})..."
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' RETURN
  local archive="node_exporter-${NODE_EXPORTER_VERSION}.linux-${NODE_EXPORTER_ARCH}.tar.gz"
  local url="https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER_VERSION}/${archive}"
  curl -fsSL "$url" -o "${tmp_dir}/${archive}"
  tar -xzf "${tmp_dir}/${archive}" -C "$tmp_dir"
  install -m 0755 "${tmp_dir}/node_exporter-${NODE_EXPORTER_VERSION}.linux-${NODE_EXPORTER_ARCH}/node_exporter" "$NODE_EXPORTER_BIN"
  _ok "node_exporter installed to ${NODE_EXPORTER_BIN}."
}

install_node_exporter_unit() {
  _info "Installing node_exporter systemd unit..."
  mkdir -p "$TEXTFILE_DIR"
  cat > "$NODE_EXPORTER_UNIT" <<UNIT
[Unit]
Description=Prometheus Node Exporter
After=network.target

[Service]
User=nobody
ExecStart=${NODE_EXPORTER_BIN} --web.listen-address=:9100 --collector.textfile.directory=${TEXTFILE_DIR}
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
ReadWritePaths=${TEXTFILE_DIR}

[Install]
WantedBy=multi-user.target
UNIT
  _ok "node_exporter unit installed."
}

ensure_grafana_repo() {
  if [[ -f /etc/apt/sources.list.d/grafana.list ]]; then
    return
  fi
  _info "Adding Grafana APT repository..."
  mkdir -p /etc/apt/keyrings
  wget -q -O /tmp/grafana.gpg.asc https://apt.grafana.com/gpg.key
  gpg --batch --yes --dearmor -o /etc/apt/keyrings/grafana.gpg /tmp/grafana.gpg.asc
  rm -f /tmp/grafana.gpg.asc
  echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" > /etc/apt/sources.list.d/grafana.list
  apt-get update -q
  apt-get install -y -q grafana
  _ok "Grafana repository and package installed."
}

install_monitoring_units_and_configs() {
  _info "Installing monitoring units and configs..."
  cp "${SOURCE_DIR}/pnl-exporter.service" "${SYSTEMD_DIR}/pnl-exporter.service"
  cp "${SOURCE_DIR}/deploy/scorecard-status.service" "${SYSTEMD_DIR}/scorecard-status.service"
  cp "${SOURCE_DIR}/deploy/scorecard-status.timer" "${SYSTEMD_DIR}/scorecard-status.timer"
  cp "${SOURCE_DIR}/deploy/node-exporter-textfile.service" "${SYSTEMD_DIR}/node-exporter-textfile.service"
  cp "${SOURCE_DIR}/deploy/trading-bot.service" "${SYSTEMD_DIR}/trading-bot.service"

  mkdir -p "$PROMETHEUS_DIR" "$PROM_RULES_DIR" "$GRAFANA_DS_DIR" "$GRAFANA_DASH_DIR" "$GRAFANA_JSON_DIR"
  cp "${SOURCE_DIR}/deploy/prometheus.yml" "${PROMETHEUS_DIR}/prometheus.yml"
  cp "${SOURCE_DIR}/deploy/trading-alerts.yml" "${PROM_RULES_DIR}/trading-alerts.yml"
  cp "${SOURCE_DIR}/deploy/grafana-datasource.yml" "${GRAFANA_DS_DIR}/trading.yml"
  cp "${SOURCE_DIR}/deploy/grafana-dashboard-provisioning.yml" "${GRAFANA_DASH_DIR}/trading.yml"
  cp "${SOURCE_DIR}/deploy/grafana-dashboard.json" "${GRAFANA_JSON_DIR}/trading-bot.json"

  mkdir -p /etc/systemd/system/grafana-server.service.d
  cp "${SOURCE_DIR}/deploy/grafana-memory-limit.conf" /etc/systemd/system/grafana-server.service.d/memory-limit.conf
  cp "${SOURCE_DIR}/deploy/grafana-low-power.conf" /etc/systemd/system/grafana-server.service.d/low-power.conf
  _ok "Monitoring units and configs installed."
}

patch_trading_bot_unit_if_needed() {
  _info "Normalizing trading-bot.service to use project venv..."
  sed -i "s#^ExecStart=.*#ExecStart=${BOT_PYTHON} ${INSTALL_DIR}/src/trading_bot.py#" "${SYSTEMD_DIR}/trading-bot.service"
  _ok "trading-bot.service ExecStart set to ${BOT_PYTHON}."
}

start_services() {
  _info "Reloading systemd and starting services..."
  systemctl daemon-reload
  systemctl enable --now prometheus-node-exporter.service || true
  systemctl enable --now node-exporter.service
  systemctl enable --now pnl-exporter.service
  systemctl enable --now node-exporter-textfile.service || true
  systemctl enable --now scorecard-status.timer || true
  systemctl enable --now grafana-server || true
  systemctl restart prometheus
  systemctl restart grafana-server || true
  systemctl enable trading-bot.service
  systemctl reset-failed trading-bot.service || true
  systemctl restart trading-bot.service
  _ok "Core services restarted."
}

health_checks() {
  _info "Running health checks..."
  sleep 5
  echo
  echo "Service states:"
  systemctl is-active prometheus-node-exporter.service || true
  systemctl is-active node-exporter.service || true
  systemctl is-active pnl-exporter.service || true
  systemctl is-active trading-bot.service || true
  systemctl is-active prometheus || true
  systemctl is-active grafana-server || true
  echo
  echo "Prometheus targets:"
  curl -fsS http://localhost:9090/api/v1/targets | python3 - <<'PY'
import json, sys
payload = json.load(sys.stdin)
for item in payload.get('data', {}).get('activeTargets', []):
    labels = item.get('labels', {})
    print(f"{labels.get('job','?'):24} {labels.get('instance','?'):24} {item.get('health','?')} {item.get('lastError','')}")
PY
  echo
  echo "Key exporter metrics:"
  curl -fsS http://localhost:9200/metrics | grep -E '^(trading_portfolio_value_eur|trading_portfolio_cash_eur|trading_open_positions_count|trading_closed_trades|trading_realized_pnl_usd|trading_scorecard_final_exit_code)' || true
}

install_packages
ensure_service_user
sync_install_tree
ensure_venv_and_requirements
install_node_exporter_if_missing
install_node_exporter_unit
ensure_grafana_repo
install_monitoring_units_and_configs
patch_trading_bot_unit_if_needed
start_services
health_checks

echo
_ok "Trading stack install/repair complete."
echo "Grafana:    http://<host-ip>:3000"
echo "Prometheus: http://<host-ip>:9090"
echo "Metrics:    http://<host-ip>:9200/metrics"
