"""Transactional persistence primitives; this module does not send orders.

Each operation owns a fresh connection and transaction. A successful return
means commit succeeded; a connection error during commit has an ambiguous
outcome and must be reconciled, not blindly retried as a new order.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

import psycopg
from psycopg.pq import TransactionStatus


class DatabaseError(RuntimeError):
    """Database operation failed; commit outcome may be unknown."""


class Conflict(RuntimeError):
    """A version, identifier, or idempotency conflict."""


class NotFound(LookupError):
    """The requested record does not exist in this scope."""


class InvalidTransition(ValueError):
    """An order state transition violates the state machine."""


class Mode(StrEnum):
    LIVE = "live"
    PAPER = "paper"
    SIMULATION = "simulation"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    INTENT = "intent"
    SUBMITTING = "submitting"
    UNKNOWN = "unknown"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Scope:
    account: str
    mode: Mode
    venue: str

    def __post_init__(self) -> None:
        _text(self.account, "account")
        _text(self.venue, "venue")
        if not isinstance(self.mode, Mode):
            raise TypeError("mode must be a Mode")


@dataclass(frozen=True)
class OrderIntent:
    client_id: str
    symbol: str
    side: Side
    quantity: Decimal
    # None means market order; otherwise a positive limit price.
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        _text(self.client_id, "client_id")
        _text(self.symbol, "symbol")
        if not isinstance(self.side, Side):
            raise TypeError("side must be a Side")
        _positive(self.quantity, "quantity")
        if self.limit_price is not None:
            _positive(self.limit_price, "limit_price")


@dataclass(frozen=True)
class Order:
    scope: Scope
    intent: OrderIntent
    status: OrderStatus
    exchange_id: str | None
    filled_quantity: Decimal
    version: int


@dataclass(frozen=True)
class Fill:
    execution_id: str
    quantity: Decimal
    price: Decimal

    def __post_init__(self) -> None:
        _text(self.execution_id, "execution_id")
        _positive(self.quantity, "quantity")
        _positive(self.price, "price")


@dataclass(frozen=True)
class RiskSnapshot:
    version: int
    state: dict[str, Any]


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty string without NUL")


def _positive(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a positive finite Decimal")


_DDL = (
    """CREATE TABLE IF NOT EXISTS safety_orders_v1 (
        account TEXT NOT NULL, mode TEXT NOT NULL CHECK (mode IN ('live','paper','simulation')),
        venue TEXT NOT NULL, client_id TEXT NOT NULL, symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('buy','sell')),
        quantity NUMERIC NOT NULL CHECK (quantity > 0),
        limit_price NUMERIC CHECK (limit_price > 0),
        status TEXT NOT NULL CHECK (status IN
            ('intent','submitting','unknown','open','partially_filled','filled','canceled','rejected')),
        exchange_id TEXT, filled_quantity NUMERIC NOT NULL DEFAULT 0
            CHECK (filled_quantity >= 0 AND filled_quantity <= quantity),
        version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
        PRIMARY KEY (account, mode, venue, client_id),
        UNIQUE (account, mode, venue, exchange_id)
    )""",
    """CREATE TABLE IF NOT EXISTS safety_fills_v1 (
        account TEXT NOT NULL, mode TEXT NOT NULL, venue TEXT NOT NULL,
        execution_id TEXT NOT NULL, client_id TEXT NOT NULL,
        quantity NUMERIC NOT NULL CHECK (quantity > 0),
        price NUMERIC NOT NULL CHECK (price > 0),
        PRIMARY KEY (account, mode, venue, execution_id),
        FOREIGN KEY (account, mode, venue, client_id)
            REFERENCES safety_orders_v1 (account, mode, venue, client_id)
    )""",
    """CREATE TABLE IF NOT EXISTS safety_risk_v1 (
        account TEXT NOT NULL, mode TEXT NOT NULL, venue TEXT NOT NULL,
        version BIGINT NOT NULL CHECK (version > 0), state JSONB NOT NULL,
        PRIMARY KEY (account, mode, venue)
    )""",
)

_COLUMNS = "client_id, symbol, side, quantity, limit_price, status, exchange_id, filled_quantity, version"
_WHERE = "account = %s AND mode = %s AND venue = %s AND client_id = %s"
_ALLOWED = {
    OrderStatus.INTENT: {OrderStatus.SUBMITTING},
    OrderStatus.SUBMITTING: {OrderStatus.UNKNOWN, OrderStatus.OPEN, OrderStatus.REJECTED},
    OrderStatus.UNKNOWN: {OrderStatus.OPEN, OrderStatus.CANCELED, OrderStatus.REJECTED},
    OrderStatus.OPEN: {OrderStatus.UNKNOWN, OrderStatus.CANCELED},
    OrderStatus.PARTIALLY_FILLED: {OrderStatus.UNKNOWN, OrderStatus.CANCELED},
}


class TradingSafetyStore:
    """Use a dedicated, trusted PostgreSQL schema/search_path.

    ``connect`` must return a fresh psycopg connection, not a shared connection.
    ``initialize`` explicitly creates v1 tables; no automatic migration occurs.
    """

    def __init__(self, connect: Callable[[], psycopg.Connection[Any]]) -> None:
        self._connect = connect

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        conn = None
        try:
            conn = self._connect()
            if conn.autocommit:
                raise ValueError("autocommit connections are not supported")
            if conn.info.transaction_status != TransactionStatus.IDLE:
                raise ValueError("connection must not already have a transaction")
            with conn.transaction(), conn.cursor() as cur:
                yield cur
        except psycopg.errors.UniqueViolation as exc:
            raise Conflict("identifier already exists in this scope") from exc
        except psycopg.Error as exc:
            raise DatabaseError("PostgreSQL operation failed; outcome may be unknown") from exc
        finally:
            if conn is not None:
                handling_error = sys.exc_info()[0] is not None
                try:
                    conn.close()
                except psycopg.Error as exc:
                    if not handling_error:
                        raise DatabaseError("connection close failed; outcome may be unknown") from exc

    def initialize(self) -> None:
        with self._transaction() as cur:
            for statement in _DDL:
                cur.execute(statement)

    def create_intent(self, scope: Scope, intent: OrderIntent) -> Order:
        with self._transaction() as cur:
            cur.execute(
                f"""INSERT INTO safety_orders_v1
                    (account, mode, venue, client_id, symbol, side, quantity, limit_price, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'intent')
                    RETURNING {_COLUMNS}""",
                (*_scope(scope), intent.client_id, intent.symbol, intent.side.value,
                 intent.quantity, intent.limit_price),
            )
            result = _order(scope, cur.fetchone())
        return result

    def get_order(self, scope: Scope, client_id: str) -> Order:
        _text(client_id, "client_id")
        with self._transaction() as cur:
            result = self._get(cur, scope, client_id)
        return result

    def recovery_orders(self, scope: Scope) -> list[Order]:
        """Return every nonterminal intent/submission/unknown/open order."""
        with self._transaction() as cur:
            cur.execute(
                f"""SELECT {_COLUMNS} FROM safety_orders_v1
                    WHERE account = %s AND mode = %s AND venue = %s
                    AND status IN ('intent','submitting','unknown','open','partially_filled')
                    ORDER BY client_id""", _scope(scope),
            )
            result = [_order(scope, row) for row in cur.fetchall()]
        return result

    def transition(
        self, scope: Scope, client_id: str, expected_version: int,
        status: OrderStatus, exchange_id: str | None = None,
    ) -> Order:
        _text(client_id, "client_id")
        if not isinstance(status, OrderStatus):
            raise TypeError("status must be an OrderStatus")
        if exchange_id is not None:
            _text(exchange_id, "exchange_id")
        with self._transaction() as cur:
            order = self._get(cur, scope, client_id, lock=True)
            _version(order.version, expected_version)
            if status not in _ALLOWED.get(order.status, set()):
                raise InvalidTransition(f"{order.status} -> {status} is not allowed")
            if order.exchange_id is not None and exchange_id not in (None, order.exchange_id):
                raise Conflict("exchange ID cannot be changed")
            effective_id = exchange_id or order.exchange_id
            if status == OrderStatus.OPEN and not effective_id:
                raise InvalidTransition("open orders require an exchange ID")
            if status == OrderStatus.REJECTED and order.filled_quantity:
                raise InvalidTransition("an order with fills cannot be rejected")
            # An unknown partially-filled order must resume as partially filled.
            if status == OrderStatus.OPEN and order.filled_quantity:
                status = OrderStatus.PARTIALLY_FILLED
            cur.execute(
                f"""UPDATE safety_orders_v1 SET status = %s, exchange_id = %s,
                    version = version + 1 WHERE {_WHERE} RETURNING {_COLUMNS}""",
                (status.value, effective_id, *_scope(scope), client_id),
            )
            result = _order(scope, cur.fetchone())
        return result

    def record_fill(self, scope: Scope, client_id: str, fill: Fill) -> Order:
        """Record a venue execution exactly once; conflicting duplicates fail."""
        _text(client_id, "client_id")
        with self._transaction() as cur:
            order = self._get(cur, scope, client_id, lock=True)
            cur.execute(
                """INSERT INTO safety_fills_v1
                   (account, mode, venue, execution_id, client_id, quantity, price)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (account, mode, venue, execution_id) DO NOTHING
                   RETURNING execution_id""",
                (*_scope(scope), fill.execution_id, client_id, fill.quantity, fill.price),
            )
            if cur.fetchone() is None:
                cur.execute(
                    """SELECT client_id, quantity, price FROM safety_fills_v1
                       WHERE account = %s AND mode = %s AND venue = %s AND execution_id = %s""",
                    (*_scope(scope), fill.execution_id),
                )
                if cur.fetchone() != (client_id, fill.quantity, fill.price):
                    raise Conflict("execution ID already has different fill data")
                return order
            if order.status not in {
                OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN,
                OrderStatus.CANCELED,
            } or not order.exchange_id:
                raise InvalidTransition("fill requires an identified exchange order")
            total = order.filled_quantity + fill.quantity
            if total > order.intent.quantity:
                raise Conflict("fill exceeds order quantity")
            if total == order.intent.quantity:
                status = OrderStatus.FILLED
            elif order.status in {OrderStatus.UNKNOWN, OrderStatus.CANCELED}:
                status = order.status
            else:
                status = OrderStatus.PARTIALLY_FILLED
            cur.execute(
                f"""UPDATE safety_orders_v1 SET filled_quantity = %s, status = %s,
                    version = version + 1 WHERE {_WHERE} RETURNING {_COLUMNS}""",
                (total, status.value, *_scope(scope), client_id),
            )
            result = _order(scope, cur.fetchone())
        return result

    def save_risk(self, scope: Scope, state: dict[str, Any], expected_version: int) -> RiskSnapshot:
        """Compare-and-swap a JSON object; expected_version=0 creates it."""
        if not isinstance(state, dict):
            raise TypeError("risk state must be a JSON object")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a nonnegative integer")
        payload = json.dumps(state, allow_nan=False)
        with self._transaction() as cur:
            if expected_version == 0:
                cur.execute(
                    """INSERT INTO safety_risk_v1 (account, mode, venue, version, state)
                       VALUES (%s, %s, %s, 1, %s::jsonb)
                       ON CONFLICT DO NOTHING RETURNING version, state""",
                    (*_scope(scope), payload),
                )
            else:
                cur.execute(
                    """UPDATE safety_risk_v1 SET version = version + 1, state = %s::jsonb
                       WHERE account = %s AND mode = %s AND venue = %s AND version = %s
                       RETURNING version, state""",
                    (payload, *_scope(scope), expected_version),
                )
            row = cur.fetchone()
            if row is None:
                raise Conflict("risk version conflict")
            result = RiskSnapshot(*row)
        return result

    def load_risk(self, scope: Scope) -> RiskSnapshot:
        with self._transaction() as cur:
            cur.execute(
                """SELECT version, state FROM safety_risk_v1
                   WHERE account = %s AND mode = %s AND venue = %s""", _scope(scope),
            )
            row = cur.fetchone()
            if row is None:
                raise NotFound("risk state not found")
            result = RiskSnapshot(*row)
        return result

    @staticmethod
    def _get(cur: Any, scope: Scope, client_id: str, *, lock: bool = False) -> Order:
        cur.execute(
            f"SELECT {_COLUMNS} FROM safety_orders_v1 WHERE {_WHERE}" + (" FOR UPDATE" if lock else ""),
            (*_scope(scope), client_id),
        )
        row = cur.fetchone()
        if row is None:
            raise NotFound("order not found")
        return _order(scope, row)


def _scope(scope: Scope) -> tuple[str, str, str]:
    if not isinstance(scope, Scope):
        raise TypeError("scope must be a Scope")
    return scope.account, scope.mode.value, scope.venue


def _order(scope: Scope, row: tuple[Any, ...]) -> Order:
    client_id, symbol, side, quantity, price, status, exchange_id, filled, version = row
    return Order(scope, OrderIntent(client_id, symbol, Side(side), quantity, price),
                 OrderStatus(status), exchange_id, filled, version)


def _version(actual: int, expected: int) -> None:
    if type(expected) is not int or expected < 1:
        raise ValueError("expected_version must be a positive integer")
    if actual != expected:
        raise Conflict("order version conflict")
