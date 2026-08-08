"""Helpers to produce canonical research signal JSON used by the bridge."""
import json
from datetime import UTC, datetime
from pathlib import Path


def make_canonical(signal: dict) -> dict:
    # normalize into fields expected by bridge/training: sentiment, confidence, regime, timestamp_utc
    sentiment_map = {"buy": 1.0, "sell": -1.0, "hold": 0.0}
    s = signal.get("signal", "hold")
    sentiment = sentiment_map.get(s, 0.0)
    confidence = float(signal.get("confidence", 0.0))
    regime = signal.get("regime", "sideways")
    ts = datetime.now(UTC).isoformat()
    return {
        "sentiment": sentiment,
        "confidence": confidence,
        "regime": regime,
        "timestamp_utc": ts,
        "meta": {"source": "autoresearch"},
    }


def write_canonical(path: str, payload: dict):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2))
