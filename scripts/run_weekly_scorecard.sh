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
MIN_CLOSED_TRADES="${MIN_CLOSED_TRADES:-}"
MIN_WIN_RATE="${MIN_WIN_RATE:-}"
MIN_PROFIT_FACTOR="${MIN_PROFIT_FACTOR:-}"
MIN_AVG_PNL="${MIN_AVG_PNL:-}"
MAX_DRAWDOWN_PCT="${MAX_DRAWDOWN_PCT:-}"
RECENT_TRADES_WINDOW="${RECENT_TRADES_WINDOW:-}"
MIN_RECENT_REALIZED_PNL="${MIN_RECENT_REALIZED_PNL:-}"
MIN_RECENT_WIN_RATE="${MIN_RECENT_WIN_RATE:-}"
MIN_CATBOOST_VS_RULES_PNL_DELTA="${MIN_CATBOOST_VS_RULES_PNL_DELTA:-}"
MIN_SOURCE_TRADES_FOR_DELTA="${MIN_SOURCE_TRADES_FOR_DELTA:-}"

# Go/No-Go gate profile for fast tuning of threshold strictness.
# Individual env vars still override profile defaults when set explicitly.
SCORECARD_GATES_PROFILE="${SCORECARD_GATES_PROFILE:-balanced}"
case "${SCORECARD_GATES_PROFILE,,}" in
  aggressive)
    DEFAULT_MIN_WIN_RATE="42"
    DEFAULT_MIN_PROFIT_FACTOR="1.05"
    DEFAULT_MIN_AVG_PNL="-0.005"
    DEFAULT_MAX_DRAWDOWN_PCT="12"
    DEFAULT_RECENT_TRADES_WINDOW="100"
    DEFAULT_MIN_RECENT_REALIZED_PNL="-0.10"
    DEFAULT_MIN_RECENT_WIN_RATE="42"
    DEFAULT_MIN_CATBOOST_VS_RULES_PNL_DELTA="-0.20"
    DEFAULT_MIN_SOURCE_TRADES_FOR_DELTA="30"
    ;;
  conservative)
    DEFAULT_MIN_WIN_RATE="50"
    DEFAULT_MIN_PROFIT_FACTOR="1.30"
    DEFAULT_MIN_AVG_PNL="0.0"
    DEFAULT_MAX_DRAWDOWN_PCT="8"
    DEFAULT_RECENT_TRADES_WINDOW="120"
    DEFAULT_MIN_RECENT_REALIZED_PNL="0.10"
    DEFAULT_MIN_RECENT_WIN_RATE="50"
    DEFAULT_MIN_CATBOOST_VS_RULES_PNL_DELTA="0.0"
    DEFAULT_MIN_SOURCE_TRADES_FOR_DELTA="80"
    ;;
  *)
    SCORECARD_GATES_PROFILE="balanced"
    DEFAULT_MIN_WIN_RATE="45"
    DEFAULT_MIN_PROFIT_FACTOR="1.20"
    DEFAULT_MIN_AVG_PNL="0.0"
    DEFAULT_MAX_DRAWDOWN_PCT="10"
    DEFAULT_RECENT_TRADES_WINDOW="100"
    DEFAULT_MIN_RECENT_REALIZED_PNL="0.0"
    DEFAULT_MIN_RECENT_WIN_RATE="45"
    DEFAULT_MIN_CATBOOST_VS_RULES_PNL_DELTA="-0.05"
    DEFAULT_MIN_SOURCE_TRADES_FOR_DELTA="50"
    ;;
esac

MIN_WIN_RATE="${MIN_WIN_RATE:-$DEFAULT_MIN_WIN_RATE}"
MIN_PROFIT_FACTOR="${MIN_PROFIT_FACTOR:-$DEFAULT_MIN_PROFIT_FACTOR}"
MIN_AVG_PNL="${MIN_AVG_PNL:-$DEFAULT_MIN_AVG_PNL}"
MAX_DRAWDOWN_PCT="${MAX_DRAWDOWN_PCT:-$DEFAULT_MAX_DRAWDOWN_PCT}"
RECENT_TRADES_WINDOW="${RECENT_TRADES_WINDOW:-$DEFAULT_RECENT_TRADES_WINDOW}"
MIN_RECENT_REALIZED_PNL="${MIN_RECENT_REALIZED_PNL:-$DEFAULT_MIN_RECENT_REALIZED_PNL}"
MIN_RECENT_WIN_RATE="${MIN_RECENT_WIN_RATE:-$DEFAULT_MIN_RECENT_WIN_RATE}"
MIN_CATBOOST_VS_RULES_PNL_DELTA="${MIN_CATBOOST_VS_RULES_PNL_DELTA:-$DEFAULT_MIN_CATBOOST_VS_RULES_PNL_DELTA}"
MIN_SOURCE_TRADES_FOR_DELTA="${MIN_SOURCE_TRADES_FOR_DELTA:-$DEFAULT_MIN_SOURCE_TRADES_FOR_DELTA}"

# Gate profile: use stricter defaults for live and friendlier defaults for paper/test.
# Explicit MIN_CLOSED_TRADES keeps highest priority for backward compatibility.
SCORECARD_PROFILE="${SCORECARD_PROFILE:-live}"
MIN_CLOSED_TRADES_LIVE="${MIN_CLOSED_TRADES_LIVE:-100}"
MIN_CLOSED_TRADES_TEST="${MIN_CLOSED_TRADES_TEST:-50}"
if [[ -z "${MIN_CLOSED_TRADES:-}" ]]; then
  case "${SCORECARD_PROFILE,,}" in
    paper|test|sim|simulation)
      MIN_CLOSED_TRADES="$MIN_CLOSED_TRADES_TEST"
      ;;
    *)
      MIN_CLOSED_TRADES="$MIN_CLOSED_TRADES_LIVE"
      ;;
  esac
fi

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
METRICS_JSON_FILE="$OUT_DIR/scorecard_metrics_${TS}.json"

mkdir -p "$OUT_DIR"

echo "=== Weekly Go/No-Go Scorecard ===" | tee "$OUT_FILE"
echo "Timestamp: $(date -Iseconds)" | tee -a "$OUT_FILE"
echo "Python: $PYTHON_CMD" | tee -a "$OUT_FILE"
echo "Journal: $JOURNAL_FILE" | tee -a "$OUT_FILE"
echo "SCORECARD_PROFILE=${SCORECARD_PROFILE,,}" | tee -a "$OUT_FILE"
echo "SCORECARD_GATES_PROFILE=${SCORECARD_GATES_PROFILE,,}" | tee -a "$OUT_FILE"
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
  --recent-trades-window "$RECENT_TRADES_WINDOW" \
  --min-recent-realized-pnl "$MIN_RECENT_REALIZED_PNL" \
  --min-recent-win-rate "$MIN_RECENT_WIN_RATE" \
  --min-catboost-vs-rules-pnl-delta "$MIN_CATBOOST_VS_RULES_PNL_DELTA" \
  --min-source-trades-for-delta "$MIN_SOURCE_TRADES_FOR_DELTA" \
  --metrics-json "$METRICS_JSON_FILE" \
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

PRIMARY_REASON="none"
if [[ "$VERDICT" != "GO" ]]; then
  PRIMARY_REASON="$(awk '/^Reason\(s\):/{in_reasons=1; next} in_reasons && /^- /{sub(/^- /, "", $0); print; exit} in_reasons && NF==0{exit}' "$OUT_FILE")"
  if [[ -z "$PRIMARY_REASON" ]]; then
    PRIMARY_REASON="unspecified"
  fi
fi
echo "Primary reason: $PRIMARY_REASON" | tee -a "$OUT_FILE"

METRICS_CLOSED_TRADES="0"
METRICS_WIN_RATE="0"
METRICS_REALIZED_PNL="0"
METRICS_AVG_PNL="0"
METRICS_PROFIT_FACTOR="0"
METRICS_MAX_DRAWDOWN_BASE="0"
METRICS_MAX_DRAWDOWN_PCT="0"
if [[ -f "$METRICS_JSON_FILE" ]]; then
  while IFS='=' read -r metric_key metric_value; do
    case "$metric_key" in
      METRICS_CLOSED_TRADES) METRICS_CLOSED_TRADES="$metric_value" ;;
      METRICS_WIN_RATE) METRICS_WIN_RATE="$metric_value" ;;
      METRICS_REALIZED_PNL) METRICS_REALIZED_PNL="$metric_value" ;;
      METRICS_AVG_PNL) METRICS_AVG_PNL="$metric_value" ;;
      METRICS_PROFIT_FACTOR) METRICS_PROFIT_FACTOR="$metric_value" ;;
      METRICS_MAX_DRAWDOWN_BASE) METRICS_MAX_DRAWDOWN_BASE="$metric_value" ;;
      METRICS_MAX_DRAWDOWN_PCT) METRICS_MAX_DRAWDOWN_PCT="$metric_value" ;;
    esac
  done < <(
    "$PYTHON_CMD" - "$METRICS_JSON_FILE" <<'PY'
import json
import math
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    payload = json.load(f)

metrics = payload.get("metrics", {})
mapping = {
    "METRICS_CLOSED_TRADES": int(metrics.get("closed_trades", 0)),
    "METRICS_WIN_RATE": float(metrics.get("win_rate", 0.0)),
    "METRICS_REALIZED_PNL": float(metrics.get("realized_pnl", 0.0)),
    "METRICS_AVG_PNL": float(metrics.get("avg_pnl", 0.0)),
    "METRICS_PROFIT_FACTOR": float(metrics.get("profit_factor", 0.0)),
    "METRICS_MAX_DRAWDOWN_BASE": float(metrics.get("max_drawdown_base", 0.0)),
    "METRICS_MAX_DRAWDOWN_PCT": float(metrics.get("max_drawdown_pct", 0.0)),
}

for key, value in mapping.items():
    if isinstance(value, float) and not math.isfinite(value):
        value = -1.0
    print(f"{key}={value}")
PY
  )
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
  PRIMARY_REASON="$PRIMARY_REASON" \
  UNDERLYING_EXIT_CODE="$RC" \
  FINAL_EXIT_CODE="$FINAL_RC" \
  REPORT_FILE="$OUT_FILE" \
  LATEST_REPORT_LINK="$LATEST_LINK" \
  METRICS_CLOSED_TRADES="$METRICS_CLOSED_TRADES" \
  METRICS_WIN_RATE="$METRICS_WIN_RATE" \
  METRICS_REALIZED_PNL="$METRICS_REALIZED_PNL" \
  METRICS_AVG_PNL="$METRICS_AVG_PNL" \
  METRICS_PROFIT_FACTOR="$METRICS_PROFIT_FACTOR" \
  METRICS_MAX_DRAWDOWN_BASE="$METRICS_MAX_DRAWDOWN_BASE" \
  METRICS_MAX_DRAWDOWN_PCT="$METRICS_MAX_DRAWDOWN_PCT" \
  STATUS_JSON_TMP_FILE="$STATUS_JSON_TMP_FILE" \
  "$PYTHON_CMD" - <<'PY'
import json
import os

payload = {
    "timestamp_utc": os.environ["TIMESTAMP_UTC"],
    "run_mode": os.environ["RUN_MODE"],
    "verdict": os.environ["VERDICT"],
    "primary_reason": os.environ["PRIMARY_REASON"],
    "underlying_exit_code": int(os.environ["UNDERLYING_EXIT_CODE"]),
    "final_exit_code": int(os.environ["FINAL_EXIT_CODE"]),
    "report_file": os.environ["REPORT_FILE"],
    "latest_report_link": os.environ["LATEST_REPORT_LINK"],
    "metrics": {
        "closed_trades": int(os.environ["METRICS_CLOSED_TRADES"]),
        "win_rate": float(os.environ["METRICS_WIN_RATE"]),
        "realized_pnl": float(os.environ["METRICS_REALIZED_PNL"]),
        "avg_pnl": float(os.environ["METRICS_AVG_PNL"]),
        "profit_factor": float(os.environ["METRICS_PROFIT_FACTOR"]),
        "max_drawdown_base": float(os.environ["METRICS_MAX_DRAWDOWN_BASE"]),
        "max_drawdown_pct": float(os.environ["METRICS_MAX_DRAWDOWN_PCT"]),
    },
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
  AI_STATE_FILE="${AI_COPILOT_STATE_FILE:-$ROOT_DIR/ai_copilot_state.json}"
  PORTFOLIO_STATE_FILE="${PORTFOLIO_STATE_FILE:-$ROOT_DIR/.portfolio_state.json}"
  BOT_LOG_FILE="${BOT_LOG_FILE:-$ROOT_DIR/logs/bot.log}"

  TS_UNIX="$(date -u +%s)"
  PROM_REASON_ESCAPED="${PRIMARY_REASON//\\/\\\\}"
  PROM_REASON_ESCAPED="${PROM_REASON_ESCAPED//\"/\\\"}"

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
    echo "# HELP trading_scorecard_fail_reason Current primary fail reason (labelled gauge set to 1 for active reason)"
    echo "# TYPE trading_scorecard_fail_reason gauge"
    echo "trading_scorecard_fail_reason{reason=\"$PROM_REASON_ESCAPED\"} 1"
    echo "# HELP trading_scorecard_closed_trades Number of closed trades in the evaluated window"
    echo "# TYPE trading_scorecard_closed_trades gauge"
    echo "trading_scorecard_closed_trades $METRICS_CLOSED_TRADES"
    echo "# HELP trading_scorecard_win_rate_percent Win rate in percent for closed trades"
    echo "# TYPE trading_scorecard_win_rate_percent gauge"
    echo "trading_scorecard_win_rate_percent $METRICS_WIN_RATE"
    echo "# HELP trading_scorecard_realized_pnl Realized PnL in base currency for the evaluated window"
    echo "# TYPE trading_scorecard_realized_pnl gauge"
    echo "trading_scorecard_realized_pnl $METRICS_REALIZED_PNL"
    echo "# HELP trading_scorecard_avg_pnl_per_sell Average realized PnL per sell in base currency"
    echo "# TYPE trading_scorecard_avg_pnl_per_sell gauge"
    echo "trading_scorecard_avg_pnl_per_sell $METRICS_AVG_PNL"
    echo "# HELP trading_scorecard_profit_factor Profit factor for the evaluated window; -1 means infinite because no losses occurred"
    echo "# TYPE trading_scorecard_profit_factor gauge"
    echo "trading_scorecard_profit_factor $METRICS_PROFIT_FACTOR"
    echo "# HELP trading_scorecard_max_drawdown_base Maximum realized drawdown in base currency"
    echo "# TYPE trading_scorecard_max_drawdown_base gauge"
    echo "trading_scorecard_max_drawdown_base $METRICS_MAX_DRAWDOWN_BASE"
    echo "# HELP trading_scorecard_max_drawdown_pct Maximum realized drawdown as percentage of starting capital"
    echo "# TYPE trading_scorecard_max_drawdown_pct gauge"
    echo "trading_scorecard_max_drawdown_pct $METRICS_MAX_DRAWDOWN_PCT"

    STATUS_TS_UNIX="$TS_UNIX" \
    SCORECARD_STARTING_CAPITAL="$STARTING_CAPITAL" \
    AI_STATE_FILE="$AI_STATE_FILE" \
    PORTFOLIO_STATE_FILE="$PORTFOLIO_STATE_FILE" \
    BOT_LOG_FILE="$BOT_LOG_FILE" \
    "$PYTHON_CMD" - <<'PY'
import json
import os
import re
from pathlib import Path


def _read_json(path: str):
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _as_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_latest_portfolio_value(log_path: str) -> float:
    file_path = Path(log_path)
    if not file_path.exists():
        return 0.0
    pattern = re.compile(r"Portfolio value:\s*([0-9]+(?:\.[0-9]+)?)")
    latest = 0.0
    try:
        with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                match = pattern.search(line)
                if match:
                    latest = _as_float(match.group(1), latest)
    except OSError:
        return 0.0
    return latest


def _read_portfolio_book_value(path: str) -> float:
    state = _read_json(path)
    cash = _as_float(state.get("cash"), 0.0)
    open_trades = state.get("open_trades") or {}
    invested_cost = 0.0
    if isinstance(open_trades, dict):
        for trade in open_trades.values():
            if isinstance(trade, dict):
                invested_cost += _as_float(trade.get("amount_base"), 0.0)
    return cash + invested_cost


def _read_portfolio_values_from_log(log_path: str):
    file_path = Path(log_path)
    if not file_path.exists():
        return 0.0, 0.0

    simulated_pattern = re.compile(r"Simulated starting capital:\s*([0-9]+(?:\.[0-9]+)?)")
    portfolio_pattern = re.compile(r"Portfolio value:\s*([0-9]+(?:\.[0-9]+)?)")
    marker_pattern = re.compile(r"Portfolio state loaded from|Portfolio initialized from exchange")

    session_start = 0.0
    latest_value = 0.0
    awaiting_first_value = False

    try:
        with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                simulated_match = simulated_pattern.search(line)
                if simulated_match:
                    session_start = _as_float(simulated_match.group(1), session_start)
                    awaiting_first_value = False
                    continue

                if marker_pattern.search(line):
                    session_start = 0.0
                    awaiting_first_value = True
                    continue

                portfolio_match = portfolio_pattern.search(line)
                if portfolio_match:
                    latest_value = _as_float(portfolio_match.group(1), latest_value)
                    if awaiting_first_value and session_start <= 0:
                        session_start = latest_value
                        awaiting_first_value = False
    except OSError:
        return 0.0, 0.0

    return session_start, latest_value


ai_state = _read_json(os.environ["AI_STATE_FILE"])
portfolio_state = _read_json(os.environ["PORTFOLIO_STATE_FILE"])
session_start_value, latest_portfolio_value = _read_portfolio_values_from_log(os.environ["BOT_LOG_FILE"])
portfolio_value = latest_portfolio_value
if portfolio_value <= 0:
    portfolio_value = _read_portfolio_book_value(os.environ["PORTFOLIO_STATE_FILE"])

starting_capital = _as_float(portfolio_state.get("initial_portfolio_value"), 0.0)
if starting_capital <= 0:
    starting_capital = session_start_value
if starting_capital <= 0:
    starting_capital = _as_float(os.environ.get("SCORECARD_STARTING_CAPITAL"), 0.0)
monthly_calls = int(_as_float(ai_state.get("monthly_calls"), 0.0))
daily_calls = int(_as_float(ai_state.get("daily_calls"), 0.0))
monthly_spend = _as_float(ai_state.get("monthly_spend_usd"), 0.0)
monthly_budget = _as_float(ai_state.get("budget_cap_usd"), 0.0)
budget_used_pct = 0.0
if monthly_budget > 0:
    budget_used_pct = (monthly_spend / monthly_budget) * 100.0

lines = [
    "# HELP trading_ai_copilot_daily_calls AI copilot calls used in the current day",
    "# TYPE trading_ai_copilot_daily_calls gauge",
    f"trading_ai_copilot_daily_calls {daily_calls}",
    "# HELP trading_ai_copilot_monthly_calls AI copilot calls used in the current month",
    "# TYPE trading_ai_copilot_monthly_calls gauge",
    f"trading_ai_copilot_monthly_calls {monthly_calls}",
    "# HELP trading_ai_copilot_monthly_spend_usd Estimated AI copilot spend in USD for the current month",
    "# TYPE trading_ai_copilot_monthly_spend_usd gauge",
    f"trading_ai_copilot_monthly_spend_usd {monthly_spend:.6f}",
    "# HELP trading_ai_copilot_budget_used_pct Estimated percentage of AI copilot monthly budget already used",
    "# TYPE trading_ai_copilot_budget_used_pct gauge",
    f"trading_ai_copilot_budget_used_pct {budget_used_pct:.4f}",
    "# HELP trading_runtime_portfolio_value Latest portfolio value seen by the trading bot",
    "# TYPE trading_runtime_portfolio_value gauge",
    f"trading_runtime_portfolio_value {portfolio_value:.6f}",
    "# HELP trading_runtime_portfolio_start_value Configured starting portfolio value used for scorecard evaluation",
    "# TYPE trading_runtime_portfolio_start_value gauge",
    f"trading_runtime_portfolio_start_value {starting_capital:.6f}",
]

print("\n".join(lines))
PY
  } > "$STATUS_PROM_TMP_FILE"

  mv "$STATUS_PROM_TMP_FILE" "$STATUS_PROM_FILE"
  echo "Status Prometheus file: $STATUS_PROM_FILE" | tee -a "$OUT_FILE"
fi

echo "Saved scorecard report: $OUT_FILE"
exit $FINAL_RC
