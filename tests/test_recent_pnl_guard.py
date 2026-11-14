import logging
from types import SimpleNamespace

import pandas as pd

from src.trading_bot import CryptoTradingBot, _compute_recent_pnl_guard_state


class TestRecentPnlGuard:
    def test_guard_active_for_negative_recent_window(self):
        rows = []
        for _ in range(70):
            rows.append({"action": "sell", "pnl_base": -0.02})
        for _ in range(50):
            rows.append({"action": "sell", "pnl_base": 0.01})
        df = pd.DataFrame(rows)

        state = _compute_recent_pnl_guard_state(
            df,
            window=120,
            min_trades=60,
            min_realized_pnl=0.0,
            max_profit_factor=1.0,
        )

        assert state["active"] is True
        assert state["recent_trades"] == 120
        assert state["recent_realized_pnl"] < 0.0
        assert state["recent_profit_factor"] <= 1.0

    def test_guard_inactive_for_positive_recent_window(self):
        rows = []
        for _ in range(80):
            rows.append({"action": "sell", "pnl_base": 0.03})
        for _ in range(40):
            rows.append({"action": "sell", "pnl_base": -0.01})
        df = pd.DataFrame(rows)

        state = _compute_recent_pnl_guard_state(
            df,
            window=120,
            min_trades=60,
            min_realized_pnl=0.0,
            max_profit_factor=1.0,
        )

        assert state["active"] is False
        assert state["reason"] == "healthy_recent_window"


def test_guard_status_logs_stale_and_unknown_freshness(caplog):
    bot = CryptoTradingBot.__new__(CryptoTradingBot)
    bot.config = SimpleNamespace(base_currency="EUR", recent_pnl_guard_stale_hours=24)
    with caplog.at_level(logging.INFO):
        bot._log_recent_pnl_guard_status({
            "active": True, "journal_age_hours": 48, "journal_stale": True,
            "latest_closed_trade_at": "2026-11-12T07:00:00Z",
            "reason": "negative_recent_pnl_and_low_pf",
        })
        bot._log_recent_pnl_guard_status({
            "active": False, "reason": "journal_missing",
        })
    assert "journal_age_hours=48.0" in caplog.text
    assert "journal_stale=True, journal_freshness=stale" in caplog.text
    assert "latest_closed_trade=2026-11-12T07:00:00Z" in caplog.text
    assert "guard inactive" in caplog.text
    assert "reason=journal_missing" in caplog.text
    assert "journal_freshness=unknown" in caplog.text
