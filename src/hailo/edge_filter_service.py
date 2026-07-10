"""
Hailo-8 Edge Filter Service

Main service loop for Node 2 (Hailo-8 Worker).
Connects to Kraken WebSocket, runs continuous inference, buffers ticks to NVMe.

24/7 high-frequency filtering: only alerts GPT-5 when anomaly detected.
"""

import os
import asyncio
import logging
import json
import time
from datetime import datetime, timedelta
from typing import Optional

from src.kraken_websocket_v2 import KrakenWebSocketV2, Tick, kraken_websocket_session
from src.hailo.inference import TimeSeriesTransformerONNX, AnomalyDetector
from src.hybrid.decision_gate import AnomalyAlert
from src.hybrid.grpc_bridge import EdgeFilterRpcServer, StrategistRpcClient, grpc_available
from src.hybrid.monitoring import ServiceMetrics
from src.hybrid.transport import EdgeAlertPayload, MarketSnapshotPayload, TransportAck

logger = logging.getLogger(__name__)


class HailoEdgeFilterService:
    """
    Hailo-8 Edge Inference Service

    Runs 24/7 on Node 2 with minimal overhead:
    - Kraken WebSocket V2 streams ticks every 100ms
    - Time-Series-Transformer ONNX inference
    - 26 TOPS Hailo-8 accelerator
    - Anomaly alerts streamed to Cloud Strategist
    """

    def __init__(self):
        """Initialize edge service"""
        self.enabled = os.getenv("HAILO8_ENABLED", "true").lower() == "true"
        self.pairs = os.getenv("CRYPTO_PAIRS", "BTC/USD,ETH/USD").split(",")
        self.marketdata_mode = os.getenv(
            "HAILO8_MARKETDATA_MODE", "websocket").lower()
        self.node_name = os.getenv("NODE_NAME")
        self.grpc_listen_host = os.getenv("HAILO8_GRPC_HOST", "0.0.0.0")
        self.grpc_listen_port = int(os.getenv("HAILO8_GRPC_PORT", "50051"))
        self.strategist_target = os.getenv("CLOUD_STRATEGIST_GRPC_TARGET", "")
        self.strategist_timeout = float(
            os.getenv("CLOUD_STRATEGIST_GRPC_TIMEOUT_SEC", "10"))
        self.metrics_port = int(os.getenv("HAILO8_METRICS_PORT", "9201"))
        self.service_metrics = ServiceMetrics("edge_filter", self.metrics_port)

        # Load ONNX model (cluster-only mode tolerates missing model and runs pass-through).
        self.model = None
        self.detector = None
        self.inference_enabled = True
        model_path = os.getenv(
            "HAILO8_MODEL_PATH",
            "/models/hailo/timeseries_transformer.onnx"
        )
        threshold = float(os.getenv("HAILO8_ANOMALY_THRESHOLD", "85"))
        window_size = int(os.getenv("HAILO8_WINDOW_SIZE", "100"))
        try:
            self.model = TimeSeriesTransformerONNX(model_path)
            self.detector = AnomalyDetector(
                self.model, threshold=threshold, window_size=window_size
            )
        except Exception as e:
            self.inference_enabled = False
            logger.warning(
                f"Inference disabled (model/runtime unavailable): {e}. "
                "Worker continues in pass-through mode."
            )
        # WebSocket client
        self.ws_client = None
        self.grpc_server = None
        self.strategist_client = None
        if self.strategist_target and grpc_available():
            self.strategist_client = StrategistRpcClient(
                self.strategist_target)
        self.metrics = {
            "ticks_processed": 0,
            "anomalies_detected": 0,
            "alerts_forwarded": 0,
            "inference_time_ms": 0.0,
            "last_update": None,
        }

        logger.info("🚀 HailoEdgeFilterService initialized")

    async def tick_handler(self, tick: Tick):
        """
        Callback for incoming Kraken ticks.

        - Store tick to NVMe buffer
        - Run ONNX inference
        - Emit alert if anomaly detected
        """
        tick_dict = tick.to_dict()
        await self.process_tick_dict(tick_dict, tick.symbol)

    async def process_tick_dict(self, tick_dict: dict, symbol: str) -> Optional[AnomalyAlert]:
        """Run inference for a single tick dictionary."""
        started = time.perf_counter()

        try:
            alert_dict = None
            if self.inference_enabled and self.detector is not None:
                # Run inference & check for anomalies
                alert_dict = self.detector.update(tick_dict)

            self.metrics["ticks_processed"] += 1
            self.metrics["last_update"] = datetime.utcnow().isoformat()
            if self.model is not None:
                self.metrics["inference_time_ms"] = round(
                    self.model.last_inference_seconds * 1000.0, 3
                )
                self.service_metrics.set_value(
                    "last_inference_ms", self.metrics["inference_time_ms"]
                )
            self.service_metrics.observe_event(
                "tick_processed", time.perf_counter() - started
            )
            self.service_metrics.set_value(
                "ticks_processed", self.metrics["ticks_processed"])

            if alert_dict:
                self.metrics["anomalies_detected"] += 1
                self.service_metrics.observe_event("anomaly_detected")
                self.service_metrics.set_value(
                    "anomalies_detected", self.metrics["anomalies_detected"]
                )

                # Create AnomalyAlert for hybrid gate
                anomaly_alert = AnomalyAlert(
                    timestamp=datetime.utcnow(),
                    hailo_score=alert_dict["anomaly_score"],
                    window_size=alert_dict["window_size"],
                    signal_type=alert_dict["signal_type"],
                    confidence=alert_dict["confidence"],
                    market_context={
                        **self.detector.get_buffer_stats(),
                        "inference_latency_ms": alert_dict.get("inference_latency_ms", 0.0),
                        "model_provider": alert_dict.get("model_provider", "unknown"),
                    },
                    coin=symbol,
                )

                # Emit alert (via Redis/RabbitMQ in production)
                await self.emit_alert(anomaly_alert)
                return anomaly_alert
        except Exception as e:
            logger.error(f"Error processing tick: {e}")
            self.service_metrics.observe_event("tick_error")
        return None

    async def process_market_snapshot(self, snapshot: MarketSnapshotPayload) -> TransportAck:
        """Consume a market snapshot sent by the master-node relay."""
        if not snapshot.ticks:
            return TransportAck(accepted=False, message="snapshot did not contain ticks")
        started = time.perf_counter()

        latest_alert = None
        for tick_dict in snapshot.tick_dicts():
            latest_alert = await self.process_tick_dict(tick_dict, snapshot.pair) or latest_alert

        self.service_metrics.observe_event(
            "market_snapshot", time.perf_counter() - started
        )
        self.service_metrics.set_value("snapshot_ticks", len(snapshot.ticks))

        return TransportAck(
            accepted=True,
            message="snapshot processed",
            metadata={
                "pair": snapshot.pair,
                "tick_count": len(snapshot.ticks),
                "alert_emitted": latest_alert is not None,
            },
        )

    async def emit_alert(self, alert: AnomalyAlert):
        """
        Send anomaly alert to Cloud Strategist service.

        In production: via Kubernetes Inter-Pod Communication
        - Option 1: Redis queue
        - Option 2: RabbitMQ broker
        - Option 3: Shared volume + file I/O
        """
        logger.info(f"📤 Emitting alert: {alert}")

        alert_payload = EdgeAlertPayload.from_anomaly_alert(
            alert,
            source_node=self.node_name,
            metadata={"transport": "grpc" if self.strategist_client else "jsonl"},
        )

        if self.strategist_client is not None:
            try:
                decision = await self.strategist_client.process_anomaly_alert(
                    alert_payload,
                    timeout=self.strategist_timeout,
                )
                self.metrics["alerts_forwarded"] += 1
                self.service_metrics.observe_event("alert_forwarded")
                self.service_metrics.set_value(
                    "alerts_forwarded", self.metrics["alerts_forwarded"]
                )
                logger.info("Strategist decision: %s", decision.to_dict())
                return
            except Exception as exc:
                logger.warning("Failed to forward alert via gRPC: %s", exc)
                self.service_metrics.observe_event("alert_forward_failed")

        # TODO: Implement actual alert transmission
        # For now: log & write to JSON file (development)
        alert_file = "/tmp/hailo_alerts.jsonl"
        with open(alert_file, "a") as f:
            f.write(json.dumps(alert_payload.to_dict()) + "\n")

    async def run(self):
        """
        Main service loop: connect to Kraken, process ticks forever.
        """
        logger.info("Starting Hailo Edge Filter Service...")
        self.service_metrics.start_http_server()
        self.service_metrics.set_up(True)
        if self.model is not None:
            self.service_metrics.publish_mapping(
                self.model.get_runtime_stats())

        if self.marketdata_mode == "grpc":
            if not grpc_available():
                raise RuntimeError(
                    "grpc mode requested but grpcio is not installed")
            self.grpc_server = EdgeFilterRpcServer(
                processor=self.process_market_snapshot,
                host=self.grpc_listen_host,
                port=self.grpc_listen_port,
            )
            await self.grpc_server.start()
            logger.info(
                "Edge filter gRPC server listening on %s:%s",
                self.grpc_listen_host,
                self.grpc_listen_port,
            )
            await self.grpc_server.wait_for_termination()
            return

        async with kraken_websocket_session(
            pairs=self.pairs,
            tick_callback=self.tick_handler,
        ) as ws:
            self.ws_client = ws

            try:
                # Listen forever
                await ws.listen_forever()
            except KeyboardInterrupt:
                logger.info("Shutting down...")
            except Exception as e:
                logger.error(f"Service error: {e}")
            finally:
                # Cleanup
                ws.cleanup_old_ticks()
                logger.info("Service stopped")
                self.service_metrics.set_up(False)

    def get_metrics(self):
        """Get service metrics for Prometheus"""
        if self.detector is None:
            return {
                **self.metrics,
                "inference_enabled": False,
                "detector_buffer_stats": {},
                "detector_alerts": 0,
                "marketdata_mode": self.marketdata_mode,
            }
        return {
            **self.metrics,
            "inference_enabled": True,
            "detector_buffer_stats": self.detector.get_buffer_stats(),
            "detector_alerts": len(self.detector.alerts),
            "marketdata_mode": self.marketdata_mode,
        }


async def main():
    """Main entry point"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    service = HailoEdgeFilterService()
    await service.run()


if __name__ == "__main__":
    asyncio.run(main())
