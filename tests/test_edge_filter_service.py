import asyncio
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from src.hailo.edge_filter_service import HailoEdgeFilterService
from src.hailo.inference import AnomalyDetector
from src.hailo.preprocessing import PREPROCESSING_VERSION
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
    edge.detectors["BTC/USD"] = Mock()
    edge.detectors["BTC/USD"].update.return_value = None
    edge.inference_enabled = True

    async def run():
        await edge.process_market_snapshot(_snapshot("BTC/USD", [1, 2, 3]))
        await edge.process_market_snapshot(_snapshot("BTC/USD", [2, 3, 4]))

    asyncio.run(run())
    assert edge.metrics["ticks_processed"] == 4
    assert [call.args[0]["timestamp"] for call in edge.detectors["BTC/USD"].update.call_args_list] == [1, 2, 3, 4]


def test_service_creates_distinct_detectors_with_one_shared_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRYPTO_PAIRS", "BTC/USD,ETH/USD")
    monkeypatch.setenv("HAILO8_WINDOW_SIZE", "2")
    model = Mock(preprocessing_version="legacy", seq_length=2,
                 last_inference_seconds=0.0, last_provider="test")
    factory = Mock(return_value=model)
    monkeypatch.setattr("src.hailo.edge_filter_service.TimeSeriesTransformerONNX", factory)
    service = HailoEdgeFilterService()
    btc, eth = service.detectors["BTC/USD"], service.detectors["ETH/USD"]
    factory.assert_called_once()
    assert btc is not eth
    assert btc.model is eth.model is model
    assert btc.tick_buffer is not eth.tick_buffer


@pytest.mark.parametrize("version", ["legacy", PREPROCESSING_VERSION])
def test_real_detectors_keep_interleaved_pair_windows_separate(
    monkeypatch: pytest.MonkeyPatch, version: str,
) -> None:
    edge = _edge(monkeypatch)
    edge.process_tick_dict = HailoEdgeFilterService.process_tick_dict.__get__(edge)
    model = Mock(preprocessing_version=version, seq_length=2,
                 last_inference_seconds=0.0, last_provider="test")
    windows = []

    def infer(ticks):
        windows.append([(t["symbol"], t["timestamp"], t["mid"]) for t in ticks])
        return 90.0, 0.9

    model.infer.side_effect = infer
    edge.model = model
    edge.detectors = {pair: AnomalyDetector(model, window_size=2) for pair in edge.pairs}
    edge.inference_enabled = True
    edge.emit_alert = AsyncMock()

    async def run():
        for symbol, timestamp, price in (
            ("BTC/USD", 100, 100), ("ETH/USD", 50, 10),
            ("BTC/USD", 101, 101), ("ETH/USD", 51, 11),
        ):
            await edge.tick_handler(Tick(timestamp, symbol, price, price + 1,
                                         price + 0.5, 2, 1, price, 1))

    asyncio.run(run())
    assert windows == [
        [("BTC/USD", 100, 100.5), ("BTC/USD", 101, 101.5)],
        [("ETH/USD", 50, 10.5), ("ETH/USD", 51, 11.5)],
    ]
    alerts = [call.args[0] for call in edge.emit_alert.await_args_list]
    assert [(a.coin, a.market_context["min_price"]) for a in alerts] == [
        ("BTC/USD", 100.5), ("ETH/USD", 10.5),
    ]
    stats = edge.get_metrics()["detector_buffer_stats"]
    assert stats["BTC/USD"]["avg_price"] == 101.0
    assert stats["ETH/USD"]["avg_price"] == 11.0
    assert edge.get_metrics()["detector_alerts"] == 2


def test_tick_window_v1_timestamp_failure_is_isolated_by_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edge = _edge(monkeypatch)
    edge.process_tick_dict = HailoEdgeFilterService.process_tick_dict.__get__(edge)
    model = Mock(preprocessing_version=PREPROCESSING_VERSION, seq_length=2,
                 last_inference_seconds=0.0, last_provider="test")
    model.infer.return_value = (0, 0)
    edge.model = model
    edge.detectors = {pair: AnomalyDetector(model, window_size=2) for pair in edge.pairs}
    edge.inference_enabled = True

    async def run():
        assert (await edge.process_market_snapshot(_snapshot("BTC/USD", [100, 101]))).accepted
        assert (await edge.process_market_snapshot(_snapshot("ETH/USD", [1, 2]))).accepted
        bad = _snapshot("BTC/USD", [102])
        bad.ticks[0].timestamp = 99
        ack = await edge.process_market_snapshot(bad)
        assert not ack.accepted and ack.metadata["stream_blocked"]
        assert (await edge.process_market_snapshot(_snapshot("ETH/USD", [2, 3]))).accepted
        equal = _snapshot("ETH/USD", [4])
        equal.ticks[0].timestamp = 3
        assert (await edge.process_market_snapshot(equal)).accepted
        assert not (await edge.process_market_snapshot(_snapshot("BTC/USD", [102]))).accepted

    asyncio.run(run())
    assert [t["timestamp"] for t in edge.detectors["BTC/USD"].tick_buffer] == [100, 101]
    assert [t["timestamp"] for t in edge.detectors["ETH/USD"].tick_buffer] == [3, 3]


def test_tick_lock_serializes_direct_and_snapshot_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    edge = _edge(monkeypatch)
    edge.process_tick_dict = HailoEdgeFilterService.process_tick_dict.__get__(edge)
    model = Mock(preprocessing_version="legacy", seq_length=1,
                 last_inference_seconds=0.0, last_provider="test")
    model.infer.return_value = (90, 0.9)
    edge.model = model
    edge.detectors = {pair: AnomalyDetector(model, window_size=1) for pair in edge.pairs}
    edge.inference_enabled = True

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def emit(alert):
            if alert.coin == "BTC/USD":
                entered.set()
                await release.wait()

        edge.emit_alert = emit
        first = asyncio.create_task(edge.tick_handler(_tick(1)))
        await entered.wait()
        second = asyncio.create_task(edge.process_market_snapshot(_snapshot("ETH/USD", [1])))
        await asyncio.sleep(0)
        assert model.infer.call_count == 1
        release.set()
        await first
        assert (await second).accepted
        assert model.infer.call_count == 2

    asyncio.run(run())


def test_unconfigured_pair_is_rejected_without_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    edge = _edge(monkeypatch)
    ack = asyncio.run(edge.process_market_snapshot(_snapshot("DOGE/USD", [1])))
    assert not ack.accepted
    edge.process_tick_dict.assert_not_awaited()


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
    edge.detectors["BTC/USD"] = Mock()
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

    edge.detectors["BTC/USD"].update.side_effect = update
    edge.detectors["BTC/USD"].get_buffer_stats.return_value = {}
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
    edge.detectors["BTC/USD"] = Mock()
    edge.inference_enabled = True
    edge.detectors["BTC/USD"].update.return_value = {
        "anomaly_score": 90, "signal_type": "breakout", "window_size": 1,
        "confidence": 0.9,
    }
    edge.detectors["BTC/USD"].get_buffer_stats.return_value = {}

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
    edge.detectors["BTC/USD"].update.assert_called_once()
    assert edge.metrics["ticks_processed"] == 0


def test_invalid_tick_rejects_entire_snapshot_before_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    edge = _edge(monkeypatch)
    snapshot = _snapshot("BTC/USD", [1, 2])
    snapshot.ticks[1].symbol = "ETH/USD"
    ack = asyncio.run(edge.process_market_snapshot(snapshot))
    assert not ack.accepted
    edge.process_tick_dict.assert_not_awaited()
    snapshot.ticks[1].symbol = "BTC/USD"
    snapshot.ticks[1].bid = float("nan")
    assert not asyncio.run(edge.process_market_snapshot(snapshot)).accepted
    edge.process_tick_dict.assert_not_awaited()


def test_relay_transport_failure_is_visible_and_later_ticks_continue(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("HAILO8_WINDOW_SIZE", "1")
    monkeypatch.setenv("HAILO8_RELAY_FLUSH_TICKS", "1")
    monkeypatch.setattr("src.hybrid.market_data_relay.grpc_available", lambda: False)
    relay = MarketDataRelayService()
    relay.service_metrics = Mock()
    edge = _edge(monkeypatch)
    async def submit(snapshot):
        if snapshot.metadata["relay_last_sequence"] == 1:
            raise RuntimeError("network failure")
        return await edge.process_market_snapshot(snapshot)

    relay.client = Mock(submit_market_snapshot=AsyncMock(side_effect=submit))

    async def run():
        await relay.handle_tick(_tick(1))
        await relay.handle_tick(_tick(2))

    asyncio.run(run())
    assert relay.client.submit_market_snapshot.await_count == 2
    assert "Snapshot transport failed" in caplog.text
    assert "Relay sequence gap" in caplog.text
    relay.service_metrics.observe_event.assert_any_call("flush_inference_disabled")
    assert any(call.args[0] == "flush_failed" for call in relay.service_metrics.observe_event.call_args_list)


def test_alert_transport_failure_blocks_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAILO8_ALERT_FALLBACK_MODE", "fail_closed")
    edge = _edge(monkeypatch)
    file_open = Mock()
    monkeypatch.setattr("src.hailo.edge_filter_service.open", file_open, raising=False)
    edge.process_tick_dict = HailoEdgeFilterService.process_tick_dict.__get__(edge)
    edge.model = Mock(preprocessing_version="legacy", last_inference_seconds=0.0)
    edge.detectors["BTC/USD"] = Mock()
    edge.inference_enabled = True
    edge.detectors["BTC/USD"].update.return_value = {
        "anomaly_score": 90, "signal_type": "breakout", "window_size": 1,
        "confidence": 0.9,
    }
    edge.detectors["BTC/USD"].get_buffer_stats.return_value = {}
    edge.strategist_client = Mock(process_anomaly_alert=AsyncMock(side_effect=RuntimeError("offline")))
    edge.service_metrics = Mock()

    async def run():
        ack = await edge.process_market_snapshot(_snapshot("BTC/USD", [1]))
        assert not ack.accepted and ack.metadata["stream_blocked"]
        assert not (await edge.process_market_snapshot(_snapshot("BTC/USD", [1]))).accepted

    asyncio.run(run())
    edge.service_metrics.observe_event.assert_any_call("alert_forward_failed")
    edge.detectors["BTC/USD"].update.assert_called_once()
    file_open.assert_not_called()


def test_missing_strategist_is_not_silently_acknowledged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAILO8_ALERT_FALLBACK_MODE", "fail_closed")
    edge = _edge(monkeypatch)
    file_open = Mock()
    monkeypatch.setattr("src.hailo.edge_filter_service.open", file_open, raising=False)
    edge.process_tick_dict = HailoEdgeFilterService.process_tick_dict.__get__(edge)
    edge.model = Mock(preprocessing_version="legacy", last_inference_seconds=0.0)
    edge.detectors["BTC/USD"] = Mock()
    edge.inference_enabled = True
    edge.detectors["BTC/USD"].update.return_value = {
        "anomaly_score": 90, "signal_type": "breakout", "window_size": 1,
        "confidence": 0.9,
    }
    edge.detectors["BTC/USD"].get_buffer_stats.return_value = {}
    edge.service_metrics = Mock()
    ack = asyncio.run(edge.process_market_snapshot(_snapshot("BTC/USD", [1])))
    assert not ack.accepted and ack.metadata["stream_blocked"]
    edge.service_metrics.observe_event.assert_any_call("alert_forward_unavailable")
    file_open.assert_not_called()


@pytest.mark.parametrize("grpc_failure", [False, True])
def test_legacy_fallback_appends_and_acknowledges_local_write(
    monkeypatch: pytest.MonkeyPatch, grpc_failure: bool,
) -> None:
    path = Path.cwd() / f".test_hailo_alerts_{uuid.uuid4().hex}.jsonl"
    monkeypatch.setenv("HAILO8_ALERT_FALLBACK_MODE", "legacy_jsonl")
    monkeypatch.setenv("HAILO8_ALERT_FALLBACK_PATH", str(path))
    edge = _edge(monkeypatch)
    edge.process_tick_dict = HailoEdgeFilterService.process_tick_dict.__get__(edge)
    edge.model = Mock(preprocessing_version="legacy", last_inference_seconds=0.0)
    edge.detectors["BTC/USD"] = Mock()
    edge.inference_enabled = True
    edge.detectors["BTC/USD"].update.return_value = {
        "anomaly_score": 90, "signal_type": "breakout", "window_size": 1,
        "confidence": 0.9,
    }
    edge.detectors["BTC/USD"].get_buffer_stats.return_value = {}
    edge.service_metrics = Mock()
    if grpc_failure:
        edge.strategist_client = Mock(
            process_anomaly_alert=AsyncMock(side_effect=RuntimeError("offline"))
        )

    try:
        path.write_text('{"existing":true}\n', encoding="utf-8")
        ack = asyncio.run(edge.process_market_snapshot(_snapshot("BTC/USD", [1])))
        assert ack.accepted and ack.metadata["alert_emitted"]
        lines = path.read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[0]) == {"existing": True}
        assert len(lines) == 2
        record = json.loads(lines[1])
        assert record["coin"] == "BTC/USD"
        assert record["hailo_score"] == 90
        assert record["metadata"]["transport"] == ("grpc" if grpc_failure else "jsonl")
        assert edge.metrics["alerts_forwarded"] == 0
        edge.service_metrics.observe_event.assert_any_call("alert_fallback_written")
        edge.service_metrics.observe_event.assert_any_call(
            "alert_forward_failed" if grpc_failure else "alert_forward_unavailable"
        )
    finally:
        path.unlink(missing_ok=True)


def test_legacy_fallback_write_failure_blocks_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAILO8_ALERT_FALLBACK_MODE", "legacy_jsonl")
    monkeypatch.setenv("HAILO8_ALERT_FALLBACK_PATH", "alerts.jsonl")
    edge = _edge(monkeypatch)
    edge.process_tick_dict = HailoEdgeFilterService.process_tick_dict.__get__(edge)
    edge.model = Mock(preprocessing_version="legacy", last_inference_seconds=0.0)
    edge.detectors["BTC/USD"] = Mock()
    edge.inference_enabled = True
    edge.detectors["BTC/USD"].update.return_value = {
        "anomaly_score": 90, "signal_type": "breakout", "window_size": 1,
        "confidence": 0.9,
    }
    edge.detectors["BTC/USD"].get_buffer_stats.return_value = {}
    edge.service_metrics = Mock()
    monkeypatch.setattr(
        "src.hailo.edge_filter_service.open",
        Mock(side_effect=OSError("disk full")), raising=False,
    )
    async def run():
        ack = await edge.process_market_snapshot(_snapshot("BTC/USD", [1]))
        assert not ack.accepted and ack.metadata["stream_blocked"]
        assert not (await edge.process_market_snapshot(_snapshot("BTC/USD", [1]))).accepted

    asyncio.run(run())
    edge.service_metrics.observe_event.assert_any_call("alert_fallback_failed")
    edge.detectors["BTC/USD"].update.assert_called_once()


def test_legacy_mode_does_not_write_when_grpc_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAILO8_ALERT_FALLBACK_MODE", "legacy_jsonl")
    monkeypatch.setenv("HAILO8_ALERT_FALLBACK_PATH", "alerts.jsonl")
    edge = _edge(monkeypatch)
    edge.process_tick_dict = HailoEdgeFilterService.process_tick_dict.__get__(edge)
    edge.model = Mock(preprocessing_version="legacy", last_inference_seconds=0.0)
    edge.detectors["BTC/USD"] = Mock()
    edge.inference_enabled = True
    edge.detectors["BTC/USD"].update.return_value = {
        "anomaly_score": 90, "signal_type": "breakout", "window_size": 1,
        "confidence": 0.9,
    }
    edge.detectors["BTC/USD"].get_buffer_stats.return_value = {}
    edge.strategist_client = Mock(process_anomaly_alert=AsyncMock(return_value=Mock()))
    file_open = Mock()
    monkeypatch.setattr("src.hailo.edge_filter_service.open", file_open, raising=False)

    ack = asyncio.run(edge.process_market_snapshot(_snapshot("BTC/USD", [1])))

    assert ack.accepted
    assert edge.metrics["alerts_forwarded"] == 1
    file_open.assert_not_called()


def test_fallback_defaults_to_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HAILO8_ALERT_FALLBACK_MODE", raising=False)
    assert _edge(monkeypatch).alert_fallback_mode == "fail_closed"


def test_legacy_fallback_requires_explicit_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAILO8_ALERT_FALLBACK_MODE", "legacy_jsonl")
    monkeypatch.delenv("HAILO8_ALERT_FALLBACK_PATH", raising=False)
    with pytest.raises(ValueError, match="HAILO8_ALERT_FALLBACK_PATH"):
        HailoEdgeFilterService()


def test_unknown_fallback_mode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAILO8_ALERT_FALLBACK_MODE", "legacy")
    with pytest.raises(ValueError, match="HAILO8_ALERT_FALLBACK_MODE"):
        HailoEdgeFilterService()


def test_relay_rejects_invalid_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAILO8_RELAY_FLUSH_TICKS", "0")
    with pytest.raises(ValueError, match="positive"):
        MarketDataRelayService()
