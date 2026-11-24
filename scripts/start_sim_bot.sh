#!/usr/bin/env bash
set -euo pipefail

# Error handling with context-aware messages
trap 'echo "ERROR: Script failed at line $LINENO with exit code $?" >&2; exit 1' ERR

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Configuration with safe defaults
ENV_FILE="${ENV_FILE:-.env.simulation.example}"
PYTHON_BIN="${PYTHON_BIN:-}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/sim_bot.log}"
PID_FILE="${PID_FILE:-$LOG_DIR/sim_bot.pid}"
BOT_MODULE="src.trading_bot"

# Validate environment file
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: Environment file not found: $ENV_FILE" >&2
  exit 1
fi

# Load environment before resolving Python and PID/log paths.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# Resolve Python binary with fallbacks
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    echo "ERROR: No Python found. Install Python 3 or set PYTHON_BIN." >&2
    exit 1
  fi
fi

if [[ "$PYTHON_BIN" != */* ]]; then
  PYTHON_BIN="$(command -v -- "$PYTHON_BIN" 2>/dev/null || true)"
fi
if [[ -n "$PYTHON_BIN" && "$PYTHON_BIN" != /* ]]; then
  PYTHON_BIN="$ROOT_DIR/$PYTHON_BIN"
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "ERROR: Python binary not found or not executable: ${PYTHON_BIN:-<empty>}" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

process_start_ticks() {
  local pid="$1" stat_line rest
  local -a fields

  [[ -r "/proc/$pid/stat" ]] || return 1
  IFS= read -r stat_line < "/proc/$pid/stat" || return 1
  rest="${stat_line##*) }"
  read -r -a fields <<< "$rest"
  [[ "${#fields[@]}" -gt 19 && "${fields[19]}" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "${fields[19]}"
}

process_is_running() {
  local pid="$1" stat_line rest
  local -a fields

  [[ -r "/proc/$pid/stat" ]] || return 1
  IFS= read -r stat_line < "/proc/$pid/stat" || return 1
  rest="${stat_line##*) }"
  read -r -a fields <<< "$rest"
  [[ "${#fields[@]}" -gt 0 && "${fields[0]}" != "Z" && "${fields[0]}" != "X" ]]
}

remove_pid_file_if_ours() {
  local pid="$1" start_ticks="$2"
  local -a pid_record

  [[ -f "$PID_FILE" ]] || return 0
  mapfile -t pid_record < "$PID_FILE"
  if [[ "${#pid_record[@]}" -eq 3 \
    && "${pid_record[0]}" == "$pid" \
    && "${pid_record[1]}" == "$start_ticks" ]]; then
    rm -f "$PID_FILE"
  fi
}

is_bot_process() {
  local pid="$1" python_bin="${2:-$PYTHON_BIN}" process_uid
  local -a command

  [[ -r "/proc/$pid/cmdline" ]] || return 1
  process_uid="$(stat -c '%u' "/proc/$pid" 2>/dev/null)" || return 1
  [[ "$process_uid" == "$EUID" ]] || return 1
  mapfile -d '' -t command < "/proc/$pid/cmdline"
  [[ "${#command[@]}" -ge 3 \
    && "${command[0]}" == "$python_bin" \
    && "${command[1]}" == "-m" \
    && "${command[2]}" == "$BOT_MODULE" ]]
}

if [[ -f "$PID_FILE" ]]; then
  mapfile -t pid_record < "$PID_FILE"
  if [[ "${#pid_record[@]}" -ne 3 \
    || ! "${pid_record[0]}" =~ ^[1-9][0-9]*$ \
    || ! "${pid_record[1]}" =~ ^[0-9]+$ \
    || "${pid_record[2]}" != /* ]]; then
    echo "ERROR: Invalid or legacy PID file: $PID_FILE. Inspect it before restarting." >&2
    exit 1
  fi

  OLD_PID="${pid_record[0]}"
  OLD_START_TICKS="${pid_record[1]}"
  CURRENT_START_TICKS="$(process_start_ticks "$OLD_PID" 2>/dev/null || true)"
  if [[ -n "$CURRENT_START_TICKS" && "$CURRENT_START_TICKS" == "$OLD_START_TICKS" ]]; then
    if is_bot_process "$OLD_PID" "${pid_record[2]}"; then
      echo "INFO: Simulation bot already running (PID $OLD_PID)."
      echo "INFO: Log: $LOG_FILE"
      exit 0
    fi
    echo "ERROR: PID $OLD_PID has the recorded start time but is not the simulation bot; refusing to start another instance." >&2
    exit 1
  fi
  rm -f "$PID_FILE"
fi

# The bootstrap atomically records its PID before exec replaces it with the bot.
# If PID-file creation fails, the bot module is never started.
read -r -d '' LAUNCHER <<'PY' || true
import os
import sys

pid_file, python_bin = sys.argv[1:]
pid = os.getpid()
try:
    with open(f"/proc/{pid}/stat", encoding="ascii") as stat_file:
        start_ticks = stat_file.read().rsplit(")", 1)[1].split()[19]
    record = f"{pid}\n{start_ticks}\n{python_bin}\n"
    temporary = f"{pid_file}.tmp.{pid}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as pid_file_handle:
        pid_file_handle.write(record)
        pid_file_handle.flush()
        os.fsync(pid_file_handle.fileno())
    os.replace(temporary, pid_file)
except (OSError, IndexError, ValueError) as error:
    try:
        os.unlink(temporary)
    except (OSError, UnboundLocalError):
        pass
    print(f"ERROR: Could not create PID file {pid_file}: {error}", file=sys.stderr)
    raise SystemExit(1)

try:
    os.execv(python_bin, [python_bin, "-m", "src.trading_bot"])
except OSError as error:
    try:
        with open(pid_file, encoding="ascii") as current_file:
            if current_file.read() == record:
                os.unlink(pid_file)
    except FileNotFoundError:
        pass
    print(f"ERROR: Could not launch {python_bin} -m src.trading_bot: {error}", file=sys.stderr)
    raise SystemExit(1)
PY

nohup "$PYTHON_BIN" -c "$LAUNCHER" "$PID_FILE" "$PYTHON_BIN" > "$LOG_FILE" 2>&1 &
NEW_PID=$!

NEW_START_TICKS=""
PID_RECORD_READY=0
for _ in {1..20}; do
  if [[ -f "$PID_FILE" ]]; then
    mapfile -t pid_record < "$PID_FILE"
    if [[ "${#pid_record[@]}" -eq 3 \
      && "${pid_record[0]}" == "$NEW_PID" \
      && "${pid_record[1]}" =~ ^[0-9]+$ \
      && "${pid_record[2]}" == "$PYTHON_BIN" ]]; then
      NEW_START_TICKS="${pid_record[1]}"
      PID_RECORD_READY=1
    fi
  fi
  CURRENT_START_TICKS="$(process_start_ticks "$NEW_PID" 2>/dev/null || true)"
  if [[ "$PID_RECORD_READY" -eq 1 \
    && "$CURRENT_START_TICKS" == "$NEW_START_TICKS" ]] \
    && is_bot_process "$NEW_PID" "$PYTHON_BIN"; then
    break
  fi
  if ! kill -0 "$NEW_PID" 2>/dev/null; then
    START_STATUS=0
    wait "$NEW_PID" || START_STATUS=$?
    remove_pid_file_if_ours "$NEW_PID" "$NEW_START_TICKS"
    echo "ERROR: Simulation bot failed to start (exit code $START_STATUS). See log: $LOG_FILE" >&2
    exit 1
  fi
  if [[ "$PID_RECORD_READY" -eq 1 ]] && ! process_is_running "$NEW_PID"; then
    START_STATUS=0
    wait "$NEW_PID" || START_STATUS=$?
    remove_pid_file_if_ours "$NEW_PID" "$NEW_START_TICKS"
    echo "ERROR: Simulation bot failed to start (exit code $START_STATUS). See log: $LOG_FILE" >&2
    exit 1
  fi
  sleep 0.1
done

if [[ "$PID_RECORD_READY" -ne 1 \
  || "$(process_start_ticks "$NEW_PID" 2>/dev/null || true)" != "$NEW_START_TICKS" ]] \
  || ! is_bot_process "$NEW_PID" "$PYTHON_BIN"; then
  echo "ERROR: Could not verify the launched simulation bot (PID $NEW_PID); no success reported. See log: $LOG_FILE" >&2
  exit 1
fi

sleep 1
if [[ "$(process_start_ticks "$NEW_PID" 2>/dev/null || true)" != "$NEW_START_TICKS" ]] \
  || ! is_bot_process "$NEW_PID" "$PYTHON_BIN"; then
  START_STATUS=0
  if ! process_is_running "$NEW_PID"; then
    wait "$NEW_PID" 2>/dev/null || START_STATUS=$?
    remove_pid_file_if_ours "$NEW_PID" "$NEW_START_TICKS"
  fi
  echo "ERROR: Simulation bot exited or failed validation during the 1-second startup check (exit code $START_STATUS). See log: $LOG_FILE" >&2
  exit 1
fi

echo "INFO: Simulation bot started."
echo "INFO: PID: $NEW_PID"
echo "INFO: Log: $LOG_FILE"
echo "INFO: To stop, run: bash scripts/stop_sim_bot.sh"
