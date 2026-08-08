"""Bucketize news records into fixed-minute buckets and aggregate sentiment per bucket."""
from datetime import UTC, datetime
from typing import Any


def _parse_iso(ts: str):
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def bucketize(records: list[dict[str, Any]], bucket_minutes: int = 5) -> dict[str, dict[str, Any]]:
    """Return dict mapping bucket_key -> aggregated features.

    bucket_key is ISO timestamp of bucket start (UTC).
    Aggregated features include: average_sentiment, total_confidence, count, weighted_sentiment.
    """
    buckets = {}
    for r in records:
        ts = r.get("timestamp_utc") or r.get("ts")
        if not ts:
            continue
        dt = _parse_iso(ts)
        if dt is None:
            continue
        # normalize to UTC and bucket start
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        minutes = (dt.hour * 60) + dt.minute
        bucket_start_min = (minutes // bucket_minutes) * bucket_minutes
        hour = bucket_start_min // 60
        minute = bucket_start_min % 60
        bucket_dt = datetime(dt.year, dt.month, dt.day, hour, minute, tzinfo=UTC)
        key = bucket_dt.isoformat()
        s = float(r.get("sentiment", 0.0))
        c = float(r.get("confidence", 1.0))
        if key not in buckets:
            buckets[key] = {"sum_sentiment": 0.0, "sum_conf": 0.0, "count": 0}
        b = buckets[key]
        b["sum_sentiment"] += s * c
        b["sum_conf"] += c
        b["count"] += 1

    # finalize
    out = {}
    for k, v in buckets.items():
        conf = v["sum_conf"] or 1.0
        avg = v["sum_sentiment"] / conf
        out[k] = {"average_sentiment": float(avg), "total_confidence": float(v["sum_conf"]), "count": int(v["count"])}
    return out
