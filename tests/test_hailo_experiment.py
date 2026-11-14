import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from scripts import run_hailo_experiment as experiment
from src.hailo_contract import HAILO_FEATURES
from src.train_catboost_model import LABEL_HORIZON, add_features, create_profit_labels
from src.train_hailo_model import prepare_hailo_data


def make_data():
    rows = []
    for coin, n in (("BTC", 500), ("ETH", 475)):
        for i in range(n):
            close = 100 + 8 * np.sin(i / 9) + 2 * np.sin(i / 3)
            row = {name: float(i % 20 + 1) for name in HAILO_FEATURES
                   if name not in experiment.DERIVED_FEATURES}
            row.update({"timestamp": f"{pd.Timestamp('2024-01-01', tz='UTC') + pd.Timedelta(hours=i)}",
                        "coin": coin, "close": close, "volume": 10 + i % 7})
            rows.append(row)
    return pd.DataFrame(rows)


@pytest.mark.parametrize("text", [
    "{}", '{"epochs":1,"patience":1,"learning_rate":0.001,"extra":1}',
    '{"epochs":true,"patience":1,"learning_rate":0.001}',
    '{"epochs":1.0,"patience":1,"learning_rate":0.001}',
    '{"epochs":301,"patience":1,"learning_rate":0.001}',
    '{"epochs":1,"patience":2,"learning_rate":0.001}',
    '{"epochs":1,"patience":1,"learning_rate":NaN}',
    '{"epochs":1,"epochs":2,"patience":1,"learning_rate":0.001}',
    '{"epochs":1,"patience":1,"learning_rate":0.1}',
])
def test_invalid_proposals(text, tmp_path):
    proposal = tmp_path / "proposal.json"
    proposal.write_text(text)
    with pytest.raises(ValueError):
        experiment.load_proposal(proposal)


def test_holdout_is_excluded_and_coin_horizons_preserved(tmp_path):
    data = make_data()
    source = data.to_csv(index=False).encode()
    x, y, start, _ = experiment.split_data(source, tmp_path / "dev.csv")
    dev = pd.read_csv(tmp_path / "dev.csv", parse_dates=["timestamp"])
    assert dev.timestamp.max() < start
    expected = create_profit_labels(add_features(data.assign(
        timestamp=pd.to_datetime(data.timestamp)
    )), horizon=LABEL_HORIZON)
    expected = expected[(expected.timestamp >= start) & expected.future_net_return.notna()]
    assert len(y) == len(expected) and len(x) == len(expected)
    # Changing every holdout close cannot alter the development CSV or its split.
    data.loc[pd.to_datetime(data.timestamp) >= start, "close"] *= 3
    experiment.split_data(data.to_csv(index=False).encode(), tmp_path / "other.csv")
    assert (tmp_path / "dev.csv").read_bytes() == (tmp_path / "other.csv").read_bytes()
    # Every label available to the trainer ends within the development interval.
    labelled_dev = create_profit_labels(add_features(dev), horizon=LABEL_HORIZON)
    for _, coin_data in labelled_dev.groupby("coin"):
        assert coin_data.tail(LABEL_HORIZON).future_net_return.isna().all()


def test_invalid_duplicate_timestamp_coin(tmp_path):
    data = make_data()
    data = pd.concat([data, data.iloc[[0]]])
    with pytest.raises(ValueError, match="Duplicate"):
        experiment.split_data(data.to_csv(index=False).encode(), tmp_path / "dev.csv")


def test_missing_feature_is_rejected(tmp_path):
    data = make_data().drop(columns="rsi")
    with pytest.raises(ValueError, match="Missing columns: rsi"):
        experiment.split_data(data.to_csv(index=False).encode(), tmp_path / "dev.csv")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "not-a-number"])
def test_invalid_feature_is_rejected(tmp_path, value):
    data = make_data()
    data["rsi"] = data["rsi"].astype(object)
    data.loc[0, "rsi"] = value
    with pytest.raises(ValueError, match="Feature rsi must contain finite"):
        experiment.split_data(data.to_csv(index=False).encode(), tmp_path / "dev.csv")


def test_real_offline_run_and_immutable_outputs(tmp_path):
    data = tmp_path / "data.csv"
    make_data().to_csv(data, index=False)
    proposal = tmp_path / "proposal.json"
    proposal.write_text(json.dumps({"epochs": 2, "patience": 1, "learning_rate": 0.001}))
    output = tmp_path / "experiment"
    result = experiment.run(data, proposal, output)
    assert result == json.loads((output / "result.json").read_text())
    assert result["source_sha256"] == hashlib.sha256(data.read_bytes()).hexdigest()
    assert result["baseline"]["rows"] == result["candidate"]["rows"] > 0
    assert result["macro_f1_delta"] == pytest.approx(
        result["candidate"]["macro_f1"] - result["baseline"]["macro_f1"]
    )
    train_x, _, validation_x, _, _ = prepare_hailo_data(str(output / "development.csv"))
    model_ids = set()
    for model in ("baseline", "candidate"):
        model_dir = output / model
        meta = json.loads((model_dir / "metadata.json").read_text())
        model_ids.add(meta["model_id"])
        assert meta["data_provenance"]["training_data_sha256"] == result["development_sha256"]
        calibration = np.load(model_dir / "calibration.npy")
        expected = ((train_x - np.asarray(meta["means"])) / meta["scales"]).astype(np.float32)
        assert np.allclose(calibration.reshape(-1, 22), expected)
        assert np.load(model_dir / "validation_inputs.npy").shape[0] == len(validation_x)
    assert len(model_ids) == 2
    before = (output / "result.json").read_bytes()
    with pytest.raises(FileExistsError):
        experiment.run(data, proposal, output)
    assert (output / "result.json").read_bytes() == before


def test_failure_never_creates_result(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data.csv"
    make_data().to_csv(data, index=False)
    proposal = tmp_path / "proposal.json"
    proposal.write_text('{"epochs":1,"patience":1,"learning_rate":0.001}')
    output = tmp_path / "failed"

    def fail(*args, **kwargs):
        raise RuntimeError("training failed")

    monkeypatch.setattr(experiment, "train_and_export", fail)
    assert experiment.main(["--data", str(data), "--proposal", str(proposal),
                            "--output-dir", str(output)]) == 1
    assert "training failed" in capsys.readouterr().err
    assert not (output / "result.json").exists()
