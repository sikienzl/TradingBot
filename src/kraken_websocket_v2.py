"""
Kraken WebSocket V2 API Integration

24/7 real-time market data streaming for high-frequency edge filtering.
Replaces CCXT polling with event-driven WebSocket ticks.

Features:
- Persistent connection with auto-reconnect
- Tick buffering to SQLite for replay & analysis
- Multi-coin support (configurable pairs)
- Metrics: connection uptime, ticks/sec, buffer health

Ticks combine the latest received complete ticker quote and latest trade for
each symbol, not an atomic market snapshot; either component can become stale
when its channel is quiet. Both are required; incomplete events are discarded.
The trade channel supplies the trade price and size:
ticker volume is a 24-hour total, not a trade size. Tick timestamps are the
later of the two exchange timestamps. Cached data is cleared on reconnect.

The default SQLite buffer under /tmp is ephemeral, including for the relay.
Set HAILO8_LOCAL_TICK_PATH to a persistent mounted directory when replay must
survive restarts; the in-memory relay window itself is not persisted here.
"""

import asyncio
import json
import logging
import math
import os
import shutil
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import websockets
from websockets.client import WebSocketClientProtocol

logger = logging.getLogger(__name__)

_STORAGE_HEALTH_INTERVAL_SECONDS = 60


class TickStorageError(RuntimeError):
    """A tick could not be persisted or read from the SQLite buffer."""


@dataclass
class Tick:
    """Single tick from Kraken WebSocket V2"""

    timestamp: float         # Later exchange timestamp of the quote and trade
    symbol: str              # e.g., "BTC/USD"
    bid: float               # Best bid price
    ask: float               # Best ask price
    mid: float               # (bid + ask) / 2
    bid_size: float          # Bid volume
    ask_size: float          # Ask volume
    last_trade_price: float  # Last trade price
    last_trade_size: float   # Last trade volume

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON/storage"""
        return asdict(self)

    @classmethod
    def from_kraken_message(cls, quote: dict, trade: dict) -> "Tick":
        """Combine validated Kraken v2 ticker and trade data for one symbol."""
        if quote["symbol"] != trade["symbol"]:
            raise ValueError("Ticker and trade symbols do not match")
        bid = _number(quote, "bid", positive=True)
        ask = _number(quote, "ask", positive=True)
        return cls(
            timestamp=max(_timestamp(quote), _timestamp(trade)),
            symbol=quote["symbol"],
            bid=bid,
            ask=ask,
            mid=(bid + ask) / 2,
            bid_size=_number(quote, "bid_qty"),
            ask_size=_number(quote, "ask_qty"),
            last_trade_price=_number(trade, "price", positive=True),
            last_trade_size=_number(trade, "qty", positive=True),
        )


def _number(data: dict, key: str, *, positive: bool = False) -> float:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"Invalid {key}")
    value = float(value)
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"Invalid {key}")
    return value


def _timestamp(data: dict) -> float:
    value = data["timestamp"]
    if not isinstance(value, str):
        raise TypeError("Invalid timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return parsed.timestamp()


class KrakenWebSocketV2:
    """
    Kraken WebSocket V2 API client for streaming ticks.

    Connects to: wss://ws.kraken.com/v2

    Channels:
    - ticker: Real-time bid/ask updates
    - trade: Latest executed trade price and size
    """

    KRAKEN_WS_URL = "wss://ws.kraken.com/v2"
    PING_INTERVAL = 30  # seconds

    def __init__(
        self,
        pairs: list[str],
        buffer_path: str | None = None,
        tick_callback: Callable[[Tick], Awaitable[None]] | None = None,
    ):
        """
        Initialize Kraken WebSocket V2 client.

        Args:
            pairs: List of trading pairs, e.g., ["BTC/USD", "ETH/USD"]
            buffer_path: SQLite directory; use persistent storage for replay
            tick_callback: Async callback function for incoming ticks
        """
        self.pairs = pairs
        self.tick_callback = tick_callback
        self.ws: WebSocketClientProtocol | None = None
        self.connected = False
        self.ticks_received = 0
        self.last_tick_time = None
        self.connection_start_time = None
        self._quotes: dict[str, dict] = {}
        self._trades: dict[str, dict] = {}

        self.retention_days = int(
            os.getenv("HAILO8_LOCAL_TICK_RETENTION_DAYS", "7")
        )
        if self.retention_days <= 0:
            raise ValueError("HAILO8_LOCAL_TICK_RETENTION_DAYS must be positive")
        self.max_db_bytes = int(
            os.getenv("HAILO8_LOCAL_TICK_MAX_DB_BYTES", "0")
        )
        if self.max_db_bytes < 0:
            raise ValueError("HAILO8_LOCAL_TICK_MAX_DB_BYTES cannot be negative")
        self.warn_free_bytes = int(
            os.getenv("HAILO8_LOCAL_TICK_WARN_FREE_BYTES", str(1024 ** 3))
        )
        if self.warn_free_bytes < 0:
            raise ValueError("HAILO8_LOCAL_TICK_WARN_FREE_BYTES cannot be negative")
        self._last_storage_health_check = 0.0
        self._db_limit_warning_emitted = False

        # Storage setup
        self.buffer_path = Path(buffer_path or os.getenv(
            "HAILO8_LOCAL_TICK_PATH", "/tmp/trading_ticks"
        ))
        self.buffer_path.mkdir(parents=True, exist_ok=True)
        self.db_path = self.buffer_path / "kraken_ticks.db"
        self._init_db()
        self.reconnect_timeout = int(
            os.getenv("KRAKEN_WS_RECONNECT_TIMEOUT", "30")
        )
        self._check_storage_health(force=True)

        logger.info(
            f"KrakenWebSocketV2 initialized: pairs={pairs}, buffer={self.buffer_path}"
        )

    @contextmanager
    def _database(self):
        """Open a bounded SQLite connection and always close it."""
        conn = sqlite3.connect(self.db_path)
        try:
            if self.max_db_bytes:
                page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                max_pages = self.max_db_bytes // page_size
                if max_pages < 1:
                    logger.error(
                        "SQLite DB quota for %s is too small: %d bytes "
                        "(minimum page size: %d bytes)",
                        self.db_path,
                        self.max_db_bytes,
                        page_size,
                    )
                    raise TickStorageError(
                        f"SQLite DB quota for {self.db_path} is "
                        f"{self.max_db_bytes} bytes, smaller than one "
                        f"{page_size}-byte page"
                    )
                if page_count > max_pages:
                    logger.error(
                        "SQLite database %s already uses %d bytes, above "
                        "configured %d-byte limit",
                        self.db_path,
                        page_count * page_size,
                        self.max_db_bytes,
                    )
                    raise TickStorageError(
                        f"SQLite database {self.db_path} already uses "
                        f"{page_count * page_size} bytes, above its configured "
                        f"{self.max_db_bytes}-byte limit"
                    )
                conn.execute(f"PRAGMA max_page_count = {max_pages}")
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        """Initialize SQLite database for tick storage"""
        try:
            with self._database() as conn:
                with conn:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS ticks (
                            id INTEGER PRIMARY KEY,
                            timestamp REAL NOT NULL,
                            symbol TEXT NOT NULL,
                            bid REAL NOT NULL,
                            ask REAL NOT NULL,
                            mid REAL NOT NULL,
                            bid_size REAL NOT NULL,
                            ask_size REAL NOT NULL,
                            last_trade_price REAL NOT NULL,
                            last_trade_size REAL NOT NULL,
                            received_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_timestamp ON ticks(timestamp)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_symbol ON ticks(symbol)"
                    )
                if self.max_db_bytes:
                    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                    max_pages = self.max_db_bytes // page_size
                    if page_count > max_pages:
                        raise TickStorageError(
                            f"SQLite database {self.db_path} needs at least "
                            f"{page_count * page_size} bytes, above its "
                            f"configured {self.max_db_bytes}-byte limit"
                        )
                    conn.execute(f"PRAGMA max_page_count = {max_pages}")
        except sqlite3.Error as exc:
            logger.exception("Failed to initialize SQLite tick store at %s", self.db_path)
            raise TickStorageError(
                f"Failed to initialize SQLite tick store at {self.db_path}: {exc}"
            ) from exc
        logger.info(f"Database initialized: {self.db_path}")

    async def connect(self) -> bool:
        """
        Connect to Kraken WebSocket V2 API.

        Returns:
            True if successful
        """
        try:
            self._quotes.clear()
            self._trades.clear()
            if self.ws:
                await self.ws.close()
                self.ws = None
            logger.info(f"Connecting to {self.KRAKEN_WS_URL}")
            self.ws = await websockets.connect(
                self.KRAKEN_WS_URL,
                ping_interval=self.PING_INTERVAL,
            )
            self.connected = True
            self.connection_start_time = datetime.now(UTC)

            # Subscribe to both channels for each pair.
            for pair in self.pairs:
                await self._subscribe_ticker(pair)
                await self._subscribe_trade(pair)

            logger.info("✅ Connected to Kraken WebSocket V2")
            return True

        except Exception as e:
            logger.error(f"❌ WebSocket connection failed: {e}")
            self.connected = False
            ws, self.ws = self.ws, None
            if ws:
                try:
                    await ws.close()
                except Exception:
                    logger.exception("Failed to close WebSocket after connect failure")
            return False

    async def _subscribe_ticker(self, pair: str):
        """Subscribe to ticker (bid/ask) updates for a pair"""
        subscription = {
            "method": "subscribe",
            "params": {
                "channel": "ticker",
                "symbol": [pair],
                "event_trigger": "bbo",
            },
        }
        await self.ws.send(json.dumps(subscription))
        logger.info(f"Subscribed to ticker for {pair}")

    async def _subscribe_trade(self, pair: str):
        """Request the latest trades, then live trade updates."""
        subscription = {
            "method": "subscribe",
            "params": {"channel": "trade", "symbol": [pair], "snapshot": True},
        }
        await self.ws.send(json.dumps(subscription))

    async def disconnect(self):
        """Gracefully disconnect from WebSocket"""
        ws, self.ws = self.ws, None
        self.connected = False
        if ws:
            try:
                await ws.close()
            finally:
                logger.info("Disconnected from Kraken WebSocket")

    async def listen_forever(self):
        """
        Main loop: listen for ticks indefinitely with auto-reconnect.

        Incoming messages are:
        1. Stored to the configured SQLite buffer
        2. Passed to callback (if provided)

        A storage error stops this listener; it does not continue with ticks
        that cannot be persisted.
        """
        reconnect_count = 0

        while True:
            if not self.connected:
                logger.info(
                    f"Attempting reconnection ({reconnect_count + 1})...")
                if await self.connect():
                    reconnect_count = 0
                else:
                    await asyncio.sleep(self.reconnect_timeout)
                    reconnect_count += 1
                    continue

            try:
                async for message in self.ws:
                    await self._handle_message(message)
                self.connected = False
                await asyncio.sleep(5)

            except TickStorageError:
                logger.exception("Tick listener stopped because SQLite storage failed")
                raise

            except websockets.exceptions.ConnectionClosed:
                logger.warning(
                    "WebSocket connection closed, will reconnect...")
                self.connected = False
                await asyncio.sleep(5)

            except Exception as e:
                logger.error(f"Error in listen loop: {e}")
                self.connected = False
                await asyncio.sleep(5)

    async def _handle_message(self, raw_msg: str):
        """Process v2 envelopes, discarding incomplete or invalid data entries."""
        try:
            msg = json.loads(raw_msg)
            if not isinstance(msg, dict):
                return
            if msg.get("method") == "subscribe":
                if msg.get("success") is False:
                    logger.error("Kraken subscription failed: %s", msg.get("error"))
                    self.connected = False
                    if self.ws:
                        await self.ws.close()
                return
            channel = msg.get("channel")
            if channel not in ("ticker", "trade") or msg.get("type") not in (
                "snapshot", "update"
            ) or not isinstance(msg.get("data"), list):
                return

            changed = set()
            for entry in msg["data"]:
                try:
                    if not isinstance(entry, dict):
                        raise TypeError("Entry is not an object")
                    symbol = entry["symbol"]
                    if not isinstance(symbol, str) or symbol not in self.pairs:
                        raise ValueError("Unexpected symbol")
                    timestamp = _timestamp(entry)
                    if channel == "ticker":
                        _number(entry, "bid", positive=True)
                        _number(entry, "ask", positive=True)
                        _number(entry, "bid_qty")
                        _number(entry, "ask_qty")
                        previous = self._quotes.get(symbol)
                        if previous and timestamp < _timestamp(previous):
                            continue
                        self._quotes[symbol] = entry
                    else:
                        _number(entry, "price", positive=True)
                        _number(entry, "qty", positive=True)
                        trade_id = entry["trade_id"]
                        if isinstance(trade_id, bool) or not isinstance(trade_id, int):
                            raise ValueError("Invalid trade_id")
                        previous = self._trades.get(symbol)
                        if previous and trade_id <= previous["trade_id"]:
                            continue
                        self._trades[symbol] = entry
                    changed.add(symbol)
                except (KeyError, TypeError, ValueError, OverflowError) as e:
                    logger.warning("Discarding invalid Kraken %s entry: %s", channel, e)
                    continue
                if msg["type"] == "update":
                    await self._emit_tick(symbol)

            # A trade snapshot can contain 50 historical trades: emit only the
            # most recent per symbol rather than replaying them as live ticks.
            if msg["type"] == "snapshot":
                for symbol in sorted(changed):
                    await self._emit_tick(symbol)
        except json.JSONDecodeError as e:
            logger.warning("Invalid Kraken message: %s", e)

    async def _emit_tick(self, symbol: str):
        if symbol not in self._quotes or symbol not in self._trades:
            return
        tick = Tick.from_kraken_message(self._quotes[symbol], self._trades[symbol])
        await self._store_tick(tick)
        if self.tick_callback:
            await self.tick_callback(tick)
        self.ticks_received += 1
        self.last_tick_time = datetime.now(UTC)

    async def _store_tick(self, tick: Tick):
        """Persist a tick or raise instead of silently dropping it."""
        try:
            with self._database() as conn, conn:
                conn.execute(
                    """
                        INSERT INTO ticks (
                            timestamp, symbol, bid, ask, mid,
                            bid_size, ask_size, last_trade_price, last_trade_size
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                    (
                        tick.timestamp,
                        tick.symbol,
                        tick.bid,
                        tick.ask,
                        tick.mid,
                        tick.bid_size,
                        tick.ask_size,
                        tick.last_trade_price,
                        tick.last_trade_size,
                    ),
                )
        except TickStorageError:
            raise
        except sqlite3.Error as exc:
            logger.exception("Failed to persist tick to SQLite at %s", self.db_path)
            raise TickStorageError(
                f"Failed to persist tick to SQLite at {self.db_path}: {exc}"
            ) from exc
        self._check_storage_health()

    def cleanup_old_ticks(self):
        """Explicitly remove ticks older than retention period; never automatic."""
        cutoff_date = datetime.now(UTC) - timedelta(days=self.retention_days)
        cutoff_timestamp = cutoff_date.timestamp()

        try:
            with self._database() as conn, conn:
                conn.execute(
                    "DELETE FROM ticks WHERE timestamp < ?",
                    (cutoff_timestamp,),
                )
                deleted = conn.total_changes
            logger.info(
                f"Cleaned up {deleted} old ticks (>{self.retention_days}d)")
        except TickStorageError:
            raise
        except sqlite3.Error as exc:
            logger.exception("Failed to clean up old ticks in %s", self.db_path)
            raise TickStorageError(
                f"Failed to clean up old ticks in {self.db_path}: {exc}"
            ) from exc
        self._db_limit_warning_emitted = False

    def get_recent_ticks(
        self, symbol: str, limit: int = 100, seconds: int = 300
    ) -> list[Tick]:
        """
        Get recent ticks from database.

        Args:
            symbol: Trading pair (e.g., "BTC/USD")
            limit: Maximum ticks to return
            seconds: Time window in seconds

        Returns:
            List of Tick objects
        """
        cutoff = time.time() - seconds

        try:
            with self._database() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT * FROM ticks
                    WHERE symbol = ? AND timestamp > ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (symbol, cutoff, limit),
                ).fetchall()
        except TickStorageError:
            raise
        except sqlite3.Error as exc:
            logger.exception("Failed to fetch ticks from SQLite at %s", self.db_path)
            raise TickStorageError(
                f"Failed to fetch ticks from SQLite at {self.db_path}: {exc}"
            ) from exc

        return [
            Tick(
                timestamp=row["timestamp"],
                symbol=row["symbol"],
                bid=row["bid"],
                ask=row["ask"],
                mid=row["mid"],
                bid_size=row["bid_size"],
                ask_size=row["ask_size"],
                last_trade_price=row["last_trade_price"],
                last_trade_size=row["last_trade_size"],
            )
            for row in rows
        ]

    def _check_storage_health(self, *, force: bool = False):
        """Warn about retention, database quota, and filesystem free space."""
        now = time.monotonic()
        if not force and now - self._last_storage_health_check < (
            _STORAGE_HEALTH_INTERVAL_SECONDS
        ):
            return
        self._last_storage_health_check = now

        try:
            db_size = self.db_path.stat().st_size
            with self._database() as conn:
                expired = conn.execute(
                    "SELECT COUNT(*) FROM ticks WHERE timestamp < ?",
                    (
                        (datetime.now(UTC) - timedelta(days=self.retention_days))
                        .timestamp(),
                    ),
                ).fetchone()[0]
            if expired:
                logger.warning(
                    "%d ticks are older than the %d-day retention setting; "
                    "retention is manual and no rows were deleted",
                    expired,
                    self.retention_days,
                )
            if self.max_db_bytes and db_size >= self.max_db_bytes * 0.8:
                if not self._db_limit_warning_emitted:
                    logger.warning(
                        "SQLite tick database is at %d/%d bytes (at least 80%% "
                        "of configured limit); new writes may fail when full",
                        db_size,
                        self.max_db_bytes,
                    )
                    self._db_limit_warning_emitted = True
            else:
                self._db_limit_warning_emitted = False
        except OSError:
            logger.exception("Could not inspect tick storage at %s", self.buffer_path)
        except sqlite3.Error:
            logger.exception("Could not inspect SQLite tick storage at %s", self.db_path)

        if self.warn_free_bytes:
            try:
                free_bytes = shutil.disk_usage(self.buffer_path).free
                if free_bytes < self.warn_free_bytes:
                    logger.warning(
                        "Tick storage filesystem has only %d free bytes "
                        "(warning threshold: %d bytes)",
                        free_bytes,
                        self.warn_free_bytes,
                    )
            except OSError:
                logger.exception(
                    "Could not inspect filesystem space for %s", self.buffer_path
                )

    def get_metrics(self) -> dict[str, Any]:
        """Get connection & throughput metrics"""
        uptime = None
        if self.connection_start_time:
            uptime = (datetime.now(UTC) -
                      self.connection_start_time).total_seconds()

        ticks_per_sec = 0
        if self.last_tick_time and uptime:
            ticks_per_sec = self.ticks_received / uptime

        return {
            "connected": self.connected,
            "uptime_seconds": uptime,
            "ticks_received": self.ticks_received,
            "ticks_per_sec": round(ticks_per_sec, 2),
            "last_tick_time": self.last_tick_time.isoformat() if self.last_tick_time else None,
            "buffer_path": str(self.buffer_path),
            "db_size_mb": self.db_path.stat().st_size / (1024 ** 2) if self.db_path.exists() else 0,
        }


@asynccontextmanager
async def kraken_websocket_session(
    pairs: list[str],
    tick_callback: Callable[[Tick], Awaitable[None]] | None = None,
):
    """
    Context manager for Kraken WebSocket session.

    Usage:
        async with kraken_websocket_session(["BTC/USD", "ETH/USD"]) as client:
            await client.listen_forever()
    """
    client = KrakenWebSocketV2(pairs=pairs, tick_callback=tick_callback)

    try:
        if await client.connect():
            yield client
        else:
            raise ConnectionError("Failed to connect to Kraken WebSocket")
    finally:
        await client.disconnect()


async def example_usage():
    """Example: Stream BTC & ETH ticks from Kraken"""

    async def tick_handler(tick: Tick):
        """Callback for incoming ticks"""
        logger.info(
            f"{tick.symbol}: bid={tick.bid:.2f}, ask={tick.ask:.2f}, "
            f"mid={tick.mid:.2f}"
        )

    pairs = ["BTC/USD", "ETH/USD"]

    async with kraken_websocket_session(pairs, tick_callback=tick_handler) as client:
        # Listen for 1 minute, then cleanup
        for i in range(60):
            await asyncio.sleep(1)
            if i % 10 == 0:
                metrics = client.get_metrics()
                logger.info(f"Metrics: {metrics}")

        # Get last 10 ticks for BTC
        recent = client.get_recent_ticks("BTC/USD", limit=10)
        logger.info(f"Last 10 BTC ticks: {recent}")

        # Cleanup old data
        client.cleanup_old_ticks()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    asyncio.run(example_usage())
