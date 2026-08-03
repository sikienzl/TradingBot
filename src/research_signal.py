import json
import os
from typing import Any

import pandas as pd

from src.api_models import MarketRegime, ResearchSignalFeatures, ResearchSignalPayload

RESEARCH_FEATURE_COLUMNS = [
    "research_sentiment_score",
    "research_confidence",
    "research_risk_score",
    "research_regime_bull",
    "research_regime_bear",
    "research_regime_sideways",
]


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _normalize_market_regime(value: Any) -> MarketRegime:
    raw = str(value or MarketRegime.SIDEWAYS.value).strip().lower()
    if raw == MarketRegime.BULL.value:
        return MarketRegime.BULL
    if raw == MarketRegime.BEAR.value:
        return MarketRegime.BEAR
    return MarketRegime.SIDEWAYS


def normalize_research_payload_model(payload: dict[str, Any] | None) -> ResearchSignalFeatures:
    """Maps arbitrary AutoResearch-style JSON to a typed fixed model feature vector."""
    raw = payload or {}
    regime = _normalize_market_regime(
        raw.get("market_regime", MarketRegime.SIDEWAYS.value))

    return ResearchSignalFeatures(
        research_sentiment_score=_clamp(
            _to_float(raw.get("sentiment_score"), 0.0), -1.0, 1.0),
        research_confidence=_clamp(
            _to_float(raw.get("confidence"), 0.0), 0.0, 1.0),
        research_risk_score=_clamp(
            _to_float(raw.get("risk_score"), 0.0), 0.0, 1.0),
        research_regime_bull=1.0 if regime == MarketRegime.BULL else 0.0,
        research_regime_bear=1.0 if regime == MarketRegime.BEAR else 0.0,
        research_regime_sideways=1.0 if regime == MarketRegime.SIDEWAYS else 0.0,
    )


def normalize_research_payload(payload: dict[str, Any] | None) -> dict[str, float]:
    """Maps arbitrary AutoResearch-style JSON to a fixed model feature vector."""
    return normalize_research_payload_model(payload).model_dump()


def load_latest_research_signal_model(path: str | None) -> ResearchSignalFeatures:
    """Loads AutoResearch JSON from disk and returns typed normalized model features."""
    if not path or not os.path.exists(path):
        return normalize_research_payload_model(None)

    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("Research signal file must contain a JSON object")
    return normalize_research_payload_model(payload)


def load_research_signal_payload(path: str | None) -> ResearchSignalPayload:
    """Loads canonical research signal JSON and returns a validated payload."""
    if not path or not os.path.exists(path):
        features = normalize_research_payload_model(None)
        return ResearchSignalPayload(
            market_regime=MarketRegime.SIDEWAYS,
            normalized_features=features,
        )

    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError("Research signal file must contain a JSON object")

    features = normalize_research_payload_model(payload)
    regime = _normalize_market_regime(payload.get(
        "market_regime", MarketRegime.SIDEWAYS.value))
    return ResearchSignalPayload.model_validate(
        {
            **payload,
            "market_regime": regime.value,
            "sentiment_score": _to_float(payload.get("sentiment_score"), features.research_sentiment_score),
            "confidence": _to_float(payload.get("confidence"), features.research_confidence),
            "risk_score": _to_float(payload.get("risk_score"), features.research_risk_score),
            "citations": [str(item) for item in payload.get("citations", [])],
            "normalized_features": features.model_dump(),
        }
    )


def load_latest_research_signal(path: str | None) -> dict[str, float]:
    """Loads AutoResearch JSON from disk and returns normalized model features."""
    return load_latest_research_signal_model(path).model_dump()


def apply_research_features(df: pd.DataFrame, research_features: dict[str, float]) -> pd.DataFrame:
    out = df.copy()
    for col in RESEARCH_FEATURE_COLUMNS:
        out[col] = _to_float(research_features.get(col), 0.0)
    return out
