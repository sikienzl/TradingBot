from fastapi.testclient import TestClient

import src.fastapi_app as fastapi_app
from src.fastapi_app import app


client = TestClient(app)


def test_health_endpoint_returns_ok():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_normalize_research_signal_endpoint_returns_typed_features():
    response = client.post(
        "/research-signal/normalize",
        json={
            "sentiment_score": -0.4,
            "confidence": 0.7,
            "risk_score": 0.9,
            "market_regime": "bear",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["research_sentiment_score"] == -0.4
    assert body["research_regime_bear"] == 1.0
    assert body["research_regime_sideways"] == 0.0


def test_scorecard_endpoint_returns_typed_scorecard(tmp_path):
    journal_path = tmp_path / "trade_journal.csv"
    journal_path.write_text(
        "action,pnl_base,signal_source\n"
        "buy,0.0,catboost\n"
        "sell,2.0,catboost\n"
        "sell,-0.5,rules\n"
        "sell,1.5,catboost\n",
        encoding="utf-8",
    )

    response = client.get(
        "/scorecard",
        params={
            "file": str(journal_path),
            "min_closed_trades": 2,
            "min_win_rate": 40,
            "min_profit_factor": 1.1,
            "min_avg_pnl": 0,
            "max_drawdown_pct": 20,
            "recent_trades_window": 3,
            "min_recent_realized_pnl": 0,
            "min_recent_win_rate": 40,
            "min_source_trades_for_delta": 0,
            "min_catboost_vs_rules_pnl_delta": -10,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source_file"] == str(journal_path)
    assert body["metrics"]["closed_trades"] == 3
    assert body["verdict"] == "GO"


def test_scorecard_endpoint_returns_404_for_missing_file():
    response = client.get(
        "/scorecard", params={"file": "/tmp/does-not-exist.csv"})

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_catboost_predict_endpoint_returns_typed_prediction(monkeypatch):
    class _FakePredictor:
        def __init__(self, model_dir: str, research_signal_path: str):
            self.model_dir = model_dir
            self.research_signal_path = research_signal_path

        def predict_from_features(self, features, confidence_threshold=None):
            assert features["rsi"] == 48.0
            assert confidence_threshold == 0.55
            return {
                "decision": "kaufen",
                "confidence": 0.72,
                "proba": {"verkaufen": 0.08, "halten": 0.20, "kaufen": 0.72},
                "threshold_used": 0.55,
                "margin": 0.52,
            }

    monkeypatch.setattr(
        fastapi_app, "CatBoostTradingPredictor", _FakePredictor)

    response = client.post(
        "/predict/catboost",
        json={
            "model_dir": "./model/catboost_trading_model",
            "confidence_threshold": 0.55,
            "features": {
                "rsi": 48.0,
                "macd": 0.11,
                "ret_1": 0.02,
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["model_dir"] == "./model/catboost_trading_model"
    assert body["decision"] == "kaufen"
    assert body["proba"]["kaufen"] == 0.72


def test_catboost_predict_endpoint_validates_non_empty_features():
    response = client.post(
        "/predict/catboost",
        json={
            "features": {},
        },
    )

    assert response.status_code == 422


def test_catboost_predict_endpoint_rejects_unknown_features():
    response = client.post(
        "/predict/catboost",
        json={
            "features": {
                "rsi": 48.0,
                "unknown_signal": 1.23,
            },
        },
    )

    assert response.status_code == 422
