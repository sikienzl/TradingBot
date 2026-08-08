"""
GPT-5 Chief Strategist Service

Cloud-side orchestrator (Node 1).
Listens for anomaly alerts from Hailo-8 edge.
Calls GPT-5 API only when critical anomalies detected (95% cost reduction).
"""

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from src.hybrid.decision_gate import AnomalyAlert, HybridDecisionGate
from src.hybrid.grpc_bridge import StrategistRpcServer, grpc_available
from src.hybrid.monitoring import ServiceMetrics
from src.hybrid.transport import EdgeAlertPayload, StrategistDecisionPayload

logger = logging.getLogger(__name__)


class GPT5StrategistService:
    """
    GPT-5 Chief Strategist

    Runs on Node 1 (Cloud):
    - Listens for anomaly alerts from Hailo-8
    - Validates with macro context (news, market regime, portfolio state)
    - Makes GO/VETO trade decisions
    - Tracks cost & token usage
    """

    def __init__(self):
        """Initialize GPT-5 service"""
        self.enabled = os.getenv("GPT5_ENABLED", "true").lower() == "true"
        self.api_endpoint = os.getenv("GPT5_API_ENDPOINT")
        self.api_key = os.getenv("GPT5_API_KEY")
        self.model = os.getenv("GPT5_MODEL", "gpt-5.4-nano")
        self.max_calls_per_day = int(
            os.getenv("GPT5_MAX_CALLS_PER_DAY", "100"))
        self.max_spend_per_month = float(
            os.getenv("GPT5_MAX_SPEND_PER_MONTH_USD", "500")
        )
        self.shadow_mode = os.getenv(
            "GPT5_SHADOW_MODE", "false").lower() == "true"
        self.grpc_enabled = os.getenv(
            "GPT5_GRPC_ENABLED", "true").lower() == "true"
        self.grpc_host = os.getenv("GPT5_GRPC_HOST", "0.0.0.0")
        self.grpc_port = int(os.getenv("GPT5_GRPC_PORT", "50052"))
        self.metrics_port = int(os.getenv("GPT5_METRICS_PORT", "9202"))
        self.grpc_server = None
        self.service_metrics = ServiceMetrics("strategist", self.metrics_port)

        # Hybrid gate
        self.hybrid_gate = HybridDecisionGate()

        # Metrics
        self.metrics = {
            "calls_today": 0,
            "calls_total": 0,
            "tokens_used": 0,
            "spend_usd": 0.0,
            "decisions_go": 0,
            "decisions_veto": 0,
            "last_decision": None,
        }

        if not self.api_key or self.shadow_mode:
            logger.warning(
                "⚠️  GPT5 SHADOW MODE active — no real API calls will be made. "
                "Set GPT5_API_KEY and GPT5_SHADOW_MODE=false to enable live trading decisions."
            )
        else:
            logger.info("✅ GPT5 LIVE MODE — real API calls enabled (model=%s, endpoint=%s)",
                        self.model, self.api_endpoint)

        logger.info("🧠 GPT5StrategistService initialized")

    async def process_anomaly_alert(self, alert: AnomalyAlert) -> dict[str, Any]:
        """
        Process anomaly alert from Hailo-8.

        1. Validate through hybrid gate
        2. Fetch macro context (optional)
        3. Call GPT-5 API
        4. Track cost
        5. Return decision (GO/VETO)

        Args:
            alert: AnomalyAlert from Hailo-8

        Returns:
            Decision dict: {"decision": "GO"|"VETO", "reason": "...", "risk_level": 0-1}
        """

        started = time.perf_counter()
        # Check hybrid gate
        gate_result = await self.hybrid_gate.evaluate(alert)
        if gate_result is None:
            logger.debug("Alert filtered by hybrid gate")
            self.service_metrics.observe_event(
                "alert_filtered", time.perf_counter() - started)
            return {
                "decision": "FILTERED",
                "reason": "Filtered by hybrid gate (score below threshold)",
                "risk_level": 0.0,
            }

        logger.info(
            f"🚨 Processing anomaly: {alert.coin}, score={alert.hailo_score}")

        # Fetch macro context (market regime, portfolio state, etc.)
        macro_context = await self._get_macro_context(alert)

        # Prepare prompt for GPT-5
        prompt = self._build_gpt5_prompt(alert, macro_context)

        # Call GPT-5 API
        gpt5_response = await self._call_gpt5(prompt)

        # Parse decision
        decision = self._parse_gpt5_response(gpt5_response)

        # Track metrics
        self._update_metrics(gpt5_response)
        self.service_metrics.observe_event(
            "gpt5_decision", time.perf_counter() - started)
        self.service_metrics.publish_mapping(
            {
                "calls_total": self.metrics["calls_total"],
                "calls_today": self.metrics["calls_today"],
                "tokens_used": self.metrics["tokens_used"],
                "spend_usd": self.metrics["spend_usd"],
                "decisions_go": self.metrics["decisions_go"],
                "decisions_veto": self.metrics["decisions_veto"],
            }
        )

        logger.info(
            f"✅ Decision: {decision['decision']} - {decision['reason']}")
        return decision

    async def process_anomaly_payload(
        self,
        payload: EdgeAlertPayload,
    ) -> StrategistDecisionPayload:
        decision = await self.process_anomaly_alert(payload.to_anomaly_alert())
        return StrategistDecisionPayload(
            decision=decision.get("decision", "VETO"),
            reason=decision.get("reason", "Unknown"),
            risk_level=float(decision.get("risk_level", 0.0)),
            confidence=float(decision.get("confidence", 0.0)),
            position_size_pct=float(decision.get("position_size_pct", 0.0)),
            metadata={
                "coin": payload.coin,
                "source_node": payload.source_node,
            },
        )

    async def _get_macro_context(self, alert: AnomalyAlert) -> dict[str, Any]:
        """
        Fetch macro context for anomaly.

        Includes:
        - Current market regime (trend, volatility, regime)
        - Portfolio state (open positions, risk metrics)
        - Recent news/events
        - Technical indicators
        """
        context = {
            "timestamp": datetime.now(UTC).isoformat(),
            "portfolio": {
                # TODO: Fetch from trading bot service
                "open_positions": [],
                "total_pnl": 0.0,
                "drawdown": 0.0,
            },
            "market_regime": "unknown",  # TODO: Fetch from market detection service
            "technical": {
                "rsi": 50,  # TODO: Compute from recent ticks
                "volatility": 0.0,  # TODO: Compute from recent ticks
            },
        }
        return context

    def _build_gpt5_prompt(self, alert: AnomalyAlert, macro_context: dict) -> str:
        """
        Build prompt for GPT-5 Chief Strategist.

        Context includes:
        - Anomaly details from Hailo-8
        - Market regime
        - Portfolio state
        - Risk constraints
        """
        prompt = f"""
        You are an expert trading strategist for cryptocurrency markets.
        
        An anomaly has been detected in {alert.coin} with high confidence:
        - Anomaly Score: {alert.hailo_score:.1f}/100
        - Confidence: {alert.confidence:.1%}
        - Signal Type: {alert.signal_type}
        - Window Size: {alert.window_size} ticks
        
        Market Context:
        - Regime: {macro_context.get('market_regime', 'unknown')}
        - Portfolio RSI: {macro_context['technical'].get('rsi', 'N/A')}
        - Volatility: {macro_context['technical'].get('volatility', 'N/A')}
        
        Portfolio State:
        - Open Positions: {len(macro_context['portfolio']['open_positions'])}
        - Total P&L: ${macro_context['portfolio']['total_pnl']:,.2f}
        - Drawdown: {macro_context['portfolio']['drawdown']:.1%}
        
        Decision Required:
        1. Is this a genuine trading opportunity (GO) or false positive (VETO)?
        2. What is the risk level (0-1)?
        3. Suggested position size (as % of portfolio)?
        
        Respond in JSON format:
        {{
            "decision": "GO" or "VETO",
            "confidence": 0.0-1.0,
            "reasoning": "...",
            "risk_level": 0.0-1.0,
            "position_size_pct": 0-100
        }}
        """
        return prompt

    async def _call_gpt5(self, prompt: str) -> dict[str, Any]:
        """
        Call GPT-5 API.

        In mock mode: returns simulated response.
        In production: actual API call with cost tracking.
        """
        if not self.api_key or self.shadow_mode:
            logger.info("📋 Shadow mode: simulating GPT-5 response (no real API call)")
            self.service_metrics.observe_event("shadow_call")
            return {
                "decision": "GO",
                "confidence": 0.85,
                "reasoning": "[SHADOW] Anomaly appears genuine based on context",
                "risk_level": 0.4,
                "position_size_pct": 1.0,
                "tokens_input": 0,
                "tokens_output": 0,
                "cost_usd": 0.0,
            }

        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 500,
                    "temperature": 0.3,  # Low temperature for consistent decisions
                }

                async with session.post(
                    self.api_endpoint,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.service_metrics.observe_event("api_call_success")
                        return self._extract_gpt5_response(data)
                    else:
                        logger.error(f"GPT-5 API error: {resp.status}")
                        self.service_metrics.observe_event("api_call_error")
                        return {"decision": "ERROR", "reason": f"API error: {resp.status}"}

        except (TimeoutError, aiohttp.ClientError) as e:
            logger.error(f"GPT-5 call failed: {e}")
            self.service_metrics.observe_event("api_call_exception")
            return {"decision": "ERROR", "reason": f"Exception: {e!s}"}

    def _extract_gpt5_response(self, api_response: dict) -> dict:
        """Parse GPT-5 API response"""
        try:
            content = api_response["choices"][0]["message"]["content"]
            json_start = content.find("{")
            json_end = content.rfind("}") + 1

            if json_start >= 0 and json_end > json_start:
                response_json = json.loads(content[json_start:json_end])
                return response_json
            else:
                logger.warning("No JSON found in response")
                return {"decision": "VETO", "reason": "Parsing error"}

        except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError) as e:
            logger.error(f"Response parsing error: {e}")
            return {"decision": "ERROR", "reason": f"Parse error: {e!s}"}

    def _parse_gpt5_response(self, gpt5_reply: dict) -> dict[str, Any]:
        """Convert GPT-5 response to trading decision"""
        return {
            "decision": gpt5_reply.get("decision", "VETO"),
            "reason": gpt5_reply.get("reasoning", "Unknown"),
            "risk_level": float(gpt5_reply.get("risk_level", 0.5)),
            "confidence": float(gpt5_reply.get("confidence", 0.0)),
            "position_size_pct": float(gpt5_reply.get("position_size_pct", 1.0)),
        }

    def _update_metrics(self, gpt5_response: dict):
        """Update metrics from GPT-5 call"""
        self.metrics["calls_today"] += 1
        self.metrics["calls_total"] += 1

        tokens_in = gpt5_response.get("tokens_input", 0)
        tokens_out = gpt5_response.get("tokens_output", 0)
        self.metrics["tokens_used"] += tokens_in + tokens_out

        cost = gpt5_response.get("cost_usd", 0.0)
        self.metrics["spend_usd"] += cost

        decision = gpt5_response.get("decision", "")
        if decision == "GO":
            self.metrics["decisions_go"] += 1
        elif decision == "VETO":
            self.metrics["decisions_veto"] += 1

        self.metrics["last_decision"] = datetime.now(UTC).isoformat()

    def get_metrics(self) -> dict:
        """Get service metrics for Prometheus"""
        return self.metrics


async def _daily_counter_reset_task(service: "GPT5StrategistService") -> None:
    """Reset call counters at midnight UTC every day."""
    while True:
        now = datetime.now(UTC)
        tomorrow_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        wait_seconds = (tomorrow_midnight - now).total_seconds()
        logger.info(
            "Daily counter reset scheduled in %.0f s (at %s UTC)",
            wait_seconds,
            tomorrow_midnight.isoformat(),
        )
        await asyncio.sleep(wait_seconds)
        service.metrics["calls_today"] = 0
        service.hybrid_gate.reset_daily_counter()
        logger.info("Daily GPT-5 call counter reset (new day: %s UTC)", tomorrow_midnight.date())


async def main():
    """Main entry point"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    service = GPT5StrategistService()
    service.service_metrics.start_http_server()
    service.service_metrics.set_up(True)

    if service.grpc_enabled:
        if not grpc_available():
            raise RuntimeError(
                "GPT5_GRPC_ENABLED=true but grpcio is not installed")
        service.grpc_server = StrategistRpcServer(
            processor=service.process_anomaly_payload,
            host=service.grpc_host,
            port=service.grpc_port,
        )
        await service.grpc_server.start()
        logger.info(
            "Strategist gRPC server listening on %s:%s",
            service.grpc_host,
            service.grpc_port,
        )

    # Start daily counter reset background task
    asyncio.create_task(_daily_counter_reset_task(service))

    # Optional startup self-check in shadow mode.
    if os.getenv("GPT5_STARTUP_SELFTEST", "false").lower() == "true":
        alert = AnomalyAlert(
            timestamp=datetime.now(UTC),
            hailo_score=92.5,
            window_size=100,
            signal_type="breakout",
            confidence=0.85,
            market_context={"rsi": 75, "volatility": 0.025},
            coin="BTC/USD",
        )
        decision = await service.process_anomaly_alert(alert)
        logger.info(f"Startup self-test decision: {decision}")

    logger.info("Cloud strategist service is running.")
    while True:
        await asyncio.sleep(30)
        logger.info("Cloud strategist heartbeat")
        service.service_metrics.publish_mapping(
            {
                "calls_total": service.metrics["calls_total"],
                "spend_usd": service.metrics["spend_usd"],
                "shadow_mode": service.shadow_mode,
            }
        )


if __name__ == "__main__":
    asyncio.run(main())
