"""Collect public Kraken candles and derive Hailo features via the bot's own analysis path.

Read-only: only public OHLCV endpoints are used. No credentials, orders or balances.
"""

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

DEFAULT_COINS = ("BTC", "ETH", "SOL", "XRP", "ADA", "LINK", "DOGE", "AVAX", "DOT", "LTC")
MAX_KRAKEN_CANDLES = 720
MIN_CANDLES = 200
SAFE_ENVIRONMENT = {
    "SIMULATE_DATA": "true",
    "SIMULATION_INITIAL_BALANCE_FROM_KRAKEN": "false",
    "DRY_RUN": "true",
    "USE_TABULAR_MODEL": "false",
    "USE_ML_MODEL": "false",
    "PERFORMANCE_LOG_ENABLED": "false",
    "KRAKEN_API_KEY": "",
    "KRAKEN_API_SECRET": "",
    "HAILO_API_TOKEN": "",
}


class CapturePredictor:
    """Stands in for the tabular predictor and records the exact feature row."""

    def __init__(self) -> None:
        self.row: dict | None = None

    def predict(self, row, confidence_threshold=None):
        self.row = row.iloc[0].to_dict()
        return {
            "decision": "halten",
            "confidence": 0.0,
            "proba": {"verkaufen": 0.0, "halten": 1.0, "kaufen": 0.0},
        }


def build_capture_bot():
    """Create an offline dry-run bot (default config) wired to a capturing predictor."""
    os.environ.update(SAFE_ENVIRONMENT)
    from src.trading_bot import BotConfig, CryptoTradingBot

    logging.disable(logging.CRITICAL)
    bot = CryptoTradingBot(BotConfig())
    capture = CapturePredictor()
    bot.tabular_predictor = capture
    return bot, capture


def candles_to_frame(candles: list) -> pd.DataFrame:
    frame = pd.DataFrame(
        candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    # The newest candle is still forming and would leak incomplete data.
    return frame.iloc[:-1].reset_index(drop=True)


def collect_rows(bot, capture, exchange, coins, limit: int = MAX_KRAKEN_CANDLES) -> list[dict]:
    config = bot.config
    window_size = config.ohlcv_limit
    rows: list[dict] = []
    for coin in coins:
        candles = exchange.fetch_ohlcv(
            f"{coin}/{config.base_currency}", timeframe=config.ohlcv_timeframe, limit=limit
        )
        if len(candles) < MIN_CANDLES:
            raise RuntimeError(f"Insufficient candles for {coin}: {len(candles)}")
        frame = candles_to_frame(candles)
        for end in range(window_size, len(frame) + 1):
            window = frame.iloc[end - window_size:end]
            bot._fetch_ohlcv_data = lambda *args, data=window, **kwargs: data
            capture.row = None
            bot._analyze_coin(coin, float(window["close"].iloc[-1]))
            if capture.row is None:
                raise RuntimeError(f"No features for {coin} at row {end}")
            rows.append({
                **capture.row,
                "coin": coin,
                "timestamp": window["timestamp"].iloc[-1],
                "close": float(window["close"].iloc[-1]),
            })
        print(f"{coin}: {len(candles)} candles", flush=True)
    return rows


def write_csv(rows: list[dict], output: Path, overwrite: bool) -> None:
    if not rows:
        raise RuntimeError("No training rows collected")
    if output.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite {output.name}; use --overwrite")
    with tempfile.NamedTemporaryFile(
        "w", dir=output.parent, suffix=".tmp", delete=False, encoding="utf-8", newline=""
    ) as handle:
        temporary = Path(handle.name)
    try:
        pd.DataFrame(rows).to_csv(temporary, index=False)
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Output CSV path")
    parser.add_argument("--coins", default=",".join(DEFAULT_COINS), help="Comma-separated base assets")
    parser.add_argument("--limit", type=int, default=MAX_KRAKEN_CANDLES)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    args.coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    if not args.coins:
        parser.error("--coins must not be empty")
    if not MIN_CANDLES <= args.limit <= MAX_KRAKEN_CANDLES:
        parser.error(f"--limit must be between {MIN_CANDLES} and {MAX_KRAKEN_CANDLES}")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    output = args.output.absolute()
    if not output.parent.is_dir():
        raise ValueError(f"Output directory does not exist: {output.parent.name}")
    if output.exists() and not args.overwrite:
        raise FileExistsError(f"Refusing to overwrite {output.name}; use --overwrite")
    import ccxt

    bot, capture = build_capture_bot()
    exchange = ccxt.kraken({"enableRateLimit": True})
    rows = collect_rows(bot, capture, exchange, args.coins, args.limit)
    write_csv(rows, output, args.overwrite)
    print(f"Training rows: {len(rows)}; saved to {output.name}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
