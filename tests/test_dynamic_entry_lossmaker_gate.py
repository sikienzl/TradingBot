import pandas as pd

from src.trading_bot import _compute_dynamic_lossmaker_entry_pairs


class TestDynamicLossmakerEntryPairs:
    def test_blocks_recent_btc_rules_pair_but_not_positive_eth_rules_pair(self):
        df = pd.DataFrame(
            [
                {
                    "action": "sell",
                    "coin": "BTC",
                    "signal_source": "rules",
                    "pnl_base": -0.12,
                    "hold_seconds": 303,
                    "reason": "⏰ MAX-HOLD-TIME reached (303s >= 300s)",
                },
                {
                    "action": "sell",
                    "coin": "BTC",
                    "signal_source": "rules",
                    "pnl_base": -0.08,
                    "hold_seconds": 304,
                    "reason": "⏰ MAX-HOLD-TIME reached (304s >= 300s)",
                },
                {
                    "action": "sell",
                    "coin": "BTC",
                    "signal_source": "rules",
                    "pnl_base": -0.05,
                    "hold_seconds": 302,
                    "reason": "⏰ MAX-HOLD-TIME reached (302s >= 300s)",
                },
                {
                    "action": "sell",
                    "coin": "ETH",
                    "signal_source": "rules",
                    "pnl_base": 0.06,
                    "hold_seconds": 180,
                    "reason": "🎉 ATR-TAKE-PROFIT (TP: 2058.0)",
                },
                {
                    "action": "sell",
                    "coin": "ETH",
                    "signal_source": "rules",
                    "pnl_base": 0.04,
                    "hold_seconds": 190,
                    "reason": "🎉 ATR-TAKE-PROFIT (TP: 2059.0)",
                },
                {
                    "action": "sell",
                    "coin": "ETH",
                    "signal_source": "rules",
                    "pnl_base": 0.03,
                    "hold_seconds": 200,
                    "reason": "🎉 ATR-TAKE-PROFIT (TP: 2060.0)",
                },
            ]
        )

        blocked = _compute_dynamic_lossmaker_entry_pairs(
            df,
            window=10,
            min_sells=3,
            max_win_rate_pct=45.0,
            min_pnl_loss=0.003,
            min_max_hold_exit_ratio=0.5,
            max_avg_hold_seconds=120.0,
        )

        assert ("BTC", "rules") in blocked
        assert ("ETH", "rules") not in blocked
