"""Orchestrator for running autoresearch experiments."""
import logging
from typing import Any

import pandas as pd

from .news.align import align_buckets_to_prices
from .news.bucketize import bucketize
from .news.collector import collect
from .news.sentiment import aggregate_news_scores
from .strategies import auto_generator, example_strategy

logger = logging.getLogger(__name__)


class AutoResearchRunner:
    def __init__(self, storage):
        self.storage = storage

    def run_experiment(self, strategy_name: str, params: dict[str, Any], market_data):
        # for now support only example_strategy and auto_generator
        if strategy_name == "example":
            strat = example_strategy.create(params)
        elif strategy_name in ("auto", "auto_generator"):
            strat = auto_generator.create(params)
        else:
            raise ValueError("unknown strategy")

        signals = strat.generate_signals(market_data)
        # if news path provided in params, collect and incorporate
        news_path = params.get("news_path")
        if news_path:
            records = collect(news_path)
            # apply decay before aggregating
            try:
                from .news.sentiment import apply_time_decay

                records = apply_time_decay(records)
            except Exception:
                logger.exception("apply_time_decay failed")
            # bucketize and include bucket-level aggregates in signals
            try:
                buckets = bucketize(records, bucket_minutes=int(params.get("bucket_minutes", 5)))
                # attach a simple aggregate: latest bucket average if present
                if buckets:
                    latest_key = max(buckets.keys())
                    latest = buckets[latest_key]
                    news_features = {**aggregate_news_scores(records), "bucket_latest": latest}
                    # if market_data is a pandas DataFrame with datetime index, align buckets
                    try:
                        if isinstance(market_data, pd.DataFrame):
                            price_features = align_buckets_to_prices(buckets, market_data)
                            # attach aggregated latest price-bucket features
                            latest_price_idx = price_features.dropna().index[-1] if not price_features.dropna().empty else None
                            if latest_price_idx is not None:
                                row = price_features.loc[latest_price_idx]
                                news_features["bucket_price_sentiment"] = float(row["news_avg_sentiment"])
                                news_features["bucket_price_confidence"] = float(row["news_confidence"])
                    except Exception:
                        logger.exception("align_buckets_to_prices failed")
                else:
                    news_features = aggregate_news_scores(records)
            except Exception:
                logger.exception("bucketize/aggregate failed")
                news_features = aggregate_news_scores(records)
            # merge into signals
            signals = {**signals, **news_features}
        result = {"strategy": strategy_name, "params": params, "signals": signals}
        self.storage.save_result(result)
        return result
