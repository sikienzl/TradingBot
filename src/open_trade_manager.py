"""Monitoring loop for open positions: excursions, stops and the exit chain.

Layer-1 collaborator: the ``BotConfig`` and the ``Portfolio`` arrive through a
frozen :class:`OpenTradeContext`, and everything the bot owns but this loop only
calls -- the ATR cache, the trade executor and the public-paper-mode probe --
arrives through :class:`OpenTradeHooks`. The exit-reason chain itself lives in
:mod:`src.exit_policy` and is called directly, so the whole per-position sweep
can be unit-tested without an exchange or a bot. ``CryptoTradingBot`` keeps a
thin delegator with the previous method name and still owns the trade loop, the
ATR cache, the journal and the portfolio.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from src.exit_policy import (
    core_exit_reason,
    downtrend_reversal_exit_reason,
    effective_stop_loss_level,
    maybe_partial_take_profit,
    partial_tp_remainder_exit_reason,
    uptrend_rules_exit_reason,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenTradeContext:
    """Bot-owned state that the sweep reads and mutates."""

    config: Any
    portfolio: Any


@dataclass(frozen=True)
class OpenTradeHooks:
    """Bot-owned collaborators that the sweep calls but does not own."""

    get_atr: Callable[..., float]
    execute_trade: Callable[..., Any]
    public_paper_mode: Callable[[], bool]


def manage_open_trades(
    context: OpenTradeContext,
    hooks: OpenTradeHooks,
    current_market_data: dict[str, dict],
    market_analysis: dict | None = None,
) -> None:
    """Monitors open trades for stop-loss, take-profit, time and signal exits."""
    config = context.config
    portfolio = context.portfolio
    trades_to_remove = []
    excursions_updated = False
    for coin, trade_info in list(portfolio.open_trades.items()):
        buy_price = trade_info['buy_price']
        amount_coin = trade_info['amount_coin']
        partial_tp_taken = bool(trade_info.get('partial_tp_taken', False))
        partial_tp_timestamp = trade_info.get('partial_tp_timestamp')
        current_price_data = current_market_data.get(coin)
        if current_price_data is None:
            logger.warning(
                f"Current market data for {coin} not available, cannot monitor trade.")
            continue
        current_price = current_price_data['price']
        peak_price = max(
            float(trade_info.get('peak_price', buy_price)),
            buy_price,
            current_price,
        )
        trough_price = min(
            float(trade_info.get('trough_price', buy_price)),
            buy_price,
            current_price,
        )
        if (
            peak_price != float(trade_info.get('peak_price', buy_price))
            or trough_price != float(trade_info.get('trough_price', buy_price))
        ):
            excursions_updated = True
        trade_info['peak_price'] = peak_price
        trade_info['trough_price'] = trough_price
        pnl_pct = (current_price - buy_price) / buy_price * 100
        hold_seconds = (datetime.now() -
                        trade_info['timestamp']).total_seconds()
        max_hold_seconds = config.coin_max_hold_seconds.get(
            coin, config.max_hold_seconds)
        # Fetch ATR for dynamic stops
        atr_started = time.monotonic()
        atr = hooks.get_atr(coin, period=14)
        atr_seconds = time.monotonic() - atr_started
        observed_hold_seconds = (datetime.now() - trade_info['timestamp']).total_seconds()
        if max_hold_seconds > 0 and observed_hold_seconds >= max_hold_seconds:
            logger.warning(
                "Position hold limit reached: coin=%s observed_hold_seconds=%.1f "
                "limit_seconds=%d overdue_seconds=%.1f atr_fetch_seconds=%.1f; "
                "exit is checked during the trading loop, not at an exact deadline",
                coin, observed_hold_seconds, max_hold_seconds,
                observed_hold_seconds - max_hold_seconds, atr_seconds,
            )
        if np.isnan(atr) or atr == 0:
            atr = buy_price * 0.02  # Fallback: 2% of price
        stop_loss_level = effective_stop_loss_level(
            config,
            trade_info, current_price, atr)
        take_profit_level = buy_price + config.atr_tp_mult * atr
        partial_take_profit_level = buy_price + \
            config.partial_take_profit_atr_mult * atr
        peak_price = float(trade_info.get('peak_price', buy_price))
        logger.info(
            f"  📊 {coin}: Buy {buy_price:.4f} → Current {current_price:.4f} "
            f"({pnl_pct:+.2f}%) | SL {stop_loss_level:.4f} | TP {take_profit_level:.4f} "
            f"| Peak {peak_price:.4f} | Hold time {int(hold_seconds)}s")
        exit_reason = None
        partial_fraction = min(
            max(config.partial_take_profit_fraction, 0.0), 1.0)
        if maybe_partial_take_profit(
            config,
            portfolio,
            hooks.execute_trade,
            hooks.public_paper_mode,
            coin, trade_info, partial_tp_taken, amount_coin, current_price,
            partial_take_profit_level, partial_fraction, atr,
        ):
            continue
        exit_reason = partial_tp_remainder_exit_reason(
            config,
            exit_reason, coin, partial_tp_taken, partial_tp_timestamp,
            market_analysis,
        )
        exit_reason = downtrend_reversal_exit_reason(
            config,
            exit_reason, coin, trade_info, hold_seconds, pnl_pct,
            market_analysis,
        )
        exit_reason = uptrend_rules_exit_reason(
            config,
            exit_reason, coin, trade_info, hold_seconds, pnl_pct,
            market_analysis,
        )
        exit_reason = core_exit_reason(
            config,
            exit_reason, coin, current_price, stop_loss_level,
            take_profit_level, max_hold_seconds, hold_seconds, pnl_pct,
            market_analysis,
        )
        if exit_reason:
            logger.info(
                f"{exit_reason} → Verkauf {coin} @ {current_price:.4f}")
            if hooks.execute_trade(
                coin,
                "sell",
                current_price,
                amount_coin * current_price,
                atr=atr,
                reason=exit_reason,
            ):
                remaining_trade = portfolio.open_trades.get(coin)
                if (
                    remaining_trade is None
                    or float(remaining_trade.get("amount_coin", 0.0))
                    >= amount_coin - 1e-12
                ):
                    trades_to_remove.append(coin)
            else:
                logger.warning(
                    "Exit sell failed for %s; retaining the position for retry.",
                    coin,
                )
    for coin in trades_to_remove:
        portfolio.remove_trade(coin)
    if excursions_updated and config.dry_run:
        portfolio.save_state()

