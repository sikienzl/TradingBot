"""Tests for circuit breaker (price staleness) in CryptoTradingBot."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


def _make_bot():
    """Create a minimal CryptoTradingBot with mocked config and exchange."""
    from src.trading_bot import CryptoTradingBot

    with (
        patch("src.trading_bot.CryptoTradingBot._initialize_exchange", return_value=None),
        patch("src.trading_bot.CryptoTradingBot._load_market_info"),
        patch("src.trading_bot.PostgresAnalyticsWriter"),
    ):
        bot = CryptoTradingBot.__new__(CryptoTradingBot)
        bot.config = MagicMock()
        bot.config.price_staleness_max_seconds = 300
        bot.config.max_daily_loss_pct = 0
        bot.config.max_buys_per_hour = 0
        bot.config.loss_streak_pause_threshold = 0
        bot.config.scorecard_verdict_path = "/nonexistent/path.json"
        bot._last_successful_price_at = None
        bot.buy_pause_until_utc = None
        bot.buy_timestamps_utc = []
        bot.consecutive_losses = 0
        bot.daily_anchor_value = 0.0
        bot.daily_anchor_date = None
        return bot


class TestCircuitBreaker:
    def test_no_price_data_yet_allows_buys(self):
        """When no price has ever been fetched, circuit breaker should not block."""
        bot = _make_bot()
        bot._last_successful_price_at = None
        ok, reason = bot._can_open_new_positions(100.0)
        assert ok, f"Should allow when no price yet: {reason}"

    def test_fresh_price_allows_buys(self):
        bot = _make_bot()
        bot._last_successful_price_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        ok, reason = bot._can_open_new_positions(100.0)
        assert ok, f"Fresh price should allow buys: {reason}"

    def test_stale_price_blocks_buys(self):
        bot = _make_bot()
        bot._last_successful_price_at = datetime.now(timezone.utc) - timedelta(seconds=400)
        ok, reason = bot._can_open_new_positions(100.0)
        assert not ok
        assert "Circuit breaker" in reason
        assert "stale" in reason

    def test_exactly_at_threshold_still_ok(self):
        bot = _make_bot()
        # exactly at threshold = not yet stale
        bot._last_successful_price_at = datetime.now(timezone.utc) - timedelta(seconds=299)
        ok, _ = bot._can_open_new_positions(100.0)
        assert ok

    def test_disabled_when_max_seconds_zero(self):
        bot = _make_bot()
        bot.config.price_staleness_max_seconds = 0
        bot._last_successful_price_at = datetime.now(timezone.utc) - timedelta(seconds=9999)
        ok, _ = bot._can_open_new_positions(100.0)
        assert ok, "Circuit breaker disabled when max_seconds=0"

    def test_scorecard_no_go_blocks_even_with_fresh_price(self):
        """NO-GO verdict should still block buys regardless of price freshness."""
        import json
        import tempfile
        import os

        bot = _make_bot()
        bot._last_successful_price_at = datetime.now(timezone.utc) - timedelta(seconds=5)

        verdict = {"verdict": "NO-GO", "reasons": ["drawdown exceeded"]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(verdict, f)
            tmp = f.name

        try:
            bot.config.scorecard_verdict_path = tmp
            ok, reason = bot._can_open_new_positions(100.0)
            assert not ok
            assert "NO-GO" in reason or "Scorecard" in reason
        finally:
            os.unlink(tmp)
