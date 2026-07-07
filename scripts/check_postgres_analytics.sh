#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${1:-trading-bot}"
POD_NAME="${2:-postgres-analytics-0}"

kubectl exec -n "$NAMESPACE" "$POD_NAME" -- sh -lc '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  echo "== data_directory =="
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select current_setting('"'"'data_directory'"'"');"
  echo
  echo "== table_counts =="
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -F $'"'"'\t'"'"' -Atc "
    select '"'"'trade_events'"'"', count(*), coalesce(to_char(max(event_ts), '"'"'YYYY-MM-DD HH24:MI:SSOF'"'"'), '"'"'never'"'"')
    from trading_analytics.trade_events
    union all
    select '"'"'portfolio_snapshots'"'"', count(*), coalesce(to_char(max(snapshot_ts), '"'"'YYYY-MM-DD HH24:MI:SSOF'"'"'), '"'"'never'"'"')
    from trading_analytics.portfolio_snapshots;
  "
'