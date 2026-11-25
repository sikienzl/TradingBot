"""Public market-recorder observations behind the paper-trading dashboard.

Layer-1 collaborator: it reads the recorder's ``metadata.json`` /
``tickers.csv`` / ``candles_1m.csv`` files, validates and groups the rows that
fall inside the observation window, and builds the per-symbol quality summary
plus the downsampled chart series the dashboard renders. It knows nothing about
authentication, routes, or the trading bot; ``HTTPException`` is raised only for
a symbol that is not part of the recording, which the caller turns into a 404.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

from fastapi import HTTPException

logger = logging.getLogger(__name__)

MARKET_TICKER_FIELDS = (
    "observed_at_utc",
    "exchange_timestamp_utc",
    "symbol",
    "bid",
    "ask",
    "last",
    "base_volume",
    "quote_volume",
)
MARKET_CANDLE_FIELDS = (
    "timestamp_utc",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
)
MARKET_DATA_READ_LIMIT = 16 * 1024 * 1024
MARKET_DATA_WINDOW = timedelta(hours=24)


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _number(value: object) -> float | None:
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _read_csv_tail(
    path: Path, fields: tuple[str, ...]
) -> tuple[Iterator[dict[str, str]], bool]:
    with path.open("rb") as source:
        size = os.fstat(source.fileno()).st_size
        header = source.readline(4096).decode("utf-8")
        if tuple(next(csv.reader([header]), [])) != fields:
            raise ValueError(f"{path.name} has an unexpected CSV header")
        if size > MARKET_DATA_READ_LIMIT:
            source.seek(-MARKET_DATA_READ_LIMIT, os.SEEK_END)
            content = source.read(MARKET_DATA_READ_LIMIT)
            first_newline = content.find(b"\n")
            if first_newline < 0:
                raise ValueError(f"{path.name} has no complete rows in its read window")
            content = content[first_newline + 1 :]
            truncated = True
        else:
            source.seek(0)
            content = source.read(MARKET_DATA_READ_LIMIT)
            truncated = False
    if content and not content.endswith(b"\n"):
        last_newline = content.rfind(b"\n")
        content = content[: last_newline + 1] if last_newline >= 0 else b""
    text = content.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) == fields:
        return reader, truncated
    if truncated:
        reader = csv.DictReader(io.StringIO(",".join(fields) + "\n" + text, newline=""))
        return reader, True
    raise ValueError(f"{path.name} has an unexpected CSV header")


def _market_unavailable(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "window_hours": int(MARKET_DATA_WINDOW.total_seconds() // 3600),
        "symbols": [],
        "markets": [],
        "selected_symbol": None,
        "ticker_series": [],
        "candle_series": [],
        "read_window_truncated": False,
    }


def load_market_observations(selected_symbol: str | None = None) -> dict[str, Any]:
    market_dir = Path(os.getenv("DASHBOARD_MARKET_DATA_DIR", "/market-data"))
    metadata_path = market_dir / "metadata.json"
    ticker_path = market_dir / "tickers.csv"
    candle_path = market_dir / "candles_1m.csv"
    if not market_dir.is_dir():
        return _market_unavailable("Market recorder data directory is not mounted.")
    if not all(path.is_file() for path in (metadata_path, ticker_path, candle_path)):
        return _market_unavailable("Market recorder data files are not available yet.")
    try:
        if metadata_path.stat().st_size > 1024 * 1024:
            raise ValueError("market recorder metadata exceeds the read limit")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise TypeError("market recorder metadata has an invalid JSON shape")
        symbols = metadata.get("symbols")
        if (
            metadata.get("exchange") != "kraken"
            or not isinstance(symbols, list)
            or not symbols
            or any(
                not isinstance(symbol, str)
                or not re.fullmatch(r"[A-Z0-9]+/EUR", symbol)
                for symbol in symbols
            )
        ):
            raise ValueError(
                "market recorder metadata has an invalid exchange or symbol list"
            )
        symbols = list(dict.fromkeys(symbols))
        if selected_symbol is not None and selected_symbol not in symbols:
            raise HTTPException(status_code=404, detail="Market is not recorded.")
        tickers, ticker_truncated = _read_csv_tail(ticker_path, MARKET_TICKER_FIELDS)
        candles, candle_truncated = _read_csv_tail(candle_path, MARKET_CANDLE_FIELDS)
    except HTTPException:
        raise
    except (OSError, UnicodeError, csv.Error, TypeError, ValueError) as exc:
        logger.error("Unable to read public market observations: %s", exc)
        return _market_unavailable(
            "Market recorder data is invalid or temporarily unavailable."
        )

    now = datetime.now(UTC)
    window_start = now - MARKET_DATA_WINDOW

    try:
        all_tickers_by_symbol, invalid_tickers = _valid_rows(
            tickers, "observed_at_utc", symbols, window_start, now)
        del tickers
        all_candles_by_symbol, invalid_candles = _valid_rows(
            candles, "timestamp_utc", symbols, window_start, now)
        del candles
    except csv.Error as exc:
        logger.error("Unable to parse public market observations: %s", exc)
        return _market_unavailable(
            "Market recorder data is invalid or temporarily unavailable."
        )
    ticker_by_symbol = {
        symbol: [entry for entry in entries if entry[0] >= window_start]
        for symbol, entries in all_tickers_by_symbol.items()
    }
    candle_by_symbol = {
        symbol: [entry for entry in entries if entry[0] >= window_start]
        for symbol, entries in all_candles_by_symbol.items()
    }
    summaries = [
        _build_market_summary(
            symbol, ticker_by_symbol, candle_by_symbol,
            all_tickers_by_symbol, all_candles_by_symbol)
        for symbol in symbols
    ]

    selected = selected_symbol or symbols[0]
    ticker_series = [
        {
            "timestamp": timestamp.isoformat(),
            "bid": _number(row.get("bid")),
            "ask": _number(row.get("ask")),
            "last": _number(row.get("last")),
        }
        for timestamp, row in ticker_by_symbol.get(selected, [])
    ]
    candle_series = [
        {
            "timestamp": timestamp.isoformat(),
            "close": _number(row.get("close")),
        }
        for timestamp, row in candle_by_symbol.get(selected, [])
    ]

    ticker_stat = ticker_path.stat()
    candle_stat = candle_path.stat()
    newest_timestamps = [
        timestamp
        for market in summaries
        for timestamp in (
            _parse_utc(market["latest_observed_at"]),
            _parse_utc(market["latest_candle_at"]),
        )
        if timestamp is not None
    ]
    newest = max(newest_timestamps, default=None)
    return {
        "available": True,
        "reason": None,
        "generated_at": now.isoformat(),
        "window_hours": int(MARKET_DATA_WINDOW.total_seconds() // 3600),
        "symbols": symbols,
        "selected_symbol": selected,
        "markets": summaries,
        "ticker_series": _downsample(ticker_series, 300),
        "candle_series": _downsample(candle_series, 360),
        "latest_data_at": newest.isoformat() if newest else None,
        "latest_data_age_seconds": round((now - newest).total_seconds(), 1)
        if newest
        else None,
        "freshness_status": (
            "unavailable"
            if newest is None
            else "stale"
            if (now - newest).total_seconds() > 60
            else "fresh"
        ),
        "ticker_file_bytes": ticker_stat.st_size,
        "candle_file_bytes": candle_stat.st_size,
        "read_window_truncated": ticker_truncated or candle_truncated,
        "invalid_ticker_rows": invalid_tickers,
        "invalid_candle_rows": invalid_candles,
        "metadata": {
            "created_at_utc": metadata.get("created_at_utc"),
            "ticker_interval_seconds": metadata.get("ticker_interval_seconds"),
            "candle_timeframe": metadata.get("candle_timeframe"),
            "candle_backfill_limit": metadata.get("candle_backfill_limit"),
        },
        "limitations": [
            "Public quotes and candles are observations, not executable fills.",
            "Ticker sampling does not establish intrainterval price paths or order-book depth.",
            "The 24-hour price change uses available first and last ticker observations.",
        ],
    }


def _valid_rows(
    rows: Iterator[dict[str, str]],
    timestamp_field: str,
    symbols: list[str],
    window_start: datetime,
    now: datetime,
) -> tuple[dict[str, list[tuple[datetime, dict[str, str]]]], int]:
    """Group valid ticker/candle rows by symbol within the data window."""
    grouped: dict[str, list[tuple[datetime, dict[str, str]]]] = defaultdict(list)
    older_latest: dict[str, tuple[datetime, dict[str, str]]] = {}
    invalid = 0
    for row in rows:
        symbol = row.get("symbol", "")
        timestamp = _parse_utc(row.get(timestamp_field))
        if symbol not in symbols or timestamp is None:
            invalid += 1
            continue
        numeric_fields = (
            ("bid", "ask", "last")
            if timestamp_field == "observed_at_utc"
            else ("open", "high", "low", "close", "volume")
        )
        parsed_values = [_number(row.get(field)) for field in numeric_fields]
        if any(value is None for value in parsed_values):
            invalid += 1
            continue
        values = cast("list[float]", parsed_values)
        if timestamp_field == "observed_at_utc":
            bid, ask = values[:2]
            if bid <= 0 or ask < bid or values[2] <= 0:
                invalid += 1
                continue
        else:
            open_price, high, low, close, volume = values
            if min(open_price, high, low, close) <= 0 or volume < 0 or high < low:
                invalid += 1
                continue
        if window_start <= timestamp <= now:
            grouped[symbol].append((timestamp, row))
        elif timestamp < window_start and (
            symbol not in older_latest or timestamp > older_latest[symbol][0]
        ):
            older_latest[symbol] = (timestamp, row)
    for symbol, entry in older_latest.items():
        if not grouped[symbol]:
            grouped[symbol].append(entry)
    for entries in grouped.values():
        entries.sort(key=lambda item: item[0])
    return grouped, invalid


def _build_market_summary(
    symbol: str,
    ticker_by_symbol: dict[str, list[tuple[datetime, dict[str, str]]]],
    candle_by_symbol: dict[str, list[tuple[datetime, dict[str, str]]]],
    all_tickers_by_symbol: dict[str, list[tuple[datetime, dict[str, str]]]],
    all_candles_by_symbol: dict[str, list[tuple[datetime, dict[str, str]]]],
) -> dict[str, Any]:
    """Compute the per-symbol market quality summary row."""
    ticker_rows = ticker_by_symbol.get(symbol, [])
    candle_rows = candle_by_symbol.get(symbol, [])
    all_ticker_rows = all_tickers_by_symbol.get(symbol, [])
    all_candle_rows = all_candles_by_symbol.get(symbol, [])
    first_ticker = ticker_rows[0] if ticker_rows else None
    last_ticker = all_ticker_rows[-1] if all_ticker_rows else None
    latest_candle = all_candle_rows[-1] if all_candle_rows else None
    bid = _number(last_ticker[1].get("bid")) if last_ticker else None
    ask = _number(last_ticker[1].get("ask")) if last_ticker else None
    last = _number(last_ticker[1].get("last")) if last_ticker else None
    spread_bps = (
        round((ask - bid) / ((ask + bid) / 2) * 10_000, 2)
        if bid is not None and ask is not None and ask >= bid and ask + bid > 0
        else None
    )
    first_price = _number(first_ticker[1].get("last")) if first_ticker else None
    change_pct = (
        round((last / first_price - 1) * 100, 4)
        if last is not None and first_price is not None and first_price > 0
        else None
    )
    spreads = []
    for _, row in ticker_rows:
        row_bid = _number(row.get("bid"))
        row_ask = _number(row.get("ask"))
        if (
            row_bid is not None
            and row_ask is not None
            and row_ask >= row_bid
            and row_ask + row_bid > 0
        ):
            spreads.append((row_ask - row_bid) / ((row_ask + row_bid) / 2) * 10_000)
    candle_times = [timestamp for timestamp, _ in candle_rows]
    gap_count = 0
    max_gap_minutes = 0
    for earlier, later in pairwise(candle_times):
        difference_minutes = int((later - earlier).total_seconds() // 60)
        if difference_minutes > 1:
            gap_count += difference_minutes - 1
            max_gap_minutes = max(max_gap_minutes, difference_minutes - 1)
    ticker_span_seconds = (
        (ticker_rows[-1][0] - ticker_rows[0][0]).total_seconds()
        if len(ticker_rows) > 1
        else 0
    )
    expected_candles = (
        int((candle_times[-1] - candle_times[0]).total_seconds() // 60) + 1
        if len(candle_times) > 1
        else len(candle_times)
    )
    return (
        {
            "symbol": symbol,
            "latest_observed_at": last_ticker[0].isoformat()
            if last_ticker
            else None,
            "ticker_samples_24h": len(ticker_rows),
            "ticker_samples_per_hour": round(len(ticker_rows) / 24, 1),
            "ticker_span_hours": round(ticker_span_seconds / 3600, 2),
            "latest_bid": bid,
            "latest_ask": ask,
            "latest_last": last,
            "latest_spread_bps": spread_bps,
            "average_spread_bps": round(sum(spreads) / len(spreads), 2)
            if spreads
            else None,
            "price_change_pct_observed": change_pct,
            "first_ticker_at": first_ticker[0].isoformat()
            if first_ticker
            else None,
            "candle_count_24h": len(candle_rows),
            "expected_candles_in_observed_span": expected_candles,
            "candle_coverage_pct": (
                round(len(candle_rows) / expected_candles * 100, 2)
                if expected_candles
                else None
            ),
            "candle_gap_minutes": gap_count,
            "largest_candle_gap_minutes": max_gap_minutes,
            "latest_candle_at": latest_candle[0].isoformat()
            if latest_candle
            else None,
        }
    )


def _downsample(points: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Evenly thin a point series down to at most *limit* points."""
    if len(points) <= limit:
        return points
    stride = (len(points) - 1) / (limit - 1)
    return [points[round(index * stride)] for index in range(limit)]
