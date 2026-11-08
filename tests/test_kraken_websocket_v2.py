import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.kraken_websocket_v2 import KrakenWebSocketV2, Tick, TickStorageError

QUOTE = {
    "symbol": "ALGO/USD", "bid": 0.10025, "bid_qty": 740.0,
    "ask": 0.10036, "ask_qty": 1361.44813783, "last": 0.10035,
    "volume": 997038.98383185, "vwap": 0.10148, "low": 0.09979,
    "high": 0.10285, "change": -0.00017, "change_pct": -0.17,
    "timestamp": "2023-09-25T09:04:31.742648Z",
}
TRADE = {
    "symbol": "ALGO/USD", "side": "buy", "price": 0.10035,
    "qty": 42.5, "ord_type": "limit", "trade_id": 4665847,
    "timestamp": "2023-09-25T09:04:31.742600Z",
}


@pytest.fixture
def client():
    with patch("src.kraken_websocket_v2.Path.mkdir"), patch.object(
        KrakenWebSocketV2, "_init_db"
    ):
        client = KrakenWebSocketV2(["ALGO/USD", "BTC/USD"], buffer_path="unused")
    client.warn_free_bytes = 0
    client._store_tick = AsyncMock()
    client.tick_callback = AsyncMock()
    return client


def send(client, channel, entries, kind="update"):
    asyncio.run(client._handle_message(json.dumps({
        "channel": channel, "type": kind, "data": entries,
    })))


def test_v2_subscriptions_and_reconnect_resets_cache(client):
    ws = AsyncMock()
    client._quotes["ALGO/USD"] = QUOTE
    client._trades["ALGO/USD"] = TRADE
    with patch("src.kraken_websocket_v2.websockets.connect", new=AsyncMock(return_value=ws)):
        assert asyncio.run(client.connect())
    requests = [json.loads(call.args[0]) for call in ws.send.await_args_list]
    assert requests == [
        {"method": "subscribe", "params": {
            "channel": "ticker", "symbol": ["ALGO/USD"], "event_trigger": "bbo"}},
        {"method": "subscribe", "params": {
            "channel": "trade", "symbol": ["ALGO/USD"], "snapshot": True}},
        {"method": "subscribe", "params": {
            "channel": "ticker", "symbol": ["BTC/USD"], "event_trigger": "bbo"}},
        {"method": "subscribe", "params": {
            "channel": "trade", "symbol": ["BTC/USD"], "snapshot": True}},
    ]
    assert not client._quotes and not client._trades
    asyncio.run(client.disconnect())
    ws.close.assert_awaited_once()


@pytest.mark.parametrize("first", ["ticker", "trade"])
def test_complete_tick_uses_actual_trade_and_exchange_timestamp(client, first):
    entries = {"ticker": QUOTE, "trade": TRADE}
    second = "trade" if first == "ticker" else "ticker"
    send(client, first, [entries[first]], "snapshot")
    client.tick_callback.assert_not_awaited()
    send(client, second, [entries[second]], "snapshot")
    tick = client.tick_callback.await_args.args[0]
    assert tick == Tick(
        timestamp=1695632671.742648, symbol="ALGO/USD",
        bid=0.10025, ask=0.10036, mid=(0.10025 + 0.10036) / 2,
        bid_size=740.0, ask_size=1361.44813783,
        last_trade_price=0.10035, last_trade_size=42.5,
    )
    assert tick.last_trade_size != QUOTE["volume"]
    client._store_tick.assert_awaited_once_with(tick)
    assert client.ticks_received == 1


def test_batched_snapshots_updates_and_symbols(client):
    btc_quote = {**QUOTE, "symbol": "BTC/USD", "bid": 26000, "ask": 26001}
    btc_trade = {**TRADE, "symbol": "BTC/USD", "price": 26000.5, "qty": 0.3}
    send(client, "ticker", [QUOTE, btc_quote], "snapshot")
    send(client, "trade", [
        {**TRADE, "trade_id": 1, "qty": 1},
        {**TRADE, "trade_id": 3, "qty": 3},
        {**TRADE, "trade_id": 2, "qty": 2},
        btc_trade,
    ], "snapshot")
    ticks = [call.args[0] for call in client.tick_callback.await_args_list]
    assert [(t.symbol, t.last_trade_size) for t in ticks] == [
        ("ALGO/USD", 3), ("BTC/USD", 0.3)]
    send(client, "trade", [
        {**TRADE, "trade_id": 4, "qty": 4},
        {**TRADE, "trade_id": 5, "qty": 5},
        {**TRADE, "trade_id": 4, "qty": 999},
    ])
    assert [call.args[0].last_trade_size for call in
            client.tick_callback.await_args_list] == [3, 0.3, 4, 5]
    send(client, "ticker", [{**QUOTE, "ask": 0.101}])
    assert client.tick_callback.await_args.args[0].ask == 0.101
    assert client.tick_callback.await_args.args[0].last_trade_size == 5


@pytest.mark.parametrize("channel,entry", [
    ("ticker", {key: value for key, value in QUOTE.items() if key != "ask_qty"}),
    ("ticker", {**QUOTE, "bid": None}),
    ("ticker", {**QUOTE, "bid": float("nan")}),
    ("ticker", {**QUOTE, "timestamp": "2023-09-25T09:04:31"}),
    ("trade", {key: value for key, value in TRADE.items() if key != "qty"}),
    ("trade", {**TRADE, "qty": None}),
    ("trade", {**TRADE, "qty": 0}),
    ("trade", {**TRADE, "trade_id": None}),
    ("trade", {**TRADE, "symbol": "UNEXPECTED/USD"}),
])
def test_incomplete_or_invalid_entries_never_create_ticks(client, channel, entry):
    if channel == "ticker":
        send(client, "ticker", [entry])
        send(client, "trade", [TRADE])
    else:
        send(client, "ticker", [QUOTE])
        send(client, "trade", [entry])
    assert client.ticks_received == 0
    client.tick_callback.assert_not_awaited()
    client._store_tick.assert_not_awaited()


def test_invalid_entry_does_not_prevent_later_valid_entry(client):
    send(client, "ticker", [QUOTE])
    send(client, "trade", [{**TRADE, "qty": None}, TRADE])
    assert client.ticks_received == 1
    assert client.tick_callback.await_args.args[0].last_trade_size == 42.5


def test_stale_and_incomplete_updates_do_not_replace_valid_state(client):
    send(client, "ticker", [QUOTE])
    send(client, "trade", [TRADE])
    send(client, "ticker", [
        {**QUOTE, "bid": 0.9, "timestamp": "2023-09-25T09:04:30Z"},
        {**QUOTE, "ask_qty": None},
    ])
    send(client, "trade", [{**TRADE, "trade_id": 4665848, "qty": 8}])
    tick = client.tick_callback.await_args.args[0]
    assert tick.bid == QUOTE["bid"]
    assert tick.ask_size == QUOTE["ask_qty"]
    assert tick.last_trade_size == 8
    assert client.ticks_received == 2


def test_explicit_zero_quote_quantity_is_not_a_missing_default(client):
    send(client, "ticker", [{**QUOTE, "bid_qty": 0}])
    send(client, "trade", [TRADE])
    assert client.tick_callback.await_args.args[0].bid_size == 0


def test_callback_failure_is_not_treated_as_a_bad_entry(client):
    send(client, "ticker", [QUOTE])
    client.tick_callback.side_effect = ValueError("callback failed")
    with pytest.raises(ValueError, match="callback failed"):
        send(client, "trade", [TRADE])


def test_control_messages_ignored_and_subscription_failure_closes(client, caplog):
    ws = AsyncMock()
    client.ws, client.connected = ws, True
    for msg in [
        {"channel": "heartbeat"}, {"channel": "status", "data": [{}]},
        {"method": "subscribe", "success": True, "result": {"channel": "ticker"}},
        ["ticker", QUOTE], {"channel": "ticker", "type": "update", "data": {}},
    ]:
        asyncio.run(client._handle_message(json.dumps(msg)))
    asyncio.run(client._handle_message("{bad json"))
    assert client.connected
    asyncio.run(client._handle_message(json.dumps({
        "method": "subscribe", "success": False, "error": "Unknown symbol",
    })))
    assert not client.connected
    ws.close.assert_awaited_once()
    assert "Unknown symbol" in caplog.text
    client.tick_callback.assert_not_awaited()


def test_database_round_trip_and_retention(client):
    # Keep a shared in-memory SQLite database alive across client connections.
    uri = "file:kraken_test?mode=memory&cache=shared"
    real_connect = sqlite3.connect
    keeper = real_connect(uri, uri=True)
    try:
        with patch("src.kraken_websocket_v2.sqlite3.connect",
                   side_effect=lambda *args: real_connect(uri, uri=True)):
            client._init_db()
            client._store_tick = KrakenWebSocketV2._store_tick.__get__(client)
            send(client, "ticker", [QUOTE])
            send(client, "trade", [TRADE])
            with patch("src.kraken_websocket_v2.time.time", return_value=1695632672):
                ticks = client.get_recent_ticks("ALGO/USD", seconds=60)
            assert ticks == [client.tick_callback.await_args.args[0]]
            assert ticks[0].last_trade_size == 42.5
            client.retention_days = 7
            client.cleanup_old_ticks()
            assert keeper.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 0
    finally:
        keeper.close()


def test_storage_failure_is_reported_and_tick_is_not_sent(client, caplog):
    connection = MagicMock()
    connection.execute.side_effect = sqlite3.OperationalError("database is full")
    client.db_path = Path("ticks.db")
    client._store_tick = KrakenWebSocketV2._store_tick.__get__(client)
    client.tick_callback = AsyncMock()
    send(client, "ticker", [QUOTE])

    with patch(
        "src.kraken_websocket_v2.sqlite3.connect", return_value=connection
    ), pytest.raises(TickStorageError, match="database is full"):
        send(client, "trade", [TRADE])

    connection.close.assert_called_once()
    client.tick_callback.assert_not_awaited()
    assert client.ticks_received == 0
    assert "ticks.db" in caplog.text


def test_read_failure_is_not_reported_as_empty_result(client):
    connection = MagicMock()
    connection.execute.side_effect = sqlite3.OperationalError("I/O error")
    client.db_path = Path("ticks.db")

    with patch(
        "src.kraken_websocket_v2.sqlite3.connect", return_value=connection
    ), pytest.raises(TickStorageError, match="I/O error"):
        client.get_recent_ticks("ALGO/USD")

    connection.close.assert_called_once()


def test_database_page_limit_is_applied_and_retention_only_warns(
    client, tmp_path, caplog
):
    client.db_path = tmp_path / "kraken_ticks.db"
    client.buffer_path = tmp_path
    client.max_db_bytes = 64 * 1024
    client.warn_free_bytes = 0
    client._init_db()

    with client._database() as connection:
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        page_limit = connection.execute("PRAGMA max_page_count").fetchone()[0]
        with connection:
            connection.execute(
                """
                INSERT INTO ticks (
                    timestamp, symbol, bid, ask, mid, bid_size, ask_size,
                    last_trade_price, last_trade_size
                ) VALUES (0, 'ALGO/USD', 1, 2, 1.5, 1, 1, 1.5, 1)
                """
            )
    assert page_limit * page_size <= client.max_db_bytes

    client._check_storage_health(force=True)
    assert "retention is manual" in caplog.text
    with client._database() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 1


def test_database_quota_fails_writes_without_deleting_existing_ticks(
    client, tmp_path
):
    client.db_path = tmp_path / "kraken_ticks.db"
    client.buffer_path = tmp_path
    client.max_db_bytes = 0
    client.warn_free_bytes = 0
    client._init_db()
    with client._database() as connection:
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
    client.max_db_bytes = page_size * page_count

    client._store_tick = KrakenWebSocketV2._store_tick.__get__(client)
    persisted = 0
    with pytest.raises(TickStorageError, match="full"):
        for _ in range(500):
            asyncio.run(client._store_tick(Tick.from_kraken_message(QUOTE, TRADE)))
            persisted += 1

    with client._database() as connection:
        count = connection.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    assert count == persisted
    assert persisted > 0


def test_disconnect_releases_websocket_reference_even_if_close_fails(client):
    ws = AsyncMock()
    ws.close.side_effect = RuntimeError("close failed")
    client.ws, client.connected = ws, True

    with pytest.raises(RuntimeError, match="close failed"):
        asyncio.run(client.disconnect())

    assert client.ws is None
    assert not client.connected
