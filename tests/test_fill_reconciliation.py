"""Tests for :mod:`src.fill_reconciliation` (journal rows versus Kraken fills).

The reconciliation is a read-only audit boundary, so the interesting cases are
the ways it can be wrong or unusable: an order that never filled, a partial
fill, an over-fill, a cancelled order whose fill the journal booked, a failing
exchange call, missing credentials, an unreadable journal, and rows that
predate client order ids. A fake exchange is used throughout - these tests must
never talk to a live account.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import ccxt

from src.fill_reconciliation import (
    MAX_SYMBOL_SCOPES,
    STATUS_MATCHED,
    STATUS_NOT_CHECKABLE,
    STATUS_PARTIAL,
    STATUS_UNMATCHED,
    build_exchange,
    collect_exchange_orders,
    iter_findings,
    journal_symbols,
    log_report,
    marker_age_seconds,
    marker_int,
    parse_exchange_order,
    read_journal_orders,
    read_report_marker,
    reconcile_and_report,
    reconcile_journal_fills,
    report_marker_lines,
    report_to_dict,
    resolve_reconciliation_config,
    write_report_marker,
)
from src.trade_journal import TRADE_JOURNAL_FIELDS

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
NOW_ISO = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeExchange:
    """Minimal ccxt-shaped stand-in: no network, no live account."""

    def __init__(
        self,
        closed: list[dict[str, Any]] | None = None,
        open_orders: list[dict[str, Any]] | None = None,
        *,
        fail: tuple[str, ...] = (),
        lookup_fail: bool = False,
        lookup: Any = None,
    ) -> None:
        self.closed = list(closed or [])
        self.open_orders = list(open_orders or [])
        self.fail = set(fail)
        self.lookup_fail = lookup_fail
        self.lookup = lookup
        self.calls: list[tuple[str, str | None, Any]] = []

    def fetch_closed_orders(
        self, symbol: str | None = None, since: int | None = None,
        limit: int | None = None, params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(("fetch_closed_orders", symbol, params))
        if "fetch_closed_orders" in self.fail:
            raise ccxt.NetworkError("exchange unavailable")
        if params:
            if self.lookup_fail:
                raise ccxt.ExchangeError("order lookup rejected")
            wanted = str(params.get("clientOrderId") or "")
            if self.lookup is not None:
                return list(self.lookup(symbol, wanted) or [])
            return [order for order in self.closed if str(order.get("clientOrderId")) == wanted]
        return list(self.closed)

    def fetch_open_orders(
        self, symbol: str | None = None, since: int | None = None,
        limit: int | None = None, params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(("fetch_open_orders", symbol, params))
        if "fetch_open_orders" in self.fail:
            raise ccxt.ExchangeError("bad request")
        return list(self.open_orders)


class NoHistoryExchange:
    """Exchange client without the order-history methods at all."""


def exchange_order(
    client_order_id: str,
    *,
    symbol: str = "ETH/EUR",
    side: str = "buy",
    status: str = "closed",
    filled: float | None = 0.5,
    amount: float | None = 0.5,
    average: float | None = 2000.0,
    cost: float | None = 1000.0,
    fee_cost: float | None = 2.0,
    fee_currency: str = "EUR",
    order_id: str = "OID1",
) -> dict[str, Any]:
    order: dict[str, Any] = {
        "id": order_id,
        "clientOrderId": client_order_id,
        "symbol": symbol,
        "side": side,
        "status": status,
        "filled": filled,
        "amount": amount,
        "average": average,
        "cost": cost,
        "timestamp": int(NOW.timestamp() * 1000),
        "closeTimestamp": int(NOW.timestamp() * 1000),
    }
    if fee_cost is not None:
        order["fees"] = [{"cost": fee_cost, "currency": fee_currency}]
    return order


def live_row(client_order_id: str = "cid-1", **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": NOW_ISO,
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


def write_journal(path: Path, rows: list[dict[str, Any]]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(TRADE_JOURNAL_FIELDS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def config_for(tmp_path: Path, **overrides: str) -> Any:
    values: dict[str, str] = {
        "TRADING_FILL_RECONCILIATION_ENABLED": "1",
        "TRADING_JOURNAL_FILE": str(tmp_path / "journal.csv"),
        "BASE_CURRENCY": "EUR",
        "KRAKEN_API_KEY": "key",
        "KRAKEN_API_SECRET": "secret",
    }
    values.update(overrides)
    return resolve_reconciliation_config(values)


def reconcile(tmp_path: Path, exchange: Any, rows: list[dict[str, Any]], **env: str) -> Any:
    write_journal(tmp_path / "journal.csv", rows)
    return reconcile_journal_fills(config_for(tmp_path, **env), exchange=exchange, now=NOW)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_config_defaults_follow_the_run_mode() -> None:
    # A dry-run deployment has no exchange side to compare against, so the
    # reconciliation stays off unless the operator asks for it.
    default = resolve_reconciliation_config({})
    assert default.enabled is False
    live = resolve_reconciliation_config({"DRY_RUN": "0"})
    assert live.enabled is True
    explicit = resolve_reconciliation_config({"DRY_RUN": "1", "TRADING_FILL_RECONCILIATION_ENABLED": "1"})
    assert explicit.enabled is True


def test_config_defaults_journal_path_and_limits() -> None:
    config = resolve_reconciliation_config({"TRADING_DATA_DIR": "/opt/trading_2/data"})
    assert config.journal_file == "/opt/trading_2/data/trade_journal.csv"
    assert config.lookback_days > 0
    assert config.page_limit > 0
    assert config.read_limit > 0
    assert config.symbols == ()


def test_config_rejects_nonsensical_numbers() -> None:
    config = resolve_reconciliation_config({
        "TRADING_FILL_RECONCILIATION_LOOKBACK_DAYS": "0",
        "TRADING_FILL_RECONCILIATION_PAGE_LIMIT": "-5",
    })
    assert config.lookback_days > 0
    assert config.page_limit > 0


def test_build_exchange_requires_credentials() -> None:
    base = {"BASE_CURRENCY": "EUR"}
    anonymous = resolve_reconciliation_config(base)
    assert build_exchange(anonymous.exchange_name, anonymous.api_key, anonymous.api_secret) is None
    keyed = resolve_reconciliation_config({**base, "KRAKEN_API_KEY": "k", "KRAKEN_API_SECRET": "s"})
    exchange = build_exchange(keyed.exchange_name, keyed.api_key, keyed.api_secret)
    assert exchange is not None
    assert exchange.apiKey == "k"
    unknown = resolve_reconciliation_config({
        **base, "KRAKEN_API_KEY": "k", "KRAKEN_API_SECRET": "s", "EXCHANGE_NAME": "not-an-exchange",
    })
    assert build_exchange(unknown.exchange_name, unknown.api_key, unknown.api_secret) is None


# ---------------------------------------------------------------------------
# Journal reading
# ---------------------------------------------------------------------------


def test_read_journal_orders_skips_paper_rows(tmp_path: Path) -> None:
    write_journal(tmp_path / "journal.csv", [
        live_row("cid-paper", dry_run=True),
        live_row("cid-empty", dry_run=""),
        live_row("cid-garbage", dry_run="maybe"),
        live_row("cid-live"),
    ])
    orders = read_journal_orders(str(tmp_path / "journal.csv"), lookback_days=30, now=NOW)
    assert [order.client_order_id for order in orders] == ["cid-live"]


def test_read_journal_orders_honours_the_window(tmp_path: Path) -> None:
    old = (NOW - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_journal(tmp_path / "journal.csv", [
        live_row("cid-old", timestamp=old),
        live_row("cid-broken", timestamp="not-a-timestamp"),
        live_row("cid-new"),
    ])
    orders = read_journal_orders(str(tmp_path / "journal.csv"), lookback_days=30, now=NOW)
    assert [order.client_order_id for order in orders] == ["cid-new"]


def test_read_journal_orders_ignores_non_order_rows(tmp_path: Path) -> None:
    write_journal(tmp_path / "journal.csv", [
        live_row("cid-hold", action="HOLD"),
        live_row("cid-sell", action="SELL"),
    ])
    orders = read_journal_orders(str(tmp_path / "journal.csv"), lookback_days=30, now=NOW)
    assert [order.action for order in orders] == ["SELL"]


def test_missing_journal_is_not_an_error(tmp_path: Path) -> None:
    assert read_journal_orders(str(tmp_path / "absent.csv"), lookback_days=30, now=NOW) == []


def test_journal_without_order_columns_is_empty(tmp_path: Path) -> None:
    (tmp_path / "journal.csv").write_text("timestamp,coin\n2026-11-26T12:00:00Z,ETH\n", encoding="utf-8")
    assert read_journal_orders(str(tmp_path / "journal.csv"), lookback_days=30, now=NOW) == []


def test_torn_tail_line_is_dropped(tmp_path: Path) -> None:
    path = tmp_path / "journal.csv"
    write_journal(path, [live_row("cid-1")])
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("2026-11-26T12:00:00Z,1,BTC,BUY,1000")
    orders = read_journal_orders(str(path), lookback_days=30, now=NOW)
    assert [order.client_order_id for order in orders] == ["cid-1"]


def test_tail_read_never_invents_rows(tmp_path: Path) -> None:
    path = tmp_path / "journal.csv"
    write_journal(path, [live_row("cid-1"), live_row("cid-2", coin="BTC")])
    # A read window too small to contain the header must not be parsed as if it
    # were a journal.
    assert read_journal_orders(str(path), lookback_days=30, read_limit=40, now=NOW) == []


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_matched_fill_reports_a_clean_pass(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1")])
    report = reconcile(tmp_path, exchange, [live_row("cid-1")])
    assert report.complete is True
    assert report.reason == "reconciled"
    assert report.counts[STATUS_MATCHED] == 1
    assert report.quantity_mismatch == 0
    assert report.fee_mismatch == 0
    assert report.has_mismatches is False
    assert report.exchange_calls == 2
    assert report.exchange_errors == 0
    assert report.journal_quantity_base == 1000.0
    assert report.exchange_quantity_base == 1000.0
    assert report.exchange_fee_base == 2.0
    finding = report.findings[0]
    assert finding.exchange_order_id == "OID1"
    assert finding.exchange_status == "closed"


def test_partial_fill_is_a_mismatch(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1", filled=0.3, cost=600.0)])
    report = reconcile(tmp_path, exchange, [live_row("cid-1")])
    assert report.complete is True
    assert report.counts[STATUS_PARTIAL] == 1
    assert report.quantity_mismatch == 1
    assert report.has_mismatches is True
    assert "exchange filled" in report.findings[0].reason


def test_overfill_is_unmatched(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1", filled=0.7, cost=1400.0)])
    report = reconcile(tmp_path, exchange, [live_row("cid-1")])
    assert report.counts[STATUS_UNMATCHED] == 1
    assert report.quantity_mismatch == 1


def test_cancelled_order_with_a_booked_fill_is_a_mismatch(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1", status="canceled", filled=0.0, cost=0.0)])
    report = reconcile(tmp_path, exchange, [live_row("cid-1")])
    assert report.complete is True
    assert report.counts[STATUS_UNMATCHED] == 1
    assert report.quantity_mismatch == 1
    assert "canceled" in report.findings[0].reason
    # The order was found, so no extra lookup call was needed.
    assert report.exchange_calls == 2


def test_cancelled_order_without_fill_matches(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1", status="canceled", filled=0.0, cost=0.0)])
    report = reconcile(tmp_path, exchange, [live_row("cid-1", amount_coin=0.0, amount_base=0.0)])
    assert report.complete is True
    assert report.counts[STATUS_MATCHED] == 1
    assert report.has_mismatches is False


def test_open_order_is_compared_by_filled_quantity(tmp_path: Path) -> None:
    exchange = FakeExchange(
        closed=[],
        open_orders=[exchange_order("cid-1", status="open", filled=0.2, cost=400.0)],
    )
    report = reconcile(tmp_path, exchange, [live_row("cid-1")])
    assert report.counts[STATUS_PARTIAL] == 1
    assert report.findings[0].exchange_status == "open"


def test_order_absent_from_the_exchange_is_unmatched(tmp_path: Path) -> None:
    exchange = FakeExchange()
    report = reconcile(tmp_path, exchange, [live_row("cid-gone")])
    assert report.complete is True
    assert report.counts[STATUS_UNMATCHED] == 1
    assert report.quantity_mismatch == 1
    assert report.exchange_calls == 3  # two history pages plus one explicit lookup
    assert "no matching exchange order" in report.findings[0].reason


def test_explicit_lookup_finds_orders_outside_the_page(tmp_path: Path) -> None:
    exchange = FakeExchange(lookup=lambda symbol, cid: [exchange_order(cid)] if cid == "cid-late" else [])
    report = reconcile(tmp_path, exchange, [live_row("cid-late")])
    assert report.complete is True
    assert report.counts[STATUS_MATCHED] == 1
    assert any(call[2] == {"clientOrderId": "cid-late"} for call in exchange.calls)


def test_lookup_failure_is_not_checkable_and_incomplete(tmp_path: Path) -> None:
    exchange = FakeExchange(lookup_fail=True)
    report = reconcile(tmp_path, exchange, [live_row("cid-x")])
    assert report.complete is False
    assert "lookup" in report.reason
    assert report.counts[STATUS_NOT_CHECKABLE] == 1
    assert report.quantity_mismatch == 0
    assert report.has_mismatches is False
    assert report.exchange_errors == 1


def test_history_failure_is_incomplete_not_clean(tmp_path: Path) -> None:
    exchange = FakeExchange(fail=("fetch_closed_orders", "fetch_open_orders"))
    report = reconcile(tmp_path, exchange, [live_row("cid-1")])
    assert report.complete is False
    # Two failed history pages plus the failed per-row lookup.
    assert report.exchange_errors == 3
    assert report.counts[STATUS_NOT_CHECKABLE] == 1
    assert report.has_mismatches is False


def test_missing_history_methods_are_incomplete(tmp_path: Path) -> None:
    report = reconcile(tmp_path, NoHistoryExchange(), [live_row("cid-1")])
    assert report.complete is False
    assert report.exchange_errors == 2


def test_missing_credentials_fail_closed(tmp_path: Path) -> None:
    write_journal(tmp_path / "journal.csv", [live_row("cid-1")])
    config = config_for(tmp_path)
    config = type(config)(**{**config.__dict__, "api_key": "", "api_secret": ""})
    report = reconcile_journal_fills(config, now=NOW)
    assert report.complete is False
    assert report.reason == "no authenticated exchange"
    assert report.journal_rows == 1
    assert report.orders == 0


def test_disabled_config_reports_disabled(tmp_path: Path) -> None:
    write_journal(tmp_path / "journal.csv", [live_row("cid-1")])
    config = config_for(tmp_path)
    config = type(config)(**{**config.__dict__, "enabled": False})
    report = reconcile_journal_fills(config, exchange=FakeExchange(), now=NOW)
    assert report.complete is False
    assert report.reason == "disabled"
    assert report.journal_rows == 0


def test_empty_journal_is_a_clean_pass(tmp_path: Path) -> None:
    report = reconcile(tmp_path, FakeExchange(), [])
    assert report.complete is True
    assert report.reason == "no live orders in the lookback window"
    assert report.journal_rows == 0
    assert report.exchange_calls == 0


def test_unreadable_journal_is_incomplete(tmp_path: Path) -> None:
    path = tmp_path / "journal.csv"
    write_journal(path, [live_row("cid-1")])
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("x," + "y" * (csv.field_size_limit() + 16) + "\n")
    config = config_for(tmp_path)
    report = reconcile_journal_fills(config, exchange=FakeExchange(), now=NOW)
    assert report.complete is False
    assert report.reason == "journal unreadable"


def test_rows_without_client_order_id_are_not_checkable(tmp_path: Path) -> None:
    report = reconcile(tmp_path, FakeExchange(), [
        live_row("", run_id="legacy"),
        live_row("", coin="BTC", run_id="run-2"),
    ])
    assert report.complete is True
    assert report.counts[STATUS_NOT_CHECKABLE] == 2
    reasons = {finding.reason for finding in report.findings}
    assert reasons == {"journal row predates client order ids", "journal row has no client order id"}
    # History is still fetched once per scope (BTC/EUR, ETH/EUR) and per method.
    assert report.exchange_calls == 4


# ---------------------------------------------------------------------------
# Fees, currencies, sides
# ---------------------------------------------------------------------------


def test_fee_mismatch_is_reported_with_a_matching_fill(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1", fee_cost=5.0)])
    report = reconcile(tmp_path, exchange, [live_row("cid-1")])
    assert report.counts[STATUS_MATCHED] == 1
    assert report.fee_mismatch == 1
    assert report.quantity_mismatch == 0
    assert report.has_mismatches is True
    assert "fee" in report.findings[0].reason


def test_unconvertible_exchange_fee_record_is_not_a_mismatch(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1", fee_currency="USD")])
    report = reconcile(tmp_path, exchange, [live_row("cid-1")])
    assert report.counts[STATUS_MATCHED] == 1
    assert report.fee_mismatch == 0


def test_fee_in_traded_asset_is_converted(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1", fee_cost=0.001, fee_currency="ETH")])
    report = reconcile(tmp_path, exchange, [live_row("cid-1")])
    assert report.exchange_fee_base == 2.0
    assert report.fee_mismatch == 0


def test_kraken_currency_aliases_match(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1", symbol="XBT/EUR")])
    report = reconcile(tmp_path, exchange, [live_row("cid-1", coin="BTC")])
    assert report.counts[STATUS_MATCHED] == 1


def test_margin_side_suffix_matches(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1", side="marginBuy")])
    report = reconcile(tmp_path, exchange, [live_row("cid-1")])
    assert report.counts[STATUS_MATCHED] == 1


def test_sell_side_matches_sell_row(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1", side="sell")])
    report = reconcile(tmp_path, exchange, [live_row("cid-1", action="SELL")])
    assert report.counts[STATUS_MATCHED] == 1


def test_parse_exchange_order_requires_a_client_order_id() -> None:
    assert parse_exchange_order({"symbol": "ETH/EUR"}) is None
    assert parse_exchange_order("not a mapping") is None  # type: ignore[arg-type]
    parsed = parse_exchange_order(exchange_order("cid-1"), base_currency="EUR", traded_asset="ETH")
    assert parsed is not None
    assert parsed.filled == 0.5
    assert parsed.closed_at.endswith("+00:00")


def test_parse_exchange_order_survives_missing_timestamps() -> None:
    parsed = parse_exchange_order(
        {"clientOrderId": "cid-1", "symbol": "ETH/EUR", "side": "buy", "status": "open"},
        base_currency="EUR",
    )
    assert parsed is not None
    assert parsed.closed_at == ""
    assert parsed.fee_base is None


# ---------------------------------------------------------------------------
# Exchange scoping
# ---------------------------------------------------------------------------


def test_journal_symbols_derives_pairs(tmp_path: Path) -> None:
    write_journal(tmp_path / "journal.csv", [
        live_row("cid-1", coin="ETH"),
        live_row("cid-2", coin="BTC"),
        live_row("cid-3", coin="BTC"),
    ])
    rows = read_journal_orders(str(tmp_path / "journal.csv"), lookback_days=30, now=NOW)
    assert journal_symbols(rows, "EUR") == ("BTC/EUR", "ETH/EUR")


def test_journal_symbols_is_bounded() -> None:
    class _Row:
        def __init__(self, coin: str) -> None:
            self.coin = coin

    symbols = journal_symbols([_Row(f"C{index}") for index in range(40)], "EUR")
    assert len(symbols) == MAX_SYMBOL_SCOPES


def test_configured_symbols_scope_the_history_calls(tmp_path: Path) -> None:
    exchange = FakeExchange()
    report = reconcile(tmp_path, exchange, [live_row("cid-1")],
                       TRADING_FILL_RECONCILIATION_SYMBOLS="ETH/EUR,BTC/EUR")
    scoped = [call[1] for call in exchange.calls]
    # Two configured scopes per history method, then the lookup for the row the
    # history did not contain.
    assert scoped == ["ETH/EUR", "BTC/EUR", "ETH/EUR", "BTC/EUR", "ETH/EUR"]
    assert report.complete is True


def test_collect_exchange_orders_counts_calls_and_errors() -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1")], fail=("fetch_open_orders",))
    orders, calls, errors = collect_exchange_orders(exchange, 0, symbols=("ETH/EUR",))
    assert [order["clientOrderId"] for order in orders] == ["cid-1"]
    assert calls == 2
    assert errors == 1


# ---------------------------------------------------------------------------
# Reports, markers, logging
# ---------------------------------------------------------------------------


def test_report_to_dict_is_json_serialisable(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1", filled=0.3, cost=600.0)])
    report = reconcile(tmp_path, exchange, [live_row("cid-1")])
    payload = json.loads(json.dumps(report_to_dict(report)))
    assert payload["complete"] is True
    assert payload["counts"][STATUS_PARTIAL] == 1
    assert payload["findings"][0]["client_order_id"] == "cid-1"


def test_marker_round_trip(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1")])
    report = reconcile(tmp_path, exchange, [live_row("cid-1")])
    path = tmp_path / "fill-reconciliation.env"
    write_report_marker(str(path), report)
    values = read_report_marker(str(path))
    assert values is not None
    assert values["status"] == "complete"
    assert values["reason"] == "reconciled"
    assert marker_int(values, "matched") == 1
    assert marker_int(values, "quantity_mismatch") == 0
    assert marker_age_seconds(values, NOW) == 0.0
    assert len(report_marker_lines(report)) == len(values)


def test_reconcile_and_report_writes_marker_and_json(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1", filled=0.4, cost=800.0)])
    marker = tmp_path / "state" / "fill-reconciliation.env"
    config = config_for(tmp_path, TRADING_FILL_RECONCILIATION_REPORT=str(marker))
    write_journal(tmp_path / "journal.csv", [live_row("cid-1")])
    report = reconcile_and_report(config, exchange=exchange, now=NOW)
    assert report.has_mismatches is True
    values = read_report_marker(str(marker))
    assert values is not None
    assert marker_int(values, "partial_fills") == 1
    payload = json.loads((tmp_path / "state" / "fill-reconciliation.env.json").read_text(encoding="utf-8"))
    assert payload["quantity_mismatch"] == 1
    assert not list(tmp_path.glob("state/.fill-reconciliation-*"))


def test_reconcile_and_report_survives_unwritable_report(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1")])
    write_journal(tmp_path / "journal.csv", [live_row("cid-1")])
    config = config_for(tmp_path, TRADING_FILL_RECONCILIATION_REPORT=str(tmp_path / "journal.csv" / "nope.env"))
    report = reconcile_and_report(config, exchange=exchange, now=NOW)
    assert report.complete is True


def test_read_report_marker_missing_or_malformed(tmp_path: Path) -> None:
    assert read_report_marker(str(tmp_path / "absent.env")) is None
    (tmp_path / "junk.env").write_text("not a marker\n?????\n", encoding="utf-8")
    assert read_report_marker(str(tmp_path / "junk.env")) is None


def test_marker_helpers_tolerate_garbage() -> None:
    assert marker_int({"quantity_mismatch": "n/a"}, "quantity_mismatch") is None
    assert marker_int({}, "quantity_mismatch") is None
    assert marker_age_seconds({}) is None
    assert marker_age_seconds({"checked_at_unixtime": "0"}) is None
    assert marker_age_seconds({"checked_at_unixtime": "bad"}) is None
    # A marker from the future must not produce a negative age.
    future = int((NOW + timedelta(hours=1)).timestamp())
    assert marker_age_seconds({"checked_at_unixtime": str(future)}, NOW) == 0.0


def test_log_report_summarises_findings(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1", filled=0.4, cost=800.0)])
    report = reconcile(tmp_path, exchange, [live_row("cid-1")])
    records: list[str] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    collector = _Collector()
    log = logging.getLogger("test.fill_reconciliation")
    log.addHandler(collector)
    log.setLevel(logging.INFO)
    try:
        log_report(report, log=log)
    finally:
        log.removeHandler(collector)
    assert any("Fill reconciliation:" in message for message in records)
    assert any("Fill reconciliation finding" in message for message in records)


def test_log_report_flags_incomplete_pass(tmp_path: Path) -> None:
    exchange = FakeExchange(fail=("fetch_closed_orders", "fetch_open_orders"))
    report = reconcile(tmp_path, exchange, [live_row("cid-1")])
    messages: list[str] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    log = logging.getLogger("test.fill_reconciliation.incomplete")
    collector = _Collector()
    log.addHandler(collector)
    log.setLevel(logging.INFO)
    try:
        log_report(report, log=log)
    finally:
        log.removeHandler(collector)
    assert any("incomplete" in message for message in messages)
    assert not any("Fill reconciliation finding" in message for message in messages)


def test_iter_findings_filters_by_status(tmp_path: Path) -> None:
    exchange = FakeExchange(closed=[exchange_order("cid-1"), exchange_order("cid-2", filled=0.1, cost=200.0)])
    report = reconcile(tmp_path, exchange, [live_row("cid-1"), live_row("cid-2", coin="BTC")])
    partial = iter_findings(report, [STATUS_PARTIAL])
    assert [finding.client_order_id for finding in partial] == ["cid-2"]
    assert iter_findings(report, [STATUS_UNMATCHED]) == []
