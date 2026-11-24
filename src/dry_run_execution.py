"""Simulated (dry-run / paper) trade execution for the trading bot.

Layer-1 collaborator: the ``BotConfig`` and the ``Portfolio`` are injected and
every piece of bot-owned state (the journal, the performance counters, the
public-paper transaction commit, the cooldown bookkeeping and the clock)
reaches these functions as an explicit argument or callback, so the
slippage/fee fill maths and the simulated accounting can be unit-tested
without constructing a :class:`CryptoTradingBot`. ``CryptoTradingBot`` keeps a
thin delegator for :func:`execute_dry_run_trade` and still owns the trade
loop, the journal writers and the loss-streak bookkeeping.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DryRunHooks:
    """Bot-owned collaborators that :func:`execute_dry_run_trade` needs.

    The bot passes these bound so the service never reaches into the bot, and
    monkeypatching a bot method in a test still takes effect.
    """

    record_close_performance: Callable[..., tuple[float, float, float]]
    trade_excursion_metrics: Callable[[dict[str, Any]], tuple[float, float]]
    entry_cost_basis: Callable[[dict[str, Any], float, float], float]
    append_trade_journal: Callable[[dict[str, Any]], Any]
    commit_paper_trade: Callable[[dict[str, Any]], Any]
    public_paper_mode: Callable[[], bool]
    register_buy_timestamp: Callable[[], None]
    register_sell_timestamp: Callable[[str], None]
    now: Callable[[], datetime] = datetime.now


def execute_dry_run_trade(
    config: Any,
    portfolio: Any,
    performance: dict[str, Any],
    hooks: DryRunHooks,
    coin: str,
    action: str,
    price: float,
    amount_in_base_currency: float,
    *,
    signal_source: str = 'rules',
    signal_confidence: float | None = None,
    recommendation: str = 'HOLD',
    reason: str = '',
    partial_take_profit: bool = False,
    logger: logging.Logger = logger,
) -> bool:
    """Simulated (dry-run / paper) trade execution."""
    costs_enabled = config.dry_run_costs_enabled
    tracked_entry = portfolio.open_trades.get(coin, {})
    net_tracked = (
        action == 'sell' and isinstance(tracked_entry, dict)
        and tracked_entry.get('execution_cost_mode') == 'net_costs'
    )
    plan = plan_dry_run_fill(
        config, coin, action, price, amount_in_base_currency,
        costs_enabled, net_tracked, logger=logger)
    if plan is None:
        return False
    fill_price, amount_coin = plan
    logger.info(
        "💰 DRY-RUN: %s %.8f %s reference=%.8f fill=%.8f %s "
        "requested_base=%.8f cost_mode=%s",
        action.upper(), amount_coin, coin, price, fill_price,
        config.base_currency, amount_in_base_currency,
        'net_costs' if costs_enabled else 'gross_legacy')
    if action == "buy":
        return dry_run_buy(
            config, portfolio, performance, hooks,
            coin, price, amount_in_base_currency, amount_coin,
            fill_price, costs_enabled, signal_source,
            signal_confidence, recommendation, reason, logger=logger)
    if action == "sell":
        return dry_run_sell(
            config, portfolio, hooks,
            coin, price, amount_coin, fill_price, costs_enabled,
            net_tracked, tracked_entry, reason, partial_take_profit,
            logger=logger)
    return True


def plan_dry_run_fill(
    config: Any,
    coin: str,
    action: str,
    price: float,
    amount_in_base_currency: float,
    costs_enabled: bool,
    net_tracked: bool,
    *,
    logger: logging.Logger = logger,
) -> tuple[float, float] | None:
    """Validate a dry-run request and compute (fill_price, amount_coin).

    Returns None when the request is invalid or the calculation breaks down.
    """
    if (costs_enabled or net_tracked) and (
        action not in ('buy', 'sell')
        or not math.isfinite(price) or price <= 0
        or not math.isfinite(amount_in_base_currency)
        or amount_in_base_currency <= 0
    ):
        logger.warning("Invalid DRY-RUN trade request for %s", coin)
        return None
    amount_coin = amount_in_base_currency / price
    if costs_enabled and action == 'buy':
        fill_price = price * (1.0 + config.dry_run_slippage_rate)
        amount_coin = amount_in_base_currency / (
            fill_price * (1.0 + config.dry_run_fee_rate))
    else:
        fill_price = price * (
            1.0 - config.dry_run_slippage_rate
            if costs_enabled else 1.0)
    if costs_enabled and (
        not math.isfinite(fill_price) or fill_price <= 0
        or not math.isfinite(amount_coin) or amount_coin <= 0
    ):
        logger.warning("DRY-RUN trade calculation invalid for %s", coin)
        return None
    return fill_price, amount_coin


def dry_run_buy(
    config: Any,
    portfolio: Any,
    performance: dict[str, Any],
    hooks: DryRunHooks,
    coin: str,
    price: float,
    amount_in_base_currency: float,
    amount_coin: float,
    fill_price: float,
    costs_enabled: bool,
    signal_source: str,
    signal_confidence: float | None,
    recommendation: str,
    reason: str,
    *,
    logger: logging.Logger = logger,
) -> bool:
    """Dry-run buy branch of :func:`execute_dry_run_trade`."""
    if costs_enabled and (
        coin in portfolio.open_trades
        or portfolio.holdings.get(coin, 0.0) > 1e-12
    ):
        logger.warning("DRY-RUN buy skipped for existing %s position", coin)
        return False
    if portfolio.cash < amount_in_base_currency:
        logger.warning(
            f"Insufficient cash for DRY-RUN buy of {coin}: {portfolio.cash:.2f} < {amount_in_base_currency:.2f} {config.base_currency}")
        return False
    portfolio.cash -= amount_in_base_currency
    portfolio.holdings[coin] = portfolio.holdings.get(
        coin, 0.0) + amount_coin
    portfolio.add_trade(
        coin,
        fill_price,
        amount_coin,
        amount_in_base_currency,
        signal_source=signal_source,
        signal_confidence=signal_confidence,
        recommendation=recommendation,
    )
    if costs_enabled:
        portfolio.open_trades[coin]['execution_cost_mode'] = 'net_costs'
        portfolio.open_trades[coin]['entry_reference_price'] = price
    performance['buys'] += 1
    performance['by_source'][signal_source]['buys'] += 1
    hooks.register_buy_timestamp()
    journal_row = {
        'coin': coin,
        'action': 'buy',
        'price': fill_price,
        'amount_coin': amount_coin,
        'amount_base': amount_in_base_currency,
        'signal_source': signal_source,
        'signal_confidence': signal_confidence,
        'recommendation': recommendation,
        'reason': reason,
        'dry_run': True,
        'execution_cost_mode': 'net_costs' if costs_enabled else 'gross_legacy',
        'reference_price': price,
        'fee_base': amount_coin * fill_price * config.dry_run_fee_rate if costs_enabled else 0.0,
        'slippage_base': amount_coin * (fill_price - price) if costs_enabled else 0.0,
    }
    # Persist portfolio state after successful dry-run buy
    if hooks.public_paper_mode():
        hooks.commit_paper_trade(journal_row)
    else:
        hooks.append_trade_journal(journal_row)
        portfolio.save_state()
    return True


def dry_run_sell(
    config: Any,
    portfolio: Any,
    hooks: DryRunHooks,
    coin: str,
    price: float,
    amount_coin: float,
    fill_price: float,
    costs_enabled: bool,
    net_tracked: bool,
    tracked_entry: dict,
    reason: str,
    partial_take_profit: bool,
    *,
    logger: logging.Logger = logger,
) -> bool:
    """Dry-run sell branch of :func:`execute_dry_run_trade`."""
    held = portfolio.holdings.get(coin, 0.0)
    sell_amount = min(amount_coin, held)
    entry_trade = tracked_entry
    resolved_entry = resolve_dry_run_sell_entry(
        coin, entry_trade, held, sell_amount, costs_enabled, net_tracked,
        hooks.entry_cost_basis, logger=logger)
    if resolved_entry is None:
        return False
    entry_cost_mode, entry_basis, sell_amount = resolved_entry
    if sell_amount <= 0:
        logger.warning(
            f"DRY-RUN sell skipped, no holdings for {coin}.")
        return False
    proceeds_and_fee = dry_run_sell_proceeds(
        config, coin, sell_amount, fill_price, costs_enabled, net_tracked,
        logger=logger)
    if proceeds_and_fee is None:
        return False
    proceeds, fee_base = proceeds_and_fee
    portfolio.cash += proceeds
    apply_dry_run_sell_to_holdings(portfolio, coin, held, sell_amount)

    original_amount_coin = float(
        entry_trade.get('amount_coin', sell_amount))
    remaining_open_amount = max(
        0.0, original_amount_coin - sell_amount)
    remaining_ratio = (
        remaining_open_amount / original_amount_coin
        if original_amount_coin > 0 else 0.0
    )
    pnl_base, pnl_pct, hold_seconds = record_dry_run_sell_close(
        hooks.record_close_performance, entry_trade, fill_price, sell_amount,
        proceeds, entry_basis, costs_enabled, entry_cost_mode)
    gross_pnl_base, net_pnl_base = dry_run_sell_gross_net_pnl(
        entry_trade, price, sell_amount, pnl_base, entry_cost_mode)
    mfe_pct, mae_pct = hooks.trade_excursion_metrics(entry_trade)
    journal_row = {
        'coin': coin,
        'action': 'sell',
        'price': fill_price,
        'amount_coin': sell_amount,
        'amount_base': proceeds,
        'pnl_base': pnl_base,
        'pnl_pct': pnl_pct,
        'hold_seconds': hold_seconds,
        'mfe_pct': mfe_pct,
        'mae_pct': mae_pct,
        'signal_source': entry_trade.get('signal_source', 'rules'),
        'signal_confidence': entry_trade.get('signal_confidence', ''),
        'recommendation': entry_trade.get('recommendation', ''),
        'reason': reason,
        'dry_run': True,
        'execution_cost_mode': 'net_costs' if costs_enabled else 'gross_legacy',
        'entry_cost_mode': entry_cost_mode,
        'reference_price': price,
        'fee_base': fee_base,
        'slippage_base': sell_amount * (price - fill_price) if costs_enabled else 0.0,
        'gross_pnl_base': gross_pnl_base,
        'net_pnl_base': net_pnl_base,
    }
    if not hooks.public_paper_mode():
        hooks.append_trade_journal(journal_row)
    hooks.register_sell_timestamp(coin)
    update_open_trades_after_dry_run_sell(
        portfolio, hooks.public_paper_mode, hooks.now,
        coin, entry_trade, remaining_open_amount, remaining_ratio,
        entry_basis, costs_enabled, entry_cost_mode, partial_take_profit)
    # Persist portfolio state after successful dry-run sell
    if hooks.public_paper_mode():
        hooks.commit_paper_trade(journal_row)
    else:
        portfolio.save_state()
    return True


def resolve_dry_run_sell_entry(
    coin: str, entry_trade: dict, held: float,
    sell_amount: float, costs_enabled: bool, net_tracked: bool,
    entry_cost_basis: Callable[[dict, float, float], float],
    *,
    logger: logging.Logger = logger,
) -> tuple[str, float | None, float] | None:
    """Validate the tracked entry and cap the sell amount to its quantity.

    Returns (entry_cost_mode, entry_basis, sell_amount) or *None*
    when the sell must be skipped.
    """
    if costs_enabled and (not isinstance(entry_trade, dict) or not entry_trade):
        logger.warning("DRY-RUN sell skipped for %s: no tracked entry cost basis", coin)
        return None
    entry_cost_mode = entry_trade.get('execution_cost_mode', 'gross_legacy')
    entry_basis = None
    if costs_enabled or net_tracked:
        try:
            entry_quantity = float(entry_trade.get('amount_coin'))
            entry_price = float(entry_trade.get('buy_price'))
            if (not math.isfinite(entry_quantity) or entry_quantity <= 0
                    or not math.isfinite(entry_price) or entry_price <= 0
                    or not math.isfinite(held) or held <= 0):
                raise ValueError("Invalid entry price, quantity or holdings")
            entry_basis = entry_cost_basis(entry_trade, entry_price, entry_quantity)
        except (TypeError, ValueError, OverflowError) as exc:
            logger.warning("DRY-RUN sell skipped for %s: invalid entry cost basis or position: %s", coin, exc)
            return None
        sell_amount = min(sell_amount, entry_quantity)
    return entry_cost_mode, entry_basis, sell_amount


def dry_run_sell_proceeds(
    config: Any,
    coin: str,
    sell_amount: float,
    fill_price: float,
    costs_enabled: bool,
    net_tracked: bool,
    *,
    logger: logging.Logger = logger,
) -> tuple[float, float] | None:
    """Net proceeds and fee for a dry-run sell, *None* when invalid."""
    gross_proceeds = sell_amount * fill_price
    fee_base = gross_proceeds * config.dry_run_fee_rate if costs_enabled else 0.0
    proceeds = gross_proceeds - fee_base
    if (costs_enabled or net_tracked) and (not math.isfinite(proceeds) or proceeds <= 0):
        logger.warning("DRY-RUN proceeds invalid for %s", coin)
        return None
    return proceeds, fee_base


def apply_dry_run_sell_to_holdings(
    portfolio: Any, coin: str, held: float, sell_amount: float
) -> None:
    """Reduce or remove the coin holding after a dry-run sell."""
    remaining = held - sell_amount
    if remaining > 1e-12:
        portfolio.holdings[coin] = remaining
    elif coin in portfolio.holdings:
        del portfolio.holdings[coin]


def record_dry_run_sell_close(
    record_close_performance: Callable[..., tuple[float, float, float]],
    entry_trade: dict, fill_price: float, sell_amount: float,
    proceeds: float, entry_basis: float | None, costs_enabled: bool,
    entry_cost_mode: str,
) -> tuple[float, float, float]:
    """Record close performance, net-aware when costs are tracked."""
    if costs_enabled or entry_cost_mode == 'net_costs':
        return record_close_performance(
            entry_trade, fill_price, sell_amount, net_proceeds=proceeds,
            entry_basis=entry_basis)
    return record_close_performance(entry_trade, fill_price, sell_amount)


def dry_run_sell_gross_net_pnl(
    entry_trade: dict, price: float, sell_amount: float,
    pnl_base: float, entry_cost_mode: str,
) -> tuple[str | float, str | float]:
    """Gross/net PnL pair for the journal; empty strings when unavailable."""
    gross_pnl_base: str | float = ''
    net_pnl_base: str | float = ''
    if entry_cost_mode == 'net_costs':
        try:
            entry_reference = float(entry_trade.get('entry_reference_price'))
        except (TypeError, ValueError):
            entry_reference = 0.0
        if (math.isfinite(entry_reference) and entry_reference > 0
                and math.isfinite(price) and price > 0):
            gross_pnl = sell_amount * (price - entry_reference)
            if math.isfinite(gross_pnl) and math.isfinite(pnl_base):
                gross_pnl_base = gross_pnl
                net_pnl_base = pnl_base
    return gross_pnl_base, net_pnl_base


def update_open_trades_after_dry_run_sell(
    portfolio: Any,
    public_paper_mode: Callable[[], bool],
    now: Callable[[], datetime],
    coin: str, entry_trade: dict,
    remaining_open_amount: float, remaining_ratio: float,
    entry_basis: float | None, costs_enabled: bool,
    entry_cost_mode: str, partial_take_profit: bool,
) -> None:
    """Scale down the open trade after a partial sell, or remove it."""
    if remaining_open_amount <= 1e-12 or coin not in portfolio.open_trades:
        portfolio.remove_trade(coin)
        return
    if costs_enabled or entry_cost_mode == 'net_costs':
        # resolve_dry_run_sell_entry guarantees a float basis in this branch.
        remaining_basis = (entry_basis or 0.0) * remaining_ratio
    else:
        remaining_basis = float(entry_trade.get('amount_base', 0.0)) * remaining_ratio
    portfolio.open_trades[coin]['amount_coin'] = remaining_open_amount
    portfolio.open_trades[coin]['amount_base'] = remaining_basis
    if public_paper_mode() and partial_take_profit:
        portfolio.open_trades[coin]['partial_tp_taken'] = True
        portfolio.open_trades[coin]['partial_tp_timestamp'] = now()
