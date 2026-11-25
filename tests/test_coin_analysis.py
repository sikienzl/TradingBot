"""Unit tests for the layer-1 per-coin analysis.

Every function receives the config and the collaborators (OHLCV fetcher, ML
predictor, tabular predictor, rule recommender) explicitly, so these tests drive
the real code paths with plain stubs and never construct a
:class:`src.trading_bot.CryptoTradingBot`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.coin_analysis import (
    analyze_coin,
    apply_ml_signal,
    build_tabular_features,
    recommendation_bias,
    should_apply_tabular_signal,
)


@dataclass
class _Config:
    base_currency: str = "EUR"
    ohlcv_timeframe: str = "1h"
    ohlcv_limit: int = 60
    rsi_period: int = 14
    sma_period_short: int = 20
    sma_period_long: int = 50
    ml_min_confidence: float = 0.6
    tabular_min_confidence: float = 0.5
    tabular_buy_min_confidence: float = 0.55
    tabular_model_backend: str = "catboost"
    tabular_block_on_sell: bool = True
    tabular_sell_block_min_proba: float = 0.6
    tabular_allow_buy_entries: bool = True
    tabular_source_gate_enabled: bool = True
    tabular_override_min_confidence: float = 0.8
    tabular_override_margin: float = 0.2


def _ohlcv(rows: int, *, start_price: float = 100.0, drift: float = 0.1) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=rows, freq="h")
    close = start_price + drift * np.arange(rows, dtype=float)
    return pd.DataFrame(
        {
            "timestamp": index,
            "open": close - 0.05,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.linspace(10.0, 20.0, rows),
        }
    )


def _fetcher(frame: pd.DataFrame | None):
    calls: list[tuple[str, dict[str, Any]]] = []

    def fetch(symbol: str, **kwargs: Any) -> pd.DataFrame | None:
        calls.append((symbol, kwargs))
        return frame

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def _rules(recommendation: str = "HOLD", score: int = 50):
    def rule_recommender(rsi: float, sma_short: float, sma_long: float, price: float):
        return recommendation, score

    return rule_recommender


class _TabularPredictor:
    def __init__(self, result: dict[str, Any] | None = None, error: Exception | None = None):
        self.result = {
            "decision": "halten",
            "confidence": 0.0,
            "proba": {"kaufen": 0.0, "halten": 1.0, "verkaufen": 0.0},
        }
        if result is not None:
            self.result.update(result)
        self.error = error
        self.frames: list[pd.DataFrame] = []
        self.thresholds: list[float] = []

    def predict(self, row_data: pd.DataFrame, confidence_threshold: float) -> dict[str, Any]:
        self.frames.append(row_data)
        self.thresholds.append(confidence_threshold)
        if self.error is not None:
            raise self.error
        return self.result


class _MLPredictor:
    def __init__(self, result: dict[str, Any] | None = None, error: Exception | None = None):
        self.result = {"decision": "halten", "confidence": 0.0}
        if result is not None:
            self.result.update(result)
        self.error = error
        self.frames: list[pd.DataFrame] = []

    def predict(self, row_data: pd.DataFrame) -> dict[str, Any]:
        self.frames.append(row_data)
        if self.error is not None:
            raise self.error
        return self.result


# --------------------------------------------------------------------------- #
# recommendation_bias
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("recommendation", "expected"),
    [
        ("STRONG BUY", 1),
        ("buy", 1),
        (" HOLD (UP-TREND) ", 1),
        ("SELL", -1),
        ("weak sell", -1),
        ("HOLD (DOWN-TREND)", -1),
        ("HOLD", 0),
        ("", 0),
        (None, 0),  # type: ignore[arg-type]
    ],
)
def test_recommendation_bias_maps_rule_labels(recommendation: str, expected: int) -> None:
    assert recommendation_bias(recommendation) == expected


# --------------------------------------------------------------------------- #
# should_apply_tabular_signal
# --------------------------------------------------------------------------- #

def test_tabular_gate_blocks_buy_entries_when_disabled() -> None:
    config = _Config(tabular_allow_buy_entries=False)
    assert should_apply_tabular_signal(
        config, "HOLD", 50, "kaufen", 0.99
    ) == (False, "buy_entries_disabled")


def test_tabular_gate_requires_min_confidence() -> None:
    config = _Config()
    assert should_apply_tabular_signal(config, "HOLD", 50, "kaufen", 0.54) == (
        False,
        "below_min_confidence",
    )
    assert should_apply_tabular_signal(config, "HOLD", 50, "verkaufen", 0.49) == (
        False,
        "below_min_confidence",
    )


def test_tabular_gate_disabled_passes_everything() -> None:
    config = _Config(tabular_source_gate_enabled=False)
    assert should_apply_tabular_signal(config, "STRONG BUY", 90, "verkaufen", 0.6) == (
        True,
        "gate_disabled",
    )


def test_tabular_gate_needs_a_directional_signal() -> None:
    config = _Config()
    assert should_apply_tabular_signal(config, "HOLD", 50, "halten", 0.9) == (
        False,
        "neutral_tabular_signal",
    )


def test_tabular_gate_accepts_rule_confirmation() -> None:
    config = _Config()
    assert should_apply_tabular_signal(config, "BUY", 60, "kaufen", 0.6) == (
        True,
        "rule_confirmed",
    )
    assert should_apply_tabular_signal(config, "SELL", 20, "verkaufen", 0.7) == (
        True,
        "rule_confirmed",
    )


def test_tabular_gate_allows_strong_override_only_with_margin() -> None:
    config = _Config(tabular_override_min_confidence=0.8, tabular_override_margin=0.2)
    # rule_strength = |20 - 50| / 50 = 0.6 -> advantage = 0.85 - 0.6 = 0.25
    assert should_apply_tabular_signal(config, "BUY", 20, "verkaufen", 0.85) == (
        True,
        "strong_override",
    )
    assert should_apply_tabular_signal(config, "BUY", 20, "verkaufen", 0.79) == (
        False,
        "gated_by_rules",
    )
    # A confident rule signal (strength 0.9) leaves no override margin.
    assert should_apply_tabular_signal(config, "STRONG BUY", 95, "verkaufen", 0.85) == (
        False,
        "gated_by_rules",
    )


# --------------------------------------------------------------------------- #
# build_tabular_features
# --------------------------------------------------------------------------- #

def test_build_tabular_features_row_columns_and_macd_hist() -> None:
    frame = _ohlcv(60)
    close = frame["close"]
    row, macd_hist = build_tabular_features(
        frame, close, 105.0, 55.0, 100.0, 95.0, 1.2
    )
    assert len(row) == 1
    expected_columns = {
        "rsi", "macd", "macd_signal", "macd_hist", "sma_50", "sma_200",
        "ema_20", "ema_50", "ema_200", "atr_14", "stoch_k", "stoch_d",
        "cci_20", "obv", "bb_upper", "bb_middle", "bb_lower", "volume",
        "ret_1", "ret_3", "ret_6", "vol_6",
    }
    assert set(row.columns) == expected_columns
    values = row.iloc[0]
    ema12 = close.ewm(span=12, adjust=False).mean().iloc[-1]
    ema26 = close.ewm(span=26, adjust=False).mean().iloc[-1]
    signal = (close.ewm(span=12, adjust=False).mean()
              - close.ewm(span=26, adjust=False).mean()).ewm(
        span=9, adjust=False).mean().iloc[-1]
    assert values["macd"] == pytest.approx(ema12 - ema26)
    assert values["macd_signal"] == pytest.approx(signal)
    assert values["macd_hist"] == pytest.approx(ema12 - ema26 - signal)
    assert macd_hist == pytest.approx(values["macd_hist"])
    assert values["sma_50"] == 100.0
    assert values["sma_200"] == 95.0
    assert values["atr_14"] == 1.2
    assert values["volume"] == pytest.approx(float(frame["volume"].iloc[-1]))
    assert values["ret_1"] == pytest.approx(float(close.pct_change(1).iloc[-1]))
    assert values["bb_upper"] > values["bb_middle"] > values["bb_lower"]
    # Enough history for the stochastic and CCI branches.
    assert not math.isclose(values["stoch_k"], 50.0)
    assert not math.isclose(values["cci_20"], 0.0)


def test_build_tabular_features_short_frame_falls_back_to_neutrals() -> None:
    frame = _ohlcv(3)
    row, macd_hist = build_tabular_features(
        frame, frame["close"], 100.0, float("nan"), float("nan"), float("nan"),
        float("nan"),
    )
    values = row.iloc[0]
    assert len(row) == 1
    assert values["rsi"] == 50.0
    assert values["sma_50"] == 100.0
    assert values["sma_200"] == 100.0
    assert values["atr_14"] == 0.0
    assert values["stoch_k"] == 50.0
    assert values["stoch_d"] == 50.0
    assert values["cci_20"] == 0.0
    assert values["bb_middle"] == 100.0
    assert values["bb_upper"] == 100.0
    assert values["bb_lower"] == 100.0
    assert values["ret_3"] == 0.0
    assert values["ret_6"] == 0.0
    assert values["vol_6"] == 0.0
    assert math.isfinite(macd_hist)


def test_build_tabular_features_missing_volume_column() -> None:
    # A single row skips the OBV branch, which is the only place that reads
    # the volume column directly.
    frame = _ohlcv(1).drop(columns=["volume"])
    row, _ = build_tabular_features(
        frame, frame["close"], 100.0, 50.0, 100.0, 100.0, 1.0
    )
    assert row.iloc[0]["volume"] == 0.0
    assert row.iloc[0]["obv"] == 0.0


# --------------------------------------------------------------------------- #
# apply_ml_signal
# --------------------------------------------------------------------------- #

def test_apply_ml_signal_without_predictor_is_a_passthrough() -> None:
    result = apply_ml_signal(
        _Config(), None, "BTC", _ohlcv(60), 100.0, 50.0, "HOLD", 50, "rules", None
    )
    assert result == ("HOLD", 50, "rules", None)


def test_apply_ml_signal_weaves_buy_vote_into_hold() -> None:
    predictor = _MLPredictor({"decision": "kaufen", "confidence": 0.8})
    recommendation, score, source, confidence = apply_ml_signal(
        _Config(), predictor, "BTC", _ohlcv(60), 100.0, 50.0, "HOLD", 50,
        "rules", None,
    )
    assert (recommendation, score, source) == ("BUY", 70, "llm")
    assert confidence == 0.8
    assert len(predictor.frames) == 1
    ml_columns = {"coin", "timestamp", "open", "high", "low", "close", "volume", "rsi", "macd"}
    assert ml_columns <= set(predictor.frames[0].columns)


def test_apply_ml_signal_boosts_existing_buy() -> None:
    predictor = _MLPredictor({"decision": "kaufen", "confidence": 0.8})
    recommendation, score, source, _ = apply_ml_signal(
        _Config(), predictor, "BTC", _ohlcv(60), 100.0, 50.0, "BUY", 80,
        "rules", None,
    )
    assert (recommendation, score, source) == ("BUY", 92, "llm")


def test_apply_ml_signal_turns_buy_into_weak_sell() -> None:
    predictor = _MLPredictor({"decision": "verkaufen", "confidence": 0.9})
    recommendation, score, source, _ = apply_ml_signal(
        _Config(), predictor, "BTC", _ohlcv(60), 100.0, 50.0, "BUY", 80,
        "rules", None,
    )
    assert (recommendation, score, source) == ("WEAK SELL", 30, "llm")


def test_apply_ml_signal_below_min_confidence_keeps_rule_signal() -> None:
    predictor = _MLPredictor({"decision": "kaufen", "confidence": 0.4})
    result = apply_ml_signal(
        _Config(ml_min_confidence=0.6), predictor, "BTC", _ohlcv(60), 100.0,
        50.0, "HOLD", 50, "rules", None,
    )
    assert result == ("HOLD", 50, "rules", None)


def test_apply_ml_signal_failure_logs_warning_and_keeps_rule_signal(caplog) -> None:
    predictor = _MLPredictor(error=RuntimeError("model down"))
    with caplog.at_level("INFO"):
        result = apply_ml_signal(
            _Config(), predictor, "BTC", _ohlcv(60), 100.0, 50.0, "HOLD", 50,
            "rules", None,
        )
    assert result == ("HOLD", 50, "rules", None)
    assert "ML prediction for BTC failed: model down" in caplog.text


# --------------------------------------------------------------------------- #
# analyze_coin
# --------------------------------------------------------------------------- #

def test_analyze_coin_skips_missing_history() -> None:
    fetch = _fetcher(None)
    assert analyze_coin(
        _Config(), fetch, None, None, "BTC", 100.0, rule_recommender=_rules()
    ) is None
    assert fetch.calls == [("BTC/EUR", {"timeframe": "1h", "limit": 60})]


def test_analyze_coin_skips_truncated_history() -> None:
    fetch = _fetcher(_ohlcv(10))
    assert analyze_coin(
        _Config(), fetch, None, None, "BTC", 100.0, rule_recommender=_rules()
    ) is None


def test_analyze_coin_rules_only_result() -> None:
    frame = _ohlcv(60)
    result = analyze_coin(
        _Config(), _fetcher(frame), None, None, "BTC", 100.0,
        rule_recommender=_rules("BUY", 65),
    )
    assert result is not None
    close = frame["close"]
    assert result["recommendation"] == "BUY"
    assert result["score"] == 65
    assert result["rule_recommendation"] == "BUY"
    assert result["rule_score"] == 65
    assert result["signal_source"] == "rules"
    assert result["signal_confidence"] is None
    assert result["tabular_buy_proba"] is None
    assert result["tabular_sell_veto"] is False
    assert result["macd_hist"] is None
    assert result["ema_20"] == pytest.approx(
        close.ewm(span=20, adjust=False).mean().iloc[-1]
    )
    assert result["ret_1"] == pytest.approx(float(close.pct_change(1).iloc[-1]))
    assert result["ret_3"] == pytest.approx(float(close.pct_change(3).iloc[-1]))
    assert result["atr_14"] == pytest.approx(1.0)
    assert result["atr_pct"] == pytest.approx(1.0 / 100.0)


def test_analyze_coin_reports_missing_atr_as_none() -> None:
    # Passes the "len >= limit - 5" tolerance but is too short for ATR(14).
    result = analyze_coin(
        _Config(ohlcv_limit=16), _fetcher(_ohlcv(12)), None, None, "BTC", 100.0,
        rule_recommender=_rules(),
    )
    assert result is not None
    assert result["atr_14"] is None
    assert result["atr_pct"] is None


def test_analyze_coin_applies_ml_predictor() -> None:
    predictor = _MLPredictor({"decision": "kaufen", "confidence": 0.9})
    result = analyze_coin(
        _Config(), _fetcher(_ohlcv(60)), predictor, None, "BTC", 100.0,
        rule_recommender=_rules("HOLD", 50),
    )
    assert result is not None
    assert (result["recommendation"], result["score"]) == ("BUY", 70)
    assert result["signal_source"] == "llm"
    assert result["signal_confidence"] == 0.9
    assert (result["rule_recommendation"], result["rule_score"]) == ("HOLD", 50)


def test_analyze_coin_tabular_buy_confirms_rules() -> None:
    tabular = _TabularPredictor(
        {"decision": "kaufen", "confidence": 0.7,
         "proba": {"kaufen": 0.7, "halten": 0.2, "verkaufen": 0.1}}
    )
    result = analyze_coin(
        _Config(), _fetcher(_ohlcv(60)), None, tabular, "BTC", 100.0,
        rule_recommender=_rules("BUY", 60),
    )
    assert result is not None
    assert (result["recommendation"], result["score"]) == ("BUY", 68)
    assert result["signal_source"] == "catboost"
    assert result["signal_confidence"] == 0.7
    assert result["tabular_buy_proba"] == 0.7
    assert result["tabular_hold_proba"] == 0.2
    assert result["tabular_sell_proba"] == 0.1
    assert result["macd_hist"] is not None
    assert tabular.thresholds == [0.5]
    assert len(tabular.frames) == 1


def test_analyze_coin_tabular_sell_veto_overrides_buy(caplog) -> None:
    tabular = _TabularPredictor(
        {"decision": "verkaufen", "confidence": 0.8,
         "proba": {"kaufen": 0.05, "halten": 0.15, "verkaufen": 0.8}}
    )
    with caplog.at_level("INFO"):
        result = analyze_coin(
            _Config(tabular_sell_block_min_proba=0.6), _fetcher(_ohlcv(60)),
            None, tabular, "BTC", 100.0, rule_recommender=_rules("BUY", 70),
        )
    assert result is not None
    # The veto caps the score at 25; the (strong_override) tabular signal then
    # pushes the WEAK SELL down by int(0.8 * 8) = 6 more.
    assert (result["recommendation"], result["score"]) == ("WEAK SELL", 19)
    assert result["tabular_sell_veto"] is True
    assert result["signal_source"] == "catboost"
    assert result["signal_confidence"] == 0.8
    assert "sell-filter active for BTC" in caplog.text


def test_analyze_coin_preserves_rule_buy_below_entry_veto_threshold(caplog) -> None:
    # Sells hard enough to trip the veto, so the veto branch runs first; use a
    # decision that is gated instead to reach the "below threshold" branch.
    tabular = _TabularPredictor(
        {"decision": "verkaufen", "confidence": 0.55,
         "proba": {"kaufen": 0.1, "halten": 0.5, "verkaufen": 0.4}}
    )
    with caplog.at_level("INFO"):
        result = analyze_coin(
            _Config(tabular_sell_block_min_proba=0.6), _fetcher(_ohlcv(60)),
            None, tabular, "BTC", 100.0, rule_recommender=_rules("BUY", 70),
        )
    assert result is not None
    assert (result["recommendation"], result["score"]) == ("BUY", 70)
    assert result["tabular_sell_veto"] is False
    assert result["signal_source"] == "rules"
    assert "sell signal below entry-veto threshold for BTC" in caplog.text


def test_analyze_coin_logs_gated_tabular_signal(caplog) -> None:
    tabular = _TabularPredictor(
        {"decision": "verkaufen", "confidence": 0.7,
         "proba": {"kaufen": 0.1, "halten": 0.2, "verkaufen": 0.7}}
    )
    with caplog.at_level("INFO"):
        result = analyze_coin(
            _Config(tabular_block_on_sell=False,
                    tabular_override_min_confidence=0.95),
            _fetcher(_ohlcv(60)), None, tabular, "BTC", 100.0,
            rule_recommender=_rules("BUY", 60),
        )
    assert result is not None
    assert (result["recommendation"], result["score"]) == ("BUY", 60)
    assert result["signal_source"] == "rules"
    assert "gated for BTC: gated_by_rules" in caplog.text


def test_analyze_coin_tabular_buy_entries_disabled_is_gated(caplog) -> None:
    tabular = _TabularPredictor(
        {"decision": "kaufen", "confidence": 0.9,
         "proba": {"kaufen": 0.9, "halten": 0.05, "verkaufen": 0.05}}
    )
    with caplog.at_level("INFO"):
        result = analyze_coin(
            _Config(tabular_allow_buy_entries=False), _fetcher(_ohlcv(60)),
            None, tabular, "BTC", 100.0, rule_recommender=_rules("HOLD", 50),
        )
    assert result is not None
    assert (result["recommendation"], result["score"]) == ("HOLD", 50)
    assert "gated for BTC: buy_entries_disabled" in caplog.text


def test_analyze_coin_hailo_failure_preserves_rule_signal(caplog) -> None:
    tabular = _TabularPredictor(error=RuntimeError("hailo gone"))
    with caplog.at_level("INFO"):
        result = analyze_coin(
            _Config(tabular_model_backend="hailo"), _fetcher(_ohlcv(60)),
            None, tabular, "BTC", 100.0, rule_recommender=_rules("BUY", 80),
        )
    assert result is not None
    assert (result["recommendation"], result["score"]) == ("BUY", 80)
    assert result["signal_source"] == "rules"
    assert result["tabular_sell_veto"] is False
    assert "preserving the existing signal" in caplog.text


def test_analyze_coin_catboost_failure_keeps_rule_signal(caplog) -> None:
    tabular = _TabularPredictor(error=RuntimeError("catboost gone"))
    with caplog.at_level("INFO"):
        result = analyze_coin(
            _Config(), _fetcher(_ohlcv(60)), None, tabular, "BTC", 100.0,
            rule_recommender=_rules("BUY", 80),
        )
    assert result is not None
    assert (result["recommendation"], result["score"]) == ("BUY", 80)
    assert result["signal_source"] == "rules"
    assert "CatBoost prediction for BTC failed: catboost gone" in caplog.text


def test_atr_gate_skips_optional_models_before_inference(monkeypatch, caplog):
    calls = []

    class Predictor:
        def predict(self, *_args, **_kwargs):
            calls.append("tabular")
            return {"decision": "kaufen", "confidence": 1.0, "proba": {}}

    monkeypatch.setattr("src.coin_analysis.calculate_atr", lambda *_args, **_kwargs: 0.1)
    config = _Config()
    config.min_entry_atr_pct = 0.01
    with caplog.at_level("INFO", logger="src.coin_analysis"):
        result = analyze_coin(
            config,
            _fetcher(_ohlcv(60)),
            object(),
            Predictor(),
            "BTC",
            100.0,
            rule_recommender=_rules("BUY", 80),
        )

    assert result is not None
    assert result["recommendation"] == "BUY"
    assert result["signal_source"] == "rules"
    assert calls == []
    assert "ATR gate not met" in caplog.text
