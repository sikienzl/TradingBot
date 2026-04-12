import numpy as np
import pandas as pd

from train_catboost_model import create_profit_labels, generate_walk_forward_splits


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
