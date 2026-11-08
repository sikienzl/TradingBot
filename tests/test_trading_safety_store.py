from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal as D
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from src.trading_safety import (
    Conflict,
    DatabaseError,
    Fill,
    InvalidTransition,
    Mode,
    NotFound,
    OrderIntent,
    OrderStatus,
    Scope,
    Side,
    TradingSafetyStore,
)

SCOPE = Scope("account", Mode.PAPER, "exchange")
INTENT = OrderIntent("client", "BTC/USD", Side.BUY, D(2), D(10))


def order_row(status="intent", exchange_id=None, filled="0", version=1):
    return ("client", "BTC/USD", "buy", D(2), D(10), status, exchange_id, D(filled), version)


class FakeCursor:
    def __init__(self, replies, events, fail_at=None):
        self.replies = iter(replies)
        self.events = events
        self.fail_at = fail_at
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))
        if self.fail_at == len(self.calls):
            raise psycopg.OperationalError("query failed")

    def fetchone(self):
        return next(self.replies)

    def fetchall(self):
        return next(self.replies)


class FakeConnection:
    autocommit = False
    info = SimpleNamespace(transaction_status=psycopg.pq.TransactionStatus.IDLE)

    def __init__(self, replies=(), fail_at=None, commit_error=False):
        self.events = []
        self.cur = FakeCursor(replies, self.events, fail_at)
        self.commit_error = commit_error

    @contextmanager
    def transaction(self):
        self.events.append("begin")
        try:
            yield
            if self.commit_error:
                raise psycopg.OperationalError("commit failed")
        except BaseException:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")

    def cursor(self):
        return self.cur

    def close(self):
        self.events.append("close")


def fake_store(*replies, fail_at=None, commit_error=False):
    conn = FakeConnection(replies, fail_at, commit_error)
    return TradingSafetyStore(lambda: conn), conn


def test_create_intent_commits_and_binds_untrusted_values():
    malicious = "id'); DROP TABLE safety_orders_v1; --"
    intent = OrderIntent(malicious, "BTC/USD", Side.BUY, D(2))
    row = (malicious, "BTC/USD", "buy", D(2), None, "intent", None, D(0), 1)
    store, conn = fake_store(row)
    assert store.create_intent(SCOPE, intent).intent == intent
    assert conn.events == ["begin", "commit", "close"]
    query, params = conn.cur.calls[0]
    assert malicious not in query
    assert malicious in params
    assert params[:3] == ("account", "paper", "exchange")


@pytest.mark.parametrize("fail_at,commit_error", [(1, False), (None, True)])
def test_query_and_commit_errors_never_return_success(fail_at, commit_error):
    store, conn = fake_store(order_row(), fail_at=fail_at, commit_error=commit_error)
    with pytest.raises(DatabaseError) as error:
        store.create_intent(SCOPE, INTENT)
    assert isinstance(error.value.__cause__, psycopg.Error)
    assert conn.events == ["begin", "rollback", "close"]


def test_connection_error_is_explicit():
    def connect():
        raise psycopg.OperationalError("offline")

    with pytest.raises(DatabaseError):
        TradingSafetyStore(connect).initialize()


def test_close_failure_after_commit_is_explicit():
    store, conn = fake_store(order_row())

    def close():
        raise psycopg.OperationalError("close failed")

    conn.close = close
    with pytest.raises(DatabaseError, match="outcome may be unknown"):
        store.create_intent(SCOPE, INTENT)
    assert conn.events == ["begin", "commit"]


def test_autocommit_rejected_and_connection_closed():
    store, conn = fake_store()
    conn.autocommit = True
    with pytest.raises(ValueError, match="autocommit"):
        store.initialize()
    assert conn.events == ["close"]


def test_existing_transaction_is_rejected():
    store, conn = fake_store()
    conn.info = SimpleNamespace(transaction_status=psycopg.pq.TransactionStatus.INTRANS)
    with pytest.raises(ValueError, match="already have a transaction"):
        store.initialize()
    assert conn.events == ["close"]


def test_initialize_is_one_transaction():
    store, conn = fake_store()
    store.initialize()
    assert len(conn.cur.calls) == 3
    assert conn.events == ["begin", "commit", "close"]


@pytest.mark.parametrize("current,target", [
    ("intent", OrderStatus.OPEN),
    ("intent", OrderStatus.UNKNOWN),
    ("submitting", OrderStatus.FILLED),
    ("filled", OrderStatus.OPEN),
    ("canceled", OrderStatus.SUBMITTING),
])
def test_invalid_transitions_roll_back(current, target):
    store, conn = fake_store(order_row(current, "ex"))
    with pytest.raises(InvalidTransition):
        store.transition(SCOPE, "client", 1, target)
    assert len(conn.cur.calls) == 1
    assert "FOR UPDATE" in conn.cur.calls[0][0]
    assert conn.events == ["begin", "rollback", "close"]


def test_transition_requires_correct_version_and_exchange_id():
    store, conn = fake_store(order_row("submitting", version=2))
    with pytest.raises(Conflict):
        store.transition(SCOPE, "client", 1, OrderStatus.OPEN, "ex")
    assert len(conn.cur.calls) == 1
    store, conn = fake_store(order_row("submitting"))
    with pytest.raises(InvalidTransition):
        store.transition(SCOPE, "client", 1, OrderStatus.OPEN)
    assert conn.events[-2:] == ["rollback", "close"]


def test_exchange_id_is_immutable_and_unknown_partial_reopens_partial():
    store, _ = fake_store(order_row("unknown", "ex", "1", 3))
    with pytest.raises(Conflict):
        store.transition(SCOPE, "client", 3, OrderStatus.OPEN, "different")
    store, conn = fake_store(
        order_row("unknown", "ex", "1", 3),
        order_row("partially_filled", "ex", "1", 4),
    )
    order = store.transition(SCOPE, "client", 3, OrderStatus.OPEN)
    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert conn.cur.calls[1][1][:2] == ("partially_filled", "ex")


def test_fills_commit_atomically_and_retries_do_not_update():
    fill = Fill("execution", D(1), D(10))
    store, conn = fake_store(
        order_row("open", "ex", "0", 2),
        ("execution",),
        order_row("partially_filled", "ex", "1", 3),
    )
    assert store.record_fill(SCOPE, "client", fill).filled_quantity == D(1)
    assert len(conn.cur.calls) == 3
    assert conn.events == ["begin", "commit", "close"]
    store, conn = fake_store(
        order_row("partially_filled", "ex", "1", 3),
        None,
        ("client", D(1), D(10)),
    )
    assert store.record_fill(SCOPE, "client", fill).version == 3
    assert len(conn.cur.calls) == 3
    assert conn.events[-2:] == ["commit", "close"]


def test_fill_update_failure_rolls_back_insert():
    store, conn = fake_store(order_row("open", "ex"), ("execution",), fail_at=3)
    with pytest.raises(DatabaseError):
        store.record_fill(SCOPE, "client", Fill("execution", D(1), D(10)))
    assert conn.events == ["begin", "rollback", "close"]


@pytest.mark.parametrize("existing", [
    ("other", D(1), D(10)), ("client", D(1), D(11)),
])
def test_fill_id_collision_rolls_back(existing):
    store, conn = fake_store(order_row("open", "ex"), None, existing)
    with pytest.raises(Conflict):
        store.record_fill(SCOPE, "client", Fill("execution", D(1), D(10)))
    assert conn.events[-2:] == ["rollback", "close"]


@pytest.mark.parametrize("status,exchange,filled,quantity,error", [
    ("intent", None, "0", "1", InvalidTransition),
    ("open", None, "0", "1", InvalidTransition),
    ("open", "ex", "1", "2", Conflict),
    ("rejected", "ex", "0", "1", InvalidTransition),
])
def test_invalid_fills_roll_back_insert(status, exchange, filled, quantity, error):
    store, conn = fake_store(order_row(status, exchange, filled), ("execution",))
    with pytest.raises(error):
        store.record_fill(SCOPE, "client", Fill("execution", D(quantity), D(10)))
    assert conn.events[-2:] == ["rollback", "close"]
    assert len(conn.cur.calls) == 2


@pytest.mark.parametrize("status,filled,expected", [
    ("open", "0", "partially_filled"),
    ("unknown", "0", "unknown"),
    ("canceled", "0", "canceled"),
    ("canceled", "1", "filled"),
])
def test_fill_state_derived_from_quantity(status, filled, expected):
    store, conn = fake_store(
        order_row(status, "ex", filled), ("execution",),
        order_row(expected, "ex", str(D(filled) + 1), 2),
    )
    assert store.record_fill(SCOPE, "client", Fill("execution", D(1), D(10))).status == expected
    assert conn.cur.calls[2][1][1] == expected


def test_risk_cas_and_missing_values_are_explicit():
    store, conn = fake_store((1, {"drawdown": 4}))
    assert store.save_risk(SCOPE, {"drawdown": 4}, 0).version == 1
    assert "ON CONFLICT DO NOTHING" in conn.cur.calls[0][0]
    store, conn = fake_store((2, {"drawdown": 5}))
    assert store.save_risk(SCOPE, {"drawdown": 5}, 1).version == 2
    assert conn.cur.calls[0][1][-1] == 1
    store, conn = fake_store(None)
    with pytest.raises(Conflict):
        store.save_risk(SCOPE, {}, 1)
    assert conn.events[-2:] == ["rollback", "close"]
    store, _ = fake_store(None)
    with pytest.raises(NotFound):
        store.load_risk(SCOPE)
    store, _ = fake_store((2, {"drawdown": 5}))
    assert store.load_risk(SCOPE).state == {"drawdown": 5}


def test_recovery_includes_all_nonterminal_states_and_scope():
    store, conn = fake_store([order_row("unknown"), order_row("open", "ex")])
    assert [o.status for o in store.recovery_orders(SCOPE)] == ["unknown", "open"]
    query, params = conn.cur.calls[0]
    for state in ("intent", "submitting", "unknown", "open", "partially_filled"):
        assert f"'{state}'" in query
    assert params == ("account", "paper", "exchange")


@pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), D(0), D(-1), 1.0])
def test_decimal_values_are_positive_finite_and_never_float(value):
    with pytest.raises(ValueError):
        OrderIntent("id", "BTC/USD", Side.BUY, value)
    with pytest.raises(ValueError):
        Fill("fill", value, D(1))


def test_json_and_scope_validation():
    with pytest.raises(TypeError):
        Scope("account", "live", "venue")
    with pytest.raises(ValueError):
        Scope("", Mode.LIVE, "venue")
    store, conn = fake_store()
    with pytest.raises(ValueError):
        store.save_risk(SCOPE, {"value": float("nan")}, 0)
    assert conn.events == []


@pytest.fixture
def postgres_store():
    """Opt in to a dedicated test DB; create/drop only a unique test schema."""
    dsn = os.getenv("TRADING_SAFETY_TEST_DSN")
    if not dsn:
        pytest.skip("TRADING_SAFETY_TEST_DSN not supplied")
    schema = "safety_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    def connect():
        return psycopg.connect(dsn, options=f"-c search_path={schema}")
    try:
        store = TradingSafetyStore(connect)
        store.initialize()
        yield store
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_real_postgres_order_fill_risk_and_isolation(postgres_store):
    store = postgres_store
    other = Scope("account", Mode.LIVE, "exchange")
    assert store.create_intent(SCOPE, INTENT).version == 1
    assert store.create_intent(other, INTENT).version == 1
    with pytest.raises(Conflict):
        store.create_intent(SCOPE, INTENT)
    order = store.transition(SCOPE, "client", 1, OrderStatus.SUBMITTING)
    order = store.transition(SCOPE, "client", order.version, OrderStatus.OPEN, "exchange-id")
    fill = Fill("execution", D(1), D(10))
    order = store.record_fill(SCOPE, "client", fill)
    assert store.record_fill(SCOPE, "client", fill) == order
    with pytest.raises(Conflict):
        store.record_fill(SCOPE, "client", Fill("execution", D(2), D(10)))
    assert store.get_order(SCOPE, "client") == order
    assert store.recovery_orders(SCOPE) == [order]
    assert store.recovery_orders(other)[0].status == OrderStatus.INTENT
    assert store.save_risk(SCOPE, {"risk": 3}, 0).version == 1
    with pytest.raises(Conflict):
        store.save_risk(SCOPE, {}, 0)
    assert store.save_risk(SCOPE, {"risk": 4}, 1).version == 2
    assert store.load_risk(SCOPE).state == {"risk": 4}
    with pytest.raises(NotFound):
        store.load_risk(other)


def test_real_postgres_unique_exchange_ids_and_rollback(postgres_store):
    store = postgres_store
    store.create_intent(SCOPE, INTENT)
    store.create_intent(SCOPE, OrderIntent("second", "BTC/USD", Side.SELL, D(1)))
    store.transition(SCOPE, "client", 1, OrderStatus.SUBMITTING)
    store.transition(SCOPE, "client", 2, OrderStatus.OPEN, "exchange-id")
    store.transition(SCOPE, "second", 1, OrderStatus.SUBMITTING)
    with pytest.raises(Conflict):
        store.transition(SCOPE, "second", 2, OrderStatus.OPEN, "exchange-id")
    assert store.get_order(SCOPE, "second").status == OrderStatus.SUBMITTING
    with pytest.raises(Conflict):
        store.record_fill(SCOPE, "client", Fill("overfill", D(3), D(10)))
    assert store.get_order(SCOPE, "client").filled_quantity == 0
    assert store.record_fill(SCOPE, "client", Fill("overfill", D(1), D(10))).filled_quantity == 1


def test_real_postgres_concurrent_risk_cas_and_duplicate_fills(postgres_store):
    store = postgres_store
    store.save_risk(SCOPE, {"risk": 0}, 0)
    barrier = Barrier(2)

    def update_risk(value):
        barrier.wait()
        try:
            return store.save_risk(SCOPE, {"risk": value}, 1).version
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update_risk, (1, 2)))
    assert results.count(2) == 1
    assert results.count("conflict") == 1

    store.create_intent(SCOPE, INTENT)
    store.transition(SCOPE, "client", 1, OrderStatus.SUBMITTING)
    store.transition(SCOPE, "client", 2, OrderStatus.OPEN, "exchange-id")
    barrier = Barrier(2)

    def record(_):
        barrier.wait()
        return store.record_fill(SCOPE, "client", Fill("execution", D(1), D(10)))

    with ThreadPoolExecutor(max_workers=2) as pool:
        orders = list(pool.map(record, (1, 2)))
    assert orders[0] == orders[1]
    assert orders[0].filled_quantity == 1
