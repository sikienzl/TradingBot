from datetime import datetime, timedelta, timezone

from trading_bot import BotConfig, CryptoTradingBot


def _make_test_bot(monkeypatch):
    monkeypatch.setenv("SIMULATE_DATA", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("USE_TABULAR_MODEL", "false")
    monkeypatch.setenv("USE_ML_MODEL", "false")
    monkeypatch.setenv("PERFORMANCE_LOG_ENABLED", "false")

    config = BotConfig()
    return CryptoTradingBot(config)


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
