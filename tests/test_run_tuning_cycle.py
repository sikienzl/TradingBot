"""Tests for the tuning chain's scorecard evidence handling.

The single-Pi release profile keeps ``STATUS_JSON_ENABLED`` off, so the tuning
cycle used to abort with ``FileNotFoundError`` on a default Pi install: it read
one fixed JSON marker and nothing else. These tests pin the replacement
contract - every evidence source is optional, the recommender always gets a
usable verdict, and missing evidence blocks ``--apply`` instead of raising.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.recommend_tuning_change import load_status
from scripts.run_tuning_cycle import (
    DEFAULT_MAX_VERDICT_AGE_SECONDS,
    _build_safety_guard_result,
)
from src.scorecard_verdict_history import append_verdict_record
from src.task_status import read_task_status, task_exit_code

ROOT = Path(__file__).resolve().parents[1]
TUNING_WRAPPER = ROOT / "scripts" / "run_weekly_tuning_cycle.sh"

HEALTHY_METRICS = {
    "profit_factor": 1.5,
    "avg_pnl": 5.0,
    "win_rate": 55.0,
    "max_drawdown_pct": 1.0,
}


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch) -> None:
    """Keep relative defaults away from the real repository results dir."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SCORECARD_VERDICT_HISTORY_FILE", raising=False)


def iso(offset_seconds: int = 0) -> str:
    """Timestamp relative to the real clock - the age guards use it."""
    moment = datetime.now(UTC) + timedelta(seconds=offset_seconds)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_marker(path: Path, verdict: str = "GO") -> None:
    payload = {
        "timestamp_utc": iso(),
        "run_mode": "standard",
        "verdict": verdict,
        "primary_reason": "ok",
        "profile": "paper",
        "metrics": dict(HEALTHY_METRICS),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def env_marker(path: Path, verdict: str = "GO", *, stamp: str | None = None) -> None:
    lines = [
        f"TIMESTAMP_UTC={stamp or iso()}",
        "RUN_MODE=standard",
        f"VERDICT={verdict}",
        "UNDERLYING_EXIT_CODE=0",
        "FINAL_EXIT_CODE=0",
        "PRIMARY_REASON=ok",
        "PROFILE=paper",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def append_history(path: Path, verdict: str, *, offset: int, report: str) -> None:
    result = append_verdict_record(
        str(path),
        {
            "timestamp_utc": iso(offset),
            "verdict": verdict,
            "run_mode": "standard",
            "report_file": report,
            "metrics": dict(HEALTHY_METRICS),
        },
    )
    assert result == "appended"


def guard_args(tmp_path: Path, **overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "require_go_verdict": True,
        "block_on_recent_no_go": True,
        "recent_scorecard_limit": 3,
        "scorecard_reports_dir": str(tmp_path / "reports"),
        "max_drawdown_pct_limit": None,
        "verdict_history": str(tmp_path / "verdict_history.jsonl"),
        "max_verdict_age_seconds": DEFAULT_MAX_VERDICT_AGE_SECONDS,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


# ---------------------------------------------------------------------------
# load_status: every evidence source is optional
# ---------------------------------------------------------------------------


def test_load_status_prefers_json_marker(tmp_path: Path) -> None:
    json_marker(tmp_path / "latest_status.json")
    env_marker(tmp_path / "latest_status.env", "NO-GO")

    status, source = load_status(
        str(tmp_path / "latest_status.json"),
        status_env=str(tmp_path / "latest_status.env"),
    )

    assert source == "json"
    assert status["verdict"] == "GO"
    assert status["metrics"]["profit_factor"] == 1.5


def test_load_status_falls_back_to_env_marker(tmp_path: Path) -> None:
    """The default Pi profile only has the uppercase KEY=value marker."""
    env_marker(tmp_path / "latest_status.env")

    status, source = load_status(
        str(tmp_path / "missing_status.json"),
        status_env=str(tmp_path / "latest_status.env"),
    )

    assert source == "env"
    assert status["verdict"] == "GO"
    assert status["metrics"] == {}
    assert status["profile"] == "paper"
    assert status["timestamp_utc"] == iso()


def test_load_status_skips_empty_and_malformed_markers(tmp_path: Path) -> None:
    (tmp_path / "empty.json").write_text("", encoding="utf-8")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    env_marker(tmp_path / "latest_status.env", "HOLD")

    status, source = load_status(
        str(tmp_path / "empty.json"), status_env=str(tmp_path / "latest_status.env")
    )
    assert (source, status["verdict"]) == ("env", "HOLD")

    status, source = load_status(str(tmp_path / "broken.json"), status_env="")
    assert (source, status["verdict"]) == ("missing", "ERROR")


def test_load_status_falls_back_to_verdict_history(tmp_path: Path) -> None:
    history = tmp_path / "verdict_history.jsonl"
    append_history(history, "HOLD", offset=-3600, report="scorecard_earlier.txt")
    append_history(history, "GO", offset=-60, report="scorecard_latest.txt")

    status, source = load_status(
        str(tmp_path / "missing.json"),
        status_env=str(tmp_path / "missing.env"),
        verdict_history=str(history),
    )

    assert source == "verdict_history"
    assert status["verdict"] == "GO"
    assert status["metrics"]["win_rate"] == 55.0


def test_load_status_without_any_evidence_fails_closed(tmp_path: Path) -> None:
    status, source = load_status(
        str(tmp_path / "missing.json"), status_env="", verdict_history="off"
    )

    assert source == "missing"
    assert status["verdict"] == "ERROR"
    assert status["metrics"] == {}


def test_load_status_ignores_history_when_disabled(tmp_path: Path) -> None:
    history = tmp_path / "verdict_history.jsonl"
    append_history(history, "GO", offset=-60, report="scorecard_x.txt")

    status, source = load_status(
        str(tmp_path / "missing.json"), status_env="", verdict_history="off"
    )

    assert (source, status["verdict"]) == ("missing", "ERROR")


@pytest.mark.parametrize("verdict", ["GO", "HOLD", "NO-GO", "ERROR"])
def test_load_status_round_trips_every_verdict(tmp_path: Path, verdict: str) -> None:
    env_marker(tmp_path / "latest_status.env", verdict)

    status, source = load_status(
        str(tmp_path / "missing.json"), status_env=str(tmp_path / "latest_status.env")
    )

    assert source == "env"
    assert status["verdict"] == verdict


# ---------------------------------------------------------------------------
# Safety guard: verdict age and history-based recent NO-GO
# ---------------------------------------------------------------------------


def test_guard_blocks_stale_verdict(tmp_path: Path) -> None:
    args = guard_args(tmp_path, verdict_history="", max_verdict_age_seconds=3600)
    status = {"verdict": "GO", "timestamp_utc": iso(-7200), "metrics": {}}

    result = _build_safety_guard_result(args, status)

    assert result["allow_apply"] is False
    # The age is rounded to whole seconds and measured against the real clock, so
    # a run that straddles a second boundary reports 7201s. Assert the bound, not
    # an exact string.
    stale = [
        reason
        for reason in result["reasons"]
        if (match := re.search(r"verdict is (\d+)s old", reason))
        and 7200 <= int(match.group(1)) <= 7202
    ]
    assert stale, result["reasons"]


def test_guard_blocks_status_without_timestamp(tmp_path: Path) -> None:
    args = guard_args(tmp_path, verdict_history="")

    result = _build_safety_guard_result(args, {"verdict": "GO", "metrics": {}})

    assert result["allow_apply"] is False
    assert any("no usable timestamp" in reason for reason in result["reasons"])


def test_guard_age_check_can_be_disabled(tmp_path: Path) -> None:
    args = guard_args(tmp_path, verdict_history="", max_verdict_age_seconds=0)
    status = {"verdict": "GO", "timestamp_utc": iso(-400 * 24 * 3600), "metrics": {}}

    result = _build_safety_guard_result(args, status)

    assert result["allow_apply"] is True
    assert result["verdict_age_seconds"] is not None


def test_guard_uses_verdict_history_for_recent_no_go(tmp_path: Path) -> None:
    history = tmp_path / "verdict_history.jsonl"
    append_history(history, "NO-GO", offset=-60, report="scorecard_20261129_115900.txt")
    args = guard_args(tmp_path)

    result = _build_safety_guard_result(args, {"verdict": "GO", "timestamp_utc": iso()})

    assert result["allow_apply"] is False
    assert result["recent_no_go_reports"] == []
    assert len(result["recent_no_go_verdicts"]) == 1
    assert any("verdict history" in reason for reason in result["reasons"])


def test_guard_recent_no_go_only_looks_at_the_limit(tmp_path: Path) -> None:
    history = tmp_path / "verdict_history.jsonl"
    append_history(history, "NO-GO", offset=-7 * 24 * 3600, report="scorecard_old.txt")
    for index in range(3):
        append_history(history, "GO", offset=-index * 60, report=f"scorecard_new{index}.txt")
    args = guard_args(tmp_path, recent_scorecard_limit=3)

    result = _build_safety_guard_result(args, {"verdict": "GO", "timestamp_utc": iso()})

    assert result["allow_apply"] is True
    assert result["recent_no_go_verdicts"] == []


def test_guard_falls_back_to_report_glob_when_history_off(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "scorecard_20261129_120000.txt").write_text(
        "VERDICT:              NO-GO\n", encoding="utf-8"
    )
    args = guard_args(tmp_path, verdict_history="")

    result = _build_safety_guard_result(args, {"verdict": "GO", "timestamp_utc": iso()})

    assert result["allow_apply"] is False
    assert len(result["recent_no_go_reports"]) == 1
    assert result["recent_no_go_verdicts"] == []


def test_guard_passes_with_healthy_evidence(tmp_path: Path) -> None:
    history = tmp_path / "verdict_history.jsonl"
    append_history(history, "GO", offset=-3600, report="scorecard_ok.txt")
    args = guard_args(tmp_path)

    result = _build_safety_guard_result(
        args,
        {"verdict": "GO", "timestamp_utc": iso(), "metrics": dict(HEALTHY_METRICS)},
    )

    assert result["allow_apply"] is True
    assert result["reasons"] == ["Safety guard passed."]


# ---------------------------------------------------------------------------
# run_tuning_cycle.main(): blocked instead of crashing
# ---------------------------------------------------------------------------


def run_cycle(tmp_path: Path, extra: list[str] | None = None) -> tuple[int, dict[str, Any]]:
    """Run the orchestrator as a subprocess so the exit code is the real one."""
    cycle_json = tmp_path / "cycle_summary.json"
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "run_tuning_cycle.py"),
        "--policy",
        str(ROOT / "tuning_policy.json"),
        "--status-json",
        str(tmp_path / "latest_status.json"),
        "--status-env",
        str(tmp_path / "latest_status.env"),
        "--verdict-history",
        str(tmp_path / "verdict_history.jsonl"),
        "--env-file",
        str(tmp_path / ".env"),
        "--recommender-state",
        str(tmp_path / "state.json"),
        "--recommendation-json",
        str(tmp_path / "recommendation.json"),
        "--apply-summary-json",
        str(tmp_path / "apply.json"),
        "--cycle-summary-json",
        str(cycle_json),
        "--backup-dir",
        str(tmp_path / "backups"),
    ]
    if extra:
        cmd.extend(extra)
    proc = subprocess.run(
        cmd,
        cwd=str(tmp_path),
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode in (0, 3), proc.stdout + proc.stderr
    summary = json.loads(cycle_json.read_text(encoding="utf-8"))
    return proc.returncode, summary


def test_cycle_blocked_without_evidence(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MAX_HOLD_SECONDS=300\n", encoding="utf-8")

    code, summary = run_cycle(tmp_path, ["--apply"])

    assert code == 3
    assert summary["status"] == "blocked"
    assert summary["step"] == "safety_guard"
    assert summary["reason"] == "no scorecard verdict evidence"
    assert summary["apply_effective"] is False
    # Fail closed: the live env file must not be touched.
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "MAX_HOLD_SECONDS=300\n"
    assert not (tmp_path / "backups").exists()


def test_cycle_succeeds_with_env_marker_only(tmp_path: Path) -> None:
    """The default Pi profile: no JSON marker, only the uppercase marker."""
    (tmp_path / ".env").write_text("MAX_HOLD_SECONDS=300\n", encoding="utf-8")
    env_marker(tmp_path / "latest_status.env")

    code, summary = run_cycle(tmp_path)

    assert code == 0, json.dumps(summary, indent=2)
    assert summary["status"] == "ok"
    assert summary["status_source"] == "env"
    assert summary["status_verdict"] == "GO"
    assert summary["safety_guard"]["allow_apply"] is True


def test_cycle_apply_blocked_by_history_no_go(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MAX_HOLD_SECONDS=300\n", encoding="utf-8")
    env_marker(tmp_path / "latest_status.env")
    append_history(
        tmp_path / "verdict_history.jsonl",
        "NO-GO",
        offset=-60,
        report="scorecard_20261129_115900.txt",
    )

    code, summary = run_cycle(tmp_path, ["--apply"])

    assert code == 0
    assert summary["apply_effective"] is False
    assert summary["safety_guard"]["recent_no_go_verdicts"]
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "MAX_HOLD_SECONDS=300\n"


def test_cycle_apply_blocked_by_stale_verdict(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MAX_HOLD_SECONDS=300\n", encoding="utf-8")
    env_marker(tmp_path / "latest_status.env", stamp=iso(-40 * 24 * 3600))

    code, summary = run_cycle(tmp_path, ["--apply"])

    assert code == 0
    assert summary["apply_effective"] is False
    assert any("old," in reason for reason in summary["safety_guard"]["reasons"])


# ---------------------------------------------------------------------------
# Wrapper: one scheduled-task marker per attempt
# ---------------------------------------------------------------------------


def run_wrapper(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    environ = {
        "PATH": "/usr/bin:/bin",
        "PYTHON_BIN": sys.executable,
        "TUNING_ENV_FILE": str(tmp_path / ".env"),
        "TUNING_STATUS_JSON": str(tmp_path / "latest_status.json"),
        "TUNING_STATUS_ENV": str(tmp_path / "latest_status.env"),
        "TUNING_VERDICT_HISTORY_FILE": str(tmp_path / "verdict_history.jsonl"),
        "TUNING_RECOMMENDER_STATE": str(tmp_path / "state.json"),
        "TUNING_RECOMMENDATION_JSON": str(tmp_path / "recommendation.json"),
        "TUNING_APPLY_SUMMARY_JSON": str(tmp_path / "apply.json"),
        "TUNING_CYCLE_SUMMARY_JSON": str(tmp_path / "cycle_summary.json"),
        "TUNING_BACKUP_DIR": str(tmp_path / "backups"),
        "TUNING_STATUS_FILE": str(tmp_path / "tuning" / "latest_status.env"),
        "TUNING_SCORECARD_REPORTS_DIR": str(tmp_path / "reports"),
    }
    environ.update(env)
    return subprocess.run(
        ["bash", str(TUNING_WRAPPER)],
        cwd=str(tmp_path),
        env=environ,
        text=True,
        capture_output=True,
        check=False,
    )


def test_wrapper_writes_task_marker_parseable_by_task_status(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MAX_HOLD_SECONDS=300\n", encoding="utf-8")
    env_marker(tmp_path / "latest_status.env")

    proc = run_wrapper(tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    fields = read_task_status(tmp_path / "tuning" / "latest_status.env")
    assert fields is not None
    assert task_exit_code(fields) == 0
    assert fields["reason"] == "cycle_exit_0"
    assert int(fields["finished_unixtime"]) >= int(fields["started_unixtime"])


def test_wrapper_marker_records_blocked_exit_code(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MAX_HOLD_SECONDS=300\n", encoding="utf-8")

    proc = run_wrapper(tmp_path, TUNING_APPLY_CHANGES="true")

    assert proc.returncode == 3, proc.stdout + proc.stderr
    fields = read_task_status(tmp_path / "tuning" / "latest_status.env")
    assert fields is not None
    assert task_exit_code(fields) == 3
    assert fields["reason"] == "cycle_exit_3"


def test_wrapper_marker_written_when_python_is_missing(tmp_path: Path) -> None:
    """A wrapper that dies early still has to leave a marker behind."""
    (tmp_path / ".env").write_text("MAX_HOLD_SECONDS=300\n", encoding="utf-8")

    proc = run_wrapper(tmp_path, PYTHON_BIN=str(tmp_path / "no-such-python"))

    assert proc.returncode == 1
    fields = read_task_status(tmp_path / "tuning" / "latest_status.env")
    assert fields is not None
    assert task_exit_code(fields) == 1
    assert fields["reason"] == "python_not_found"


def test_wrapper_reports_disabled_history(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MAX_HOLD_SECONDS=300\n", encoding="utf-8")
    env_marker(tmp_path / "latest_status.env")

    proc = run_wrapper(tmp_path, TUNING_VERDICT_HISTORY_FILE="off")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Verdict history: disabled" in proc.stdout
    assert not (tmp_path / "verdict_history.jsonl").exists()


def test_wrapper_resolves_relative_history_path(tmp_path: Path) -> None:
    """A relative setting is repository-relative, like the scorecard wrapper."""
    (tmp_path / ".env").write_text("MAX_HOLD_SECONDS=300\n", encoding="utf-8")
    env_marker(tmp_path / "latest_status.env")

    proc = run_wrapper(tmp_path, TUNING_VERDICT_HISTORY_FILE="custom_history.jsonl")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next(
        line for line in proc.stdout.splitlines() if line.startswith("Verdict history:")
    )
    assert line.strip() == f"Verdict history: {ROOT}/custom_history.jsonl"
