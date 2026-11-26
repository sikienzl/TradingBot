"""Pre-trade execution-cost estimates and exchange fill accounting helpers.

Layer-1 collaborator: the live entry path needs a fee/spread/slippage estimate
*before* an order is approved, and the journal needs a stable way to describe an
exchange fill (including a partial fill) afterwards. Both are pure functions
here so the money maths can be unit-tested without an exchange, a ``BotConfig``
or a :class:`CryptoTradingBot`. :mod:`src.execution_service` owns the exchange
calls and passes every value in explicitly.
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from typing import Any, cast

logger = logging.getLogger(__name__)

#: Kraken spot taker fee used when the exchange reports no usable fee tier.
DEFAULT_TAKER_FEE_RATE = 0.0026
#: Adverse move assumed between the reference quote and a market fill.
DEFAULT_SLIPPAGE_RATE = 0.001
#: A cost budget below this share of the order is treated as not configured.
MIN_COST_BUDGET_BPS = 1.0

#: Journal columns describing the pre-trade estimate (added by ``init_journal``).
ESTIMATE_JOURNAL_FIELDS = (
    'est_fee_base', 'est_slippage_base', 'est_spread_base',
    'est_total_cost_base', 'est_cost_pct', 'est_taker_fee_rate',
    'est_slippage_rate', 'est_spread_bps',
)
#: Journal columns describing the requested quantity and a partial fill.
FILL_JOURNAL_FIELDS = ('requested_amount_coin', 'partial_fill')


def _finite_float(value: Any) -> float | None:
    """Returns ``value`` as a finite float, or ``None`` when that is impossible."""
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_float(value: Any) -> float | None:
    number = _finite_float(value)
    if number is None or number <= 0:
        return None
    return number


def fee_rate_from_market_info(market_info: Any, *, fallback: float = DEFAULT_TAKER_FEE_RATE) -> float:
    """Returns the exchange-reported taker fee for one market.

    ccxt exposes the Kraken schedule in two shapes: ``makerFee``/``takerFee`` as
    percentages on some responses, and ``maker``/``taker`` as decimal fractions
    on the parsed market. Anything missing, non-numeric or outside ``[0, 1)``
    falls back to the documented default tier so a cost estimate always exists.
    """
    if not isinstance(market_info, Mapping):
        return fallback
    for key in ('takerFee', 'taker_fee'):
        percent = _finite_float(market_info.get(key))
        if percent is None:
            continue
        rate = percent / 100.0
        if 0.0 <= rate < 1.0:
            return rate
    taker = _finite_float(market_info.get('taker'))
    if taker is not None and 0.0 <= taker < 1.0:
        return taker
    maker_percent = _finite_float(market_info.get('makerFee'))
    if maker_percent is not None and 0.0 <= maker_percent / 100.0 < 1.0:
        return maker_percent / 100.0
    maker = _finite_float(market_info.get('maker'))
    if maker is not None and 0.0 <= maker < 1.0:
        return maker
    return fallback


def resolve_execution_cost_rates(
    config: Any,
    market_info: Any = None,
) -> tuple[float, float]:
    """Returns the ``(taker_fee_rate, slippage_rate)`` used for cost estimates.

    An explicit operator setting wins, the exchange-reported market fee is used
    when available, and the documented default tier is the last resort.
    """
    fee_rate = _finite_float(getattr(config, 'live_taker_fee_rate', None))
    if fee_rate is None or not 0.0 <= fee_rate < 1.0:
        fee_rate = fee_rate_from_market_info(market_info)
    slippage = _finite_float(getattr(config, 'live_slippage_rate', None))
    if slippage is None or not 0.0 <= slippage < 1.0:
        slippage = DEFAULT_SLIPPAGE_RATE
    return fee_rate, slippage


def quote_from_market_data(market_data: Any, coin: str, price: float) -> dict[str, Any] | None:
    """Extracts the ``(bid, ask, spread_bps)`` observation recorded for ``coin``.

    Returns ``None`` when the per-iteration market data has no usable quote, so
    the caller can decide whether to ask the exchange for an order book instead.
    """
    if not isinstance(market_data, Mapping):
        return None
    return quote_from_record(market_data.get(coin), price, coin=coin)


def quote_from_record(record: Any, price: float, *, coin: str = "") -> dict[str, Any] | None:
    """Extracts the quote fields of one record that holds ``bid``/``ask`` directly.

    Both the per-iteration market-data entry and the market-analysis record carry
    these fields, so either can be the source of the spread cost observation.
    """
    if not isinstance(record, Mapping):
        return None
    bid = _positive_float(record.get('bid'))
    ask = _positive_float(record.get('ask'))
    spread_bps = _finite_float(record.get('spread_bps'))
    if spread_bps is not None and spread_bps < 0:
        spread_bps = None
    if bid is None and ask is None and spread_bps is None:
        return None
    if spread_bps is None and bid is not None and ask is not None and ask >= bid:
        mid = (ask + bid) / 2.0
        if mid > 0:
            spread_bps = (ask - bid) / mid * 10_000.0
    reference = _positive_float(price)
    if reference is not None and bid is not None and ask is not None:
        mid = (ask + bid) / 2.0
        if mid > 0 and abs(reference - mid) / mid > 0.05:
            logger.info(
                "Quote for %s (bid=%.8f ask=%.8f) does not match the reference "
                "price %.8f; spread cost is not applied to this order.",
                coin, bid, ask, reference,
            )
            return {'bid': bid, 'ask': ask, 'spread_bps': None}
    return {'bid': bid, 'ask': ask, 'spread_bps': spread_bps}


def estimate_execution_costs(
    coin: str,
    price: float,
    amount_in_base_currency: float,
    taker_fee_rate: float,
    slippage_rate: float,
    quote: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Estimates the fee, slippage and spread cost of a market order.

    ``None`` means the request itself is unusable; a ``None`` ``spread_bps``
    inside the estimate means only that the spread could not be observed.
    """
    reference_price = _positive_float(price)
    amount_base = _positive_float(amount_in_base_currency)
    fee_rate = _finite_float(taker_fee_rate)
    slip_rate = _finite_float(slippage_rate)
    if reference_price is None or amount_base is None:
        return None
    if fee_rate is None or fee_rate < 0 or slip_rate is None or slip_rate < 0:
        return None

    bid = ask = None
    spread_bps: float | None = None
    if isinstance(quote, Mapping):
        bid = _positive_float(quote.get('bid'))
        ask = _positive_float(quote.get('ask'))
        spread_bps = _finite_float(quote.get('spread_bps'))
        if spread_bps is not None and spread_bps < 0:
            spread_bps = None
        # A normalized quote (:func:`quote_from_record`) always carries the key,
        # so an explicit ``None`` means "the spread must not be charged" and must
        # not be re-derived from the bid/ask pair it kept for context.
        if (spread_bps is None and 'spread_bps' not in quote
                and bid is not None and ask is not None and ask >= bid):
            mid = (ask + bid) / 2.0
            if mid > 0:
                spread_bps = (ask - bid) / mid * 10_000.0

    fee_base = amount_base * fee_rate
    slippage_base = amount_base * slip_rate
    spread_base = amount_base * spread_bps / 10_000.0 if spread_bps is not None else None
    total_cost_base = fee_base + slippage_base + (spread_base or 0.0)
    return {
        'coin': coin,
        'reference_price': reference_price,
        'amount_base': amount_base,
        'taker_fee_rate': fee_rate,
        'slippage_rate': slip_rate,
        'bid': bid,
        'ask': ask,
        'spread_bps': spread_bps,
        'fee_base': fee_base,
        'slippage_base': slippage_base,
        'spread_base': spread_base,
        'total_cost_base': total_cost_base,
        'cost_pct': total_cost_base / amount_base * 100.0,
    }


def _cost_budget_bps(config: Any) -> float:
    budget = _finite_float(getattr(config, 'max_entry_cost_bps', 0.0))
    if budget is None or budget < MIN_COST_BUDGET_BPS:
        return 0.0
    return budget


def build_cost_estimate(
    config: Any,
    coin: str,
    price: float,
    amount_in_base_currency: float,
    *,
    market_info: Any = None,
    quote: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Builds an execution-cost estimate for one order.

    ``market_info`` supplies the exchange-reported fee tier and ``quote`` the
    observed bid/ask, so the same helper serves the pre-trade entry gate (which
    has a quote) and live order accounting (which may only have the market).
    """
    taker_fee_rate, slippage_rate = resolve_execution_cost_rates(config, market_info)
    return estimate_execution_costs(
        coin,
        price,
        amount_in_base_currency,
        taker_fee_rate,
        slippage_rate,
        quote,
    )


def execution_cost_block_reason(config: Any, estimate: Mapping[str, Any] | None) -> str | None:
    """Returns the cost-budget refusal, or ``None`` when the order may go out.

    The budget is compared against the whole estimated cost (fee + slippage +
    observed spread). The separate spread ceiling is enforced by the entry gate
    in :func:`src.execution_service._attempt_candidate_buy`, which keeps its
    established refusal code.
    """
    if estimate is None:
        return None
    budget_bps = _cost_budget_bps(config)
    if budget_bps <= 0:
        return None
    cost_bps = float(estimate['cost_pct']) * 100.0
    if cost_bps > budget_bps:
        return (
            f'estimated_cost_above_limit '
            f'({cost_bps:.1f}bps > {budget_bps:.1f}bps)'
        )
    return None


def format_execution_cost_estimate(estimate: Mapping[str, Any] | None) -> str:
    """Single-line summary of an estimate for log records."""
    if estimate is None:
        return 'cost estimate unavailable'
    spread = estimate.get('spread_bps')
    spread_text = 'n/a' if spread is None else f'{float(spread):.2f}bps'
    return (
        f"fee={estimate['fee_base']:.2f} "
        f"slippage={estimate['slippage_base']:.2f} "
        f"spread={spread_text} "
        f"total={estimate['total_cost_base']:.2f} "
        f"({estimate['cost_pct']:.3f}% of {estimate['amount_base']:.2f})"
    )


def execution_cost_journal_fields(estimate: Mapping[str, Any] | None) -> dict[str, Any]:
    """Journal columns describing the pre-trade estimate for one order."""
    if estimate is None:
        return {}
    spread_base = estimate.get('spread_base')
    spread_bps = estimate.get('spread_bps')
    return {
        'est_fee_base': round(float(estimate['fee_base']), 8),
        'est_slippage_base': round(float(estimate['slippage_base']), 8),
        'est_spread_base': '' if spread_base is None else round(float(spread_base), 8),
        'est_total_cost_base': round(float(estimate['total_cost_base']), 8),
        'est_cost_pct': round(float(estimate['cost_pct']), 6),
        'est_taker_fee_rate': round(float(estimate['taker_fee_rate']), 8),
        'est_slippage_rate': round(float(estimate['slippage_rate']), 8),
        'est_spread_bps': '' if spread_bps is None else round(float(spread_bps), 4),
    }


def normalize_requested_amount_coin(
    requested_amount_coin: Any,
    amount_in_base_currency: Any,
    price: Any,
) -> float:
    """Returns the quantity the exchange was asked to fill.

    Callers normally pass the quantity explicitly; the base-currency amount and
    the reference price are only a fallback so an older call site still gets a
    partial-fill comparison instead of silently reporting a full fill.
    """
    requested = _positive_float(requested_amount_coin)
    if requested is not None:
        return requested
    try:
        derived = float(cast(Any, amount_in_base_currency)) / float(cast(Any, price))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
    return derived if math.isfinite(derived) and derived > 0 else 0.0


def partial_fill_journal_fields(requested_amount_coin: Any, filled_amount_coin: float) -> dict[str, Any]:
    """Journal columns that keep a partial fill visible after the trade."""
    requested = _finite_float(requested_amount_coin)
    if requested is None or requested <= 0:
        return {'requested_amount_coin': '', 'partial_fill': ''}
    tolerance = max(1e-12, requested * 1e-8)
    return {
        'requested_amount_coin': requested,
        'partial_fill': int(filled_amount_coin < requested - tolerance),
    }


def fee_amount_in_quote(
    order: Mapping[str, Any],
    quote_currency: str,
    traded_asset: str,
    average_price: float,
) -> tuple[float | None, str]:
    """Converts the exchange-reported fees of one order into quote currency."""
    fees = order.get('fees')
    if fees is None:
        fee = order.get('fee')
        fees = [fee] if isinstance(fee, dict) else []
    if not isinstance(fees, list) or not fees:
        return None, ''

    total = 0.0
    for fee in fees:
        if not isinstance(fee, dict):
            return None, json.dumps(fees, sort_keys=True, default=str)
        try:
            amount = float(fee['cost'])
        except (KeyError, TypeError, ValueError):
            return None, json.dumps(fees, sort_keys=True, default=str)
        currency = str(fee.get('currency') or '').upper()
        if not math.isfinite(amount) or amount < 0 or not currency:
            return None, json.dumps(fees, sort_keys=True, default=str)
        if currency == quote_currency.upper():
            total += amount
        elif currency == traded_asset.upper():
            total += amount * average_price
        else:
            return None, json.dumps(fees, sort_keys=True, default=str)
    return total, json.dumps(fees, sort_keys=True, default=str)
