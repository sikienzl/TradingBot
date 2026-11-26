"""Unit tests for the pure hard-risk guardrails (audit point 4).

The guardrails are evaluated before any signal source is consulted, so their
reason strings and their precedence are part of the operational contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from src.risk_guardrails import (
    BUY_BUDGET_WINDOW,
    GuardrailState,
    can_open_new_positions,
    is_coin_in_reentry_cooldown,
    register_buy_timestamp,
    resolve_reentry_cooldown_seconds,
)

NOW = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)


@dataclass
class _Config:
    max_daily_loss_pct: float = 5.0
    max_buys_per_hour: int = 3
    reentry_cooldown_seconds: int = 3600
    reentry_cooldown_min_seconds: int = 60
    reentry_cooldown_max_seconds: int = 7200
    reentry_dynamic_enabled: bool = False
    reentry_volatility_low_atr_pct: float = 0.008
    reentry_volatility_high_atr_pct: float = 0.018
    reentry_cooldown_mult_low_vol: float = 0.5
    reentry_cooldown_mult_mid_vol: float = 1.0
    reentry_cooldown_mult_high_vol: float = 2.0


def _now():
    return NOW


# ---------------------------------------------------------------------------
# Fail closed first
# ---------------------------------------------------------------------------
def test_unreadable_risk_state_blocks_before_every_other_guardrail():
    config = _Config(max_daily_loss_pct=0.0, max_buys_per_hour=0)
    state = GuardrailState(
        buy_pause_until_utc=NOW - timedelta(hours=1),
        daily_anchor_value=0.0,
        buy_timestamps_utc=[],
        risk_state_unreadable=True,
    )
    allowed, reason = can_open_new_positions(config, state, 1000.0, now_utc=NOW)
    assert allowed is False
    assert reason == "Risk state unreadable - daily-loss budget and loss streak unknown"


def test_unreadable_flag_does_not_change_other_reasons():
    state = GuardrailState(risk_state_unreadable=False)
    assert can_open_new_positions(_Config(), state, 1000.0, now_utc=NOW) == (True, "")


# ---------------------------------------------------------------------------
# Loss-streak pause
# ---------------------------------------------------------------------------
def test_active_buy_pause_is_reported_with_remaining_seconds():
    state = GuardrailState(buy_pause_until_utc=NOW + timedelta(seconds=95))
    allowed, reason = can_open_new_positions(_Config(), state, 1000.0, now_utc=NOW)
    assert allowed is False
    assert reason == "BUY pause active for another 95s"


def test_expired_pause_no_longer_blocks():
    state = GuardrailState(buy_pause_until_utc=NOW - timedelta(seconds=1))
    assert can_open_new_positions(_Config(), state, 1000.0, now_utc=NOW) == (True, "")


# ---------------------------------------------------------------------------
# Daily loss limit
# ---------------------------------------------------------------------------
def test_daily_loss_limit_uses_the_persisted_anchor():
    state = GuardrailState(daily_anchor_value=1000.0)
    allowed, reason = can_open_new_positions(config=_Config(max_daily_loss_pct=5.0),
                                             state=state, portfolio_value=950.0,
                                             now_utc=NOW)
    assert allowed is False
    assert reason == "Daily-Loss-Limit erreicht (5.00% >= 5.00%)"


def test_daily_loss_limit_is_ignored_when_disabled_or_unanchored():
    for config, value in ((_Config(max_daily_loss_pct=0.0), 1.0),
                         (_Config(), 0.0)):
        state = GuardrailState(daily_anchor_value=value)
        assert can_open_new_positions(config, state, 1.0, now_utc=NOW) == (True, "")


def test_profit_above_anchor_never_triggers_the_daily_limit():
    state = GuardrailState(daily_anchor_value=1000.0)
    assert can_open_new_positions(_Config(), state, 1200.0, now_utc=NOW) == (True, "")


# ---------------------------------------------------------------------------
# Hourly BUY budget
# ---------------------------------------------------------------------------
def test_hourly_buy_budget_counts_only_the_recent_window():
    state = GuardrailState(buy_timestamps_utc=[
        NOW - timedelta(minutes=70),
        NOW - timedelta(minutes=30),
        NOW - timedelta(minutes=10),
    ])
    config = _Config(max_buys_per_hour=2)
    allowed, reason = can_open_new_positions(config, state, 1000.0, now_utc=NOW)
    assert allowed is False
    assert reason == "BUY-Limit pro Stunde erreicht (2/2)"
    # The stale timestamp is pruned in place, so the caller's list stays correct.
    assert state.buy_timestamps_utc == [NOW - timedelta(minutes=30),
                                        NOW - timedelta(minutes=10)]


def test_stale_timestamps_leave_room_for_a_new_entry():
    state = GuardrailState(buy_timestamps_utc=[
        NOW - timedelta(minutes=70),
        NOW - timedelta(minutes=65),
        NOW - timedelta(minutes=61),
    ])
    assert can_open_new_positions(_Config(max_buys_per_hour=3), state, 1000.0,
                                  now_utc=NOW) == (True, "")
    assert state.buy_timestamps_utc == []


def test_hourly_budget_disabled_or_missing_is_allowed():
    assert can_open_new_positions(_Config(max_buys_per_hour=0),
                                  GuardrailState(buy_timestamps_utc=[NOW] * 9),
                                  1000.0, now_utc=NOW) == (True, "")
    assert can_open_new_positions(_Config(), GuardrailState(), 1000.0,
                                  now_utc=NOW) == (True, "")


def test_register_buy_timestamp_appends_and_prunes():
    timestamps = [NOW - timedelta(minutes=90), NOW - timedelta(minutes=5)]
    result = register_buy_timestamp(timestamps, now_utc=NOW)
    assert result == [NOW - timedelta(minutes=5), NOW]
    assert BUY_BUDGET_WINDOW == timedelta(hours=1)


# ---------------------------------------------------------------------------
# Re-entry cooldown
# ---------------------------------------------------------------------------
def test_static_cooldown_is_clamped_to_its_bounds():
    config = _Config(reentry_cooldown_seconds=10, reentry_cooldown_min_seconds=60)
    assert resolve_reentry_cooldown_seconds(config, 100.0, 1.0) == (60, 'static')
    config = _Config(reentry_cooldown_seconds=99999, reentry_cooldown_max_seconds=7200)
    assert resolve_reentry_cooldown_seconds(config, 100.0, 1.0) == (7200, 'static')


def test_disabled_cooldown_short_circuits():
    assert resolve_reentry_cooldown_seconds(_Config(reentry_cooldown_seconds=0),
                                            100.0, 1.0) == (0, 'base_zero')


@pytest.mark.parametrize("price, atr, reason", [
    (None, 1.0, 'fallback_no_atr'),
    (0.0, 1.0, 'fallback_no_atr'),
    (100.0, None, 'fallback_no_atr'),
    (100.0, float('nan'), 'fallback_no_atr'),
    (100.0, 0.0, 'fallback_no_atr'),
])
def test_dynamic_cooldown_falls_back_without_usable_volatility(price, atr, reason):
    config = _Config(reentry_dynamic_enabled=True)
    assert resolve_reentry_cooldown_seconds(config, price, atr) == (3600, reason)


@pytest.mark.parametrize("atr, expected, regime", [
    (0.5, 1800, 'low_vol'),
    (1.0, 3600, 'mid_vol'),
    (8.0, 7200, 'high_vol'),
])
def test_dynamic_cooldown_scales_with_volatility(atr, expected, regime):
    config = _Config(reentry_dynamic_enabled=True)
    seconds, reason = resolve_reentry_cooldown_seconds(config, 100.0, atr)
    assert seconds == expected
    assert reason.startswith(f'{regime},atr_pct=')


def test_dynamic_cooldown_clamps_to_the_configured_bounds():
    config = _Config(reentry_dynamic_enabled=True, reentry_cooldown_max_seconds=2000,
                     reentry_volatility_high_atr_pct=0.018)
    assert resolve_reentry_cooldown_seconds(config, 100.0, 8.0) == (
        2000, 'high_vol,atr_pct=0.0800,mult=2.00')


def test_cooldown_lookup_uses_the_last_sell_timestamp():
    sells = {"BTC": NOW - timedelta(seconds=3500)}
    in_cooldown, remaining = is_coin_in_reentry_cooldown(
        _Config(), sells, "BTC", now_utc=NOW)
    assert in_cooldown is True
    assert remaining == 100


def test_cooldown_lookup_for_unknown_coin_or_disabled_cooldown():
    sells = {"BTC": NOW - timedelta(seconds=10)}
    assert is_coin_in_reentry_cooldown(_Config(), sells, "ETH", now_utc=NOW) == (False, 0)
    assert is_coin_in_reentry_cooldown(_Config(reentry_cooldown_seconds=0), sells,
                                       "BTC", now_utc=NOW) == (False, 0)


def test_expired_cooldown_is_not_active():
    sells = {"BTC": NOW - timedelta(seconds=3601)}
    assert is_coin_in_reentry_cooldown(_Config(), sells, "BTC", now_utc=NOW) == (False, 0)


def test_explicit_cooldown_overrides_the_configured_base():
    sells = {"BTC": NOW - timedelta(seconds=100)}
    in_cooldown, remaining = is_coin_in_reentry_cooldown(
        _Config(), sells, "BTC", 120, now_utc=NOW)
    assert (in_cooldown, remaining) == (True, 20)
