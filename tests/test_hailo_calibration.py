import csv
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

from scripts.hailo_prepare_calibration import build_windows, load_ticks, main
from src.hailo.inference import AnomalyDetector, TimeSeriesTransformerONNX
from src.hailo.preprocessing import PREPROCESSING_VERSION, prepare_tick_window


def tick(symbol: str, index: int) -> dict:
    return {
        "timestamp": float(index), "symbol": symbol,
        "bid": 100 + index, "ask": 102 + index, "mid": 101 + index,
        "bid_size": 1 + index, "ask_size": 2 + index,
        "last_trade_price": 101 + index, "last_trade_size": 3 + index,
    }


def test_worker_and_calibration_use_identical_preprocessing() -> None:
    ticks = [tick("BTC/USD", index) for index in range(5)]
    worker = TimeSeriesTransformerONNX.__new__(TimeSeriesTransformerONNX)
    worker.seq_length = 3
    worker.preprocessing_version = PREPROCESSING_VERSION
    result, counts = build_windows({"BTC/USD": ticks}, 3, 10, 1)
    assert result.shape == (3, 1, 3, 9)
    assert result.dtype == np.float32
    assert counts == {"BTC/USD": 3}
    for index in range(3):
        np.testing.assert_array_equal(result[index], worker.preprocess_ticks(ticks[index:index + 3]))


def test_training_worker_and_calibration_share_windows() -> None:
    pytest.importorskip("torch")
    import pandas as pd

    from src.models.train_timeseries_transformer import TimeSeriesDataset

    rows = [dict(tick("BTC/USD", index), is_anomaly=index % 2) for index in range(5)]
    dataset = TimeSeriesDataset(pd.DataFrame(rows), seq_length=3, preprocessing_version=PREPROCESSING_VERSION)
    worker = TimeSeriesTransformerONNX.__new__(TimeSeriesTransformerONNX)
    worker.seq_length = 3
    worker.preprocessing_version = PREPROCESSING_VERSION
    windows, _ = build_windows({"BTC/USD": rows}, 3, 10, 1)
    for index in range(3):
        np.testing.assert_array_equal(dataset.sequences[index], windows[index, 0])
        np.testing.assert_array_equal(worker.preprocess_ticks(rows[index:index + 3]), windows[index])
        assert dataset.labels[index] == rows[index + 2]["is_anomaly"]


def test_synthetic_training_export_and_worker_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    torch = pytest.importorskip("torch")
    pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    import pandas as pd

    from src.models.train_timeseries_transformer import (
        TimeSeriesTransformerConfig,
        train_transformer,
    )

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    rows = [dict(tick("BTC/USD", index), is_anomaly=index % 2) for index in range(20)]
    source = tmp_path / "synthetic.csv"
    pd.DataFrame(rows).to_csv(source, index=False)
    model_dir = tmp_path / "model"
    config = TimeSeriesTransformerConfig(
        seq_length=2, hidden_dim=8, n_heads=2, n_layers=1, ff_dim=16,
        batch_size=32, epochs=1, preprocessing_version=PREPROCESSING_VERSION,
    )
    train_transformer(str(source), str(model_dir), config)
    worker = TimeSeriesTransformerONNX(str(model_dir / "timeseries_transformer.onnx"), use_hailo=False)
    windows, _ = build_windows({"BTC/USD": rows}, 2, 1, 1)
    assert worker.preprocessing_version == PREPROCESSING_VERSION
    np.testing.assert_array_equal(worker.preprocess_ticks(rows[:2]), windows[0])
    score, confidence = worker.infer(rows[:2])
    assert 0 <= score <= 100
    assert 0 <= confidence <= 1


def test_new_detector_isolates_symbol_windows_and_rejects_reordering() -> None:
    class Model:
        preprocessing_version = PREPROCESSING_VERSION
        seq_length = 2
        last_inference_seconds = 0.0
        last_provider = "CPU"

        def __init__(self) -> None:
            self.windows = []

        def infer(self, ticks: list[dict]) -> tuple[float, float]:
            self.windows.append(list(ticks))
            return 0.0, 0.0

    model = Model()
    detector = AnomalyDetector(model, window_size=3)
    detector.update(tick("BTC/USD", 0))
    detector.update(tick("ETH/USD", 0))
    detector.update(tick("BTC/USD", 1))
    assert len(model.windows) == 1
    assert [row["symbol"] for row in model.windows[0]] == ["BTC/USD"] * 2
    with pytest.raises(ValueError, match="went backwards"):
        detector.update(tick("BTC/USD", 0))
    invalid = tick("BTC/USD", 2)
    invalid["bid"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        detector.update(invalid)
    assert detector.last_timestamps["BTC/USD"] == 1
    assert [row["timestamp"] for row in detector.tick_buffers["BTC/USD"]] == [0, 1]
    detector.update(tick("BTC/USD", 2))
    assert [row["timestamp"] for row in model.windows[-1]] == [0, 1, 2]


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), "not-a-number"])
def test_bad_tick_values_are_rejected(invalid: object) -> None:
    rows = [tick("BTC/USD", 0), tick("BTC/USD", 1)]
    rows[1]["bid"] = invalid
    with pytest.raises(ValueError):
        prepare_tick_window(rows, 2)


def test_insufficient_or_missing_data_is_rejected() -> None:
    with pytest.raises(ValueError, match="fewer than"):
        build_windows({"BTC/USD": [tick("BTC/USD", 0)]}, 2, 10, 1)
    rows = [tick("BTC/USD", 0), tick("BTC/USD", 1)]
    del rows[0]["ask"]
    with pytest.raises(ValueError, match="missing tick feature"):
        prepare_tick_window(rows, 2)


def test_window_rejects_mixed_symbols_and_out_of_order_timestamps() -> None:
    with pytest.raises(ValueError, match="one symbol"):
        prepare_tick_window([tick("BTC/USD", 0), tick("ETH/USD", 1)], 2)
    with pytest.raises(ValueError, match="chronological"):
        prepare_tick_window([tick("BTC/USD", 1), tick("BTC/USD", 0)], 2)


def test_csv_and_sqlite_output_are_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [tick(symbol, index) for symbol in ("BTC/USD", "ETH/USD") for index in range(8)]
    csv_path = tmp_path / "ticks.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(reversed(rows))
    db_path = tmp_path / "ticks.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE ticks (id INTEGER PRIMARY KEY, timestamp REAL, symbol TEXT, "
            "bid REAL, ask REAL, mid REAL, bid_size REAL, ask_size REAL, "
            "last_trade_price REAL, last_trade_size REAL)"
        )
        for row in reversed(rows):
            db.execute(
                "INSERT INTO ticks (timestamp,symbol,bid,ask,mid,bid_size,ask_size,"
                "last_trade_price,last_trade_size) VALUES (?,?,?,?,?,?,?,?,?)",
                tuple(row.values()),
            )
    config = tmp_path / "model_config.json"
    config.write_text(json.dumps({
        "preprocessing_version": PREPROCESSING_VERSION, "seq_length": 3, "n_features": 9,
    }))
    outputs = [tmp_path / "csv.npy", tmp_path / "sqlite.npy"]
    for source, output in zip((csv_path, db_path), outputs):
        monkeypatch.setattr(sys, "argv", [
            "calibrate", "--ticks", str(source), "--model-config", str(config),
            "--symbol", "ETH/USD", "--symbol", "BTC/USD", "--output", str(output),
            "--max-windows-per-symbol", "3", "--stride", "2",
        ])
        assert main() == 0
    np.testing.assert_array_equal(np.load(outputs[0]), np.load(outputs[1]))
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    assert json.loads(Path(str(outputs[0]) + ".json").read_text()) == json.loads(
        Path(str(outputs[1]) + ".json").read_text()
    )
    assert np.load(outputs[0]).shape == (6, 1, 3, 9)
    before = outputs[1].read_bytes()
    with pytest.raises(FileExistsError):
        main()
    assert outputs[1].read_bytes() == before


def test_ohlcv_data_is_not_treated_as_order_book_ticks(tmp_path: Path) -> None:
    source = tmp_path / "ohlcv.csv"
    source.write_text("timestamp,symbol,open,high,low,close,volume\n")
    with pytest.raises(ValueError, match="Tick CSV requires columns"):
        load_ticks(source, ["BTC/USD"])
