from datetime import datetime, timezone

from src.hybrid.decision_gate import AnomalyAlert
from src.hybrid.transport import EdgeAlertPayload, MarketSnapshotPayload


def test_market_snapshot_roundtrip_preserves_tick_payloads() -> None:
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
            }
        ],
        source="test-feed",
        node_name="pi-master",
        metadata={"window": 1},
    )

    restored = MarketSnapshotPayload.from_dict(snapshot.to_dict())

    assert restored.pair == "BTC/USD"
    assert restored.source == "test-feed"
    assert restored.node_name == "pi-master"
    assert restored.metadata["window"] == 1
    assert restored.tick_dicts()[0]["mid"] == 100.5


def test_edge_alert_roundtrip_restores_domain_alert() -> None:
    domain_alert = AnomalyAlert(
        timestamp=datetime(2026, 7, 10, tzinfo=timezone.utc),
        hailo_score=91.2,
        window_size=100,
        signal_type="breakout",
        confidence=0.83,
        market_context={"volatility": 0.03},
        coin="ETH/USD",
    )

    payload = EdgeAlertPayload.from_anomaly_alert(
        domain_alert,
        source_node="pi-worker",
        metadata={"transport": "grpc"},
    )
    restored = EdgeAlertPayload.from_dict(payload.to_dict()).to_anomaly_alert()

    assert restored.coin == "ETH/USD"
    assert restored.signal_type == "breakout"
    assert restored.market_context["volatility"] == 0.03
    assert restored.hailo_score == 91.2
