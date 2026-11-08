"""Opt-in integration tests; only a dedicated local test database is allowed."""

import os
from contextlib import ExitStack
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from src.trading_safety.dry_run_store import (
    DryRunStore,
    RecoveryConflict,
    RecoveryStoreError,
    Snapshot,
)


@pytest.fixture
def postgres_store():
    dsn = os.environ.get("TRADING_SAFETY_TEST_DSN")
    if not dsn:
        pytest.skip("TRADING_SAFETY_TEST_DSN not supplied")

    params = conninfo_to_dict(dsn)
    host = params.get("host")
    if (
        host not in {"127.0.0.1", "::1"}
        or params.get("hostaddr", host) != host
        or "service" in params
    ):
        pytest.fail("TRADING_SAFETY_TEST_DSN must specify a direct loopback IP")

    token = uuid4().hex
    schema = f"dry_run_test_{token}"
    stores = ExitStack()
    admin = psycopg.connect(
        dsn, hostaddr=host, options="-c search_path=pg_catalog",
        connect_timeout=3, autocommit=True,
    )
    try:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            connections = []

            def connect():
                conn = psycopg.connect(
                    dsn, hostaddr=host,
                    options=f"-c search_path={schema}",
                    connect_timeout=3,
                )
                connections.append(conn)
                return conn

            def open_store(scope):
                store = DryRunStore(connect, f"{token}:{scope}")
                stores.enter_context(store)
                return store

            yield open_store, admin, connections
        finally:
            try:
                stores.close()
            finally:
                admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(schema)
                ))
                assert admin.execute(
                    "SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = %s",
                    (schema,),
                ).fetchone() is None
    finally:
        admin.close()


def test_ownership_lock_release_and_restart(postgres_store):
    open_store, _, _ = postgres_store
    first = open_store("account")
    first.initialize()
    state = {"portfolio": {"cash": 42}, "risk": {"losses": 2}}
    assert first.save(state, 0) == 1
    with pytest.raises(RecoveryConflict, match="already owned"):
        open_store("account")
    assert first.load() == Snapshot(1, state)
    first.close()
    restarted = open_store("account")
    assert restarted.load() == Snapshot(1, state)
    assert restarted.save({"cash": 41}, 1) == 2
    restarted.close()
    assert open_store("account").load() == Snapshot(2, {"cash": 41})


@pytest.mark.parametrize("stale_version", [0, 1, 3])
def test_versions_conflict_and_restart_preserves_committed_snapshot(
    postgres_store, stale_version
):
    open_store, _, _ = postgres_store
    store = open_store("account")
    store.initialize()
    assert store.load() is None
    assert store.save({"cash": 20}, 0) == 1
    assert store.save({"cash": 19}, 1) == 2
    with pytest.raises(RecoveryConflict, match="version mismatch"):
        store.save({"cash": 0}, stale_version)
    with pytest.raises(RecoveryStoreError, match="closed"):
        store.load()
    restarted = open_store("account")
    assert restarted.load() == Snapshot(2, {"cash": 19})
    assert restarted.save({"cash": 18}, 2) == 3


def test_two_scopes_are_independent(postgres_store):
    open_store, _, _ = postgres_store
    first = open_store("first")
    first.initialize()
    second = open_store("second")
    assert first.load() is None
    assert second.load() is None
    assert first.save({"cash": 1}, 0) == 1
    assert second.save({"cash": 2}, 0) == 1
    assert first.save({"cash": 3}, 1) == 2
    assert second.load() == Snapshot(1, {"cash": 2})
    first.close()
    assert open_store("first").load() == Snapshot(2, {"cash": 3})
    with pytest.raises(RecoveryConflict, match="already owned"):
        open_store("second")
    assert second.load() == Snapshot(1, {"cash": 2})


def test_backend_disconnect_halts_store_and_allows_restart(postgres_store):
    open_store, admin, connections = postgres_store
    store = open_store("account")
    store.initialize()
    assert store.save({"cash": 5}, 0) == 1
    pid = connections[-1].info.backend_pid
    assert admin.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone() == (True,)
    with pytest.raises(RecoveryStoreError, match="outcome may be unknown"):
        store.save({"cash": 0}, 1)
    with pytest.raises(RecoveryStoreError, match="closed"):
        store.load()
    restarted = open_store("account")
    assert restarted.load() == Snapshot(1, {"cash": 5})
    assert restarted.save({"cash": 4}, 1) == 2
