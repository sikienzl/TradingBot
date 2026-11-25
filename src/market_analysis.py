"""Market screening: which coins get a detailed analysis, and the analysis loop.

Layer-1 collaborator: the ``BotConfig`` is injected and the two pieces of
bot-owned state this cluster touches (the dynamic lossmaker exclusion set and
the per-coin analyser) arrive as explicit callables, so the volume gate, the
Top-N selection and the scored analysis map can be unit-tested without
constructing a :class:`CryptoTradingBot`. ``CryptoTradingBot`` keeps a thin
delegator for :func:`analyze_markets` and :func:`dynamic_excluded_coins` and
still owns the journal reader, the exchange and the per-coin indicator/ML
pipeline in :mod:`src.coin_analysis`.
"""
from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable
from typing import Any, cast

import pandas as pd

logger = logging.getLogger(__name__)


def dynamic_excluded_coins(config: Any, *, logger: logging.Logger = logger) -> set[str]:
    """Builds a temporary exclusion set from recent sell-side underperformance."""
    if not config.dynamic_lossmaker_exclusion_enabled:
        return set()

    path = config.performance_log_file
    if not config.performance_log_enabled or not os.path.exists(path):
        return set()

    try:
        df = pd.read_csv(path)
    except (
        OSError,
        UnicodeError,
        pd.errors.ParserError,
        pd.errors.EmptyDataError,
    ) as e:
        logger.warning(
            f"Journal could not be read for dynamic exclusions: {e}")
        return set()

    required_columns = {'action', 'coin', 'pnl_base', 'reason'}
    if df.empty or not required_columns.issubset(df.columns):
        return set()

    sells = df.loc[
        df['action'].fillna(
            '').astype(str).str.lower() == 'sell'
    ].copy()
    recent_window = sells.tail(config.dynamic_lossmaker_window).copy()
    if recent_window.empty:
        return set()

    sells = recent_window.loc[
        recent_window['action'].fillna(
            '').astype(str).str.lower() == 'sell'
    ].copy()
    if sells.empty:
        return set()

    dynamic_exclusions: set[str] = set()
    for coin, group in sells.groupby('coin', dropna=True):
        if len(group) < config.dynamic_lossmaker_min_sells:
            continue

        pnl = pd.to_numeric(group['pnl_base'], errors='coerce').fillna(0.0)
        pnl_sum = float(pnl.sum())
        if pnl_sum > -config.dynamic_lossmaker_min_pnl_loss:
            continue

        win_rate_pct = float((pnl > 0).mean() * 100.0)
        if win_rate_pct > config.dynamic_lossmaker_max_win_rate_pct:
            continue

        reasons = group['reason'].fillna('').astype(str)
        max_hold_exit_ratio = float(
            reasons.str.contains(
                'MAX-HOLD-TIME', case=False, na=False).mean()
        )
        if max_hold_exit_ratio < config.dynamic_lossmaker_min_max_hold_exit_ratio:
            continue

        dynamic_exclusions.add(str(coin).upper())

    return dynamic_exclusions


def select_analysis_coins(
    config: Any,
    market_data: dict[str, dict],
    dynamic_excluded_coins: set[str],
    extra_coins: list[str] | None = None,
    *,
    logger: logging.Logger = logger,
) -> list[str]:
    """Volume gate plus Top-N selection, with ``extra_coins`` always included."""
    filtered_coins_by_volume = []
    max_spread_bps = float(getattr(config, 'max_entry_spread_bps', 0.0))
    for coin, data in market_data.items():
        if coin in config.excluded_coins or coin in dynamic_excluded_coins:
            continue
        if data['volume'] >= config.min_volume_base:
            if max_spread_bps > 0:
                try:
                    spread_bps = float(cast(Any, data.get('spread_bps')))
                except (TypeError, ValueError):
                    logger.info("Skipping %s before inference: spread is unavailable", coin)
                    continue
                if not math.isfinite(spread_bps) or spread_bps > max_spread_bps:
                    logger.info(
                        "Skipping %s before inference: spread %.2f bps exceeds %.2f bps",
                        coin, spread_bps, max_spread_bps)
                    continue
            filtered_coins_by_volume.append(coin)

    logger.info(
        f"Coins filtered by volume ({config.min_volume_base:,.0f} {config.base_currency} minimum): {len(filtered_coins_by_volume)}")

    # Select only the Top-N by volume for detailed analysis to limit API calls
    # or analyse all if TOP_N_FOR_ANALYSIS is very high
    if len(filtered_coins_by_volume) > config.top_n_for_analysis:
        # Sort by volume descending and select the Top-N
        sorted_by_volume = sorted(
            filtered_coins_by_volume, key=lambda c: market_data[c]['volume'], reverse=True)
        coins_for_detailed_analysis = sorted_by_volume[:config.top_n_for_analysis]
        logger.info(
            f"Running detailed analysis for top {config.top_n_for_analysis} coins (by volume).")
    else:
        coins_for_detailed_analysis = filtered_coins_by_volume
        logger.info(
            f"Running detailed analysis for all {len(coins_for_detailed_analysis)} high-volume coins.")

    # Always analyse open positions, even if they are not in the volume top-N.
    if extra_coins:
        for coin in extra_coins:
            if coin in market_data and coin not in coins_for_detailed_analysis:
                coins_for_detailed_analysis.append(coin)

    return coins_for_detailed_analysis


def analyze_markets(
    config: Any,
    dynamic_exclusions: Callable[[], set[str]],
    analyze_coin: Callable[[str, float], dict | None],
    market_data: dict[str, dict],
    extra_coins: list[str] | None = None,
    *,
    logger: logging.Logger = logger,
) -> dict[str, dict]:
    """Performs a comprehensive market analysis and returns recommendations."""
    dynamic_excluded_coins = dynamic_exclusions()
    if dynamic_excluded_coins:
        logger.info(
            "Dynamically excluded lossmaker coins: %s",
            ', '.join(sorted(dynamic_excluded_coins)),
        )
    coins_for_detailed_analysis = select_analysis_coins(
        config, market_data, dynamic_excluded_coins, extra_coins, logger=logger)

    analysis: dict[str, dict] = {}
    total_analysis_coins = len(coins_for_detailed_analysis)
    for idx, coin in enumerate(coins_for_detailed_analysis, start=1):
        logger.info(
            f"🔎 Analysis progress: {idx}/{total_analysis_coins} - {coin}")
        started_at = time.monotonic()
        current_price = market_data[coin]['price']
        coin_analysis = analyze_coin(coin, current_price)
        elapsed = time.monotonic() - started_at
        if elapsed >= config.slow_analysis_warning_seconds:
            logger.warning(
                f"Slow coin analysis for {coin}: {elapsed:.1f}s")
        if coin_analysis:
            analysis[coin] = {
                'coin': coin,
                'price': current_price,
                'volume': market_data[coin]['volume'],
                **{
                    key: market_data[coin][key]
                    for key in ('bid', 'ask', 'spread_bps')
                    if key in market_data[coin]
                },
                **coin_analysis
            }

    # Sort by score/recommendation
    sorted_analysis = dict(
        sorted(analysis.items(), key=lambda x: x[1]['score'], reverse=True))
    return sorted_analysis
