import pandas as pd

from src.api_models import ScorecardMetrics, ScorecardThresholds
from src.go_no_go_scorecard import (
    ScorecardResult,
    ScorecardVerdict,
    _compute_metrics,
    _evaluate_verdict,
    _max_drawdown_base,
    _safe_float,
)


class TestSafeFloat:
    """Tests for _safe_float helper function."""

    def test_safe_float_converts_numeric_strings(self):
        """Test conversion of numeric strings."""
        series = pd.Series(["1.5", "2.0", "3.5"])
        result = _safe_float(series)
        expected = pd.Series([1.5, 2.0, 3.5])
        pd.testing.assert_series_equal(result, expected)

    def test_safe_float_fills_nan_with_zero(self):
        """Test that NaN values are filled with 0."""
        series = pd.Series(["1.5", "invalid", "3.5"])
        result = _safe_float(series)
        assert result.iloc[0] == 1.5
        assert result.iloc[1] == 0.0
        assert result.iloc[2] == 3.5

    def test_safe_float_handles_empty_series(self):
        """Test handling of empty series."""
        series = pd.Series([], dtype=float)
        result = _safe_float(series)
        assert len(result) == 0


class TestMaxDrawdownBase:
    """Tests for _max_drawdown_base helper function."""

    def test_max_drawdown_empty_series(self):
        """Test max drawdown with empty series."""
        series = pd.Series([], dtype=float)
        result = _max_drawdown_base(series)
        assert result == 0.0

    def test_max_drawdown_positive_series(self):
        """Test max drawdown with only positive values."""
        # Growing equity curve: [100, 110, 120, 130]
        series = pd.Series([100.0, 110.0, 120.0, 130.0])
        result = _max_drawdown_base(series)
        assert result == 0.0

    def test_max_drawdown_with_decline(self):
        """Test max drawdown calculation with decline."""
        # Equity: [100, 120, 110, 90]
        # Running max: [100, 120, 120, 120]
        # Drawdown: [0, 0, -10, -30]
        series = pd.Series([100.0, 120.0, 110.0, 90.0])
        result = _max_drawdown_base(series)
        assert result == -30.0

    def test_max_drawdown_single_value(self):
        """Test max drawdown with single value."""
        series = pd.Series([100.0])
        result = _max_drawdown_base(series)
        assert result == 0.0


class TestEvaluateVerdict:
    """Tests for _evaluate_verdict function."""

    def test_verdict_go_all_criteria_met(self):
        """Test GO verdict when all criteria are met."""
        # Build parameters using reflection approach
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=500.0,
            win_rate=55.0,
            profit_factor=2.5,
            avg_pnl=1.67,
            max_drawdown_pct=5.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == ScorecardVerdict.GO
        assert "All defined scorecard criteria met" in result.reasons[0]

    def test_verdict_hard_fail_too_few_trades(self):
        """Test NO-GO for hard fail: too few trades."""
        metrics = ScorecardMetrics(
            closed_trades=50,
            realized_pnl=500.0,
            win_rate=55.0,
            profit_factor=2.5,
            avg_pnl=1.67,
            max_drawdown_pct=5.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == ScorecardVerdict.NO_GO
        assert any("Too few closed trades" in r for r in result.reasons)

    def test_verdict_hard_fail_negative_pnl(self):
        """Test NO-GO for hard fail: negative PnL."""
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=-50.0,
            win_rate=55.0,
            profit_factor=2.5,
            avg_pnl=1.67,
            max_drawdown_pct=5.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == ScorecardVerdict.NO_GO
        assert any("Realized PnL not positive" in r for r in result.reasons)

    def test_verdict_hard_fail_low_profit_factor(self):
        """Test NO-GO for hard fail: profit factor below 1.0."""
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=500.0,
            win_rate=55.0,
            profit_factor=0.8,
            avg_pnl=1.67,
            max_drawdown_pct=5.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == ScorecardVerdict.NO_GO
        assert any("Profit factor below 1.0" in r for r in result.reasons)

    def test_verdict_hard_fail_high_drawdown(self):
        """Test NO-GO for hard fail: max drawdown too high."""
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=500.0,
            win_rate=55.0,
            profit_factor=2.5,
            avg_pnl=1.67,
            max_drawdown_pct=16.0,  # This exceeds the hard-fail threshold (> 10.0 * 1.5 = 15.0)
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == ScorecardVerdict.NO_GO
        assert any("Max drawdown significantly too high" in r for r in result.reasons)

    def test_verdict_hold_low_trade_count(self):
        """Test HOLD verdict when trade count is low but other criteria met."""
        metrics = ScorecardMetrics(
            closed_trades=150,  # Below threshold of 200
            realized_pnl=500.0,
            win_rate=55.0,
            profit_factor=2.5,
            avg_pnl=1.67,
            max_drawdown_pct=5.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == ScorecardVerdict.HOLD
        assert any("Trade count still too low" in r for r in result.reasons)

    def test_verdict_hold_low_win_rate(self):
        """Test HOLD verdict when win rate is too low."""
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=500.0,
            win_rate=40.0,  # Below threshold of 45.0
            profit_factor=2.5,
            avg_pnl=1.67,
            max_drawdown_pct=5.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == ScorecardVerdict.HOLD
        assert any("Win rate too low" in r for r in result.reasons)

    def test_verdict_hold_low_profit_factor(self):
        """Test HOLD verdict when profit factor is too low."""
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=500.0,
            win_rate=55.0,
            profit_factor=1.0,  # Below threshold of 1.2
            avg_pnl=1.67,
            max_drawdown_pct=5.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == ScorecardVerdict.HOLD
        assert any("Profit factor too low" in r for r in result.reasons)

    def test_verdict_hold_low_avg_pnl(self):
        """Test HOLD verdict when average PnL is too low."""
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=500.0,
            win_rate=55.0,
            profit_factor=2.5,
            avg_pnl=0.5,  # Below threshold of 1.0
            max_drawdown_pct=5.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == ScorecardVerdict.HOLD
        assert any("Avg PnL/trade too low" in r for r in result.reasons)

    def test_verdict_hold_high_drawdown(self):
        """Test HOLD verdict when max drawdown is high but not hard-fail level."""
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=500.0,
            win_rate=55.0,
            profit_factor=2.5,
            avg_pnl=1.67,
            max_drawdown_pct=12.0,  # This is above threshold but not hard-fail level
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == ScorecardVerdict.HOLD
        assert any("Max drawdown too high" in r for r in result.reasons)

    def test_verdict_hard_fail_negative_pnl_large_loss(self):
        """Test NO-GO for hard fail: negative PnL with large loss."""
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=-100.0,
            win_rate=45.0,
            profit_factor=2.5,
            avg_pnl=1.67,
            max_drawdown_pct=5.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == "NO-GO"
        assert any("not positive" in r for r in result.reasons)

    def test_verdict_hard_fail_profit_factor_below_1(self):
        """Test NO-GO for hard fail: profit factor < 1."""
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=500.0,
            win_rate=55.0,
            profit_factor=0.8,
            avg_pnl=1.67,
            max_drawdown_pct=5.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == "NO-GO"
        assert any("Profit factor below 1.0" in r for r in result.reasons)

    def test_verdict_hard_fail_excessive_drawdown(self):
        """Test NO-GO for hard fail: excessive drawdown."""
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=500.0,
            win_rate=55.0,
            profit_factor=2.5,
            avg_pnl=1.67,
            max_drawdown_pct=25.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == "NO-GO"
        assert any("significantly too high" in r for r in result.reasons)

    def test_verdict_hold_soft_fail_low_trade_count(self):
        """Test HOLD verdict for soft fail: low trade count."""
        metrics = ScorecardMetrics(
            closed_trades=150,
            realized_pnl=500.0,
            win_rate=55.0,
            profit_factor=2.5,
            avg_pnl=1.67,
            max_drawdown_pct=5.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == "HOLD"
        assert any("Trade count still too low" in r for r in result.reasons)

    def test_verdict_hold_soft_fail_low_win_rate(self):
        """Test HOLD verdict for soft fail: low win rate."""
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=500.0,
            win_rate=40.0,
            profit_factor=2.5,
            avg_pnl=1.67,
            max_drawdown_pct=5.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == "HOLD"
        assert any("Win rate too low" in r for r in result.reasons)

    def test_verdict_hold_soft_fail_low_profit_factor(self):
        """Test HOLD verdict for soft fail: low profit factor."""
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=500.0,
            win_rate=55.0,
            profit_factor=1.1,
            avg_pnl=1.67,
            max_drawdown_pct=5.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == "HOLD"
        assert any("Profit factor too low" in r for r in result.reasons)

    def test_verdict_hold_soft_fail_high_drawdown(self):
        """Test HOLD verdict for soft fail: high drawdown."""
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=500.0,
            win_rate=55.0,
            profit_factor=2.5,
            avg_pnl=1.67,
            max_drawdown_pct=12.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == "HOLD"
        assert any("Max drawdown too high" in r for r in result.reasons)

    def test_verdict_hold_soft_fail_recent_pnl(self):
        """Test HOLD verdict when recent window PnL is below threshold."""
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=500.0,
            win_rate=55.0,
            profit_factor=2.5,
            avg_pnl=1.67,
            max_drawdown_pct=5.0,
            recent_closed_trades=100,
            recent_realized_pnl=-0.2,
            recent_win_rate=55.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == "HOLD"
        assert any("Recent PnL too low" in r for r in result.reasons)

    def test_verdict_hold_soft_fail_catboost_underperformance(self):
        """Test HOLD verdict when CatBoost trails rules beyond allowed delta."""
        metrics = ScorecardMetrics(
            closed_trades=300,
            realized_pnl=500.0,
            win_rate=55.0,
            profit_factor=2.5,
            avg_pnl=1.67,
            max_drawdown_pct=5.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=-0.3,
            rules_closed_trades=140,
            rules_realized_pnl=0.4,
            catboost_vs_rules_pnl_delta=-0.7,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == "HOLD"
        assert any("CatBoost underperforms rules" in r for r in result.reasons)

    def test_verdict_multiple_soft_failures(self):
        """Test HOLD with multiple soft failures."""
        metrics = ScorecardMetrics(
            closed_trades=100,
            realized_pnl=50.0,
            win_rate=40.0,
            profit_factor=1.1,
            avg_pnl=0.5,
            max_drawdown_pct=12.0,
            gross_profit=500.0,
            gross_loss=100.0,
            max_drawdown_base=-1.0,
            recent_closed_trades=100,
            recent_realized_pnl=50.0,
            recent_win_rate=55.0,
            catboost_closed_trades=120,
            catboost_realized_pnl=2.0,
            rules_closed_trades=130,
            rules_realized_pnl=1.5,
            catboost_vs_rules_pnl_delta=0.5,
        )
        thresholds = ScorecardThresholds(
            min_closed_trades=200,
            min_win_rate=45.0,
            min_profit_factor=1.2,
            min_avg_pnl=1.0,
            max_drawdown_pct=10.0,
            recent_trades_window=100,
            min_recent_realized_pnl=0.0,
            min_recent_win_rate=45.0,
            min_catboost_vs_rules_pnl_delta=-0.05,
            min_source_trades_for_delta=50,
            starting_capital=20.0,
            lookback_days=30,
        )
        
        result = _evaluate_verdict(metrics=metrics, thresholds=thresholds)
        assert result.verdict == "HOLD"
        assert len(result.reasons) > 1


class TestComputeMetrics:
    """Tests for _compute_metrics helper function."""

    def test_compute_metrics_with_closed_trades(self):
        df = pd.DataFrame([
            {"action": "buy", "pnl_base": 0.0},
            {"action": "sell", "pnl_base": 2.0},
            {"action": "sell", "pnl_base": -1.0},
            {"action": "sell", "pnl_base": 3.0},
        ])

        result = _compute_metrics(df, starting_capital=20.0)

        assert result.closed_trades == 3
        assert result.realized_pnl == 4.0
        assert result.avg_pnl == 4.0 / 3.0
        assert result.win_rate == (2.0 / 3.0) * 100.0
        assert result.gross_profit == 5.0
        assert result.gross_loss == 1.0
        assert result.profit_factor == 5.0
        assert result.max_drawdown_base == -1.0
        assert result.max_drawdown_pct == 5.0
        assert result.recent_closed_trades == 3
        assert result.recent_realized_pnl == 4.0
        assert result.recent_win_rate == (2.0 / 3.0) * 100.0

    def test_compute_metrics_without_closed_trades(self):
        df = pd.DataFrame([
            {"action": "buy", "pnl_base": 0.0},
        ])

        result = _compute_metrics(df, starting_capital=20.0)

        assert result.closed_trades == 0
        assert result.realized_pnl == 0.0
        assert result.avg_pnl == 0.0
        assert result.win_rate == 0.0
        assert result.gross_profit == 0.0
        assert result.gross_loss == 0.0
        assert result.max_drawdown_base == 0.0
        assert result.max_drawdown_pct == 0.0
        assert result.recent_closed_trades == 0
        assert result.catboost_closed_trades == 0
        assert result.rules_closed_trades == 0


class TestScorecardResultDataclass:
    """Tests for ScorecardResult dataclass."""

    def test_scorecard_result_creation(self):
        """Test ScorecardResult creation."""
        result = ScorecardResult(
            verdict="GO",
            reasons=["Test reason"]
        )
        assert result.verdict == "GO"
        assert len(result.reasons) == 1
        assert result.reasons[0] == "Test reason"
