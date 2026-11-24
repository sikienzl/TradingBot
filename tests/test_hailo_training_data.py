import json

import numpy as np
import pandas as pd
import pytest

from scripts import collect_hailo_training as collect
from scripts import prepare_hailo_calibration as prepare
from src.hailo_contract import HAILO_FEATURES
from src.train_catboost_model import LABEL_HORIZON
from src.train_hailo_model import TRAIN_FRACTION, file_sha256


class FakeExchange:
    def __init__(self, count=260):
        self.count = count
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        rng = np.random.default_rng(1)
        close = 100 + np.cumsum(rng.normal(0, 1, self.count))
        return [
            [1_700_000_000_000 + i * 3_600_000, c, c + 1, c - 1, c, 10 + i % 7]
            for i, c in enumerate(close)
        ]


def test_collect_uses_bot_feature_path_and_drops_open_candle(monkeypatch):
    for name in ("KRAKEN_API_KEY", "KRAKEN_API_SECRET"):
        monkeypatch.setenv(name, "secret")
    bot, capture = collect.build_capture_bot()
    assert bot.config.dry_run and bot.config.simulate_data
    assert bot.config.api_key == "" and bot.config.api_secret == ""
    exchange = FakeExchange()
    rows = collect.collect_rows(bot, capture, exchange, ["BTC"], limit=300)
    assert exchange.calls == [("BTC/EUR", "1h", 300)]
    assert len(rows) == 259 - bot.config.ohlcv_limit + 1
    assert set(HAILO_FEATURES) <= set(rows[0])
    assert rows[0]["coin"] == "BTC"
    assert isinstance(rows[0]["timestamp"], pd.Timestamp)


def test_collect_rejects_short_history():
    bot, capture = collect.build_capture_bot()
    with pytest.raises(RuntimeError, match="Insufficient"):
        collect.collect_rows(bot, capture, FakeExchange(50), ["ETH"])


def test_write_csv_refuses_overwrite(tmp_path):
    target = tmp_path / "out.csv"
    collect.write_csv([{"a": 1}], target, overwrite=False)
    with pytest.raises(FileExistsError):
        collect.write_csv([{"a": 2}], target, overwrite=False)
    collect.write_csv([{"a": 2}], target, overwrite=True)
    assert pd.read_csv(target)["a"].tolist() == [2]
    assert list(tmp_path.glob("*.tmp")) == []


def _setup(tmp_path, monkeypatch, **overrides):
    rng = np.random.default_rng(0)
    x_train = rng.normal(size=(50, 22)).astype(np.float32)
    x_validation = (rng.normal(size=(20, 22)) + 1000).astype(np.float32)
    monkeypatch.setattr(
        prepare, "prepare_hailo_data",
        lambda path: (x_train, None, x_validation, None, None),
    )
    data = tmp_path / "x.csv"
    data.write_text("a\n1\n")
    metadata = {
        "features": list(HAILO_FEATURES), "means": [0.0] * 22, "scales": [1.0] * 22,
        "data_provenance": {
            "training_data_sha256": file_sha256(data),
            "label_horizon": LABEL_HORIZON,
            "train_fraction": TRAIN_FRACTION,
            "train_rows": 50,
            "validation_rows": 20,
        },
    }
    metadata.update(overrides)
    (tmp_path / "metadata.json").write_text(json.dumps(metadata))
    return data


def test_calibration_comes_only_from_purged_training(tmp_path, monkeypatch):
    data = _setup(tmp_path, monkeypatch)
    result = prepare.prepare(data, tmp_path, size=1024)
    assert result == {"train": 50, "calibration": 50, "validation": 20}
    calibration = np.load(tmp_path / "calibration.npy")
    assert calibration.shape == (50, 1, 1, 22) and calibration.max() < 100
    assert np.load(tmp_path / "validation_inputs.npy").min() > 100
    assert (tmp_path / "calibration.alls").read_text() == (
        "model_optimization_config(calibration, calibset_size=50)\n"
    )


def test_calibration_rejects_feature_mismatch(tmp_path):
    (tmp_path / "metadata.json").write_text(json.dumps({"features": ["rsi"]}))
    with pytest.raises(ValueError, match="contract"):
        prepare.prepare(tmp_path / "x.csv", tmp_path)


@pytest.mark.parametrize("field,value", [
    ("training_data_sha256", "0" * 64), ("label_horizon", 99),
    ("train_fraction", 0.5), ("train_rows", 49), ("validation_rows", 19),
])
def test_calibration_detects_provenance_mismatch(tmp_path, monkeypatch, field, value):
    data = _setup(tmp_path, monkeypatch)
    metadata = json.loads((tmp_path / "metadata.json").read_text())
    metadata["data_provenance"][field] = value
    (tmp_path / "metadata.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="differ"):
        prepare.prepare(data, tmp_path)
    assert not (tmp_path / "calibration.npy").exists()


def test_calibration_requires_provenance(tmp_path, monkeypatch):
    data = _setup(tmp_path, monkeypatch)
    metadata = json.loads((tmp_path / "metadata.json").read_text())
    del metadata["data_provenance"]
    (tmp_path / "metadata.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="data_provenance"):
        prepare.prepare(data, tmp_path)


@pytest.mark.parametrize("override", [
    {"scales": [0.0] + [1.0] * 21}, {"scales": [-1.0] + [1.0] * 21},
    {"scales": [float("nan")] + [1.0] * 21}, {"means": [float("inf")] + [0.0] * 21},
])
def test_calibration_rejects_bad_normalization(tmp_path, monkeypatch, override):
    data = _setup(tmp_path, monkeypatch, **override)
    with pytest.raises(ValueError, match="finite"):
        prepare.prepare(data, tmp_path)


def test_calibration_refuses_overwrite_unless_requested(tmp_path, monkeypatch):
    data = _setup(tmp_path, monkeypatch)
    prepare.prepare(data, tmp_path)
    with pytest.raises(FileExistsError, match="calibration.npy"):
        prepare.prepare(data, tmp_path)
    assert prepare.prepare(data, tmp_path, overwrite=True)["calibration"] == 50
