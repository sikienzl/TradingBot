from __future__ import annotations

import logging

import pytest

from src.bot_runtime import run_bot
from src.execution_owner import ExecutionOwnerLock
from src.trading_bot import CryptoTradingBot


class _AnalyticsWriter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Bot:
    def __init__(self, lock_path: str) -> None:
        self._execution_owner_lock = ExecutionOwnerLock(lock_path)
        self.analytics_writer = _AnalyticsWriter()

    def _log_startup_banner(self) -> None:
        raise RuntimeError("stop runtime")


def test_run_bot_releases_owner_lock_when_stopped(tmp_path):
    lock_path = str(tmp_path / "bot.lock")
    bot = _Bot(lock_path)

    with pytest.raises(RuntimeError, match="stop runtime"):
        run_bot(bot, logging.getLogger("test.bot_runtime"))

    assert bot.analytics_writer.closed is True
    replacement = ExecutionOwnerLock(lock_path)
    replacement.acquire()
    replacement.release()


def test_trading_bot_run_delegates_to_runtime(monkeypatch):
    calls = []
    monkeypatch.setattr("src.trading_bot._run_bot", lambda bot, logger: calls.append(bot))
    bot = CryptoTradingBot.__new__(CryptoTradingBot)

    bot.run()

    assert calls == [bot]