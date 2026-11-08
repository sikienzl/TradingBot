"""Create reproducible calibration windows from recorded market ticks."""

import argparse
import csv
import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import numpy as np

from src.hailo.preprocessing import (
    PREPROCESSING_VERSION,
    TICK_FIELDS,
    prepare_tick_window,
)


def load_ticks(path: Path, symbols: list[str]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {symbol: [] for symbol in symbols}
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            required = {"timestamp", "symbol", *TICK_FIELDS}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError(f"Tick CSV requires columns: {', '.join(sorted(required))}")
            for row in reader:
                symbol = row["symbol"]
                if symbol in grouped:
                    grouped[symbol].append(row)
    elif path.suffix.lower() in (".db", ".sqlite", ".sqlite3"):
        uri = path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            for symbol in symbols:
                query = (
                    "SELECT timestamp, symbol, bid, ask, mid, bid_size, ask_size, "
                    "last_trade_price, last_trade_size FROM ticks "
                    "WHERE symbol = ? ORDER BY timestamp, id"
                )
                grouped[symbol] = [dict(row) for row in conn.execute(query, (symbol,))]
    else:
        raise ValueError("Tick source must be a .csv, .db, .sqlite, or .sqlite3 file")

    for symbol, ticks in grouped.items():
        for tick in ticks:
            try:
                tick["timestamp"] = float(tick["timestamp"])
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"Invalid Unix timestamp for {symbol}") from exc
            if not math.isfinite(tick["timestamp"]):
                raise ValueError(f"Invalid Unix timestamp for {symbol}")
        ticks.sort(key=lambda tick: tick["timestamp"])
    return grouped


def build_windows(
    grouped: dict[str, list[dict]], seq_length: int, max_windows: int, stride: int
) -> tuple[np.ndarray, dict[str, int]]:
    if seq_length <= 0 or max_windows <= 0 or stride <= 0:
        raise ValueError("Sequence length, window limit and stride must be positive")
    windows = []
    counts = {}
    for symbol, ticks in grouped.items():
        eligible = range(seq_length, len(ticks) + 1, stride)
        total = len(eligible)
        if total == 0:
            raise ValueError(f"{symbol} has fewer than {seq_length} ticks")
        count = min(max_windows, total)
        indices = [0] if count == 1 else [
            index * (total - 1) // (count - 1) for index in range(count)
        ]
        for index in indices:
            end = eligible[index]
            try:
                window = prepare_tick_window(ticks[end - seq_length:end], seq_length)
            except ValueError as exc:
                raise ValueError(f"Invalid {symbol} window ending at {ticks[end - 1]['timestamp']}: {exc}") from exc
            windows.append(window[0][np.newaxis, :, :])
        counts[symbol] = count
    return np.stack(windows).astype(np.float32, copy=False), counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", required=True, type=Path, help="Tick CSV or Kraken tick SQLite database")
    parser.add_argument("--model-config", required=True, type=Path, help="New model's model_config.json")
    parser.add_argument("--symbol", required=True, action="append", help="Market pair; repeat for each pair")
    parser.add_argument("--output", required=True, type=Path, help="Output .npy; must not exist")
    parser.add_argument("--max-windows-per-symbol", type=int, default=1024)
    parser.add_argument("--stride", type=int, default=1)
    args = parser.parse_args()

    if any(not symbol.strip() or symbol != symbol.strip() for symbol in args.symbol):
        raise ValueError("Symbols must be non-empty and contain no surrounding whitespace")
    if len(set(args.symbol)) != len(args.symbol):
        raise ValueError("Each symbol may be specified only once")
    with args.model_config.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if config.get("preprocessing_version") != PREPROCESSING_VERSION:
        raise ValueError(f"Model configuration must declare {PREPROCESSING_VERSION}")
    if config.get("n_features") != len(TICK_FIELDS) + 2:
        raise ValueError(f"{PREPROCESSING_VERSION} requires exactly 9 model features")
    seq_length = config.get("seq_length")
    if isinstance(seq_length, bool) or not isinstance(seq_length, int) or seq_length <= 0:
        raise ValueError("Model sequence length must be a positive integer")

    output = args.output.absolute()
    metadata = output.with_name(output.name + ".json")
    if output.suffix.lower() != ".npy" or not output.parent.is_dir():
        raise ValueError("Output must be an .npy file in an existing directory")
    if output.exists() or metadata.exists():
        raise FileExistsError("Output or its metadata already exists")

    grouped = load_ticks(args.ticks, sorted(args.symbol))
    data, counts = build_windows(grouped, seq_length, args.max_windows_per_symbol, args.stride)
    with tempfile.TemporaryDirectory(prefix=".hailo-calibration-", dir=output.parent) as temp:
        array_path = Path(temp) / "calibration.npy"
        metadata_path = Path(temp) / "calibration.json"
        np.save(array_path, data, allow_pickle=False)
        digest = hashlib.sha256(array_path.read_bytes()).hexdigest()
        manifest = {
            "preprocessing_version": PREPROCESSING_VERSION,
            "sequence_length": seq_length,
            "n_features": config["n_features"],
            "shape": list(data.shape),
            "dtype": str(data.dtype),
            "symbols": counts,
            "stride": args.stride,
            "max_windows_per_symbol": args.max_windows_per_symbol,
            "selection": "evenly-spaced-eligible-windows-per-symbol",
            "npy_sha256": digest,
        }
        metadata_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.link(metadata_path, metadata)
        try:
            os.link(array_path, output)
        except OSError:
            metadata.unlink()
            raise
    print(f"Calibration: {output} shape={data.shape} sha256={digest}")
    print(f"Metadata: {metadata}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
