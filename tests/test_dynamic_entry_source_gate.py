import pandas as pd

from src.trading_bot import _compute_degraded_entry_sources


class TestDynamicEntrySourceGate:
    def test_marks_rules_as_degraded_for_recent_negative_short_hold_churn(self):
        df = pd.DataFrame(
            [
                {
                    "action": "sell",
                    "coin": "BTC",
                    "signal_source": "rules",
                    "pnl_base": -0.11,
                    "hold_seconds": 14,
                },
                {
                    "action": "sell",
                    "coin": "ETH",
                    "signal_source": "rules",
                    "pnl_base": -0.07,
                    "hold_seconds": 18,
                },
                {
                    "action": "sell",
                    "coin": "SOL",
                    "signal_source": "rules",
                    "pnl_base": -0.06,
                    "hold_seconds": 20,
                },
                {
                    "action": "sell",
                    "coin": "TRX",
                    "signal_source": "catboost",
                    "pnl_base": 0.05,
                    "hold_seconds": 320,
                },
                {
                    "action": "sell",
                    "coin": "XLM",
                    "signal_source": "catboost",
                    "pnl_base": -0.01,
                    "hold_seconds": 300,
                },
                {
                    "action": "sell",
                    "coin": "LINK",
                    "signal_source": "catboost",
                    "pnl_base": 0.03,
                    "hold_seconds": 280,
                },
            ]
        )

        degraded = _compute_degraded_entry_sources(
            df,
            window=20,
            min_sells=3,
            min_pnl_loss=0.01,
            max_profit_factor=1.0,
            max_avg_hold_seconds=120.0,
        )

        assert "rules" in degraded
        assert "catboost" not in degraded
