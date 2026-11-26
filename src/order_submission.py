"""Idempotent live order submission.

A market order that times out on the way to the exchange is the classic source
of duplicate orders: the bot does not know whether the exchange accepted it.
Every live order therefore carries a deterministic client order id, so an
ambiguous submission is resolved by looking the order up again instead of
resubmitting it. If the order cannot be found the caller keeps the existing
fail-closed behaviour and latches the kill switch.

Layer-1 module: the ccxt exchange is injected and every value is an explicit
argument, so the recovery paths are unit-tested against a stub exchange.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import ccxt

logger = logging.getLogger(__name__)

#: Kraken accepts a ``cl_ord_id`` of at most 64 characters.
MAX_CLIENT_ORDER_ID_LENGTH = 64
#: Prefix that keeps the id alphabetic, as Kraken requires.
CLIENT_ORDER_ID_PREFIX = "tb"

_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


def build_client_order_id(
    run_id: str,
    config_hash: str,
    iteration: int,
    coin: str,
    action: str,
    *,
    prefix: str = CLIENT_ORDER_ID_PREFIX,
) -> str:
    """Returns a stable id for one order attempt.

    The id is unique per process (``run_id``) and per loop iteration, and it is
    identical for every retry of the same attempt, which is what makes a retry
    safe: the exchange can only ever hold one order with that id.
    """
    token = f"{prefix}-{config_hash[:8]}-{run_id[:8]}-{int(iteration)}-{coin}-{action}"
    token = _UNSAFE_ID_CHARS.sub("-", token).strip("-_")
    if len(token) > MAX_CLIENT_ORDER_ID_LENGTH:
        token = token[:MAX_CLIENT_ORDER_ID_LENGTH].strip("-_")
    if not token or not token[0].isalpha():
        token = f"{prefix}-{token}"[:MAX_CLIENT_ORDER_ID_LENGTH].strip("-_")
    return token


def find_order_by_client_order_id(
    exchange: Any,
    symbol: str,
    client_order_id: str,
    *,
    logger: logging.Logger = logger,
) -> dict[str, Any] | None:
    """Searches open and closed orders for ``client_order_id``.

    Returns ``None`` when the order is not visible. A lookup error is reported
    and treated as "not found" so the caller fails closed; it never resubmits.
    """
    if exchange is None or not client_order_id:
        return None
    for method_name in ("fetch_open_orders", "fetch_closed_orders"):
        method = getattr(exchange, method_name, None)
        if method is None:
            continue
        try:
            orders = method(symbol)
        except (ccxt.BaseError, OSError, ValueError) as exc:
            logger.warning(
                "Could not look up order %s via %s for %s: %s",
                client_order_id, method_name, symbol, exc,
            )
            continue
        if not isinstance(orders, list):
            continue
        for order in orders:
            if not isinstance(order, dict):
                continue
            if str(order.get("clientOrderId") or "") == client_order_id:
                return order
    return None


def submit_market_order(
    exchange: Any,
    action: str,
    symbol: str,
    amount_coin: float,
    client_order_id: str | None,
    *,
    logger: logging.Logger = logger,
) -> dict[str, Any]:
    """Submits one market order with its idempotency key.

    On a network error after submission the order is looked up by its client
    order id. A found order is returned so the caller accounts for the fill it
    already caused; a missing order re-raises the original error, which makes
    the caller latch the kill switch. The order is never submitted twice.
    """
    if action == "buy":
        create = exchange.create_market_buy_order
    elif action == "sell":
        create = exchange.create_market_sell_order
    else:
        raise ValueError(f"Unsupported order action: {action!r}")

    try:
        if client_order_id:
            return create(symbol, amount_coin, {"clientOrderId": client_order_id})
        return create(symbol, amount_coin)
    except ccxt.NetworkError as exc:
        if not client_order_id:
            raise
        recovered = find_order_by_client_order_id(
            exchange, symbol, client_order_id, logger=logger)
        if recovered is None:
            logger.error(
                "Order %s (%s %s) has an unknown submission state after %s; "
                "it is not resubmitted.",
                client_order_id, action.upper(), symbol, exc,
            )
            raise
        logger.warning(
            "Recovered %s order %s (%s %s) after a submission error; "
            "accounting for the original order instead of resubmitting.",
            recovered.get("status") or "unknown", client_order_id,
            action.upper(), symbol,
        )
        return recovered
