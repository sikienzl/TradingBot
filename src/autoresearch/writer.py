"""Helpers to produce canonical research signal JSON used by the bridge."""
import json
from datetime import UTC, datetime
from pathlib import Path


def write_prom(prom_path: str, payload: dict) -> None:
    """Write autoresearch signal as Prometheus textfile metrics.

    Produces trading_autoresearch_sentiment, trading_autoresearch_confidence,
    trading_autoresearch_regime and trading_autoresearch_signal_age_seconds.
    """
    sentiment = float(payload.get("sentiment", 0.0))
    confidence = float(payload.get("confidence", 0.0))
    regime = str(payload.get("regime", "sideways"))
    ts_str = payload.get("timestamp_utc", "")
    try:
        t = datetime.fromisoformat(ts_str)
        age_s = (datetime.now(UTC) - t).total_seconds()
    except (ValueError, TypeError):
        age_s = -1.0

    lines = [
        "# HELP trading_autoresearch_sentiment AutoResearch sentiment (-1=sell 0=hold 1=buy)",
        "# TYPE trading_autoresearch_sentiment gauge",
        f"trading_autoresearch_sentiment {sentiment}",
        "# HELP trading_autoresearch_confidence AutoResearch signal confidence (0.0-1.0)",
        "# TYPE trading_autoresearch_confidence gauge",
        f"trading_autoresearch_confidence {confidence}",
        "# HELP trading_autoresearch_regime AutoResearch market regime (labelled gauge, 1 for active regime)",
        "# TYPE trading_autoresearch_regime gauge",
        f'trading_autoresearch_regime{{regime="{regime}"}} 1',
        "# HELP trading_autoresearch_signal_age_seconds Seconds since last AutoResearch signal",
        "# TYPE trading_autoresearch_signal_age_seconds gauge",
        f"trading_autoresearch_signal_age_seconds {age_s:.1f}",
    ]
    p = Path(prom_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")


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
