#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TUNING_POLICY_FILE="${TUNING_POLICY_FILE:-$ROOT_DIR/tuning_policy.json}"
TUNING_STATUS_JSON="${TUNING_STATUS_JSON:-$ROOT_DIR/results/scorecards/latest_status.json}"
# The single-Pi profile keeps STATUS_JSON_ENABLED off, so the uppercase
# KEY=value marker is the scorecard evidence that actually exists on a Pi.
TUNING_STATUS_ENV="${TUNING_STATUS_ENV:-$ROOT_DIR/results/scorecards/latest_status.env}"
# Last evidence fallback and the source of the recent NO-GO guard.
TUNING_VERDICT_HISTORY_SETTING="${TUNING_VERDICT_HISTORY_FILE-${SCORECARD_VERDICT_HISTORY_FILE-$ROOT_DIR/results/scorecards/verdict_history.jsonl}}"
case "${TUNING_VERDICT_HISTORY_SETTING,,}" in
  ""|off|none|false|no|disabled) TUNING_VERDICT_HISTORY="" ;;
  /*) TUNING_VERDICT_HISTORY="$TUNING_VERDICT_HISTORY_SETTING" ;;
  *) TUNING_VERDICT_HISTORY="$ROOT_DIR/$TUNING_VERDICT_HISTORY_SETTING" ;;
esac
# One missed weekly scorecard cycle plus slack: an older verdict says nothing
# about the current market and must not license a parameter change.
TUNING_MAX_VERDICT_AGE_SECONDS="${TUNING_MAX_VERDICT_AGE_SECONDS:-777600}"
# Scheduled-task marker for this unit, read by src/task_status.py through
# TRADING_TASK_STATUS_FILES in deploy/trading-operational-health.service.
TUNING_STATUS_FILE="${TUNING_STATUS_FILE:-$ROOT_DIR/results/tuning/latest_status.env}"
TUNING_CYCLE_TIMEOUT_SECONDS="${TUNING_CYCLE_TIMEOUT_SECONDS:-1800}"
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
mkdir -p "$(dirname "$TUNING_STATUS_FILE")"

STARTED_UNIXTIME="$(date -u +%s)"
MARKER_PUBLISHED="false"
CYCLE_RC=0
ABORT_REASON=""

# One marker per attempt, success or failure, so the TradingScheduledTaskFailed
# / Stale / MarkerMissing alerts cover this timer too. Lowercase keys only: the
# gate contract lives in the scorecard marker, not here.
write_task_marker() {
  local finished_unixtime temporary_path
  finished_unixtime="$(date -u +%s)"
  temporary_path="${TUNING_STATUS_FILE}.tmp.$$"
  mkdir -p "$(dirname "$TUNING_STATUS_FILE")"
  {
    printf 'exit_code=%s\n' "$CYCLE_RC"
    printf 'started_unixtime=%s\n' "$STARTED_UNIXTIME"
    printf 'finished_unixtime=%s\n' "$finished_unixtime"
    printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'reason=%s\n' "$ABORT_REASON"
  } > "$temporary_path"
  chmod 0644 "$temporary_path" 2>/dev/null || true
  mv -f "$temporary_path" "$TUNING_STATUS_FILE"
  MARKER_PUBLISHED="true"
}

on_exit() {
  local code="$1"
  if [[ "$MARKER_PUBLISHED" == "true" ]]; then
    return 0
  fi
  CYCLE_RC="$code"
  [[ -z "$ABORT_REASON" ]] && ABORT_REASON="script_aborted"
  write_task_marker || true
}
trap 'on_exit $?' EXIT

# Resolved after the trap is installed, so a host without a usable interpreter
# still leaves a marker the operational-health exporter can alert on.
if [[ -n "${PYTHON_BIN:-}" ]]; then
  # An explicit override is authoritative: a broken path must fail loudly
  # instead of silently running against a different interpreter.
  if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "PYTHON_BIN is set but not executable: ${PYTHON_BIN}" >&2
    ABORT_REASON="python_not_found"
    exit 1
  fi
  PYTHON_CMD="$PYTHON_BIN"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_CMD="$ROOT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD="$(command -v python3)"
else
  echo "ERROR: No Python found. Set PYTHON_BIN or install python3." >&2
  ABORT_REASON="python_not_found"
  exit 1
fi

# A tuning pass that hangs (network, a stuck apply) must not hold the timer
# open forever and must still leave a marker behind.
TIMEOUT_CMD=()
if [[ "$TUNING_CYCLE_TIMEOUT_SECONDS" != "0" ]] && command -v timeout >/dev/null 2>&1; then
  TIMEOUT_CMD=(timeout --signal=TERM --kill-after=30s "$TUNING_CYCLE_TIMEOUT_SECONDS")
fi

echo "=== Weekly Tuning Cycle ==="
echo "Timestamp: $(date -Iseconds)"
echo "Python: $PYTHON_CMD"
echo "Policy: $TUNING_POLICY_FILE"
echo "Status JSON: $TUNING_STATUS_JSON"
echo "Status ENV: $TUNING_STATUS_ENV"
if [[ -n "$TUNING_VERDICT_HISTORY" ]]; then
  echo "Verdict history: $TUNING_VERDICT_HISTORY"
else
  echo "Verdict history: disabled"
fi
echo "Max verdict age (s): $TUNING_MAX_VERDICT_AGE_SECONDS"
echo "Task status file: $TUNING_STATUS_FILE"
echo "Cycle timeout (s): $TUNING_CYCLE_TIMEOUT_SECONDS"
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
  --status-env "$TUNING_STATUS_ENV"
  --verdict-history "${TUNING_VERDICT_HISTORY:-off}"
  --max-verdict-age-seconds "$TUNING_MAX_VERDICT_AGE_SECONDS"
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

# Exit codes are stable and documented in deploy/tuning-cycle.service: 0 ok,
# 3 blocked by the safety guard, anything else a failed step.
PYTHONPATH="$ROOT_DIR" ${TIMEOUT_CMD[@]+"${TIMEOUT_CMD[@]}"} "$PYTHON_CMD" "${ARGS[@]}" || CYCLE_RC=$?
ABORT_REASON="cycle_exit_${CYCLE_RC}"
write_task_marker
exit "$CYCLE_RC"
