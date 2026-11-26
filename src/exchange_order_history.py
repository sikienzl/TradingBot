"""Reads recent order history from the exchange for fill reconciliation.

Layer-1 helper for :mod:`src.fill_reconciliation`. Everything that touches the
exchange lives here: building an authenticated client, paging the order-history
endpoints, normalising a ccxt order dict, and resolving one
``clientOrderId`` with an explicit server-side filter.

Every failure is recorded instead of raised. An incomplete history must show up
as an incomplete reconciliation, never as a matching one - a reconciliation that
crashes the bot would be worse than one that reports a gap.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import ccxt

from src.execution_costs import fee_amount_in_quote
from src.journal_order_reader import JournalOrder, to_float
from src.order_submission import find_order_by_client_order_id
from src.portfolio_bootstrap import currency_aliases

logger = logging.getLogger(__name__)

# Kraken returns at most this many orders per call; the reconciliation asks for
# one page per scope and resolves the rest by explicit lookup.
DEFAULT_ORDER_PAGE_LIMIT = 100

# Upper bound on per-scope history calls, so a wide journal cannot turn into an
# unbounded burst of private requests.
MAX_SYMBOL_SCOPES = 12


@dataclass(frozen=True)
class ExchangeOrder:
    """One order as the exchange reports it."""

    client_order_id: str
    order_id: str
    status: str
    symbol: str
    side: str
    filled: float | None
    amount: float | None
    average: float | None
    cost: float | None
    fee_base: float | None
    fee_raw: str
    closed_at: str


def build_exchange(
    exchange_name: str,
    api_key: str,
    api_secret: str,
) -> Any:
    """Creates a rate-limited exchange client for the reconciliation.

    Returns ``None`` when the account is not authenticated: a paper deployment
    has no credentials by design, and the reconciliation must report that rather
    than attempt an anonymous private call.
    """
    if not api_key or not api_secret:
        return None
    exchange_class = getattr(ccxt, exchange_name, None)
    if exchange_class is None:
        return None
    return exchange_class({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {"adjustForTimeDifference": True, "verbose": False},
    })


def currency_matches(currency: str, coin: str) -> bool:
    return currency in currency_aliases(coin)


def order_currency_matches(order: ExchangeOrder, coin: str) -> bool:
    base, _separator, _quote = order.symbol.partition("/")
    return currency_matches(base.strip().upper(), coin)


def order_side_matches(order: ExchangeOrder, action: str) -> bool:
    side = order.side
    if side in ("buy", "sell"):
        return side == action.lower()
    # Kraken margin pairs report "marginBuy"/"marginSell"; the suffix still tells
    # us which direction the journal row should have been.
    return side.endswith(action.lower())


def order_timestamp(order: Mapping[str, Any]) -> str:
    for key in ("closeTimestamp", "updated", "timestamp"):
        raw = order.get(key)
        if raw is None:
            continue
        try:
            stamp = datetime.fromtimestamp(float(raw) / 1000.0, UTC)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        return stamp.isoformat()
    return ""


def parse_exchange_order(
    order: Mapping[str, Any],
    *,
    base_currency: str = "EUR",
    traded_asset: str = "",
) -> ExchangeOrder | None:
    """Normalise one ccxt order dict, or ``None`` when it is unusable.

    ``traded_asset`` is the coin the journal row is about: Kraken charges fees in
    either the quote or the base currency, and :func:`fee_amount_in_quote` needs
    both to express the fee in base-currency terms.
    """
    if not isinstance(order, Mapping):
        return None
    client_order_id = str(order.get("clientOrderId") or "").strip()
    if not client_order_id:
        return None
    average = to_float(order.get("average")) or to_float(order.get("price"))
    fee_base, fee_raw = fee_amount_in_quote(order, base_currency, traded_asset, average or 0.0)
    return ExchangeOrder(
        client_order_id=client_order_id,
        order_id=str(order.get("id") or ""),
        status=str(order.get("status") or "").strip().lower(),
        symbol=str(order.get("symbol") or ""),
        side=str(order.get("side") or "").strip().lower(),
        filled=to_float(order.get("filled")),
        amount=to_float(order.get("amount")),
        average=average,
        cost=to_float(order.get("cost")),
        fee_base=fee_base,
        fee_raw=fee_raw,
        closed_at=order_timestamp(order),
    )


def fetch_page(
    exchange: Any,
    method_name: str,
    symbol: str | None,
    since_ms: int | None,
    limit: int,
    errors: list[str],
) -> list[Mapping[str, Any]]:
    method = getattr(exchange, method_name, None)
    if not callable(method):
        errors.append(f"{method_name} unavailable")
        return []
    try:
        orders = method(symbol, since_ms, limit)
    except TypeError:
        # A minimal stub (or an older client) may not accept since/limit.
        try:
            orders = method(symbol)
        except (ccxt.BaseError, OSError, ValueError) as exc:
            errors.append(f"{method_name}: {exc}")
            return []
    except (ccxt.BaseError, OSError, ValueError) as exc:
        errors.append(f"{method_name}: {exc}")
        return []
    if not isinstance(orders, list):
        errors.append(f"{method_name}: unexpected payload")
        return []
    return [order for order in orders if isinstance(order, Mapping)]


def collect_exchange_orders(
    exchange: Any,
    since_ms: int | None,
    *,
    page_limit: int = DEFAULT_ORDER_PAGE_LIMIT,
    symbols: Sequence[str] = (),
    log: logging.Logger = logger,
) -> tuple[list[Mapping[str, Any]], int, int]:
    """Fetch the recent open and closed orders.

    Kraken's order endpoints are account-wide, so the default scope is one
    unscoped call per method; other clients require a market, which is what
    ``symbols`` provides.

    Returns the raw order dicts plus the number of exchange calls and errors.
    """
    errors: list[str] = []
    collected: list[Mapping[str, Any]] = []
    scopes: list[str | None] = list(symbols[:MAX_SYMBOL_SCOPES]) or [None]
    calls = 0
    for method_name in ("fetch_closed_orders", "fetch_open_orders"):
        if not callable(getattr(exchange, method_name, None)):
            errors.append(f"{method_name} unavailable")
            continue
        for symbol in scopes:
            calls += 1
            collected.extend(
                fetch_page(exchange, method_name, symbol, since_ms, page_limit, errors)
            )
    for error in errors:
        log.warning("Fill reconciliation could not read order history: %s", error)
    return collected, calls, len(errors)


def journal_symbols(
    orders: Iterable[JournalOrder],
    base_currency: str,
    configured: Sequence[str] = (),
) -> tuple[str, ...]:
    """The unified symbols the journal actually needs history for."""
    if configured:
        return tuple(configured[:MAX_SYMBOL_SCOPES])
    coins = {order.coin for order in orders if order.coin}
    return tuple(f"{coin}/{base_currency.upper()}" for coin in sorted(coins))[:MAX_SYMBOL_SCOPES]


def lookup_order_by_id(
    exchange: Any,
    journal: JournalOrder,
    base_currency: str,
    log: logging.Logger,
) -> tuple[ExchangeOrder | None, str]:
    """Resolve one id with an explicit Kraken ``cl_ord_id`` filter.

    Returns ``(order, error)``. A genuine "not found" is not an error; a failing
    endpoint is, because the caller must not report an unverified row as a
    mismatch it can act on. Kraken supports the server-side filter, which keeps
    this to one small request instead of paging the whole history again.
    """
    symbol = f"{journal.coin}/{base_currency.upper()}"
    method = getattr(exchange, "fetch_closed_orders", None)
    if callable(method):
        try:
            orders = method(symbol, None, 1, {"clientOrderId": journal.client_order_id})
        except (ccxt.BaseError, OSError, ValueError) as exc:
            return None, str(exc)
        except TypeError:
            pass  # client without params support: fall through to the generic search
        else:
            for raw in orders if isinstance(orders, list) else []:
                if not isinstance(raw, Mapping):
                    continue
                if str(raw.get("clientOrderId") or "").strip() != journal.client_order_id:
                    continue
                parsed = parse_exchange_order(
                    raw, base_currency=base_currency, traded_asset=journal.coin,
                )
                if parsed is not None:
                    return parsed, ""
            return None, ""
    found = find_order_by_client_order_id(exchange, symbol, journal.client_order_id, logger=log)
    if found is None:
        return None, ""
    return parse_exchange_order(found, base_currency=base_currency, traded_asset=journal.coin), ""


__all__ = [
    "DEFAULT_ORDER_PAGE_LIMIT",
    "MAX_SYMBOL_SCOPES",
    "ExchangeOrder",
    "build_exchange",
    "collect_exchange_orders",
    "currency_matches",
    "fetch_page",
    "journal_symbols",
    "lookup_order_by_id",
    "order_currency_matches",
    "order_side_matches",
    "order_timestamp",
    "parse_exchange_order",
]
