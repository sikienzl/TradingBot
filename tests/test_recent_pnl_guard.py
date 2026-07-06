import pandas as pd

from src.trading_bot import _compute_recent_pnl_guard_state


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
