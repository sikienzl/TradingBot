#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_USER="${REMOTE_USER:-siegfried}"
NODE1_IP="${NODE1_IP:-192.168.62.74}"
NODE2_IP="${NODE2_IP:-192.168.62.75}"
APP_DIR="${APP_DIR:-/opt/trading_2}"
KUBE_NAMESPACE="${KUBE_NAMESPACE:-trading-bot}"
MASTER_LABEL_VALUE="${MASTER_LABEL_VALUE:-master-ssd}"
HAILO_LABEL_VALUE="${HAILO_LABEL_VALUE:-hailo-worker}"
BUILD_IMAGES="true"
RELOAD_MONITORING="true"
RUN_SMOKE_TEST="true"

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
Usage: bash scripts/rollout_hybrid_cluster.sh [options]

Options:
  --skip-image-build      Skip docker build/import on both nodes.
  --skip-monitoring       Skip Prometheus/Grafana config reload on node 1.
  --skip-smoke-test       Skip the local shadow-mode smoke test on node 1.
  --help                  Show this help text.

Environment overrides:
  REMOTE_USER, NODE1_IP, NODE2_IP, APP_DIR, KUBE_NAMESPACE.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-image-build)
      BUILD_IMAGES="false"
      ;;
    --skip-monitoring)
      RELOAD_MONITORING="false"
      ;;
    --skip-smoke-test)
      RUN_SMOKE_TEST="false"
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

containerd_socket() {
  local host="$1"
  remote_sudo "$host" "for candidate in /run/k3s/containerd/containerd.sock /run/containerd/containerd.sock; do if [[ -S \"\$candidate\" ]]; then echo \"\$candidate\"; exit 0; fi; done; exit 1"
}

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
    --exclude '.env' \
    "$ROOT_DIR/" "${REMOTE_USER}@${host}:${APP_DIR}/"
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

label_nodes() {
  info "Ensuring stable scheduling labels are present"
  local node1_name node2_name
  node1_name="$(remote_sudo "$NODE1_IP" "kubectl get nodes -o wide --no-headers | awk '\$6 == \"${NODE1_IP}\" {print \$1; exit}'")"
  node2_name="$(remote_sudo "$NODE1_IP" "kubectl get nodes -o wide --no-headers | awk '\$6 == \"${NODE2_IP}\" {print \$1; exit}'")"
  [[ -n "$node1_name" ]] || die "Could not determine node name for ${NODE1_IP}"
  [[ -n "$node2_name" ]] || die "Could not determine node name for ${NODE2_IP}"

  remote_sudo "$NODE1_IP" "kubectl label node '$node1_name' trading-role='${MASTER_LABEL_VALUE}' --overwrite; kubectl label node '$node2_name' trading-role='${HAILO_LABEL_VALUE}' --overwrite; kubectl taint nodes '$node2_name' hailo-workload=true:NoSchedule --overwrite"
}

create_runtime_secret() {
  info "Refreshing runtime env secret from ${APP_DIR}/.env on node 1"
  remote_sudo "$NODE1_IP" "test -f '$APP_DIR/.env' || exit 2"
  remote_sudo "$NODE1_IP" "kubectl -n '$KUBE_NAMESPACE' delete secret trading-bot-runtime-env --ignore-not-found; kubectl -n '$KUBE_NAMESPACE' create secret generic trading-bot-runtime-env --from-env-file='$APP_DIR/.env'"
}

apply_manifests() {
  info "Applying hybrid Kubernetes overlays"
  remote_sudo "$NODE1_IP" "kubectl apply -k '$APP_DIR/k8s/base'"
  remote_sudo "$NODE1_IP" "kubectl apply -k '$APP_DIR/k8s/overlays/cloud-strategist'"
  remote_sudo "$NODE1_IP" "kubectl apply -k '$APP_DIR/k8s/overlays/hailo-worker'"
  remote_sudo "$NODE1_IP" "kubectl apply -k '$APP_DIR/k8s/overlays/market-data-relay'"
  remote_sudo "$NODE1_IP" "kubectl -n '$KUBE_NAMESPACE' rollout status deployment/trading-cloud-strategist --timeout=300s"
  remote_sudo "$NODE1_IP" "kubectl -n '$KUBE_NAMESPACE' rollout status deployment/trading-market-data-relay --timeout=300s"
  remote_sudo "$NODE1_IP" "kubectl -n '$KUBE_NAMESPACE' rollout status daemonset/trading-hailo-worker --timeout=300s"
}

reload_monitoring() {
  info "Reloading Prometheus and Grafana configs on node 1"
  remote_sudo "$NODE1_IP" "mkdir -p /etc/prometheus /etc/prometheus/rules /etc/grafana/provisioning/datasources /etc/grafana/provisioning/dashboards /etc/grafana/dashboards"
  remote_sudo "$NODE1_IP" "cd '$APP_DIR' && WORKER_NODE=\$(kubectl get nodes -l trading-role='${HAILO_LABEL_VALUE}' -o jsonpath='{.items[0].metadata.name}') && python3 scripts/render_cluster_prometheus_config.py '$KUBE_NAMESPACE' \"\$WORKER_NODE\" > /etc/prometheus/prometheus.yml && cp '$APP_DIR/deploy/trading-alerts.yml' /etc/prometheus/rules/trading-alerts.yml"
  remote_sudo "$NODE1_IP" "cp '$APP_DIR/deploy/grafana-datasource.yml' /etc/grafana/provisioning/datasources/trading.yml; cp '$APP_DIR/deploy/grafana-dashboard-provisioning.yml' /etc/grafana/provisioning/dashboards/trading.yml; cp '$APP_DIR/deploy/grafana-dashboard.json' /etc/grafana/dashboards/trading-bot.json; systemctl restart prometheus; systemctl restart grafana-server"
}

run_smoke_test() {
  info "Running hybrid shadow smoke test on node 1"
  remote_sudo "$NODE1_IP" "kubectl wait --for=condition=Ready pod -n '$KUBE_NAMESPACE' -l component=hailo-worker --timeout=180s >/dev/null; POD=\$(kubectl get pods -n '$KUBE_NAMESPACE' -l component=hailo-worker --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}'); test -n \"\$POD\" || exit 1; kubectl exec -n '$KUBE_NAMESPACE' -c hailo-worker \"\$POD\" -- python3 /app/scripts/run_hybrid_shadow_mode_smoke_test.py"
}

verify_monitoring() {
  info "Verifying Prometheus hybrid targets and Grafana availability"
  remote_sudo "$NODE1_IP" "set -euo pipefail
prom_targets=\$(mktemp)
prom_query=\$(mktemp)
trap 'rm -f \"\$prom_targets\" \"\$prom_query\"' EXIT
attempt=0
until curl -fsSI http://127.0.0.1:3000/login >/dev/null 2>&1; do
  attempt=\$((attempt + 1))
  [[ \$attempt -lt 20 ]] || exit 1
  sleep 3
done

attempt=0
while true; do
  curl -fsS http://127.0.0.1:9090/api/v1/targets > \"\$prom_targets\"
  if PROM_TARGETS_PATH=\"\$prom_targets\" python3 - <<'PY'
import json
import os
obj = json.load(open(os.environ['PROM_TARGETS_PATH']))
required = {
    'trading_hybrid_edge_filter',
    'trading_hybrid_strategist',
    'trading_hybrid_market_relay',
}
seen = {}
for target in obj['data']['activeTargets']:
    job = target['labels'].get('job')
    seen[job] = target.get('health')
missing = required.difference(seen)
if missing:
    raise SystemExit(f'missing targets: {sorted(missing)}')
bad = [job for job in required if seen.get(job) != 'up']
if bad:
    raise SystemExit(f'unhealthy targets: {bad}')
PY
  then
    break
  fi
  attempt=\$((attempt + 1))
  [[ \$attempt -lt 20 ]] || exit 1
  sleep 3
done

curl -fsS http://127.0.0.1:9090/api/v1/query --data-urlencode 'query=trading_hybrid_edge_filter_up or trading_hybrid_strategist_up or trading_hybrid_market_relay_up' > \"\$prom_query\" && PROM_QUERY_PATH=\"\$prom_query\" python3 - <<'PY'
import json
import os
data = json.load(open(os.environ['PROM_QUERY_PATH']))
print(json.dumps(data['data']['result'], indent=2))
PY
curl -fsSI http://127.0.0.1:3000/login | head -n 1"
}

show_status() {
  info "Collecting cluster status"
  remote_sudo "$NODE1_IP" "kubectl get nodes -o wide; echo '---'; kubectl get pods -n '$KUBE_NAMESPACE' -o wide; echo '---'; kubectl get svc -n '$KUBE_NAMESPACE'; echo '---'; kubectl get endpoints -n '$KUBE_NAMESPACE'"
}

require_cmd ssh
require_cmd scp
require_cmd rsync

sync_repo "$NODE1_IP"
sync_repo "$NODE2_IP"
label_nodes
create_runtime_secret

if [[ "$BUILD_IMAGES" == "true" ]]; then
  build_and_import_image "$NODE1_IP" "docker/cloud-strategist.Dockerfile" "trading-cloud:latest"
  build_and_import_image "$NODE2_IP" "docker/hailo-worker.Dockerfile" "trading-hailo:latest"
fi

apply_manifests

if [[ "$RELOAD_MONITORING" == "true" ]]; then
  reload_monitoring
fi

if [[ "$RUN_SMOKE_TEST" == "true" ]]; then
  run_smoke_test
fi

if [[ "$RELOAD_MONITORING" == "true" ]]; then
  verify_monitoring
fi

show_status
ok "Hybrid cluster rollout completed."