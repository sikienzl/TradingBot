"""Pure hard-risk guardrail and re-entry cooldown evaluations.

Layer-1 collaborator for audit point 4: the daily-loss anchor, the hourly BUY
budget, the loss-streak pause and the re-entry cooldown are hard guardrails, so
their decision logic must be reviewable and testable without a bot, an exchange
or a ``BotConfig`` instance. Every value reaches these functions as an explicit
argument and the caller keeps its own attributes, which is what lets
:mod:`src.trading_bot` persist the same state across restarts.

The returned reason strings are part of the operational contract: they appear in
the log, in the blocked-attempt diagnostics and in tests.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

#: Window used by the hourly BUY budget.
BUY_BUDGET_WINDOW = timedelta(hours=1)


@dataclass
class GuardrailState:
    """The process risk counters that :func:`can_open_new_positions` evaluates.

    ``buy_timestamps_utc`` is pruned in place so the caller's own list stays the
    single source of truth for the hourly budget.
    """

    buy_pause_until_utc: datetime | None = None
    daily_anchor_value: float = 0.0
    buy_timestamps_utc: list[datetime] | None = None
    #: True when the durable risk state exists but could not be read.
    risk_state_unreadable: bool = False


def can_open_new_positions(
    config: Any,
    state: GuardrailState,
    portfolio_value: float,
    *,
    now_utc: datetime | None = None,
) -> tuple[bool, str]:
    """Returns ``(allowed, block_reason)`` for a new entry.

    Fails closed: an unreadable durable risk state means the daily-loss budget
    and the loss streak are unknown, so new entries stay blocked until an
    operator repairs or removes the state file.
    """
    now_utc = now_utc or datetime.now(UTC)

    if state.risk_state_unreadable:
        return False, (
            "Risk state unreadable - daily-loss budget and loss streak unknown"
        )

    if state.buy_pause_until_utc and now_utc < state.buy_pause_until_utc:
        remaining = (state.buy_pause_until_utc - now_utc).total_seconds()
        return False, f"BUY pause active for another {int(max(0, remaining))}s"

    if config.max_daily_loss_pct > 0 and state.daily_anchor_value > 0:
        drawdown_pct = max(
            0.0,
            (state.daily_anchor_value - portfolio_value) /
            state.daily_anchor_value * 100.0,
        )
        if drawdown_pct >= config.max_daily_loss_pct:
            return False, (
                f"Daily-Loss-Limit erreicht ({drawdown_pct:.2f}% >= "
                f"{config.max_daily_loss_pct:.2f}%)"
            )

    if config.max_buys_per_hour > 0:
        timestamps = state.buy_timestamps_utc
        if timestamps is not None:
            cutoff = now_utc - BUY_BUDGET_WINDOW
            timestamps[:] = [ts for ts in timestamps if ts >= cutoff]
            if len(timestamps) >= config.max_buys_per_hour:
                return False, (
                    f"BUY-Limit pro Stunde erreicht "
                    f"({len(timestamps)}/{config.max_buys_per_hour})"
                )

    return True, ""


def register_buy_timestamp(
    timestamps: list[datetime],
    *,
    now_utc: datetime | None = None,
) -> list[datetime]:
    """Adds one BUY to the hourly budget and prunes entries that left the window."""
    now_utc = now_utc or datetime.now(UTC)
    timestamps.append(now_utc)
    cutoff = now_utc - BUY_BUDGET_WINDOW
    return [ts for ts in timestamps if ts >= cutoff]


def resolve_reentry_cooldown_seconds(config: Any, current_price: float, atr: float) -> tuple[int, str]:
    """Returns an effective re-entry cooldown (seconds), optionally adjusted by volatility."""
    base = max(0, int(config.reentry_cooldown_seconds))
    minimum = max(0, int(config.reentry_cooldown_min_seconds))
    maximum = max(minimum, int(config.reentry_cooldown_max_seconds))

    if base <= 0:
        return 0, 'base_zero'

    if not config.reentry_dynamic_enabled:
        return min(max(base, minimum), maximum), 'static'

    if (
        current_price is None
        or current_price <= 0
        or atr is None
        or math.isnan(float(atr))
        or atr <= 0
    ):
        return min(max(base, minimum), maximum), 'fallback_no_atr'

    atr_pct = float(atr) / float(current_price)
    low = max(0.0, float(config.reentry_volatility_low_atr_pct))
    high = max(low, float(config.reentry_volatility_high_atr_pct))

    if atr_pct < low:
        mult = float(config.reentry_cooldown_mult_low_vol)
        regime = 'low_vol'
    elif atr_pct > high:
        mult = float(config.reentry_cooldown_mult_high_vol)
        regime = 'high_vol'
    else:
        mult = float(config.reentry_cooldown_mult_mid_vol)
        regime = 'mid_vol'

    effective = round(base * mult)
    effective = min(max(effective, minimum), maximum)
    reason = f'{regime},atr_pct={atr_pct:.4f},mult={mult:.2f}'
    return effective, reason


def is_coin_in_reentry_cooldown(
    config: Any,
    last_sell_timestamps: Mapping[str, datetime],
    coin: str,
    cooldown_seconds: int | None = None,
    *,
    now_utc: datetime | None = None,
) -> tuple[bool, int]:
    """True when ``coin`` sold recently enough that a re-entry is still paused."""
    cooldown = max(
        0,
        int(config.reentry_cooldown_seconds if cooldown_seconds is None else cooldown_seconds),
    )
    if cooldown <= 0:
        return False, 0

    last_sell = last_sell_timestamps.get(coin)
    if last_sell is None:
        return False, 0

    elapsed = ((now_utc or datetime.now(UTC)) - last_sell).total_seconds()
    remaining = int(max(0, cooldown - elapsed))
    if remaining > 0:
        return True, remaining
    return False, 0
