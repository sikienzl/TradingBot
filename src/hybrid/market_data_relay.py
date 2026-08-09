from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict

from src.hybrid.grpc_bridge import EdgeFilterRpcClient, grpc_available
from src.hybrid.monitoring import ServiceMetrics
from src.hybrid.transport import MarketSnapshotPayload
from src.kraken_websocket_v2 import Tick, kraken_websocket_session

logger = logging.getLogger(__name__)


class MarketDataRelayService:
    def __init__(self):
        self.pairs = os.getenv("CRYPTO_PAIRS", "BTC/USD,ETH/USD").split(",")
        self.window_size = int(os.getenv("HAILO8_WINDOW_SIZE", "100"))
        self.flush_every_ticks = int(
            os.getenv("HAILO8_RELAY_FLUSH_TICKS", "10"))
        self.node_name = os.getenv("NODE_NAME")
        self.target = os.getenv("HAILO8_EDGE_GRPC_TARGET",
                                "trading-hailo-worker:50051")
        self.metrics_port = int(os.getenv("HAILO8_RELAY_METRICS_PORT", "9203"))
        self.buffers: dict[str, list[dict]] = defaultdict(list)
        self.counters: dict[str, int] = defaultdict(int)
        self.client = EdgeFilterRpcClient(
            self.target) if grpc_available() else None
        self.service_metrics = ServiceMetrics(
            "market_relay", self.metrics_port)

    async def handle_tick(self, tick: Tick) -> None:
        started = time.perf_counter()
        tick_dict = tick.to_dict()
        buffer = self.buffers[tick.symbol]
        buffer.append(tick_dict)
        if len(buffer) > self.window_size:
            del buffer[0:len(buffer) - self.window_size]

        self.counters[tick.symbol] += 1
        self.service_metrics.observe_event("tick_ingress")
        self.service_metrics.set_value("buffer_size", len(buffer))
        self.service_metrics.set_value(f"ticks_received_{tick.symbol}", self.counters[tick.symbol])
        
        if len(buffer) < self.window_size:
            return
        if self.counters[tick.symbol] % self.flush_every_ticks != 0:
            return

        if self.client is None:
            logger.warning("grpc transport unavailable; skipping relay flush")
            self.service_metrics.observe_event("flush_skipped")
            return

        snapshot = MarketSnapshotPayload.from_tick_dicts(
            pair=tick.symbol,
            ticks=list(buffer),
            source="kraken-websocket-relay",
            node_name=self.node_name,
            metadata={"flush_every_ticks": self.flush_every_ticks},
        )
        ack = await self.client.submit_market_snapshot(snapshot)
        self.service_metrics.observe_event(
            "flush_snapshot", time.perf_counter() - started)
        self.service_metrics.set_value("flush_tick_count", len(buffer))
        if not ack.accepted:
            logger.warning(
                "Edge filter rejected snapshot for %s: %s", tick.symbol, ack.message)
            self.service_metrics.observe_event("flush_rejected")
        else:
            logger.debug(f"✅ Flushed {len(buffer)} ticks for {tick.symbol} in {(time.perf_counter()-started)*1000:.1f}ms")

    async def run(self) -> None:
        try:
            self.service_metrics.start_http_server()
            logger.info(f"✅ Market Relay Prometheus metrics server started on port {self.metrics_port}")
        except Exception as e:
            logger.error(f"Failed to start metrics server: {e}")
        
        self.service_metrics.set_up(True)
        logger.info("🔄 Market Relay service is running and listening for ticks...")
        async with kraken_websocket_session(
            pairs=self.pairs,
            tick_callback=self.handle_tick,
        ) as ws:
            await ws.listen_forever()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    service = MarketDataRelayService()
    await service.run()


if __name__ == "__main__":
    asyncio.run(main())
