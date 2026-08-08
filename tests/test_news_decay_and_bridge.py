import time
from datetime import datetime, timezone, timedelta
from src.autoresearch.news.sentiment import apply_time_decay
from src.autoresearch.bridge import is_fresh, neutral_payload


def test_apply_time_decay_recent():
    now = datetime.now(timezone.utc)
    recs = [{"timestamp_utc": now.isoformat(), "title": "good"}]
    out = apply_time_decay(recs, reference_ts=now, half_life_hours=6.0)
    assert len(out) == 1
    assert out[0]["decay_weight"] == 1.0


def test_apply_time_decay_old():
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=12)
    recs = [{"timestamp_utc": old.isoformat(), "title": "old news"}]
    out = apply_time_decay(recs, reference_ts=now, half_life_hours=6.0)
    assert out[0]["decay_weight"] < 0.6


def test_bridge_fresh_and_neutral():
    p = neutral_payload()
    assert not is_fresh(p, max_age_minutes=0)  # neutral just created but max_age 0 => not fresh
    # with reasonable age should be fresh
    assert is_fresh(p, max_age_minutes=60)
