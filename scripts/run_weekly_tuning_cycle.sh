#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON_CMD="$PYTHON_BIN"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_CMD="$ROOT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD="$(command -v python3)"
else
  echo "ERROR: No Python found. Set PYTHON_BIN or install python3." >&2
  exit 1
fi

TUNING_POLICY_FILE="${TUNING_POLICY_FILE:-$ROOT_DIR/tuning_policy.json}"
TUNING_STATUS_JSON="${TUNING_STATUS_JSON:-$ROOT_DIR/results/scorecards/latest_status.json}"
TUNING_ENV_FILE="${TUNING_ENV_FILE:-$ROOT_DIR/.env}"
TUNING_RECOMMENDER_STATE="${TUNING_RECOMMENDER_STATE:-$ROOT_DIR/results/scorecards/tuning_recommender_state.json}"
TUNING_RECOMMENDATION_JSON="${TUNING_RECOMMENDATION_JSON:-$ROOT_DIR/results/scorecards/latest_tuning_recommendation.json}"
TUNING_APPLY_SUMMARY_JSON="${TUNING_APPLY_SUMMARY_JSON:-$ROOT_DIR/results/scorecards/latest_tuning_apply_plan.json}"
TUNING_CYCLE_SUMMARY_JSON="${TUNING_CYCLE_SUMMARY_JSON:-$ROOT_DIR/results/scorecards/latest_tuning_cycle_summary.json}"
TUNING_BACKUP_DIR="${TUNING_BACKUP_DIR:-$ROOT_DIR/results/scorecards/env_backups}"
TUNING_APPLY_CHANGES="${TUNING_APPLY_CHANGES:-false}"
TUNING_REQUIRE_GO_VERDICT="${TUNING_REQUIRE_GO_VERDICT:-true}"
TUNING_BLOCK_ON_RECENT_NO_GO="${TUNING_BLOCK_ON_RECENT_NO_GO:-true}"
TUNING_RECENT_SCORECARD_LIMIT="${TUNING_RECENT_SCORECARD_LIMIT:-3}"
TUNING_SCORECARD_REPORTS_DIR="${TUNING_SCORECARD_REPORTS_DIR:-$ROOT_DIR/results/scorecards}"

TUNING_MAX_DRAWDOWN_PCT_LIMIT="${TUNING_MAX_DRAWDOWN_PCT_LIMIT:-15.0}"
mkdir -p "$(dirname "$TUNING_RECOMMENDATION_JSON")"
mkdir -p "$(dirname "$TUNING_APPLY_SUMMARY_JSON")"
mkdir -p "$(dirname "$TUNING_CYCLE_SUMMARY_JSON")"
mkdir -p "$TUNING_BACKUP_DIR"

echo "=== Weekly Tuning Cycle ==="
echo "Timestamp: $(date -Iseconds)"
echo "Python: $PYTHON_CMD"
echo "Policy: $TUNING_POLICY_FILE"
echo "Status JSON: $TUNING_STATUS_JSON"
echo "Env file: $TUNING_ENV_FILE"
echo "Apply changes: ${TUNING_APPLY_CHANGES,,}"
echo "Require GO verdict: ${TUNING_REQUIRE_GO_VERDICT,,}"
echo "Block on recent NO-GO: ${TUNING_BLOCK_ON_RECENT_NO_GO,,}"
echo "Recent scorecard limit: $TUNING_RECENT_SCORECARD_LIMIT"
echo
echo "Max drawdown pct limit: $TUNING_MAX_DRAWDOWN_PCT_LIMIT"

ARGS=(
  "$ROOT_DIR/scripts/run_tuning_cycle.py"
  --policy "$TUNING_POLICY_FILE"
  --status-json "$TUNING_STATUS_JSON"
  --env-file "$TUNING_ENV_FILE"
  --recommender-state "$TUNING_RECOMMENDER_STATE"
  --recommendation-json "$TUNING_RECOMMENDATION_JSON"
  --apply-summary-json "$TUNING_APPLY_SUMMARY_JSON"
  --cycle-summary-json "$TUNING_CYCLE_SUMMARY_JSON"
  --backup-dir "$TUNING_BACKUP_DIR"
  --recent-scorecard-limit "$TUNING_RECENT_SCORECARD_LIMIT"
  --scorecard-reports-dir "$TUNING_SCORECARD_REPORTS_DIR"
  --max-drawdown-pct-limit "$TUNING_MAX_DRAWDOWN_PCT_LIMIT"
)

if [[ "${TUNING_APPLY_CHANGES,,}" == "true" ]]; then
  ARGS+=(--apply)
fi

if [[ "${TUNING_REQUIRE_GO_VERDICT,,}" == "true" ]]; then
  ARGS+=(--require-go-verdict)
else
  ARGS+=(--no-require-go-verdict)
fi

if [[ "${TUNING_BLOCK_ON_RECENT_NO_GO,,}" == "true" ]]; then
  ARGS+=(--block-on-recent-no-go)
else
  ARGS+=(--no-block-on-recent-no-go)
fi

"$PYTHON_CMD" "${ARGS[@]}"
