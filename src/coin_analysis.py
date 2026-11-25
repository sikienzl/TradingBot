"""Per-coin market analysis: indicators, rule signal and model overlays.

Layer-1 collaborator extracted from :class:`src.trading_bot.CryptoTradingBot`.
Every function is a pure transformation over an OHLCV frame plus injected
collaborators - the OHLCV fetcher, the optional ML predictor, the optional
tabular predictor and the rule recommender - so the whole analysis path can be
unit-tested without an exchange, a database or a model (the CI test job has no
TA-Lib and no torch). ``CryptoTradingBot`` keeps thin delegators with the
original method names and still owns the config, the predictors and the data
fetching. ``rule_recommender`` is injected rather than imported so that
tests can keep patching ``src.trading_bot.get_rule_recommendation``.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

from src.indicator_service import (
    calculate_atr,
    calculate_macd,
    calculate_rsi,
    calculate_sma,
)

logger = logging.getLogger(__name__)

def recommendation_bias(recommendation: str) -> int:
    normalized = (recommendation or '').strip().upper()
    if normalized in {'STRONG BUY', 'BUY', 'HOLD (UP-TREND)'}:
        return 1
    if normalized in {'SELL', 'WEAK SELL', 'HOLD (DOWN-TREND)'}:
        return -1
    return 0


def should_apply_tabular_signal(
    config: Any,
    rule_recommendation: str,
    rule_score: int,
    tab_decision: str,
    tab_confidence: float,
) -> tuple[bool, str]:
    if tab_decision == 'kaufen' and not config.tabular_allow_buy_entries:
        return False, 'buy_entries_disabled'

    required_confidence = config.tabular_buy_min_confidence
    if tab_decision == 'verkaufen':
        required_confidence = config.tabular_min_confidence

    if tab_confidence < required_confidence:
        return False, 'below_min_confidence'

    if not config.tabular_source_gate_enabled:
        return True, 'gate_disabled'

    tab_bias = 1 if tab_decision == 'kaufen' else - \
        1 if tab_decision == 'verkaufen' else 0
    rule_bias = recommendation_bias(rule_recommendation)

    if tab_bias == 0:
        return False, 'neutral_tabular_signal'

    if tab_bias == rule_bias:
        return True, 'rule_confirmed'

    rule_strength = abs(float(rule_score) - 50.0) / 50.0
    advantage = tab_confidence - rule_strength
    if (
        tab_confidence >= config.tabular_override_min_confidence
        and advantage >= config.tabular_override_margin
    ):
        return True, 'strong_override'

    return False, 'gated_by_rules'


def build_tabular_features(
    ohlcv_df,
    close,
    current_price: float,
    rsi: float,
    sma_short: float,
    sma_long: float,
    atr_14: float,
) -> tuple[pd.DataFrame, float]:
    """Build the tabular model feature row and return it with the MACD histogram."""
    n = len(ohlcv_df)
    latest = ohlcv_df.iloc[-1]

    # MACD + signal + histogram
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    macd_signal_line = macd_line.ewm(span=9, adjust=False).mean()
    _macd = float(macd_line.iloc[-1])
    _macd_sig = float(macd_signal_line.iloc[-1])
    _macd_hist = _macd - _macd_sig
    macd_hist = _macd_hist

    # EMAs
    _ema20 = float(
        close.ewm(span=20, adjust=False).mean().iloc[-1])
    _ema50 = float(
        close.ewm(span=50, adjust=False).mean().iloc[-1])
    _ema200 = float(
        close.ewm(span=200, adjust=False).mean().iloc[-1])

    # Stochastic %K/%D
    _stoch_k, _stoch_d = 50.0, 50.0
    if n >= 17:
        low14 = ohlcv_df['low'].rolling(14).min()
        high14 = ohlcv_df['high'].rolling(14).max()
        _raw_k = 100.0 * (close - low14) / (high14 - low14 + 1e-12)
        _stoch_k = float(_raw_k.rolling(3).mean().iloc[-1])
        _stoch_d = float(_raw_k.rolling(
            3).mean().rolling(3).mean().iloc[-1])

    # CCI 20
    _cci = 0.0
    if n >= 20:
        tp = (ohlcv_df['high'] + ohlcv_df['low'] + close) / 3.0
        tp_sma = tp.rolling(20).mean()
        tp_mad = tp.rolling(20).apply(
            lambda x: np.mean(np.abs(x - x.mean())), raw=True)
        _cci = float(
            ((tp - tp_sma) / (0.015 * tp_mad + 1e-12)).iloc[-1])

    # OBV
    if n >= 2:
        _direction = np.sign(close.diff().fillna(0))
        _obv = float(
            (_direction * ohlcv_df['volume']).cumsum().iloc[-1])
    else:
        _obv = 0.0

    # Bollinger Bands (20, 2)
    _bb_mid = float(close.rolling(20).mean(
    ).iloc[-1]) if n >= 20 else float(current_price)
    _bb_std = float(close.rolling(
        20).std().iloc[-1]) if n >= 20 else 0.0
    _bb_upper = _bb_mid + 2.0 * _bb_std
    _bb_lower = _bb_mid - 2.0 * _bb_std

    # Return features
    _ret1 = float(close.pct_change(1).iloc[-1]) if n >= 2 else 0.0
    _ret3 = float(close.pct_change(3).iloc[-1]) if n >= 4 else 0.0
    _ret6 = float(close.pct_change(6).iloc[-1]) if n >= 7 else 0.0
    _vol6 = float(close.pct_change().rolling(
        6).std().iloc[-1]) if n >= 7 else 0.0

    row_data = pd.DataFrame([{
        'rsi': float(rsi) if not np.isnan(rsi) else 50.0,
        'macd': _macd,
        'macd_signal': _macd_sig,
        'macd_hist': _macd_hist,
        'sma_50': float(sma_short) if not np.isnan(sma_short) else float(current_price),
        'sma_200': float(sma_long) if not np.isnan(sma_long) else float(current_price),
        'ema_20': _ema20,
        'ema_50': _ema50,
        'ema_200': _ema200,
        'atr_14': atr_14 if math.isfinite(atr_14) else 0.0,
        'stoch_k': _stoch_k,
        'stoch_d': _stoch_d,
        'cci_20': _cci,
        'obv': _obv,
        'bb_upper': _bb_upper,
        'bb_middle': _bb_mid,
        'bb_lower': _bb_lower,
        'volume': float(latest['volume']) if 'volume' in latest else 0.0,
        'ret_1': _ret1,
        'ret_3': _ret3,
        'ret_6': _ret6,
        'vol_6': _vol6,
    }])

    return row_data, macd_hist


def apply_ml_signal(
    config: Any,
    ml_predictor: Any,
    coin: str,
    ohlcv_df,
    current_price: float,
    rsi: float,
    recommendation: str,
    score: int,
    signal_source: str,
    signal_confidence,
) -> tuple[str, int, str, Any]:
    """Run the optional ML predictor and weave its signal into the rule result."""
    if ml_predictor is None:
        return recommendation, score, signal_source, signal_confidence
    try:
        macd = calculate_macd(ohlcv_df['close'])
        row_data = pd.DataFrame([{
            'coin': coin,
            'timestamp': int(ohlcv_df['timestamp'].iloc[-1].timestamp() * 1000),
            'open': float(ohlcv_df['open'].iloc[-1]),
            'high': float(ohlcv_df['high'].iloc[-1]),
            'low': float(ohlcv_df['low'].iloc[-1]),
            'close': float(current_price),
            'volume': float(ohlcv_df['volume'].iloc[-1]),
            'rsi': float(rsi) if not np.isnan(rsi) else 50.0,
            'macd': float(macd) if not np.isnan(macd) else 0.0,
        }])
        ml_result = ml_predictor.predict(row_data)
        ml_decision = ml_result['decision']
        ml_confidence = ml_result['confidence']
        logger.info(
            f"  🤖 ML for {coin}: {ml_decision.upper()} "
            f"(Confidence: {ml_confidence*100:.0f}%, "
            f"Votes: {ml_result.get('llm_votes', {})}, "
            f"Rule: {ml_result.get('rule', '-')})")

        # Ensemble: weave ML signal into score/recommendation
        if ml_confidence >= config.ml_min_confidence:
            signal_source = 'llm'
            signal_confidence = ml_confidence
            if ml_decision == 'kaufen':
                if recommendation in ['HOLD', 'HOLD (Up-Trend)', 'HOLD (Down-Trend)']:
                    recommendation = 'BUY'
                    score = max(score, 70)
                elif recommendation in ['BUY', 'STRONG BUY']:
                    score = min(score + int(ml_confidence * 15), 95)
            elif ml_decision == 'verkaufen':
                if recommendation in ['HOLD', 'HOLD (Up-Trend)', 'BUY', 'STRONG BUY']:
                    recommendation = 'WEAK SELL'
                    score = min(score, 30)
                elif recommendation in ['SELL', 'WEAK SELL']:
                    score = max(score - int(ml_confidence * 10), 5)
    except (IndexError, KeyError, OSError, RuntimeError, TypeError, ValueError) as e:
        logger.warning(f"ML prediction for {coin} failed: {e}")
    return recommendation, score, signal_source, signal_confidence


def analyze_coin(
    config: Any,
    fetch_ohlcv: Callable[..., pd.DataFrame | None],
    ml_predictor: Any,
    tabular_predictor: Any,
    coin: str,
    current_price: float,
    *,
    rule_recommender: Callable[..., tuple[str, int]],
) -> dict | None:
    """Performs a detailed analysis for a single coin."""
    symbol = f"{coin}/{config.base_currency}"
    ohlcv_df = fetch_ohlcv(
        symbol, timeframe=config.ohlcv_timeframe, limit=config.ohlcv_limit)

    # Less tolerance for missing data
    if ohlcv_df is None or len(ohlcv_df) < config.ohlcv_limit - 5:
        logger.warning(
            f"Insufficient historical data for {coin} for analysis. Skipping.")
        return None

    # Calculate indicators
    rsi = calculate_rsi(ohlcv_df['close'], config.rsi_period)
    sma_short = calculate_sma(
        ohlcv_df['close'], config.sma_period_short)
    sma_long = calculate_sma(
        ohlcv_df['close'], config.sma_period_long)
    close = ohlcv_df['close']
    ema_20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    ret_1 = float(close.pct_change(
        1).iloc[-1]) if len(ohlcv_df) >= 2 else 0.0
    ret_3 = float(close.pct_change(
        3).iloc[-1]) if len(ohlcv_df) >= 4 else 0.0

    # Simple strategy based on indicators
    recommendation, score = rule_recommender(
        rsi, sma_short, sma_long, current_price
    )
    signal_source = 'rules'
    signal_confidence = None
    tab_buy_proba = None
    tab_hold_proba = None
    tab_sell_proba = None
    tabular_sell_veto = False
    macd_hist = None
    atr_14 = float(calculate_atr(ohlcv_df, 14))
    atr_pct = (
        atr_14 / current_price
        if math.isfinite(atr_14) and math.isfinite(current_price) and current_price > 0
        else None
    )
    minimum_atr_pct = float(getattr(config, 'min_entry_atr_pct', 0.0))
    allow_optional_models = (
        minimum_atr_pct <= 0
        or (atr_pct is not None and atr_pct >= minimum_atr_pct)
    )
    if not allow_optional_models:
        logger.info(
            "Skipping optional inference for %s before model calls: ATR gate not met",
            coin,
        )

    rule_recommendation = recommendation
    rule_score = score

    # ML model analysis (optional, only if loaded and data is valid)
    if allow_optional_models:
        recommendation, score, signal_source, signal_confidence = (
            apply_ml_signal(
                config, ml_predictor,
                coin, ohlcv_df, current_price, rsi, recommendation, score,
                signal_source, signal_confidence,
            )
        )

    # Fast tabular model analysis (optional)
    if tabular_predictor is not None and allow_optional_models:
        try:
            row_data, macd_hist = build_tabular_features(
                ohlcv_df, close, current_price, rsi, sma_short, sma_long,
                atr_14,
            )
            tab_result = tabular_predictor.predict(
                row_data, confidence_threshold=config.tabular_min_confidence)
            model_backend = tab_result.get(
                'backend', config.tabular_model_backend)
            tab_decision = tab_result.get('decision', 'halten')
            tab_confidence = float(tab_result.get('confidence', 0.0))
            tab_proba = tab_result.get('proba', {})
            tab_sell_proba = float(tab_proba.get('verkaufen', 0.0))
            tab_hold_proba = float(tab_proba.get('halten', 0.0))
            tab_buy_proba = float(tab_proba.get('kaufen', 0.0))
            model_name = str(model_backend).upper()
            logger.info(
                f"  📊 {model_name} for {coin}: {tab_decision.upper()} "
                f"(Confidence: {tab_confidence*100:.0f}%, Proba: {tab_proba})")

            # A decisive tabular SELL is a separate veto; HOLD or low-confidence
            # predictions are not treated as sell evidence.
            if (
                config.tabular_block_on_sell
                and tab_decision == 'verkaufen'
                and tab_confidence >= max(
                    config.tabular_min_confidence,
                    config.tabular_sell_block_min_proba,
                )
                and tab_sell_proba >= config.tabular_sell_block_min_proba
                and recommendation in ['HOLD', 'HOLD (Up-Trend)', 'HOLD (Down-Trend)', 'BUY', 'STRONG BUY']
            ):
                tabular_sell_veto = True
                recommendation = 'WEAK SELL'
                score = min(score, 25)
                signal_source = model_backend
                signal_confidence = tab_sell_proba
                logger.info(
                    f"  🛡️ {model_name} sell-filter active for {coin}: "
                    f"P(sell)={tab_sell_proba*100:.0f}% >= "
                    f"{config.tabular_sell_block_min_proba*100:.0f}%")

            should_apply, gate_reason = should_apply_tabular_signal(
                config,
                rule_recommendation=rule_recommendation,
                rule_score=rule_score,
                tab_decision=tab_decision,
                tab_confidence=tab_confidence,
            )
            if (
                config.tabular_block_on_sell
                and tab_decision == 'verkaufen'
                and rule_recommendation in ['BUY', 'STRONG BUY']
                and not tabular_sell_veto
            ):
                should_apply = False
                gate_reason = 'sell_below_entry_veto_threshold'
                logger.info(
                    "  🛡️ %s sell signal below entry-veto threshold for %s; "
                    "preserving rule BUY (P(sell)=%.0f%%, threshold=%.0f%%)",
                    model_name,
                    coin,
                    tab_sell_proba * 100,
                    config.tabular_sell_block_min_proba * 100,
                )
            required_confidence = config.tabular_buy_min_confidence if tab_decision == 'kaufen' else config.tabular_min_confidence
            if tab_confidence >= required_confidence and not should_apply:
                logger.info(
                    f"  🚧 {model_name} gated for {coin}: {gate_reason} "
                    f"(rule={rule_recommendation}, rule_score={rule_score}, "
                    f"tab_decision={tab_decision}, tab_confidence={tab_confidence*100:.0f}%)"
                )

            if should_apply:
                signal_source = model_backend
                signal_confidence = tab_confidence
                if tab_decision == 'kaufen':
                    if recommendation in ['HOLD', 'HOLD (Up-Trend)', 'HOLD (Down-Trend)']:
                        recommendation = 'BUY'
                        score = max(score, 72)
                    elif recommendation in ['BUY', 'STRONG BUY']:
                        score = min(score + int(tab_confidence * 12), 97)
                elif tab_decision == 'verkaufen':
                    if recommendation in ['HOLD', 'HOLD (Up-Trend)', 'BUY', 'STRONG BUY']:
                        recommendation = 'WEAK SELL'
                        score = min(score, 28)
                    elif recommendation in ['SELL', 'WEAK SELL']:
                        score = max(score - int(tab_confidence * 8), 3)
        except (IndexError, KeyError, OSError, RuntimeError, TypeError, ValueError) as e:
            if config.tabular_model_backend == 'hailo':
                logger.error(
                    "Tabular inference unavailable for %s; preserving the existing signal: %s",
                    coin,
                    e,
                )
            else:
                logger.warning(
                    f"CatBoost prediction for {coin} failed: {e}")

    return {
        'rsi': rsi,
        'sma_short': sma_short,
        'sma_long': sma_long,
        'ema_20': ema_20,
        'ret_1': ret_1,
        'ret_3': ret_3,
        'recommendation': recommendation,
        'score': score,
        'rule_recommendation': rule_recommendation,
        'rule_score': rule_score,
        'signal_source': signal_source,
        'signal_confidence': signal_confidence,
        'tabular_buy_proba': tab_buy_proba,
        'tabular_hold_proba': tab_hold_proba,
        'tabular_sell_proba': tab_sell_proba,
        'tabular_sell_veto': tabular_sell_veto,
        'atr_14': atr_14 if math.isfinite(atr_14) else None,
        'atr_pct': atr_pct,
        'macd_hist': macd_hist,
    }
