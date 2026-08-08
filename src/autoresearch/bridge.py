"""Bridge validation utilities for AutoResearch outputs."""
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def load_payload(path: str):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        logger.debug("failed to load JSON payload from %s", path)
        return None


def is_fresh(payload: dict, max_age_minutes: int = 180) -> bool:
    ts = payload.get("timestamp_utc")
    if not ts:
        return False
    try:
        t = datetime.fromisoformat(ts)
    except ValueError:
        return False
    now = datetime.now(UTC)
    return (now - t) <= timedelta(minutes=max_age_minutes)


def neutral_payload():
    return {"sentiment": 0.0, "confidence": 0.0, "regime": "sideways", "timestamp_utc": datetime.now(UTC).isoformat(), "meta": {"source": "autoresearch-fallback"}}
