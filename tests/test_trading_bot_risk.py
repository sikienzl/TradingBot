import logging
from datetime import datetime, timedelta, timezone

from trading_bot import BotConfig, CryptoTradingBot


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
    bot.config.entry_require_price_above_ema20 = False

    passes, reason = bot._passes_entry_momentum_filter({
        "recommendation": "BUY",
        "ret_3": -0.03,
        "price": 105.0,
        "ema_20": 100.0,
    })

    assert passes is False
    assert reason.startswith("ret_3_below_min")


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
    bot.config.exit_on_downtrend = False
    bot.config.uptrend_rules_fast_exit_enabled = True
    bot.config.uptrend_rules_fast_exit_seconds = 120
    bot.config.uptrend_rules_flat_max_profit_pct = 0.08
    bot.config.uptrend_rules_max_hold_seconds = 300

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
