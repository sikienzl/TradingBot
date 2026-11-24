#!/usr/bin/env bash
set -euo pipefail

# Error handling
trap 'echo "ERROR: Script failed at line $LINENO with exit code $?" >&2; exit 1' ERR

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs}"
PID_FILE="${PID_FILE:-$LOG_DIR/sim_bot.pid}"
BOT_MODULE="src.trading_bot"

if [[ ! -e "$PID_FILE" ]]; then
  echo "INFO: Simulation bot is not running."
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required to safely validate and stop the recorded process." >&2
  exit 1
fi

python3 - "$PID_FILE" "$BOT_MODULE" <<'PY'
import errno
import os
import select
import signal
import sys

pid_file, bot_module = sys.argv[1:]

try:
    record = open(pid_file, encoding="ascii").read().splitlines()
except FileNotFoundError:
    print("INFO: Simulation bot is not running.")
    raise SystemExit(0)
except (OSError, UnicodeError) as error:
    print(f"ERROR: Cannot read PID file {pid_file}: {error}", file=sys.stderr)
    raise SystemExit(1)

if (
    len(record) != 3
    or not record[0].isdigit()
    or int(record[0]) <= 1
    or not record[1].isdigit()
    or not record[2].startswith("/")
):
    print(
        f"ERROR: Invalid or legacy PID file {pid_file}; refusing to signal any process.",
        file=sys.stderr,
    )
    raise SystemExit(1)

pid, expected_start_ticks, python_bin = int(record[0]), record[1], record[2]

def remove_unchanged_pid_file():
    try:
        with open(pid_file, encoding="ascii") as current_file:
            if current_file.read().splitlines() == record:
                os.unlink(pid_file)
    except FileNotFoundError:
        pass
    except OSError as error:
        print(f"ERROR: Cannot remove stale PID file {pid_file}: {error}", file=sys.stderr)
        raise SystemExit(1)

# pidfd_open needs Linux 5.3+; Python exposes both pidfd APIs starting with 3.9.
if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
    print(
        "ERROR: Safe stopping requires Linux 5.3+ pidfd support and Python 3.9+ with os.pidfd_open/signal.pidfd_send_signal.",
        file=sys.stderr,
    )
    raise SystemExit(1)

try:
    pidfd = os.pidfd_open(pid)
except ProcessLookupError:
    remove_unchanged_pid_file()
    print("INFO: Simulation bot is not running (stale PID file removed).")
    raise SystemExit(0)
except (AttributeError, OSError) as error:
    print(f"ERROR: Cannot safely open process {pid}: {error}", file=sys.stderr)
    raise SystemExit(1)

with os.fdopen(pidfd, "rb", closefd=True) as process_fd:
    poller = select.poll()
    poller.register(process_fd, select.POLLIN)
    if poller.poll(0):
        remove_unchanged_pid_file()
        print("INFO: Simulation bot is not running (stale PID file removed).")
        raise SystemExit(0)

    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as stat_file:
            stat_fields = stat_file.read().rsplit(")", 1)[1].split()
        with open(f"/proc/{pid}/cmdline", "rb") as command_file:
            command = command_file.read().rstrip(b"\0").split(b"\0")
        process_uid = os.stat(f"/proc/{pid}").st_uid
    except (OSError, IndexError) as error:
        print(f"ERROR: Cannot validate recorded process {pid}: {error}", file=sys.stderr)
        raise SystemExit(1)

    actual_start_ticks = stat_fields[19] if len(stat_fields) > 19 else ""
    expected_command = [os.fsencode(python_bin), b"-m", os.fsencode(bot_module)]
    if process_uid != os.geteuid():
        print(
            f"ERROR: PID {pid} belongs to another user; refusing to signal it.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if actual_start_ticks != expected_start_ticks:
        remove_unchanged_pid_file()
        print(
            f"INFO: PID {pid} was reused or exited; removed stale PID file without signaling it."
        )
        raise SystemExit(0)
    if command[:3] != expected_command:
        print(
            f"ERROR: PID {pid} does not match the recorded simulation bot command; refusing to signal it.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        signal.pidfd_send_signal(process_fd.fileno(), signal.SIGTERM)
    except OSError as error:
        if error.errno != errno.ESRCH:
            print(f"ERROR: Could not stop simulation bot PID {pid}: {error}", file=sys.stderr)
            raise SystemExit(1)
    else:
        print(f"INFO: Stopping simulation bot (PID {pid})...")
        if not poller.poll(1000):
            try:
                signal.pidfd_send_signal(process_fd.fileno(), signal.SIGKILL)
            except OSError as error:
                if error.errno != errno.ESRCH:
                    print(f"ERROR: Could not force-stop simulation bot PID {pid}: {error}", file=sys.stderr)
                    raise SystemExit(1)
            if not poller.poll(1000):
                print(f"ERROR: Simulation bot PID {pid} did not exit after SIGKILL.", file=sys.stderr)
                raise SystemExit(1)

remove_unchanged_pid_file()
print("INFO: Simulation bot stopped successfully.")
PY
