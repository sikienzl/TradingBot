"""Read values from an ``.env`` style file without python-dotenv.

Both PnL exporters resolve their guard settings and AI co-pilot caps from the
deployment ``.env`` file. The bot itself uses :mod:`src.bot_config`, which reads
the real process environment instead, so these helpers stay exporter-only.
"""
from __future__ import annotations

import os
import sys

from .exporter_paths import ENV_PATH


def read_env_value(env_key, env_path=ENV_PATH, *, quiet=False):
    try:
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    if key.strip() != env_key:
                        continue
                    return value.strip().strip('"').strip("'")
    except (OSError, UnicodeError) as exc:
        if not quiet:
            print(f"Error reading environment file: {exc}", file=sys.stderr)
    return ''


def env_bool(env_key, default=False, env_path=ENV_PATH, *, quiet=False):
    value = read_env_value(env_key, env_path, quiet=quiet)
    if value == '':
        return bool(default)
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def safe_int_env(env_key, default, env_path=ENV_PATH):
    value = read_env_value(env_key, env_path)
    if value == '':
        return int(default)
    try:
        return int(value)
    except ValueError:
        return int(default)


def safe_float_env(env_key, default, env_path=ENV_PATH):
    value = read_env_value(env_key, env_path)
    if value == '':
        return float(default)
    try:
        return float(value)
    except ValueError:
        return float(default)
