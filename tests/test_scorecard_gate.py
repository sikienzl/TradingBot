"""Tests for :mod:`src.scorecard_gate` (fresh GO prerequisite for risky actions).

The gate is a fail-closed boundary, so the interesting cases are all the ways a
scorecard marker can be unusable: missing, empty, malformed, uppercase-only,
stale, ``HOLD``/``NO-GO``/``ERROR``, or produced by a maintenance override.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from src.scorecard_gate import (
    DEFAULT_MAX_AGE_SECONDS,
    ENABLE_ENV,
    MAINTENANCE_RUN_MODE,
    MAX_AGE_ENV,
    STATUS_ENV_ENV,
    STATUS_JSON_ENV,
    evaluate_scorecard_gate,
    read_scorecard_status,
    scorecard_gate_refusal,
    scorecard_gate_settings,
    scorecard_status_age_seconds,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def _stamp(seconds_ago: float) -> str:
    from datetime import timedelta

    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_env_marker(
    path: Path, verdict: str = "GO", *, run_mode: str = "standard",
    seconds_ago: float = 60.0, timestamp: str | None = None,
) -> Path:
    """Write the marker ``scripts/run_weekly_scorecard.sh`` really produces."""
    path.write_text(
        f"TIMESTAMP_UTC={timestamp or _stamp(seconds_ago)}\n"
        f"RUN_MODE={run_mode}\n"
        f"VERDICT={verdict}\n"
        "UNDERLYING_EXIT_CODE=0\n"
        "FINAL_EXIT_CODE=0\n"
        f"REPORT_FILE=/opt/trading_2/results/scorecards/scorecard_x.txt\n"
        "LATEST_REPORT_LINK=/opt/trading_2/results/scorecards/latest_scorecard.txt\n",
        encoding="utf-8",
    )
    return path


def write_json_marker(
    path: Path, verdict: str = "GO", *, run_mode: str = "standard",
    seconds_ago: float = 60.0, timestamp: str | None = None,
) -> Path:
    path.write_text(
        json.dumps({
            "timestamp_utc": timestamp or _stamp(seconds_ago),
            "run_mode": run_mode,
            "verdict": verdict,
            "primary_reason": "none",
            "underlying_exit_code": 0,
            "final_exit_code": 0,
            "metrics": {"closed_trades": 120},
        }),
        encoding="utf-8",
    )
    return path


def evaluate(marker: Path, **kwargs):
    kwargs.setdefault("max_age_seconds", DEFAULT_MAX_AGE_SECONDS)
    kwargs.setdefault("now", NOW)
    return evaluate_scorecard_gate(None, marker, **kwargs)


# ---------------------------------------------------------------------------
# Marker readers
# ---------------------------------------------------------------------------

def test_reads_uppercase_env_marker(tmp_path) -> None:
    marker = write_env_marker(tmp_path / "latest_status.env")
    status = read_scorecard_status(marker)
    assert status is not None
    assert status["VERDICT"] == "GO"
    assert status["RUN_MODE"] == "standard"
    assert status["TIMESTAMP_UTC"].endswith("Z")


def test_reads_lowercase_json_marker(tmp_path) -> None:
    marker = write_json_marker(tmp_path / "latest_status.json")
    status = read_scorecard_status(marker)
    assert status is not None
    assert status["VERDICT"] == "GO"
    assert status["UNDERLYING_EXIT_CODE"] == "0"


def test_readers_fail_closed(tmp_path) -> None:
    missing = tmp_path / "missing.env"
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    malformed = tmp_path / "broken.json"
    malformed.write_text("{not json", encoding="utf-8")
    not_an_object = tmp_path / "array.json"
    not_an_object.write_text("[1, 2]", encoding="utf-8")
    binary = tmp_path / "binary.env"
    binary.write_bytes(b"\x00\x01\x02")

    for path in (missing, empty, malformed, not_an_object, binary):
        assert read_scorecard_status(path) is None, path


def test_age_uses_marker_timestamp(tmp_path) -> None:
    marker = write_env_marker(tmp_path / "latest_status.env", seconds_ago=90.0)
    status = read_scorecard_status(marker)
    assert status is not None
    assert scorecard_status_age_seconds(status, NOW) == 90.0


def test_age_rejects_unusable_timestamps() -> None:
    for status in ({}, {"TIMESTAMP_UTC": ""}, {"TIMESTAMP_UTC": "yesterday"},
                   {"TIMESTAMP_UTC": "not-a-date"}):
        assert scorecard_status_age_seconds(status, NOW) is None, status


def test_naive_timestamp_is_read_as_utc() -> None:
    status = {"TIMESTAMP_UTC": "2026-11-26T11:30:00"}
    assert scorecard_status_age_seconds(status, NOW) == 1800.0


# ---------------------------------------------------------------------------
# Gate decisions
# ---------------------------------------------------------------------------

def test_fresh_go_allows(tmp_path) -> None:
    result = evaluate(write_env_marker(tmp_path / "latest_status.env"))
    assert result.allowed is True
    assert result.verdict == "GO"
    assert result.age_seconds == 60.0


def test_every_non_go_verdict_refuses(tmp_path) -> None:
    for verdict in ("HOLD", "NO-GO", "ERROR", "GO-ish"):
        marker = write_env_marker(tmp_path / "latest_status.env", verdict)
        result = evaluate(marker)
        assert result.allowed is False
        assert verdict.upper() in result.reason


def test_missing_marker_refuses(tmp_path) -> None:
    result = evaluate(tmp_path / "absent.env")
    assert result.allowed is False
    assert result.reason == "no scorecard status marker was found"


def test_unreadable_marker_refuses(tmp_path) -> None:
    marker = tmp_path / "latest_status.env"
    marker.write_text("garbage without separators\n", encoding="utf-8")
    result = evaluate(marker)
    assert result.allowed is False
    assert result.reason == "scorecard status marker is unreadable"


def test_marker_without_verdict_refuses(tmp_path) -> None:
    marker = tmp_path / "latest_status.env"
    marker.write_text(f"TIMESTAMP_UTC={_stamp(60)}\nRUN_MODE=standard\n",
                      encoding="utf-8")
    result = evaluate(marker)
    assert result.allowed is False
    assert result.reason == "scorecard verdict is missing"


def test_stale_go_refuses(tmp_path) -> None:
    marker = write_env_marker(tmp_path / "latest_status.env", seconds_ago=7 * 24 * 3600)
    result = evaluate(marker, max_age_seconds=86_400)
    assert result.allowed is False
    assert "stale" in result.reason
    assert result.age_seconds == 7 * 24 * 3600


def test_marker_without_timestamp_refuses(tmp_path) -> None:
    marker = tmp_path / "latest_status.env"
    marker.write_text("VERDICT=GO\nRUN_MODE=standard\n", encoding="utf-8")
    result = evaluate(marker)
    assert result.allowed is False
    assert "no usable timestamp" in result.reason


def test_maintenance_override_go_refuses_unless_allowed(tmp_path) -> None:
    marker = write_env_marker(
        tmp_path / "latest_status.env", run_mode=MAINTENANCE_RUN_MODE)
    result = evaluate(marker)
    assert result.allowed is False
    assert "maintenance override" in result.reason
    assert evaluate(marker, allow_maintenance_override=True).allowed is True


def test_json_marker_is_preferred_over_env(tmp_path) -> None:
    json_marker = write_json_marker(tmp_path / "latest_status.json", "NO-GO")
    env_marker = write_env_marker(tmp_path / "latest_status.env", "GO")
    result = evaluate_scorecard_gate(json_marker, env_marker, now=NOW)
    assert result.allowed is False
    assert result.verdict == "NO-GO"


def test_env_marker_used_when_json_is_missing(tmp_path) -> None:
    env_marker = write_env_marker(tmp_path / "latest_status.env", "GO")
    result = evaluate_scorecard_gate(tmp_path / "absent.json", env_marker, now=NOW)
    assert result.allowed is True


def test_unreadable_json_falls_back_to_env_marker(tmp_path) -> None:
    broken = tmp_path / "latest_status.json"
    broken.write_text("{truncated", encoding="utf-8")
    env_marker = write_env_marker(tmp_path / "latest_status.env", "HOLD")
    result = evaluate_scorecard_gate(broken, env_marker, now=NOW)
    assert result.allowed is False
    assert result.verdict == "HOLD"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_settings_default_to_deployment_paths() -> None:
    settings = scorecard_gate_settings({})
    assert settings.enabled is True
    assert settings.status_json_file == Path("results/scorecards/latest_status.json")
    assert settings.status_env_file == Path("results/scorecards/latest_status.env")
    assert settings.max_age_seconds == DEFAULT_MAX_AGE_SECONDS


def test_settings_read_overrides() -> None:
    settings = scorecard_gate_settings({
        ENABLE_ENV: "false",
        STATUS_JSON_ENV: "/tmp/a.json",
        STATUS_ENV_ENV: "/tmp/b.env",
        MAX_AGE_ENV: "600",
    })
    assert settings.enabled is False
    assert settings.status_json_file == Path("/tmp/a.json")
    assert settings.status_env_file == Path("/tmp/b.env")
    assert settings.max_age_seconds == 600.0


def test_settings_ignore_invalid_age_and_enable_values() -> None:
    settings = scorecard_gate_settings({MAX_AGE_ENV: "garbage", ENABLE_ENV: ""})
    assert settings.max_age_seconds == DEFAULT_MAX_AGE_SECONDS
    assert settings.enabled is True
    assert scorecard_gate_settings({MAX_AGE_ENV: "-5"}).max_age_seconds == (
        DEFAULT_MAX_AGE_SECONDS
    )


def test_settings_evaluate_uses_configured_markers(tmp_path) -> None:
    marker = write_env_marker(tmp_path / "status.env", "NO-GO")
    settings = scorecard_gate_settings({
        STATUS_JSON_ENV: str(tmp_path / "absent.json"),
        STATUS_ENV_ENV: str(marker),
    })
    result = settings.evaluate(now=NOW)
    assert result.allowed is False
    assert result.verdict == "NO-GO"


def test_refusal_message_names_the_action_and_override(tmp_path) -> None:
    settings = scorecard_gate_settings({})
    status = evaluate_scorecard_gate(None, None, now=NOW)
    message = scorecard_gate_refusal("Live trading (DRY_RUN=false)", status, settings)
    assert "requires a fresh GO scorecard verdict" in message
    assert "no scorecard status marker was found" in message
    assert STATUS_JSON_ENV in message
    assert ENABLE_ENV in message
