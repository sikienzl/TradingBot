import pandas as pd

from go_no_go_scorecard import (
    _safe_float,
    _max_drawdown_base,
    _evaluate_verdict,
    ScorecardResult,
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
        result = _evaluate_verdict(
            closed_trades=300,
            min_closed_trades=200,
            realized_pnl=500.0,
            win_rate=55.0,
            min_win_rate=45.0,
            profit_factor=2.5,
            min_profit_factor=1.2,
            avg_pnl=1.67,
            min_avg_pnl=1.0,
            max_drawdown_pct=5.0,
            max_allowed_drawdown_pct=10.0,
        )
        assert result.verdict == "GO"
        assert "All defined scorecard criteria met" in result.reasons[0]

    def test_verdict_hard_fail_too_few_trades(self):
        """Test NO-GO for hard fail: too few trades."""
        result = _evaluate_verdict(
            closed_trades=50,
            min_closed_trades=200,
            realized_pnl=500.0,
            win_rate=55.0,
            min_win_rate=45.0,
            profit_factor=2.5,
            min_profit_factor=1.2,
            avg_pnl=1.67,
            min_avg_pnl=1.0,
            max_drawdown_pct=5.0,
            max_allowed_drawdown_pct=10.0,
        )
        assert result.verdict == "NO-GO"
        assert any("Too few closed trades" in r for r in result.reasons)

    def test_verdict_hard_fail_negative_pnl(self):
        """Test NO-GO for hard fail: negative PnL."""
        result = _evaluate_verdict(
            closed_trades=300,
            min_closed_trades=200,
            realized_pnl=-100.0,
            win_rate=45.0,
            min_win_rate=45.0,
            profit_factor=2.5,
            min_profit_factor=1.2,
            avg_pnl=1.67,
            min_avg_pnl=1.0,
            max_drawdown_pct=5.0,
            max_allowed_drawdown_pct=10.0,
        )
        assert result.verdict == "NO-GO"
        assert any("not positive" in r for r in result.reasons)

    def test_verdict_hard_fail_profit_factor_below_1(self):
        """Test NO-GO for hard fail: profit factor < 1."""
        result = _evaluate_verdict(
            closed_trades=300,
            min_closed_trades=200,
            realized_pnl=500.0,
            win_rate=55.0,
            min_win_rate=45.0,
            profit_factor=0.8,
            min_profit_factor=1.2,
            avg_pnl=1.67,
            min_avg_pnl=1.0,
            max_drawdown_pct=5.0,
            max_allowed_drawdown_pct=10.0,
        )
        assert result.verdict == "NO-GO"
        assert any("Profit factor below 1.0" in r for r in result.reasons)

    def test_verdict_hard_fail_excessive_drawdown(self):
        """Test NO-GO for hard fail: excessive drawdown."""
        result = _evaluate_verdict(
            closed_trades=300,
            min_closed_trades=200,
            realized_pnl=500.0,
            win_rate=55.0,
            min_win_rate=45.0,
            profit_factor=2.5,
            min_profit_factor=1.2,
            avg_pnl=1.67,
            min_avg_pnl=1.0,
            max_drawdown_pct=25.0,
            max_allowed_drawdown_pct=10.0,
        )
        assert result.verdict == "NO-GO"
        assert any("significantly too high" in r for r in result.reasons)

    def test_verdict_hold_soft_fail_low_trade_count(self):
        """Test HOLD verdict for soft fail: low trade count."""
        result = _evaluate_verdict(
            closed_trades=150,
            min_closed_trades=200,
            realized_pnl=500.0,
            win_rate=55.0,
            min_win_rate=45.0,
            profit_factor=2.5,
            min_profit_factor=1.2,
            avg_pnl=1.67,
            min_avg_pnl=1.0,
            max_drawdown_pct=5.0,
            max_allowed_drawdown_pct=10.0,
        )
        assert result.verdict == "HOLD"
        assert any("Trade count still too low" in r for r in result.reasons)

    def test_verdict_hold_soft_fail_low_win_rate(self):
        """Test HOLD verdict for soft fail: low win rate."""
        result = _evaluate_verdict(
            closed_trades=300,
            min_closed_trades=200,
            realized_pnl=500.0,
            win_rate=40.0,
            min_win_rate=45.0,
            profit_factor=2.5,
            min_profit_factor=1.2,
            avg_pnl=1.67,
            min_avg_pnl=1.0,
            max_drawdown_pct=5.0,
            max_allowed_drawdown_pct=10.0,
        )
        assert result.verdict == "HOLD"
        assert any("Win rate too low" in r for r in result.reasons)

    def test_verdict_hold_soft_fail_low_profit_factor(self):
        """Test HOLD verdict for soft fail: low profit factor."""
        result = _evaluate_verdict(
            closed_trades=300,
            min_closed_trades=200,
            realized_pnl=500.0,
            win_rate=55.0,
            min_win_rate=45.0,
            profit_factor=1.1,
            min_profit_factor=1.2,
            avg_pnl=1.67,
            min_avg_pnl=1.0,
            max_drawdown_pct=5.0,
            max_allowed_drawdown_pct=10.0,
        )
        assert result.verdict == "HOLD"
        assert any("Profit factor too low" in r for r in result.reasons)

    def test_verdict_hold_soft_fail_high_drawdown(self):
        """Test HOLD verdict for soft fail: high drawdown."""
        result = _evaluate_verdict(
            closed_trades=300,
            min_closed_trades=200,
            realized_pnl=500.0,
            win_rate=55.0,
            min_win_rate=45.0,
            profit_factor=2.5,
            min_profit_factor=1.2,
            avg_pnl=1.67,
            min_avg_pnl=1.0,
            max_drawdown_pct=12.0,
            max_allowed_drawdown_pct=10.0,
        )
        assert result.verdict == "HOLD"
        assert any("Max drawdown too high" in r for r in result.reasons)

    def test_verdict_multiple_soft_failures(self):
        """Test HOLD with multiple soft failures."""
        result = _evaluate_verdict(
            closed_trades=100,
            min_closed_trades=200,
            realized_pnl=50.0,
            win_rate=40.0,
            min_win_rate=45.0,
            profit_factor=1.1,
            min_profit_factor=1.2,
            avg_pnl=0.5,
            min_avg_pnl=1.0,
            max_drawdown_pct=12.0,
            max_allowed_drawdown_pct=10.0,
        )
        assert result.verdict == "HOLD"
        assert len(result.reasons) > 1


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
