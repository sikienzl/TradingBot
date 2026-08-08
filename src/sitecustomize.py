import json
import os
import sys
import traceback

try:
    p = '/opt/trading_2/logs/sitecustomize_start.jsonl'
    os.makedirs(os.path.dirname(p), exist_ok=True)
    data = {
        'cwd': os.getcwd(),
        'argv': sys.argv[:],
        'sys_path': sys.path[:],
        'env': {k: os.environ.get(k) for k in ['PYTHONPATH', 'VIRTUAL_ENV', 'PATH']},
    }
    with open(p, 'a') as f:
        f.write(json.dumps(data) + '\n')
except Exception:  # noqa: BLE001
    sys.stderr.write('sitecustomize dump failed:\n' + traceback.format_exc())
