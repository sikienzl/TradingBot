"""Experimental snapshot storage; not connected to the trading bot.

The caller must validate the domain state and stop on every persistence error.
This is not an execution log or a complete recovery protocol.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Self

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import tuple_row

logger = logging.getLogger(__name__)


class RecoveryStoreError(RuntimeError):
    """The store cannot safely continue; the commit outcome may be unknown."""


class RecoveryConflict(RecoveryStoreError):
    """The scope is already owned or its snapshot version has changed."""


@dataclass(frozen=True)
class Snapshot:
    version: int
    state: dict[str, Any]


class DryRunStore:
    """Single-session snapshot primitive for cooperating dry-run writers only.

    The factory must return a fresh, direct, idle non-autocommit connection with
    a trusted search_path. Do not use transaction-pooling proxies, automatic
    reconnect, failover without fencing, or the same connection elsewhere.
    """

    def __init__(self, connect: Callable[[], psycopg.Connection], scope: str):
        if not isinstance(scope, str) or not scope.strip() or "\x00" in scope:
            raise ValueError("scope must be a non-empty string without NUL")
        self._scope = scope
        self._mutex = threading.Lock()
        self._conn = None
        # A hash collision only denies access to a different scope.
        key = int.from_bytes(
            hashlib.sha256(("dry-run-store-v1:" + scope).encode()).digest()[:8],
            byteorder="big", signed=True,
        )
        try:
            self._conn = connect()
            if self._conn.autocommit or self._conn.info.transaction_status != TransactionStatus.IDLE:
                raise ValueError("connection must be idle and non-autocommit")
            with self._conn.transaction(), self._conn.cursor(row_factory=tuple_row) as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                if cur.fetchone() != (True,):
                    raise RecoveryConflict("dry-run scope is already owned")
        except BaseException as exc:
            self._discard()
            if isinstance(exc, psycopg.Error):
                raise RecoveryStoreError("failed to acquire PostgreSQL ownership") from exc
            raise

    def _discard(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except psycopg.Error as exc:
                logger.error(
                    "Failed to close PostgreSQL recovery connection (%s); "
                    "session ownership release is uncertain.",
                    type(exc).__name__,
                )

    def close(self) -> None:
        with self._mutex:
            self._discard()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _run(self, operation: Callable[[Any], Any]) -> Any:
        with self._mutex:
            if self._conn is None:
                raise RecoveryStoreError("store is closed or failed")
            try:
                with self._conn.transaction(), self._conn.cursor(row_factory=tuple_row) as cur:
                    result = operation(cur)
                return result
            except BaseException as exc:
                self._discard()
                if isinstance(exc, psycopg.Error):
                    raise RecoveryStoreError(
                        "PostgreSQL operation failed; commit outcome may be unknown"
                    ) from exc
                raise

    def initialize(self) -> None:
        """Create the table in a pre-existing, trusted schema; not a migration."""
        self._run(lambda cur: cur.execute(
            """CREATE TABLE IF NOT EXISTS dry_run_snapshots_v1 (
                scope TEXT PRIMARY KEY,
                version BIGINT NOT NULL CHECK (version > 0),
                state JSONB NOT NULL CHECK (jsonb_typeof(state) = 'object')
            )"""
        ))

    def load(self) -> Snapshot | None:
        def read(cur: Any) -> Snapshot | None:
            cur.execute(
                "SELECT version, state FROM dry_run_snapshots_v1 WHERE scope = %s",
                (self._scope,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            version, state = row
            if type(version) is not int or version <= 0 or not isinstance(state, dict):
                raise RecoveryStoreError("invalid stored snapshot")
            return Snapshot(version, state)

        return self._run(read)

    def save(self, state: dict[str, Any], expected_version: int) -> int:
        """Replace one whole JSON snapshot; version 0 creates a new scope."""
        if not isinstance(state, dict):
            raise TypeError("state must be a JSON object")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        # Reject NaN/Infinity and non-JSON values before starting a transaction.
        payload = json.dumps(state, allow_nan=False)

        def write(cur: Any) -> int:
            if expected_version == 0:
                cur.execute(
                    """INSERT INTO dry_run_snapshots_v1 (scope, version, state)
                       VALUES (%s, 1, %s::jsonb) ON CONFLICT (scope) DO NOTHING
                       RETURNING version""",
                    (self._scope, payload),
                )
            else:
                cur.execute(
                    """UPDATE dry_run_snapshots_v1 SET version = version + 1,
                       state = %s::jsonb WHERE scope = %s AND version = %s
                       RETURNING version""",
                    (payload, self._scope, expected_version),
                )
            row = cur.fetchone()
            if row is None:
                raise RecoveryConflict("snapshot version mismatch")
            if row != (expected_version + 1,):
                raise RecoveryStoreError("invalid snapshot version returned")
            return row[0]

        return self._run(write)
