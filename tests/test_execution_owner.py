from __future__ import annotations

import pytest

from src.execution_owner import ExecutionOwnerLock, PostgresAdvisoryExecutionLease


def test_execution_owner_lock_rejects_a_second_process_owner(tmp_path):
    lock_path = str(tmp_path / "bot.lock")
    first = ExecutionOwnerLock(lock_path)
    second = ExecutionOwnerLock(lock_path)

    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="Another bot process owns"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_postgres_lease_acquires_verifies_and_releases(monkeypatch, tmp_path):
    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, params):
            calls.append((query, params))

        def fetchone(self):
            return (True,)

    class Connection:
        closed = False

        def cursor(self):
            return Cursor()

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setitem(
        __import__("sys").modules,
        "psycopg",
        type("Psycopg", (), {"connect": staticmethod(lambda *_args, **_kwargs: connection)}),
    )
    lease = PostgresAdvisoryExecutionLease("postgresql://test", "kraken-account")
    owner = ExecutionOwnerLock(str(tmp_path / "bot.lock"), lease)

    owner.acquire()
    owner.assert_owned()
    owner.release()

    assert len(calls) == 4
    assert calls[0][0] == "SELECT pg_try_advisory_lock(%s)"
    assert calls[1][0] == "SELECT pg_try_advisory_lock(%s)"
    assert calls[2][0] == "SELECT pg_advisory_unlock(%s)"
    assert calls[3][0] == "SELECT pg_advisory_unlock(%s)"
    assert connection.closed is True


def test_postgres_lease_denial_releases_local_lock(monkeypatch, tmp_path):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, *_args):
            return None

        def fetchone(self):
            return (False,)

    class Connection:
        closed = False

        def cursor(self):
            return Cursor()

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setitem(
        __import__("sys").modules,
        "psycopg",
        type("Psycopg", (), {"connect": staticmethod(lambda *_args, **_kwargs: connection)}),
    )
    path = str(tmp_path / "bot.lock")
    owner = ExecutionOwnerLock(
        path, PostgresAdvisoryExecutionLease("postgresql://test", "account"))

    with pytest.raises(RuntimeError, match="Another cluster process"):
        owner.acquire()
    assert connection.closed is True

    replacement = ExecutionOwnerLock(path)
    replacement.acquire()
    replacement.release()