import json
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from src.trading_safety.dry_run_store import (
    DryRunStore,
    RecoveryConflict,
    RecoveryStoreError,
    Snapshot,
)


class FakeConnection:
    def __init__(self, rows=()):
        self.autocommit = False
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.rows = iter(rows)
        self.statements = []
        self.closed = False
        self.fail_execute = False
        self.fail_commit = False
        self.fail_close = False
        self.commits = 0

    @contextmanager
    def transaction(self):
        yield
        if self.fail_commit:
            raise psycopg.OperationalError("ambiguous commit")
        self.commits += 1

    @contextmanager
    def cursor(self, **kwargs):
        yield self

    def execute(self, query, params=()):
        self.statements.append((query, params))
        if self.fail_execute:
            raise psycopg.OperationalError("database unavailable")

    def fetchone(self):
        return next(self.rows)

    def close(self):
        self.closed = True
        if self.fail_close:
            raise psycopg.OperationalError("close failed")


def test_snapshot_save_load_and_versioned_sql():
    conn = FakeConnection([(True,), None, (1,), (1, {"portfolio": {"cash": 10}}), (2,)])
    with DryRunStore(lambda: conn, "account:paper:EUR") as store:
        store.initialize()
        assert store.load() is None
        state = {"portfolio": {"cash": 10}}
        assert store.save(state, 0) == 1
        assert store.load() == Snapshot(1, state)
        assert store.save({"risk": {"losses": 1}}, 1) == 2
        insert = conn.statements[3]
        assert "ON CONFLICT (scope) DO NOTHING" in insert[0]
        assert json.loads(insert[1][1]) == state
        update = conn.statements[5]
        assert "AND version = %s" in update[0]
        assert update[1][1:] == ("account:paper:EUR", 1)
        assert conn.commits == 6
    assert conn.closed
    with pytest.raises(RecoveryStoreError, match="closed"):
        store.load()


def test_ownership_denial_closes_connection():
    conn = FakeConnection([(False,)])
    with pytest.raises(RecoveryConflict, match="already owned"):
        DryRunStore(lambda: conn, "same-scope")
    assert conn.closed


def test_ownership_key_is_stable_and_scope_specific():
    first, second, other = (FakeConnection([(True,)]) for _ in range(3))
    for conn, scope in ((first, "same"), (second, "same"), (other, "different")):
        DryRunStore(lambda conn=conn: conn, scope).close()
    assert first.statements[0][1] == second.statements[0][1]
    assert first.statements[0][1] != other.statements[0][1]


@pytest.mark.parametrize("failure", ["execute", "commit"])
def test_acquire_failure_closes_connection(failure):
    conn = FakeConnection([(True,)])
    setattr(conn, f"fail_{failure}", True)
    with pytest.raises(RecoveryStoreError, match="acquire"):
        DryRunStore(lambda: conn, "scope")
    assert conn.closed


def test_close_failure_is_logged_and_store_remains_invalid(caplog):
    conn = FakeConnection([(True,)])
    store = DryRunStore(lambda: conn, "scope")
    conn.fail_close = True
    with caplog.at_level(logging.ERROR, logger="src.trading_safety.dry_run_store"):
        store.close()
    assert "Failed to close PostgreSQL recovery connection (OperationalError)" in caplog.text
    assert "ownership release is uncertain" in caplog.text
    with pytest.raises(RecoveryStoreError, match="closed"):
        store.load()


def test_close_failure_does_not_mask_acquisition_error(caplog):
    conn = FakeConnection([(False,)])
    conn.fail_close = True
    with (
        caplog.at_level(logging.ERROR, logger="src.trading_safety.dry_run_store"),
        pytest.raises(RecoveryConflict, match="already owned"),
    ):
        DryRunStore(lambda: conn, "scope")
    assert "Failed to close PostgreSQL recovery connection" in caplog.text


def test_close_failure_does_not_mask_database_error(caplog):
    conn = FakeConnection([(True,)])
    store = DryRunStore(lambda: conn, "scope")
    conn.fail_execute = True
    conn.fail_close = True
    with (
        caplog.at_level(logging.ERROR, logger="src.trading_safety.dry_run_store"),
        pytest.raises(RecoveryStoreError, match="outcome may be unknown") as error,
    ):
        store.load()
    assert isinstance(error.value.__cause__, psycopg.OperationalError)
    assert str(error.value.__cause__) == "database unavailable"
    assert "Failed to close PostgreSQL recovery connection" in caplog.text
    with pytest.raises(RecoveryStoreError, match="closed"):
        store.load()


def test_close_failure_does_not_mask_context_body_error(caplog):
    conn = FakeConnection([(True,)])
    conn.fail_close = True
    with (
        caplog.at_level(logging.ERROR, logger="src.trading_safety.dry_run_store"),
        pytest.raises(ValueError, match="primary"),
        DryRunStore(lambda: conn, "scope"),
    ):
        raise ValueError("primary")
    assert "Failed to close PostgreSQL recovery connection" in caplog.text


@pytest.mark.parametrize("failure", ["execute", "commit"])
def test_database_failure_permanently_invalidates_session(failure):
    conn = FakeConnection([(True,), (1,)])
    store = DryRunStore(lambda: conn, "scope")
    setattr(conn, f"fail_{failure}", True)
    with pytest.raises(RecoveryStoreError, match="outcome may be unknown"):
        store.save({"cash": 100}, 0)
    assert conn.closed
    with pytest.raises(RecoveryStoreError, match="closed"):
        store.save({"cash": 100}, 0)


def test_stale_version_halts_the_session():
    conn = FakeConnection([(True,), None])
    store = DryRunStore(lambda: conn, "scope")
    with pytest.raises(RecoveryConflict, match="version mismatch"):
        store.save({}, 4)
    assert conn.closed
    with pytest.raises(RecoveryStoreError, match="closed"):
        store.load()


def test_invalid_snapshot_halts_the_session():
    conn = FakeConnection([(True,), (1, [])])
    store = DryRunStore(lambda: conn, "scope")
    with pytest.raises(RecoveryStoreError, match="invalid stored"):
        store.load()
    assert conn.closed


@pytest.mark.parametrize("version", [-1, True, 1.0])
def test_invalid_versions_are_rejected_before_writing(version):
    conn = FakeConnection([(True,)])
    store = DryRunStore(lambda: conn, "scope")
    with pytest.raises(ValueError):
        store.save({}, version)
    assert len(conn.statements) == 1
    store.close()


def test_non_finite_json_is_rejected_before_writing():
    conn = FakeConnection([(True,)])
    store = DryRunStore(lambda: conn, "scope")
    with pytest.raises(ValueError):
        store.save({"cash": float("nan")}, 0)
    assert len(conn.statements) == 1
    store.close()


def test_non_dict_state_is_rejected_before_writing():
    conn = FakeConnection([(True,)])
    store = DryRunStore(lambda: conn, "scope")
    with pytest.raises(TypeError, match="state must be a JSON object"):
        store.save([], 0)
    assert len(conn.statements) == 1
    store.close()


@pytest.mark.parametrize("invalid", ["autocommit", "in_transaction"])
def test_requires_a_fresh_transactional_connection(invalid):
    conn = FakeConnection()
    if invalid == "autocommit":
        conn.autocommit = True
    else:
        conn.info.transaction_status = TransactionStatus.INTRANS
    with pytest.raises(ValueError, match="idle and non-autocommit"):
        DryRunStore(lambda: conn, "scope")
    assert conn.closed
