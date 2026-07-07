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
from datetime import datetime, timedelta
from typing import Optional

from src.kraken_websocket_v2 import KrakenWebSocketV2, Tick, kraken_websocket_session
from src.hailo.inference import TimeSeriesTransformerONNX, AnomalyDetector
from src.hybrid.decision_gate import AnomalyAlert

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
        self.metrics = {
            "ticks_processed": 0,
            "anomalies_detected": 0,
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

        try:
            alert_dict = None
            if self.inference_enabled and self.detector is not None:
                # Run inference & check for anomalies
                alert_dict = self.detector.update(tick_dict)

            self.metrics["ticks_processed"] += 1
            self.metrics["last_update"] = datetime.utcnow().isoformat()

            if alert_dict:
                self.metrics["anomalies_detected"] += 1

                # Create AnomalyAlert for hybrid gate
                anomaly_alert = AnomalyAlert(
                    timestamp=datetime.utcnow(),
                    hailo_score=alert_dict["anomaly_score"],
                    window_size=alert_dict["window_size"],
                    signal_type=alert_dict["signal_type"],
                    confidence=alert_dict["confidence"],
                    market_context=self.detector.get_buffer_stats(),
                    coin=tick.symbol,
                )

                # Emit alert (via Redis/RabbitMQ in production)
                await self.emit_alert(anomaly_alert)
        except Exception as e:
            logger.error(f"Error processing tick: {e}")

    async def emit_alert(self, alert: AnomalyAlert):
        """
        Send anomaly alert to Cloud Strategist service.

        In production: via Kubernetes Inter-Pod Communication
        - Option 1: Redis queue
        - Option 2: RabbitMQ broker
        - Option 3: Shared volume + file I/O
        """
        logger.info(f"📤 Emitting alert: {alert}")

        # TODO: Implement actual alert transmission
        # For now: log & write to JSON file (development)
        alert_file = "/tmp/hailo_alerts.jsonl"
        with open(alert_file, "a") as f:
            f.write(json.dumps({
                "timestamp": alert.timestamp.isoformat(),
                "hailo_score": alert.hailo_score,
                "coin": alert.coin,
                "signal_type": alert.signal_type,
                "confidence": alert.confidence,
            }) + "\n")

    async def run(self):
        """
        Main service loop: connect to Kraken, process ticks forever.
        """
        logger.info("Starting Hailo Edge Filter Service...")

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

    def get_metrics(self):
        """Get service metrics for Prometheus"""
        if self.detector is None:
            return {
                **self.metrics,
                "inference_enabled": False,
                "detector_buffer_stats": {},
                "detector_alerts": 0,
            }
        return {
            **self.metrics,
            "inference_enabled": True,
            "detector_buffer_stats": self.detector.get_buffer_stats(),
            "detector_alerts": len(self.detector.alerts),
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
