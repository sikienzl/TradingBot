"""Pure technical indicators used by the trading bot.

These helpers were extracted from :class:`src.trading_bot.CryptoTradingBot` so
that the analysis maths can be tested - and reused by the backtester and the
dashboard - without constructing a bot. They are deliberately free of any state:
each function takes a series or frame and returns the most recent value.

Every function returns ``numpy.nan`` when the input is too short, which is what
the callers already check for.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_rsi(data: pd.Series, period: int) -> float:
    """Calculates the Relative Strength Index (RSI)."""
    if len(data) < period + 1:
        return np.nan  # Not enough data
    delta = data.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]  # Last RSI value


def calculate_sma(data: pd.Series, period: int) -> float:
    """Calculates the Simple Moving Average (SMA)."""
    if len(data) < period:
        return np.nan
    return data.rolling(window=period).mean().iloc[-1]


def calculate_macd(data: pd.Series, fast: int = 12, slow: int = 26) -> float:
    """Calculates the MACD (EMA12 - EMA26)."""
    if len(data) < slow:
        return np.nan
    ema_fast = data.ewm(span=fast, adjust=False).mean()
    ema_slow = data.ewm(span=slow, adjust=False).mean()
    return (ema_fast - ema_slow).iloc[-1]


def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Calculates the Average True Range (ATR) for the given DataFrame."""
    if len(df) < period + 1:
        return np.nan
    high = df['high']
    low = df['low']
    close = df['close']
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr.iloc[-1]
