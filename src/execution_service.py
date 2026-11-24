"""Live order execution for the trading bot.

Layer-1 collaborator: the ccxt exchange is injected and every piece of
bot-owned state reaches the functions here as an explicit argument or callback,
so the fill-confirmation state machine and the real-money accounting can be
unit-tested against a stub exchange. :class:`CryptoTradingBot` keeps thin
delegators for every entry point here, and still owns the portfolio, the
journal, the performance counters and the cooldown bookkeeping.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import ccxt

logger = logging.getLogger(__name__)

#: Order statuses after which the exchange will not change the fill again.
TERMINAL_ORDER_STATUSES = frozenset({
    "closed", "filled", "canceled", "cancelled", "expired", "rejected",
})


def order_fill_details(
    order: dict[str, Any],
    requested_amount: float,
    fallback_price: float,
) -> tuple[float, float, float]:
    """Returns validated filled amount, average price, and quote cost."""
    if not isinstance(order, dict):
        raise TypeError("Exchange order response is not an object.")
    status = str(order.get("status", "")).lower()
    filled_value = order.get("filled")
    if filled_value is None and status in {"closed", "filled"}:
        filled_value = order.get("amount", requested_amount)
    if filled_value is None:
        raise ValueError("Exchange order response does not report a filled amount.")

    filled = float(filled_value)
    average = float(order.get("average") or order.get("price") or fallback_price)
    cost = float(order.get("cost") or filled * average)
    if (
        not math.isfinite(filled)
        or not math.isfinite(average)
        or not math.isfinite(cost)
        or not math.isfinite(requested_amount)
        or not math.isfinite(fallback_price)
        or filled <= 0
        or average <= 0
        or cost <= 0
        or requested_amount <= 0
        or filled > requested_amount + max(1e-12, requested_amount * 1e-8)
    ):
        raise ValueError("Exchange order response contains invalid fill details.")
    return filled, average, cost


def confirm_order_fill(
    exchange: Any,
    order: dict[str, Any],
    symbol: str,
    requested_amount: float,
    fallback_price: float,
    *,
    logger: logging.Logger = logger,
) -> tuple[float, float, float]:
    """Resolve an order response into a trustworthy ``(filled, average, cost)``.

    A non-terminal response is cancelled and re-read; only a terminal order is
    accounted, and a refresh that stays non-terminal raises rather than letting
    the portfolio guess at the fill.
    """
    if not isinstance(order, dict):
        raise TypeError("Exchange order response is not an object.")
    status = str(order.get("status", "")).lower()
    terminal_statuses = TERMINAL_ORDER_STATUSES
    if status in terminal_statuses:
        try:
            return order_fill_details(order, requested_amount, fallback_price)
        except (TypeError, ValueError) as exc:
            order_id = order.get("id")
            if not order_id:
                raise ValueError(
                    "Terminal order response has no usable fill details or order ID."
                ) from exc
    else:
        order_id = order.get("id")
        if not order_id or exchange is None:
            raise ValueError(
                "Unconfirmed order response has no usable order ID."
            )

    cancel_error = None
    if status not in terminal_statuses:
        try:
            exchange.cancel_order(order_id, symbol)
        except (ccxt.OrderNotFound, ccxt.InvalidOrder):
            pass
        except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
            cancel_error = exc

    try:
        refreshed_order = exchange.fetch_order(order_id, symbol)
    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        raise ValueError(
            f"Could not refresh order {order_id} to confirm its final state."
        ) from exc

    if not isinstance(refreshed_order, dict):
        raise TypeError(f"Order {order_id} refresh returned an invalid response.")
    refreshed_status = str(refreshed_order.get("status", "")).lower()
    if refreshed_status not in terminal_statuses:
        error = ValueError(
            f"Order {order_id} remains non-terminal ({refreshed_status or 'unknown'}); "
            "fill state is not safe to account."
        )
        if cancel_error:
            raise error from cancel_error
        raise error

    try:
        return order_fill_details(refreshed_order, requested_amount, fallback_price)
    except (TypeError, ValueError) as exc:
        if status in terminal_statuses:
            try:
                return order_fill_details(order, requested_amount, fallback_price)
            except (TypeError, ValueError):
                pass
        else:
            raise ValueError(
                f"Order {order_id} has no usable confirmed fill details."
            ) from exc
        raise ValueError(
            f"Order {order_id} has no usable confirmed fill details."
        ) from exc


@dataclass(frozen=True)
class RealTradeHooks:
    """Bot-owned collaborators that :func:`execute_real_trade` needs.

    The bot passes these bound so the service never reaches into the bot, and
    monkeypatching a bot method in a test still takes effect.
    """

    confirm_fill: Callable[..., tuple[float, float, float]]
    record_close_performance: Callable[..., tuple[float, float, float]]
    trade_excursion_metrics: Callable[..., tuple[float, float]]
    append_trade_journal: Callable[[dict[str, Any]], Any]
    register_buy_timestamp: Callable[[], None]
    register_sell_timestamp: Callable[[str], None]
    update_portfolio_balance: Callable[[], None]


def execute_real_trade(
    exchange: Any,
    config: Any,
    portfolio: Any,
    performance: dict[str, Any],
    hooks: RealTradeHooks,
    coin: str,
    action: str,
    price: float,
    amount_in_base_currency: float,
    *,
    signal_source: str = 'rules',
    signal_confidence: float | None = None,
    recommendation: str = 'HOLD',
    reason: str = '',
    logger: logging.Logger = logger,
) -> bool:
    """Live exchange trade execution (ccxt market orders)."""
    if exchange is None:
        logger.error(
            "Real trade cannot be executed in simulation mode or without an exchange.")
        return False

    symbol = f"{coin}/{config.base_currency}"
    try:
        market = exchange.market(symbol)
        min_amount_base = market['limits']['amount']['min'] * \
            price if market['limits']['amount']['min'] else 0
        if amount_in_base_currency < min_amount_base and min_amount_base > 0:
            logger.warning(
                f"Trade amount {amount_in_base_currency:.2f} {config.base_currency} for {coin} is below the minimum of {min_amount_base:.2f} {config.base_currency}. Trade not executed.")
            return False

        if action == "buy":
            bought = _execute_real_buy(
                exchange, config, portfolio, performance, hooks, coin, price,
                amount_in_base_currency, symbol,
                signal_source=signal_source,
                signal_confidence=signal_confidence,
                recommendation=recommendation,
                reason=reason,
                logger=logger,
            )
            if not bought:
                return False
        elif action == "sell":
            sold = _execute_real_sell(
                exchange, config, portfolio, performance, hooks, coin, price,
                amount_in_base_currency, symbol,
                signal_source=signal_source,
                recommendation=recommendation,
                reason=reason,
                logger=logger,
            )
            if not sold:
                return False
        hooks.update_portfolio_balance()
        return True
    except ccxt.InsufficientFunds as e:
        logger.error(f"Insufficient funds for {action} {coin}: {e}")
        return False
    except ccxt.InvalidOrder as e:
        logger.error(
            f"Invalid order parameter for {action} {coin}: {e}")
        return False
    except (ccxt.NetworkError, ccxt.ExchangeError) as e:
        logger.error(
            f"Error executing trade ({action} {coin}): {e}")
        return False
    except ccxt.BaseError as e:
        logger.error(f"Exchange error executing trade ({action} {coin}): {e}")
        return False


def _execute_real_buy(
    exchange: Any,
    config: Any,
    portfolio: Any,
    performance: dict[str, Any],
    hooks: RealTradeHooks,
    coin: str,
    price: float,
    amount_in_base_currency: float,
    symbol: str,
    *,
    signal_source: str,
    signal_confidence: float | None,
    recommendation: str,
    reason: str,
    logger: logging.Logger,
) -> bool:
    """Market buy, fill confirmation and the accounting that follows it.

    Returns ``False`` when the fill cannot be confirmed, which makes the caller
    stop before touching the balance.
    """
    order = exchange.create_market_buy_order(
        symbol, amount_in_base_currency / price)
    try:
        filled, average, cost = hooks.confirm_fill(
            order, symbol, amount_in_base_currency / price, price
        )
    except (TypeError, ValueError) as exc:
        logger.error("Unable to confirm BUY fill for %s: %s", coin, exc)
        return False
    logger.info(
        f"✅ REAL TRADE (BUY): {filled:.4f} {coin} @ {average:.4f} {config.base_currency} (cost: {cost:.2f} {config.base_currency}, ID: {order.get('id', 'n/a')})")
    portfolio.add_trade(
        coin,
        average,
        filled,
        cost,
        signal_source=signal_source,
        signal_confidence=signal_confidence,
        recommendation=recommendation,
    )
    performance['buys'] += 1
    performance['by_source'][signal_source]['buys'] += 1
    hooks.register_buy_timestamp()
    hooks.append_trade_journal({
        'coin': coin,
        'action': 'buy',
        'price': average,
        'amount_coin': filled,
        'amount_base': cost,
        'signal_source': signal_source,
        'signal_confidence': signal_confidence,
        'recommendation': recommendation,
        'reason': reason,
        'dry_run': False,
    })
    return True


def _execute_real_sell(
    exchange: Any,
    config: Any,
    portfolio: Any,
    performance: dict[str, Any],
    hooks: RealTradeHooks,
    coin: str,
    price: float,
    amount_in_base_currency: float,
    symbol: str,
    *,
    signal_source: str,
    recommendation: str,
    reason: str,
    logger: logging.Logger,
) -> bool:
    """Market sell, fill confirmation and the accounting that follows it.

    Returns ``False`` when the portfolio cannot cover the sell, which makes the
    caller stop before touching the balance.
    """
    if coin not in portfolio.holdings or portfolio.holdings[coin] < amount_in_base_currency / price:
        logger.warning(
            f"Insufficient {coin} ({portfolio.holdings.get(coin, 0):.4f}) in portfolio to sell {amount_in_base_currency / price:.4f} {coin}.")
        return False
    order = exchange.create_market_sell_order(
        symbol, amount_in_base_currency / price)
    try:
        sold_amount_coin, average, cost = hooks.confirm_fill(
            order, symbol, amount_in_base_currency / price, price
        )
    except (TypeError, ValueError) as exc:
        logger.error("Unable to confirm SELL fill for %s: %s", coin, exc)
        return False
    logger.info(
        f"✅ REAL TRADE (SELL): {sold_amount_coin:.4f} {coin} @ {average:.4f} {config.base_currency} (proceeds: {cost:.2f} {config.base_currency}, ID: {order.get('id', 'n/a')})")

    entry_trade = portfolio.open_trades.get(coin, {})
    original_amount_coin = float(
        entry_trade.get('amount_coin', amount_in_base_currency / price))
    remaining_open_amount = max(
        0.0, original_amount_coin - sold_amount_coin)
    remaining_ratio = (
        remaining_open_amount / original_amount_coin
        if original_amount_coin > 0 else 0.0
    )
    pnl_base, pnl_pct, hold_seconds = hooks.record_close_performance(
        entry_trade, average, sold_amount_coin)
    mfe_pct, mae_pct = hooks.trade_excursion_metrics(entry_trade)
    hooks.append_trade_journal({
        'coin': coin,
        'action': 'sell',
        'price': average,
        'amount_coin': sold_amount_coin,
        'amount_base': cost,
        'pnl_base': pnl_base,
        'pnl_pct': pnl_pct,
        'hold_seconds': hold_seconds,
        'mfe_pct': mfe_pct,
        'mae_pct': mae_pct,
        'signal_source': entry_trade.get('signal_source', 'rules'),
        'signal_confidence': entry_trade.get('signal_confidence', ''),
        'recommendation': entry_trade.get('recommendation', ''),
        'reason': reason,
        'dry_run': False,
    })
    hooks.register_sell_timestamp(coin)
    if remaining_open_amount > 1e-12 and coin in portfolio.open_trades:
        portfolio.open_trades[coin]['amount_coin'] = remaining_open_amount
        portfolio.open_trades[coin]['amount_base'] = float(
            entry_trade.get('amount_base', 0.0)) * remaining_ratio
    else:
        portfolio.remove_trade(coin)
    return True


@dataclass(frozen=True)
class EntryHooks:
    """Bot-owned collaborators that :func:`open_new_positions` needs.

    The risk gates, position sizing, momentum filter and cooldown bookkeeping
    stay with the bot; the service only drives the candidate loop.
    """

    get_atr: Callable[..., float]
    compute_position_size: Callable[..., float]
    passes_momentum_filter: Callable[[dict[str, Any]], tuple[bool, str]]
    resolve_cooldown_seconds: Callable[[float, float], tuple[int, str]]
    in_reentry_cooldown: Callable[..., tuple[bool, int]]
    execute_trade: Callable[..., bool]
    log_blocked_attempts: Callable[[list[dict[str, Any]]], None]


def open_new_positions(
    config: Any,
    portfolio: Any,
    hooks: EntryHooks,
    current_market_data: dict[str, Any],
    market_analysis: dict[str, Any],
    top_buy_recommendations: list[str],
    portfolio_value_live: float,
    *,
    effective_trade_amount: float,
    effective_max_open_trades: int,
    can_open_positions: bool,
    block_reason: str,
    logger: logging.Logger = logger,
) -> None:
    """Open new positions for the selected buy candidates within risk limits."""
    # Step 5: Trade logic - open new positions
    current_open_trades_count = len(portfolio.open_trades)
    available_funds_for_trade = portfolio.cash
    min_required_cash = config.min_trade_amount if config.allow_partial_trades else effective_trade_amount

    if not can_open_positions:
        logger.warning(
            f"⛔ Risk guardrail blocking new positions: {block_reason}")
    elif available_funds_for_trade >= min_required_cash and current_open_trades_count < effective_max_open_trades:
        logger.info(
            f"Attempting to open new positions. Available: {available_funds_for_trade:.2f} {config.base_currency}, Open trades: {current_open_trades_count}/{effective_max_open_trades}, Min amount: {min_required_cash:.2f} {config.base_currency}, Effective trade amount: {effective_trade_amount:.2f} {config.base_currency}.")
        _open_candidate_positions(
            config, portfolio, hooks, current_market_data, market_analysis,
            top_buy_recommendations, portfolio_value_live,
            effective_trade_amount, effective_max_open_trades,
            logger=logger,
        )
    elif current_open_trades_count >= effective_max_open_trades:
        logger.info(
            f"Maximum number of open trades ({effective_max_open_trades}) reached. No new buys.")
    else:
        logger.info(
            f"Insufficient available funds ({available_funds_for_trade:.2f} {config.base_currency}) for minimum trade ({min_required_cash:.2f} {config.base_currency}).")


def _open_candidate_positions(
    config: Any,
    portfolio: Any,
    hooks: EntryHooks,
    current_market_data: dict[str, Any],
    market_analysis: dict[str, Any],
    top_buy_recommendations: list[str],
    portfolio_value_live: float,
    effective_trade_amount: float,
    effective_max_open_trades: int,
    *,
    logger: logging.Logger,
) -> None:
    """Walk the shortlist and buy every candidate that clears the entry gates."""
    blocked_buy_attempts: list[dict[str, Any]] = []
    successful_buy_count = 0
    for coin in top_buy_recommendations:
        # Only trade if no position is already open
        if coin not in portfolio.open_trades and coin not in portfolio.holdings:
            coin_data = market_analysis.get(coin, {})
            attempt_meta = {
                'coin': coin,
                'signal_source': coin_data.get('signal_source', 'rules'),
                'score': coin_data.get('score', 0),
                'recommendation': coin_data.get('recommendation', 'HOLD'),
                'position_size_text': 'n/a',
                'cash_text': f"{portfolio.cash:.2f}",
                'cooldown_text': 'n/a',
            }
            price_data = current_market_data.get(coin)
            if price_data:
                current_price = price_data['price']
                atr = hooks.get_atr(coin, period=14)
                position_size = hooks.compute_position_size(
                    coin, current_price, atr,
                    portfolio_value_live, effective_trade_amount,
                    attempt_meta)

                if position_size < config.min_trade_amount:
                    logger.info(
                        f"Skipping {coin}: effective trade amount {position_size:.2f} {config.base_currency} is below MIN_TRADE_AMOUNT {config.min_trade_amount:.2f}.")
                    blocked_buy_attempts.append({
                        **attempt_meta,
                        'reason': (
                            'below_min_trade_amount '
                            f'({position_size:.2f} < {config.min_trade_amount:.2f})'
                        ),
                    })
                    continue

                if portfolio.cash >= position_size:
                    blocked = _attempt_candidate_buy(
                        config, portfolio, hooks, coin, coin_data, attempt_meta,
                        current_price, atr, position_size,
                        logger=logger,
                    )
                    if blocked is None:
                        successful_buy_count += 1
                        # Update available capital
                        if len(portfolio.open_trades) >= effective_max_open_trades:
                            logger.info(
                                "Maximum number of open trades reached, stopping further buys.")
                            break
                    else:
                        blocked_buy_attempts.append(blocked)
                else:
                    logger.warning(
                        f"Insufficient available funds ({portfolio.cash:.2f} {config.base_currency}) for a trade of {position_size:.2f} {config.base_currency} for {coin}.")
                    blocked_buy_attempts.append({
                        **attempt_meta,
                        'reason': (
                            'insufficient_available_funds '
                            f'({portfolio.cash:.2f} < {position_size:.2f})'
                        ),
                    })
            else:
                logger.warning(
                    f"Price data for {coin} not found, cannot buy.")
                blocked_buy_attempts.append({
                    **attempt_meta,
                    'reason': 'missing_price_data',
                })
        else:
            logger.debug(
                f"{coin} already in portfolio or open position.")
    if top_buy_recommendations and successful_buy_count == 0:
        hooks.log_blocked_attempts(blocked_buy_attempts)


def _attempt_candidate_buy(
    config: Any,
    portfolio: Any,
    hooks: EntryHooks,
    coin: str,
    coin_data: dict[str, Any],
    attempt_meta: dict[str, Any],
    current_price: float,
    atr: float,
    position_size: float,
    *,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    """Run the momentum/cooldown gates and execute one candidate buy.

    Returns ``None`` when the position was opened, otherwise the blocked-attempt
    record that the caller adds to its diagnostics list.
    """
    logger.info(
        f"ℹ️ Attempting to buy {coin}...")
    signal_source = coin_data.get(
        'signal_source', 'rules')
    signal_confidence = coin_data.get(
        'signal_confidence')
    passes_filter, filter_reason = hooks.passes_momentum_filter(
        coin_data)
    if not passes_filter:
        logger.info(
            f"⛔ Momentum filter blocked entry for {coin}: {filter_reason}")
        return {**attempt_meta, 'reason': filter_reason}
    effective_cooldown, cooldown_mode = hooks.resolve_cooldown_seconds(
        current_price, atr)
    in_cooldown, cooldown_remaining = hooks.in_reentry_cooldown(
        coin, cooldown_seconds=effective_cooldown)
    if in_cooldown:
        cooldown_text = (
            f"{cooldown_remaining}s remaining "
            f"(effective={effective_cooldown}s, {cooldown_mode})"
        )
        logger.info(
            f"⛔ Re-entry cooldown active for {coin}: {cooldown_remaining}s remaining "
            f"(effective={effective_cooldown}s, base={config.reentry_cooldown_seconds}s, {cooldown_mode})")
        return {
            **attempt_meta,
            'reason': 'reentry_cooldown_active',
            'cooldown_text': cooldown_text,
        }
    if hooks.execute_trade(
        coin,
        "buy",
        current_price,
        position_size,
        atr=atr,
        signal_source=signal_source,
        signal_confidence=signal_confidence,
        recommendation=coin_data.get(
            'recommendation', 'HOLD'),
        reason='ENTRY',
    ):
        return None
    return {**attempt_meta, 'reason': 'execute_trade_failed'}
