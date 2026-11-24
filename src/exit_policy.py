"""Exit policy for open positions: dynamic stops and the ranked exit reasons.

Layer-1 collaborator: the ``BotConfig`` and the ``Portfolio`` are injected and
everything bot-owned (the trade executor, the public-paper-mode probe, the
clock) reaches these functions as an explicit argument or callback, so the
stop-level maths and the five-step exit-reason chain can be unit-tested without
constructing a :class:`CryptoTradingBot`. ``CryptoTradingBot`` keeps thin
delegators for every entry point here and still owns the trade loop, the ATR
cache and the journal.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def effective_stop_loss_level(
    config: Any,
    trade_info: dict,
    current_price: float,
    atr: float,
) -> float:
    """Builds a dynamic stop level combining base ATR stop, trailing stop and break-even protection."""
    buy_price = float(trade_info.get('buy_price', 0.0))
    if buy_price <= 0:
        return current_price

    peak_price = float(trade_info.get('peak_price', buy_price))
    peak_price = max(peak_price, current_price, buy_price)
    trade_info['peak_price'] = peak_price

    stop_loss_level = buy_price - config.atr_stop_mult * atr

    if config.trailing_stop_enabled:
        trailing_level = peak_price - config.trailing_stop_atr_mult * atr
        stop_loss_level = max(stop_loss_level, trailing_level)

    if config.break_even_enabled and buy_price > 0:
        runup_pct = (peak_price - buy_price) / buy_price * 100.0
        if runup_pct >= config.break_even_trigger_pct:
            break_even_level = buy_price * \
                (1.0 + config.break_even_buffer_pct / 100.0)
            stop_loss_level = max(stop_loss_level, break_even_level)

    return stop_loss_level


def is_rules_uptrend_trade(trade_info: dict) -> bool:
    """Returns True for uptrend trades that were opened by the rules engine."""
    recommendation = str(trade_info.get('recommendation', ''))
    signal_source = str(trade_info.get('signal_source', ''))
    return recommendation == 'HOLD (Up-Trend)' and signal_source == 'rules'


def is_downtrend_reversal_trade(trade_info: dict) -> bool:
    """Returns True for trades opened via the guarded downtrend reversal path."""
    return str(trade_info.get('recommendation', '')) == 'HOLD (Down-Trend)'


def maybe_partial_take_profit(
    config: Any,
    portfolio: Any,
    execute_trade: Callable[..., Any],
    public_paper_mode: Callable[[], bool],
    coin: str,
    trade_info: dict,
    partial_tp_taken: bool,
    amount_coin: float,
    current_price: float,
    partial_take_profit_level: float,
    partial_fraction: float,
    atr: float,
    *,
    now: Callable[[], datetime] = datetime.now,
    logger: logging.Logger = logger,
) -> bool:
    """Execute a partial take-profit sell; return True if the caller should skip the rest."""
    if (
        config.partial_take_profit_enabled
        and not partial_tp_taken
        and 0.0 < partial_fraction < 1.0
        and current_price >= partial_take_profit_level
    ):
        partial_amount_coin = amount_coin * partial_fraction
        if partial_amount_coin > 1e-12:
            partial_reason = (
                f"💸 PARTIAL-TAKE-PROFIT ({partial_fraction*100:.0f}% @ "
                f"{partial_take_profit_level:.4f})"
            )
            logger.info(
                f"{partial_reason} → Teilverkauf {coin} @ {current_price:.4f}")
            if execute_trade(
                coin,
                "sell",
                current_price,
                partial_amount_coin * current_price,
                atr=atr,
                reason=partial_reason,
                partial_take_profit=True,
            ):
                if coin in portfolio.open_trades and not public_paper_mode():
                    portfolio.open_trades[coin]['partial_tp_taken'] = True
                    portfolio.open_trades[coin]['partial_tp_timestamp'] = now(
                    )
                    if config.dry_run:
                        portfolio.save_state()
                return True
            logger.warning(
                "Partial take-profit sell failed for %s; continuing "
                "with remaining exit checks.",
                coin,
            )
        return False
    return False


def partial_tp_remainder_exit_reason(
    config: Any,
    exit_reason: str | None,
    coin: str,
    partial_tp_taken: bool,
    partial_tp_timestamp: Any,
    market_analysis: dict | None,
    *,
    now: Callable[[], datetime] = datetime.now,
) -> str | None:
    """Exit reason for the leftover position after a partial take-profit."""
    if partial_tp_taken and partial_tp_timestamp:
        partial_tp_elapsed_seconds = (
            now() - partial_tp_timestamp).total_seconds()
        if (
            config.partial_take_profit_remainder_max_hold_seconds > 0
            and partial_tp_elapsed_seconds >= config.partial_take_profit_remainder_max_hold_seconds
        ):
            exit_reason = (
                "⏱️ PARTIAL-TP-REMAINDER-TIMEOUT "
                f"({int(partial_tp_elapsed_seconds)}s >= "
                f"{config.partial_take_profit_remainder_max_hold_seconds}s)"
            )
        elif (
            config.partial_take_profit_exit_on_weak_signal
            and market_analysis is not None
        ):
            signal = market_analysis.get(
                coin, {}).get('recommendation', '')
            if signal in {'SELL', 'WEAK SELL', 'HOLD (Down-Trend)'}:
                exit_reason = (
                    '📉 PARTIAL-TP-REMAINDER-WEAK-SIGNAL '
                    f'(recommendation: {signal})'
                )
    return exit_reason


def downtrend_reversal_exit_reason(
    config: Any,
    exit_reason: str | None,
    coin: str,
    trade_info: dict,
    hold_seconds: float,
    pnl_pct: float,
    market_analysis: dict | None,
) -> str | None:
    """Exit reason for downtrend-reversal trades (max-hold, fast, weak-signal)."""
    is_downtrend_reversal = is_downtrend_reversal_trade(
        trade_info)
    if (
        exit_reason is None
        and is_downtrend_reversal
        and config.downtrend_reversal_fast_exit_enabled
        and market_analysis is not None
    ):
        current_signal = market_analysis.get(
            coin, {}).get('recommendation', '')
        if (
            config.downtrend_reversal_max_hold_seconds > 0
            and hold_seconds >= config.downtrend_reversal_max_hold_seconds
        ):
            exit_reason = (
                '⏱️ DOWNTREND-REVERSAL-MAX-HOLD '
                f'({int(hold_seconds)}s >= {config.downtrend_reversal_max_hold_seconds}s)'
            )
        elif (
            hold_seconds >= config.downtrend_reversal_fast_exit_seconds
            and pnl_pct <= config.downtrend_reversal_flat_max_profit_pct
            and current_signal in {'HOLD (Down-Trend)', 'WEAK SELL', 'SELL'}
        ):
            exit_reason = (
                '📉 DOWNTREND-REVERSAL-FAST-EXIT '
                f'({pnl_pct:+.2f}% <= {config.downtrend_reversal_flat_max_profit_pct:.2f}% '
                f'after {int(hold_seconds)}s, signal: {current_signal})'
            )
        elif (
            hold_seconds >= config.downtrend_reversal_weak_signal_exit_seconds
            and pnl_pct <= config.downtrend_reversal_weak_signal_max_profit_pct
            and current_signal in {'HOLD (Down-Trend)', 'WEAK SELL', 'SELL'}
        ):
            exit_reason = (
                '📉 DOWNTREND-REVERSAL-WEAK-SIGNAL-EXIT '
                f'({pnl_pct:+.2f}% <= {config.downtrend_reversal_weak_signal_max_profit_pct:.2f}% '
                f'after {int(hold_seconds)}s, signal: {current_signal})'
            )
    return exit_reason


def uptrend_rules_exit_reason(
    config: Any,
    exit_reason: str | None,
    coin: str,
    trade_info: dict,
    hold_seconds: float,
    pnl_pct: float,
    market_analysis: dict | None,
) -> str | None:
    """Exit reason for rules-based uptrend trades (max-hold, fast-exit)."""
    is_rules_uptrend = is_rules_uptrend_trade(trade_info)
    if (
        exit_reason is None
        and is_rules_uptrend
        and config.uptrend_rules_fast_exit_enabled
        and market_analysis is not None
    ):
        current_signal = market_analysis.get(
            coin, {}).get('recommendation', '')
        fast_exit_seconds = config.uptrend_rules_fast_exit_seconds_by_coin.get(
            coin, config.uptrend_rules_fast_exit_seconds)
        flat_max_profit_pct = config.uptrend_rules_flat_max_profit_pct_by_coin.get(
            coin, config.uptrend_rules_flat_max_profit_pct)
        uptrend_rules_max_hold_seconds = config.uptrend_rules_max_hold_seconds_by_coin.get(
            coin, config.uptrend_rules_max_hold_seconds)
        if (
            uptrend_rules_max_hold_seconds > 0
            and hold_seconds >= uptrend_rules_max_hold_seconds
        ):
            exit_reason = (
                '⏱️ UPTREND-RULES-MAX-HOLD '
                f'({int(hold_seconds)}s >= {uptrend_rules_max_hold_seconds}s)'
            )
        elif (
            fast_exit_seconds > 0
            and hold_seconds >= fast_exit_seconds
            and pnl_pct <= flat_max_profit_pct
            and current_signal not in {'BUY', 'STRONG BUY'}
        ):
            exit_reason = (
                '📉 UPTREND-RULES-FAST-EXIT '
                f'({pnl_pct:+.2f}% <= {flat_max_profit_pct:.2f}% '
                f'after {int(hold_seconds)}s, signal: {current_signal})'
            )
    return exit_reason


def core_exit_reason(
    config: Any,
    exit_reason: str | None,
    coin: str,
    current_price: float,
    stop_loss_level: float,
    take_profit_level: float,
    max_hold_seconds: int,
    hold_seconds: float,
    pnl_pct: float,
    market_analysis: dict | None,
) -> str | None:
    """Core stop-loss, take-profit, max-hold and signal-based exit checks."""
    if current_price <= stop_loss_level:
        exit_reason = f"🚨 ATR-STOP-LOSS (Stop: {stop_loss_level:.4f})"
    elif (not config.partial_take_profit_enabled) and current_price >= take_profit_level:
        exit_reason = f"🎉 ATR-TAKE-PROFIT (TP: {take_profit_level:.4f})"
    elif max_hold_seconds > 0 and hold_seconds >= max_hold_seconds:
        exit_reason = f"⏰ MAX-HOLD-TIME reached ({int(hold_seconds)}s >= {max_hold_seconds}s)"
    elif config.exit_on_downtrend and market_analysis is not None:
        signal = market_analysis.get(
            coin, {}).get('recommendation', '')
        if signal == 'HOLD (Down-Trend)':
            exit_reason = f"📉 Down-trend signal detected (recommendation: {signal})"
    elif pnl_pct < 0 and market_analysis is not None:
        signal = market_analysis.get(
            coin, {}).get('recommendation', '')
        if signal not in {'BUY', 'STRONG BUY', 'HOLD (Up-Trend)'}:
            exit_reason = f"🔻 PnL-NEG-WEAK-SIGNAL ({pnl_pct:+.2f}% | signal: {signal})"
    return exit_reason
