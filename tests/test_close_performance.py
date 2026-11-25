from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.close_performance import (
    ClosePerformanceHooks,
    entry_cost_basis,
    record_close_performance,
)


def _performance():
    return {
        "sells": 0,
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "break_even": 0,
        "realized_pnl_base": 0.0,
        "by_source": defaultdict(
            lambda: {"buys": 0, "sells": 0, "pnl": 0.0}),
    }


def _hooks(events=None, *, consecutive=0):
    events = events if events is not None else []
    state = {"consecutive": consecutive, "pause": None}
    now = datetime(2026, 11, 25, 12, 0, 0)
    hooks = ClosePerformanceHooks(
        get_consecutive_losses=lambda: state["consecutive"],
        set_consecutive_losses=lambda value: (
            state.__setitem__("consecutive", value),
            events.append(("consecutive", value)) if events is not None else None,
        ),
        set_buy_pause_until_utc=lambda value: (
            state.__setitem__("pause", value),
            events.append(("pause", value)) if events is not None else None,
        ),
        now_local=lambda: events.append(("now_local",)) or now,
        now_utc=lambda: events.append(("now_utc",)) or now.replace(tzinfo=UTC),
    )
    return hooks, state


def _config(**overrides):
    values = {
        "loss_streak_pause_threshold": 0,
        "loss_streak_pause_seconds": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_entry_cost_basis_uses_recorded_amount_base():
    assert entry_cost_basis({"amount_base": 100.5}, 100.0, 1.0) == 100.5


def test_entry_cost_basis_reconstructs_legacy_gross_position(caplog):
    with caplog.at_level(logging.WARNING, logger="src.close_performance"):
        basis = entry_cost_basis({}, 100.0, 0.5)
    assert basis == 50.0
    assert caplog.messages == [
        "Legacy DRY-RUN position has no cost basis; reconstructing from entry price"
    ]


def test_entry_cost_basis_rejects_missing_net_basis():
    with pytest.raises(ValueError, match="Invalid entry cost basis"):
        entry_cost_basis({"execution_cost_mode": "net_costs"}, 100.0, 1.0)


@pytest.mark.parametrize("bad_basis", [0.0, -1.0, float("nan"), "not-a-number"])
def test_entry_cost_basis_rejects_invalid_values(bad_basis):
    with pytest.raises(ValueError, match="Invalid entry cost basis"):
        entry_cost_basis({"amount_base": bad_basis}, 100.0, 1.0)


def test_close_performance_updates_positive_and_break_even_attribution():
    performance = _performance()
    hooks, state = _hooks(consecutive=2)
    entry = {
        "buy_price": 100.0,
        "amount_coin": 2.0,
        "amount_base": 200.0,
        "timestamp": datetime(2026, 11, 25, 11, 59, 0),
        "signal_source": "catboost",
    }

    positive = record_close_performance(
        _config(), performance, hooks, entry, 110.0, 1.0)
    flat = record_close_performance(
        _config(), performance, hooks, entry, 100.0, 1.0)

    assert positive == (10.0, 10.0, 60.0)
    assert flat == (0.0, 0.0, 60.0)
    assert performance["sells"] == 2
    assert performance["closed_trades"] == 2
    assert performance["wins"] == 1
    assert performance["break_even"] == 1
    assert performance["realized_pnl_base"] == 10.0
    assert performance["by_source"]["catboost"] == {
        "buys": 0, "sells": 2, "pnl": 10.0,
    }
    assert state["consecutive"] == 0


def test_net_partial_close_uses_proceeds_and_proportional_entry_basis():
    performance = _performance()
    hooks, _ = _hooks()
    entry = {
        "buy_price": 100.0,
        "amount_coin": 2.0,
        "amount_base": 204.0,
        "execution_cost_mode": "live_exchange",
        "timestamp": datetime(2026, 11, 25, 11, 59, 0),
    }

    result = record_close_performance(
        _config(), performance, hooks, entry, 110.0, 1.0, net_proceeds=109.0)

    assert result == (7.0, pytest.approx(7.0 / 102.0 * 100.0), 60.0)
    assert performance["realized_pnl_base"] == 7.0


def test_nonpositive_position_does_not_mutate_performance_or_hooks():
    events = []
    performance = _performance()
    hooks, state = _hooks(events)

    result = record_close_performance(
        _config(), performance, hooks, {"buy_price": 10.0, "amount_coin": 0.0},
        11.0, 1.0)

    assert result == (0.0, 0.0, 0.0)
    assert performance["closed_trades"] == 0
    assert events == []
    assert state["consecutive"] == 0


def test_loss_streak_pause_preserves_mutation_and_hook_order(caplog):
    events = []
    performance = _performance()
    hooks, state = _hooks(events, consecutive=1)
    entry = {
        "buy_price": 100.0,
        "amount_coin": 1.0,
        "timestamp": datetime(2026, 11, 25, 11, 58, 0),
    }

    with caplog.at_level(logging.WARNING, logger="src.close_performance"):
        result = record_close_performance(
            _config(loss_streak_pause_threshold=2, loss_streak_pause_seconds=300),
            performance, hooks, entry, 99.0, 1.0)

    assert result == (-1.0, -1.0, 120.0)
    assert state["consecutive"] == 2
    assert state["pause"] == datetime(2026, 11, 25, 12, 5, tzinfo=UTC)
    assert events == [
        ("now_local",),
        ("now_local",),
        ("consecutive", 2),
        ("now_utc",),
        ("pause", datetime(2026, 11, 25, 12, 5, tzinfo=UTC)),
    ]
    assert caplog.messages == [
        ("🧯 Loss streak detected (2 in a row). New BUYs paused until "
         "2026-11-25T12:05:00+00:00")
    ]