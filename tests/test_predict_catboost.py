import numpy as np
import pandas as pd

from predict_catboost import CatBoostTradingPredictor


class _FakeModel:
    def predict_proba(self, x):
        # max Wahrscheinlichkeit 0.40 => unter Standard-Threshold
        return np.array([[0.30, 0.40, 0.30]])


def test_catboost_predict_holds_when_confidence_too_low():
    predictor = CatBoostTradingPredictor.__new__(CatBoostTradingPredictor)
    predictor.model = _FakeModel()
    predictor.features = ["rsi", "macd", "ret_1"]
    predictor.label_map = {"verkaufen": 0, "halten": 1, "kaufen": 2}
    predictor.inv_label_map = {0: "verkaufen", 1: "halten", 2: "kaufen"}

    row = pd.DataFrame([{"rsi": 50.0, "macd": 0.2, "ret_1": 0.01}])
    result = predictor.predict(row, confidence_threshold=0.45)

    assert result["confidence"] == 0.40
    assert result["decision"] == "halten"
    assert set(result["proba"].keys()) == {"verkaufen", "halten", "kaufen"}
