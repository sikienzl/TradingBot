import json
import os
from typing import Any, Dict, Optional

import pandas as pd


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


def normalize_research_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """Maps arbitrary AutoResearch-style JSON to a fixed model feature vector."""
    raw = payload or {}
    regime = str(raw.get("market_regime", "sideways")
                 or "sideways").strip().lower()

    features = {
        "research_sentiment_score": _to_float(raw.get("sentiment_score"), 0.0),
        "research_confidence": _to_float(raw.get("confidence"), 0.0),
        "research_risk_score": _to_float(raw.get("risk_score"), 0.0),
        "research_regime_bull": 1.0 if regime == "bull" else 0.0,
        "research_regime_bear": 1.0 if regime == "bear" else 0.0,
        "research_regime_sideways": 1.0 if regime not in {"bull", "bear"} else 0.0,
    }

    # Clamp bounded values to avoid accidental outliers from malformed JSON.
    features["research_sentiment_score"] = max(
        -1.0, min(1.0, features["research_sentiment_score"]))
    features["research_confidence"] = max(
        0.0, min(1.0, features["research_confidence"]))
    features["research_risk_score"] = max(
        0.0, min(1.0, features["research_risk_score"]))
    return features


def load_latest_research_signal(path: Optional[str]) -> Dict[str, float]:
    """Loads AutoResearch JSON from disk and returns normalized model features."""
    if not path:
        return normalize_research_payload(None)
    if not os.path.exists(path):
        return normalize_research_payload(None)

    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return normalize_research_payload(payload)


def apply_research_features(df: pd.DataFrame, research_features: Dict[str, float]) -> pd.DataFrame:
    out = df.copy()
    for col in RESEARCH_FEATURE_COLUMNS:
        out[col] = _to_float(research_features.get(col), 0.0)
    return out
