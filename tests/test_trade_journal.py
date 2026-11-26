"""Unit tests for :mod:`src.trade_journal`.

The journal schema, the two-phase paper-trade transaction and the
journal-derived threshold recommendation are exercised without a
:class:`src.trading_bot.CryptoTradingBot`: the config, the analytics writer and
the portfolio are stubs, and the transaction helpers arrive through
:class:`~src.trade_journal.PaperTradeHooks`.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.execution_costs import ESTIMATE_JOURNAL_FIELDS, FILL_JOURNAL_FIELDS
from src.trade_journal import (
    TRADE_JOURNAL_FIELDS,
    JournalContext,
    PaperTradeHooks,
    append_trade_journal,
    commit_paper_trade,
    init_journal,
    journal_row,
    recommend_tabular_threshold,
    recover_paper_trade,
    sync_directory,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class Config:
    performance_log_file: str = "journal.csv"
    performance_log_enabled: bool = True
    dry_run: bool = True
    base_currency: str = "EUR"
    auto_tune_lookback_trades: int = 100
    auto_tune_threshold_step: float = 0.05
    auto_tune_threshold_min: float = 0.0
    auto_tune_threshold_max: float = 0.2
    auto_tune_min_trades: int = 2


class RecordingAnalyticsWriter:
    def __init__(self) -> None:
        self.trades: list[tuple[dict, str]] = []

    def write_trade(self, row: dict, base_currency: str) -> None:
        self.trades.append((row, base_currency))


class FakePortfolio:
    def __init__(self, state_file: str, base_currency: str = "EUR") -> None:
        self.state_file = state_file
        self.base_currency = base_currency
        self.saved: list[dict] = []

    def _state(self) -> dict:
        return {"base_currency": self.base_currency, "cash": 42.0}

    def save_state(self, *, state: dict) -> None:
        self.saved.append(state)


@dataclass
class Hooks:
    """Recording :class:`PaperTradeHooks` plus the flag the bot owns."""

    failed: bool | None = None
    sync_calls: list[str] = field(default_factory=list)
    recover_calls: int = 0
    fail_sync: bool = False
    fail_recover: bool = False
    recover_impl: Callable[[], None] | None = None

    def as_hooks(self) -> PaperTradeHooks:
        return PaperTradeHooks(
            sync_directory=self._sync,
            recover_paper_trade=self._recover,
            set_paper_trade_failed=self._set_failed,
        )

    def _sync(self, path: str) -> None:
        if self.fail_sync:
            raise OSError("disk")
        self.sync_calls.append(path)

    def _recover(self) -> None:
        self.recover_calls += 1
        if self.fail_recover:
            raise RuntimeError("recover boom")
        if self.recover_impl is not None:
            self.recover_impl()

    def _set_failed(self, value: bool) -> None:
        self.failed = value


def make_context(tmp_path: Path, config: Config | None = None, **overrides) -> JournalContext:
    config = config or Config(performance_log_file=str(tmp_path / "journal.csv"))
    defaults = {
        "config": config,
        "analytics_writer": RecordingAnalyticsWriter(),
        "run_id": "run-1",
        "config_hash": "hash-1",
        "journal_fields": list(TRADE_JOURNAL_FIELDS),
        "journal_row": None,
    }
    defaults.update(overrides)
    if defaults["journal_row"] is None:
        defaults["journal_row"] = lambda row: journal_row(
            defaults["config"], defaults["run_id"], defaults["config_hash"], 7, row
        )
    return JournalContext(**defaults)


def journal_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _capture(monkeypatch, logger_name: str = "src.trade_journal"):
    """Capture records even if another module disabled logging at import time."""
    monkeypatch.setattr(logging.Logger.manager, "disable", logging.NOTSET)
    records: list[logging.LogRecord] = []

    class Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    target = logging.getLogger(logger_name)
    handler = Handler()
    target.addHandler(handler)
    previous = target.level
    target.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        target.removeHandler(handler)
        target.setLevel(previous)


@pytest.fixture
def cap_logs(monkeypatch):
    yield from _capture(monkeypatch)


# ---------------------------------------------------------------------------
# init_journal
# ---------------------------------------------------------------------------


def test_missing_journal_is_created_with_full_schema(tmp_path):
    path = tmp_path / "journal.csv"
    fields = init_journal(Config(performance_log_file=str(path)))

    assert fields == TRADE_JOURNAL_FIELDS
    with path.open(newline="", encoding="utf-8") as handle:
        assert list(csv.reader(handle)) == [TRADE_JOURNAL_FIELDS]


def test_execution_and_idempotency_columns_are_part_of_the_schema():
    """The columns a restart needs to keep a partial fill and its estimate."""
    expected = [
        "client_order_id", "requested_amount_coin", "partial_fill",
        "est_fee_base", "est_slippage_base", "est_spread_base",
        "est_total_cost_base", "est_cost_pct", "est_taker_fee_rate",
        "est_slippage_rate", "est_spread_bps",
    ]
    assert all(field in TRADE_JOURNAL_FIELDS for field in expected)
    assert ["client_order_id", *FILL_JOURNAL_FIELDS,
            *ESTIMATE_JOURNAL_FIELDS] == expected


def test_old_journal_gains_the_execution_columns(tmp_path):
    path = tmp_path / "journal.csv"
    legacy = list(TRADE_JOURNAL_FIELDS)
    for column in ("client_order_id", "partial_fill", "est_cost_pct"):
        legacy.remove(column)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(legacy)
        writer.writerow([""] * len(legacy))

    fields = init_journal(Config(performance_log_file=str(path)))

    # The missing columns are appended, never reordered, so old rows stay valid.
    assert fields == list(dict.fromkeys(legacy + TRADE_JOURNAL_FIELDS))
    assert set(fields) == set(TRADE_JOURNAL_FIELDS)
    rows = journal_rows(path)
    assert len(rows) == 1
    for column in ("client_order_id", "requested_amount_coin", "partial_fill",
                   "est_fee_base", "est_cost_pct", "est_spread_bps"):
        assert rows[0][column] == ""


def test_existing_journal_with_full_schema_is_left_untouched(tmp_path):
    path = tmp_path / "journal.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(TRADE_JOURNAL_FIELDS)
        csv.writer(handle).writerow([""] * len(TRADE_JOURNAL_FIELDS))
    before = path.read_bytes()

    assert init_journal(Config(performance_log_file=str(path))) == TRADE_JOURNAL_FIELDS
    assert path.read_bytes() == before


def test_legacy_journal_is_extended_with_defaults(tmp_path):
    path = tmp_path / "journal.csv"
    legacy = ["timestamp", "iteration", "coin", "action", "price"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(legacy)
        writer.writerow(["2026-11-01T00:00:00Z", 1, "BTC", "sell", 100])
    os.chmod(path, 0o640)

    fields = init_journal(Config(performance_log_file=str(path)))

    assert fields == list(dict.fromkeys(legacy + TRADE_JOURNAL_FIELDS))
    rows = journal_rows(path)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "legacy"
    assert rows[0]["config_hash"] == "legacy"
    assert rows[0]["mfe_pct"] == ""
    assert rows[0]["mae_pct"] == ""
    assert rows[0]["execution_cost_mode"] == "legacy_unknown"
    assert rows[0]["timestamp"] == "2026-11-01T00:00:00Z"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
    assert list(tmp_path.iterdir()) == [path]


def test_extension_failure_removes_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "journal.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(["timestamp"])
    monkeypatch.setattr(
        "src.trade_journal.os.replace",
        lambda *args: (_ for _ in ()).throw(OSError("disk")),
    )

    with pytest.raises(OSError, match="disk"):
        init_journal(Config(performance_log_file=str(path)))

    assert list(tmp_path.iterdir()) == [path]


def test_bom_prefixed_journal_is_read_and_rewritten(tmp_path):
    path = tmp_path / "journal.csv"
    path.write_bytes("\ufefftimestamp,coin\n2026-11-01T00:00:00Z,BTC\n".encode("utf-8"))

    fields = init_journal(Config(performance_log_file=str(path)))

    assert fields[0] == "timestamp"
    rows = journal_rows(path)
    assert rows[0]["timestamp"] == "2026-11-01T00:00:00Z"


# ---------------------------------------------------------------------------
# sync_directory
# ---------------------------------------------------------------------------


def test_sync_directory_fsyncs_and_closes_the_parent(tmp_path, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        "src.trade_journal.os.open",
        lambda path, flags: (calls.append(("open", path, flags)), 7)[1],
    )
    monkeypatch.setattr(
        "src.trade_journal.os.fsync", lambda fd: calls.append(("fsync", fd))
    )
    monkeypatch.setattr(
        "src.trade_journal.os.close", lambda fd: calls.append(("close", fd))
    )

    sync_directory(str(tmp_path / "journal.csv"))

    assert calls == [
        ("open", str(tmp_path), os.O_RDONLY),
        ("fsync", 7),
        ("close", 7),
    ]


def test_sync_directory_closes_descriptor_on_fsync_failure(tmp_path, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr("src.trade_journal.os.open", lambda path, flags: 9)

    def boom(fd: int) -> None:
        raise OSError("disk")

    monkeypatch.setattr("src.trade_journal.os.fsync", boom)
    monkeypatch.setattr(
        "src.trade_journal.os.close", lambda fd: calls.append(("close", fd))
    )

    with pytest.raises(OSError, match="disk"):
        sync_directory(str(tmp_path / "journal.csv"))

    assert calls == [("close", 9)]


# ---------------------------------------------------------------------------
# journal_row
# ---------------------------------------------------------------------------


def test_journal_row_fills_defaults_from_context():
    row = journal_row(Config(), "run-9", "hash-9", 3, {"coin": "ETH"})

    assert row["run_id"] == "run-9"
    assert row["config_hash"] == "hash-9"
    assert row["iteration"] == 3
    assert row["coin"] == "ETH"
    assert row["action"] == ""
    assert row["price"] == 0.0
    assert row["dry_run"] is True
    assert row["execution_cost_mode"] == "gross_legacy"
    assert row["mfe_pct"] == ""
    assert row["mae_pct"] == ""


def test_journal_row_keeps_explicit_values_and_derives_cost_mode():
    row = journal_row(
        Config(dry_run=True), "run-9", "hash-9", 3,
        {
            "run_id": "explicit-run",
            "config_hash": "explicit-hash",
            "iteration": 11,
            "timestamp": "2026-11-14T07:00:00Z",
            "dry_run": False,
            "mfe_pct": 2.5,
        },
    )

    assert row["run_id"] == "explicit-run"
    assert row["config_hash"] == "explicit-hash"
    assert row["iteration"] == 11
    assert row["timestamp"] == "2026-11-14T07:00:00Z"
    assert row["dry_run"] is False
    assert row["execution_cost_mode"] == "live_exchange"
    assert row["mfe_pct"] == 2.5


def test_journal_row_cost_mode_follows_row_dry_run_flag():
    live = journal_row(Config(dry_run=True), "r", "h", 1, {"dry_run": False})
    dry = journal_row(Config(dry_run=False), "r", "h", 1, {"dry_run": True})
    explicit = journal_row(
        Config(dry_run=True), "r", "h", 1,
        {"dry_run": False, "execution_cost_mode": "net_costs"},
    )

    assert live["execution_cost_mode"] == "live_exchange"
    assert dry["execution_cost_mode"] == "gross_legacy"
    assert explicit["execution_cost_mode"] == "net_costs"


def test_journal_row_timestamp_defaults_to_now_utc():
    before = datetime.now(UTC).isoformat()
    row = journal_row(Config(), "r", "h", 1, {})
    after = datetime.now(UTC).isoformat()

    assert before <= row["timestamp"] <= after


# ---------------------------------------------------------------------------
# append_trade_journal
# ---------------------------------------------------------------------------


def test_append_writes_enriched_row_and_analytics(tmp_path):
    path = tmp_path / "journal.csv"
    init_journal(Config(performance_log_file=str(path)))
    context = make_context(tmp_path)

    append_trade_journal(context, {"coin": "BTC", "action": "buy", "price": 100.0})

    rows = journal_rows(path)
    assert len(rows) == 1
    assert rows[0]["coin"] == "BTC"
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["config_hash"] == "hash-1"
    assert rows[0]["iteration"] == "7"
    assert len(context.analytics_writer.trades) == 1


def test_append_sends_unnormalised_row_to_analytics(tmp_path):
    path = tmp_path / "journal.csv"
    init_journal(Config(performance_log_file=str(path)))
    context = make_context(tmp_path)

    append_trade_journal(context, {"coin": "BTC", "action": "buy"})

    row, base_currency = context.analytics_writer.trades[0]
    assert base_currency == "EUR"
    assert "iteration" not in row
    assert "mfe_pct" not in row
    assert row["run_id"] == "run-1"
    assert row["execution_cost_mode"] == "gross_legacy"


def test_append_skips_csv_when_journal_disabled(tmp_path):
    config = Config(performance_log_file=str(tmp_path / "journal.csv"),
                    performance_log_enabled=False)
    context = make_context(tmp_path, config)

    append_trade_journal(context, {"coin": "ETH", "action": "sell"})

    assert not (tmp_path / "journal.csv").exists()
    assert len(context.analytics_writer.trades) == 1


def test_append_uses_context_journal_fields(tmp_path):
    path = tmp_path / "journal.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(["timestamp", "coin", "action"])
    context = make_context(tmp_path, journal_fields=["timestamp", "coin", "action"])

    append_trade_journal(context, {"coin": "ADA", "action": "buy", "pnl_base": 5})

    rows = journal_rows(path)
    assert list(rows[0]) == ["timestamp", "coin", "action"]
    assert rows[0]["coin"] == "ADA"


def test_append_reuses_the_context_row_enricher(tmp_path):
    path = tmp_path / "journal.csv"
    init_journal(Config(performance_log_file=str(path)))
    seen: list[dict] = []

    def enrich(row: dict) -> dict:
        seen.append(row)
        return {**row, "coin": "PATCHED"}

    context = make_context(tmp_path, journal_fields=list(TRADE_JOURNAL_FIELDS),
                           journal_row=enrich)

    append_trade_journal(context, {"coin": "BTC", "action": "buy"})

    assert len(seen) == 1
    assert journal_rows(path)[0]["coin"] == "PATCHED"


# ---------------------------------------------------------------------------
# recover_paper_trade
# ---------------------------------------------------------------------------


def write_intent(tmp_path: Path, portfolio: FakePortfolio, config: Config,
                 row: str, *, offset: int | None = None,
                 journal_text: str = "run,1\n", tamper: str | None = None) -> Path:
    journal = tmp_path / "journal.csv"
    journal.write_text(journal_text, encoding="utf-8")
    payload = {
        "version": 1,
        "journal": str(journal.resolve()),
        "state_file": os.path.abspath(portfolio.state_file),
        "offset": len(journal_text.encode("utf-8")) if offset is None else offset,
        "prefix_sha256": hashlib.sha256(journal_text.encode("utf-8")).hexdigest(),
        "row": row,
        "state": {"base_currency": portfolio.base_currency, "cash": 20.0},
    }
    if tamper == "journal_path":
        payload["journal"] = str(tmp_path / "elsewhere.csv")
    elif tamper == "version":
        payload["version"] = 2
    elif tamper == "offset":
        payload["offset"] = -1
    elif tamper == "row_type":
        payload["row"] = 5
    elif tamper == "state":
        payload["state"] = "nope"
    elif tamper == "base_currency":
        payload["state"] = {"base_currency": "USD"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    envelope = {"payload": payload}
    if tamper != "sha":
        envelope["sha256"] = hashlib.sha256(encoded).hexdigest()
    else:
        envelope["sha256"] = "0" * 64
    intent = Path(portfolio.state_file + ".trade-transaction.json")
    intent.write_text(json.dumps(envelope), encoding="utf-8")
    if tamper == "truncated_json":
        intent.write_text("{", encoding="utf-8")
    return intent


def make_transaction_stubs(tmp_path: Path, **overrides
                           ) -> tuple[JournalContext, FakePortfolio, Config]:
    config = Config(performance_log_file=str(tmp_path / "journal.csv"))
    portfolio = FakePortfolio(str(tmp_path / ".portfolio_state.json"))
    context = make_context(tmp_path, config, **overrides)
    return context, portfolio, config


def test_recover_without_intent_is_a_noop(tmp_path, monkeypatch, cap_logs):
    context, portfolio, _ = make_transaction_stubs(tmp_path)
    monkeypatch.setattr(
        "src.trade_journal.os.path.lexists", lambda path: False
    )

    recover_paper_trade(context, portfolio, logger=logging.getLogger("src.trade_journal"))

    assert portfolio.saved == []
    assert cap_logs == []


def test_recover_completes_a_pending_row(tmp_path, cap_logs):
    context, portfolio, config = make_transaction_stubs(tmp_path)
    row = "BTC,100\n"
    intent = write_intent(tmp_path, portfolio, config, row)

    recover_paper_trade(context, portfolio)

    journal = tmp_path / "journal.csv"
    assert journal.read_text(encoding="utf-8") == "run,1\n" + row
    assert not intent.exists()
    assert portfolio.saved == [{"base_currency": "EUR", "cash": 20.0}]
    assert [record.getMessage() for record in cap_logs] == [
        "Completed public paper trade transaction"
    ]


def test_recover_leaves_a_complete_row_untouched(tmp_path, cap_logs):
    context, portfolio, config = make_transaction_stubs(tmp_path)
    row = "BTC,100\n"
    intent = write_intent(tmp_path, portfolio, config, row)
    journal = tmp_path / "journal.csv"
    journal.write_text("run,1\n" + row, encoding="utf-8")
    before = journal.read_bytes()

    recover_paper_trade(context, portfolio)

    assert journal.read_bytes() == before
    assert not intent.exists()
    assert portfolio.saved == [{"base_currency": "EUR", "cash": 20.0}]


def test_recover_reports_damage(tmp_path, tamper=None):
    pass


@pytest.mark.parametrize("tamper", [
    "sha", "version", "journal_path", "offset", "row_type", "state",
    "base_currency", "truncated_json",
])
def test_recover_fails_closed_on_damaged_intent(tmp_path, tamper):
    context, portfolio, config = make_transaction_stubs(tmp_path)
    write_intent(tmp_path, portfolio, config, "BTC,100\n", tamper=tamper)

    with pytest.raises(RuntimeError, match="Cannot recover paper trade"):
        recover_paper_trade(context, portfolio)

    assert portfolio.saved == []


def test_recover_rejects_empty_row(tmp_path):
    context, portfolio, config = make_transaction_stubs(tmp_path)
    write_intent(tmp_path, portfolio, config, "")

    with pytest.raises(RuntimeError, match="Cannot recover paper trade"):
        recover_paper_trade(context, portfolio)


def test_reject_changed_journal_prefix(tmp_path):
    context, portfolio, config = make_transaction_stubs(tmp_path)
    write_intent(tmp_path, portfolio, config, "BTC,100\n")
    (tmp_path / "journal.csv").write_text("damaged\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Cannot recover paper trade"):
        recover_paper_trade(context, portfolio)


def test_reject_unexpected_journal_tail(tmp_path):
    context, portfolio, config = make_transaction_stubs(tmp_path)
    write_intent(tmp_path, portfolio, config, "BTC,100\n")
    with (tmp_path / "journal.csv").open("ab") as handle:
        handle.write(b"unexpected")

    with pytest.raises(RuntimeError, match="Cannot recover paper trade"):
        recover_paper_trade(context, portfolio)


def test_recover_propagates_save_state_failure_without_removing_intent(tmp_path):
    context, _, config = make_transaction_stubs(tmp_path)

    def boom(*, state: dict) -> None:
        raise RuntimeError("crash")

    portfolio = FakePortfolio(str(tmp_path / ".portfolio_state.json"))
    portfolio.save_state = boom  # type: ignore[method-assign]
    intent = write_intent(tmp_path, portfolio, config, "BTC,100\n")

    with pytest.raises(RuntimeError, match="crash"):
        recover_paper_trade(context, portfolio)

    assert intent.exists()


def test_recover_uses_the_injected_sync_helper(tmp_path, monkeypatch):
    context, portfolio, config = make_transaction_stubs(tmp_path)
    intent = write_intent(tmp_path, portfolio, config, "BTC,100\n")
    calls: list[str] = []
    monkeypatch.setattr(
        "src.trade_journal.os.open",
        lambda path, flags: (_ for _ in ()).throw(OSError("module sync unused")),
    )

    recover_paper_trade(context, portfolio, sync_directory=calls.append)

    assert calls == [str(intent)]
    assert not intent.exists()


def test_recover_sync_helper_receives_intent_path(tmp_path):
    context, portfolio, config = make_transaction_stubs(tmp_path)
    intent = write_intent(tmp_path, portfolio, config, "BTC,100\n")
    calls: list[str] = []

    recover_paper_trade(context, portfolio, sync_directory=calls.append)

    assert calls == [str(intent)]


# ---------------------------------------------------------------------------
# commit_paper_trade
# ---------------------------------------------------------------------------


def started_journal(tmp_path: Path) -> Path:
    path = tmp_path / "journal.csv"
    init_journal(Config(performance_log_file=str(path)))
    return path


def test_commit_writes_intent_then_recovers(tmp_path):
    journal = started_journal(tmp_path)
    context, portfolio, _config = make_transaction_stubs(tmp_path)
    hooks = Hooks(recover_impl=lambda: recover_paper_trade(context, portfolio))

    commit_paper_trade(context, portfolio, {"coin": "BTC", "action": "buy"},
                       hooks=hooks.as_hooks())

    intent = Path(portfolio.state_file + ".trade-transaction.json")
    assert not intent.exists()
    assert hooks.failed is False
    assert hooks.recover_calls == 1
    assert hooks.sync_calls == [str(journal.resolve()), str(intent)]
    rows = journal_rows(journal)
    assert len(rows) == 1
    assert rows[0]["coin"] == "BTC"
    assert rows[0]["run_id"] == "run-1"
    assert portfolio.saved == [{"base_currency": "EUR", "cash": 42.0}]
    assert len(context.analytics_writer.trades) == 1
    row, base_currency = context.analytics_writer.trades[0]
    assert base_currency == "EUR"
    assert row["coin"] == "BTC"
    assert row["execution_cost_mode"] == "gross_legacy"


def test_commit_intent_payload_is_self_describing(tmp_path):
    journal = started_journal(tmp_path)
    with journal.open("a", newline="", encoding="utf-8") as handle:
        handle.write("existing,row\n")
    context, portfolio, _config = make_transaction_stubs(tmp_path)
    hooks = Hooks(fail_recover=True)

    with pytest.raises(RuntimeError, match="recover boom"):
        commit_paper_trade(context, portfolio, {"coin": "ETH", "action": "sell"},
                           hooks=hooks.as_hooks())

    intent = Path(portfolio.state_file + ".trade-transaction.json")
    envelope = json.loads(intent.read_text(encoding="utf-8"))
    payload = envelope["payload"]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    prefix = journal.read_bytes()
    assert envelope["sha256"] == hashlib.sha256(encoded).hexdigest()
    assert payload["version"] == 1
    assert payload["journal"] == str(journal.resolve())
    assert payload["state_file"] == os.path.abspath(portfolio.state_file)
    assert payload["offset"] == len(prefix)
    assert payload["prefix_sha256"] == hashlib.sha256(prefix).hexdigest()
    assert payload["state"] == {"base_currency": "EUR", "cash": 42.0}
    assert payload["row"].startswith("20")
    assert hooks.failed is True
    assert hooks.recover_calls == 1


def test_commit_refuses_a_pending_intent(tmp_path):
    started_journal(tmp_path)
    context, portfolio, config = make_transaction_stubs(tmp_path)
    write_intent(tmp_path, portfolio, config, "BTC,100\n")
    hooks = Hooks()

    with pytest.raises(RuntimeError, match="restart to recover"):
        commit_paper_trade(context, portfolio, {"coin": "BTC", "action": "buy"},
                           hooks=hooks.as_hooks())

    assert hooks.failed is True
    assert hooks.recover_calls == 0
    assert context.analytics_writer.trades == []


def test_commit_rejects_incomplete_last_row(tmp_path):
    journal = started_journal(tmp_path)
    journal.write_text("timestamp,coin\npartial", encoding="utf-8")
    context, portfolio, _ = make_transaction_stubs(tmp_path)
    hooks = Hooks()

    with pytest.raises(RuntimeError, match="Cannot commit paper trade"):
        commit_paper_trade(context, portfolio, {"coin": "BTC", "action": "buy"},
                           hooks=hooks.as_hooks())

    assert not Path(portfolio.state_file + ".trade-transaction.json").exists()
    assert list(tmp_path.iterdir()) == [journal]


def test_commit_cleans_temporary_file_on_failure(tmp_path, monkeypatch):
    journal = started_journal(tmp_path)
    context, portfolio, _ = make_transaction_stubs(tmp_path)
    hooks = Hooks()
    real_replace = os.replace

    def boom(src: str, dst: str) -> None:
        if dst.endswith(".trade-transaction.json"):
            raise OSError("disk")
        real_replace(src, dst)

    monkeypatch.setattr("src.trade_journal.os.replace", boom)

    with pytest.raises(RuntimeError, match="Cannot commit paper trade"):
        commit_paper_trade(context, portfolio, {"coin": "BTC", "action": "buy"},
                           hooks=hooks.as_hooks())

    assert hooks.sync_calls == [str(journal.resolve())]
    assert [entry.name for entry in tmp_path.iterdir()] == ["journal.csv"]


def test_commit_propagates_sync_failure(tmp_path):
    started_journal(tmp_path)
    context, portfolio, _ = make_transaction_stubs(tmp_path)
    hooks = Hooks(fail_sync=True)

    with pytest.raises(RuntimeError, match="Cannot commit paper trade"):
        commit_paper_trade(context, portfolio, {"coin": "BTC", "action": "buy"},
                           hooks=hooks.as_hooks())

    assert hooks.failed is True
    assert context.analytics_writer.trades == []


def test_commit_uses_the_context_row_enricher(tmp_path):
    started_journal(tmp_path)
    context, portfolio, _ = make_transaction_stubs(
        tmp_path, journal_row=lambda row: {**row, "coin": "PATCHED"})
    hooks = Hooks(fail_recover=True)

    with pytest.raises(RuntimeError, match="recover boom"):
        commit_paper_trade(context, portfolio, {"coin": "BTC", "action": "buy"},
                           hooks=hooks.as_hooks())

    intent = Path(portfolio.state_file + ".trade-transaction.json")
    payload = json.loads(intent.read_text(encoding="utf-8"))["payload"]
    assert ",PATCHED,buy," in payload["row"]


def test_commit_normalises_datetime_state_values(tmp_path):
    started_journal(tmp_path)
    context, portfolio, _ = make_transaction_stubs(tmp_path)
    portfolio._state = lambda: {  # type: ignore[method-assign]
        "base_currency": "EUR",
        "last_update": datetime(2026, 11, 25, 7, 30, tzinfo=UTC),
    }
    hooks = Hooks(fail_recover=True)

    with pytest.raises(RuntimeError, match="recover boom"):
        commit_paper_trade(context, portfolio, {"coin": "BTC", "action": "buy"},
                           hooks=hooks.as_hooks())

    intent = Path(portfolio.state_file + ".trade-transaction.json")
    payload = json.loads(intent.read_text(encoding="utf-8"))["payload"]
    assert payload["state"]["last_update"] == "2026-11-25T07:30:00+00:00"


# ---------------------------------------------------------------------------
# recommend_tabular_threshold
# ---------------------------------------------------------------------------


def sell_rows(count: int, confidence: float, pnl: float) -> list[dict]:
    return [
        {"action": "sell", "pnl_base": pnl, "signal_confidence": confidence}
        for _ in range(count)
    ]


def write_journal(path: Path, rows: list[dict],
                  fieldnames: list[str] | None = None) -> None:
    names = fieldnames if fieldnames is not None else list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=names)
        writer.writeheader()
        writer.writerows(rows)


def test_threshold_without_journal_returns_none(tmp_path):
    assert recommend_tabular_threshold(
        Config(performance_log_file=str(tmp_path / "journal.csv"))) is None


def test_threshold_reports_unreadable_journal(tmp_path, cap_logs):
    path = tmp_path / "journal.csv"
    path.write_bytes(b'action,pnl_base,signal_confidence\n"unclosed,1,0.5\n')

    assert recommend_tabular_threshold(
        Config(performance_log_file=str(path))) is None
    assert cap_logs[0].levelname == "WARNING"
    assert cap_logs[0].getMessage().startswith(
        "Journal could not be read for auto-tuning: ")


def test_threshold_reports_empty_journal(tmp_path, cap_logs):
    path = tmp_path / "journal.csv"
    path.write_bytes(b"")

    assert recommend_tabular_threshold(
        Config(performance_log_file=str(path))) is None
    assert cap_logs[0].levelname == "WARNING"


def test_threshold_directory_journal_warns(tmp_path, cap_logs):
    path = tmp_path / "journal.csv"
    path.mkdir()

    assert recommend_tabular_threshold(
        Config(performance_log_file=str(path))) is None
    assert cap_logs[0].levelname == "WARNING"


@pytest.mark.parametrize("drop", ["action", "pnl_base", "signal_confidence"])
def test_threshold_requires_all_three_columns(tmp_path, drop):
    rows = sell_rows(4, 0.5, 1.0)
    rows = [{key: value for key, value in row.items() if key != drop} for row in rows]
    path = tmp_path / "journal.csv"
    write_journal(path, rows)

    assert recommend_tabular_threshold(
        Config(performance_log_file=str(path))) is None


def test_threshold_ignores_empty_and_buy_only_journals(tmp_path):
    empty = tmp_path / "empty.csv"
    write_journal(empty, [], fieldnames=["action", "pnl_base", "signal_confidence"])
    assert recommend_tabular_threshold(
        Config(performance_log_file=str(empty))) is None

    buys = tmp_path / "buys.csv"
    write_journal(buys, [{"action": "buy", "pnl_base": 1.0,
                          "signal_confidence": 0.9}])
    assert recommend_tabular_threshold(
        Config(performance_log_file=str(buys))) is None


def test_threshold_drops_unparseable_numbers(tmp_path):
    path = tmp_path / "journal.csv"
    write_journal(path, [
        {"action": "sell", "pnl_base": "n/a", "signal_confidence": "0.4"},
        {"action": "sell", "pnl_base": 1.0, "signal_confidence": "bad"},
    ])

    assert recommend_tabular_threshold(
        Config(performance_log_file=str(path))) is None


def test_threshold_needs_the_minimum_trade_count(tmp_path):
    path = tmp_path / "journal.csv"
    write_journal(path, sell_rows(3, 0.5, 1.0))

    config = Config(performance_log_file=str(path), auto_tune_min_trades=4)
    assert recommend_tabular_threshold(config) is None


def test_threshold_prefers_highest_total_pnl(tmp_path, cap_logs):
    path = tmp_path / "journal.csv"
    rows = [
        {"action": "sell", "pnl_base": 5.0, "signal_confidence": 0.0},
        {"action": "sell", "pnl_base": 5.0, "signal_confidence": 0.1},
        {"action": "sell", "pnl_base": 9.0, "signal_confidence": 0.2},
        {"action": "sell", "pnl_base": -10.0, "signal_confidence": 0.0},
    ]
    write_journal(path, rows)
    config = Config(performance_log_file=str(path), auto_tune_min_trades=2)

    best = recommend_tabular_threshold(config)

    # Dropping the two losing trades (threshold 0.05) beats taking all four.
    assert best == pytest.approx(0.05)
    message = cap_logs[-1].getMessage()
    assert message.startswith("🔧 Auto-Tune Threshold Recommendation: 0.05 ")
    assert "trades=2" in message
    assert "pnl=14.000000 EUR" in message
    assert "avg=7.000000" in message
    assert "win_rate=100.00%" in message


def test_threshold_lookback_limits_the_window(tmp_path):
    path = tmp_path / "journal.csv"
    rows = sell_rows(4, 0.2, 3.0) + sell_rows(2, 0.0, -20.0)
    write_journal(path, rows)
    base = Config(performance_log_file=str(path), auto_tune_min_trades=2)

    assert recommend_tabular_threshold(
        replace(base, auto_tune_lookback_trades=2)) == pytest.approx(0.0)
    assert recommend_tabular_threshold(base) == pytest.approx(0.05)


def test_threshold_ties_favour_average_pnl(tmp_path):
    path = tmp_path / "journal.csv"
    rows = [
        {"action": "sell", "pnl_base": 0.0, "signal_confidence": 0.0},
        {"action": "sell", "pnl_base": 6.0, "signal_confidence": 0.1},
    ]
    write_journal(path, rows)
    config = Config(performance_log_file=str(path), auto_tune_min_trades=1)

    # Both thresholds reach the same total PnL; the higher average wins.
    assert recommend_tabular_threshold(config) == pytest.approx(0.05)


def test_threshold_step_floor_is_applied(tmp_path):
    path = tmp_path / "journal.csv"
    write_journal(path, sell_rows(2, 0.0, 3.0))
    config = Config(performance_log_file=str(path), auto_tune_min_trades=2,
                    auto_tune_threshold_step=0.0, auto_tune_threshold_max=0.0)

    assert recommend_tabular_threshold(config) == pytest.approx(0.0)
