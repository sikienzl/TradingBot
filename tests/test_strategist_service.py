import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

from src.cloud.strategist_service import GPT5StrategistService
from src.hailo.edge_filter_service import HailoEdgeFilterService
from src.hybrid.decision_gate import AnomalyAlert, HybridDecisionGate
from src.hybrid.transport import EdgeAlertPayload


class _AsyncContext:
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, *_args: object) -> None:
        return None


def _service() -> GPT5StrategistService:
    service = GPT5StrategistService.__new__(GPT5StrategistService)
    service.enabled = True
    service.shadow_mode = False
    service.api_key = "test-key"
    service._request_lock = asyncio.Lock()
    service.hybrid_gate = HybridDecisionGate()
    service.max_calls_per_day = 100
    service.skip_after_daily_calls = 50
    service.max_spend_per_month = 500.0
    service.metrics = {
        "calls_today": 0,
        "calls_total": 0,
        "tokens_used": 0,
        "spend_usd": 0.0,
        "spend_month_usd": 0.0,
        "decisions_go": 0,
        "decisions_veto": 0,
        "last_decision": None,
    }
    service.service_metrics = Mock()
    service._get_macro_context = AsyncMock(return_value={})
    service._build_gpt5_prompt = Mock(return_value="test prompt")
    service._call_gpt5 = AsyncMock(return_value={
        "decision": "VETO",
        "reasoning": "test response",
        "risk_level": 0.0,
        "confidence": 0.9,
        "position_size_pct": 0.0,
        "tokens_input": 10,
        "tokens_output": 10,
        "cost_usd": 0.01,
    })
    service._save_spend_state = Mock()
    return service


def _alert() -> AnomalyAlert:
    return AnomalyAlert(
        timestamp=datetime.now(UTC),
        hailo_score=92.0,
        window_size=100,
        signal_type="breakout",
        confidence=0.9,
        market_context={},
        coin="BTC/USD",
    )


def test_anomaly_invokes_strategist_once() -> None:
    service = _service()

    asyncio.run(service.process_anomaly_alert(_alert()))

    service._call_gpt5.assert_awaited_once_with("test prompt")
    assert service.metrics["calls_today"] == 1
    assert service.metrics["calls_total"] == 1


def test_daily_budget_blocks_request_before_api_call() -> None:
    service = _service()
    service.metrics["calls_today"] = 50

    result = asyncio.run(service.process_anomaly_alert(_alert()))

    assert result["decision"] == "VETO"
    assert "Daily GPT-5 call limit" in result["reason"]
    service._call_gpt5.assert_not_awaited()


def test_monthly_budget_blocks_request_before_api_call() -> None:
    service = _service()
    service.metrics["spend_month_usd"] = 500.0

    result = asyncio.run(service.process_anomaly_alert(_alert()))

    assert result["decision"] == "VETO"
    assert "Monthly GPT-5 spend limit" in result["reason"]
    service._call_gpt5.assert_not_awaited()


def test_shadow_response_cannot_authorize_trade() -> None:
    service = GPT5StrategistService.__new__(GPT5StrategistService)
    service.api_key = "test-key"
    service.shadow_mode = True
    service.service_metrics = Mock()

    response = asyncio.run(service._call_gpt5("test prompt"))

    assert response["decision"] == "VETO"
    assert response["position_size_pct"] == 0.0


def test_shadow_mode_blocks_even_an_unexpected_go() -> None:
    service = _service()
    service.shadow_mode = True
    service._call_gpt5.return_value = {
        "decision": "GO", "risk_level": 0.1, "confidence": 0.9,
        "position_size_pct": 5,
    }

    decision = asyncio.run(service.process_anomaly_alert(_alert()))

    assert decision["decision"] == "VETO"
    assert decision["position_size_pct"] == 0.0
    service.service_metrics.observe_event.assert_any_call("shadow_decision_blocked")


def test_local_grpc_shadow_tick_to_decision(monkeypatch) -> None:
    from src.hailo.inference import AnomalyDetector
    from src.hailo.preprocessing import PREPROCESSING_VERSION
    from src.hybrid.grpc_bridge import (
        EdgeFilterRpcClient, EdgeFilterRpcServer, StrategistRpcClient,
        StrategistRpcServer, grpc_available,
    )
    from src.hybrid.market_data_relay import MarketDataRelayService
    from src.kraken_websocket_v2 import Tick
    import pytest

    if not grpc_available():
        pytest.skip("grpcio not installed")
    monkeypatch.setenv("HAILO8_WINDOW_SIZE", "2")
    monkeypatch.setenv("HAILO8_RELAY_FLUSH_TICKS", "1")
    monkeypatch.setenv("CRYPTO_PAIRS", "BTC/USD,ETH/USD")
    monkeypatch.setattr(
        "src.hailo.edge_filter_service.TimeSeriesTransformerONNX",
        Mock(side_effect=RuntimeError("no test model")),
    )
    monkeypatch.delenv("CLOUD_STRATEGIST_GRPC_TARGET", raising=False)
    strategist = _service()
    strategist.api_key = "test-key"
    strategist.shadow_mode = True
    strategist._call_gpt5 = GPT5StrategistService._call_gpt5.__get__(strategist)
    edge = HailoEdgeFilterService()
    edge.inference_enabled = True
    edge.model = Mock(preprocessing_version=PREPROCESSING_VERSION, seq_length=2,
                      last_inference_seconds=0.0, last_provider="stub")
    windows = []

    def infer(ticks):
        windows.append([(tick["symbol"], tick["timestamp"]) for tick in ticks])
        return 92, 0.9

    edge.model.infer.side_effect = infer
    edge.detectors = {pair: AnomalyDetector(edge.model, window_size=2)
                      for pair in edge.pairs}

    async def run():
        strategist_server = StrategistRpcServer(strategist.process_anomaly_payload, "127.0.0.1", 0)
        edge_server = EdgeFilterRpcServer(edge.process_market_snapshot, "127.0.0.1", 0)
        try:
            await strategist_server.start()
            await edge_server.start()
            edge.strategist_client = StrategistRpcClient(f"127.0.0.1:{strategist_server.port}")
            relay = MarketDataRelayService()
            relay.client = EdgeFilterRpcClient(f"127.0.0.1:{edge_server.port}")
            for n in range(1, 4):
                await relay.handle_tick(Tick(100 + n, "BTC/USD", 100, 101, 100.5, 2, 1, 100, 1))
                await relay.handle_tick(Tick(n, "ETH/USD", 10, 11, 10.5, 2, 1, 10, 1))
            assert edge.metrics["ticks_processed"] == 6
            assert edge.metrics["alerts_forwarded"] == 2
            assert strategist.metrics["decisions_veto"] == 2
            assert strategist.metrics["decisions_go"] == 0
        finally:
            await edge_server.stop(0)
            await strategist_server.stop(0)

    with patch("src.cloud.strategist_service.aiohttp.ClientSession") as http:
        asyncio.run(run())
    http.assert_not_called()
    assert windows == [
        [("BTC/USD", 101), ("BTC/USD", 102)],
        [("ETH/USD", 1), ("ETH/USD", 2)],
        [("BTC/USD", 102), ("BTC/USD", 103)],
        [("ETH/USD", 2), ("ETH/USD", 3)],
    ]


def test_edge_strategist_shadow_pipeline_never_calls_api_or_executes_order() -> None:
    strategist = _service()
    strategist.api_key = "test-key"
    strategist.shadow_mode = True
    strategist._call_gpt5 = GPT5StrategistService._call_gpt5.__get__(strategist)
    edge = HailoEdgeFilterService.__new__(HailoEdgeFilterService)
    edge.node_name = "test-edge"
    edge.strategist_timeout = 10
    edge.metrics = {"alerts_forwarded": 0}
    edge.service_metrics = Mock()
    edge.strategist_client = Mock()

    async def forward(payload, timeout):
        return await strategist.process_anomaly_payload(payload)

    edge.strategist_client.process_anomaly_alert = AsyncMock(
        side_effect=forward
    )

    with patch("src.cloud.strategist_service.aiohttp.ClientSession") as http:
        asyncio.run(edge.emit_alert(_alert()))

    http.assert_not_called()
    edge.strategist_client.process_anomaly_alert.assert_awaited_once()
    payload = edge.strategist_client.process_anomaly_alert.await_args.args[0]
    assert isinstance(payload, EdgeAlertPayload)
    decision = asyncio.run(strategist.process_anomaly_payload(payload))
    assert decision.decision == "VETO"
    assert decision.position_size_pct == 0.0
    assert edge.metrics["alerts_forwarded"] == 1


def test_live_request_uses_bearer_auth_and_openai_compatible_payload() -> None:
    service = GPT5StrategistService.__new__(GPT5StrategistService)
    service.api_key = "test-api-key"
    service.api_endpoint = "https://provider.test/v1/chat/completions"
    service.model = "test-model"
    service.shadow_mode = False
    service.request_timeout = 12
    service.service_metrics = Mock()

    response = Mock(status=200)
    response.json = AsyncMock(return_value={
        "choices": [{
            "message": {
                "content": '{"decision":"VETO","reasoning":"test"}',
            },
        }],
    })
    session = Mock()
    session.post.return_value = _AsyncContext(response)

    with patch(
        "src.cloud.strategist_service.aiohttp.ClientSession",
        return_value=_AsyncContext(session),
    ):
        result = asyncio.run(service._call_gpt5("test prompt"))

    call = session.post.call_args
    assert call.args[0] == service.api_endpoint
    assert call.kwargs["headers"]["Authorization"] == "Bearer test-api-key"
    assert call.kwargs["headers"]["Accept"] == "application/json"
    assert call.kwargs["json"] == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "test prompt"}],
        "max_tokens": 500,
        "temperature": 0.3,
    }
    assert result["decision"] == "VETO"


def test_provider_error_fails_closed() -> None:
    service = GPT5StrategistService.__new__(GPT5StrategistService)
    service.api_key = "test-api-key"
    service.api_endpoint = "https://provider.test/v1/chat/completions"
    service.model = "test-model"
    service.shadow_mode = False
    service.request_timeout = 12
    service.service_metrics = Mock()

    response = Mock(status=401)
    session = Mock()
    session.post.return_value = _AsyncContext(response)

    with patch(
        "src.cloud.strategist_service.aiohttp.ClientSession",
        return_value=_AsyncContext(session),
    ):
        result = asyncio.run(service._call_gpt5("test prompt"))

    assert result["decision"] == "VETO"
    assert result["reasoning"] == "API error: 401"
    service.service_metrics.observe_event.assert_called_once_with("api_call_error")


def test_invalid_model_decision_fails_closed() -> None:
    service = GPT5StrategistService.__new__(GPT5StrategistService)

    decision = service._parse_gpt5_response({
        "decision": "MAYBE",
        "risk_level": 0.1,
        "confidence": 0.9,
        "position_size_pct": 5,
    })

    assert decision["decision"] == "VETO"
    assert decision["position_size_pct"] == 0.0
