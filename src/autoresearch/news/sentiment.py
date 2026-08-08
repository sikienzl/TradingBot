"""Simple news sentiment pipeline with a lightweight fallback (VADER-like)."""
import logging
import math
from datetime import UTC
from typing import Any

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    _VADER = SentimentIntensityAnalyzer()
except ImportError:
    _VADER = None

logger = logging.getLogger(__name__)


def score_text_simple(text: str) -> dict[str, Any]:
    # naive polarity: count positive/negative words
    pos = ["good", "buy", "positive", "up", "bull"]
    neg = ["bad", "sell", "negative", "down", "bear"]
    t = text.lower() if text else ""
    p = sum(1 for w in pos if w in t)
    n = sum(1 for w in neg if w in t)
    score = (p - n) / (1 + p + n)
    confidence = min(1.0, (p + n) / 5.0)
    return {"sentiment": float(score), "confidence": float(confidence)}


def score_text(text: str) -> dict[str, Any]:
    """Try VADER first, fallback to simple scorer."""
    if _VADER:
        try:
            vs = _VADER.polarity_scores(text or "")
            # map compound [-1,1] directly
            return {"sentiment": float(vs.get("compound", 0.0)), "confidence": float(abs(vs.get("compound", 0.0)))}
        except Exception:
            logger.exception("VADER scoring failed, falling back")
    return score_text_simple(text)


def aggregate_news_scores(records: list[dict]) -> dict[str, Any]:
    # compute weighted average sentiment
    if not records:
        return {"sentiment": 0.0, "confidence": 0.0}
    scores = [score_text(r.get("title", "") + " " + r.get("body", "")) for r in records]
    s = sum(x["sentiment"] * x["confidence"] for x in scores)
    w = sum(x["confidence"] for x in scores) or 1.0
    avg = s / w
    conf = min(1.0, w / len(records))
    # map to discrete regime heuristic
    regime = "bullish" if avg > 0.2 else "bearish" if avg < -0.2 else "sideways"
    return {"sentiment": float(avg), "confidence": float(conf), "regime": regime}


def apply_time_decay(records: list[dict], reference_ts=None, half_life_hours: float = 6.0) -> list[dict]:
    """Apply exponential time decay to records based on timestamp_utc.

    If `reference_ts` is None, uses now(). half_life_hours controls decay speed.
    Adds field `decay_weight` to each record and returns modified list.
    """
    from datetime import datetime

    if reference_ts is None:
        reference = datetime.now(UTC)
    else:
        reference = reference_ts

    out = []
    for r in records:
        ts = r.get("timestamp_utc")
        try:
            t = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            out.append({**r, "decay_weight": 1.0})
            continue
        delta_hours = (reference - t).total_seconds() / 3600.0
        # exponential decay weight
        weight = math.exp(-math.log(2) * delta_hours / half_life_hours) if delta_hours >= 0 else 1.0
        out.append({**r, "decay_weight": float(weight)})
    return out
