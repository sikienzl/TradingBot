"""Tests for the persistent scorecard verdict history."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.scorecard_gate import read_scorecard_status
from src.scorecard_verdict_history import (
    DEFAULT_VERDICT_HISTORY_FILE,
    HISTORY_PATH_ENV,
    VERDICT_GO,
    VERDICT_NO_GO,
    append_verdict_record,
    history_age_seconds,
    history_stats,
    latest_verdict_record,
    parse_history_timestamp,
    read_verdict_history,
    recent_no_go_records,
    resolve_scorecard_status,
    resolve_verdict_history_path,
    to_verdict_record,
    verdict_streak,
)
from src.task_status import read_task_status, task_exit_code, task_last_run_age_seconds

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "run_weekly_scorecard.sh"

NOW = datetime(2026, 11, 29, 12, 0, tzinfo=UTC)


def iso(offset_seconds: int = 0) -> str:
    return (NOW + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def base_record(**overrides):
    payload = {
        "verdict": "NO-GO",
        "timestamp_utc": iso(),
        "run_mode": "standard",
        "underlying_exit_code": 3,
        "final_exit_code": 3,
        "primary_reason": "profit_factor_below_1.0",
        "report_file": "/opt/trading_2/results/scorecards/scorecard_20261129_090000.txt",
        "profile": "paper",
        "metrics": {"closed_trades": 120, "profit_factor": 0.81},
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# record shape
# ---------------------------------------------------------------------------


def test_to_verdict_record_normalises_and_stable_id():
    first = to_verdict_record(base_record())
    second = to_verdict_record(base_record())
    assert first is not None and second is not None
    assert first["schema_version"] == 1
    assert first["verdict"] == "NO-GO"
    assert first["record_id"] == second["record_id"]
    assert first["metrics"]["profit_factor"] == 0.81


def test_to_verdict_record_rejects_unusable_payloads():
    assert to_verdict_record({}) is None
    assert to_verdict_record({"verdict": "MAYBE", "timestamp_utc": iso()}) is None
    assert to_verdict_record({"verdict": "GO"}) is None
    assert to_verdict_record({"timestamp_utc": "not-a-time"}) is None


def test_to_verdict_record_requires_usable_timestamp():
    assert to_verdict_record({"verdict": "GO", "report_file": "x.txt"}) is None
    assert to_verdict_record({"verdict": "GO", "timestamp_utc": "yesterday"}) is None


def test_timestamp_parser_accepts_both_marker_shapes():
    assert parse_history_timestamp("2026-11-29T09:00:00Z") is not None
    assert parse_history_timestamp("2026-11-29T09:00:00+00:00") is not None
    assert parse_history_timestamp("nope") is None
    assert parse_history_timestamp(None) is None


# ---------------------------------------------------------------------------
# append / dedupe
# ---------------------------------------------------------------------------


def test_append_then_duplicate_is_skipped(tmp_path):
    path = tmp_path / "history" / "verdict_history.jsonl"
    assert append_verdict_record(path, base_record()) == "appended"
    assert append_verdict_record(path, base_record()) == "duplicate"
    assert append_verdict_record(path, base_record(report_file="other.txt")) == "appended"
    records = read_verdict_history(path)
    assert len(records) == 2
    assert records[0]["verdict"] == "NO-GO"


def test_append_disabled_and_missing_paths(tmp_path):
    assert append_verdict_record("", base_record()) == "disabled"
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    assert append_verdict_record(blocked / "history.jsonl", base_record()) == "error"
    assert append_verdict_record(tmp_path / "x.jsonl", {"verdict": "NOPE"}) == "invalid"


def test_append_repairs_torn_final_line(tmp_path):
    path = tmp_path / "verdict_history.jsonl"
    assert append_verdict_record(path, base_record()) == "appended"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"record_id": "trunc')
    assert append_verdict_record(path, base_record(report_file="next.txt")) == "appended"
    records = read_verdict_history(path)
    assert [record["report_file"] for record in records] == [
        base_record()["report_file"],
        "next.txt",
    ]


def test_read_history_skips_malformed_lines(tmp_path):
    path = tmp_path / "verdict_history.jsonl"
    append_verdict_record(path, base_record())
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n\n")
    append_verdict_record(path, base_record(report_file="later.txt", verdict="GO"))
    records = read_verdict_history(path)
    assert len(records) == 2
    assert records[-1]["verdict"] == "GO"


def test_read_history_bounds_the_tail_window(tmp_path):
    path = tmp_path / "verdict_history.jsonl"
    for index in range(600):
        assert append_verdict_record(
            path,
            base_record(
                report_file=f"scorecard_{index:04d}.txt",
                timestamp_utc=iso(index),
                metrics={"closed_trades": index, "pad": "x" * 2000},
            ),
            dedupe_window=1,
        ) == "appended"
    assert path.stat().st_size > 1_048_576
    # The byte cap keeps a reader off an unbounded scan even when the caller
    # asks for more records than fit in the window.
    records = read_verdict_history(path, limit=1000)
    assert 0 < len(records) < 600
    assert records[-1]["metrics"]["closed_trades"] == 599
    limited = read_verdict_history(path, limit=3)
    assert [record["metrics"]["closed_trades"] for record in limited] == [597, 598, 599]


def test_streaks_and_latest(tmp_path):
    path = tmp_path / "verdict_history.jsonl"
    for index, verdict in enumerate(["NO-GO", "GO", "GO", "NO-GO"]):
        append_verdict_record(
            path,
            base_record(
                verdict=verdict,
                report_file=f"scorecard_{index}.txt",
                timestamp_utc=iso(index),
            ),
        )
    assert verdict_streak(path, VERDICT_NO_GO) == 1
    assert verdict_streak(path, VERDICT_GO) == 0
    latest = latest_verdict_record(path)
    assert latest is not None and latest["verdict"] == "NO-GO"
    no_go = recent_no_go_records(path, limit=5)
    assert [record["report_file"] for record in no_go] == [
        "scorecard_3.txt",
        "scorecard_0.txt",
    ]


def test_history_stats_and_age(tmp_path):
    path = tmp_path / "verdict_history.jsonl"
    stats = history_stats(path)
    assert stats["enabled"] is True and stats["exists"] is False
    assert stats["records"] == 0
    assert history_age_seconds(path, NOW) is None

    append_verdict_record(
        path,
        base_record(
            verdict="GO",
            timestamp_utc="2026-11-29T10:00:00Z",
            report_file="b.txt",
        ),
    )
    append_verdict_record(
        path,
        base_record(
            verdict="GO",
            timestamp_utc="2026-11-29T11:00:00Z",
            report_file="a.txt",
        ),
    )
    stats = history_stats(path)
    assert stats["records"] == 2
    assert stats["newest_verdict"] == "GO"
    assert stats["go_streak"] == 2
    assert stats["newest_timestamp_utc"] == "2026-11-29T11:00:00Z"
    assert history_age_seconds(path, NOW) == 3600.0

    disabled = history_stats("")
    assert disabled["enabled"] is False and disabled["reason"] == "disabled"


# ---------------------------------------------------------------------------
# path resolution
# ---------------------------------------------------------------------------


def test_resolve_verdict_history_path(monkeypatch, tmp_path):
    monkeypatch.delenv(HISTORY_PATH_ENV, raising=False)
    assert resolve_verdict_history_path() == DEFAULT_VERDICT_HISTORY_FILE
    for token in ("", "off", "none", "false", "no", "disabled"):
        monkeypatch.setenv(HISTORY_PATH_ENV, token)
        assert resolve_verdict_history_path() == ""
    monkeypatch.setenv(HISTORY_PATH_ENV, str(tmp_path / "verdicts.jsonl"))
    assert resolve_verdict_history_path() == str(tmp_path / "verdicts.jsonl")


# ---------------------------------------------------------------------------
# unified status resolution (JSON marker -> ENV marker -> history)
# ---------------------------------------------------------------------------


def write_env_marker(path: Path, verdict: str = "HOLD", **overrides) -> None:
    lines = {
        "TIMESTAMP_UTC": iso(),
        "RUN_MODE": "standard",
        "VERDICT": verdict,
        "UNDERLYING_EXIT_CODE": "2",
        "FINAL_EXIT_CODE": "2",
        "REPORT_FILE": str(path.parent / "scorecard_env.txt"),
    }
    lines.update(overrides)
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in lines.items()) + "\n",
        encoding="utf-8",
    )


def write_json_marker(path: Path, verdict: str = "NO-GO", **overrides) -> None:
    payload = {
        "timestamp_utc": iso(),
        "run_mode": "standard",
        "verdict": verdict,
        "primary_reason": "max_drawdown_breach",
        "underlying_exit_code": 3,
        "final_exit_code": 3,
        "report_file": str(path.parent / "scorecard_json.txt"),
        "metrics": {"closed_trades": 150, "max_drawdown_pct": 18.5},
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def test_resolve_status_prefers_json_marker(tmp_path):
    json_file = tmp_path / "latest_status.json"
    env_file = tmp_path / "latest_status.env"
    history = tmp_path / "verdict_history.jsonl"
    write_json_marker(json_file)
    write_env_marker(env_file, verdict="GO")
    append_verdict_record(history, base_record(verdict="GO", report_file="h.txt"))

    status = resolve_scorecard_status(
        status_json_file=json_file, status_env_file=env_file, history_file=history
    )
    assert status is not None
    assert status["source"] == "json"
    assert status["verdict"] == "NO-GO"
    assert status["metrics"]["max_drawdown_pct"] == 18.5
    assert status["primary_reason"] == "max_drawdown_breach"


def test_resolve_status_falls_back_to_env_marker(tmp_path):
    env_file = tmp_path / "latest_status.env"
    write_env_marker(env_file, verdict="GO", FINAL_EXIT_CODE="0")
    status = resolve_scorecard_status(
        status_json_file=tmp_path / "missing.json",
        status_env_file=env_file,
        history_file=tmp_path / "missing.jsonl",
    )
    assert status is not None
    assert status["source"] == "env"
    assert status["verdict"] == "GO"
    assert status["final_exit_code"] == 0


def test_resolve_status_falls_back_to_history(tmp_path):
    history = tmp_path / "verdict_history.jsonl"
    append_verdict_record(
        history,
        base_record(
            verdict="GO",
            timestamp_utc="2026-11-29T11:30:00Z",
            report_file="gone.txt",
            underlying_exit_code=0,
            final_exit_code=0,
            primary_reason="",
            metrics={"closed_trades": 90, "win_rate": 0.55},
        ),
    )
    status = resolve_scorecard_status(
        status_json_file=tmp_path / "missing.json",
        status_env_file=tmp_path / "missing.env",
        history_file=history,
    )
    assert status is not None
    assert status["source"] == "verdict_history"
    assert status["verdict"] == "GO"
    assert status["metrics"]["win_rate"] == 0.55
    assert "primary_reason" not in status


def test_resolve_status_fail_closed_without_evidence(tmp_path):
    status = resolve_scorecard_status(
        status_json_file=tmp_path / "missing.json",
        status_env_file=tmp_path / "missing.env",
        history_file="",
    )
    assert status is None


def test_resolve_status_empty_files_are_ignored(tmp_path):
    json_file = tmp_path / "latest_status.json"
    env_file = tmp_path / "latest_status.env"
    json_file.write_text("", encoding="utf-8")
    env_file.write_text("", encoding="utf-8")
    assert (
        resolve_scorecard_status(
            status_json_file=json_file, status_env_file=env_file, history_file=""
        )
        is None
    )


def test_history_append_survives_concurrent_writers(tmp_path):
    path = tmp_path / "verdict_history.jsonl"
    payload = base_record(report_file="same.txt")
    child_pids = []
    for _ in range(4):
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child process
            outcome = append_verdict_record(path, payload)
            os._exit(0 if outcome == "appended" else 1)
        child_pids.append(pid)
    statuses = [os.waitpid(pid, 0)[1] for pid in child_pids]
    assert sum(1 for status in statuses if status == 0) == 1
    assert len(read_verdict_history(path)) == 1


@pytest.mark.parametrize("verdict", ["GO", "HOLD", "NO-GO", "ERROR"])
def test_every_verdict_round_trips(tmp_path, verdict):
    path = tmp_path / "verdict_history.jsonl"
    assert append_verdict_record(path, base_record(verdict=verdict)) == "appended"
    assert latest_verdict_record(path)["verdict"] == verdict


# ---------------------------------------------------------------------------
# the real scheduled wrapper: marker + history for every attempt
# ---------------------------------------------------------------------------


def run_wrapper(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    """Run ``scripts/run_weekly_scorecard.sh`` with every output inside ``tmp_path``."""
    out_dir = tmp_path / "scorecards"
    env = {
        **os.environ,
        "PYTHON_BIN": sys.executable,
        "JOURNAL_FILE": str(tmp_path / "trade_journal.csv"),
        "OUT_DIR": str(out_dir),
        "SCORECARD_PROFILE": "paper",
        "MIN_CLOSED_TRADES_TEST": "1",
        "SCORECARD_VERDICT_HISTORY_FILE": str(out_dir / "verdict_history.jsonl"),
    }
    env.update(extra)
    (tmp_path / "trade_journal.csv").write_text(
        "timestamp,symbol,action,side,quantity,price,fee,cost\n", encoding="utf-8"
    )
    return subprocess.run(
        ["bash", str(WRAPPER)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_wrapper_publishes_gate_and_task_marker_and_history(tmp_path: Path) -> None:
    completed = run_wrapper(tmp_path)

    # An empty journal is a HOLD (exit 2), not a script error.
    assert completed.returncode == 2, completed.stderr
    out_dir = tmp_path / "scorecards"
    marker = out_dir / "latest_status.env"

    # The gate contract (uppercase keys) must stay readable by src.scorecard_gate.
    gate_status = read_scorecard_status(marker)
    assert gate_status is not None
    assert gate_status["VERDICT"] == "HOLD"
    assert gate_status["RUN_MODE"] == "standard"
    assert gate_status["UNDERLYING_EXIT_CODE"] == "2"
    assert gate_status["TIMESTAMP_UTC"].endswith("Z")

    # The scheduled-task contract (lowercase keys) feeds the existing alerts.
    task_status = read_task_status(marker)
    assert task_status is not None
    assert task_exit_code(task_status) == 2
    assert task_last_run_age_seconds(task_status) is not None
    assert float(task_status["started_unixtime"]) <= float(
        task_status["finished_unixtime"]
    )

    records = read_verdict_history(str(out_dir / "verdict_history.jsonl"))
    assert len(records) == 1
    assert records[0]["verdict"] == "HOLD"
    assert records[0]["profile"] == "paper"
    assert records[0]["primary_reason"]


def test_wrapper_records_history_for_every_attempt(tmp_path: Path) -> None:
    run_wrapper(tmp_path)
    run_wrapper(tmp_path)
    history = str(tmp_path / "scorecards" / "verdict_history.jsonl")
    records = read_verdict_history(history)
    assert len(records) == 2
    assert len({record["record_id"] for record in records}) == 2


def test_wrapper_history_can_be_disabled(tmp_path: Path) -> None:
    completed = run_wrapper(tmp_path, SCORECARD_VERDICT_HISTORY_FILE="off")
    assert completed.returncode == 2, completed.stderr
    assert not (tmp_path / "scorecards" / "verdict_history.jsonl").exists()


def test_wrapper_aborts_replace_stale_go_marker(tmp_path: Path) -> None:
    """A hard failure must not leave last week's GO marker behind."""
    out_dir = tmp_path / "scorecards"
    out_dir.mkdir(parents=True)
    stale = out_dir / "latest_status.env"
    stale.write_text(
        "TIMESTAMP_UTC=2026-11-22T09:00:00Z\nRUN_MODE=standard\nVERDICT=GO\n"
        "UNDERLYING_EXIT_CODE=0\n",
        encoding="utf-8",
    )

    completed = run_wrapper(
        tmp_path,
        AUTORESEARCH_ENABLED="true",
        AUTORESEARCH_REQUIRED="true",
        AUTORESEARCH_PRECHECK="true",
        AUTORESEARCH_STRICT_ALLOW_MAINTENANCE_FALLBACK="false",
    )

    assert completed.returncode == 1, completed.stderr
    gate_status = read_scorecard_status(stale)
    assert gate_status is not None
    assert gate_status["VERDICT"] == "ERROR"

    records = read_verdict_history(str(out_dir / "verdict_history.jsonl"))
    assert len(records) == 1
    assert records[0]["verdict"] == "ERROR"
    assert records[0]["primary_reason"] == "autoresearch_precheck_failed"


def test_wrapper_json_marker_keeps_metrics_for_history(tmp_path: Path) -> None:
    completed = run_wrapper(tmp_path, STATUS_JSON_ENABLED="true")
    assert completed.returncode == 2, completed.stderr
    out_dir = tmp_path / "scorecards"

    status = resolve_scorecard_status(
        status_json_file=out_dir / "latest_status.json",
        status_env_file=out_dir / "latest_status.env",
        history_file=str(out_dir / "verdict_history.jsonl"),
    )
    assert status is not None
    assert status["source"] == "json"
    assert status["verdict"] == "HOLD"
    assert status["profile"] == "paper"
    assert "closed_trades" in status["metrics"]

    records = read_verdict_history(str(out_dir / "verdict_history.jsonl"))
    assert records[0]["metrics"].get("closed_trades") == 0
