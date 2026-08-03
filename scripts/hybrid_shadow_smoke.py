#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


def build_snapshot():
    from src.hybrid.transport import MarketSnapshotPayload

    ticks = []
    for index in range(120):
        bid = 100.0 + index * 0.8
        ask = bid + (0.6 if index < 118 else 4.5)
        bid_size = 1.5 + (0.02 * index)
        ask_size = 1.0 if index < 118 else 0.25
        ticks.append(
            {
                "timestamp": float(index),
                "symbol": "BTC/USD",
                "bid": bid,
                "ask": ask,
                "mid": (bid + ask) / 2.0,
                "bid_size": bid_size,
                "ask_size": ask_size,
                "last_trade_price": ask,
                "last_trade_size": 0.2 + (0.01 * index),
                "spread": ask - bid,
            }
        )
    return MarketSnapshotPayload.from_tick_dicts(
        pair="BTC/USD",
        ticks=ticks,
        source="hybrid-smoke-test",
        node_name="local-dev",
        metadata={"purpose": "shadow-mode-smoke-test"},
    )


async def run() -> None:
    from src.cloud.strategist_service import GPT5StrategistService
    from src.hailo.edge_filter_service import HailoEdgeFilterService
    from src.hybrid.decision_gate import AnomalyAlert

    os.environ.setdefault("GPT5_SHADOW_MODE", "true")
    os.environ.setdefault("HYBRID_GATE_ENABLED", "true")
    os.environ.setdefault("HAILO8_ANOMALY_THRESHOLD", "75")
    os.environ.setdefault(
        "HAILO8_MODEL_PATH", "./model/hailo_prefilter/timeseries_transformer.onnx"
    )
    os.environ.setdefault(
        "HAILO8_MODEL_CONFIG_PATH", "./model/hailo_prefilter/model_config.json"
    )

    strategist = GPT5StrategistService()
    edge = HailoEdgeFilterService()
    captured: list[dict] = []

    async def fake_emit(alert: AnomalyAlert) -> None:
        decision = await strategist.process_anomaly_alert(alert)
        captured.append(
            {
                "alert": {
                    "coin": alert.coin,
                    "hailo_score": alert.hailo_score,
                    "signal_type": alert.signal_type,
                    "confidence": alert.confidence,
                },
                "decision": decision,
            }
        )

    edge.emit_alert = fake_emit  # type: ignore[method-assign]

    ack = await edge.process_market_snapshot(build_snapshot())
    if not captured:
        fallback_alert = AnomalyAlert(
            timestamp=datetime.now(UTC),
            hailo_score=92.0,
            window_size=100,
            signal_type="smoke_breakout",
            confidence=0.88,
            market_context={
                "source": "synthetic-fallback", "volatility": 0.04},
            coin="BTC/USD",
        )
        await fake_emit(fallback_alert)

    result = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "snapshot_ack": ack.to_dict(),
        "edge_metrics": edge.get_metrics(),
        "strategist_metrics": strategist.get_metrics(),
        "decisions": captured,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(run())
