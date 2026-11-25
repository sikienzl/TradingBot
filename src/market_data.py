"""Exchange market-data access with retry handling.

Extracted from :class:`src.trading_bot.CryptoTradingBot`. The bot keeps thin
delegators (``_fetch_ohlcv_data`` / ``_get_market_data``) so existing tests and
call sites keep working, while the batching/retry policy can be unit tested
against a stub exchange without building a bot.
"""

import logging
import math
import time
from typing import Any, cast

import ccxt
import pandas as pd

logger = logging.getLogger(__name__)


def fetch_ohlcv(
    exchange,
    symbol: str,
    timeframe: str = '1h',
    limit: int = 100,
    *,
    retries: int = 0,
    delay_seconds: float = 0.0,
    slow_warning_seconds: float = 0.0,
) -> pd.DataFrame | None:
    """Fetches OHLCV candles, retrying transient exchange errors.

    Returns ``None`` when the exchange answers with no data or keeps failing.
    """
    retries = max(0, retries)
    delay_seconds = max(0.0, delay_seconds)

    for attempt in range(retries + 1):
        started_at = time.monotonic()
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            elapsed = time.monotonic() - started_at
            if slow_warning_seconds > 0 and elapsed >= slow_warning_seconds:
                logger.warning(
                    f"Slow OHLCV fetch for {symbol}: {elapsed:.1f}s "
                    f"(timeframe={timeframe}, limit={limit})")
            if not ohlcv:
                logger.warning(f"No OHLCV data available for {symbol}.")
                return None
            df = pd.DataFrame(
                ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            elapsed = time.monotonic() - started_at
            if attempt >= retries:
                logger.error(
                    f"Error fetching OHLCV data for {symbol} after {elapsed:.1f}s: {e}")
                return None
            wait = delay_seconds * (attempt + 1)
            logger.warning(
                f"Retrying OHLCV fetch for {symbol} after {elapsed:.1f}s error: {e} "
                f"(attempt {attempt + 2}/{retries + 1}, wait {wait:.1f}s)")
            if wait > 0:
                time.sleep(wait)
    return None


def fetch_market_data(
    exchange,
    symbols: list[str],
    *,
    batch_size: int = 1,
    retries: int = 0,
    delay_seconds: float = 0.0,
) -> dict[str, dict]:
    """Fetches ticker price/volume for ``symbols`` in batches.

    Keys the result by base coin. A batch that keeps failing is skipped rather
    than aborting the whole sweep.
    """
    market_data: dict[str, dict] = {}
    if exchange is None:
        logger.error(
            "Exchange is not initialised, cannot fetch market data.")
        return {}

    symbols = list(symbols)
    if not symbols:
        return {}

    batch_size = max(1, batch_size)
    retries = max(0, retries)
    delay_seconds = max(0.0, delay_seconds)
    batch_count = (len(symbols) - 1) // batch_size + 1

    for start in range(0, len(symbols), batch_size):
        batch = symbols[start:start + batch_size]
        batch_number = start // batch_size + 1
        tickers = None
        for attempt in range(retries + 1):
            try:
                tickers = exchange.fetch_tickers(batch)
                break
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                if attempt >= retries:
                    logger.error(
                        f"Error fetching ticker data for batch {batch_number}: {e}")
                else:
                    wait = delay_seconds * (attempt + 1)
                    logger.warning(
                        f"Retrying ticker batch {batch_number}/{batch_count} "
                        f"after error: {e} (wait {wait:.1f}s)")
                    if wait > 0:
                        time.sleep(wait)

        if not tickers:
            continue

        for symbol in batch:
            ticker = tickers.get(symbol)
            if ticker and ticker['last'] is not None and ticker['quoteVolume'] is not None:
                coin = symbol.split('/')[0]
                market_data[coin] = {
                    'price': ticker['last'],
                    # Volume in base currency
                    'volume': ticker['quoteVolume'],
                    'bid': ticker.get('bid'),
                    'ask': ticker.get('ask'),
                    'spread_bps': _spread_bps(
                        ticker.get('bid'), ticker.get('ask')),
                }

    return market_data


def _spread_bps(bid: object, ask: object) -> float | None:
    try:
        bid_value = float(cast(Any, bid))
        ask_value = float(cast(Any, ask))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(bid_value) and math.isfinite(ask_value)):
        return None
    if bid_value <= 0 or ask_value < bid_value or ask_value + bid_value <= 0:
        return None
    midpoint = (ask_value + bid_value) / 2.0
    return (ask_value - bid_value) / midpoint * 10_000.0
