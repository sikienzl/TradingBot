"""Tests for the operator CLI :mod:`scripts.reconcile_fills`.

The CLI is the manual half of fill reconciliation: it must exit with a code a
timer or shell wrapper can act on, and it must refuse to pretend a paper
deployment has an exchange side. A fake exchange is injected by patching
:func:`src.fill_reconciliation.build_exchange`, so no test here talks to an
account.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.reconcile_fills import (
    EXIT_CLEAN,
    EXIT_INCOMPLETE,
    EXIT_MISMATCH,
    build_parser,
    config_from_args,
    main,
)
from src.fill_reconciliation import marker_int, read_report_marker
from src.trade_journal import TRADE_JOURNAL_FIELDS

STAMP = datetime.now(UTC)
STAMP_ISO = STAMP.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeExchange:
    """Minimal ccxt-shaped stand-in: no network, no live account."""

    def __init__(self, closed: list[dict[str, Any]] | None = None) -> None:
        self.closed = list(closed or [])

    def fetch_closed_orders(
        self,
        symbol: str | None = None,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if params:
            wanted = str(params.get("clientOrderId") or "")
            return [o for o in self.closed if str(o.get("clientOrderId")) == wanted]
        return list(self.closed)

    def fetch_open_orders(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []


def _row(client_order_id: str = "cid-1", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": STAMP_ISO,
        "iteration": 1,
        "coin": "ETH",
        "action": "BUY",
        "price": 2000.0,
        "amount_coin": 0.5,
        "amount_base": 1000.0,
        "dry_run": False,
        "run_id": "run-1",
        "fee_base": 2.0,
        "client_order_id": client_order_id,
        "requested_amount_coin": 0.5,
        "partial_fill": 0,
    }
    row.update(overrides)
    return row


def _journal(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "journal.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(TRADE_JOURNAL_FIELDS), extrasaction="ignore"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _exchange_order(client_order_id: str, **overrides: Any) -> dict[str, Any]:
    order: dict[str, Any] = {
        "id": "OID1",
        "clientOrderId": client_order_id,
        "symbol": "ETH/EUR",
        "side": "buy",
        "status": "closed",
        "filled": 0.5,
        "amount": 0.5,
        "average": 2000.0,
        "cost": 1000.0,
        "timestamp": int(STAMP.timestamp() * 1000),
        "closeTimestamp": int(STAMP.timestamp() * 1000),
        "fees": [{"cost": 2.0, "currency": "EUR"}],
    }
    order.update(overrides)
    return order


@pytest.fixture
def live_env(tmp_path: Path, monkeypatch) -> Path:
    """Environment that looks like a live deployment with a journal on disk."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("TRADING_JOURNAL_FILE", str(tmp_path / "journal.csv"))
    monkeypatch.setenv("KRAKEN_API_KEY", "key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "secret")
    monkeypatch.setenv("BASE_CURRENCY", "EUR")
    monkeypatch.delenv("PERFORMANCE_LOG_FILE", raising=False)
    monkeypatch.delenv("TRADING_FILL_RECONCILIATION_REPORT", raising=False)
    monkeypatch.delenv("TRADING_FILL_RECONCILIATION_SYMBOLS", raising=False)
    monkeypatch.delenv("TRADING_FILL_RECONCILIATION_LOOKBACK_DAYS", raising=False)
    monkeypatch.delenv("TRADING_FILL_RECONCILIATION_PAGE_LIMIT", raising=False)
    return tmp_path


def _use_exchange(monkeypatch, exchange: Any) -> None:
    monkeypatch.setattr(
        "src.fill_reconciliation.build_exchange", lambda name, key, secret: exchange
    )


def test_cli_refuses_paper_deployment_without_opt_in(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr(
        "scripts.reconcile_fills.reconcile_journal_fills",
        lambda *args, **kwargs: pytest.fail("a paper run must not call the exchange"),
    )

    assert main([]) == EXIT_INCOMPLETE
    assert "DRY_RUN is enabled" in capsys.readouterr().err


def test_cli_clean_run_exits_zero(live_env, monkeypatch, capsys) -> None:
    _journal(live_env, [_row()])
    _use_exchange(monkeypatch, FakeExchange([_exchange_order("cid-1")]))

    assert main([]) == EXIT_CLEAN
    out = capsys.readouterr().out
    assert "Fill reconciliation complete (reconciled)" in out
    assert "matched:             1" in out
    assert "findings (" not in out


def test_cli_mismatch_exits_one_and_lists_the_finding(live_env, monkeypatch, capsys) -> None:
    _journal(live_env, [_row()])
    _use_exchange(monkeypatch, FakeExchange([_exchange_order("cid-1", filled=0.2)]))

    assert main([]) == EXIT_MISMATCH
    out = capsys.readouterr().out
    assert "partial fills:       1" in out
    assert "id=cid-1" in out


def test_cli_incomplete_pass_exits_two(live_env, monkeypatch, capsys) -> None:
    _journal(live_env, [_row()])
    monkeypatch.setattr(
        "src.fill_reconciliation.build_exchange", lambda name, key, secret: None
    )

    assert main([]) == EXIT_INCOMPLETE
    out = capsys.readouterr().out
    assert "INCOMPLETE (exchange client unavailable)" in out
    assert "checked orders:      0" in out


def test_cli_json_output_is_parseable(live_env, monkeypatch, capsys) -> None:
    _journal(live_env, [_row()])
    _use_exchange(monkeypatch, FakeExchange([_exchange_order("cid-1")]))

    assert main(["--json"]) == EXIT_CLEAN
    payload = json.loads(capsys.readouterr().out)
    assert payload["complete"] is True
    assert payload["counts"]["matched"] == 1


def test_cli_writes_marker_and_json_report(live_env, monkeypatch) -> None:
    _journal(live_env, [_row()])
    _use_exchange(monkeypatch, FakeExchange([_exchange_order("cid-1", filled=0.2)]))
    marker = live_env / "reports" / "fill_reconciliation.env"

    assert main(["--report", str(marker)]) == EXIT_MISMATCH

    values = read_report_marker(str(marker))
    assert values is not None
    assert values["status"] == "complete"
    assert marker_int(values, "partial_fills") == 1
    payload = json.loads(
        (live_env / "reports" / "fill_reconciliation.env.json").read_text(encoding="utf-8")
    )
    assert payload["counts"]["partial_fill"] == 1


def test_cli_overrides_win_over_environment(live_env, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_FILL_RECONCILIATION_LOOKBACK_DAYS", "5")
    monkeypatch.setenv("TRADING_FILL_RECONCILIATION_PAGE_LIMIT", "7")
    monkeypatch.setenv("TRADING_FILL_RECONCILIATION_ENABLED", "0")
    monkeypatch.setenv("TRADING_FILL_RECONCILIATION_SYMBOLS", "BTC/EUR")
    argv = [
        "--journal", str(live_env / "other.csv"),
        "--lookback-days", "45",
        "--page-limit", "25",
        "--symbol", "eth/eur",
        "--report", str(live_env / "report.env"),
    ]

    config = config_from_args(build_parser().parse_args(argv))

    # An explicit run is always enabled: the deployment flag only governs the
    # automatic pass the bot performs at startup.
    assert config.enabled is True
    assert config.journal_file == str(live_env / "other.csv")
    assert config.lookback_days == 45
    assert config.page_limit == 25
    assert config.symbols == ("ETH/EUR",)
    assert config.report_file == str(live_env / "report.env")


def test_cli_findings_filter_hides_other_statuses(live_env, monkeypatch, capsys) -> None:
    _journal(live_env, [_row(client_order_id="")])
    _use_exchange(monkeypatch, FakeExchange([]))

    assert main(["--findings", "unmatched"]) == EXIT_CLEAN
    out = capsys.readouterr().out
    assert "not checkable:       1" in out
    assert "findings (" not in out


def test_cli_journal_path_falls_back_to_bot_variable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.delenv("TRADING_JOURNAL_FILE", raising=False)
    monkeypatch.setenv("PERFORMANCE_LOG_FILE", str(tmp_path / "bot_journal.csv"))

    config = config_from_args(build_parser().parse_args([]))

    assert config.journal_file == str(tmp_path / "bot_journal.csv")
