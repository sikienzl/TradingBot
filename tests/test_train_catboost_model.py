import json

import numpy as np
import pandas as pd
import pytest

from src.train_catboost_model import (
    _json_fold,
    create_profit_labels,
    generate_walk_forward_splits,
    train_val_split_time,
)


def test_generate_walk_forward_splits_monotonic_windows():
    splits = generate_walk_forward_splits(
        n_rows=1200,
        n_splits=3,
        min_train_size=300,
        min_val_size=100,
    )

    assert len(splits) == 3
    prev_val_end = 0
    for train_start, train_end, val_start, val_end in splits:
        assert train_start == 0
        assert train_end >= 300
        assert val_end > val_start
        assert val_start == train_end
        assert val_start >= prev_val_end
        prev_val_end = val_end


def test_create_profit_labels_applies_roundtrip_costs():
    df = pd.DataFrame(
        {
            "coin": ["BTC", "BTC"],
            "close": [100.0, 100.5],
        }
    )

    out = create_profit_labels(
        df,
        horizon=1,
        buy_threshold=0.002,
        sell_threshold=-0.002,
        fee_per_trade=0.002,
        slippage=0.001,
    )

    # Bruttorendite der ersten Zeile: +0.5%
    gross_return = (100.5 / 100.0) - 1.0
    # Roundtrip-Kosten: 2 * (fee + slippage) = 0.6%
    expected_net = gross_return - 0.006

    assert np.isclose(out.loc[0, "future_net_return"], expected_net)
    assert out.loc[0, "label"] == "halten"


def test_train_val_split_purges_labels_that_cross_validation_boundary():
    df = pd.DataFrame(
        {
            "coin": ["BTC"] * 12,
            "timestamp": pd.date_range("2024-01-01", periods=12, freq="1h"),
            "label": [0] * 12,
        }
    )

    train, validation = train_val_split_time(df, train_ratio=0.5, horizon=2)

    assert len(validation) == 6
    assert train["timestamp"].max() == pd.Timestamp("2024-01-01 03:00:00")


def test_json_fold_writes_json_safe_scalars():
    """A fold must survive the trip into model metadata and the release manifest."""
    fold = {
        "fold": np.int64(2),
        "train_size": np.int64(650),
        "val_size": np.int64(150),
        "accuracy": np.float64(0.79),
        "macro_f1": np.float32(0.7),
        "weighted_f1": 0.73,
    }

    record = _json_fold(fold)

    assert set(record) == set(fold)
    assert isinstance(record["fold"], int)
    assert isinstance(record["train_size"], int)
    assert isinstance(record["val_size"], int)
    assert all(isinstance(record[key], float) for key in ("accuracy", "macro_f1", "weighted_f1"))
    restored = json.loads(json.dumps(record))
    assert restored["fold"] == 2
    assert restored["train_size"] == 650
    assert restored["accuracy"] == pytest.approx(0.79)
    assert restored["macro_f1"] == pytest.approx(0.7, abs=1e-6)


def test_train_val_split_purges_per_coin_without_timestamps():
    df = pd.DataFrame(
        {
            "coin": ["BTC", "ETH"] * 6,
            "label": [0] * 12,
        }
    )

    train, validation = train_val_split_time(df, train_ratio=0.5, horizon=1)

    assert len(validation) == 6
    assert train["coin"].tolist() == ["BTC", "ETH"] * 2
