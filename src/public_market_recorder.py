"""Record public Kraken ticker snapshots and completed OHLCV candles only."""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import logging
import math
import os
import signal
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import ccxt

DEFAULT_SYMBOLS = ("ADA/EUR", "ETH/EUR", "LINK/EUR", "NEAR/EUR", "SOL/EUR", "SUI/EUR")
DEFAULT_INTERVAL_SECONDS = 10
DEFAULT_CANDLE_TIMEFRAME = "1m"
DEFAULT_CANDLE_LIMIT = 720
CANDLE_TIMEFRAME_MS = 60_000
TICKER_FIELDS = (
    "observed_at_utc",
    "exchange_timestamp_utc",
    "symbol",
    "bid",
    "ask",
    "last",
    "base_volume",
    "quote_volume",
)
CANDLE_FIELDS = (
    "timestamp_utc",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
)
LOGGER = logging.getLogger("public_market_recorder")


def _utc_iso(timestamp_ms: float | None) -> str:
    if timestamp_ms is None:
        return ""
    try:
        value = float(timestamp_ms)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(value):
        return ""
    return datetime.fromtimestamp(value / 1000, UTC).isoformat()


def _finite_number(value: object, *, positive: bool = False) -> float | None:
    try:
        result = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or (positive and result <= 0):
        return None
    return result


def _append_rows(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        if os.fstat(output.fileno()).st_size == 0:
            writer.writeheader()
        writer.writerows(rows)
        output.flush()
        os.fsync(output.fileno())


def _validate_header(path: Path, fields: tuple[str, ...]) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.reader(source)
        header = next(reader, [])
    if tuple(header) != fields:
        raise ValueError(f"{path.name} has an unexpected CSV header")


class PublicMarketRecorder:
    def __init__(
        self,
        exchange: Any,
        data_dir: Path,
        symbols: tuple[str, ...],
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        candle_timeframe: str = DEFAULT_CANDLE_TIMEFRAME,
        candle_limit: int = DEFAULT_CANDLE_LIMIT,
    ) -> None:
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("symbols must be non-empty and unique")
        if interval_seconds < 5:
            raise ValueError("interval_seconds must be at least 5")
        if candle_timeframe != DEFAULT_CANDLE_TIMEFRAME:
            raise ValueError("candle_timeframe must be 1m")
        if not 200 <= candle_limit <= DEFAULT_CANDLE_LIMIT:
            raise ValueError("candle_limit must be between 200 and 720")

        self.exchange = exchange
        self.data_dir = data_dir
        self.symbols = symbols
        self.interval_seconds = interval_seconds
        self.candle_timeframe = candle_timeframe
        self.candle_limit = candle_limit
        self.ticker_path = data_dir / "tickers.csv"
        self.candle_path = data_dir / "candles_1m.csv"
        self.metadata_path = data_dir / "metadata.json"
        self.lock_file: Any = None
        self.last_candle_ms: dict[str, int] = {}

        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _validate_header(self.ticker_path, TICKER_FIELDS)
        _validate_header(self.candle_path, CANDLE_FIELDS)
        self._acquire_lock()
        try:
            self._load_candle_cursors()
            self._write_or_validate_metadata()
        except Exception:
            self.close()
            raise

    def _acquire_lock(self) -> None:
        self.lock_file = (self.data_dir / ".recorder.lock").open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock_file.close()
            self.lock_file = None
            raise RuntimeError("another public market recorder holds the data lock") from exc

    def _load_candle_cursors(self) -> None:
        if not self.candle_path.exists() or self.candle_path.stat().st_size == 0:
            return
        with self.candle_path.open(encoding="utf-8", newline="") as source:
            for row in csv.DictReader(source):
                symbol = row.get("symbol", "")
                timestamp = row.get("timestamp_utc", "")
                if symbol not in self.symbols:
                    continue
                try:
                    timestamp_ms = int(datetime.fromisoformat(timestamp).timestamp() * 1000)
                except (TypeError, ValueError, OverflowError):
                    raise ValueError("candles_1m.csv contains an invalid timestamp")
                self.last_candle_ms[symbol] = max(
                    timestamp_ms, self.last_candle_ms.get(symbol, 0)
                )

    def _write_or_validate_metadata(self) -> None:
        expected = {
            "exchange": "kraken",
            "symbols": list(self.symbols),
            "ticker_interval_seconds": self.interval_seconds,
            "candle_timeframe": self.candle_timeframe,
            "candle_backfill_limit": self.candle_limit,
            "ticker_fields": list(TICKER_FIELDS),
            "candle_fields": list(CANDLE_FIELDS),
            "credentials_used": False,
            "orders_submitted": False,
        }
        if self.metadata_path.exists():
            saved = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            for key, value in expected.items():
                if saved.get(key) != value:
                    raise ValueError(f"recorder metadata mismatch for {key}")
            return
        metadata = {
            **expected,
            "created_at_utc": datetime.now(UTC).isoformat(),
        }
        descriptor = os.open(
            self.metadata_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(metadata, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())

    def close(self) -> None:
        if self.lock_file is not None:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()
            self.lock_file = None

    def _record_tickers(self, observed_at: datetime) -> int:
        try:
            tickers = self.exchange.fetch_tickers(list(self.symbols))
        except ccxt.BaseError as exc:
            LOGGER.error("Ticker request failed (%s)", type(exc).__name__)
            return 0

        rows = []
        observed_at_text = observed_at.isoformat()
        for symbol in self.symbols:
            ticker = tickers.get(symbol)
            if not isinstance(ticker, dict):
                LOGGER.warning("No ticker returned for %s", symbol)
                continue
            rows.append(
                {
                    "observed_at_utc": observed_at_text,
                    "exchange_timestamp_utc": _utc_iso(ticker.get("timestamp")),
                    "symbol": symbol,
                    "bid": _finite_number(ticker.get("bid"), positive=True),
                    "ask": _finite_number(ticker.get("ask"), positive=True),
                    "last": _finite_number(ticker.get("last"), positive=True),
                    "base_volume": _finite_number(ticker.get("baseVolume")),
                    "quote_volume": _finite_number(ticker.get("quoteVolume")),
                }
            )
        _append_rows(self.ticker_path, TICKER_FIELDS, rows)
        return len(rows)

    def _record_candles(self, now: datetime) -> int:  # noqa: C901
        now_ms = int(now.timestamp() * 1000)
        current_candle_start_ms = (now_ms // CANDLE_TIMEFRAME_MS) * CANDLE_TIMEFRAME_MS
        rows_to_write: list[dict[str, Any]] = []
        next_cursors: dict[str, int] = {}

        for symbol in self.symbols:
            since = self.last_candle_ms.get(symbol)
            if since is not None:
                since += CANDLE_TIMEFRAME_MS
            try:
                candles = self.exchange.fetch_ohlcv(
                    symbol,
                    timeframe=self.candle_timeframe,
                    since=since,
                    limit=self.candle_limit,
                )
            except ccxt.BaseError as exc:
                LOGGER.error("OHLCV request failed for %s (%s)", symbol, type(exc).__name__)
                continue

            last_seen = self.last_candle_ms.get(symbol)
            for candle in candles:
                if len(candle) != 6:
                    LOGGER.warning("Ignoring malformed candle for %s", symbol)
                    continue
                timestamp = _finite_number(candle[0])
                values = [_finite_number(value) for value in candle[1:]]
                if (
                    timestamp is None
                    or any(value is None for value in values)
                    or any(value <= 0 for value in values[:4] if value is not None)
                    or values[4] is None
                    or values[4] < 0
                ):
                    LOGGER.warning("Ignoring invalid candle for %s", symbol)
                    continue
                timestamp_ms = int(timestamp)
                if timestamp_ms >= current_candle_start_ms:
                    continue
                if last_seen is not None and timestamp_ms <= last_seen:
                    continue
                open_price, high, low, close, volume = cast(
                    tuple[float, float, float, float, float], values)
                if high < max(open_price, close, low) or low > min(open_price, close, high):
                    LOGGER.warning("Ignoring inconsistent candle for %s", symbol)
                    continue
                if last_seen is not None and timestamp_ms > last_seen + CANDLE_TIMEFRAME_MS:
                    missing_minutes = (timestamp_ms - last_seen) // CANDLE_TIMEFRAME_MS - 1
                    LOGGER.warning(
                        "OHLCV gap for %s: %d completed minute(s) missing",
                        symbol,
                        missing_minutes,
                    )
                rows_to_write.append(
                    {
                        "timestamp_utc": _utc_iso(timestamp_ms),
                        "symbol": symbol,
                        "open": open_price,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                    }
                )
                last_seen = timestamp_ms
            if last_seen is not None:
                next_cursors[symbol] = last_seen

        _append_rows(self.candle_path, CANDLE_FIELDS, rows_to_write)
        self.last_candle_ms.update(next_cursors)
        return len(rows_to_write)

    def poll_once(self, now: datetime | None = None) -> tuple[int, int]:
        observed_at = now or datetime.now(UTC)
        if observed_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        observed_at = observed_at.astimezone(UTC)
        tickers = self._record_tickers(observed_at)
        candles = self._record_candles(observed_at)
        return tickers, candles

    def run(self, stop_event: threading.Event) -> None:
        LOGGER.info(
            "Recording public Kraken data for %s; ticker_interval=%ss "
            "candle_timeframe=%s; no credentials or orders",
            ",".join(self.symbols),
            self.interval_seconds,
            self.candle_timeframe,
        )
        next_ticker = time.monotonic()
        next_candle = next_ticker
        while not stop_event.is_set():
            cycle_started = time.monotonic()
            if cycle_started >= next_ticker:
                tickers = self._record_tickers(datetime.now(UTC))
                next_ticker = cycle_started + self.interval_seconds
                if tickers:
                    LOGGER.info("Recorded ticker snapshots for %d/%d markets", tickers, len(self.symbols))
            if cycle_started >= next_candle:
                candles = self._record_candles(datetime.now(UTC))
                next_candle = cycle_started + CANDLE_TIMEFRAME_MS / 1000
                if candles:
                    LOGGER.info("Recorded %d completed 1m candles", candles)
            next_run = min(next_ticker, next_candle)
            stop_event.wait(max(0.0, next_run - time.monotonic()))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/market_observations"),
    )
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated active Kraken EUR markets",
    )
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--candle-timeframe", default=DEFAULT_CANDLE_TIMEFRAME)
    parser.add_argument("--candle-limit", type=int, default=DEFAULT_CANDLE_LIMIT)
    args = parser.parse_args(argv)
    args.symbols = tuple(
        symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()
    )
    if not args.symbols:
        parser.error("--symbols must not be empty")
    if any("/" not in symbol or not symbol.endswith("/EUR") for symbol in args.symbols):
        parser.error("all symbols must be EUR-quoted exchange symbols such as ADA/EUR")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stop_event.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: stop_event.set())
    exchange = ccxt.kraken({"enableRateLimit": True, "timeout": 10_000})
    recorder = None
    try:
        markets = exchange.load_markets()
        for symbol in args.symbols:
            market = markets.get(symbol)
            if market is None or market.get("active") is False:
                raise ValueError(f"requested Kraken market is unavailable: {symbol}")
        recorder = PublicMarketRecorder(
            exchange,
            args.data_dir,
            args.symbols,
            interval_seconds=args.interval_seconds,
            candle_timeframe=args.candle_timeframe,
            candle_limit=args.candle_limit,
        )
        recorder.run(stop_event)
    finally:
        if recorder is not None:
            recorder.close()
        exchange.close()
        LOGGER.info("Public market recorder stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
