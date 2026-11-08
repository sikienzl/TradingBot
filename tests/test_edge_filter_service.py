import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from src.hailo.edge_filter_service import HailoEdgeFilterService
from src.hybrid.grpc_bridge import deserialize_message, serialize_message
from src.hybrid.market_data_relay import MarketDataRelayService
from src.hybrid.transport import MarketSnapshotPayload
from src.kraken_websocket_v2 import Tick


def test_process_market_snapshot_returns_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAILO8_MARKETDATA_MODE", "grpc")
    service = _edge(monkeypatch)

    calls: list[tuple[dict, str]] = []

    async def fake_process_tick_dict(tick_dict: dict, symbol: str):
        calls.append((tick_dict, symbol))

    # type: ignore[method-assign]
    service.process_tick_dict = fake_process_tick_dict

    snapshot = MarketSnapshotPayload.from_tick_dicts(
        pair="BTC/USD",
        ticks=[
            {
                "timestamp": 1.0,
                "symbol": "BTC/USD",
                "bid": 100.0,
                "ask": 101.0,
                "mid": 100.5,
                "bid_size": 2.0,
                "ask_size": 1.5,
                "last_trade_price": 100.7,
                "last_trade_size": 0.2,
            },
            {
                "timestamp": 2.0,
                "symbol": "BTC/USD",
                "bid": 102.0,
                "ask": 103.0,
                "mid": 102.5,
                "bid_size": 2.2,
                "ask_size": 1.7,
                "last_trade_price": 102.7,
                "last_trade_size": 0.4,
            },
        ],
    )

    ack = asyncio.run(service.process_market_snapshot(snapshot))

    assert ack.accepted is True
    assert ack.metadata["pair"] == "BTC/USD"
    assert ack.metadata["tick_count"] == 2
    assert len(calls) == 2

    # Legacy senders still process complete windows, as before.
    asyncio.run(service.process_market_snapshot(snapshot))
    assert len(calls) == 4


def _tick(timestamp: float, pair: str = "BTC/USD") -> Tick:
    return Tick(timestamp, pair, 100, 101, 100.5, 2, 1, 100, 1)


def _snapshot(pair: str, numbers: list[int], stream: str = "stream") -> MarketSnapshotPayload:
    return MarketSnapshotPayload.from_tick_dicts(
        pair=pair,
        ticks=[_tick(n, pair).to_dict() for n in numbers],
        metadata={
            "relay_stream_id": stream,
            "relay_first_sequence": numbers[0],
            "relay_last_sequence": numbers[-1],
        },
    )


def _edge(monkeypatch: pytest.MonkeyPatch) -> HailoEdgeFilterService:
    monkeypatch.setattr(
        "src.hailo.edge_filter_service.TimeSeriesTransformerONNX",
        Mock(side_effect=RuntimeError("test: model unavailable")),
    )
    monkeypatch.delenv("CLOUD_STRATEGIST_GRPC_TARGET", raising=False)
    service = HailoEdgeFilterService()
    service.process_tick_dict = AsyncMock(return_value=None)
    return service


def test_tick_handler_propagates_processing_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    edge = _edge(monkeypatch)
    edge.process_tick_dict = AsyncMock(side_effect=RuntimeError("tick failed"))
    tick = _tick(1)

    with pytest.raises(RuntimeError, match="tick failed"):
        asyncio.run(edge.tick_handler(tick))

    edge.process_tick_dict.assert_awaited_once_with(tick.to_dict(), tick.symbol)


def test_relay_windows_process_only_new_ticks_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAILO8_WINDOW_SIZE", "4")
    monkeypatch.setenv("HAILO8_RELAY_FLUSH_TICKS", "2")
    monkeypatch.setattr("src.hybrid.market_data_relay.grpc_available", lambda: False)
    relay = MarketDataRelayService()
    edge = _edge(monkeypatch)
    snapshots = []

    async def submit(snapshot):
        snapshots.append(snapshot)
        wire_snapshot = deserialize_message(serialize_message(snapshot), MarketSnapshotPayload.from_dict)
        return await edge.process_market_snapshot(wire_snapshot)

    relay.client = Mock(submit_market_snapshot=AsyncMock(side_effect=submit))

    async def run():
        for n in range(1, 9):
            # Identical timestamps and values are still distinct ingress events.
            await relay.handle_tick(_tick(1))
        retry = await edge.process_market_snapshot(snapshots[-1])
        assert retry.metadata["processed_tick_count"] == 0

    asyncio.run(run())
    assert len(snapshots) == 3
    assert [(s.metadata["relay_first_sequence"], s.metadata["relay_last_sequence"])
            for s in snapshots] == [(1, 4), (3, 6), (5, 8)]
    assert edge.process_tick_dict.await_count == 8


def test_sequence_pair_stream_gap_and_old_snapshot(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    edge = _edge(monkeypatch)

    async def run():
        assert (await edge.process_market_snapshot(_snapshot("BTC/USD", [1, 2]))).accepted
        assert (await edge.process_market_snapshot(_snapshot("BTC/USD", [4, 5]))).accepted
        old = await edge.process_market_snapshot(_snapshot("BTC/USD", [1, 2]))
        assert old.metadata["processed_tick_count"] == 0
        await edge.process_market_snapshot(_snapshot("ETH/USD", [1, 2]))
        await edge.process_market_snapshot(_snapshot("BTC/USD", [1, 2], "new-stream"))

    asyncio.run(run())
    assert edge.process_tick_dict.await_count == 8
    assert "Relay sequence gap" in caplog.text


def test_invalid_sequence_metadata_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    edge = _edge(monkeypatch)
    snapshot = _snapshot("BTC/USD", [1, 2])
    snapshot.metadata["relay_last_sequence"] = 3
    ack = asyncio.run(edge.process_market_snapshot(snapshot))
    assert not ack.accepted
    edge.process_tick_dict.assert_not_awaited()

    snapshot.metadata = {"relay_stream_id": "stream"}
    ack = asyncio.run(edge.process_market_snapshot(snapshot))
    assert not ack.accepted
    edge.process_tick_dict.assert_not_awaited()


def test_concurrent_retries_are_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    edge = _edge(monkeypatch)

    async def slow_tick(*_args):
        await asyncio.sleep(0)

    edge.process_tick_dict = AsyncMock(side_effect=slow_tick)

    async def run():
        snapshot = _snapshot("BTC/USD", [1, 2, 3])
        return await asyncio.gather(
            edge.process_market_snapshot(snapshot), edge.process_market_snapshot(snapshot)
        )

    acks = asyncio.run(run())
    assert sorted(ack.metadata["processed_tick_count"] for ack in acks) == [0, 3]
    assert edge.process_tick_dict.await_count == 3


def test_overlapping_snapshots_update_real_tick_handler_once(monkeypatch: pytest.MonkeyPatch) -> None:
    edge = _edge(monkeypatch)
    edge.process_tick_dict = HailoEdgeFilterService.process_tick_dict.__get__(edge)
    edge.model = Mock(preprocessing_version="legacy", last_inference_seconds=0.0)
    edge.detector = Mock()
    edge.detector.update.return_value = None
    edge.inference_enabled = True

    async def run():
        await edge.process_market_snapshot(_snapshot("BTC/USD", [1, 2, 3]))
        await edge.process_market_snapshot(_snapshot("BTC/USD", [2, 3, 4]))

    asyncio.run(run())
    assert edge.metrics["ticks_processed"] == 4
    assert [call.args[0]["timestamp"] for call in edge.detector.update.call_args_list] == [1, 2, 3, 4]


def test_cancelled_snapshot_blocks_pair_without_replaying(monkeypatch: pytest.MonkeyPatch) -> None:
    edge = _edge(monkeypatch)
    processed = []

    async def process(tick, _pair):
        processed.append(tick["timestamp"])
        if tick["timestamp"] == 2:
            raise asyncio.CancelledError()

    edge.process_tick_dict = process

    async def run():
        snapshot = _snapshot("BTC/USD", [1, 2, 3])
        with pytest.raises(asyncio.CancelledError):
            await edge.process_market_snapshot(snapshot)
        ack = await edge.process_market_snapshot(snapshot)
        assert not ack.accepted
        assert ack.metadata["stream_blocked"]
        assert edge._relay_sequences[("BTC/USD", "stream")] == 1

    asyncio.run(run())
    assert processed == [1, 2]


@pytest.mark.parametrize("failure_point", ["update", "alert"])
def test_tick_failure_blocks_pair_without_advancing_failed_sequence(
    monkeypatch: pytest.MonkeyPatch, failure_point: str,
) -> None:
    edge = _edge(monkeypatch)
    edge.process_tick_dict = HailoEdgeFilterService.process_tick_dict.__get__(edge)
    edge.model = Mock(preprocessing_version="legacy", last_inference_seconds=0.0)
    edge.detector = Mock()
    edge.inference_enabled = True
    seen = []

    def update(tick):
        seen.append(tick["timestamp"])
        if tick["timestamp"] != 2:
            return None
        if failure_point == "update":
            raise RuntimeError("update already mutated detector")
        return {
            "anomaly_score": 90, "signal_type": "breakout", "window_size": 2,
            "confidence": 0.9,
        }

    edge.detector.update.side_effect = update
    edge.detector.get_buffer_stats.return_value = {}
    edge.emit_alert = AsyncMock(side_effect=RuntimeError("alert delivery uncertain"))

    async def run():
        snapshot = _snapshot("BTC/USD", [1, 2, 3])
        ack = await edge.process_market_snapshot(snapshot)
        assert not ack.accepted
        assert ack.metadata["processed_tick_count"] == 1
        assert ack.metadata["failed_sequence"] == 2
        assert ack.metadata["stream_blocked"]
        assert edge._relay_sequences[("BTC/USD", "stream")] == 1
        assert edge.metrics["ticks_processed"] == 1

        # A retry, an overlapping future window, a new stream ID, and a
        # legacy sender must all fail closed for the same pair.
        legacy = MarketSnapshotPayload.from_tick_dicts(
            pair="BTC/USD", ticks=[_tick(2).to_dict()]
        )
        for retry in (
            snapshot, _snapshot("BTC/USD", [2, 3, 4]),
            _snapshot("BTC/USD", [1, 2], "new-stream"), legacy,
        ):
            blocked = await edge.process_market_snapshot(retry)
            assert not blocked.accepted
            assert blocked.metadata["stream_blocked"]
        assert seen == [1, 2]
        assert (await edge.process_market_snapshot(_snapshot("ETH/USD", [1]))).accepted

    asyncio.run(run())
    assert edge.emit_alert.await_count == (1 if failure_point == "alert" else 0)


def test_legacy_tick_failure_blocks_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    edge = _edge(monkeypatch)
    edge.process_tick_dict = AsyncMock(side_effect=RuntimeError("failed"))
    legacy = MarketSnapshotPayload.from_tick_dicts(
        pair="BTC/USD", ticks=[_tick(1).to_dict()]
    )

    async def run():
        ack = await edge.process_market_snapshot(legacy)
        assert not ack.accepted
        assert ack.metadata["failed_sequence"] is None
        assert not (await edge.process_market_snapshot(legacy)).accepted
        assert not (await edge.process_market_snapshot(_snapshot("BTC/USD", [1]))).accepted

    asyncio.run(run())
    edge.process_tick_dict.assert_awaited_once()


def test_cancellation_after_detector_update_blocks_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    edge = _edge(monkeypatch)
    edge.process_tick_dict = HailoEdgeFilterService.process_tick_dict.__get__(edge)
    edge.model = Mock(preprocessing_version="legacy", last_inference_seconds=0.0)
    edge.detector = Mock()
    edge.inference_enabled = True
    edge.detector.update.return_value = {
        "anomaly_score": 90, "signal_type": "breakout", "window_size": 1,
        "confidence": 0.9,
    }
    edge.detector.get_buffer_stats.return_value = {}

    async def run():
        entered = asyncio.Event()

        async def emit(_alert):
            entered.set()
            await asyncio.Event().wait()

        edge.emit_alert = emit
        snapshot = _snapshot("BTC/USD", [1])
        task = asyncio.create_task(edge.process_market_snapshot(snapshot))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        ack = await edge.process_market_snapshot(snapshot)
        assert not ack.accepted
        assert ack.metadata["stream_blocked"]
        assert ("BTC/USD", "stream") not in edge._relay_sequences

    asyncio.run(run())
    edge.detector.update.assert_called_once()
    assert edge.metrics["ticks_processed"] == 0
