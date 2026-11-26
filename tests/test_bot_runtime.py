from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from src.bot_runtime import assert_live_scorecard_gate, run_bot
from src.execution_owner import ExecutionOwnerLock
from src.scorecard_gate import scorecard_gate_settings
from src.trading_bot import CryptoTradingBot


class _AnalyticsWriter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Bot:
    def __init__(self, lock_path: str, config=None) -> None:
        self._execution_owner_lock = ExecutionOwnerLock(lock_path)
        self.analytics_writer = _AnalyticsWriter()
        self.config = config

    def _log_startup_banner(self) -> None:
        raise RuntimeError("stop runtime")


class _Config:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run


def _gate(tmp_path, verdict: str = "GO", *, seconds_ago: float = 60.0):
    marker = tmp_path / "latest_status.env"
    stamp = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    marker.write_text(
        f"TIMESTAMP_UTC={stamp}\nRUN_MODE=standard\nVERDICT={verdict}\n",
        encoding="utf-8",
    )
    return scorecard_gate_settings({
        "SCORECARD_STATUS_JSON": str(tmp_path / "absent.json"),
        "SCORECARD_STATUS_ENV": str(marker),
    })


def test_live_gate_accepts_fresh_go_before_the_loop(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.bot_runtime.scorecard_gate_settings", lambda: _gate(tmp_path))
    assert_live_scorecard_gate(_Bot("", _Config(False)), logging.getLogger("t"))


@pytest.mark.parametrize("verdict", ["HOLD", "NO-GO", "ERROR"])
def test_live_gate_refuses_live_loop_without_go(tmp_path, monkeypatch, verdict) -> None:
    monkeypatch.setattr(
        "src.bot_runtime.scorecard_gate_settings", lambda: _gate(tmp_path, verdict))
    with pytest.raises(ValueError, match=f"scorecard verdict is {verdict}"):
        assert_live_scorecard_gate(_Bot("", _Config(False)), logging.getLogger("t"))


def test_live_gate_is_silent_in_dry_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.bot_runtime.scorecard_gate_settings",
        lambda: pytest.fail("dry-run must not read the scorecard"),
    )
    assert_live_scorecard_gate(_Bot("", _Config(True)), logging.getLogger("t"))
    assert_live_scorecard_gate(_Bot("", None), logging.getLogger("t"))


def test_run_bot_refuses_live_loop_without_go(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.bot_runtime.scorecard_gate_settings",
        lambda: _gate(tmp_path, "NO-GO"),
    )
    bot = _Bot(str(tmp_path / "bot.lock"), _Config(False))

    with pytest.raises(ValueError, match="scorecard verdict is NO-GO"):
        run_bot(bot, logging.getLogger("test.bot_runtime"))

    # The loop never started (its stub raises RuntimeError), but the analytics
    # writer is still released and the execution lock was never taken.
    assert bot.analytics_writer.closed is True
    replacement = ExecutionOwnerLock(str(tmp_path / "bot.lock"))
    replacement.acquire()
    replacement.release()


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