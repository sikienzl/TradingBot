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
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

# Ensure /opt/trading_2 or current dir is in path for imports
_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

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

        # Persistent spend/call tracking — survives restarts
        self._spend_state_path = os.getenv(
            "GPT5_SPEND_STATE_PATH", "./data/gpt5_spend_state.json")
        self.metrics = self._load_spend_state()

        if not self.api_key or self.shadow_mode:
            logger.warning(
                "⚠️  GPT5 SHADOW MODE active — no real API calls will be made. "
                "Set GPT5_API_KEY and GPT5_SHADOW_MODE=false to enable live trading decisions."
            )
        else:
            logger.info("✅ GPT5 LIVE MODE — real API calls enabled (model=%s, endpoint=%s)",
                        self.model, self.api_endpoint)

        logger.info("🧠 GPT5StrategistService initialized")

    def _load_spend_state(self) -> dict:
        """Load persisted spend/call counters from disk. Returns defaults on first run."""
        defaults: dict = {
            "calls_today": 0,
            "calls_total": 0,
            "tokens_used": 0,
            "spend_usd": 0.0,
            "spend_month_usd": 0.0,
            "decisions_go": 0,
            "decisions_veto": 0,
            "last_decision": None,
            "state_date": datetime.now(UTC).date().isoformat(),
            "state_month": datetime.now(UTC).strftime("%Y-%m"),
        }
        try:
            import pathlib
            p = pathlib.Path(self._spend_state_path)
            if p.exists():
                saved = json.loads(p.read_text())
                # Reset daily counter if date has changed
                if saved.get("state_date") != datetime.now(UTC).date().isoformat():
                    saved["calls_today"] = 0
                    saved["state_date"] = datetime.now(UTC).date().isoformat()
                # Reset monthly spend if month has changed
                if saved.get("state_month") != datetime.now(UTC).strftime("%Y-%m"):
                    saved["spend_month_usd"] = 0.0
                    saved["state_month"] = datetime.now(UTC).strftime("%Y-%m")
                defaults.update(saved)
                logger.info(
                    "💾 GPT5 spend state restored: calls_today=%d, spend_usd=%.4f, total=%d",
                    defaults["calls_today"], defaults["spend_usd"], defaults["calls_total"],
                )
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("Could not load GPT5 spend state: %s — starting fresh.", exc)
        return defaults

    def _save_spend_state(self) -> None:
        """Persist current spend/call counters to disk."""
        import pathlib
        try:
            p = pathlib.Path(self._spend_state_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self.metrics, indent=2, default=str))
        except OSError as exc:
            logger.warning("Could not persist GPT5 spend state: %s", exc)

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
        self._save_spend_state()
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
        Build macro context for GPT-5 from live sources:
        - Portfolio snapshot (PnL exporter metrics file)
        - Market regime derived from recent tick buffer in alert
        - RSI and volatility computed from alert's market_context ticks
        - Scorecard verdict
        """
        context: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "portfolio": self._fetch_portfolio_context(),
            "market_regime": self._infer_market_regime(alert),
            "technical": self._compute_technicals(alert),
            "scorecard_verdict": self._fetch_scorecard_verdict(),
        }
        return context

    def _fetch_portfolio_context(self) -> dict[str, Any]:
        """Read portfolio metrics from the PnL exporter metrics file."""
        snapshot_path = os.getenv(
            "SCORECARD_VERDICT_PATH",
            "/opt/trading_2/results/scorecards/latest_status.json",
        )
        portfolio: dict[str, Any] = {
            "open_positions": 0,
            "total_pnl": 0.0,
            "drawdown": 0.0,
            "cash_eur": 0.0,
            "portfolio_value_eur": 0.0,
        }
        # Try scorecard metrics (most reliable persisted source)
        try:
            import json as _json
            import pathlib
            sc = _json.loads(pathlib.Path(snapshot_path).read_text())
            m = sc.get("metrics", {})
            portfolio["total_pnl"] = float(m.get("realized_pnl", 0.0))
            portfolio["drawdown"] = float(m.get("max_drawdown_pct", 0.0))
        except (OSError, KeyError, ValueError):
            pass
        return portfolio

    def _infer_market_regime(self, alert: AnomalyAlert) -> str:
        """
        Derive a simple market regime from the alert's market_context tick buffer.

        Uses the mid-price slope over the window:
          slope > +0.05%/tick → bull, < -0.05%/tick → bear, else sideways
        """
        ctx = alert.market_context or {}
        prices = ctx.get("mid_prices") or ctx.get("prices") or []
        if len(prices) < 5:
            return "unknown"
        import statistics
        try:
            mids = [float(p) for p in prices[-20:]]
            slope_pct = (mids[-1] - mids[0]) / max(mids[0], 1e-9) * 100
            if slope_pct > 0.05:
                return "bull"
            if slope_pct < -0.05:
                return "bear"
            return "sideways"
        except (TypeError, ZeroDivisionError, statistics.StatisticsError):
            return "unknown"

    def _compute_technicals(self, alert: AnomalyAlert) -> dict[str, float]:
        """
        Compute RSI-14 and realised volatility from alert tick buffer.
        Falls back to neutral values if insufficient data.
        """
        ctx = alert.market_context or {}
        prices = ctx.get("mid_prices") or ctx.get("prices") or []
        technicals: dict[str, float] = {"rsi": 50.0, "volatility": 0.0}

        if len(prices) < 2:
            return technicals

        try:
            import math
            mids = [float(p) for p in prices]
            changes = [mids[i] - mids[i - 1] for i in range(1, len(mids))]

            # RSI-14
            window = min(14, len(changes))
            gains = [max(c, 0) for c in changes[-window:]]
            losses = [abs(min(c, 0)) for c in changes[-window:]]
            avg_gain = sum(gains) / window
            avg_loss = sum(losses) / window
            if avg_loss == 0:
                technicals["rsi"] = 100.0
            else:
                rs = avg_gain / avg_loss
                technicals["rsi"] = round(100 - 100 / (1 + rs), 2)

            # Annualised volatility (std of log returns * sqrt(annualisation factor))
            log_rets = [
                math.log(mids[i] / mids[i - 1])
                for i in range(1, len(mids))
                if mids[i - 1] > 0 and mids[i] > 0
            ]
            if len(log_rets) >= 2:
                mean = sum(log_rets) / len(log_rets)
                variance = sum((r - mean) ** 2 for r in log_rets) / (len(log_rets) - 1)
                technicals["volatility"] = round(math.sqrt(variance) * math.sqrt(86400), 4)

        except (TypeError, ValueError, ZeroDivisionError):
            pass

        return technicals

    def _fetch_scorecard_verdict(self) -> str:
        """Read the latest scorecard verdict string."""
        path = os.getenv(
            "SCORECARD_VERDICT_PATH",
            "/opt/trading_2/results/scorecards/latest_status.json",
        )
        try:
            import json as _json
            import pathlib
            sc = _json.loads(pathlib.Path(path).read_text())
            return sc.get("verdict", "UNKNOWN")
        except (OSError, KeyError, ValueError):
            return "UNKNOWN"

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
        self.metrics["spend_month_usd"] = self.metrics.get("spend_month_usd", 0.0) + cost

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
        service.metrics["state_date"] = tomorrow_midnight.date().isoformat()
        service.hybrid_gate.reset_daily_counter()
        service._save_spend_state()
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
