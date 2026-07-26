import json
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from src.trading_bot import BotConfig, CryptoTradingBot


def _make_test_bot(monkeypatch):
    monkeypatch.setenv("SIMULATE_DATA", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("USE_TABULAR_MODEL", "false")
    monkeypatch.setenv("USE_ML_MODEL", "false")
    monkeypatch.setenv("PERFORMANCE_LOG_ENABLED", "false")

    config = BotConfig()
    bot = CryptoTradingBot(config)
    bot.portfolio.save_state = lambda filepath=None: True
    return bot


def test_daily_loss_guard_blocks_new_entries(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.max_daily_loss_pct = 1.0
    bot.daily_anchor_value = 100.0

    can_open, reason = bot._can_open_new_positions(98.5)

    assert can_open is False
    assert "Daily-Loss-Limit" in reason


def test_buy_limit_per_hour_blocks(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.max_buys_per_hour = 2

    now = datetime.now(timezone.utc)
    bot.buy_timestamps_utc = [
        now - timedelta(minutes=10), now - timedelta(minutes=1)]

    can_open, reason = bot._can_open_new_positions(100.0)

    assert can_open is False
    assert "BUY-Limit pro Stunde" in reason


def test_loss_streak_sets_pause(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.loss_streak_pause_threshold = 2
    bot.config.loss_streak_pause_seconds = 300

    entry_trade = {
        "buy_price": 100.0,
        "amount_coin": 1.0,
        "timestamp": datetime.now(),
        "signal_source": "rules",
    }

    bot._record_close_performance(
        entry_trade, sell_price=99.0, sell_amount=1.0)
    bot._record_close_performance(
        entry_trade, sell_price=98.0, sell_amount=1.0)

    assert bot.buy_pause_until_utc is not None
    can_open, reason = bot._can_open_new_positions(100.0)
    assert can_open is False
    assert "BUY pause" in reason


def test_tabular_gate_allows_same_direction_confirmation(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.tabular_source_gate_enabled = True
    bot.config.tabular_min_confidence = 0.45
    bot.config.tabular_buy_min_confidence = 0.45
    bot.config.tabular_allow_buy_entries = True

    allowed, gate_reason = bot._should_apply_tabular_signal(
        rule_recommendation="HOLD (Up-Trend)",
        rule_score=60,
        tab_decision="kaufen",
        tab_confidence=0.48,
    )

    assert allowed is True
    assert gate_reason == "rule_confirmed"


def test_tabular_gate_blocks_weak_contradiction(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.tabular_source_gate_enabled = True
    bot.config.tabular_min_confidence = 0.45
    bot.config.tabular_buy_min_confidence = 0.45
    bot.config.tabular_override_min_confidence = 0.60
    bot.config.tabular_override_margin = 0.15

    allowed, gate_reason = bot._should_apply_tabular_signal(
        rule_recommendation="HOLD (Up-Trend)",
        rule_score=60,
        tab_decision="verkaufen",
        tab_confidence=0.52,
    )

    assert allowed is False
    assert gate_reason == "gated_by_rules"


def test_tabular_gate_allows_strong_override(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.tabular_source_gate_enabled = True
    bot.config.tabular_min_confidence = 0.45
    bot.config.tabular_buy_min_confidence = 0.45
    bot.config.tabular_override_min_confidence = 0.60
    bot.config.tabular_override_margin = 0.15

    allowed, gate_reason = bot._should_apply_tabular_signal(
        rule_recommendation="HOLD (Up-Trend)",
        rule_score=60,
        tab_decision="verkaufen",
        tab_confidence=0.85,
    )

    assert allowed is True
    assert gate_reason == "strong_override"


def test_tabular_gate_uses_stricter_buy_threshold(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.tabular_source_gate_enabled = False
    bot.config.tabular_min_confidence = 0.45
    bot.config.tabular_buy_min_confidence = 0.55
    bot.config.tabular_allow_buy_entries = True

    allowed, gate_reason = bot._should_apply_tabular_signal(
        rule_recommendation="HOLD (Up-Trend)",
        rule_score=60,
        tab_decision="kaufen",
        tab_confidence=0.50,
    )

    assert allowed is False
    assert gate_reason == "below_min_confidence"


def test_tabular_gate_keeps_sell_threshold_independent(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.tabular_source_gate_enabled = False
    bot.config.tabular_min_confidence = 0.45
    bot.config.tabular_buy_min_confidence = 0.55

    allowed, gate_reason = bot._should_apply_tabular_signal(
        rule_recommendation="HOLD (Up-Trend)",
        rule_score=60,
        tab_decision="verkaufen",
        tab_confidence=0.50,
    )

    assert allowed is True
    assert gate_reason == "gate_disabled"


def test_tabular_gate_blocks_buy_entries_when_disabled(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.tabular_allow_buy_entries = False

    allowed, gate_reason = bot._should_apply_tabular_signal(
        rule_recommendation="HOLD (Up-Trend)",
        rule_score=60,
        tab_decision="kaufen",
        tab_confidence=0.90,
    )

    assert allowed is False
    assert gate_reason == "buy_entries_disabled"


def test_analyze_coin_returns_macd_hist_for_downtrend_filters(monkeypatch):
    bot = _make_test_bot(monkeypatch)

    class FakeTabularPredictor:
        def predict(self, row_data, confidence_threshold=0.0):
            return {
                "decision": "halten",
                "confidence": 0.40,
                "proba": {
                    "verkaufen": 0.38,
                    "halten": 0.36,
                    "kaufen": 0.26,
                },
            }

    timestamps = pd.date_range("2026-01-01", periods=60, freq="h", tz="UTC")
    closes = [100 - idx * 0.4 for idx in range(60)]
    ohlcv_df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [price + 0.2 for price in closes],
            "high": [price + 0.5 for price in closes],
            "low": [price - 0.5 for price in closes],
            "close": closes,
            "volume": [1000 + idx * 10 for idx in range(60)],
        }
    )

    bot.tabular_predictor = FakeTabularPredictor()
    bot._fetch_ohlcv_data = lambda *args, **kwargs: ohlcv_df

    analysis = bot._analyze_coin("SUI", current_price=float(closes[-1]))

    assert analysis is not None
    assert analysis["macd_hist"] is not None
    assert isinstance(analysis["macd_hist"], float)


def test_effective_stop_loss_raises_with_trailing_peak(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.atr_stop_mult = 1.5
    bot.config.trailing_stop_enabled = True
    bot.config.trailing_stop_atr_mult = 1.0
    bot.config.break_even_enabled = False

    trade_info = {
        "buy_price": 100.0,
        "peak_price": 100.0,
    }
    stop = bot._effective_stop_loss_level(
        trade_info, current_price=106.0, atr=2.0)

    assert stop == 104.0
    assert trade_info["peak_price"] == 106.0


def test_portfolio_load_state_parses_partial_take_profit_timestamp(monkeypatch, tmp_path):
    state_file = tmp_path / "portfolio_state.json"
    state_file.write_text(
        json.dumps(
            {
                "cash": 48.39,
                "holdings": {"ETH": 0.001},
                "open_trades": {
                    "ETH": {
                        "buy_price": 2307.3257,
                        "amount_coin": 0.001,
                        "amount_base": 2.3073,
                        "timestamp": "2026-07-26T19:58:48+00:00",
                        "peak_price": 2537.4996,
                        "partial_tp_taken": True,
                        "partial_tp_timestamp": "2026-07-26T19:57:48+00:00",
                        "signal_source": "rules",
                        "signal_confidence": None,
                        "recommendation": "BUY"
                    }
                },
                "base_currency": "EUR",
                "initial_portfolio_value": 51.14,
                "timestamp": "2026-07-26T19:58:48+00:00"
            }
        )
    )

    portfolio = _make_test_bot(monkeypatch).portfolio

    assert portfolio.load_state(str(state_file)) is True
    assert isinstance(portfolio.open_trades["ETH"]["timestamp"], datetime)
    assert isinstance(
        portfolio.open_trades["ETH"]["partial_tp_timestamp"], datetime
    )


def test_effective_stop_loss_break_even_protection(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.atr_stop_mult = 1.5
    bot.config.trailing_stop_enabled = False
    bot.config.break_even_enabled = True
    bot.config.break_even_trigger_pct = 1.0
    bot.config.break_even_buffer_pct = 0.2

    trade_info = {
        "buy_price": 100.0,
        "peak_price": 101.5,
    }
    stop = bot._effective_stop_loss_level(
        trade_info, current_price=101.5, atr=4.0)

    # Base ATR stop would be 94.0, but break-even protection lifts it.
    assert stop == 100.2


def test_entry_momentum_filter_allows_valid_buy(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.entry_momentum_filter_enabled = True
    bot.config.entry_min_ret_3 = -0.01
    bot.config.entry_require_price_above_ema20 = True

    passes, reason = bot._passes_entry_momentum_filter({
        "recommendation": "BUY",
        "ret_3": 0.02,
        "price": 105.0,
        "ema_20": 100.0,
    })

    assert passes is True
    assert reason == "ok"


def test_entry_momentum_filter_blocks_weak_ret3(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.entry_momentum_filter_enabled = True
    bot.config.entry_min_ret_3 = -0.01
    bot.config.entry_min_ret_3_by_coin = {}
    bot.config.entry_require_price_above_ema20 = False

    passes, reason = bot._passes_entry_momentum_filter({
        "coin": "VVV",
        "recommendation": "BUY",
        "ret_3": -0.03,
        "price": 105.0,
        "ema_20": 100.0,
    })

    assert passes is False
    assert reason.startswith("ret_3_below_min")


def test_entry_momentum_filter_allows_coin_specific_ret3_override(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.entry_momentum_filter_enabled = True
    bot.config.entry_min_ret_3 = -0.01
    bot.config.entry_min_ret_3_by_coin = {"VVV": -0.04}
    bot.config.entry_require_price_above_ema20 = False

    passes, reason = bot._passes_entry_momentum_filter({
        "coin": "VVV",
        "recommendation": "BUY",
        "ret_3": -0.03,
        "price": 105.0,
        "ema_20": 100.0,
    })

    assert passes is True
    assert reason == "ok"


def test_entry_momentum_filter_keeps_global_ret3_for_other_coins(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.entry_momentum_filter_enabled = True
    bot.config.entry_min_ret_3 = -0.01
    bot.config.entry_min_ret_3_by_coin = {"VVV": -0.04}
    bot.config.entry_require_price_above_ema20 = False

    passes, reason = bot._passes_entry_momentum_filter({
        "coin": "ICP",
        "recommendation": "BUY",
        "ret_3": -0.03,
        "price": 105.0,
        "ema_20": 100.0,
    })

    assert passes is False
    assert reason == "ret_3_below_min (-0.0300 < -0.0100)"


def test_entry_momentum_filter_blocks_price_below_ema20(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.entry_momentum_filter_enabled = True
    bot.config.entry_min_ret_3 = -0.05
    bot.config.entry_require_price_above_ema20 = True

    passes, reason = bot._passes_entry_momentum_filter({
        "recommendation": "BUY",
        "ret_3": 0.01,
        "price": 99.0,
        "ema_20": 100.0,
    })

    assert passes is False
    assert reason.startswith("price_below_ema20")


def test_entry_momentum_filter_blocks_sharp_pump_ret1(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.entry_momentum_filter_enabled = True
    bot.config.entry_require_price_above_ema20 = False
    bot.config.entry_sharp_pump_filter_enabled = True
    bot.config.entry_max_ret_1 = 0.04
    bot.config.entry_max_ret_3 = 0.20

    passes, reason = bot._passes_entry_momentum_filter({
        "recommendation": "BUY",
        "ret_1": 0.06,
        "ret_3": 0.03,
    })

    assert passes is False
    assert reason.startswith("sharp_pump_ret_1")


def test_simulated_market_data_is_deterministic_within_iteration(monkeypatch):
    bot = _make_test_bot(monkeypatch)

    first = bot._get_market_data()
    second = bot._get_market_data()

    assert first == second


def test_simulated_market_data_matches_latest_simulated_ohlcv(monkeypatch):
    bot = _make_test_bot(monkeypatch)

    market_data = bot._get_market_data()
    btc_df = bot._fetch_ohlcv_data("BTC/EUR", timeframe="1h", limit=100)

    assert market_data["BTC"]["price"] == float(btc_df["close"].iloc[-1])
    assert market_data["BTC"]["volume"] > 0
    assert (btc_df["high"] >= btc_df[["open", "close"]].max(axis=1)).all()
    assert (btc_df["low"] <= btc_df[["open", "close"]].min(axis=1)).all()


def test_simulated_latest_close_is_limit_independent(monkeypatch):
    bot = _make_test_bot(monkeypatch)

    short_df = bot._fetch_ohlcv_data("BTC/EUR", timeframe="1h", limit=8)
    long_df = bot._fetch_ohlcv_data("BTC/EUR", timeframe="1h", limit=100)

    assert float(short_df["close"].iloc[-1]
                 ) == float(long_df["close"].iloc[-1])


def test_simulated_market_data_changes_between_iterations(monkeypatch):
    bot = _make_test_bot(monkeypatch)

    first_btc_price = bot._get_market_data()["BTC"]["price"]
    bot.iteration += 1
    second_btc_price = bot._get_market_data()["BTC"]["price"]

    assert first_btc_price != second_btc_price


def test_simulated_uptrend_regime_rises_over_window(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.simulation_regime = "uptrend"

    btc_df = bot._fetch_ohlcv_data("BTC/EUR", timeframe="1h", limit=60)

    assert float(btc_df["close"].iloc[-1]) > float(btc_df["close"].iloc[0])


def test_simulated_crash_regime_drops_over_window(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.simulation_regime = "crash"

    btc_df = bot._fetch_ohlcv_data("BTC/EUR", timeframe="1h", limit=60)

    assert float(btc_df["close"].iloc[-1]) < float(btc_df["close"].iloc[0])
    assert float(btc_df["volume"].iloc[-1]) > 0


def test_uptrend_entry_filter_blocks_overbought_rules_trade(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.uptrend_entry_gate_enabled = True
    bot.config.uptrend_entry_max_rsi = 72.0

    passes, reason = bot._passes_uptrend_entry_filter({
        "recommendation": "HOLD (Up-Trend)",
        "signal_source": "catboost",
        "rsi": 74.0,
        "tabular_buy_proba": 0.30,
        "tabular_sell_proba": 0.20,
    })

    assert passes is False
    assert reason.startswith("rsi_above_uptrend_max")


def test_uptrend_entry_filter_blocks_weak_buy_proba(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.uptrend_entry_gate_enabled = True
    bot.config.uptrend_entry_min_buy_proba = 0.24

    passes, reason = bot._passes_uptrend_entry_filter({
        "recommendation": "HOLD (Up-Trend)",
        "signal_source": "catboost",
        "rsi": 60.0,
        "tabular_buy_proba": 0.22,
        "tabular_sell_proba": 0.18,
    })

    assert passes is False
    assert reason.startswith("buy_proba_below_uptrend_min")


def test_uptrend_entry_filter_allows_stronger_rules_trade(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.uptrend_entry_gate_enabled = True
    bot.config.uptrend_entry_max_rsi = 72.0
    bot.config.uptrend_entry_min_buy_proba = 0.24
    bot.config.uptrend_entry_max_sell_proba = 0.34
    bot.config.uptrend_entry_min_proba_edge = -0.05

    passes, reason = bot._passes_uptrend_entry_filter({
        "recommendation": "HOLD (Up-Trend)",
        "signal_source": "rules",
        "rsi": 64.0,
        "tabular_buy_proba": 0.27,
        "tabular_sell_proba": 0.31,
    })

    assert passes is True
    assert reason == "ok"


def test_uptrend_entry_filter_applies_coin_specific_overrides(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.uptrend_entry_gate_enabled = True
    bot.config.uptrend_entry_max_rsi = 72.0
    bot.config.uptrend_entry_min_buy_proba = 0.24
    bot.config.uptrend_entry_max_sell_proba = 0.34
    bot.config.uptrend_entry_max_rsi_by_coin = {"XDC": 78.0, "TRX": 74.0}
    bot.config.uptrend_entry_min_buy_proba_by_coin = {"TRX": 0.150}
    bot.config.uptrend_entry_max_sell_proba_by_coin = {"ONDO": 0.445}
    bot.config.uptrend_entry_min_proba_edge_by_coin = {
        "TRX": -0.13, "ONDO": -0.055}

    passes, reason = bot._passes_uptrend_entry_filter({
        "coin": "XDC",
        "recommendation": "HOLD (Up-Trend)",
        "signal_source": "rules",
        "rsi": 76.5,
        "tabular_buy_proba": 0.30,
        "tabular_sell_proba": 0.31,
    })
    assert passes is True
    assert reason == "ok"

    passes, reason = bot._passes_uptrend_entry_filter({
        "coin": "TRX",
        "recommendation": "HOLD (Up-Trend)",
        "signal_source": "catboost",
        "rsi": 73.6,
        "tabular_buy_proba": 0.151,
        "tabular_sell_proba": 0.276,
    })
    assert passes is True
    assert reason == "ok"

    passes, reason = bot._passes_uptrend_entry_filter({
        "coin": "ONDO",
        "recommendation": "HOLD (Up-Trend)",
        "signal_source": "rules",
        "rsi": 64.0,
        "tabular_buy_proba": 0.388,
        "tabular_sell_proba": 0.4403,
    })
    assert passes is True
    assert reason == "ok"


def test_fallback_entry_filter_applies_coin_specific_rsi_override(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.fallback_max_rsi = 68.0
    bot.config.fallback_max_rsi_by_coin = {"TRX": 72.0}

    passes, reason = bot._passes_fallback_entry_filter("TRX", {
        "coin": "TRX",
        "recommendation": "HOLD (Up-Trend)",
        "rsi": 71.2,
    })
    assert passes is True
    assert reason == "ok"

    passes, reason = bot._passes_fallback_entry_filter("ETH", {
        "coin": "ETH",
        "recommendation": "HOLD (Up-Trend)",
        "rsi": 70.7,
    })
    assert passes is False
    assert reason.startswith("rsi_above_fallback_max")


def test_uptrend_entry_filter_blocks_missing_tabular_probs_for_rules_trade(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.uptrend_entry_gate_enabled = True

    passes, reason = bot._passes_uptrend_entry_filter({
        "recommendation": "HOLD (Up-Trend)",
        "signal_source": "rules",
        "rsi": 60.0,
        "tabular_buy_proba": None,
        "tabular_sell_proba": 0.20,
    })

    assert passes is False
    assert reason == "missing_buy_proba"


def test_downtrend_reversal_filter_blocks_weak_buy_proba(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.downtrend_reversal_entry_enabled = True
    bot.config.downtrend_reversal_max_rsi = 20.0
    bot.config.downtrend_reversal_min_buy_proba = 0.22
    bot.config.downtrend_reversal_max_sell_proba = 0.30
    bot.config.downtrend_reversal_min_proba_edge = 0.07

    passes, reason = bot._passes_downtrend_reversal_filter({
        "recommendation": "HOLD (Down-Trend)",
        "signal_source": "catboost",
        "rsi": 18.0,
        "tabular_buy_proba": 0.20,
        "tabular_sell_proba": 0.10,
    })

    assert passes is False
    assert reason.startswith("buy_proba_below_min")


def test_downtrend_reversal_filter_requires_positive_confirmation(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.downtrend_reversal_entry_enabled = True
    bot.config.downtrend_reversal_max_rsi = 20.0
    bot.config.downtrend_reversal_min_buy_proba = 0.22
    bot.config.downtrend_reversal_max_sell_proba = 0.30
    bot.config.downtrend_reversal_min_proba_edge = 0.07
    bot.config.downtrend_reversal_min_ret_1 = 0.0

    passes, reason = bot._passes_downtrend_reversal_filter({
        "recommendation": "HOLD (Down-Trend)",
        "signal_source": "catboost",
        "rsi": 18.0,
        "tabular_buy_proba": 0.30,
        "tabular_sell_proba": 0.10,
        "ret_1": -0.01,
        "ret_3": 0.02,
        "macd_hist": 0.05,
    })

    assert passes is False
    assert reason.startswith("ret_1_below_reversal_min")


def test_downtrend_reversal_filter_allows_confirmed_reversal(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.downtrend_reversal_entry_enabled = True
    bot.config.downtrend_reversal_max_rsi = 20.0
    bot.config.downtrend_reversal_min_buy_proba = 0.22
    bot.config.downtrend_reversal_max_sell_proba = 0.30
    bot.config.downtrend_reversal_min_proba_edge = 0.07
    bot.config.downtrend_reversal_min_ret_1 = 0.0
    bot.config.downtrend_reversal_min_ret_3 = -0.01
    bot.config.downtrend_reversal_min_macd_hist = 0.0

    passes, reason = bot._passes_downtrend_reversal_filter({
        "recommendation": "HOLD (Down-Trend)",
        "signal_source": "catboost",
        "rsi": 18.0,
        "tabular_buy_proba": 0.30,
        "tabular_sell_proba": 0.10,
        "ret_1": 0.01,
        "ret_3": 0.03,
        "macd_hist": 0.05,
    })

    assert passes is True
    assert reason == "downtrend_reversal_ok"


def test_downtrend_reversal_filter_uses_tabular_proba_even_for_rules_signal(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.downtrend_reversal_entry_enabled = True
    bot.config.downtrend_reversal_allowed_coins = {"SUI"}
    bot.config.downtrend_reversal_max_rsi = 35.0
    bot.config.downtrend_reversal_min_buy_proba = 0.20
    bot.config.downtrend_reversal_max_sell_proba = 0.39
    bot.config.downtrend_reversal_min_proba_edge = -0.14
    bot.config.downtrend_reversal_min_ret_1 = 0.0
    bot.config.downtrend_reversal_min_ret_3 = -0.01
    bot.config.downtrend_reversal_min_macd_hist = 0.0

    passes, reason = bot._passes_downtrend_reversal_filter({
        "coin": "SUI",
        "recommendation": "HOLD (Down-Trend)",
        "signal_source": "rules",
        "rsi": 33.1,
        "tabular_buy_proba": 0.239,
        "tabular_sell_proba": 0.375,
        "ret_1": 0.01,
        "ret_3": 0.02,
        "macd_hist": 0.05,
    })

    assert passes is True
    assert reason == "downtrend_reversal_ok"


def test_downtrend_reversal_filter_blocks_non_allowed_coin(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.downtrend_reversal_allowed_coins = {"ETH"}

    passes, reason = bot._passes_downtrend_reversal_filter({
        "coin": "BTC",
        "recommendation": "HOLD (Down-Trend)",
        "signal_source": "catboost",
        "rsi": 18.0,
        "tabular_buy_proba": 0.30,
        "tabular_sell_proba": 0.10,
        "ret_1": 0.01,
        "ret_3": 0.03,
        "macd_hist": 0.05,
    })

    assert passes is False
    assert reason == "coin_not_allowed_for_reversal (BTC)"


def test_downtrend_reversal_filter_allows_coin_specific_rsi_override(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.downtrend_reversal_entry_enabled = True
    bot.config.downtrend_reversal_allowed_coins = {"SOL"}
    bot.config.downtrend_reversal_max_rsi = 20.0
    bot.config.downtrend_reversal_max_rsi_by_coin = {"SOL": 36.0}
    bot.config.downtrend_reversal_min_buy_proba = 0.15
    bot.config.downtrend_reversal_max_sell_proba = 0.45
    bot.config.downtrend_reversal_min_proba_edge = -0.30
    bot.config.downtrend_reversal_min_ret_1 = 0.0
    bot.config.downtrend_reversal_min_ret_3 = -0.01
    bot.config.downtrend_reversal_min_macd_hist = 0.0

    passes, reason = bot._passes_downtrend_reversal_filter({
        "coin": "SOL",
        "recommendation": "HOLD (Down-Trend)",
        "signal_source": "catboost",
        "rsi": 35.5,
        "tabular_buy_proba": 0.17,
        "tabular_sell_proba": 0.39,
        "ret_1": 0.01,
        "ret_3": 0.02,
        "macd_hist": 0.05,
    })

    assert passes is True
    assert reason == "downtrend_reversal_ok"


def test_downtrend_reversal_filter_honors_coin_specific_buy_proba_edge_override(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.downtrend_reversal_entry_enabled = True
    bot.config.downtrend_reversal_allowed_coins = {"TRX"}
    bot.config.downtrend_reversal_max_rsi = 35.0
    bot.config.downtrend_reversal_min_buy_proba = 0.18
    bot.config.downtrend_reversal_min_buy_proba_by_coin = {"TRX": 0.15}
    bot.config.downtrend_reversal_max_sell_proba = 0.42
    bot.config.downtrend_reversal_max_sell_proba_by_coin = {"TRX": 0.21}
    bot.config.downtrend_reversal_min_proba_edge = -0.02
    bot.config.downtrend_reversal_min_proba_edge_by_coin = {"TRX": -0.06}
    bot.config.downtrend_reversal_min_ret_1 = 0.0
    bot.config.downtrend_reversal_min_ret_3 = -0.01
    bot.config.downtrend_reversal_min_macd_hist = 0.0

    passes, reason = bot._passes_downtrend_reversal_filter({
        "coin": "TRX",
        "recommendation": "HOLD (Down-Trend)",
        "signal_source": "catboost",
        "rsi": 28.0,
        "tabular_buy_proba": 0.151,
        "tabular_sell_proba": 0.205,
        "ret_1": 0.01,
        "ret_3": 0.02,
        "macd_hist": 0.05,
    })

    assert passes is True
    assert reason == "downtrend_reversal_ok"


def test_entry_market_mode_detects_defensive_simulation_regime(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.simulation_regime = "crash"

    mode = bot._entry_market_mode({
        "BTC": {"recommendation": "BUY"},
        "ETH": {"recommendation": "HOLD (Up-Trend)"},
    })

    assert mode == "defensive"


def test_entry_market_mode_detects_bearish_live_mix(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.simulate_data = False

    mode = bot._entry_market_mode({
        "BTC": {"recommendation": "HOLD (Down-Trend)"},
        "ETH": {"recommendation": "WEAK SELL"},
        "SOL": {"recommendation": "SELL"},
        "XRP": {"recommendation": "BUY"},
    })

    assert mode == "defensive"


def test_entry_market_mode_mixed_simulation_can_escalate_to_defensive(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.simulate_data = True
    bot.config.simulation_regime = "mixed"

    mode = bot._entry_market_mode({
        "BTC": {"recommendation": "SELL"},
        "ETH": {"recommendation": "WEAK SELL"},
        "SOL": {"recommendation": "HOLD (Down-Trend)"},
        "XRP": {"recommendation": "SELL"},
    })

    assert mode == "defensive"


def test_lossmaker_exclusions_are_merged_into_excluded_coins(monkeypatch):
    monkeypatch.setenv("EXCLUDED_COINS", "USDT")
    monkeypatch.setenv("LOSSMAKER_EXCLUDED_COINS", "ZEC,HYPE,TON,BTC,XRP")

    config = BotConfig()

    assert {"USDT", "ZEC", "HYPE", "TON", "BTC",
            "XRP"}.issubset(config.excluded_coins)


def test_analytics_db_connect_kwargs_support_url_and_split_fields(monkeypatch):
    monkeypatch.setenv("ANALYTICS_DB_ENABLED", "true")
    monkeypatch.setenv("ANALYTICS_DB_URL",
                       "postgresql://user:secret@db.example/trading")
    config = BotConfig()

    kwargs = config.analytics_db_connect_kwargs()

    assert kwargs["conninfo"] == "postgresql://user:secret@db.example/trading"
    assert kwargs["connect_timeout"] == 5

    monkeypatch.delenv("ANALYTICS_DB_URL", raising=False)
    monkeypatch.setenv("ANALYTICS_DB_HOST", "db.example")
    monkeypatch.setenv("ANALYTICS_DB_PORT", "5433")
    monkeypatch.setenv("ANALYTICS_DB_NAME", "trading")
    monkeypatch.setenv("ANALYTICS_DB_USER", "bot")
    monkeypatch.setenv("ANALYTICS_DB_PASSWORD", "secret")
    monkeypatch.setenv("ANALYTICS_DB_SSLMODE", "require")

    config = BotConfig()
    kwargs = config.analytics_db_connect_kwargs()

    assert kwargs["host"] == "db.example"
    assert kwargs["port"] == 5433
    assert kwargs["dbname"] == "trading"
    assert kwargs["user"] == "bot"
    assert kwargs["password"] == "secret"
    assert kwargs["sslmode"] == "require"


def test_analytics_db_connect_kwargs_support_legacy_postgres_env_names(monkeypatch):
    monkeypatch.setenv("POSTGRES_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_HOST", "postgres-analytics")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DB", "trading_analytics")
    monkeypatch.setenv("POSTGRES_USER", "trading_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.setenv("POSTGRES_SSLMODE", "disable")
    monkeypatch.setenv("POSTGRES_SCHEMA", "trading_analytics")
    monkeypatch.setenv("POSTGRES_CONNECT_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("POSTGRES_SNAPSHOT_EVERY", "5")

    config = BotConfig()
    kwargs = config.analytics_db_connect_kwargs()

    assert config.analytics_db_enabled is True
    assert config.analytics_db_schema == "trading_analytics"
    assert config.analytics_db_snapshot_every == 5
    assert kwargs["host"] == "postgres-analytics"
    assert kwargs["port"] == 5432
    assert kwargs["dbname"] == "trading_analytics"
    assert kwargs["user"] == "trading_user"
    assert kwargs["password"] == "secret"
    assert kwargs["sslmode"] == "disable"
    assert kwargs["connect_timeout"] == 7


def test_botconfig_initializes_ai_copilot_error_limits(monkeypatch):
    monkeypatch.setenv("AI_COPILOT_ENABLED", "true")
    monkeypatch.setenv("AI_COPILOT_MAX_CONSECUTIVE_ERRORS", "7")

    config = BotConfig()

    assert config.ai_copilot_enabled is True
    assert config.ai_copilot_max_consecutive_errors == 7


def test_logs_blocked_buy_attempt_summary(monkeypatch, caplog):
    bot = _make_test_bot(monkeypatch)

    with caplog.at_level(logging.INFO):
        bot._log_blocked_buy_attempt_candidates([
            {
                "coin": "TRX",
                "reason": "ret_3_below_min (-0.0300 < -0.0100)",
                "signal_source": "rules",
                "score": 60,
                "recommendation": "BUY",
                "position_size_text": "5.00",
                "cash_text": "19.85",
                "cooldown_text": "n/a",
            }
        ])

    assert "Buy attempt blocked 1 candidate(s)" in caplog.text
    assert "ret_3_below_min" in caplog.text
    assert "TRX" in caplog.text


def test_logs_fallback_rsi_block_summary(monkeypatch, caplog):
    bot = _make_test_bot(monkeypatch)

    with caplog.at_level(logging.INFO):
        bot._log_fallback_entry_diagnostics(
            market_analysis={
                "TRX": {
                    "recommendation": "HOLD (Up-Trend)",
                    "rsi": 72.19,
                    "signal_source": "rules",
                    "score": 60,
                    "rule_score": 60,
                }
            },
            fallback_base_candidates=["TRX"],
            fallback_filter_results={
                "TRX": (
                    False,
                    "rsi_above_fallback_max (72.19 > 72.00)",
                )
            },
            fallback_allowed=True,
            fallback_suppression_reason="allowed",
            entry_market_mode="cautious",
        )

    assert "Fallback RSI gate blocked 1 candidate(s)" in caplog.text
    assert "rsi_above_fallback_max" in caplog.text
    assert "TRX" in caplog.text


def test_logs_fallback_suppressed_by_defensive_mode(monkeypatch, caplog):
    bot = _make_test_bot(monkeypatch)

    with caplog.at_level(logging.INFO):
        bot._log_fallback_entry_diagnostics(
            market_analysis={
                "TRX": {
                    "recommendation": "HOLD (Up-Trend)",
                    "rsi": 71.88,
                    "signal_source": "catboost",
                    "score": 60,
                    "rule_score": 60,
                    "signal_confidence": 0.57,
                }
            },
            fallback_base_candidates=["TRX"],
            fallback_filter_results={"TRX": (True, "ok")},
            fallback_allowed=False,
            fallback_suppression_reason="entry_mode_defensive",
            entry_market_mode="defensive",
        )

    assert "Fallback entry suppressed (entry_mode_defensive)" in caplog.text
    assert "entry_mode=defensive" in caplog.text
    assert "confidence=57%" in caplog.text


def test_allows_entry_signal_keeps_approved_downtrend_candidate(monkeypatch):
    bot = _make_test_bot(monkeypatch)

    assert bot._allows_entry_signal(
        recommendation="HOLD (Down-Trend)",
        downtrend_reversal_allowed=True,
        excluded_signals={"SELL", "WEAK SELL", "HOLD (Down-Trend)"},
    ) is True

    assert bot._allows_entry_signal(
        recommendation="HOLD (Down-Trend)",
        downtrend_reversal_allowed=False,
        excluded_signals={"SELL", "WEAK SELL", "HOLD (Down-Trend)"},
    ) is False


def test_effective_trade_amount_scales_by_portfolio_tiers(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.trade_amount = 10.0
    bot.config.portfolio_trade_amount_multipliers = [
        (50.0, 1.10),
        (100.0, 1.25),
        (200.0, 1.50),
    ]

    assert bot._effective_trade_amount(40.0) == 10.0
    assert bot._effective_trade_amount(60.0) == 11.0
    assert bot._effective_trade_amount(150.0) == 12.5
    assert bot._effective_trade_amount(250.0) == 15.0


def test_effective_max_open_trades_scales_by_portfolio_tiers(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.max_open_trades = 2
    bot.config.portfolio_max_open_trades_tiers = [
        (100.0, 3),
        (200.0, 4),
    ]

    assert bot._effective_max_open_trades(80.0) == 2
    assert bot._effective_max_open_trades(120.0) == 3
    assert bot._effective_max_open_trades(220.0) == 4


def test_identifies_rules_uptrend_trade_only_for_rules_source(monkeypatch):
    bot = _make_test_bot(monkeypatch)

    assert bot._is_rules_uptrend_trade({
        "recommendation": "HOLD (Up-Trend)",
        "signal_source": "rules",
    }) is True
    assert bot._is_rules_uptrend_trade({
        "recommendation": "HOLD (Up-Trend)",
        "signal_source": "catboost",
    }) is False


def test_uptrend_rules_fast_exit_closes_flat_rules_trade(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.partial_take_profit_enabled = False
    bot.config.trailing_stop_enabled = False
    bot.config.break_even_enabled = False
    bot.config.max_hold_seconds = 0
    bot.config.coin_max_hold_seconds = {}
    bot.config.exit_on_downtrend = False
    bot.config.uptrend_rules_fast_exit_enabled = True
    bot.config.uptrend_rules_fast_exit_seconds = 120
    bot.config.uptrend_rules_fast_exit_seconds_by_coin = {}
    bot.config.uptrend_rules_flat_max_profit_pct = 0.08
    bot.config.uptrend_rules_flat_max_profit_pct_by_coin = {}
    bot.config.uptrend_rules_max_hold_seconds = 300
    bot.config.uptrend_rules_max_hold_seconds_by_coin = {}

    bot.portfolio.cash = 0.0
    bot.portfolio.holdings["TRX"] = 1.0
    bot.portfolio.open_trades["TRX"] = {
        "buy_price": 100.0,
        "amount_coin": 1.0,
        "amount_base": 100.0,
        "timestamp": datetime.now() - timedelta(seconds=180),
        "peak_price": 100.05,
        "partial_tp_taken": False,
        "partial_tp_timestamp": None,
        "signal_source": "rules",
        "signal_confidence": None,
        "recommendation": "HOLD (Up-Trend)",
    }
    monkeypatch.setattr(bot, "_get_atr_for_coin", lambda coin, period=14: 5.0)

    executed = {}

    def _fake_execute_trade(coin, action, price, amount_in_base_currency, atr=None, signal_source='rules', signal_confidence=None, recommendation='HOLD', reason=''):
        executed["coin"] = coin
        executed["action"] = action
        executed["reason"] = reason
        return True

    monkeypatch.setattr(bot, "_execute_trade", _fake_execute_trade)

    bot._manage_open_trades(
        {"TRX": {"price": 100.05}},
        {"TRX": {"recommendation": "HOLD (Up-Trend)"}},
    )

    assert executed["coin"] == "TRX"
    assert executed["action"] == "sell"
    assert "UPTREND-RULES-FAST-EXIT" in executed["reason"]
    assert "signal: HOLD (Up-Trend)" in executed["reason"]
    assert "TRX" not in bot.portfolio.open_trades


def test_uptrend_rules_fast_exit_respects_coin_specific_seconds_override(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.partial_take_profit_enabled = False
    bot.config.trailing_stop_enabled = False
    bot.config.break_even_enabled = False
    bot.config.max_hold_seconds = 0
    bot.config.coin_max_hold_seconds = {}
    bot.config.exit_on_downtrend = False
    bot.config.uptrend_rules_fast_exit_enabled = True
    bot.config.uptrend_rules_fast_exit_seconds = 120
    bot.config.uptrend_rules_fast_exit_seconds_by_coin = {"VVV": 240}
    bot.config.uptrend_rules_flat_max_profit_pct = 0.08
    bot.config.uptrend_rules_flat_max_profit_pct_by_coin = {}
    bot.config.uptrend_rules_max_hold_seconds = 300
    bot.config.uptrend_rules_max_hold_seconds_by_coin = {}

    bot.portfolio.cash = 0.0
    bot.portfolio.holdings["VVV"] = 1.0
    bot.portfolio.open_trades["VVV"] = {
        "buy_price": 100.0,
        "amount_coin": 1.0,
        "amount_base": 100.0,
        "timestamp": datetime.now() - timedelta(seconds=180),
        "peak_price": 100.05,
        "partial_tp_taken": False,
        "partial_tp_timestamp": None,
        "signal_source": "rules",
        "signal_confidence": None,
        "recommendation": "HOLD (Up-Trend)",
    }
    monkeypatch.setattr(bot, "_get_atr_for_coin", lambda coin, period=14: 5.0)

    executed = {}

    def _fake_execute_trade(coin, action, price, amount_in_base_currency, atr=None, signal_source='rules', signal_confidence=None, recommendation='HOLD', reason=''):
        executed["coin"] = coin
        return True

    monkeypatch.setattr(bot, "_execute_trade", _fake_execute_trade)

    bot._manage_open_trades(
        {"VVV": {"price": 99.3}},
        {"VVV": {"recommendation": "HOLD (Up-Trend)"}},
    )

    assert executed == {}
    assert "VVV" in bot.portfolio.open_trades


def test_uptrend_rules_fast_exit_respects_coin_specific_profit_override(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.partial_take_profit_enabled = False
    bot.config.trailing_stop_enabled = False
    bot.config.break_even_enabled = False
    bot.config.max_hold_seconds = 0
    bot.config.coin_max_hold_seconds = {}
    bot.config.exit_on_downtrend = False
    bot.config.uptrend_rules_fast_exit_enabled = True
    bot.config.uptrend_rules_fast_exit_seconds = 120
    bot.config.uptrend_rules_fast_exit_seconds_by_coin = {}
    bot.config.uptrend_rules_flat_max_profit_pct = 0.08
    bot.config.uptrend_rules_flat_max_profit_pct_by_coin = {"VVV": -1.0}
    bot.config.uptrend_rules_max_hold_seconds = 300
    bot.config.uptrend_rules_max_hold_seconds_by_coin = {}

    bot.portfolio.cash = 0.0
    bot.portfolio.holdings["VVV"] = 1.0
    bot.portfolio.open_trades["VVV"] = {
        "buy_price": 100.0,
        "amount_coin": 1.0,
        "amount_base": 100.0,
        "timestamp": datetime.now() - timedelta(seconds=180),
        "peak_price": 100.05,
        "partial_tp_taken": False,
        "partial_tp_timestamp": None,
        "signal_source": "rules",
        "signal_confidence": None,
        "recommendation": "HOLD (Up-Trend)",
    }
    monkeypatch.setattr(bot, "_get_atr_for_coin", lambda coin, period=14: 5.0)

    executed = {}

    def _fake_execute_trade(coin, action, price, amount_in_base_currency, atr=None, signal_source='rules', signal_confidence=None, recommendation='HOLD', reason=''):
        executed["coin"] = coin
        return True

    monkeypatch.setattr(bot, "_execute_trade", _fake_execute_trade)

    bot._manage_open_trades(
        {"VVV": {"price": 99.3}},
        {"VVV": {"recommendation": "HOLD (Up-Trend)"}},
    )

    assert executed == {}
    assert "VVV" in bot.portfolio.open_trades


def test_uptrend_rules_max_hold_respects_coin_specific_override(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.partial_take_profit_enabled = False
    bot.config.trailing_stop_enabled = False
    bot.config.break_even_enabled = False
    bot.config.max_hold_seconds = 0
    bot.config.coin_max_hold_seconds = {}
    bot.config.exit_on_downtrend = False
    bot.config.uptrend_rules_fast_exit_enabled = True
    bot.config.uptrend_rules_fast_exit_seconds = 120
    bot.config.uptrend_rules_fast_exit_seconds_by_coin = {"VVV": 240}
    bot.config.uptrend_rules_flat_max_profit_pct = -1.0
    bot.config.uptrend_rules_flat_max_profit_pct_by_coin = {}
    bot.config.uptrend_rules_max_hold_seconds = 300
    bot.config.uptrend_rules_max_hold_seconds_by_coin = {"VVV": 420}

    bot.portfolio.cash = 0.0
    bot.portfolio.holdings["VVV"] = 1.0
    bot.portfolio.open_trades["VVV"] = {
        "buy_price": 100.0,
        "amount_coin": 1.0,
        "amount_base": 100.0,
        "timestamp": datetime.now() - timedelta(seconds=334),
        "peak_price": 100.5,
        "partial_tp_taken": False,
        "partial_tp_timestamp": None,
        "signal_source": "rules",
        "signal_confidence": None,
        "recommendation": "HOLD (Up-Trend)",
    }
    monkeypatch.setattr(bot, "_get_atr_for_coin", lambda coin, period=14: 5.0)

    executed = {}

    def _fake_execute_trade(coin, action, price, amount_in_base_currency, atr=None, signal_source='rules', signal_confidence=None, recommendation='HOLD', reason=''):
        executed["coin"] = coin
        executed["reason"] = reason
        return True

    monkeypatch.setattr(bot, "_execute_trade", _fake_execute_trade)

    bot._manage_open_trades(
        {"VVV": {"price": 100.3}},
        {"VVV": {"recommendation": "HOLD (Up-Trend)"}},
    )

    assert executed == {}
    assert "VVV" in bot.portfolio.open_trades


def test_uptrend_rules_max_hold_still_exits_after_coin_specific_limit(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.partial_take_profit_enabled = False
    bot.config.trailing_stop_enabled = False
    bot.config.break_even_enabled = False
    bot.config.max_hold_seconds = 0
    bot.config.coin_max_hold_seconds = {}
    bot.config.exit_on_downtrend = False
    bot.config.uptrend_rules_fast_exit_enabled = True
    bot.config.uptrend_rules_fast_exit_seconds = 120
    bot.config.uptrend_rules_fast_exit_seconds_by_coin = {"VVV": 240}
    bot.config.uptrend_rules_flat_max_profit_pct = -1.0
    bot.config.uptrend_rules_flat_max_profit_pct_by_coin = {}
    bot.config.uptrend_rules_max_hold_seconds = 300
    bot.config.uptrend_rules_max_hold_seconds_by_coin = {"VVV": 420}

    bot.portfolio.cash = 0.0
    bot.portfolio.holdings["VVV"] = 1.0
    bot.portfolio.open_trades["VVV"] = {
        "buy_price": 100.0,
        "amount_coin": 1.0,
        "amount_base": 100.0,
        "timestamp": datetime.now() - timedelta(seconds=430),
        "peak_price": 100.5,
        "partial_tp_taken": False,
        "partial_tp_timestamp": None,
        "signal_source": "rules",
        "signal_confidence": None,
        "recommendation": "HOLD (Up-Trend)",
    }
    monkeypatch.setattr(bot, "_get_atr_for_coin", lambda coin, period=14: 5.0)

    executed = {}

    def _fake_execute_trade(coin, action, price, amount_in_base_currency, atr=None, signal_source='rules', signal_confidence=None, recommendation='HOLD', reason=''):
        executed["coin"] = coin
        executed["action"] = action
        executed["reason"] = reason
        return True

    monkeypatch.setattr(bot, "_execute_trade", _fake_execute_trade)

    bot._manage_open_trades(
        {"VVV": {"price": 100.3}},
        {"VVV": {"recommendation": "HOLD (Up-Trend)"}},
    )

    assert executed["coin"] == "VVV"
    assert executed["action"] == "sell"
    assert "UPTREND-RULES-MAX-HOLD" in executed["reason"]


def test_downtrend_reversal_weak_signal_exit_closes_trade_early(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.partial_take_profit_enabled = False
    bot.config.trailing_stop_enabled = False
    bot.config.break_even_enabled = False
    bot.config.max_hold_seconds = 0
    bot.config.exit_on_downtrend = False
    bot.config.downtrend_reversal_fast_exit_enabled = True
    bot.config.downtrend_reversal_fast_exit_seconds = 120
    bot.config.downtrend_reversal_flat_max_profit_pct = 0.10
    bot.config.downtrend_reversal_weak_signal_exit_seconds = 45
    bot.config.downtrend_reversal_weak_signal_max_profit_pct = 0.02
    bot.config.downtrend_reversal_max_hold_seconds = 240

    bot.portfolio.cash = 0.0
    bot.portfolio.holdings["ETH"] = 1.0
    bot.portfolio.open_trades["ETH"] = {
        "buy_price": 100.0,
        "amount_coin": 1.0,
        "amount_base": 100.0,
        "timestamp": datetime.now() - timedelta(seconds=60),
        "peak_price": 100.05,
        "partial_tp_taken": False,
        "partial_tp_timestamp": None,
        "signal_source": "catboost",
        "signal_confidence": 0.61,
        "recommendation": "HOLD (Down-Trend)",
    }
    monkeypatch.setattr(bot, "_get_atr_for_coin", lambda coin, period=14: 5.0)

    executed = {}

    def _fake_execute_trade(coin, action, price, amount_in_base_currency, atr=None, signal_source='rules', signal_confidence=None, recommendation='HOLD', reason=''):
        executed["coin"] = coin
        executed["action"] = action
        executed["reason"] = reason
        return True

    monkeypatch.setattr(bot, "_execute_trade", _fake_execute_trade)

    bot._manage_open_trades(
        {"ETH": {"price": 100.0}},
        {"ETH": {"recommendation": "HOLD (Down-Trend)"}},
    )

    assert executed["coin"] == "ETH"
    assert executed["action"] == "sell"
    assert "DOWNTREND-REVERSAL-WEAK-SIGNAL-EXIT" in executed["reason"]
    assert "ETH" not in bot.portfolio.open_trades


def test_coin_specific_max_hold_seconds_override(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.partial_take_profit_enabled = False
    bot.config.trailing_stop_enabled = False
    bot.config.break_even_enabled = False
    bot.config.exit_on_downtrend = False
    bot.config.max_hold_seconds = 720
    bot.config.coin_max_hold_seconds = {"SUI": 420}

    bot.portfolio.cash = 0.0
    bot.portfolio.holdings["SUI"] = 1.0
    bot.portfolio.open_trades["SUI"] = {
        "buy_price": 100.0,
        "amount_coin": 1.0,
        "amount_base": 100.0,
        "timestamp": datetime.now() - timedelta(seconds=500),
        "peak_price": 100.0,
        "partial_tp_taken": False,
        "partial_tp_timestamp": None,
        "signal_source": "rules",
        "signal_confidence": None,
        "recommendation": "BUY",
    }
    monkeypatch.setattr(bot, "_get_atr_for_coin", lambda coin, period=14: 5.0)

    executed = {}

    def _fake_execute_trade(coin, action, price, amount_in_base_currency, atr=None, signal_source='rules', signal_confidence=None, recommendation='HOLD', reason=''):
        executed["coin"] = coin
        executed["action"] = action
        executed["reason"] = reason
        return True

    monkeypatch.setattr(bot, "_execute_trade", _fake_execute_trade)

    bot._manage_open_trades({"SUI": {"price": 100.0}})

    assert executed["coin"] == "SUI"
    assert executed["action"] == "sell"
    assert "MAX-HOLD-TIME reached (500s >= 420s)" in executed["reason"]
    assert "SUI" not in bot.portfolio.open_trades


def test_dynamic_lossmaker_exclusions_detect_recent_timeout_losers(monkeypatch, tmp_path):
    bot = _make_test_bot(monkeypatch)
    journal_file = tmp_path / "trade_journal.csv"
    journal_file.write_text(
        "timestamp,iteration,coin,action,price,amount_coin,amount_base,pnl_base,pnl_pct,hold_seconds,signal_source,signal_confidence,recommendation,reason,dry_run\n"
        "2026-05-20T10:00:00,1,SUI,sell,1,1,1,-0.0020,-0.2,420,rules,,BUY,MAX-HOLD-TIME reached (420s >= 420s),true\n"
        "2026-05-20T10:05:00,2,SUI,sell,1,1,1,-0.0015,-0.15,430,rules,,BUY,MAX-HOLD-TIME reached (430s >= 420s),true\n"
        "2026-05-20T10:10:00,3,SUI,sell,1,1,1,-0.0010,-0.10,440,rules,,BUY,MAX-HOLD-TIME reached (440s >= 420s),true\n"
        "2026-05-20T10:15:00,4,ETH,sell,1,1,1,0.0020,0.2,180,rules,,BUY,ATR-TAKE-PROFIT,true\n",
        encoding="utf-8",
    )
    bot.config.performance_log_enabled = True
    bot.config.performance_log_file = str(journal_file)
    bot.config.dynamic_lossmaker_exclusion_enabled = True
    bot.config.dynamic_lossmaker_min_sells = 3
    bot.config.dynamic_lossmaker_min_pnl_loss = 0.003
    bot.config.dynamic_lossmaker_min_max_hold_exit_ratio = 0.5
    bot.config.dynamic_lossmaker_max_win_rate_pct = 45.0

    assert bot._dynamic_excluded_coins() == {"SUI"}


def test_dynamic_lossmaker_exclusions_skip_new_entries_but_keep_open_positions(monkeypatch, tmp_path):
    bot = _make_test_bot(monkeypatch)
    journal_file = tmp_path / "trade_journal.csv"
    journal_file.write_text(
        "timestamp,iteration,coin,action,price,amount_coin,amount_base,pnl_base,pnl_pct,hold_seconds,signal_source,signal_confidence,recommendation,reason,dry_run\n"
        "2026-05-20T10:00:00,1,SUI,sell,1,1,1,-0.0020,-0.2,420,rules,,BUY,MAX-HOLD-TIME reached (420s >= 420s),true\n"
        "2026-05-20T10:05:00,2,SUI,sell,1,1,1,-0.0015,-0.15,430,rules,,BUY,MAX-HOLD-TIME reached (430s >= 420s),true\n"
        "2026-05-20T10:10:00,3,SUI,sell,1,1,1,-0.0010,-0.10,440,rules,,BUY,MAX-HOLD-TIME reached (440s >= 420s),true\n",
        encoding="utf-8",
    )
    bot.config.performance_log_enabled = True
    bot.config.performance_log_file = str(journal_file)
    bot.config.dynamic_lossmaker_exclusion_enabled = True
    bot.config.dynamic_lossmaker_min_sells = 3
    bot.config.dynamic_lossmaker_min_pnl_loss = 0.003
    bot.config.dynamic_lossmaker_min_max_hold_exit_ratio = 0.5
    bot.config.min_volume_base = 100.0
    bot.config.top_n_for_analysis = 10

    analyzed = []

    def _fake_analyze_coin(coin, current_price):
        analyzed.append(coin)
        return {
            "recommendation": "HOLD (Up-Trend)",
            "score": 60,
            "signal_source": "rules",
            "signal_confidence": None,
            "tabular_buy_proba": None,
            "tabular_hold_proba": None,
            "tabular_sell_proba": None,
        }

    monkeypatch.setattr(bot, "_analyze_coin", _fake_analyze_coin)

    market_data = {
        "SUI": {"price": 1.0, "volume": 1000.0},
        "ETH": {"price": 2.0, "volume": 1200.0},
    }

    analysis = bot._analyze_markets(market_data)
    assert set(analysis.keys()) == {"ETH"}
    assert analyzed == ["ETH"]

    analyzed.clear()
    analysis = bot._analyze_markets(market_data, extra_coins=["SUI"])
    assert set(analysis.keys()) == {"ETH", "SUI"}
    assert analyzed == ["ETH", "SUI"]


def test_dynamic_lossmaker_exclusions_use_recent_sell_window_not_total_rows(monkeypatch, tmp_path):
    bot = _make_test_bot(monkeypatch)
    journal_file = tmp_path / "trade_journal.csv"
    journal_file.write_text(
        "timestamp,iteration,coin,action,price,amount_coin,amount_base,pnl_base,pnl_pct,hold_seconds,signal_source,signal_confidence,recommendation,reason,dry_run\n"
        "2026-05-20T10:00:00,1,SUI,sell,1,1,1,-0.0020,-0.2,420,rules,,BUY,MAX-HOLD-TIME reached (420s >= 420s),true\n"
        "2026-05-20T10:01:00,2,SUI,sell,1,1,1,-0.0015,-0.15,430,rules,,BUY,MAX-HOLD-TIME reached (430s >= 420s),true\n"
        "2026-05-20T10:02:00,3,SUI,sell,1,1,1,-0.0010,-0.10,440,rules,,BUY,MAX-HOLD-TIME reached (440s >= 420s),true\n"
        "2026-05-20T10:03:00,4,ETH,buy,1,1,1,0,0,0,rules,,BUY,ENTRY,true\n"
        "2026-05-20T10:04:00,5,ETH,buy,1,1,1,0,0,0,rules,,BUY,ENTRY,true\n"
        "2026-05-20T10:05:00,6,ETH,buy,1,1,1,0,0,0,rules,,BUY,ENTRY,true\n"
        "2026-05-20T10:06:00,7,ETH,buy,1,1,1,0,0,0,rules,,BUY,ENTRY,true\n",
        encoding="utf-8",
    )
    bot.config.performance_log_enabled = True
    bot.config.performance_log_file = str(journal_file)
    bot.config.dynamic_lossmaker_exclusion_enabled = True
    bot.config.dynamic_lossmaker_window = 3
    bot.config.dynamic_lossmaker_min_sells = 3
    bot.config.dynamic_lossmaker_min_pnl_loss = 0.003
    bot.config.dynamic_lossmaker_min_max_hold_exit_ratio = 0.5
    bot.config.dynamic_lossmaker_max_win_rate_pct = 45.0

    assert bot._dynamic_excluded_coins() == {"SUI"}


def test_entry_momentum_filter_blocks_sharp_pump_ret3(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.entry_momentum_filter_enabled = True
    bot.config.entry_require_price_above_ema20 = False
    bot.config.entry_sharp_pump_filter_enabled = True
    bot.config.entry_max_ret_1 = 0.20
    bot.config.entry_max_ret_3 = 0.08

    passes, reason = bot._passes_entry_momentum_filter({
        "recommendation": "BUY",
        "ret_1": 0.02,
        "ret_3": 0.12,
    })

    assert passes is False
    assert reason.startswith("sharp_pump_ret_3")


def test_entry_momentum_filter_allows_when_pump_filter_disabled(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.entry_momentum_filter_enabled = True
    bot.config.entry_require_price_above_ema20 = False
    bot.config.entry_sharp_pump_filter_enabled = False
    bot.config.entry_max_ret_1 = 0.04
    bot.config.entry_max_ret_3 = 0.08

    passes, reason = bot._passes_entry_momentum_filter({
        "recommendation": "BUY",
        "ret_1": 0.09,
        "ret_3": 0.15,
    })

    assert passes is True
    assert reason == "ok"


def test_reentry_cooldown_blocks_recently_sold_coin(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.reentry_cooldown_seconds = 600
    bot.last_sell_timestamps_utc["BTC"] = datetime.now(
        timezone.utc) - timedelta(seconds=120)

    blocked, remaining = bot._is_coin_in_reentry_cooldown("BTC")

    assert blocked is True
    assert remaining > 0


def test_reentry_cooldown_allows_after_expiry(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.reentry_cooldown_seconds = 300
    bot.last_sell_timestamps_utc["ETH"] = datetime.now(
        timezone.utc) - timedelta(seconds=301)

    blocked, remaining = bot._is_coin_in_reentry_cooldown("ETH")

    assert blocked is False
    assert remaining == 0


def test_partial_take_profit_reduces_position(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.partial_take_profit_enabled = True
    bot.config.partial_take_profit_atr_mult = 1.0
    bot.config.partial_take_profit_fraction = 0.5
    bot.config.trailing_stop_enabled = False
    bot.config.break_even_enabled = False

    bot.portfolio.cash = 0.0
    bot.portfolio.holdings["BTC"] = 1.0
    bot.portfolio.add_trade("BTC", 100.0, 1.0, 100.0)
    monkeypatch.setattr(bot, "_get_atr_for_coin", lambda coin, period=14: 2.0)

    bot._manage_open_trades({"BTC": {"price": 102.5}})

    assert bot.portfolio.holdings["BTC"] == 0.5
    assert bot.portfolio.cash == 51.25
    assert "BTC" in bot.portfolio.open_trades
    assert bot.portfolio.open_trades["BTC"]["amount_coin"] == 0.5
    assert bot.portfolio.open_trades["BTC"]["partial_tp_taken"] is True


def test_partial_take_profit_only_executes_once(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.partial_take_profit_enabled = True
    bot.config.partial_take_profit_atr_mult = 1.0
    bot.config.partial_take_profit_fraction = 0.5
    bot.config.trailing_stop_enabled = False
    bot.config.break_even_enabled = False

    bot.portfolio.cash = 0.0
    bot.portfolio.holdings["BTC"] = 1.0
    bot.portfolio.add_trade("BTC", 100.0, 1.0, 100.0)
    monkeypatch.setattr(bot, "_get_atr_for_coin", lambda coin, period=14: 2.0)

    bot._manage_open_trades({"BTC": {"price": 102.5}})
    cash_after_first = bot.portfolio.cash
    amount_after_first = bot.portfolio.open_trades["BTC"]["amount_coin"]

    bot._manage_open_trades({"BTC": {"price": 103.0}})

    assert bot.portfolio.cash == cash_after_first
    assert bot.portfolio.open_trades["BTC"]["amount_coin"] == amount_after_first


def test_partial_take_profit_remainder_exits_on_timeout(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.partial_take_profit_enabled = True
    bot.config.partial_take_profit_remainder_max_hold_seconds = 300
    bot.config.partial_take_profit_exit_on_weak_signal = False
    bot.config.trailing_stop_enabled = False
    bot.config.break_even_enabled = False

    bot.portfolio.cash = 0.0
    bot.portfolio.holdings["BTC"] = 0.5
    bot.portfolio.open_trades["BTC"] = {
        "buy_price": 100.0,
        "amount_coin": 0.5,
        "amount_base": 50.0,
        "timestamp": datetime.now() - timedelta(seconds=600),
        "peak_price": 103.0,
        "partial_tp_taken": True,
        "partial_tp_timestamp": datetime.now() - timedelta(seconds=301),
        "signal_source": "rules",
        "signal_confidence": None,
        "recommendation": "BUY",
    }
    monkeypatch.setattr(bot, "_get_atr_for_coin", lambda coin, period=14: 2.0)

    bot._manage_open_trades({"BTC": {"price": 102.0}})

    assert "BTC" not in bot.portfolio.open_trades
    assert bot.portfolio.holdings.get("BTC", 0.0) == 0.0
    assert bot.portfolio.cash == 51.0


def test_partial_take_profit_remainder_exits_on_weak_signal(monkeypatch):
    bot = _make_test_bot(monkeypatch)
    bot.config.partial_take_profit_enabled = True
    bot.config.partial_take_profit_remainder_max_hold_seconds = 0
    bot.config.partial_take_profit_exit_on_weak_signal = True
    bot.config.trailing_stop_enabled = False
    bot.config.break_even_enabled = False

    bot.portfolio.cash = 0.0
    bot.portfolio.holdings["BTC"] = 0.5
    bot.portfolio.open_trades["BTC"] = {
        "buy_price": 100.0,
        "amount_coin": 0.5,
        "amount_base": 50.0,
        "timestamp": datetime.now() - timedelta(seconds=600),
        "peak_price": 103.0,
        "partial_tp_taken": True,
        "partial_tp_timestamp": datetime.now() - timedelta(seconds=60),
        "signal_source": "rules",
        "signal_confidence": None,
        "recommendation": "BUY",
    }
    monkeypatch.setattr(bot, "_get_atr_for_coin", lambda coin, period=14: 2.0)

    bot._manage_open_trades(
        {"BTC": {"price": 101.0}},
        {"BTC": {"recommendation": "WEAK SELL"}},
    )

    assert "BTC" not in bot.portfolio.open_trades
    assert bot.portfolio.holdings.get("BTC", 0.0) == 0.0
    assert bot.portfolio.cash == 50.5
