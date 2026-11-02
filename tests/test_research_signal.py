import json

from src.research_signal import (
    load_research_signal_payload,
    normalize_research_payload_model,
)


def test_normalize_research_payload_model_clamps_and_sets_sideways_default():
    payload = {
        "sentiment_score": 5,
        "confidence": -1,
        "risk_score": 7,
        "market_regime": "unknown",
    }

    result = normalize_research_payload_model(payload)

    assert result.research_sentiment_score == 1.0
    assert result.research_confidence == 0.0
    assert result.research_risk_score == 1.0
    assert result.research_regime_sideways == 1.0


def test_load_research_signal_payload_returns_typed_payload(tmp_path):
    payload_path = tmp_path / "research_signal.json"
    payload_path.write_text(
        json.dumps(
            {
                "provider": "news",
                "timestamp_utc": "2026-06-03T08:00:00Z",
                "sentiment_score": 0.3,
                "confidence": 0.8,
                "risk_score": 0.2,
                "market_regime": "bull",
                "citations": ["https://example.com/a"],
            }
        ),
        encoding="utf-8",
    )

    result = load_research_signal_payload(str(payload_path))

    assert result.provider == "news"
    assert result.market_regime == "bull"
    assert result.normalized_features.research_regime_bull == 1.0
    assert result.normalized_features.research_sentiment_score == 0.3
