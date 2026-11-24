"""Unit tests for the persistent state helpers in ``src.state_store``."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from src.state_store import (
    cooldown_remaining_minutes,
    current_day_key,
    current_month_key,
    normalize_ai_state,
    read_ai_copilot_state,
    read_auto_tune_state,
    write_ai_copilot_state,
    write_auto_tune_state,
)

# ---------------------------------------------------------------------------
# auto-tune state
# ---------------------------------------------------------------------------

def test_read_auto_tune_state_missing_or_empty_path(tmp_path) -> None:
    assert read_auto_tune_state(None) == {}
    assert read_auto_tune_state("") == {}
    assert read_auto_tune_state(str(tmp_path / "missing.json")) == {}


def test_auto_tune_state_roundtrip(tmp_path) -> None:
    path = tmp_path / "auto_tune.json"
    write_auto_tune_state({"last_applied": 0.6}, str(path))
    assert read_auto_tune_state(str(path)) == {"last_applied": 0.6}


def test_read_auto_tune_state_invalid_json_returns_empty(tmp_path) -> None:
    path = tmp_path / "auto_tune.json"
    path.write_text("{not json")
    assert read_auto_tune_state(str(path)) == {}


def test_write_auto_tune_state_failure_is_logged_not_raised(
    tmp_path, caplog
) -> None:
    with caplog.at_level("WARNING"):
        write_auto_tune_state({"x": object()}, str(tmp_path / "state.json"))
    assert "Auto-tune state could not be saved" in caplog.text


# ---------------------------------------------------------------------------
# cooldown
# ---------------------------------------------------------------------------

def test_cooldown_zero_when_disabled_or_no_timestamp() -> None:
    assert cooldown_remaining_minutes({"last_applied_at": "x"}, 0) == 0.0
    assert cooldown_remaining_minutes({}, 60) == 0.0


def test_cooldown_counts_down_from_last_applied() -> None:
    recent = datetime.now(UTC) - timedelta(minutes=10)
    state = {"last_applied_at": recent.isoformat()}
    remaining = cooldown_remaining_minutes(state, 60)
    assert 49.9 < remaining <= 50.1


def test_cooldown_treats_naive_timestamps_as_utc() -> None:
    recent = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)
    state = {"last_applied_at": recent.isoformat()}
    remaining = cooldown_remaining_minutes(state, 60)
    assert 29.9 < remaining <= 30.1


def test_cooldown_expired_or_broken_timestamp_is_zero() -> None:
    old = datetime.now(UTC) - timedelta(hours=5)
    assert cooldown_remaining_minutes(
        {"last_applied_at": old.isoformat()}, 60) == 0.0
    assert cooldown_remaining_minutes({"last_applied_at": 42}, 60) == 0.0
    assert cooldown_remaining_minutes({"last_applied_at": "nope"}, 60) == 0.0


# ---------------------------------------------------------------------------
# AI co-pilot state
# ---------------------------------------------------------------------------

def test_read_ai_copilot_state_missing_or_empty_path(tmp_path) -> None:
    assert read_ai_copilot_state(None) == {}
    assert read_ai_copilot_state(str(tmp_path / "missing.json")) == {}


def test_ai_copilot_state_roundtrip(tmp_path) -> None:
    path = tmp_path / "ai_state.json"
    write_ai_copilot_state({"monthly_calls": 2}, str(path))
    assert read_ai_copilot_state(str(path)) == {"monthly_calls": 2}


def test_write_ai_copilot_state_creates_backup_of_previous(tmp_path) -> None:
    path = tmp_path / "ai_state.json"
    write_ai_copilot_state({"v": 1}, str(path))
    write_ai_copilot_state({"v": 2}, str(path))
    backup = json.loads((tmp_path / "ai_state.json.bak").read_text())
    assert backup == {"v": 1}
    assert read_ai_copilot_state(str(path)) == {"v": 2}
    assert not (tmp_path / "ai_state.json.tmp").exists()


def test_read_ai_copilot_state_recovers_from_backup(
    tmp_path, caplog
) -> None:
    path = tmp_path / "ai_state.json"
    (tmp_path / "ai_state.json.bak").write_text(json.dumps({"v": 7}))
    path.write_text("{corrupt")
    with caplog.at_level("WARNING"):
        state = read_ai_copilot_state(str(path))
    assert state == {"v": 7}
    assert "Recovered AI co-pilot state from backup file" in caplog.text


def test_read_ai_copilot_state_logs_load_failure(tmp_path, caplog) -> None:
    path = tmp_path / "ai_state.json"
    path.write_text("{corrupt")
    with caplog.at_level("WARNING"):
        assert read_ai_copilot_state(str(path)) == {}
    assert "AI co-pilot state could not be loaded" in caplog.text


def test_write_ai_copilot_state_cleans_tmp_on_failure(
    tmp_path, monkeypatch, caplog
) -> None:
    def boom(*args):
        raise OSError("disk")

    monkeypatch.setattr("src.state_store.os.replace", boom)
    path = tmp_path / "ai_state.json"
    with caplog.at_level("WARNING"):
        write_ai_copilot_state({"v": 1}, str(path))
    assert "AI co-pilot state could not be saved" in caplog.text
    assert not (tmp_path / "ai_state.json.tmp").exists()
    assert not path.exists()


# ---------------------------------------------------------------------------
# period keys / quota normalisation
# ---------------------------------------------------------------------------

def test_period_keys_match_today_utc() -> None:
    now = datetime.now(UTC)
    assert current_month_key() == f"{now.year:04d}-{now.month:02d}"
    assert current_day_key(
    ) == f"{now.year:04d}-{now.month:02d}-{now.day:02d}"


def test_normalize_ai_state_resets_stale_periods() -> None:
    state = {
        "month_key": "2000-01",
        "day_key": "2000-01-01",
        "monthly_calls": 9,
        "monthly_spend_usd": 4.2,
        "daily_calls": 5,
        "consecutive_errors": 3,
        "last_suspended_at": "2000-01-01T00:00:00+00:00",
    }
    result = normalize_ai_state(state, monthly_call_cap=30, budget_cap_usd=2.0)
    assert result["month_key"] == current_month_key()
    assert result["day_key"] == current_day_key()
    assert result["monthly_calls"] == 0
    assert result["monthly_spend_usd"] == 0.0
    assert result["daily_calls"] == 0
    assert result["consecutive_errors"] == 0
    assert "last_suspended_at" not in result
    assert result["calls_cap_monthly"] == 30
    assert result["budget_cap_usd"] == 2.0


def test_normalize_ai_state_keeps_current_period_counters() -> None:
    state = {
        "month_key": current_month_key(),
        "day_key": current_day_key(),
        "monthly_calls": 4,
        "monthly_spend_usd": 0.75,
        "daily_calls": 2,
        "consecutive_errors": 1,
    }
    result = normalize_ai_state(state, monthly_call_cap=10, budget_cap_usd=1.0)
    assert result["monthly_calls"] == 4
    assert result["monthly_spend_usd"] == 0.75
    assert result["daily_calls"] == 2
    assert result["consecutive_errors"] == 1


def test_normalize_ai_state_fills_missing_counters() -> None:
    result = normalize_ai_state({}, monthly_call_cap=5, budget_cap_usd=0.5)
    assert result["monthly_calls"] == 0
    assert result["daily_calls"] == 0
    assert result["monthly_spend_usd"] == 0.0
    assert result["consecutive_errors"] == 0
