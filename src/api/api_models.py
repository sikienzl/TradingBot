from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScorecardVerdict(StrEnum):
    GO = "GO"
    HOLD = "HOLD"
    NO_GO = "NO-GO"


class MarketRegime(StrEnum):
    BULL = "bull"
    BEAR = "bear"
    SIDEWAYS = "sideways"


class TradingDecision(StrEnum):
    SELL = "verkaufen"
    HOLD = "halten"
    BUY = "kaufen"


class ResearchSignalFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_sentiment_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    research_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    research_risk_score: float = Field(default=0.0, ge=0.0, le=1.0)
    research_regime_bull: float = Field(default=0.0, ge=0.0, le=1.0)
    research_regime_bear: float = Field(default=0.0, ge=0.0, le=1.0)
    research_regime_sideways: float = Field(default=1.0, ge=0.0, le=1.0)


class ResearchSignalPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider: str | None = None
    timestamp_utc: str | None = None
    sentiment_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    risk_score: float = Field(default=0.0, ge=0.0, le=1.0)
    market_regime: MarketRegime = MarketRegime.SIDEWAYS
    citations: list[str] = Field(default_factory=list)
    normalized_features: ResearchSignalFeatures = Field(
        default_factory=ResearchSignalFeatures)
    source_details: dict[str, Any] = Field(default_factory=dict)
    integration: dict[str, Any] = Field(default_factory=dict)


class ScorecardResult(BaseModel):
    verdict: ScorecardVerdict
    reasons: list[str] = Field(default_factory=list)


class ScorecardMetrics(BaseModel):
    closed_trades: int
    realized_pnl: float
    avg_pnl: float
    win_rate: float
    gross_profit: float
    gross_loss: float
    profit_factor: float
    max_drawdown_base: float
    max_drawdown_pct: float
    recent_closed_trades: int
    recent_realized_pnl: float
    recent_win_rate: float
    catboost_closed_trades: int
    catboost_realized_pnl: float
    rules_closed_trades: int
    rules_realized_pnl: float
    catboost_vs_rules_pnl_delta: float


class ScorecardThresholds(BaseModel):
    min_closed_trades: int
    min_win_rate: float
    min_profit_factor: float
    min_avg_pnl: float
    max_drawdown_pct: float
    recent_trades_window: int
    min_recent_realized_pnl: float
    min_recent_win_rate: float
    min_catboost_vs_rules_pnl_delta: float
    min_source_trades_for_delta: int
    starting_capital: float
    lookback_days: int


class ScorecardResponse(BaseModel):
    source_file: str
    base_currency: str
    metrics: ScorecardMetrics
    verdict: ScorecardVerdict
    reasons: list[str] = Field(default_factory=list)
    thresholds: ScorecardThresholds


class CatBoostFeatureInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rsi: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    sma_50: float | None = None
    sma_200: float | None = None
    ema_20: float | None = None
    ema_50: float | None = None
    ema_200: float | None = None
    atr_14: float | None = None
    stoch_k: float | None = None
    stoch_d: float | None = None
    cci_20: float | None = None
    obv: float | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    volume: float | None = None
    ret_1: float | None = None
    ret_3: float | None = None
    ret_6: float | None = None
    vol_6: float | None = None

    @model_validator(mode="after")
    def validate_has_at_least_one_feature(self) -> "CatBoostFeatureInput":
        if not self.model_dump(exclude_none=True):
            raise ValueError("At least one CatBoost feature must be provided")
        return self


class CatBoostPredictionRequest(BaseModel):
    model_dir: str = "./model/catboost_trading_model"
    research_signal_path: str = ""
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    features: CatBoostFeatureInput


class CatBoostPredictionResponse(BaseModel):
    model_dir: str
    decision: TradingDecision
    confidence: float = Field(ge=0.0, le=1.0)
    proba: dict[str, float] = Field(default_factory=dict)
    threshold_used: float = Field(ge=0.0, le=1.0)
    margin: float = Field(ge=0.0)
