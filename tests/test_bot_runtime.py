from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from src.bot_runtime import (
    assert_live_scorecard_gate,
    run_bot,
    run_fill_reconciliation,
)
from src.execution_owner import ExecutionOwnerLock
from src.fill_reconciliation import ReconciliationConfig, ReconciliationReport
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


# ---------------------------------------------------------------------------
# Startup fill reconciliation
# ---------------------------------------------------------------------------


def _report(*, complete: bool = True, quantity: int = 0, fee: int = 0) -> ReconciliationReport:
    return ReconciliationReport(
        complete=complete,
        reason="reconciled" if complete else "journal unreadable",
        checked_at=datetime.now(UTC),
        lookback_days=30,
        base_currency="EUR",
        journal_rows=2,
        orders=2,
        counts={"matched": 2 - quantity, "unmatched": quantity},
        findings=[],
        quantity_mismatch=quantity,
        fee_mismatch=fee,
        exchange_calls=2,
        exchange_errors=0,
        journal_quantity_base=100.0,
        exchange_quantity_base=100.0 - quantity,
        journal_fee_base=1.0,
        exchange_fee_base=1.0,
    )


def _config(**overrides) -> ReconciliationConfig:
    values = {
        "enabled": True,
        "journal_file": "trade_journal.csv",
        "base_currency": "EUR",
        "exchange_name": "kraken",
        "lookback_days": 30,
        "read_limit": 4096,
        "page_limit": 100,
        "report_file": "",
        "api_key": "k",
        "api_secret": "s",
    }
    values.update(overrides)
    return ReconciliationConfig(**values)


def test_fill_reconciliation_skipped_in_dry_run(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.bot_runtime.reconcile_and_report",
        lambda *args, **kwargs: pytest.fail("dry-run must not call the exchange"),
    )
    run_fill_reconciliation(_Bot("", _Config(True)), logging.getLogger("t"))
    run_fill_reconciliation(_Bot("", None), logging.getLogger("t"))


def test_fill_reconciliation_skipped_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.bot_runtime.resolve_reconciliation_config", lambda: _config(enabled=False)
    )
    monkeypatch.setattr(
        "src.bot_runtime.reconcile_and_report",
        lambda *args, **kwargs: pytest.fail("disabled reconciliation must not run"),
    )
    run_fill_reconciliation(_Bot("", _Config(False)), logging.getLogger("t"))


def test_fill_reconciliation_reuses_bot_exchange_and_reports_mismatches(monkeypatch) -> None:
    seen = {}

    def fake_reconcile(settings, *, exchange=None, now=None, log=None):
        seen["settings"] = settings
        seen["exchange"] = exchange
        return _report(quantity=1)

    monkeypatch.setattr("src.bot_runtime.resolve_reconciliation_config", lambda: _config())
    monkeypatch.setattr("src.bot_runtime.reconcile_and_report", fake_reconcile)
    bot = _Bot("", _Config(False))
    bot.exchange = object()
    logger = logging.getLogger("test.fill_reconciliation")
    with pytest.MonkeyPatch.context() as patch:
        records = []
        patch.setattr(logger, "warning", lambda message, *args: records.append(message % args))
        run_fill_reconciliation(bot, logger)

    assert seen["exchange"] is bot.exchange
    assert seen["settings"].enabled is True
    assert any("do not match the exchange" in line for line in records)


def test_fill_reconciliation_never_raises(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr("src.bot_runtime.resolve_reconciliation_config", lambda: _config())
    monkeypatch.setattr("src.bot_runtime.reconcile_and_report", boom)
    run_fill_reconciliation(_Bot("", _Config(False)), logging.getLogger("t"))


def test_run_bot_reconciles_before_the_loop(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "src.bot_runtime.scorecard_gate_settings", lambda: _gate(tmp_path))
    monkeypatch.setattr("src.bot_runtime.resolve_reconciliation_config", lambda: _config())
    order = []
    monkeypatch.setattr(
        "src.bot_runtime.reconcile_and_report",
        lambda *args, **kwargs: order.append("reconcile") or _report(),
    )
    monkeypatch.setattr(
        "src.bot_runtime._run_loop", lambda bot, logger: order.append("loop")
    )
    bot = _Bot(str(tmp_path / "bot.lock"), _Config(False))

    run_bot(bot, logging.getLogger("test.bot_runtime"))

    assert order == ["reconcile", "loop"]
    assert bot.analytics_writer.closed is True