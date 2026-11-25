"""Close accounting and entry cost-basis helpers for trading execution."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClosePerformanceHooks:
    """Bot-owned mutable state and clock supplied to close accounting."""

    get_consecutive_losses: Callable[[], int]
    set_consecutive_losses: Callable[[int], None]
    set_buy_pause_until_utc: Callable[[datetime], None]
    now_local: Callable[[], datetime]
    now_utc: Callable[[], datetime]


def entry_cost_basis(
    entry_trade: dict,
    buy_price: float,
    buy_amount_coin: float,
    *,
    logger: logging.Logger = logger,
) -> float:
    raw_basis = entry_trade.get('amount_base')
    if raw_basis is None and entry_trade.get('execution_cost_mode') != 'net_costs':
        logger.warning("Legacy DRY-RUN position has no cost basis; reconstructing from entry price")
        raw_basis = buy_price * buy_amount_coin
    try:
        basis = float(cast(Any, raw_basis))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid entry cost basis") from exc
    if not math.isfinite(basis) or basis <= 0:
        raise ValueError("Invalid entry cost basis")
    return basis


def record_close_performance(
    config: Any,
    performance: dict[str, Any],
    hooks: ClosePerformanceHooks,
    entry_trade: dict,
    sell_price: float,
    sell_amount: float,
    *,
    net_proceeds: float | None = None,
    entry_basis: float | None = None,
    logger: logging.Logger = logger,
) -> tuple[float, float, float]:
    buy_price = float(entry_trade.get('buy_price', 0.0))
    buy_amount_coin = float(entry_trade.get('amount_coin', 0.0))
    if buy_amount_coin <= 0:
        return 0.0, 0.0, 0.0
    pnl_base = (sell_price - buy_price) * sell_amount
    pnl_pct = ((sell_price - buy_price) / buy_price) * \
        100 if buy_price > 0 else 0.0
    if net_proceeds is not None:
        total_basis = entry_basis if entry_basis is not None else entry_cost_basis(
            entry_trade, buy_price, buy_amount_coin, logger=logger)
        basis = total_basis * min(sell_amount / buy_amount_coin, 1.0)
        pnl_base = net_proceeds - basis
        pnl_pct = pnl_base / basis * 100 if basis > 0 else 0.0
    hold_seconds = (
        hooks.now_local() - entry_trade.get('timestamp', hooks.now_local())).total_seconds()

    performance['sells'] += 1
    performance['closed_trades'] += 1
    performance['realized_pnl_base'] += pnl_base
    if pnl_base > 0:
        performance['wins'] += 1
    elif pnl_base < 0:
        performance['losses'] += 1
    else:
        performance['break_even'] += 1

    source = entry_trade.get('signal_source', 'rules')
    performance['by_source'][source]['sells'] += 1
    performance['by_source'][source]['pnl'] += pnl_base

    if pnl_base < 0:
        consecutive_losses = hooks.get_consecutive_losses() + 1
        hooks.set_consecutive_losses(consecutive_losses)
        if (
            config.loss_streak_pause_threshold > 0
            and config.loss_streak_pause_seconds > 0
            and consecutive_losses >= config.loss_streak_pause_threshold
        ):
            buy_pause_until_utc = hooks.now_utc() + timedelta(
                seconds=config.loss_streak_pause_seconds)
            hooks.set_buy_pause_until_utc(buy_pause_until_utc)
            logger.warning(
                f"🧯 Loss streak detected ({consecutive_losses} in a row). "
                f"New BUYs paused until {buy_pause_until_utc.isoformat()}"
            )
    else:
        hooks.set_consecutive_losses(0)

    return pnl_base, pnl_pct, hold_seconds