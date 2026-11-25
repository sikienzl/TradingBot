"""Local process ownership lock for a single trading account."""

from __future__ import annotations

import fcntl
import os
from typing import Self


class ExecutionOwnerLock:
    """Holds an exclusive non-blocking lock for the bot process lifetime."""

    def __init__(self, path: str):
        self.path = path
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
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)