from __future__ import annotations

import pytest

from src.execution_owner import ExecutionOwnerLock


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