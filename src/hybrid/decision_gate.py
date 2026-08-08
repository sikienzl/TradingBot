"""
Orchestration for Hybrid Decision Flow

Hailo-8 Anomaly Score → Hybrid Gate → GPT-5 (if score > threshold) → Trade Decision
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AnomalyAlert:
    """Signal from Hailo-8 edge to cloud strategist"""

    timestamp: datetime
    hailo_score: float              # 0-100 anomaly score
    window_size: int                # ticks in analysis window
    signal_type: str                # "breakout", "reversal", "volatility_spike"
    confidence: float               # 0-1 confidence in detection
    market_context: dict[str, Any]  # Last N ticks, RSI, volume, etc.
    coin: str                       # Trading pair

    def is_critical(self) -> bool:
        """Check if this alert warrants immediate GPT-5 call"""
        threshold = float(os.getenv("HAILO8_ANOMALY_THRESHOLD", "85"))
        return self.hailo_score > threshold and self.confidence > 0.6


class HybridDecisionGate:
    """
    Orchestrates decision flow between Hailo-8 (edge) and GPT-5 (cloud).

    Rules:
    1. Hailo-8 runs continuously, emits anomaly alerts
    2. Only if anomaly score > threshold → trigger GPT-5 call
    3. GPT-5 validates macro scenario & returns GO/VETO
    4. Trading bot executes based on GPT-5 decision
    """

    def __init__(self) -> None:
        self.enabled = os.getenv(
            "HYBRID_GATE_ENABLED", "true").lower() == "true"
        self.use_historical = os.getenv(
            "HYBRID_GATE_USE_HISTORICAL_DATA", "true").lower() == "true"
        self.emit_metrics = os.getenv(
            "HYBRID_GATE_EMIT_METRICS", "true").lower() == "true"
        self.gpt5_calls_today = 0
        self.skip_after_daily_calls = int(
            os.getenv("HYBRID_GATE_SKIP_AFTER_DAILY_CALLS", "50"))

        logger.info(f"HybridDecisionGate initialized: enabled={self.enabled}")

    def process_alert(self, alert: AnomalyAlert) -> dict[str, Any] | None:
        """
        Process an anomaly alert from Hailo-8.

        Args:
            alert: AnomalyAlert object from Hailo-8

        Returns:
            Decision dictionary or None if no action required
        """
        logger.info(f"Processing anomaly alert for {alert.coin}")

        if not self.enabled:
            return {"action": "continue", "reason": "Hybrid gate disabled"}

        if alert.is_critical():
            return {
                "action": "call_gpt5",
                "reason": f"High anomaly score ({alert.hailo_score:.2f})",
                "alert": alert
            }
        return {"action": "continue", "reason": "Below threshold"}

    async def evaluate(self, alert: AnomalyAlert) -> dict[str, Any] | None:
        """
        Evaluate Hailo alert and decide whether to call GPT-5.

        Returns:
            None if alert not critical enough
            Dict with GPT-5 decision if executed
        """

        if not self.enabled:
            return None

        # Check if alert is critical
        if not alert.is_critical():
            logger.debug(f"Alert below threshold: score={alert.hailo_score}")
            return None

        # Check daily call limit
        if self.gpt5_calls_today >= self.skip_after_daily_calls:
            logger.warning(
                f"Daily GPT-5 call limit reached ({self.gpt5_calls_today})")
            return None

        logger.info(
            f"🚨 ANOMALY ALERT: score={alert.hailo_score}, coin={alert.coin}, type={alert.signal_type}")
        logger.info("Triggering GPT-5 Chief Strategist evaluation...")

        # TODO: Call GPT-5 with alert context
        # gpt5_decision = await self.call_gpt5_strategist(alert)
        # self.gpt5_calls_today += 1
        # return gpt5_decision

        # Placeholder
        return {"decision": "PENDING", "reason": "GPT-5 module not yet implemented"}

    def reset_daily_counter(self):
        """Reset call counter at midnight"""
        self.gpt5_calls_today = 0


async def example_workflow():
    """Example: Hailo anomaly → Hybrid gate → GPT-5 decision"""

    gate = HybridDecisionGate()

    # Simulated alert from Hailo-8
    alert = AnomalyAlert(
        timestamp=datetime.now(),
        hailo_score=92.5,
        window_size=100,
        signal_type="breakout",
        confidence=0.85,
        market_context={"current_price": 45230.50, "rsi": 78, "volume": 1250},
        coin="BTC/EUR"
    )

    decision = await gate.evaluate(alert)
    print(f"Hybrid decision: {decision}")


if __name__ == "__main__":
    asyncio.run(example_workflow())
