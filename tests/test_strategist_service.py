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
