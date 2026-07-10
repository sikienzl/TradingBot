from pathlib import Path

import numpy as np
import pytest

from src.hailo.inference import TimeSeriesTransformerONNX


class _FakeHailoRunner:
    output_names = ["anomaly_score", "confidence"]

    def infer(self, model_input: np.ndarray):
        assert model_input.shape == (1, 3, 9)
        return {
            "anomaly_score": np.array([[0.91]], dtype=np.float32),
            "confidence": np.array([[0.73]], dtype=np.float32),
        }


def test_infer_uses_direct_hailo_runner_when_available() -> None:
    model = TimeSeriesTransformerONNX.__new__(TimeSeriesTransformerONNX)
    model.model_path = Path(
        "model/hailo_prefilter/timeseries_transformer.onnx")
    model.hef_path = Path("model/hailo_prefilter/timeseries_transformer.hef")
    model.seq_length = 3
    model.n_features = 9
    model.last_inference_seconds = 0.0
    model.last_provider = "unknown"
    model.hailo_runner = _FakeHailoRunner()

    ticks = [
        {
            "bid": 100.0 + idx,
            "ask": 101.0 + idx,
            "mid": 100.5 + idx,
            "bid_size": 2.0,
            "ask_size": 1.5,
            "last_trade_price": 100.7 + idx,
            "last_trade_size": 0.2,
            "spread": 1.0,
        }
        for idx in range(3)
    ]

    score, confidence = model.infer(ticks)

    assert score == pytest.approx(91.0)
    assert confidence == pytest.approx(0.73)
    assert model.last_provider == "HailoHEF"
    assert model.last_inference_seconds >= 0.0
