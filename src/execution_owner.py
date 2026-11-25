"""Local process ownership lock for a single trading account."""

from __future__ import annotations

import fcntl
import hashlib
import os
from typing import Any, Protocol, Self


class RemoteExecutionLease(Protocol):
    def acquire(self) -> None: ...
    def assert_owned(self) -> None: ...
    def release(self) -> None: ...


class PostgresAdvisoryExecutionLease:
    """Holds a PostgreSQL session advisory lock for one exchange account."""

    def __init__(self, database_url: str, account_id: str) -> None:
        if not database_url or not account_id.strip():
            raise ValueError("Cluster execution lease requires a database URL and account ID")
        key_bytes = hashlib.sha256(account_id.strip().encode("utf-8")).digest()[:8]
        self.lock_key = int.from_bytes(key_bytes, byteorder="big", signed=True)
        self.database_url = database_url
        self.connection: Any | None = None

    def acquire(self) -> None:
        import psycopg

        connection = psycopg.connect(self.database_url, autocommit=True)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", (self.lock_key,))
                row = cursor.fetchone()
            if not row or row[0] is not True:
                raise RuntimeError("Another cluster process owns this account's execution lease")
        except BaseException:
            connection.close()
            raise
        self.connection = connection

    def assert_owned(self) -> None:
        if self.connection is None or self.connection.closed:
            raise RuntimeError("Cluster execution lease connection is not active")
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (self.lock_key,))
            row = cursor.fetchone()
            if not row or row[0] is not True:
                raise RuntimeError("Cluster execution lease is no longer owned")
            cursor.execute("SELECT pg_advisory_unlock(%s)", (self.lock_key,))

    def release(self) -> None:
        if self.connection is None:
            return
        connection = self.connection
        self.connection = None
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (self.lock_key,))
        finally:
            connection.close()


class ExecutionOwnerLock:
    """Holds an exclusive non-blocking lock for the bot process lifetime."""

    def __init__(self, path: str, remote_lease: RemoteExecutionLease | None = None):
        self.path = path
        self.remote_lease = remote_lease
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self._descriptor is not None:
            return
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise RuntimeError(
                f"Another bot process owns the execution lock: {self.path}"
            ) from exc
        self._descriptor = descriptor
        try:
            if self.remote_lease is not None:
                self.remote_lease.acquire()
        except BaseException:
            self.release()
            raise

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, exception_type, exception, traceback) -> None:
        self.release()

    def release(self) -> None:
        if self._descriptor is None:
            return
        descriptor = self._descriptor
        self._descriptor = None
        try:
            if self.remote_lease is not None:
                self.remote_lease.release()
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def assert_owned(self) -> None:
        if self._descriptor is None:
            if self.remote_lease is not None:
                raise RuntimeError("Local execution lock is not held")
            return
        if self.remote_lease is not None:
            self.remote_lease.assert_owned()