"""Unit tests for the exporter-side guard gauges in :mod:`src.recent_pnl_guard`.

These are the flat ``recent_pnl_guard_*`` keys the Prometheus exporters publish;
the bot's own journal guard lives in :mod:`src.journal_guards` and is covered by
``tests/test_recent_pnl_guard.py``.
"""
from __future__ import annotations

import math

import pytest

from src.recent_pnl_guard import (
    _guard_settings,
    _recent_sell_pnl,
    _window_profit_factor,
    compute_recent_pnl_guard_state,
)

DEFAULT_KEYS = {
    'recent_pnl_guard_enabled',
    'recent_pnl_guard_active',
    'recent_pnl_guard_window',
    'recent_pnl_guard_min_trades',
    'recent_pnl_guard_recent_closed_trades',
    'recent_pnl_guard_recent_realized_pnl',
    'recent_pnl_guard_recent_profit_factor',
    'recent_pnl_guard_min_realized_pnl',
    'recent_pnl_guard_max_profit_factor',
    'recent_pnl_guard_reason',
}


def _env(tmp_path, **values) -> str:
    path = tmp_path / ".env"
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    return str(path)


def _sells(*pnls, action="sell"):
    return [{'action': action, 'pnl_base': str(pnl)} for pnl in pnls]


# ------------------------------------------------------------------- defaults


def test_defaults_with_no_env_file(tmp_path):
    state = compute_recent_pnl_guard_state([], env_path=str(tmp_path / "nope.env"))

    assert set(state) == DEFAULT_KEYS
    assert state['recent_pnl_guard_enabled'] == 1.0
    assert state['recent_pnl_guard_window'] == 120.0
    assert state['recent_pnl_guard_min_trades'] == 60.0
    assert state['recent_pnl_guard_min_realized_pnl'] == 0.0
    assert state['recent_pnl_guard_max_profit_factor'] == 1.0
    assert state['recent_pnl_guard_reason'] == 'insufficient_recent_trades'
    assert state['recent_pnl_guard_active'] == 0.0


def test_disabled_guard_short_circuits(tmp_path):
    env = _env(tmp_path, RECENT_PNL_GUARD_ENABLED='false')

    state = compute_recent_pnl_guard_state(_sells(-1, -2), env_path=env)

    assert state['recent_pnl_guard_enabled'] == 0.0
    assert state['recent_pnl_guard_reason'] == 'disabled'
    assert state['recent_pnl_guard_recent_closed_trades'] == 0.0


def test_env_settings_are_clamped(tmp_path):
    env = _env(tmp_path, RECENT_PNL_GUARD_WINDOW='0',
               RECENT_PNL_GUARD_MIN_TRADES='-5')

    state = compute_recent_pnl_guard_state([], env_path=env)

    assert state['recent_pnl_guard_window'] == 1.0
    assert state['recent_pnl_guard_min_trades'] == 1.0


def test_unparsable_env_values_fall_back(tmp_path):
    env = _env(tmp_path, RECENT_PNL_GUARD_WINDOW='abc',
               RECENT_PNL_GUARD_MIN_REALIZED_PNL='oops')

    state = compute_recent_pnl_guard_state([], env_path=env)

    assert state['recent_pnl_guard_window'] == 120.0
    assert state['recent_pnl_guard_min_realized_pnl'] == 0.0


# ------------------------------------------------------------------ journal


def test_no_sell_rows_keeps_insufficient_reason(tmp_path):
    env = _env(tmp_path, RECENT_PNL_GUARD_MIN_TRADES='1')

    state = compute_recent_pnl_guard_state(
        [{'action': 'buy', 'pnl_base': '1'}], env_path=env)

    assert state['recent_pnl_guard_recent_closed_trades'] == 0.0
    assert state['recent_pnl_guard_reason'] == 'insufficient_recent_trades'


def test_fewer_trades_than_min_trades(tmp_path):
    env = _env(tmp_path, RECENT_PNL_GUARD_MIN_TRADES='3')

    state = compute_recent_pnl_guard_state(_sells(-1, -2), env_path=env)

    assert state['recent_pnl_guard_recent_closed_trades'] == 2.0
    assert state['recent_pnl_guard_recent_realized_pnl'] == 0.0
    assert state['recent_pnl_guard_reason'] == 'insufficient_recent_trades'


def test_window_only_counts_the_last_trades(tmp_path):
    env = _env(tmp_path, RECENT_PNL_GUARD_WINDOW='2',
               RECENT_PNL_GUARD_MIN_TRADES='1')

    state = compute_recent_pnl_guard_state(_sells(100, -5, -7), env_path=env)

    assert state['recent_pnl_guard_recent_closed_trades'] == 2.0
    assert state['recent_pnl_guard_recent_realized_pnl'] == -12.0
    assert state['recent_pnl_guard_recent_profit_factor'] == 0.0


def test_active_guard_on_negative_pnl_and_low_pf(tmp_path):
    env = _env(tmp_path, RECENT_PNL_GUARD_MIN_TRADES='2',
               RECENT_PNL_GUARD_MIN_REALIZED_PNL='0',
               RECENT_PNL_GUARD_MAX_PROFIT_FACTOR='1')

    state = compute_recent_pnl_guard_state(_sells(2, -5), env_path=env)

    assert state['recent_pnl_guard_recent_profit_factor'] == pytest.approx(0.4)
    assert state['recent_pnl_guard_active'] == 1.0
    assert state['recent_pnl_guard_reason'] == 'negative_recent_pnl_and_low_pf'


def test_healthy_window_when_profit_factor_is_high(tmp_path):
    env = _env(tmp_path, RECENT_PNL_GUARD_MIN_TRADES='2',
               RECENT_PNL_GUARD_MAX_PROFIT_FACTOR='1')

    state = compute_recent_pnl_guard_state(_sells(9, -1), env_path=env)

    assert state['recent_pnl_guard_recent_realized_pnl'] == 8.0
    assert state['recent_pnl_guard_active'] == 0.0
    assert state['recent_pnl_guard_reason'] == 'healthy_recent_window'


def test_healthy_window_when_realized_pnl_is_positive(tmp_path):
    env = _env(tmp_path, RECENT_PNL_GUARD_MIN_TRADES='2',
               RECENT_PNL_GUARD_MIN_REALIZED_PNL='-10')

    state = compute_recent_pnl_guard_state(_sells(-1, -2), env_path=env)

    assert state['recent_pnl_guard_active'] == 0.0
    assert state['recent_pnl_guard_reason'] == 'healthy_recent_window'


def test_profit_factor_without_losses_is_infinite(tmp_path):
    env = _env(tmp_path, RECENT_PNL_GUARD_MIN_TRADES='1',
               RECENT_PNL_GUARD_MIN_REALIZED_PNL='10')

    state = compute_recent_pnl_guard_state(_sells(1, 2), env_path=env)

    # A loss-free window reports +inf, which never satisfies the
    # "profit factor below the maximum" half of the guard condition.
    assert state['recent_pnl_guard_recent_profit_factor'] == float('inf')
    assert state['recent_pnl_guard_active'] == 0.0
    assert state['recent_pnl_guard_reason'] == 'healthy_recent_window'


def test_action_and_pnl_parsing_is_lenient(tmp_path):
    env = _env(tmp_path, RECENT_PNL_GUARD_MIN_TRADES='1')
    trades = [
        {'action': ' SELL ', 'pnl_base': '1.5'},
        {'action': None, 'pnl_base': '9'},
        {'action': 'sell', 'pnl_base': 'broken'},
        {'action': 'sell'},
        {'action': 'sell', 'pnl_base': ''},
    ]

    state = compute_recent_pnl_guard_state(trades, env_path=env)

    assert state['recent_pnl_guard_recent_closed_trades'] == 3.0
    assert state['recent_pnl_guard_recent_realized_pnl'] == 1.5


# --------------------------------------------------------------- helpers


def test_guard_settings_reads_every_key(tmp_path):
    env = _env(tmp_path,
               RECENT_PNL_GUARD_ENABLED='1',
               RECENT_PNL_GUARD_WINDOW='7',
               RECENT_PNL_GUARD_MIN_TRADES='3',
               RECENT_PNL_GUARD_MIN_REALIZED_PNL='-2.5',
               RECENT_PNL_GUARD_MAX_PROFIT_FACTOR='1.25')

    assert _guard_settings(env) == {
        'enabled': True,
        'window': 7,
        'min_trades': 3,
        'min_realized_pnl': -2.5,
        'max_profit_factor': 1.25,
    }


def test_recent_sell_pnl_skips_other_actions():
    trades = [{'action': 'buy', 'pnl_base': '5'}] + _sells(1, 2, 3)
    assert _recent_sell_pnl(trades, 2) == [2.0, 3.0]


@pytest.mark.parametrize(
    ("recent", "expected"),
    [
        ([2.0, -5.0], 0.4),
        ([1.0, -1.0], 1.0),
        ([1.0, 2.0], float('inf')),
        ([0.0, 0.0], 0.0),
        ([], 0.0),
    ],
)
def test_window_profit_factor(recent, expected):
    result = _window_profit_factor(recent)
    if math.isinf(expected):
        assert result == float('inf')
    else:
        assert result == pytest.approx(expected)
