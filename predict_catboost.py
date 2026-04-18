import json
from typing import Dict

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier

from research_signal import apply_research_features, load_latest_research_signal


class CatBoostTradingPredictor:
    def __init__(self, model_dir: str = "./model/catboost_trading_model", research_signal_path: str = ""):
        self.model_dir = model_dir
        self.research_signal_path = research_signal_path
        self.model = CatBoostClassifier()
        self.features = []
        self.label_map = {"verkaufen": 0, "halten": 1, "kaufen": 2}
        self.inv_label_map = {v: k for k, v in self.label_map.items()}
        self._load()

    def _load(self) -> None:
        model_path = f"{self.model_dir}/catboost_model.cbm"
        meta_path = f"{self.model_dir}/metadata.json"

        self.model.load_model(model_path)

        with open(meta_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)

        self.features = metadata.get("features", [])
        self.label_map = metadata.get("label_map", self.label_map)
        self.inv_label_map = {v: k for k, v in self.label_map.items()}

    def predict(self, row_df: pd.DataFrame, confidence_threshold: float = 0.45) -> Dict:
        if row_df.empty:
            return {"decision": "halten", "confidence": 0.0, "proba": {}}

        research_features = load_latest_research_signal(
            getattr(self, "research_signal_path", "")
        )
        x = apply_research_features(row_df.copy(), research_features)
        # Add missing fallback features if absent from live input.
        if "ret_1" not in x.columns and "close" in x.columns:
            x["ret_1"] = 0.0
        if "ret_3" not in x.columns and "close" in x.columns:
            x["ret_3"] = 0.0
        if "ret_6" not in x.columns and "close" in x.columns:
            x["ret_6"] = 0.0
        if "vol_6" not in x.columns and "close" in x.columns:
            x["vol_6"] = 0.0

        for feature in self.features:
            if feature not in x.columns:
                x[feature] = 0.0

        x = x[self.features].replace([np.inf, -np.inf], np.nan).fillna(0.0)

        proba = self.model.predict_proba(x)[0]
        pred_idx = int(np.argmax(proba))
        confidence = float(np.max(proba))
        decision = self.inv_label_map.get(pred_idx, "halten")

        # Conservative guard: only trade with sufficient confidence
        if confidence < confidence_threshold:
            decision = "halten"

        proba_dict = {
            self.inv_label_map.get(i, str(i)): float(p)
            for i, p in enumerate(proba)
        }

        return {
            "decision": decision,
            "confidence": confidence,
            "proba": proba_dict,
        }
