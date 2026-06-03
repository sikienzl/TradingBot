from fastapi import FastAPI, HTTPException, Query

from api_models import (
    CatBoostPredictionRequest,
    CatBoostPredictionResponse,
    ResearchSignalFeatures,
    ResearchSignalPayload,
    ScorecardResponse,
)
from go_no_go_scorecard import ScorecardDataError, evaluate_scorecard
from predict_catboost import CatBoostTradingPredictor
from research_signal import load_research_signal_payload, normalize_research_payload_model


app = FastAPI(
    title="Trading Bot Typed API",
    version="1.0.0",
    description="Typed FastAPI endpoints for scorecards and research signals.",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/research-signal", response_model=ResearchSignalPayload)
def get_research_signal(
    path: str = Query(default="./data/research_signal_latest.json"),
) -> ResearchSignalPayload:
    try:
        return load_research_signal_payload(path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/research-signal/normalize", response_model=ResearchSignalFeatures)
def normalize_research_signal(payload: dict[str, object]) -> ResearchSignalFeatures:
    return normalize_research_payload_model(payload)


@app.post("/predict/catboost", response_model=CatBoostPredictionResponse)
def predict_catboost_signal(payload: CatBoostPredictionRequest) -> CatBoostPredictionResponse:
    try:
        predictor = CatBoostTradingPredictor(
            model_dir=payload.model_dir,
            research_signal_path=payload.research_signal_path,
        )
        prediction = predictor.predict_from_features(
            payload.features.model_dump(exclude_none=True),
            confidence_threshold=payload.confidence_threshold,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return CatBoostPredictionResponse(
        model_dir=payload.model_dir,
        **prediction,
    )


@app.get("/scorecard", response_model=ScorecardResponse)
def get_scorecard(
    file: str = Query(default="trade_journal.csv"),
    base_currency: str = Query(default="EUR"),
    lookback_days: int = Query(default=0, ge=0),
    starting_capital: float = Query(default=20.0, gt=0.0),
    min_closed_trades: int = Query(default=200, ge=1),
    min_win_rate: float = Query(default=45.0),
    min_profit_factor: float = Query(default=1.2),
    min_avg_pnl: float = Query(default=0.0),
    max_drawdown_pct: float = Query(default=10.0, ge=0.0),
    recent_trades_window: int = Query(default=100, ge=1),
    min_recent_realized_pnl: float = Query(default=0.0),
    min_recent_win_rate: float = Query(default=45.0),
    min_catboost_vs_rules_pnl_delta: float = Query(default=-0.05),
    min_source_trades_for_delta: int = Query(default=50, ge=0),
) -> ScorecardResponse:
    try:
        return evaluate_scorecard(
            file_path=file,
            base_currency=base_currency,
            lookback_days=lookback_days,
            starting_capital=starting_capital,
            min_closed_trades=min_closed_trades,
            min_win_rate=min_win_rate,
            min_profit_factor=min_profit_factor,
            min_avg_pnl=min_avg_pnl,
            max_drawdown_pct=max_drawdown_pct,
            recent_trades_window=recent_trades_window,
            min_recent_realized_pnl=min_recent_realized_pnl,
            min_recent_win_rate=min_recent_win_rate,
            min_catboost_vs_rules_pnl_delta=min_catboost_vs_rules_pnl_delta,
            min_source_trades_for_delta=min_source_trades_for_delta,
        )
    except ScorecardDataError as exc:
        detail = str(exc)
        status_code = 404 if "not found" in detail.lower() else 422
        raise HTTPException(status_code=status_code, detail=detail) from exc
