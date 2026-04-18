#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

AUTORESEARCH_ENABLED="${AUTORESEARCH_ENABLED:-false}"
AUTORESEARCH_REQUIRED="${AUTORESEARCH_REQUIRED:-false}"
AUTORESEARCH_REPO_PATH="${AUTORESEARCH_REPO_PATH:-}"
AUTORESEARCH_CMD="${AUTORESEARCH_CMD:-}"
AUTORESEARCH_SOURCE_PATH="${AUTORESEARCH_SOURCE_PATH:-}"
AUTORESEARCH_OUTPUT_PATH="${AUTORESEARCH_OUTPUT_PATH:-data/research_signal_latest.json}"
AUTORESEARCH_MAX_AGE_MINUTES="${AUTORESEARCH_MAX_AGE_MINUTES:-180}"
AUTORESEARCH_WRITE_NEUTRAL_FALLBACK="${AUTORESEARCH_WRITE_NEUTRAL_FALLBACK:-true}"
AUTORESEARCH_PRECHECK_DRY_RUN="${AUTORESEARCH_PRECHECK_DRY_RUN:-false}"
AUTORESEARCH_PRECHECK_TIMEOUT_SEC="${AUTORESEARCH_PRECHECK_TIMEOUT_SEC:-90}"
PYTHON_BIN="${PYTHON_BIN:-}"

to_lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

resolve_path() {
  local p="$1"
  if [[ -z "$p" ]]; then
    printf ''
    return
  fi
  if [[ "$p" = /* ]]; then
    printf '%s' "$p"
  else
    printf '%s/%s' "$ROOT_DIR" "$p"
  fi
}

resolve_python() {
  if [[ -n "$PYTHON_BIN" ]]; then
    printf '%s' "$PYTHON_BIN"
    return
  fi
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    printf '%s' "$ROOT_DIR/.venv/bin/python"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  printf ''
}

enabled_lc="$(to_lower "$AUTORESEARCH_ENABLED")"
required_lc="$(to_lower "$AUTORESEARCH_REQUIRED")"
fallback_lc="$(to_lower "$AUTORESEARCH_WRITE_NEUTRAL_FALLBACK")"
dry_run_lc="$(to_lower "$AUTORESEARCH_PRECHECK_DRY_RUN")"

if [[ "$enabled_lc" != "true" ]]; then
  echo "AutoResearch precheck: skipped (AUTORESEARCH_ENABLED=$AUTORESEARCH_ENABLED)"
  exit 0
fi

declare -a ERRORS=()
declare -a WARNINGS=()

repo_abs="$(resolve_path "$AUTORESEARCH_REPO_PATH")"
source_abs="$(resolve_path "$AUTORESEARCH_SOURCE_PATH")"
output_abs="$(resolve_path "$AUTORESEARCH_OUTPUT_PATH")"

if [[ -n "$AUTORESEARCH_REPO_PATH" && ! -d "$repo_abs" ]]; then
  ERRORS+=("AUTORESEARCH_REPO_PATH does not exist: $repo_abs")
fi

if [[ -n "$AUTORESEARCH_CMD" ]]; then
  cmd_head="${AUTORESEARCH_CMD%% *}"
  cmd_head="${cmd_head#\"}"
  cmd_head="${cmd_head%\"}"

  if [[ "$cmd_head" == */* ]]; then
    if [[ ! -x "$cmd_head" ]]; then
      WARNINGS+=("Command starts with non-executable path: $cmd_head")
    fi
  else
    if ! command -v "$cmd_head" >/dev/null 2>&1; then
      WARNINGS+=("Command binary not found in PATH: $cmd_head")
    fi
  fi
fi

if [[ -z "$AUTORESEARCH_CMD" ]]; then
  if [[ -z "$AUTORESEARCH_SOURCE_PATH" ]]; then
    if [[ "$required_lc" == "true" && "$fallback_lc" != "true" ]]; then
      ERRORS+=("No AUTORESEARCH_CMD and no AUTORESEARCH_SOURCE_PATH in strict required mode.")
    else
      WARNINGS+=("No AUTORESEARCH_CMD and no AUTORESEARCH_SOURCE_PATH; bridge will rely on fallback/discovery.")
    fi
  elif [[ ! -f "$source_abs" ]]; then
    if [[ "$fallback_lc" == "true" ]]; then
      WARNINGS+=("AUTORESEARCH_SOURCE_PATH not found now (will fallback if run fails): $source_abs")
    else
      ERRORS+=("AUTORESEARCH_SOURCE_PATH not found: $source_abs")
    fi
  fi
fi

output_dir="$(dirname "$output_abs")"
if ! mkdir -p "$output_dir" 2>/dev/null; then
  ERRORS+=("Cannot create output directory: $output_dir")
fi

if [[ -n "$AUTORESEARCH_SOURCE_PATH" ]]; then
  source_dir="$(dirname "$source_abs")"
  if [[ ! -d "$source_dir" ]]; then
    if ! mkdir -p "$source_dir" 2>/dev/null; then
      WARNINGS+=("Cannot create source directory now: $source_dir")
    fi
  fi
fi

echo "AutoResearch precheck summary"
echo "- enabled: $AUTORESEARCH_ENABLED"
echo "- required: $AUTORESEARCH_REQUIRED"
echo "- repo: ${repo_abs:-<unset>}"
echo "- output: $output_abs"

if [[ ${#WARNINGS[@]} -gt 0 ]]; then
  echo "Warnings:"
  for w in "${WARNINGS[@]}"; do
    echo "- $w"
  done
fi

if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo "Errors:"
  for e in "${ERRORS[@]}"; do
    echo "- $e"
  done
  exit 1
fi

if [[ "$dry_run_lc" == "true" ]]; then
  python_cmd="$(resolve_python)"
  if [[ -z "$python_cmd" ]]; then
    echo "Errors:"
    echo "- Python interpreter not found for dry-run precheck. Set PYTHON_BIN or install python3."
    exit 1
  fi

  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' EXIT
  tmp_output="$tmp_dir/research_signal_precheck.json"

  bridge_args=(
    "$ROOT_DIR/scripts/update_autoresearch_signal.py"
    --output "$tmp_output"
    --max-age-minutes "$AUTORESEARCH_MAX_AGE_MINUTES"
    --allow-stale
  )

  if [[ -n "$AUTORESEARCH_REPO_PATH" ]]; then
    bridge_args+=(--repo-path "$AUTORESEARCH_REPO_PATH")
  fi
  if [[ -n "$AUTORESEARCH_CMD" ]]; then
    bridge_args+=(--command "$AUTORESEARCH_CMD")
  fi
  if [[ -n "$AUTORESEARCH_SOURCE_PATH" ]]; then
    bridge_args+=(--source "$AUTORESEARCH_SOURCE_PATH")
  fi

  echo "Running AutoResearch dry-run precheck..."
  if command -v timeout >/dev/null 2>&1; then
    AUTORESEARCH_WRITE_NEUTRAL_FALLBACK=false timeout "${AUTORESEARCH_PRECHECK_TIMEOUT_SEC}s" \
      "$python_cmd" "${bridge_args[@]}" >/dev/null
  else
    AUTORESEARCH_WRITE_NEUTRAL_FALLBACK=false "$python_cmd" "${bridge_args[@]}" >/dev/null
  fi

  "$python_cmd" - "$tmp_output" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    payload = json.load(f)

required_top = [
    "timestamp_utc",
    "sentiment_score",
    "confidence",
    "risk_score",
    "market_regime",
    "normalized_features",
]
missing_top = [k for k in required_top if k not in payload]
if missing_top:
    raise SystemExit(f"Missing top-level keys: {missing_top}")

nf = payload.get("normalized_features", {})
required_nf = [
    "research_sentiment_score",
    "research_confidence",
    "research_risk_score",
    "research_regime_bull",
    "research_regime_bear",
    "research_regime_sideways",
]
missing_nf = [k for k in required_nf if k not in nf]
if missing_nf:
    raise SystemExit(f"Missing normalized feature keys: {missing_nf}")
PY

  echo "AutoResearch dry-run precheck passed"
fi

echo "AutoResearch precheck passed"
