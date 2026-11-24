"""Tests for the Prometheus exposition formatter.

The formatter is pure, so the tests pin the exact wire format: the metric
families, the HELP/TYPE headers, label escaping and the conditional snapshot
freshness gauges that only the HTTP exporter emits.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime

import pytest

from src.export_pnl_metrics import read_ai_copilot_usage
from src.pnl_prometheus_format import escape_label_value, format_prometheus_metrics


def _trade_metrics() -> dict:
    return {
        "all_time_realized_pnl": 12.5,
        "total_realized_pnl": -3.25,
        "total_trades": 7,
        "winning_trades": 3,
        "losing_trades": 3,
        "break_even_trades": 1,
        "win_rate": 42.857,
        "avg_pnl_per_trade": -0.4642,
        "profit_factor": 1.234,
        "sharpe_ratio": 0.0,
        "max_drawdown_usd": 9.0,
        "max_drawdown_pct": 12.5,
        "coin_pnl": {"ETH": 1.5, "BTC": -4.75},
        "coin_closed": {"ETH": 3, "BTC": 3},
        "coin_win_rate": {"ETH": 66.6, "BTC": 0.0},
        "coin_break_even": {"ETH": 0, "BTC": 0},
        "coin_buy_volume": {"ETH": 100.0},
        "coin_buy_count": {"ETH": 2},
    }


def _samples(text: str) -> dict[str, str]:
    """Map every sample line to its value, ignoring HELP/TYPE lines."""
    samples = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition(" ")
        samples[name] = value
    return samples


def test_trade_gauges_carry_help_and_type_headers():
    text = format_prometheus_metrics(_trade_metrics())

    assert (
        "# HELP trading_realized_pnl_usd_all_time Total realized PnL in USD (all-time)\n"
        "# TYPE trading_realized_pnl_usd_all_time gauge\n"
        "trading_realized_pnl_usd_all_time 12.5" in text
    )
    samples = _samples(text)
    assert samples["trading_realized_pnl_usd"] == "-3.25"
    assert samples["trading_closed_trades"] == "7"
    assert samples["trading_win_rate"] == "42.857"
    assert samples["trading_max_drawdown_pct"] == "12.5"


def test_per_coin_series_are_sorted_and_labelled():
    text = format_prometheus_metrics(_trade_metrics())

    coin_lines = [
        line for line in text.splitlines() if line.startswith("trading_coin_realized_pnl_usd{")
    ]
    assert coin_lines == [
        'trading_coin_realized_pnl_usd{coin="BTC"} -4.75',
        'trading_coin_realized_pnl_usd{coin="ETH"} 1.5',
    ]
    assert 'trading_coin_buy_count{coin="ETH"} 2' in text


def test_portfolio_defaults_are_zero_without_snapshot_keys():
    samples = _samples(format_prometheus_metrics(_trade_metrics()))

    assert samples["trading_portfolio_value_eur"] == "0.0"
    assert samples["trading_portfolio_cash_eur"] == "0.0"
    assert samples["trading_portfolio_start_value_eur"] == "0.0"
    assert samples["trading_open_positions_count"] == "0"


def test_snapshot_freshness_gauges_only_when_the_exporter_attaches_them():
    without = format_prometheus_metrics(_trade_metrics())
    with_freshness = format_prometheus_metrics(
        {
            **_trade_metrics(),
            "metrics_generated_unixtime": 1000.0,
            "portfolio_snapshot_timestamp_unixtime": 970.0,
            "portfolio_snapshot_age_seconds": 30.0,
        }
    )

    for name in (
        "trading_metrics_generated_unixtime",
        "trading_portfolio_snapshot_unixtime",
        "trading_portfolio_snapshot_age_seconds",
    ):
        assert name not in without
        assert name in with_freshness

    assert _samples(with_freshness)["trading_portfolio_snapshot_age_seconds"] == "30.0"


def test_unrealized_pnl_total_sums_the_per_coin_series():
    text = format_prometheus_metrics(
        {
            **_trade_metrics(),
            "holdings_unrealized_pnl_eur": {"ETH": 10.5, "BTC": -2.25},
        }
    )

    assert (
        "trading_current_holdings_unrealized_pnl_total_eur 8.25" in text
    )
    assert 'trading_current_holding_unrealized_pnl_eur{coin="BTC"} -2.25' in text


def test_shadow_suggestions_emit_value_confidence_and_age_series():
    metrics = {
        **_trade_metrics(),
        "ai_copilot_shadow_suggestions": [
            {
                "rank": 1,
                "suggestion_id": "1_min_entry_score",
                "mode": "shadow",
                "parameter": "min_entry_score",
                "value": 55.0,
                "confidence": 0.72,
                "age_minutes": 12.5,
            }
        ],
    }

    text = format_prometheus_metrics(metrics)

    assert (
        'trading_ai_copilot_shadow_suggestion_value{suggestion_id="1_min_entry_score",'
        'mode="shadow",parameter="min_entry_score",rank="1"} 55.0' in text
    )
    assert (
        'trading_ai_copilot_shadow_suggestion_confidence{suggestion_id="1_min_entry_score",'
        'mode="shadow",parameter="min_entry_score",rank="1"} 0.72' in text
    )
    assert (
        'trading_ai_copilot_shadow_suggestion_age_minutes{suggestion_id="1_min_entry_score",'
        'mode="shadow",parameter="min_entry_score",rank="1"} 12.5' in text
    )


def test_shadow_suggestion_series_absent_when_no_suggestions():
    text = format_prometheus_metrics({**_trade_metrics(), "ai_copilot_shadow_suggestions": []})

    for name in (
        "trading_ai_copilot_shadow_suggestion_value",
        "trading_ai_copilot_shadow_suggestion_confidence",
        "trading_ai_copilot_shadow_suggestion_age_minutes",
    ):
        assert f"# TYPE {name} gauge" in text
        assert not any(line.startswith(f"{name}{{") for line in text.splitlines())


def test_recent_pnl_guard_reason_is_a_labelled_gauge_set_to_one():
    metrics = {
        **_trade_metrics(),
        "recent_pnl_guard_enabled": 1.0,
        "recent_pnl_guard_active": 1.0,
        "recent_pnl_guard_window": 120.0,
        "recent_pnl_guard_min_trades": 60.0,
        "recent_pnl_guard_recent_closed_trades": 61.0,
        "recent_pnl_guard_recent_realized_pnl": -2.5,
        "recent_pnl_guard_recent_profit_factor": 0.8,
        "recent_pnl_guard_min_realized_pnl": 0.0,
        "recent_pnl_guard_max_profit_factor": 1.0,
        "recent_pnl_guard_reason": "negative_recent_pnl_and_low_pf",
    }

    text = format_prometheus_metrics(metrics)

    assert (
        'trading_recent_pnl_guard_reason{reason="negative_recent_pnl_and_low_pf"} 1' in text
    )
    samples = _samples(text)
    assert samples["trading_recent_pnl_guard_active"] == "1.0"
    assert samples["trading_recent_pnl_guard_recent_profit_factor"] == "0.8"


def test_recent_pnl_guard_reason_defaults_to_unknown():
    text = format_prometheus_metrics(_trade_metrics())

    assert 'trading_recent_pnl_guard_reason{reason="unknown"} 1' in text


def test_escape_label_value_escapes_backslash_quote_and_newline():
    assert escape_label_value('say "hi"') == 'say \\"hi\\"'
    assert escape_label_value("a\\b") == "a\\\\b"
    assert escape_label_value("line\nbreak") == "line\\nbreak"
    assert escape_label_value(7) == "7"


def test_guard_reason_with_quotes_is_escaped_in_the_sample_line():
    text = format_prometheus_metrics(
        {**_trade_metrics(), "recent_pnl_guard_reason": 'odd "value"'}
    )

    assert 'trading_recent_pnl_guard_reason{reason="odd \\"value\\""} 1' in text


def test_non_finite_values_are_rendered_as_prometheus_literals():
    samples = _samples(format_prometheus_metrics({**_trade_metrics(), "profit_factor": math.inf}))

    assert samples["trading_profit_factor"] == "inf"


def test_every_sample_line_has_a_matching_type_header():
    text = format_prometheus_metrics(
        {
            **_trade_metrics(),
            "holdings_value_eur": {"ETH": 1.0},
            "ai_copilot_shadow_suggestions": [
                {
                    "rank": 1,
                    "suggestion_id": "1_x",
                    "mode": "unchanged",
                    "parameter": "x",
                    "value": 1.0,
                    "confidence": 0.5,
                    "age_minutes": 1.0,
                }
            ],
        }
    )
    declared = {
        line.split()[2] for line in text.splitlines() if line.startswith("# TYPE ")
    }
    emitted = {line.split("{")[0].split(" ")[0] for line in text.splitlines() if line and not line.startswith("#")}

    assert emitted - declared == set()


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n"
        "AI_COPILOT_MAX_BUDGET_USD_PER_MONTH=5.5\n"
        "AI_COPILOT_MAX_CALLS_PER_MONTH=40\n",
        encoding="utf-8",
    )
    return path


def _state(tmp_path, name, month_key, **fields):
    path = tmp_path / name
    path.write_text(json.dumps({"month_key": month_key, **fields}), encoding="utf-8")
    return path


def _current_month_key() -> str:
    now = datetime.now(UTC)
    return f"{now.year:04d}-{now.month:02d}"


def test_ai_usage_reads_caps_from_the_env_file(env_file, tmp_path):
    result = read_ai_copilot_usage(env_file, tmp_path / "missing.json")

    assert result["ai_copilot_budget_cap_usd"] == 5.5
    assert result["ai_copilot_calls_cap_monthly"] == 40.0
    assert result["ai_copilot_budget_used_usd"] == 0.0
    assert result["ai_copilot_calls_used_monthly"] == 0.0


def test_ai_usage_state_overrides_caps_and_usage_for_the_current_month(env_file, tmp_path):
    state = _state(
        tmp_path,
        "ai_copilot_state.json",
        _current_month_key(),
        budget_cap_usd=9.0,
        calls_cap_monthly=99,
        monthly_spend_usd=3.75,
        monthly_calls=12,
    )

    result = read_ai_copilot_usage(env_file, state)

    assert result["ai_copilot_budget_cap_usd"] == 9.0
    assert result["ai_copilot_calls_cap_monthly"] == 99.0
    assert result["ai_copilot_budget_used_usd"] == 3.75
    assert result["ai_copilot_calls_used_monthly"] == 12.0


def test_ai_usage_ignores_state_from_a_stale_month(env_file, tmp_path):
    state = _state(
        tmp_path,
        "ai_copilot_state.json",
        "2000-01",
        monthly_spend_usd=3.75,
        monthly_calls=12,
    )

    result = read_ai_copilot_usage(env_file, state)

    assert result["ai_copilot_budget_used_usd"] == 0.0
    assert result["ai_copilot_calls_used_monthly"] == 0.0


def test_ai_usage_falls_back_to_the_backup_state_file(env_file, tmp_path):
    backup = _state(
        tmp_path,
        "ai_copilot_state.json.bak",
        _current_month_key(),
        monthly_spend_usd=1.5,
        monthly_calls=4,
    )

    result = read_ai_copilot_usage(
        env_file, tmp_path / "ai_copilot_state.json", backup_state_path=backup
    )

    assert result["ai_copilot_budget_used_usd"] == 1.5
    assert result["ai_copilot_calls_used_monthly"] == 4.0


def test_ai_usage_prefers_the_primary_state_over_the_backup(env_file, tmp_path):
    primary = _state(
        tmp_path, "ai_copilot_state.json", _current_month_key(), monthly_calls=1
    )
    backup = _state(
        tmp_path, "ai_copilot_state.json.bak", _current_month_key(), monthly_calls=99
    )

    result = read_ai_copilot_usage(env_file, primary, backup_state_path=backup)

    assert result["ai_copilot_calls_used_monthly"] == 1.0


def test_ai_usage_without_backup_argument_never_reads_the_bak_file(env_file, tmp_path):
    _state(tmp_path, "ai_copilot_state.json.bak", _current_month_key(), monthly_calls=99)

    result = read_ai_copilot_usage(env_file, tmp_path / "ai_copilot_state.json")

    assert result["ai_copilot_calls_used_monthly"] == 0.0


def test_ai_usage_quiet_suppresses_state_diagnostics(tmp_path, capsys):
    broken = tmp_path / "ai_copilot_state.json"
    broken.write_text("{not json", encoding="utf-8")

    read_ai_copilot_usage(tmp_path / "missing.env", broken, quiet=True)
    assert capsys.readouterr().err == ""

    read_ai_copilot_usage(tmp_path / "missing.env", broken)
    assert "Error reading AI co-pilot state" in capsys.readouterr().err


def test_ai_usage_tolerates_malformed_env_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text("AI_COPILOT_MAX_BUDGET_USD_PER_MONTH=not-a-number\n", encoding="utf-8")

    result = read_ai_copilot_usage(env, tmp_path / "missing.json")

    assert result["ai_copilot_budget_cap_usd"] == 0.0
