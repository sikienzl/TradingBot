import json
from io import BytesIO

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.hailo_contract import (
    HAILO_FEATURES,
    HAILO_INPUT_NAME,
    HAILO_LABEL_MAP,
    HAILO_OUTPUT_NAME,
)
from src.hailo_runtime import (
    HailoModelMetadata,
    HailoRuntime,
    prediction_from_logits,
)
from src.hailo_service import create_app
from src.predict_hailo_remote import HailoInferenceError, HailoRemotePredictor
from src.trading_bot import BotConfig, CryptoTradingBot
from src.train_hailo_model import prepare_hailo_data


def _metadata_payload() -> dict:
    return {
        "model_id": "test-hailo-model",
        "features": list(HAILO_FEATURES),
        "means": [0.0] * len(HAILO_FEATURES),
        "scales": [1.0] * len(HAILO_FEATURES),
        "label_map": HAILO_LABEL_MAP,
        "input_name": HAILO_INPUT_NAME,
        "input_shape": [1, len(HAILO_FEATURES)],
        "output_name": HAILO_OUTPUT_NAME,
        "output_shape": [1, len(HAILO_LABEL_MAP)],
        "output_kind": "logits",
        "confidence_threshold": 0.5,
        "margin_threshold": 0.03,
    }


def test_metadata_requires_exact_feature_contract(tmp_path):
    metadata_path = tmp_path / "metadata.json"
    payload = _metadata_payload()
    payload["features"][0] = "wrong_feature"
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="feature order"):
        HailoModelMetadata.load(metadata_path)


def test_prediction_maps_logits_to_trading_decision():
    metadata_payload = _metadata_payload()
    metadata = HailoModelMetadata(
        model_id=metadata_payload["model_id"],
        means=np.zeros(len(HAILO_FEATURES)),
        scales=np.ones(len(HAILO_FEATURES)),
        confidence_threshold=0.5,
        margin_threshold=0.03,
    )

    result = prediction_from_logits(np.array([0.0, 0.1, 3.0]), metadata)

    assert result["decision"] == "kaufen"
    assert result["proba"]["kaufen"] > result["proba"]["halten"]


def test_runtime_normalizes_features_before_inference():
    metadata = HailoModelMetadata(
        model_id="test-hailo-model",
        means=np.ones(len(HAILO_FEATURES)),
        scales=np.full(len(HAILO_FEATURES), 2.0),
        confidence_threshold=0.5,
        margin_threshold=0.03,
    )

    class InferPipeline:
        def infer(self, inputs):
            assert inputs[HAILO_INPUT_NAME].shape == (len(HAILO_FEATURES),)
            assert np.allclose(inputs[HAILO_INPUT_NAME], 0.5)
            return {HAILO_OUTPUT_NAME: np.array([[0.0, 3.0, 0.0]])}

    runtime = HailoRuntime("unused.hef", "unused.json")
    runtime.metadata = metadata
    runtime._input_name = HAILO_INPUT_NAME
    runtime._output_name = HAILO_OUTPUT_NAME
    runtime._input_shape = (len(HAILO_FEATURES),)
    runtime._infer_pipeline = InferPipeline()

    result = runtime.predict({name: 2.0 for name in HAILO_FEATURES})

    assert result["decision"] == "halten"


def test_remote_predictor_sends_features_and_validates_response(monkeypatch):
    response_body = {
        "model_id": "test-hailo-model",
        "decision": "kaufen",
        "confidence": 0.8,
        "proba": {"verkaufen": 0.1, "halten": 0.1, "kaufen": 0.8},
        "threshold_used": 0.55,
        "margin": 0.7,
    }
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return BytesIO(json.dumps(response_body).encode("utf-8"))

    monkeypatch.setattr("src.predict_hailo_remote.urlopen", fake_urlopen)
    predictor = HailoRemotePredictor(
        "http://192.168.62.75:8001", "test-token", timeout_seconds=2.0
    )
    result = predictor.predict(pd.DataFrame([{"rsi": 55.0}]), 0.55)

    request = captured["request"]
    assert request.full_url == "http://192.168.62.75:8001/predict/hailo"
    assert request.get_header("Authorization") == "Bearer test-token"
    assert json.loads(request.data) == {
        "features": {"rsi": 55.0},
        "confidence_threshold": 0.55,
    }
    assert captured["timeout"] == 2.0
    assert result["decision"] == "kaufen"


def test_remote_predictor_raises_on_empty_input():
    predictor = HailoRemotePredictor("http://127.0.0.1:8001", "test-token")

    with pytest.raises(ValueError, match="exactly one"):
        predictor.predict(pd.DataFrame())


def test_remote_predictor_fails_on_http_error(monkeypatch):
    from urllib.error import URLError

    def fail_request(*args, **kwargs):
        raise URLError("unavailable")

    monkeypatch.setattr("src.predict_hailo_remote.urlopen", fail_request)
    predictor = HailoRemotePredictor("http://127.0.0.1:8001", "test-token")

    with pytest.raises(HailoInferenceError, match="request failed"):
        predictor.predict(pd.DataFrame([{"rsi": 55.0}]))


def test_hailo_service_requires_bearer_token():
    class Predictor:
        metadata = type("Metadata", (), {"model_id": "test-hailo-model"})()

        def load(self):
            return None

        def predict(self, features, confidence_threshold=None):
            return {
                "model_id": "test-hailo-model",
                "decision": "halten",
                "confidence": 0.8,
                "proba": {"verkaufen": 0.1, "halten": 0.8, "kaufen": 0.1},
                "threshold_used": confidence_threshold or 0.5,
                "margin": 0.7,
            }

    with TestClient(create_app(lambda: Predictor(), api_token="test-token")) as client:
        payload = {"features": {"rsi": 50.0}}
        denied = client.post("/predict/hailo", json=payload)
        allowed = client.post(
            "/predict/hailo",
            json=payload,
            headers={"Authorization": "Bearer test-token"},
        )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["decision"] == "halten"


def test_training_preparation_returns_fixed_feature_dimensions(tmp_path):
    from src import train_catboost_model

    rows = []
    timestamp = pd.Timestamp("2025-01-01", tz="UTC")
    for coin_index, coin in enumerate(("BTC", "ETH")):
        for index in range(500):
            price = 100.0 + 4.0 * np.sin((index + coin_index * 3) * 2 * np.pi / 24)
            rows.append(
                {
                    "timestamp": timestamp + pd.Timedelta(hours=index),
                    "coin": coin,
                    "close": price,
                    "volume": 1000.0 + index,
                }
            )
    data_path = tmp_path / "training.csv"
    pd.DataFrame(rows).to_csv(data_path, index=False)
    original_purge = train_catboost_model._purge_training_boundary
    split_times = {}

    def verify_split(train_df, validation_df, horizon):
        split_times["train_last"] = train_df["timestamp"].max()
        split_times["validation_first"] = validation_df["timestamp"].min()
        assert split_times["train_last"] < split_times["validation_first"]
        return original_purge(train_df, validation_df, horizon)

    train_catboost_model._purge_training_boundary = verify_split
    try:
        x_train, y_train, x_validation, y_validation, class_weights = (
            prepare_hailo_data(str(data_path))
        )
    finally:
        train_catboost_model._purge_training_boundary = original_purge

    assert x_train.shape[1] == len(HAILO_FEATURES)
    assert x_validation.shape[1] == len(HAILO_FEATURES)
    assert set(np.unique(y_train)) == {0, 1, 2}
    assert set(np.unique(y_validation)) == {0, 1, 2}
    assert len(class_weights) == len(HAILO_LABEL_MAP)


def test_hailo_failure_suppresses_bot_entries(monkeypatch):
    monkeypatch.setenv("SIMULATE_DATA", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("USE_TABULAR_MODEL", "false")
    monkeypatch.setenv("TABULAR_MODEL_BACKEND", "hailo")
    monkeypatch.setenv("PERFORMANCE_LOG_ENABLED", "false")
    bot = CryptoTradingBot(BotConfig())
    bot.config.use_tabular_model = True

    class FailingPredictor:
        def predict(self, *args, **kwargs):
            raise RuntimeError("service unavailable")

    bot.tabular_predictor = FailingPredictor()
    limit = bot.config.ohlcv_limit
    close = np.linspace(80.0, 120.0, limit)
    history = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(limit, 1000.0),
        }
    )
    bot._fetch_ohlcv_data = lambda *args, **kwargs: history

    result = bot._analyze_coin("BTC", float(close[-1]))

    assert result is not None
    assert result["recommendation"] == "HOLD"
    assert result["score"] == 0
    assert result["signal_source"] == "hailo_unavailable"
