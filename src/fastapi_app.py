import os
import pathlib

from fastapi import FastAPI, HTTPException, Query

from src.api_models import (
    CatBoostPredictionRequest,
    CatBoostPredictionResponse,
    ResearchSignalFeatures,
    ResearchSignalPayload,
    ScorecardResponse,
)
from src.go_no_go_scorecard import ScorecardDataError, evaluate_scorecard
from src.predict_catboost import CatBoostTradingPredictor
from src.research_signal import (
    load_research_signal_payload,
    normalize_research_payload_model,
)

app = FastAPI(
    title="Trading Bot Typed API",
    version="1.0.0",
    description="Typed FastAPI endpoints for scorecards and research signals.",
)

# Allowlist: when JOURNAL_DIR is set, restrict scorecard reads to that directory only.
# When not set, any absolute path is accepted but relative paths with traversal are blocked.
_SCORECARD_JOURNAL_DIR = os.getenv("JOURNAL_DIR", "")
_SCORECARD_ALLOWED_DIR: pathlib.Path | None = (
    pathlib.Path(_SCORECARD_JOURNAL_DIR).resolve(
    ) if _SCORECARD_JOURNAL_DIR else None
)


def _validate_scorecard_path(file: str) -> pathlib.Path:
    """Resolve and validate the scorecard file path to prevent path traversal.

    - If JOURNAL_DIR is set: the file must resolve to within that directory.
    - If JOURNAL_DIR is not set: absolute paths are accepted as-is; relative paths
      are resolved against the project root and must not escape it.
    """
    project_root = pathlib.Path(__file__).parent.parent.resolve()
    try:
        if pathlib.Path(file).is_absolute():
            resolved = pathlib.Path(file).resolve()
        else:
            resolved = (project_root / file).resolve()
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail="Invalid file path.") from exc

    if _SCORECARD_ALLOWED_DIR is not None:
        if not str(resolved).startswith(str(_SCORECARD_ALLOWED_DIR) + os.sep) and resolved != _SCORECARD_ALLOWED_DIR:
            raise HTTPException(
                status_code=403,
                detail="Access denied: file is outside the allowed directory.",
            )
    elif not pathlib.Path(file).is_absolute():
        # Relative path: block traversal that escapes the project root.
        if not str(resolved).startswith(str(project_root) + os.sep) and resolved != project_root:
            raise HTTPException(
                status_code=403,
                detail="Access denied: relative path escapes the project root.",
            )
    return resolved


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
    validated_path = _validate_scorecard_path(file)
    try:
        return evaluate_scorecard(
            file_path=str(validated_path),
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
