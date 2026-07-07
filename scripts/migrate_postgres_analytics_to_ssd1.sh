#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-trading-bot}"
STATEFULSET_NAME="${STATEFULSET_NAME:-postgres-analytics}"
POD_NAME="${POD_NAME:-postgres-analytics-0}"
PVC_NAME="${PVC_NAME:-postgres-data-postgres-analytics-0}"
OVERLAY_PATH="${OVERLAY_PATH:-k8s/overlays/postgres-analytics}"
BACKUP_FILE="${BACKUP_FILE:-./trading_analytics_$(date +%Y%m%d_%H%M%S).dump}"
SKIP_RESTORE="${SKIP_RESTORE:-0}"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

info() {
  echo "INFO:  $*"
}

warn() {
  echo "WARN:  $*" >&2
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

confirm() {
  local prompt="$1"
  local reply
  read -r -p "$prompt [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" ]]
}

require_cmd kubectl

info "This migration recreates the analytics PVC so Postgres can bind to the master NVMe-backed StorageClass."
warn "Prerequisite on rasp1-node: /mnt/nvme_data/trading_db must already exist."

confirm "Continue with backup and PVC recreation?" || die "Aborted by user."

info "Checking current Postgres pod..."
kubectl -n "$NAMESPACE" get pod "$POD_NAME" >/dev/null

info "Creating logical backup at $BACKUP_FILE ..."
kubectl -n "$NAMESPACE" exec "$POD_NAME" -- sh -lc '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc
' > "$BACKUP_FILE"

[[ -s "$BACKUP_FILE" ]] || die "Backup file is empty: $BACKUP_FILE"

info "Scaling down StatefulSet..."
kubectl -n "$NAMESPACE" scale statefulset "$STATEFULSET_NAME" --replicas=0

info "Waiting for pod termination..."
kubectl -n "$NAMESPACE" wait --for=delete "pod/$POD_NAME" --timeout=180s || true

warn "Deleting StatefulSet and PVC so the new NVMe-backed storage class can be used."
kubectl -n "$NAMESPACE" delete statefulset "$STATEFULSET_NAME" --ignore-not-found
kubectl -n "$NAMESPACE" delete pvc "$PVC_NAME" --ignore-not-found

info "Re-applying Postgres analytics overlay..."
kubectl apply -k "$OVERLAY_PATH"

info "Waiting for recreated Postgres pod..."
kubectl -n "$NAMESPACE" rollout status "statefulset/$STATEFULSET_NAME" --timeout=300s
kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/$POD_NAME" --timeout=300s

if [[ "$SKIP_RESTORE" != "1" ]]; then
  info "Copying backup into pod..."
  kubectl -n "$NAMESPACE" cp "$BACKUP_FILE" "$POD_NAME:/tmp/trading_analytics.dump"

  info "Restoring backup into recreated database..."
  kubectl -n "$NAMESPACE" exec "$POD_NAME" -- sh -lc '
    export PGPASSWORD="$POSTGRES_PASSWORD"
    pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists /tmp/trading_analytics.dump
  '
else
  warn "SKIP_RESTORE=1 set, skipping pg_restore."
fi

info "Post-migration verification:"
bash ./scripts/check_postgres_analytics.sh "$NAMESPACE" "$POD_NAME"

info "Migration complete. Backup retained at $BACKUP_FILE"