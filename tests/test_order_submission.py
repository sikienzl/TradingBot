"""Unit tests for idempotent order submission (audit point 5).

A timed-out market order must never be submitted twice: the deterministic client
order id is what lets the caller find the original order instead.
"""
from __future__ import annotations

import logging

import ccxt
import pytest

from src.order_submission import (
    MAX_CLIENT_ORDER_ID_LENGTH,
    build_client_order_id,
    find_order_by_client_order_id,
    submit_market_order,
)


class _Exchange:
    def __init__(
        self,
        *,
        create_error: Exception | None = None,
        open_orders: list[dict] | None = None,
        closed_orders: list[dict] | None = None,
        lookup_error: Exception | None = None,
    ) -> None:
        self.create_error = create_error
        self.open_orders = open_orders
        self.closed_orders = closed_orders
        self.lookup_error = lookup_error
        self.submissions: list[tuple[str, str, float, dict | None]] = []

    def create_market_buy_order(self, symbol, amount, params=None):
        self.submissions.append(("buy", symbol, amount, params))
        if self.create_error:
            raise self.create_error
        return {"id": "o1", "status": "closed"}

    def create_market_sell_order(self, symbol, amount, params=None):
        self.submissions.append(("sell", symbol, amount, params))
        if self.create_error:
            raise self.create_error
        return {"id": "o2", "status": "closed"}

    def fetch_open_orders(self, symbol):
        if self.lookup_error:
            raise self.lookup_error
        return self.open_orders if self.open_orders is not None else []

    def fetch_closed_orders(self, symbol):
        if self.lookup_error:
            raise self.lookup_error
        return self.closed_orders if self.closed_orders is not None else []


# ---------------------------------------------------------------------------
# Client order ids
# ---------------------------------------------------------------------------
def test_id_is_stable_for_the_same_attempt():
    first = build_client_order_id("run-1", "abcdef1234", 7, "BTC", "buy")
    second = build_client_order_id("run-1", "abcdef1234", 7, "BTC", "buy")
    assert first == second
    assert first.startswith("tb-abcdef12-run-1-7-BTC-buy")


def test_id_differs_per_iteration_action_and_run():
    base = build_client_order_id("run-1", "abcd1234", 7, "BTC", "buy")
    assert base != build_client_order_id("run-1", "abcd1234", 8, "BTC", "buy")
    assert base != build_client_order_id("run-1", "abcd1234", 7, "BTC", "sell")
    assert base != build_client_order_id("run-2", "abcd1234", 7, "BTC", "buy")
    assert base != build_client_order_id("run-1", "abcd9999", 7, "BTC", "buy")


def test_id_is_sanitised_and_within_the_exchange_limit():
    token = build_client_order_id(
        "run id with spaces" * 4, "cfg/../hash", 3, "BTC/USD", "buy")
    assert len(token) <= MAX_CLIENT_ORDER_ID_LENGTH
    assert token[0].isalpha()
    assert all(character.isalnum() or character in "-_" for character in token)


def test_id_stays_alphabetic_with_degenerate_inputs():
    token = build_client_order_id("", "", 0, "", "")
    assert token and token[0].isalpha()
    assert len(token) <= MAX_CLIENT_ORDER_ID_LENGTH


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------
def test_submission_carries_the_client_order_id():
    exchange = _Exchange()
    order = submit_market_order(exchange, "buy", "BTC/EUR", 0.5, "tb-1")
    assert order["id"] == "o1"
    assert exchange.submissions == [("buy", "BTC/EUR", 0.5, {"clientOrderId": "tb-1"})]


def test_submission_without_id_keeps_the_plain_call():
    exchange = _Exchange()
    submit_market_order(exchange, "sell", "BTC/EUR", 0.5, None)
    assert exchange.submissions == [("sell", "BTC/EUR", 0.5, None)]


def test_unsupported_action_is_refused():
    with pytest.raises(ValueError, match="Unsupported order action"):
        submit_market_order(_Exchange(), "cancel", "BTC/EUR", 0.5, "tb-1")


# ---------------------------------------------------------------------------
# Recovery after an ambiguous submission
# ---------------------------------------------------------------------------
def test_network_error_recovers_the_order_it_already_submitted(caplog):
    exchange = _Exchange(
        create_error=ccxt.NetworkError("timeout"),
        closed_orders=[{"id": "o9", "status": "closed", "clientOrderId": "tb-1"}],
    )
    with caplog.at_level(logging.WARNING):
        order = submit_market_order(exchange, "buy", "BTC/EUR", 0.5, "tb-1")
    assert order["id"] == "o9"
    assert len(exchange.submissions) == 1
    assert "accounting for the original order instead of resubmitting" in caplog.text


def test_network_error_without_a_recoverable_order_is_re_raised():
    exchange = _Exchange(create_error=ccxt.NetworkError("timeout"))
    with pytest.raises(ccxt.NetworkError):
        submit_market_order(exchange, "buy", "BTC/EUR", 0.5, "tb-1")
    assert len(exchange.submissions) == 1


def test_lookup_errors_are_treated_as_not_found(caplog):
    exchange = _Exchange(
        create_error=ccxt.NetworkError("timeout"),
        lookup_error=ccxt.NetworkError("still down"),
    )
    with caplog.at_level(logging.WARNING), pytest.raises(ccxt.NetworkError):
        submit_market_order(exchange, "buy", "BTC/EUR", 0.5, "tb-1")
    assert "Could not look up order tb-1" in caplog.text


def test_other_exchange_errors_are_not_recovered():
    exchange = _Exchange(create_error=ccxt.ExchangeError("insufficient funds"))
    with pytest.raises(ccxt.ExchangeError):
        submit_market_order(exchange, "buy", "BTC/EUR", 0.5, "tb-1")
    assert exchange.submissions == [("buy", "BTC/EUR", 0.5, {"clientOrderId": "tb-1"})]


def test_network_error_without_id_is_re_raised_immediately():
    exchange = _Exchange(create_error=ccxt.NetworkError("timeout"))
    with pytest.raises(ccxt.NetworkError):
        submit_market_order(exchange, "buy", "BTC/EUR", 0.5, None)
    assert exchange.submissions == [("buy", "BTC/EUR", 0.5, None)]


# ---------------------------------------------------------------------------
# Lookup helper
# ---------------------------------------------------------------------------
def test_lookup_prefers_open_orders():
    exchange = _Exchange(open_orders=[{"id": "a", "clientOrderId": "tb-1"}])
    assert find_order_by_client_order_id(exchange, "BTC/EUR", "tb-1")["id"] == "a"


def test_lookup_ignores_other_orders():
    exchange = _Exchange(
        open_orders=[{"id": "a", "clientOrderId": "tb-2"}],
        closed_orders=[{"id": "b", "clientOrderId": "tb-1"}],
    )
    assert find_order_by_client_order_id(exchange, "BTC/EUR", "tb-1")["id"] == "b"


def test_lookup_without_id_or_exchange_is_not_found():
    assert find_order_by_client_order_id(None, "BTC/EUR", "tb-1") is None
    assert find_order_by_client_order_id(_Exchange(), "BTC/EUR", "") is None


def test_lookup_tolerates_malformed_responses():
    exchange = _Exchange(open_orders=["junk", {"id": "b"}], closed_orders="not-a-list")
    assert find_order_by_client_order_id(exchange, "BTC/EUR", "tb-1") is None
