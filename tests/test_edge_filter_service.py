import pytest

from src.hailo.edge_filter_service import HailoEdgeFilterService
from src.hybrid.transport import MarketSnapshotPayload


@pytest.mark.anyio
async def test_process_market_snapshot_returns_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAILO8_MARKETDATA_MODE", "grpc")
    service = HailoEdgeFilterService()

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

    ack = await service.process_market_snapshot(snapshot)

    assert ack.accepted is True
    assert ack.metadata["pair"] == "BTC/USD"
    assert ack.metadata["tick_count"] == 2
    assert len(calls) == 2
