"""Persistent state files for the trading bot.

Extracted from :class:`src.trading_bot.CryptoTradingBot` so the JSON
read/write, cooldown and quota-normalisation logic can be tested without
constructing the bot. The bot keeps thin delegators with the original
method names.
"""

import json
import logging
import os
import shutil
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


def read_auto_tune_state(path: str | None) -> dict:
    """Loads the auto-tune state file, returning {} when missing/invalid."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def write_auto_tune_state(state: dict, path: str | None):
    """Writes the auto-tune state file, logging (not raising) on failure."""
    if not path:
        return
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=True, indent=2)
    except (OSError, TypeError, ValueError) as e:
        logger.warning(
            f"Auto-tune state could not be saved: {e}")


def cooldown_remaining_minutes(state: dict, cooldown_minutes: int) -> float:
    """Minutes until the next auto-tune is allowed (0.0 when ready)."""
    cooldown = max(0, cooldown_minutes)
    if cooldown <= 0:
        return 0.0
    last = state.get('last_applied_at')
    if not last:
        return 0.0
    try:
        last_dt = datetime.fromisoformat(last)
        # Backward-compatible: treat naive timestamps as UTC.
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return 0.0
    elapsed_minutes = (datetime.now(UTC) -
                       last_dt).total_seconds() / 60.0
    return max(0.0, cooldown - elapsed_minutes)


def read_ai_copilot_state(path: str | None) -> dict:
    """Loads the AI co-pilot state, falling back to the ``.bak`` copy."""
    if not path:
        return {}

    candidates = [path, f'{path}.bak']
    last_error = None
    for candidate in candidates:
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, 'r', encoding='utf-8') as f:
                state = json.load(f)
            if candidate != path:
                logger.warning(
                    'Recovered AI co-pilot state from backup file')
            return state
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            last_error = exc

    if last_error is not None:
        logger.warning(
            f'AI co-pilot state could not be loaded: {last_error}')
    return {}


def write_ai_copilot_state(state: dict, path: str | None):
    """Atomically writes the AI co-pilot state, keeping a ``.bak`` backup."""
    if not path:
        return
    tmp_path = f'{path}.tmp'
    backup_path = f'{path}.bak'
    try:
        if os.path.exists(path):
            try:
                shutil.copyfile(path, backup_path)
            except OSError as exc:
                logger.warning(
                    f'AI co-pilot state backup could not be updated: {exc}')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=True, indent=2)
        os.replace(tmp_path, path)
    except (OSError, TypeError, ValueError) as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        logger.warning(f"AI co-pilot state could not be saved: {e}")


def current_month_key() -> str:
    now = datetime.now(UTC)
    return f"{now.year:04d}-{now.month:02d}"


def current_day_key() -> str:
    now = datetime.now(UTC)
    return f"{now.year:04d}-{now.month:02d}-{now.day:02d}"


def normalize_ai_state(state: dict, monthly_call_cap: int, budget_cap_usd: float) -> dict:
    """Resets monthly/daily counters when the period key changed."""
    month_key = current_month_key()
    day_key = current_day_key()
    state['budget_cap_usd'] = float(budget_cap_usd)
    state['calls_cap_monthly'] = int(monthly_call_cap)
    if state.get('month_key') != month_key:
        state['month_key'] = month_key
        state['monthly_calls'] = 0
        state['monthly_spend_usd'] = 0.0
    if state.get('day_key') != day_key:
        state['day_key'] = day_key
        state['daily_calls'] = 0
        state['consecutive_errors'] = 0
        state.pop('last_suspended_at', None)
    state.setdefault('monthly_calls', 0)
    state.setdefault('daily_calls', 0)
    state.setdefault('monthly_spend_usd', 0.0)
    state.setdefault('consecutive_errors', 0)
    return state
