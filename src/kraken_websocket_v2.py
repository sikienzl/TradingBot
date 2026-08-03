"""
Kraken WebSocket V2 API Integration

24/7 real-time market data streaming for high-frequency edge filtering.
Replaces CCXT polling with event-driven WebSocket ticks.

Features:
- Persistent connection with auto-reconnect
- Tick buffering to NVMe SSD for replay & analysis
- Multi-coin support (configurable pairs)
- Metrics: connection uptime, ticks/sec, buffer health
"""

import asyncio
import json
import logging
import os
import sqlite3
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import websockets
from websockets.client import WebSocketClientProtocol

logger = logging.getLogger(__name__)


@dataclass
class Tick:
    """Single tick from Kraken WebSocket V2"""

    timestamp: float          # Unix timestamp (nanoseconds / 1e9)
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
    def from_kraken_message(cls, msg: list, symbol: str) -> "Tick":
        """
        Parse Kraken WebSocket V2 tick message.

        V2 message format (ticker channel):
        [
            <channel_id>,
            {
                "bid": <price>,
                "ask": <price>,
                "last": <price>,
                "volume": <volume>,
                "vwap": <vwap>,
                "low": <low>,
                "high": <high>,
                "change": <change>,
                "change_pct": <change_pct>
            },
            "<channel_name>",
            "<pair>"
        ]
        """
        if not msg or len(msg) < 2:
            raise ValueError(f"Invalid Kraken message format: {msg}")

        data = msg[1]
        timestamp = datetime.utcnow().timestamp()

        return cls(
            timestamp=timestamp,
            symbol=symbol,
            bid=float(data.get("bid", 0)),
            ask=float(data.get("ask", 0)),
            mid=(float(data.get("bid", 0)) + float(data.get("ask", 0))) / 2,
            bid_size=float(data.get("bid_qty", 0)),
            ask_size=float(data.get("ask_qty", 0)),
            last_trade_price=float(data.get("last", 0)),
            last_trade_size=float(data.get("volume", 0)),
        )


class KrakenWebSocketV2:
    """
    Kraken WebSocket V2 API client for streaming ticks.

    Connects to: wss://ws.kraken.com/v2

    Channels:
    - ticker: Real-time bid/ask updates
    - ohlc: 1m OHLC candles
    - spread: Spread updates
    """

    KRAKEN_WS_URL = "wss://ws.kraken.com/v2"
    PING_INTERVAL = 30  # seconds

    def __init__(
        self,
        pairs: list[str],
        buffer_path: str | None = None,
        tick_callback: Callable[[Tick], None] | None = None,
    ):
        """
        Initialize Kraken WebSocket V2 client.

        Args:
            pairs: List of trading pairs, e.g., ["BTC/USD", "ETH/USD"]
            buffer_path: Path to store ticks (SQLite DB)
            tick_callback: Async callback function for incoming ticks
        """
        self.pairs = pairs
        self.tick_callback = tick_callback
        self.ws: WebSocketClientProtocol | None = None
        self.connected = False
        self.ticks_received = 0
        self.last_tick_time = None
        self.connection_start_time = None

        # Storage setup
        self.buffer_path = Path(buffer_path or os.getenv(
            "HAILO8_LOCAL_TICK_PATH", "/tmp/trading_ticks"
        ))
        self.buffer_path.mkdir(parents=True, exist_ok=True)
        self.db_path = self.buffer_path / "kraken_ticks.db"
        self._init_db()

        # Configuration
        self.reconnect_timeout = int(
            os.getenv("KRAKEN_WS_RECONNECT_TIMEOUT", "30")
        )
        self.retention_days = int(
            os.getenv("HAILO8_LOCAL_TICK_RETENTION_DAYS", "7")
        )

        logger.info(
            f"KrakenWebSocketV2 initialized: pairs={pairs}, buffer={self.buffer_path}"
        )

    def _init_db(self):
        """Initialize SQLite database for tick storage"""
        conn = sqlite3.connect(self.db_path)
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
        conn.commit()
        conn.close()
        logger.info(f"Database initialized: {self.db_path}")

    async def connect(self) -> bool:
        """
        Connect to Kraken WebSocket V2 API.

        Returns:
            True if successful
        """
        try:
            logger.info(f"Connecting to {self.KRAKEN_WS_URL}")
            self.ws = await websockets.connect(
                self.KRAKEN_WS_URL,
                ping_interval=self.PING_INTERVAL,
            )
            self.connected = True
            self.connection_start_time = datetime.utcnow()

            # Subscribe to ticker channel for each pair
            for pair in self.pairs:
                await self._subscribe_ticker(pair)

            logger.info("✅ Connected to Kraken WebSocket V2")
            return True

        except Exception as e:
            logger.error(f"❌ WebSocket connection failed: {e}")
            self.connected = False
            return False

    async def _subscribe_ticker(self, pair: str):
        """Subscribe to ticker (bid/ask) updates for a pair"""
        subscription = {
            "method": "subscribe",
            "params": {
                "channel": "ticker",
                "pair": [pair],
            },
        }
        await self.ws.send(json.dumps(subscription))
        logger.info(f"Subscribed to ticker for {pair}")

    async def disconnect(self):
        """Gracefully disconnect from WebSocket"""
        if self.ws:
            await self.ws.close()
            self.connected = False
            logger.info("Disconnected from Kraken WebSocket")

    async def listen_forever(self):
        """
        Main loop: listen for ticks indefinitely with auto-reconnect.

        Incoming messages are:
        1. Stored to SQLite on NVMe
        2. Passed to callback (if provided)
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
        """Process incoming WebSocket message"""
        try:
            msg = json.loads(raw_msg)

            # Ignore subscription confirmations
            if isinstance(msg, dict) and "result" in msg:
                logger.debug(f"Subscription result: {msg}")
                return

            # Handle ticker data
            if isinstance(msg, list) and len(msg) >= 3:
                channel_name = msg[2]
                if channel_name == "ticker":
                    pair = msg[3] if len(msg) > 3 else self.pairs[0]
                    tick = Tick.from_kraken_message(msg, pair)
                    await self._store_tick(tick)

                    # Call callback if provided
                    if self.tick_callback:
                        await self.tick_callback(tick)

                    self.ticks_received += 1
                    self.last_tick_time = datetime.utcnow()

        except Exception as e:
            logger.warning(f"Error handling message: {e}")

    async def _store_tick(self, tick: Tick):
        """Store tick to SQLite database"""
        try:
            conn = sqlite3.connect(self.db_path)
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
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Failed to store tick: {e}")

    def cleanup_old_ticks(self):
        """Remove ticks older than retention period"""
        cutoff_date = datetime.utcnow() - timedelta(days=self.retention_days)
        cutoff_timestamp = cutoff_date.timestamp()

        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "DELETE FROM ticks WHERE timestamp < ?",
                (cutoff_timestamp,),
            )
            deleted = conn.total_changes
            conn.commit()
            conn.close()

            logger.info(
                f"Cleaned up {deleted} old ticks (>{self.retention_days}d)")
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")

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
        cutoff = datetime.utcnow().timestamp() - seconds

        try:
            conn = sqlite3.connect(self.db_path)
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
            conn.close()

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
        except Exception as e:
            logger.error(f"Failed to fetch ticks: {e}")
            return []

    def get_metrics(self) -> dict[str, Any]:
        """Get connection & throughput metrics"""
        uptime = None
        if self.connection_start_time:
            uptime = (datetime.utcnow() -
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
    tick_callback: Callable[[Tick], None] | None = None,
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
