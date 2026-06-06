from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "daily_dryrun_review.py"
SPEC = importlib.util.spec_from_file_location(
    "daily_dryrun_review", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
daily_dryrun_review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daily_dryrun_review)


def test_read_ai_state_includes_extended_fields(tmp_path: Path) -> None:
    state_file = tmp_path / "ai_state.json"
    state_file.write_text(
        """
{
  "model": "gpt-5.4",
  "monthly_calls": 12,
  "daily_calls": 2,
  "monthly_spend_usd": 0.345,
  "last_run_at": "2026-06-06T10:00:00+00:00",
  "last_suggestion": {
    "changes": {"reentry_cooldown_seconds": 600},
    "mode": "shadow",
    "risk_level": "low"
  },
  "last_error": ""
}
""".strip(),
        encoding="utf-8",
    )

    state = daily_dryrun_review._read_ai_state(str(state_file))

    assert state["available"] is True
    assert state["model"] == "gpt-5.4"
    assert state["monthly_calls"] == 12
    assert state["last_suggestion"]["changes"]["reentry_cooldown_seconds"] == 600


def test_build_html_contains_primary_and_benchmark_sections() -> None:
    report = {
        "generated_at_utc": "2026-06-06T12:00:00Z",
        "lookback_hours": 24,
        "trades": {
            "buys": 3,
            "closed_trades": 2,
            "wins": 1,
            "losses": 1,
            "win_rate_pct": 50.0,
            "realized_pnl": 0.123456,
            "avg_pnl_per_trade": 0.061728,
        },
        "log_activity": {
            "buy_attempts": 6,
            "reentry_blocks": 2,
            "momentum_blocks": 1,
            "reentry_block_ratio": 0.3333,
            "error_lines": 0,
        },
        "ai_copilot": {
            "available": True,
            "model": "gpt-5.4",
            "daily_calls": 2,
            "monthly_calls": 20,
            "monthly_spend_usd": 0.42,
            "consecutive_errors": 0,
            "last_run_at": "2026-06-06T11:00:00Z",
            "last_applied_at": "",
            "last_estimated_cost_usd": 0.0012,
            "last_suggestion": {"changes": {"min_entry_score": 66}, "mode": "shadow", "risk_level": "low", "confidence": 0.8},
            "last_applied_changes": {},
            "last_error": "",
        },
        "ai_benchmark": {
            "available": True,
            "model": "gpt-5-mini",
            "daily_calls": 2,
            "monthly_calls": 20,
            "monthly_spend_usd": 0.08,
            "consecutive_errors": 0,
            "last_run_at": "2026-06-06T11:00:00Z",
            "last_applied_at": "",
            "last_estimated_cost_usd": 0.0004,
            "last_suggestion": {"changes": {"reentry_cooldown_seconds": 600}, "mode": "shadow", "risk_level": "low", "confidence": 0.75},
            "last_applied_changes": {},
            "last_error": "",
        },
    }

    html_report = daily_dryrun_review._build_html(report)

    assert "Daily Dry-Run Review" in html_report
    assert "AI Copilot Comparison" in html_report
    assert "gpt-5.4" in html_report
    assert "gpt-5-mini" in html_report
    assert "reentry_cooldown_seconds" in html_report
