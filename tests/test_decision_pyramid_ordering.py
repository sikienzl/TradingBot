"""Decision-pyramid ordering across the real collaborators.

The repository rule is that inexpensive technical gates run first, local
inference second and paid LLM/news checks last, and that a news or LLM result
may veto a trade but never bypass a technical or risk gate. The per-stage tests
live in ``test_market_analysis``, ``test_coin_analysis`` and
``test_execution_service``; this module walks the same real functions end to end
and records every stage, so a re-ordered gate that quietly moves a paid call in
front of a technical gate fails here.

Stages, in the order the pyramid requires:

1. volume gate (``MIN_VOLUME_BASE``)
2. spread gate (``MAX_ENTRY_SPREAD_BPS``)
3. ATR/volatility gate (``MIN_ENTRY_ATR_PCT``) - before any model call
4. local inference (Hailo/CPU/rules cascade)
5. pre-trade cost budget (``MAX_ENTRY_COST_BPS``)
6. paid news veto (the only paid call on the entry path)
7. order submission
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.coin_analysis import analyze_coin
from src.execution_service import EntryHooks, open_new_positions
from src.market_analysis import analyze_markets
from src.portfolio import Portfolio


@dataclass
class _StageLog:
    stages: list[str] = field(default_factory=list)

    def record(self, stage: str) -> None:
        self.stages.append(stage)


# ---------------------------------------------------------------------------
# Stages 1-2: volume and spread gate before any per-coin analysis
# ---------------------------------------------------------------------------

@dataclass
class _MarketConfig:
    base_currency: str = "EUR"
    min_volume_base: float = 100.0
    max_entry_spread_bps: float = 100.0
    top_n_for_analysis: int = 10
    excluded_coins: set[str] = field(default_factory=set)
    slow_analysis_warning_seconds: float = 30.0


def test_volume_gate_rejects_before_inference_or_paid_call() -> None:
    stages = _StageLog()
    config = _MarketConfig(min_volume_base=1000.0)

    result = analyze_markets(
        config,
        lambda: set(),
        lambda coin, price: stages.record("inference") or {"score": 1},
        {"DOGE": {"price": 1.0, "volume": 500.0, "spread_bps": 5.0}},
    )

    assert result == {}
    assert stages.stages == []


def test_spread_gate_rejects_before_inference_or_paid_call() -> None:
    stages = _StageLog()
    config = _MarketConfig(max_entry_spread_bps=50.0)

    result = analyze_markets(
        config,
        lambda: set(),
        lambda coin, price: stages.record("inference") or {"score": 1},
        {"BTC": {"price": 100.0, "volume": 5000.0, "spread_bps": 120.0}},
    )

    assert result == {}
    assert stages.stages == []


def test_a_candidate_that_passes_the_gates_reaches_inference_once() -> None:
    stages = _StageLog()
    config = _MarketConfig()

    result = analyze_markets(
        config,
        lambda: set(),
        lambda coin, price: stages.record("inference") or {"score": 1},
        {"BTC": {"price": 100.0, "volume": 5000.0, "spread_bps": 20.0}},
    )

    assert list(result) == ["BTC"]
    assert stages.stages == ["inference"]


# ---------------------------------------------------------------------------
# Stage 3: ATR gate before the model call
# ---------------------------------------------------------------------------

@dataclass
class _AnalysisConfig:
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
    min_entry_atr_pct: float = 0.0


def _ohlcv(rows: int = 60) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=rows, freq="h")
    close = 100.0 + 0.1 * np.arange(rows, dtype=float)
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


class _RecordingPredictor:
    def __init__(self, stages: _StageLog) -> None:
        self.stages = stages

    def predict(self, row_data: pd.DataFrame, confidence_threshold: float) -> dict[str, Any]:
        self.stages.record("inference")
        return {
            "decision": "kaufen",
            "confidence": 0.9,
            "proba": {"kaufen": 0.9, "halten": 0.08, "verkaufen": 0.02},
            "backend": "cpu",
        }


def _fetcher(frame: pd.DataFrame):
    def fetch(symbol: str, **_kwargs: Any) -> pd.DataFrame:
        return frame

    return fetch


def _rules(recommendation: str = "HOLD", score: int = 50):
    def rule_recommender(rsi: float, sma_short: float, sma_long: float, price: float):
        return recommendation, score

    return rule_recommender


def test_atr_gate_blocks_inference_for_a_low_volatility_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stages = _StageLog()
    monkeypatch.setattr("src.coin_analysis.calculate_atr", lambda *_a, **_k: 0.05)
    config = _AnalysisConfig(min_entry_atr_pct=0.01)

    result = analyze_coin(
        config, _fetcher(_ohlcv()), None, _RecordingPredictor(stages),
        "BTC", 100.0, rule_recommender=_rules("HOLD", 50),
    )

    assert result is not None
    assert stages.stages == []
    assert result["recommendation"] == "HOLD"
    assert result["signal_source"] == "rules"


def test_atr_gate_allows_inference_once_volatility_is_sufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stages = _StageLog()
    monkeypatch.setattr("src.coin_analysis.calculate_atr", lambda *_a, **_k: 5.0)
    config = _AnalysisConfig(min_entry_atr_pct=0.01)

    result = analyze_coin(
        config, _fetcher(_ohlcv()), None, _RecordingPredictor(stages),
        "BTC", 100.0, rule_recommender=_rules("HOLD", 50),
    )

    assert result is not None
    assert stages.stages == ["inference"]


# ---------------------------------------------------------------------------
# Stages 5-7: cost budget before the paid news veto, news veto before the order
# ---------------------------------------------------------------------------

@dataclass
class _EntryConfig:
    base_currency: str = "EUR"
    min_trade_amount: float = 10.0
    allow_partial_trades: bool = True
    reentry_cooldown_seconds: int = 3600
    max_entry_spread_bps: float = 0.0
    max_entry_cost_bps: float = 0.0
    live_taker_fee_rate: float | None = None
    live_slippage_rate: float | None = None
    kill_switch_file: str = ".trading_kill_switch"


class _EntryRecorder:
    """Records the two expensive stages of the entry path in call order."""

    def __init__(self, stages: _StageLog) -> None:
        self.stages = stages
        self.executed: list[str] = []
        self.blocked: list[dict[str, Any]] = []

    def hooks(self, news_veto=None) -> EntryHooks:
        return EntryHooks(
            get_atr=lambda coin, period=14: 5.0,
            compute_position_size=self._position_size,
            passes_momentum_filter=lambda coin_data: (True, "ok"),
            resolve_cooldown_seconds=lambda price, atr: (3600, "static"),
            in_reentry_cooldown=lambda coin, cooldown_seconds=None: (False, 0),
            execute_trade=self._execute_trade,
            log_blocked_attempts=self._log_blocked,
            news_veto=news_veto if news_veto is not None else self._news_clear,
        )

    def _position_size(self, coin, price, atr, portfolio_value, trade_amount,
                       attempt_meta):
        attempt_meta["position_size_text"] = "100.00"
        return 100.0

    def _execute_trade(self, coin, action, price, amount, **kwargs):
        self.stages.record("order")
        self.executed.append(coin)
        return True

    def _log_blocked(self, blocked_attempts):
        self.blocked = list(blocked_attempts)

    def _news_clear(self, coin: str, candidate: dict[str, Any]) -> tuple[bool, str]:
        self.stages.record("news_veto")
        return (False, "clear")


def _portfolio(cash: float = 1000.0) -> Portfolio:
    portfolio = Portfolio("EUR")
    portfolio.cash = cash
    portfolio.save_state = lambda *args, **kwargs: True  # type: ignore[method-assign]
    return portfolio


def _open(config: _EntryConfig, recorder: _EntryRecorder, candidate: dict[str, Any],
          hooks: EntryHooks | None = None) -> None:
    open_new_positions(
        config, _portfolio(), hooks or recorder.hooks(),
        {"BTC": {"price": 100.0}},
        {"BTC": {"score": 80, "recommendation": "BUY", **candidate}},
        ["BTC"], 1000.0,
        effective_trade_amount=100.0, effective_max_open_trades=3,
        can_open_positions=True, block_reason="",
    )


def test_candidate_that_passes_every_gate_reaches_the_order() -> None:
    stages = _StageLog()
    recorder = _EntryRecorder(stages)

    _open(_EntryConfig(), recorder, {"spread_bps": 20.0, "bid": 99.9, "ask": 100.1})

    assert recorder.executed == ["BTC"]
    assert stages.stages == ["news_veto", "order"]


def test_cost_budget_blocks_before_the_paid_news_call() -> None:
    stages = _StageLog()
    recorder = _EntryRecorder(stages)
    config = _EntryConfig(
        max_entry_cost_bps=40.0, live_taker_fee_rate=0.0026,
        live_slippage_rate=0.001)

    _open(config, recorder, {"spread_bps": 20.0, "bid": 99.5, "ask": 100.5})

    assert recorder.executed == []
    assert stages.stages == []
    assert recorder.blocked[0]["reason"].startswith("estimated_cost_above_limit")


def test_spread_gate_blocks_before_the_paid_news_call() -> None:
    stages = _StageLog()
    recorder = _EntryRecorder(stages)
    config = _EntryConfig(max_entry_spread_bps=50.0)

    _open(config, recorder, {"spread_bps": 120.0})

    assert recorder.executed == []
    assert stages.stages == []
    assert recorder.blocked[0]["reason"] == "spread_unavailable_or_above_limit"


def test_news_veto_blocks_the_order_it_cannot_replace() -> None:
    stages = _StageLog()
    recorder = _EntryRecorder(stages)

    def veto(coin: str, candidate: dict[str, Any]) -> tuple[bool, str]:
        stages.record("news_veto")
        return (True, "exchange announcement risk")

    _open(_EntryConfig(), recorder, {"spread_bps": 20.0},
          hooks=recorder.hooks(veto))

    assert recorder.executed == []
    assert stages.stages == ["news_veto"]
    assert recorder.blocked[0]["reason"] == "news_veto:exchange announcement risk"


def test_full_pyramid_ordering_for_one_trading_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Volume, spread and ATR pass, the model runs, then news, then the order.

    The recorded stage order is the assertion: the paid news call happens exactly
    once, after every technical gate and the local model, and never after an
    order was submitted.
    """
    stages = _StageLog()
    monkeypatch.setattr("src.coin_analysis.calculate_atr", lambda *_a, **_k: 5.0)

    analyses = analyze_markets(
        _MarketConfig(),
        lambda: set(),
        lambda coin, price: analyze_coin(
            _AnalysisConfig(min_entry_atr_pct=0.01),
            _fetcher(_ohlcv()), None, _RecordingPredictor(stages),
            coin, price, rule_recommender=_rules("BUY", 80),
        ),
        {"BTC": {"price": 100.0, "volume": 5000.0, "spread_bps": 20.0,
                 "bid": 99.95, "ask": 100.05}},
    )
    assert list(analyses) == ["BTC"]
    assert stages.stages == ["inference"]

    recorder = _EntryRecorder(stages)
    config = _EntryConfig(
        max_entry_cost_bps=200.0, live_taker_fee_rate=0.0026,
        live_slippage_rate=0.001)
    open_new_positions(
        config, _portfolio(), recorder.hooks(),
        {"BTC": {"price": 100.0}}, analyses, ["BTC"], 1000.0,
        effective_trade_amount=100.0, effective_max_open_trades=3,
        can_open_positions=True, block_reason="",
    )

    assert stages.stages == ["inference", "news_veto", "order"]
    assert recorder.executed == ["BTC"]
    assert recorder.blocked == []
