"""Map bucketized news to OHLCV price-series buckets (nearest past index)."""
from typing import Any

try:
    import pandas as pd
except ImportError:
    pd = None
from datetime import UTC, datetime


def align_buckets_to_prices(buckets: dict[str, dict[str, Any]], price_df: pd.DataFrame):
    """Given buckets keyed by ISO ts and a price DataFrame indexed by UTC datetimes,
    return aggregated features per price index (same index as price_df).

    price_df: DataFrame with DatetimeIndex in UTC.
    Returns: DataFrame with columns ['news_avg_sentiment','news_confidence','news_count'] aligned to price_df.index
    """
    # prepare output
    if pd is None:
        raise RuntimeError("pandas is required for align_buckets_to_prices")
    idx = price_df.index
    out = pd.DataFrame(index=idx)
    out["news_avg_sentiment"] = 0.0
    out["news_confidence"] = 0.0
    out["news_count"] = 0

    # parse bucket times
    parsed = []
    import logging
    logger = logging.getLogger(__name__)

    for k, v in buckets.items():
        try:
            dt = datetime.fromisoformat(k)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            else:
                dt = dt.astimezone(UTC)
            parsed.append((pd.Timestamp(dt), v))
        except (ValueError, TypeError) as exc:
            logger.debug("skip unparsable bucket key %s: %s", k, exc)

    # assign each bucket to the nearest price index at or before bucket time
    for ts, v in parsed:
        # find last index <= ts
        sel = idx.asof(ts)
        if pd.isna(sel):
            continue
        out.at[sel, "news_avg_sentiment"] = v.get("average_sentiment", 0.0)
        out.at[sel, "news_confidence"] = v.get("total_confidence", 0.0)
        out.at[sel, "news_count"] = v.get("count", 0)

    return out
