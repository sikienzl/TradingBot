"""Recent-PnL entry guard state for the PnL exporters.

The bot blocks new entries when its recent realized performance is weak; the
exporters publish that decision as Prometheus gauges so Grafana can show it
without reading the bot's state files. The settings come from the deployment
``.env`` file (see :mod:`src.env_file`), the numbers from the trade journal.

:func:`src.journal_guards._compute_recent_pnl_guard_state` is the bot-side
equivalent: it works on a pandas journal frame and returns the bot's own keys.
This module keeps the exporter's flat ``recent_pnl_guard_*`` keys.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .env_file import env_bool, safe_float_env, safe_int_env
from .exporter_paths import ENV_PATH

GUARD_ENABLED_KEY = 'RECENT_PNL_GUARD_ENABLED'
GUARD_WINDOW_KEY = 'RECENT_PNL_GUARD_WINDOW'
GUARD_MIN_TRADES_KEY = 'RECENT_PNL_GUARD_MIN_TRADES'
GUARD_MIN_REALIZED_PNL_KEY = 'RECENT_PNL_GUARD_MIN_REALIZED_PNL'
GUARD_MAX_PROFIT_FACTOR_KEY = 'RECENT_PNL_GUARD_MAX_PROFIT_FACTOR'


def compute_recent_pnl_guard_state(trades, env_path=ENV_PATH):
    """Return the ``recent_pnl_guard_*`` gauges for the exporter."""
    settings = _guard_settings(env_path)
    state = _initial_guard_state(settings)
    if not settings['enabled']:
        return state

    recent = _recent_sell_pnl(trades, settings['window'])
    if not recent:
        return state

    state['recent_pnl_guard_recent_closed_trades'] = float(len(recent))
    if len(recent) < settings['min_trades']:
        return state

    realized_pnl = float(sum(recent))
    recent_profit_factor = _window_profit_factor(recent)
    state['recent_pnl_guard_recent_realized_pnl'] = realized_pnl
    state['recent_pnl_guard_recent_profit_factor'] = recent_profit_factor

    guard_active = (
        realized_pnl < settings['min_realized_pnl']
        and recent_profit_factor <= settings['max_profit_factor']
    )
    state['recent_pnl_guard_active'] = 1.0 if guard_active else 0.0
    state['recent_pnl_guard_reason'] = (
        'negative_recent_pnl_and_low_pf' if guard_active else 'healthy_recent_window'
    )
    return state


def _guard_settings(env_path: str) -> dict[str, Any]:
    """Read the guard configuration from the environment file."""
    enabled = env_bool(GUARD_ENABLED_KEY, True, env_path=env_path)
    return {
        'enabled': enabled,
        'window': max(1, safe_int_env(GUARD_WINDOW_KEY, 120, env_path=env_path)),
        'min_trades': max(1, safe_int_env(
            GUARD_MIN_TRADES_KEY, 60, env_path=env_path)),
        'min_realized_pnl': safe_float_env(
            GUARD_MIN_REALIZED_PNL_KEY, 0.0, env_path=env_path),
        'max_profit_factor': safe_float_env(
            GUARD_MAX_PROFIT_FACTOR_KEY, 1.0, env_path=env_path),
    }


def _initial_guard_state(settings: Mapping[str, Any]) -> dict[str, Any]:
    """The gauge set before any journal row has been looked at."""
    return {
        'recent_pnl_guard_enabled': 1.0 if settings['enabled'] else 0.0,
        'recent_pnl_guard_active': 0.0,
        'recent_pnl_guard_window': float(settings['window']),
        'recent_pnl_guard_min_trades': float(settings['min_trades']),
        'recent_pnl_guard_recent_closed_trades': 0.0,
        'recent_pnl_guard_recent_realized_pnl': 0.0,
        'recent_pnl_guard_recent_profit_factor': 0.0,
        'recent_pnl_guard_min_realized_pnl': float(settings['min_realized_pnl']),
        'recent_pnl_guard_max_profit_factor': float(settings['max_profit_factor']),
        'recent_pnl_guard_reason': (
            'disabled' if not settings['enabled'] else 'insufficient_recent_trades'
        ),
    }


def _recent_sell_pnl(trades: Iterable[Mapping[str, Any]], window: int) -> list[float]:
    """Realized PnL of the last *window* sell rows, skipping unparsable ones."""
    sells: list[float] = []
    for trade in trades:
        action = (trade.get('action') or '').strip().lower()
        if action != 'sell':
            continue
        try:
            pnl = float(trade.get('pnl_base', '0') or 0.0)
        except ValueError:
            continue
        sells.append(pnl)
    return sells[-window:]


def _window_profit_factor(recent: Iterable[float]) -> float:
    """Gross profit over gross loss for the window; ``inf`` without losses."""
    gross_profit = float(sum(p for p in recent if p > 0.0))
    gross_loss_abs = float(sum(-p for p in recent if p < 0.0))
    if gross_loss_abs > 0:
        return gross_profit / gross_loss_abs
    if gross_profit > 0:
        return float('inf')
    return 0.0


__all__ = ['compute_recent_pnl_guard_state']
