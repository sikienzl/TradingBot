import json
import sys

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("torch")

from src.hailo import preprocessing
from src.models import train_timeseries_transformer as training


@pytest.fixture
def ticks():
    rows = []
    for symbol, timestamp, value, label in [
        ("A", 1704240000.0, 3, 1),
        ("B", 1704153600.0, 12, 1),
        ("A", 1704067200.0, 1, 0),
        ("B", 1704067200.0, 11, 0),
        ("A", 1704153600.0, 2, 0),
        ("A", 1704153600.0, 4, 1),
    ]:
        rows.append({
            "timestamp": timestamp, "symbol": symbol, "bid": value, "ask": value + 1,
            "mid": value + 0.5, "bid_size": value * 2, "ask_size": value,
            "last_trade_price": value + 0.25, "last_trade_size": value,
            "is_anomaly": label,
        })
    return pd.DataFrame(rows, index=[10, 4, 30, 2, 20, 1])


def test_v1_groups_sorts_stably_and_uses_window_local_preprocessing(monkeypatch, ticks):
    calls = []

    def fake_prepare(window, seq_length):
        calls.append(window)
        return np.full((1, seq_length, 9), window[-1]["bid"], dtype=np.float32)

    monkeypatch.setattr(preprocessing, "prepare_tick_window", fake_prepare)
    dataset = training.TimeSeriesDataset(ticks, seq_length=2, preprocessing_version="tick-window-v1")
    assert [[row["bid"] for row in window] for window in calls] == [
        [1, 2], [2, 4], [4, 3], [11, 12],
    ]
    assert all(len({row["symbol"] for row in window}) == 1 for window in calls)
    assert dataset.labels == [0, 1, 1, 1]
    assert dataset[0][0].shape == (2, 9)
    assert dataset[0][0].numpy().dtype == np.float32
    assert dataset[0][0].numpy()[0, 0] == 2
    assert dataset[0][1].item() == 0


def test_v1_matches_shared_function_and_does_not_depend_on_other_windows(ticks):
    dataset = training.TimeSeriesDataset(ticks, seq_length=2, preprocessing_version="tick-window-v1")
    window = ticks.loc[[30, 20]].to_dict("records")
    expected = preprocessing.prepare_tick_window(window, 2)[0]
    np.testing.assert_array_equal(dataset.sequences[0], expected)
    subset = training.TimeSeriesDataset(
        ticks.loc[[30, 20]], seq_length=2, preprocessing_version="tick-window-v1",
    )
    np.testing.assert_array_equal(dataset.sequences[0], subset.sequences[0])


@pytest.mark.parametrize("result", [
    np.zeros((2, 9), dtype=np.float32),
    np.zeros((1, 2, 9), dtype=np.float64),
    np.full((1, 2, 9), np.nan, dtype=np.float32),
])
def test_v1_rejects_invalid_shared_output(monkeypatch, ticks, result):
    monkeypatch.setattr(preprocessing, "prepare_tick_window", lambda *args: result)
    with pytest.raises(ValueError, match="invalid array"):
        training.TimeSeriesDataset(ticks, seq_length=2, preprocessing_version="tick-window-v1")


def test_v1_rejects_incompatible_shared_version(monkeypatch, ticks):
    monkeypatch.setattr(preprocessing, "PREPROCESSING_VERSION", "other")
    with pytest.raises(ValueError, match="does not match"):
        training.TimeSeriesDataset(ticks, seq_length=2, preprocessing_version="tick-window-v1")


@pytest.mark.parametrize("column", [
    "timestamp", "symbol", "bid", "ask", "mid", "bid_size", "ask_size",
    "last_trade_price", "last_trade_size", "is_anomaly",
])
def test_v1_rejects_missing_columns_even_with_ohlc(ticks, column):
    frame = ticks.drop(columns=column).assign(open=1, high=2, low=0, close=1, volume=10)
    with pytest.raises(ValueError, match=column):
        training.TimeSeriesDataset(frame, seq_length=2, preprocessing_version="tick-window-v1")


@pytest.mark.parametrize(("column", "value"), [
    ("timestamp", "bad"), ("timestamp", None), ("symbol", None), ("symbol", "  "),
    ("bid", np.nan), ("ask", np.inf), ("mid", "bad"), ("bid_size", -1),
    ("ask_size", -1), ("last_trade_price", None), ("last_trade_size", np.nan),
    ("is_anomaly", None), ("is_anomaly", 2), ("spread", np.inf),
])
def test_v1_rejects_invalid_values(ticks, column, value):
    if column in ticks:
        ticks[column] = ticks[column].astype(object)
    ticks.loc[10, column] = value
    with pytest.raises(ValueError, match=column):
        training.TimeSeriesDataset(ticks, seq_length=2, preprocessing_version="tick-window-v1")


@pytest.mark.parametrize("length", [0, -1, 1.5, True])
def test_v1_rejects_bad_sequence_length(ticks, length):
    with pytest.raises(ValueError, match="seq_length"):
        training.TimeSeriesDataset(ticks, seq_length=length, preprocessing_version="tick-window-v1")


def test_v1_rejects_short_groups_unknown_versions_and_custom_features(ticks):
    with pytest.raises(ValueError, match="No sequences"):
        training.TimeSeriesDataset(ticks, seq_length=5, preprocessing_version="tick-window-v1")
    with pytest.raises(ValueError, match="Unknown preprocessing"):
        training.TimeSeriesDataset(ticks, preprocessing_version="future")
    with pytest.raises(ValueError, match="standard features"):
        training.TimeSeriesDataset(ticks, feature_cols=["bid"], preprocessing_version="tick-window-v1")


def test_default_legacy_is_unchanged(ticks):
    ticks = ticks.reset_index(drop=True)
    ticks["bid_ask_ratio"] = ticks["bid"] / ticks["ask"]
    ticks["spread"] = ticks["ask"] - ticks["bid"]
    default = training.TimeSeriesDataset(ticks, seq_length=2)
    explicit = training.TimeSeriesDataset(ticks, seq_length=2, preprocessing_version="legacy")
    assert training.TimeSeriesTransformerConfig().preprocessing_version == "legacy"
    assert default.labels == explicit.labels
    for first, second in zip(default.sequences, explicit.sequences, strict=True):
        np.testing.assert_array_equal(first, second)


@pytest.mark.parametrize(("timestamps", "expected"), [
    (list(range(10, 0, -1)), [list(range(1, 9)), [9, 10]]),
    ([10, 9, 9, 9, 6, 5, 4, 3, 2, 1], [[1, 2, 3, 4, 5, 6], [9, 9, 9, 10]]),
])
def test_train_wires_both_datasets_and_persists_version(monkeypatch, tmp_path, timestamps, expected):
    calls = []
    monkeypatch.setattr(
        training.pd, "read_csv",
        lambda _: pd.DataFrame({"timestamp": timestamps, "symbol": ["A"] * 10}),
    )

    def dataset(frame, **kwargs):
        calls.append((frame["timestamp"].tolist(), kwargs))
        return [0]

    monkeypatch.setattr(training, "TimeSeriesDataset", dataset)
    monkeypatch.setattr(training, "DataLoader", lambda data, **kwargs: data)
    monkeypatch.setattr(training, "export_to_onnx", lambda *args, **kwargs: None)

    class FakeTrainer:
        def __init__(self, *args, **kwargs):
            pass

        def fit(self, *args):
            return {}

    monkeypatch.setattr(training, "TimeSeriesTransformerTrainer", FakeTrainer)
    config = training.TimeSeriesTransformerConfig(
        seq_length=2, hidden_dim=8, n_heads=2, n_layers=1,
        preprocessing_version="tick-window-v1",
    )
    training.train_transformer("unused", str(tmp_path), config)
    assert calls == [
        (expected[0], {"seq_length": 2, "preprocessing_version": "tick-window-v1"}),
        (expected[1], {"seq_length": 2, "preprocessing_version": "tick-window-v1"}),
    ]
    assert json.loads((tmp_path / "model_config.json").read_text())["preprocessing_version"] == "tick-window-v1"


def test_v1_requires_coverage_on_both_sides_of_chronological_split(monkeypatch, tmp_path):
    frame = pd.DataFrame({"timestamp": range(10), "symbol": ["A"] * 8 + ["B"] * 2})
    monkeypatch.setattr(training.pd, "read_csv", lambda _: frame)
    config = training.TimeSeriesTransformerConfig(seq_length=2, preprocessing_version="tick-window-v1")
    with pytest.raises(ValueError, match="Each symbol"):
        training.train_transformer("unused", str(tmp_path), config)


def test_v1_does_not_overwrite_existing_artifacts(tmp_path):
    existing = tmp_path / "model_config.json"
    existing.write_text("existing")
    config = training.TimeSeriesTransformerConfig(preprocessing_version="tick-window-v1")
    with pytest.raises(FileExistsError, match="existing model artifacts"):
        training.train_transformer("unused", str(tmp_path), config)
    assert existing.read_text() == "existing"


@pytest.mark.parametrize(("arguments", "expected"), [
    ([], "legacy"),
    (["--preprocessing-version", "tick-window-v1"], "tick-window-v1"),
])
def test_cli_passes_preprocessing_version(monkeypatch, arguments, expected):
    calls = []
    monkeypatch.setattr(sys, "argv", ["training", *arguments])
    monkeypatch.setattr(training, "train_transformer", lambda **kwargs: calls.append(kwargs))
    training.main()
    assert calls[0]["config"].preprocessing_version == expected


def test_v1_requires_nine_model_features():
    config = training.TimeSeriesTransformerConfig(n_features=3, preprocessing_version="tick-window-v1")
    with pytest.raises(ValueError, match="n_features=9"):
        training.train_transformer("unused", config=config)
