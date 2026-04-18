#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COLLECTOR_DIR="${COLLECTOR_DIR:-./results/scorecards/textfile}"
PROM_FILE="${STATUS_PROM_FILE:-$COLLECTOR_DIR/trading_scorecard.prom}"
NODE_EXPORTER_CMD="${NODE_EXPORTER_CMD:-node_exporter}"

mkdir -p "$COLLECTOR_DIR"

# Generate/update scorecard metrics once before starting node_exporter.
STATUS_PROM_ENABLED=true \
STATUS_PROM_FILE="$PROM_FILE" \
bash "$ROOT_DIR/scripts/run_weekly_scorecard.sh" || true

echo "Starting node_exporter with textfile collector"
echo "Collector dir: $COLLECTOR_DIR"
echo "Metric file:    $PROM_FILE"

exec "$NODE_EXPORTER_CMD" \
  --collector.textfile \
  --collector.textfile.directory="$COLLECTOR_DIR"
