#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Python binary selection (override with PYTHON_BIN)
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

# Tunables (override via environment variables)
JOURNAL_FILE="${JOURNAL_FILE:-$ROOT_DIR/trade_journal.csv}"
LOOKBACK_DAYS="${LOOKBACK_DAYS:-7}"
STARTING_CAPITAL="${STARTING_CAPITAL:-20}"
MIN_CLOSED_TRADES="${MIN_CLOSED_TRADES:-200}"
MIN_WIN_RATE="${MIN_WIN_RATE:-45}"
MIN_PROFIT_FACTOR="${MIN_PROFIT_FACTOR:-1.2}"
MIN_AVG_PNL="${MIN_AVG_PNL:-0.0}"
MAX_DRAWDOWN_PCT="${MAX_DRAWDOWN_PCT:-10}"

# Optional AutoResearch refresh before scorecard/model usage.
AUTORESEARCH_ENABLED="${AUTORESEARCH_ENABLED:-false}"
AUTORESEARCH_REQUIRED="${AUTORESEARCH_REQUIRED:-false}"
AUTORESEARCH_OUTPUT_PATH="${AUTORESEARCH_OUTPUT_PATH:-data/research_signal_latest.json}"
AUTORESEARCH_SOURCE_PATH="${AUTORESEARCH_SOURCE_PATH:-}"
AUTORESEARCH_CMD="${AUTORESEARCH_CMD:-}"
AUTORESEARCH_MAX_AGE_MINUTES="${AUTORESEARCH_MAX_AGE_MINUTES:-180}"
AUTORESEARCH_ALLOW_STALE="${AUTORESEARCH_ALLOW_STALE:-false}"
AUTORESEARCH_WRITE_NEUTRAL_FALLBACK="${AUTORESEARCH_WRITE_NEUTRAL_FALLBACK:-true}"
AUTORESEARCH_PRECHECK="${AUTORESEARCH_PRECHECK:-true}"
AUTORESEARCH_PRECHECK_DRY_RUN="${AUTORESEARCH_PRECHECK_DRY_RUN:-false}"
AUTORESEARCH_STRICT_ALLOW_MAINTENANCE_FALLBACK="${AUTORESEARCH_STRICT_ALLOW_MAINTENANCE_FALLBACK:-false}"
AUTORESEARCH_MAINTENANCE_EXIT_CODE="${AUTORESEARCH_MAINTENANCE_EXIT_CODE:-4}"

AR_REQUIRED_EFFECTIVE="${AUTORESEARCH_REQUIRED,,}"
AR_FALLBACK_EFFECTIVE="${AUTORESEARCH_WRITE_NEUTRAL_FALLBACK,,}"
AR_MAINTENANCE_OVERRIDE_USED="false"

TS="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/results/scorecards}"
OUT_FILE="$OUT_DIR/scorecard_${TS}.txt"
LATEST_LINK="$OUT_DIR/latest_scorecard.txt"
STATUS_FILE="${STATUS_FILE:-$OUT_DIR/latest_status.env}"
STATUS_JSON_ENABLED="${STATUS_JSON_ENABLED:-false}"
STATUS_JSON_FILE="${STATUS_JSON_FILE:-$OUT_DIR/latest_status.json}"
STATUS_PROM_ENABLED="${STATUS_PROM_ENABLED:-false}"
STATUS_PROM_FILE="${STATUS_PROM_FILE:-$OUT_DIR/latest_status.prom}"

mkdir -p "$OUT_DIR"

echo "=== Weekly Go/No-Go Scorecard ===" | tee "$OUT_FILE"
echo "Timestamp: $(date -Iseconds)" | tee -a "$OUT_FILE"
echo "Python: $PYTHON_CMD" | tee -a "$OUT_FILE"
echo "Journal: $JOURNAL_FILE" | tee -a "$OUT_FILE"
echo "RUN_MODE=standard" | tee -a "$OUT_FILE"
echo "AUTORESEARCH_ENABLED=${AUTORESEARCH_ENABLED,,}" | tee -a "$OUT_FILE"
echo | tee -a "$OUT_FILE"

if [[ "${AUTORESEARCH_ENABLED,,}" == "true" ]]; then
  if [[ "${AUTORESEARCH_PRECHECK,,}" == "true" ]]; then
    AR_PRECHECK_DRY_RUN_EFFECTIVE="${AUTORESEARCH_PRECHECK_DRY_RUN,,}"
    if [[ "${AUTORESEARCH_REQUIRED,,}" == "true" && "$AR_PRECHECK_DRY_RUN_EFFECTIVE" != "true" ]]; then
      AR_PRECHECK_DRY_RUN_EFFECTIVE="true"
      echo "Info: Enforcing AUTORESEARCH_PRECHECK_DRY_RUN=true because AUTORESEARCH_REQUIRED=true." | tee -a "$OUT_FILE"
    fi

    echo "Running AutoResearch precheck..." | tee -a "$OUT_FILE"
    set +e
    AUTORESEARCH_PRECHECK_DRY_RUN="$AR_PRECHECK_DRY_RUN_EFFECTIVE" \
      "$ROOT_DIR/scripts/check_autoresearch_setup.sh" | tee -a "$OUT_FILE"
    AR_CHECK_RC=$?
    set -e

    if [[ $AR_CHECK_RC -ne 0 ]]; then
      if [[ "${AUTORESEARCH_REQUIRED,,}" == "true" ]]; then
        if [[ "${AUTORESEARCH_STRICT_ALLOW_MAINTENANCE_FALLBACK,,}" == "true" ]]; then
          echo "WARNING: Strict precheck failed. Maintenance fallback override is enabled." | tee -a "$OUT_FILE"
          echo "WARNING: For this run, forcing fallback signal and disabling required hard-fail." | tee -a "$OUT_FILE"
          AR_REQUIRED_EFFECTIVE="false"
          AR_FALLBACK_EFFECTIVE="true"
          AR_MAINTENANCE_OVERRIDE_USED="true"
        else
          echo "ERROR: AutoResearch precheck failed (required)." | tee -a "$OUT_FILE"
          exit 1
        fi
      else
        echo "WARNING: AutoResearch precheck failed, continuing due to non-required mode." | tee -a "$OUT_FILE"
      fi
    fi
    echo | tee -a "$OUT_FILE"
  fi

  echo "Running AutoResearch bridge..." | tee -a "$OUT_FILE"

  AR_ARGS=(
    "$ROOT_DIR/scripts/update_autoresearch_signal.py"
    --output "$AUTORESEARCH_OUTPUT_PATH"
    --max-age-minutes "$AUTORESEARCH_MAX_AGE_MINUTES"
  )

  if [[ -n "$AUTORESEARCH_CMD" ]]; then
    AR_ARGS+=(--command "$AUTORESEARCH_CMD")
  fi
  if [[ -n "$AUTORESEARCH_SOURCE_PATH" ]]; then
    AR_ARGS+=(--source "$AUTORESEARCH_SOURCE_PATH")
  fi
  if [[ "${AUTORESEARCH_ALLOW_STALE,,}" == "true" ]]; then
    AR_ARGS+=(--allow-stale)
  fi
  if [[ "$AR_FALLBACK_EFFECTIVE" == "true" ]]; then
    AR_ARGS+=(--fallback-neutral)
  fi

  set +e
  "$PYTHON_CMD" "${AR_ARGS[@]}" | tee -a "$OUT_FILE"
  AR_RC=$?
  set -e

  if [[ $AR_RC -ne 0 ]]; then
    if [[ "$AR_REQUIRED_EFFECTIVE" == "true" ]]; then
      echo "ERROR: AutoResearch bridge failed (required)." | tee -a "$OUT_FILE"
      exit 1
    fi
    echo "WARNING: AutoResearch bridge failed, continuing with existing/neutral research signal." | tee -a "$OUT_FILE"
  fi

  echo | tee -a "$OUT_FILE"
fi

set +e
"$PYTHON_CMD" "$ROOT_DIR/go_no_go_scorecard.py" \
  --file "$JOURNAL_FILE" \
  --lookback-days "$LOOKBACK_DAYS" \
  --starting-capital "$STARTING_CAPITAL" \
  --min-closed-trades "$MIN_CLOSED_TRADES" \
  --min-win-rate "$MIN_WIN_RATE" \
  --min-profit-factor "$MIN_PROFIT_FACTOR" \
  --min-avg-pnl "$MIN_AVG_PNL" \
  --max-drawdown-pct "$MAX_DRAWDOWN_PCT" \
  | tee -a "$OUT_FILE"
RC=$?
set -e

FINAL_RC="$RC"
if [[ "$AR_MAINTENANCE_OVERRIDE_USED" == "true" ]]; then
  # Always mark maintenance-override runs with a dedicated code for monitoring.
  FINAL_RC="$AUTORESEARCH_MAINTENANCE_EXIT_CODE"
fi

ln -sfn "$(basename "$OUT_FILE")" "$LATEST_LINK"

echo | tee -a "$OUT_FILE"
VERDICT="ERROR"
if [[ $RC -eq 0 ]]; then
  VERDICT="GO"
  echo "Verdict summary: GO" | tee -a "$OUT_FILE"
elif [[ $RC -eq 2 ]]; then
  VERDICT="HOLD"
  echo "Verdict summary: HOLD" | tee -a "$OUT_FILE"
elif [[ $RC -eq 3 ]]; then
  VERDICT="NO-GO"
  echo "Verdict summary: NO-GO" | tee -a "$OUT_FILE"
else
  echo "Verdict summary: ERROR (exit code $RC)" | tee -a "$OUT_FILE"
fi

RUN_MODE="standard"
if [[ "$AR_MAINTENANCE_OVERRIDE_USED" == "true" ]]; then
  RUN_MODE="maintenance_override"
  echo "RUN_MODE=maintenance_override" | tee -a "$OUT_FILE"
  echo "Maintenance mode: ACTIVE (strict precheck override used)" | tee -a "$OUT_FILE"
  echo "Underlying scorecard exit code: $RC" | tee -a "$OUT_FILE"
  echo "Exit code override: $FINAL_RC" | tee -a "$OUT_FILE"
  echo "UNDERLYING_EXIT_CODE=$RC" | tee -a "$OUT_FILE"
  echo "FINAL_EXIT_CODE=$FINAL_RC" | tee -a "$OUT_FILE"
else
  echo "RUN_MODE=standard" | tee -a "$OUT_FILE"
  echo "FINAL_EXIT_CODE=$FINAL_RC" | tee -a "$OUT_FILE"
fi

STATUS_TMP_FILE="${STATUS_FILE}.tmp"
{
  echo "TIMESTAMP_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "RUN_MODE=$RUN_MODE"
  echo "VERDICT=$VERDICT"
  echo "UNDERLYING_EXIT_CODE=$RC"
  echo "FINAL_EXIT_CODE=$FINAL_RC"
  echo "REPORT_FILE=$OUT_FILE"
  echo "LATEST_REPORT_LINK=$LATEST_LINK"
} > "$STATUS_TMP_FILE"
mv "$STATUS_TMP_FILE" "$STATUS_FILE"
echo "Status file: $STATUS_FILE" | tee -a "$OUT_FILE"

if [[ "${STATUS_JSON_ENABLED,,}" == "true" ]]; then
  STATUS_JSON_TMP_FILE="${STATUS_JSON_FILE}.tmp"
  STATUS_JSON_DIR="$(dirname "$STATUS_JSON_FILE")"
  mkdir -p "$STATUS_JSON_DIR"

  TIMESTAMP_UTC_VALUE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  TIMESTAMP_UTC="$TIMESTAMP_UTC_VALUE" \
  RUN_MODE="$RUN_MODE" \
  VERDICT="$VERDICT" \
  UNDERLYING_EXIT_CODE="$RC" \
  FINAL_EXIT_CODE="$FINAL_RC" \
  REPORT_FILE="$OUT_FILE" \
  LATEST_REPORT_LINK="$LATEST_LINK" \
  STATUS_JSON_TMP_FILE="$STATUS_JSON_TMP_FILE" \
  "$PYTHON_CMD" - <<'PY'
import json
import os

payload = {
    "timestamp_utc": os.environ["TIMESTAMP_UTC"],
    "run_mode": os.environ["RUN_MODE"],
    "verdict": os.environ["VERDICT"],
    "underlying_exit_code": int(os.environ["UNDERLYING_EXIT_CODE"]),
    "final_exit_code": int(os.environ["FINAL_EXIT_CODE"]),
    "report_file": os.environ["REPORT_FILE"],
    "latest_report_link": os.environ["LATEST_REPORT_LINK"],
}

with open(os.environ["STATUS_JSON_TMP_FILE"], "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=True, indent=2)
PY

  mv "$STATUS_JSON_TMP_FILE" "$STATUS_JSON_FILE"
  echo "Status JSON file: $STATUS_JSON_FILE" | tee -a "$OUT_FILE"
fi

if [[ "${STATUS_PROM_ENABLED,,}" == "true" ]]; then
  STATUS_PROM_TMP_FILE="${STATUS_PROM_FILE}.tmp"
  STATUS_PROM_DIR="$(dirname "$STATUS_PROM_FILE")"
  mkdir -p "$STATUS_PROM_DIR"

  TS_UNIX="$(date -u +%s)"

  {
    echo "# HELP trading_scorecard_final_exit_code Final process exit code returned by run_weekly_scorecard.sh"
    echo "# TYPE trading_scorecard_final_exit_code gauge"
    echo "trading_scorecard_final_exit_code $FINAL_RC"
    echo "# HELP trading_scorecard_underlying_exit_code Raw scorecard/go_no_go exit code before overrides"
    echo "# TYPE trading_scorecard_underlying_exit_code gauge"
    echo "trading_scorecard_underlying_exit_code $RC"
    echo "# HELP trading_scorecard_timestamp_seconds Unix timestamp when status file was generated"
    echo "# TYPE trading_scorecard_timestamp_seconds gauge"
    echo "trading_scorecard_timestamp_seconds $TS_UNIX"
    echo "# HELP trading_scorecard_run_mode Current run mode (labelled gauge set to 1 for active mode)"
    echo "# TYPE trading_scorecard_run_mode gauge"
    echo "trading_scorecard_run_mode{mode=\"$RUN_MODE\"} 1"
    echo "# HELP trading_scorecard_verdict Current scorecard verdict (labelled gauge set to 1 for active verdict)"
    echo "# TYPE trading_scorecard_verdict gauge"
    echo "trading_scorecard_verdict{verdict=\"$VERDICT\"} 1"
  } > "$STATUS_PROM_TMP_FILE"

  mv "$STATUS_PROM_TMP_FILE" "$STATUS_PROM_FILE"
  echo "Status Prometheus file: $STATUS_PROM_FILE" | tee -a "$OUT_FILE"
fi

echo "Saved scorecard report: $OUT_FILE"
exit $FINAL_RC
