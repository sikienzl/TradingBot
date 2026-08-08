#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH=/opt/trading_2
export VIRTUAL_ENV=/opt/trading_2/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
# pre-start dump to logs
python - <<'PY'
import sys,os,json
obj={
  "ts": __import__("datetime").datetime.utcnow().isoformat(),
  "sys_path": sys.path,
  "env": {"PYTHONPATH": os.environ.get("PYTHONPATH"), "VIRTUAL_ENV": os.environ.get("VIRTUAL_ENV")},
  "cwd": os.getcwd()
}
try:
  os.makedirs('/opt/trading_2/logs', exist_ok=True)
  open('/opt/trading_2/logs/sys_path_start.jsonl','a').write(json.dumps(obj)+"\n")
except Exception:
  # best-effort only
  pass
PY
exec "$VIRTUAL_ENV/bin/python" -m src.trading_bot "$@"
