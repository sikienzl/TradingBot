"""Versioned tick-window preprocessing shared by training and inference."""

from collections.abc import Mapping, Sequence

import numpy as np

PREPROCESSING_VERSION = "tick-window-v1"
TICK_FIELDS = (
    "bid", "ask", "mid", "bid_size", "ask_size",
    "last_trade_price", "last_trade_size",
)
FEATURE_NAMES = (*TICK_FIELDS, "bid_size_ask_size_ratio", "spread")


def prepare_tick_window(ticks: Sequence[Mapping], seq_length: int) -> np.ndarray:
    """Convert one chronological, single-symbol tick window into model input."""
    if seq_length <= 0:
        raise ValueError("seq_length must be positive")
    if len(ticks) < seq_length:
        raise ValueError(f"Expected at least {seq_length} ticks, got {len(ticks)}")

    rows = []
    symbol = None
    last_timestamp = None
    for tick in ticks[-seq_length:]:
        try:
            current_symbol = tick["symbol"]
            timestamp = float(tick["timestamp"])
            values = [float(tick[name]) for name in TICK_FIELDS]
            spread = float(tick.get("spread", values[1] - values[0]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid or missing tick feature: {exc}") from exc
        if not isinstance(current_symbol, str) or not current_symbol.strip():
            raise ValueError("Tick symbol must be a non-empty string")
        if symbol is not None and current_symbol != symbol:
            raise ValueError("A tick window must contain exactly one symbol")
        if not np.isfinite(timestamp):
            raise ValueError("Tick timestamp must be finite")
        if last_timestamp is not None and timestamp < last_timestamp:
            raise ValueError("Tick timestamps must be chronological")
        symbol = current_symbol
        last_timestamp = timestamp
        if not np.isfinite(values).all() or not np.isfinite(spread):
            raise ValueError("Tick features must be finite")
        bid_size, ask_size = values[3:5]
        if bid_size < 0 or ask_size < 0:
            raise ValueError("Tick sizes must not be negative")
        rows.append([*values, bid_size / max(ask_size, 1e-6), spread])

    features = np.asarray(rows, dtype=np.float32)
    if not np.isfinite(features).all():
        raise ValueError("Tick features exceed float32 range")
    normalized = (features - features.mean(axis=0)) / (features.std(axis=0) + 1e-8)
    if not np.isfinite(normalized).all():
        raise ValueError("Tick normalization produced non-finite values")
    return normalized[np.newaxis, :, :]
