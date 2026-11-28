"""Entry gates that screen a candidate coin before the bot may open a position.

Layer-1 collaborator: every gate is a pure ``(config, coin_data) -> (bool,
reason)`` predicate, so the momentum, up-trend, down-trend-reversal, fallback
and volatility screens can be unit-tested without a ``krakenex`` interface, a
database or a model (the CI test job has no TA-Lib). The ``BotConfig`` is
injected; ``CryptoTradingBot`` keeps thin delegators with the original method
names for every function here and still owns the candidate shortlist and the
diagnostics logs in :mod:`src.entry_selection`.
"""
from __future__ import annotations

import math
from typing import Any, cast

import numpy as np


def passes_entry_momentum_filter(config: Any, coin_data: dict) -> tuple[bool, str]:
    """Checks if an entry candidate has acceptable short-term momentum."""
    if not config.entry_momentum_filter_enabled:
        return True, 'disabled'

    coin = str(coin_data.get('coin', '')).upper()
    recommendation = str(coin_data.get('recommendation', ''))
    if recommendation not in {'BUY', 'STRONG BUY', 'HOLD (Up-Trend)'}:
        return True, 'not_buy_signal'

    ret_3 = coin_data.get('ret_3')
    if ret_3 is not None and not np.isnan(ret_3):
        min_ret_3 = config.entry_min_ret_3_by_coin.get(
            coin, config.entry_min_ret_3)
        if float(ret_3) < min_ret_3:
            return False, f"ret_3_below_min ({float(ret_3):.4f} < {min_ret_3:.4f})"

    if config.entry_require_price_above_ema20:
        price = coin_data.get('price')
        ema20 = coin_data.get('ema_20')
        if (
            price is not None and ema20 is not None
            and not np.isnan(price) and not np.isnan(ema20)
            and float(price) < float(ema20)
        ):
            return False, f"price_below_ema20 ({float(price):.4f} < {float(ema20):.4f})"

    if config.entry_sharp_pump_filter_enabled:
        ret_1 = coin_data.get('ret_1')
        if (
            ret_1 is not None
            and not np.isnan(ret_1)
            and float(ret_1) > config.entry_max_ret_1
        ):
            return False, f"sharp_pump_ret_1 ({float(ret_1):.4f} > {config.entry_max_ret_1:.4f})"

        if (
            ret_3 is not None
            and not np.isnan(ret_3)
            and float(ret_3) > config.entry_max_ret_3
        ):
            return False, f"sharp_pump_ret_3 ({float(ret_3):.4f} > {config.entry_max_ret_3:.4f})"

    return True, 'ok'


def passes_uptrend_entry_filter(config: Any, coin_data: dict) -> tuple[bool, str]:
    """Allows up-trend fallback entries only when trend and tabular quality are not too weak."""
    recommendation = str(coin_data.get('recommendation', ''))
    if recommendation != 'HOLD (Up-Trend)':
        return True, 'not_uptrend'

    if not config.uptrend_entry_gate_enabled:
        return True, 'uptrend_gate_disabled'

    coin = str(coin_data.get('coin', '')).upper()
    max_rsi = config.uptrend_entry_max_rsi_by_coin.get(
        coin, config.uptrend_entry_max_rsi)
    min_buy_proba = config.uptrend_entry_min_buy_proba_by_coin.get(
        coin, config.uptrend_entry_min_buy_proba)
    max_sell_proba = config.uptrend_entry_max_sell_proba_by_coin.get(
        coin, config.uptrend_entry_max_sell_proba)
    min_proba_edge = config.uptrend_entry_min_proba_edge_by_coin.get(
        coin, config.uptrend_entry_min_proba_edge)

    rsi = coin_data.get('rsi')
    if rsi is None or np.isnan(rsi):
        return False, 'missing_rsi'
    if float(rsi) > max_rsi:
        return False, f"rsi_above_uptrend_max ({float(rsi):.2f} > {max_rsi:.2f})"

    buy_proba = coin_data.get('tabular_buy_proba')
    sell_proba = coin_data.get('tabular_sell_proba')
    if buy_proba is None or np.isnan(buy_proba):
        return False, 'missing_buy_proba'
    if sell_proba is None or np.isnan(sell_proba):
        return False, 'missing_sell_proba'

    buy_proba = float(buy_proba)
    sell_proba = float(sell_proba)

    if buy_proba < min_buy_proba:
        return False, f"buy_proba_below_uptrend_min ({buy_proba:.3f} < {min_buy_proba:.3f})"
    if sell_proba > max_sell_proba:
        return False, f"sell_proba_above_uptrend_max ({sell_proba:.3f} > {max_sell_proba:.3f})"

    proba_edge = buy_proba - sell_proba
    if proba_edge < min_proba_edge:
        return False, f"proba_edge_below_uptrend_min ({proba_edge:.3f} < {min_proba_edge:.3f})"

    return True, 'ok'


def passes_fallback_entry_filter(config: Any, coin: str, coin_data: dict) -> tuple[bool, str]:
    """Applies fallback-only entry checks after the directional filters passed."""
    rsi = coin_data.get('rsi')
    if rsi is None or np.isnan(rsi):
        return False, 'missing_rsi'

    fallback_max_rsi = config.fallback_max_rsi_by_coin.get(
        str(coin).upper(), config.fallback_max_rsi)
    if float(rsi) > fallback_max_rsi:
        return False, f"rsi_above_fallback_max ({float(rsi):.2f} > {fallback_max_rsi:.2f})"

    return True, 'ok'


def entry_volatility_guard(config: Any, coin_data: dict) -> tuple[bool, str]:
    """Screens entries by ATR(14)/price, a volatility proxy rather than a return estimate."""
    minimum = config.min_entry_atr_pct
    if minimum <= 0:
        return True, 'disabled'

    try:
        atr_pct = float(cast(Any, coin_data.get('atr_pct')))
    except (TypeError, ValueError):
        atr_pct = float('nan')
    if not math.isfinite(atr_pct):
        return False, 'atr_pct_unavailable'
    if atr_pct < minimum:
        return False, f'atr_pct_below_minimum ({atr_pct:.5f} < {minimum:.5f})'
    return True, 'ok'


def passes_downtrend_reversal_filter(config: Any, coin_data: dict) -> tuple[bool, str]:  # noqa: C901
    """Allows down-trend fallback entries only for oversold coins with improving tabular odds."""
    recommendation = str(coin_data.get('recommendation', ''))
    if recommendation != 'HOLD (Down-Trend)':
        return True, 'not_downtrend'

    coin = str(coin_data.get('coin', '')).upper()
    max_rsi = config.downtrend_reversal_max_rsi_by_coin.get(
        coin, config.downtrend_reversal_max_rsi)
    min_buy_proba = config.downtrend_reversal_min_buy_proba_by_coin.get(
        coin, config.downtrend_reversal_min_buy_proba)
    max_sell_proba = config.downtrend_reversal_max_sell_proba_by_coin.get(
        coin, config.downtrend_reversal_max_sell_proba)
    min_proba_edge = config.downtrend_reversal_min_proba_edge_by_coin.get(
        coin, config.downtrend_reversal_min_proba_edge)

    if coin and config.downtrend_reversal_allowed_coins and coin not in config.downtrend_reversal_allowed_coins:
        return False, f"coin_not_allowed_for_reversal ({coin or 'unknown'})"

    if not config.downtrend_reversal_entry_enabled:
        return False, 'downtrend_reversal_disabled'

    rsi = coin_data.get('rsi')
    if rsi is None or np.isnan(rsi):
        return False, 'missing_rsi'
    if float(rsi) > max_rsi:
        return False, f"rsi_above_reversal_max ({float(rsi):.2f} > {max_rsi:.2f})"

    buy_proba = coin_data.get('tabular_buy_proba')
    sell_proba = coin_data.get('tabular_sell_proba')
    if buy_proba is None or sell_proba is None:
        return False, 'missing_tabular_proba'

    buy_proba = float(buy_proba)
    sell_proba = float(sell_proba)
    if buy_proba < min_buy_proba:
        return False, f"buy_proba_below_min ({buy_proba:.3f} < {min_buy_proba:.3f})"
    if sell_proba > max_sell_proba:
        return False, f"sell_proba_above_max ({sell_proba:.3f} > {max_sell_proba:.3f})"
    if (buy_proba - sell_proba) < min_proba_edge:
        return False, (
            f"buy_sell_edge_too_small ({buy_proba - sell_proba:.3f} < "
            f"{min_proba_edge:.3f})"
        )

    ret_1 = coin_data.get('ret_1')
    if ret_1 is None or np.isnan(ret_1):
        return False, 'missing_ret_1'
    if float(ret_1) < config.downtrend_reversal_min_ret_1:
        return False, (
            f"ret_1_below_reversal_min ({float(ret_1):.4f} < "
            f"{config.downtrend_reversal_min_ret_1:.4f})"
        )

    ret_3 = coin_data.get('ret_3')
    if ret_3 is None or np.isnan(ret_3):
        return False, 'missing_ret_3'
    if float(ret_3) < config.downtrend_reversal_min_ret_3:
        return False, (
            f"ret_3_below_reversal_min ({float(ret_3):.4f} < "
            f"{config.downtrend_reversal_min_ret_3:.4f})"
        )

    macd_hist = coin_data.get('macd_hist')
    if macd_hist is None or np.isnan(macd_hist):
        return False, 'missing_macd_hist'
    if float(macd_hist) < config.downtrend_reversal_min_macd_hist:
        return False, (
            f"macd_hist_below_reversal_min ({float(macd_hist):.4f} < "
            f"{config.downtrend_reversal_min_macd_hist:.4f})"
        )

    return True, 'downtrend_reversal_ok'
