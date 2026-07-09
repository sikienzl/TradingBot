#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_USER="${REMOTE_USER:-pi}"
NODE1_IP="${NODE1_IP:-192.168.1.10}"
NODE2_IP="${NODE2_IP:-192.168.1.11}"
OLD_BOT_IP="${OLD_BOT_IP:-192.168.1.20}"
APP_DIR="${APP_DIR:-/opt/trading_2}"
OLD_ENV_PATH="${OLD_ENV_PATH:-/opt/trading_2/.env}"
KUBE_NAMESPACE="${KUBE_NAMESPACE:-trading-bot}"
MASTER_LABEL_VALUE="${MASTER_LABEL_VALUE:-master-ssd}"
HAILO_LABEL_VALUE="${HAILO_LABEL_VALUE:-hailo-worker}"
DB_NAME="${DB_NAME:-appdb}"
DB_USER="${DB_USER:-trading_db}"
DB_PASSWORD="${DB_PASSWORD:-}"
REINSTALL_K3S="false"
BUILD_IMAGES="true"
DEPLOY_ANALYTICS_WRITER="false"

SSH_OPTS=(
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=10
)

die() {
  echo "ERROR: $*" >&2
  exit 1
}

info() {
  echo "INFO:  $*"
}

ok() {
  echo "OK:    $*"
}

usage() {
  cat <<'EOF'
Usage: DB_PASSWORD='<postgres-password>' bash scripts/setup_rpi_k3s_cluster.sh [options]

Options:
  --reinstall-k3s            Reinstall K3s server and agent before continuing.
  --skip-image-build         Skip Docker image build/import for cloud and Hailo workloads.
  --deploy-analytics-writer  Also deploy the dry-run analytics writer workload.
  --help                     Show this help text.

Environment overrides:
  REMOTE_USER, NODE1_IP, NODE2_IP, OLD_BOT_IP, APP_DIR, OLD_ENV_PATH,
  KUBE_NAMESPACE, DB_NAME, DB_USER, DB_PASSWORD.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reinstall-k3s)
      REINSTALL_K3S="true"
      ;;
    --skip-image-build)
      BUILD_IMAGES="false"
      ;;
    --deploy-analytics-writer)
      DEPLOY_ANALYTICS_WRITER="true"
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
  shift
done

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

remote() {
  local host="$1"
  shift
  ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${host}" "$@"
}

remote_sudo() {
  local host="$1"
  shift
  ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${host}" "sudo bash -lc $(printf '%q' "$*")"
}

copy_to_remote() {
  local src="$1"
  local host="$2"
  local dest="$3"
  local remote_tmp
  remote_tmp="/tmp/$(basename "$dest").$$"
  scp "${SSH_OPTS[@]}" "$src" "${REMOTE_USER}@${host}:$remote_tmp"
  remote_sudo "$host" "install -D -m 600 '$remote_tmp' '$dest'; chown '${REMOTE_USER}:${REMOTE_USER}' '$dest'; rm -f '$remote_tmp'"
}

require_cmd ssh
require_cmd scp
require_cmd rsync
require_cmd curl
require_cmd python3

[[ -n "$DB_PASSWORD" ]] || die "DB_PASSWORD must be set in the environment."

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
LOCAL_ENV_COPY="$TMP_DIR/legacy.env"
LOCAL_ENV_MERGED="$TMP_DIR/cluster.env"
LOCAL_SECRET_MANIFEST="$TMP_DIR/trading-bot-credentials.yaml"

sync_repo() {
  local host="$1"
  info "Syncing repository to ${host}:${APP_DIR}"
  remote_sudo "$host" "mkdir -p '$APP_DIR'; chown -R '${REMOTE_USER}:${REMOTE_USER}' '$APP_DIR'"
  rsync -az --delete --rsync-path="sudo rsync" \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    --exclude 'build/' \
    --exclude 'catboost_info/' \
    --exclude 'logs/' \
    --exclude 'results/' \
    --exclude 'old_data/' \
    --exclude 'model/' \
    --exclude '*.csv' \
    --exclude '*.log' \
    --exclude '.env' \
    "$ROOT_DIR/" "${REMOTE_USER}@${host}:${APP_DIR}/"
}

reset_k3s_if_requested() {
  if [[ "$REINSTALL_K3S" != "true" ]]; then
    return 0
  fi

  info "Reinstall requested, removing existing K3s components"
  remote_sudo "$NODE2_IP" "systemctl stop k3s-agent >/dev/null 2>&1 || true; if [[ -x /usr/local/bin/k3s-agent-uninstall.sh ]]; then /usr/local/bin/k3s-agent-uninstall.sh || true; fi"
  remote_sudo "$NODE1_IP" "systemctl stop k3s >/dev/null 2>&1 || true; if [[ -x /usr/local/bin/k3s-uninstall.sh ]]; then /usr/local/bin/k3s-uninstall.sh || true; fi"
}

ensure_server() {
  info "Ensuring K3s server is installed and up to date on ${NODE1_IP}"
  remote_sudo "$NODE1_IP" "curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC='server --write-kubeconfig-mode 644' sh -; systemctl enable --now k3s"
}

server_token() {
  remote_sudo "$NODE1_IP" "cat /var/lib/rancher/k3s/server/node-token"
}

ensure_agent() {
  local token="$1"
  info "Rejoining K3s agent on ${NODE2_IP}"
  remote_sudo "$NODE2_IP" "systemctl stop k3s-agent >/dev/null 2>&1 || true; if [[ -x /usr/local/bin/k3s-agent-uninstall.sh ]]; then /usr/local/bin/k3s-agent-uninstall.sh || true; fi; curl -sfL https://get.k3s.io | K3S_URL='https://${NODE1_IP}:6443' K3S_TOKEN='${token}' INSTALL_K3S_EXEC='agent' sh -; systemctl enable --now k3s-agent"
}

wait_for_nodes() {
  info "Waiting for both cluster nodes to become Ready"
  local attempts=36
  local ready_count
  while (( attempts > 0 )); do
    ready_count="$(remote_sudo "$NODE1_IP" "kubectl get nodes --no-headers 2>/dev/null | awk '\$2 == \"Ready\" {count++} END {print count + 0}'" || true)"
    if [[ "$ready_count" == "2" ]]; then
      ok "Both nodes are registered and Ready"
      return 0
    fi
    sleep 5
    attempts=$((attempts - 1))
  done
  remote_sudo "$NODE1_IP" "kubectl get nodes -o wide" || true
  die "Timed out waiting for both K3s nodes to become Ready."
}

label_nodes() {
  info "Applying stable scheduling labels and worker taint"
  local node1_name node2_name
  node1_name="$(remote_sudo "$NODE1_IP" "kubectl get nodes -o wide --no-headers | awk '\$6 == \"${NODE1_IP}\" {print \$1; exit}'")"
  node2_name="$(remote_sudo "$NODE1_IP" "kubectl get nodes -o wide --no-headers | awk '\$6 == \"${NODE2_IP}\" {print \$1; exit}'")"
  [[ -n "$node1_name" ]] || die "Could not determine node name for ${NODE1_IP}."
  [[ -n "$node2_name" ]] || die "Could not determine node name for ${NODE2_IP}."

  remote_sudo "$NODE1_IP" "kubectl label node '${node1_name}' trading-role='${MASTER_LABEL_VALUE}' --overwrite; kubectl label node '${node2_name}' trading-role='${HAILO_LABEL_VALUE}' --overwrite; kubectl taint nodes '${node2_name}' hailo-workload=true:NoSchedule --overwrite"
}

prepare_directories() {
  info "Preparing storage directories on both nodes"
  remote_sudo "$NODE1_IP" "mkdir -p /mnt/nvme_data/trading_db; chown root:root /mnt/nvme_data/trading_db; if ! findmnt -T /mnt/nvme_data >/dev/null 2>&1; then echo 'WARN: /mnt/nvme_data is not a mountpoint on node 1.' >&2; fi"
  remote_sudo "$NODE2_IP" "mkdir -p /mnt/nvme/trading_ticks; if ! findmnt -T /mnt/nvme >/dev/null 2>&1; then echo 'WARN: /mnt/nvme is not a mountpoint on node 2.' >&2; fi"
}

fetch_legacy_env() {
  info "Fetching legacy .env from ${OLD_BOT_IP}:${OLD_ENV_PATH}"
  ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${OLD_BOT_IP}" "sudo cat '$OLD_ENV_PATH'" > "$LOCAL_ENV_COPY"
}

merge_env() {
  info "Adjusting .env for the new K3s analytics service"
  python3 - "$LOCAL_ENV_COPY" "$LOCAL_ENV_MERGED" "$DB_NAME" "$DB_USER" "$DB_PASSWORD" <<'PY'
from pathlib import Path
import sys

src = Path(sys.argv[1])
dest = Path(sys.argv[2])
db_name = sys.argv[3]
db_user = sys.argv[4]
db_password = sys.argv[5]

updates = {
    "ANALYTICS_DB_ENABLED": "true",
    "ANALYTICS_DB_HOST": "postgres-analytics",
    "ANALYTICS_DB_PORT": "5432",
    "ANALYTICS_DB_NAME": db_name,
    "ANALYTICS_DB_USER": db_user,
    "ANALYTICS_DB_PASSWORD": db_password,
    "ANALYTICS_DB_SSLMODE": "disable",
    "ANALYTICS_DB_SCHEMA": "trading_analytics",
    "ANALYTICS_DB_CONNECT_TIMEOUT_SECONDS": "5",
    "ANALYTICS_DB_SNAPSHOT_EVERY": "1",
    "POSTGRES_ENABLED": "true",
    "POSTGRES_HOST": "postgres-analytics",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": db_name,
    "POSTGRES_USER": db_user,
    "POSTGRES_PASSWORD": db_password,
    "POSTGRES_SSLMODE": "disable",
}

lines = src.read_text(encoding="utf-8").splitlines()
found = set()
values = {}

for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    values[key] = value

if values.get("MAMMOUTH_API_KEY") and not values.get("GPT5_API_KEY"):
    updates["GPT5_API_KEY"] = values["MAMMOUTH_API_KEY"]
if values.get("GPT5_API_KEY") and not values.get("MAMMOUTH_API_KEY"):
    updates["MAMMOUTH_API_KEY"] = values["GPT5_API_KEY"]
if values.get("KRAKEN_API_SECRET") and not values.get("KRAKEN_SECRET_KEY"):
    updates["KRAKEN_SECRET_KEY"] = values["KRAKEN_API_SECRET"]
if values.get("KRAKEN_SECRET_KEY") and not values.get("KRAKEN_API_SECRET"):
    updates["KRAKEN_API_SECRET"] = values["KRAKEN_SECRET_KEY"]

def encode(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'

output = []
for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        output.append(line)
        continue
    key, _ = line.split("=", 1)
    key = key.strip()
    if key in updates:
        output.append(f"{key}={encode(updates[key])}")
        found.add(key)
    else:
        output.append(line)

missing_keys = [key for key in updates if key not in found]
if missing_keys:
    if output and output[-1].strip():
        output.append("")
    output.append("# Added for the K3s analytics cluster")
    for key in missing_keys:
        output.append(f"{key}={encode(updates[key])}")

dest.write_text("\n".join(output) + "\n", encoding="utf-8")
PY
}

install_env() {
  info "Installing migrated .env on both cluster nodes"
  remote_sudo "$NODE1_IP" "mkdir -p '$APP_DIR'; chown -R '${REMOTE_USER}:${REMOTE_USER}' '$APP_DIR'"
  remote_sudo "$NODE2_IP" "mkdir -p '$APP_DIR'; chown -R '${REMOTE_USER}:${REMOTE_USER}' '$APP_DIR'"
  copy_to_remote "$LOCAL_ENV_MERGED" "$NODE1_IP" "$APP_DIR/.env"
  copy_to_remote "$LOCAL_ENV_MERGED" "$NODE2_IP" "$APP_DIR/.env"
}

render_secret_manifest() {
  info "Rendering runtime secret manifest from the merged .env"
  python3 - "$LOCAL_ENV_MERGED" "$LOCAL_SECRET_MANIFEST" "$KUBE_NAMESPACE" "$DB_PASSWORD" <<'PY'
from pathlib import Path
import sys

env_path = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
namespace = sys.argv[3]
db_password = sys.argv[4]

values = {}
for line in env_path.read_text(encoding="utf-8").splitlines():
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    values[key] = value

secret_values = {
    "GPT5_API_KEY": values.get("GPT5_API_KEY") or values.get("MAMMOUTH_API_KEY") or "",
    "MAMMOUTH_API_KEY": values.get("MAMMOUTH_API_KEY") or values.get("GPT5_API_KEY") or "",
    "KRAKEN_API_KEY": values.get("KRAKEN_API_KEY", ""),
    "KRAKEN_API_SECRET": values.get("KRAKEN_API_SECRET") or values.get("KRAKEN_SECRET_KEY") or "",
    "KRAKEN_SECRET_KEY": values.get("KRAKEN_SECRET_KEY") or values.get("KRAKEN_API_SECRET") or "",
    "ANALYTICS_DB_PASSWORD": db_password,
}

def encode(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'

lines = [
    "apiVersion: v1",
    "kind: Secret",
    "metadata:",
    "  name: trading-bot-credentials",
    f"  namespace: {namespace}",
    "type: Opaque",
    "stringData:",
]
for key, value in secret_values.items():
    lines.append(f"  {key}: {encode(value)}")

manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

containerd_socket() {
  local host="$1"
  remote_sudo "$host" "for candidate in /run/k3s/containerd/containerd.sock /run/containerd/containerd.sock; do if [[ -S \"\$candidate\" ]]; then echo \"\$candidate\"; exit 0; fi; done; exit 1"
}

build_and_import_image() {
  local host="$1"
  local dockerfile="$2"
  local image="$3"
  local socket

  info "Building ${image} on ${host}"
  remote_sudo "$host" "cd '$APP_DIR' && docker build -f '$dockerfile' -t '$image' ."
  socket="$(containerd_socket "$host")"
  info "Importing ${image} into containerd on ${host} via ${socket}"
  remote_sudo "$host" "cd '$APP_DIR' && docker save '$image' | ctr -a '$socket' -n k8s.io images import -"
}

deploy_manifests() {
  info "Applying base resources and runtime secret"
  copy_to_remote "$LOCAL_SECRET_MANIFEST" "$NODE1_IP" "/tmp/trading-bot-credentials.yaml"
  remote_sudo "$NODE1_IP" "kubectl apply -k '$APP_DIR/k8s/base'; kubectl apply -f /tmp/trading-bot-credentials.yaml; kubectl -n '$KUBE_NAMESPACE' delete secret trading-bot-runtime-env --ignore-not-found; kubectl -n '$KUBE_NAMESPACE' create secret generic trading-bot-runtime-env --from-env-file='$APP_DIR/.env'"

  info "Deploying Postgres analytics"
  remote_sudo "$NODE1_IP" "kubectl apply -k '$APP_DIR/k8s/overlays/postgres-analytics'; kubectl -n '$KUBE_NAMESPACE' rollout status statefulset/postgres-analytics --timeout=300s"

  if [[ "$BUILD_IMAGES" == "true" ]]; then
    info "Deploying cloud strategist and Hailo worker workloads"
    remote_sudo "$NODE1_IP" "kubectl apply -k '$APP_DIR/k8s/overlays/cloud-strategist'; kubectl apply -k '$APP_DIR/k8s/overlays/hailo-worker'; kubectl -n '$KUBE_NAMESPACE' rollout status deployment/trading-cloud-strategist --timeout=300s; kubectl -n '$KUBE_NAMESPACE' rollout status daemonset/trading-hailo-worker --timeout=300s"
  else
    info "Skipping cloud/Hailo workload deployment because image build was disabled"
  fi

  if [[ "$DEPLOY_ANALYTICS_WRITER" == "true" ]]; then
    info "Deploying analytics writer workload"
    remote_sudo "$NODE1_IP" "kubectl apply -k '$APP_DIR/k8s/overlays/analytics-writer'; kubectl -n '$KUBE_NAMESPACE' rollout status deployment/trading-analytics-writer --timeout=300s"
  fi
}

show_status() {
  info "Current cluster status"
  remote_sudo "$NODE1_IP" "kubectl get nodes -o wide; echo '---'; kubectl get pods -n '$KUBE_NAMESPACE' -o wide; echo '---'; kubectl get pvc -n '$KUBE_NAMESPACE'"
}

reset_k3s_if_requested
ensure_server
TOKEN="$(server_token)"
ensure_agent "$TOKEN"
wait_for_nodes
label_nodes
prepare_directories
sync_repo "$NODE1_IP"
sync_repo "$NODE2_IP"
fetch_legacy_env
merge_env
install_env
render_secret_manifest

if [[ "$BUILD_IMAGES" == "true" ]]; then
  build_and_import_image "$NODE1_IP" "docker/cloud-strategist.Dockerfile" "trading-cloud:latest"
  build_and_import_image "$NODE2_IP" "docker/hailo-worker.Dockerfile" "trading-hailo:latest"
fi

deploy_manifests
show_status

ok "Cluster setup completed."