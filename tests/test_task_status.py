"""Reader tests for the scheduled-wrapper status markers.

The wrappers are bash and the reader is Python, so these tests are the contract
between the two: whatever ``scripts/run_*.sh`` writes must be readable here, and
every unusable marker must return ``None`` instead of raising.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from src.task_status import (
    parse_task_status_files,
    read_task_status,
    task_exit_code,
    task_last_run_age_seconds,
)

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def test_read_task_status_keeps_all_key_value_lines(tmp_path):
    marker = tmp_path / "latest_status.env"
    marker.write_text(
        "# comment is ignored\n"
        "exit_code=0\n"
        "started_unixtime=1764000000\n"
        "finished_unixtime=1764000060\n"
        "unknown_key=kept\n",
        encoding="utf-8",
    )

    status = read_task_status(marker)

    assert status is not None
    assert status["exit_code"] == "0"
    assert status["finished_unixtime"] == "1764000060"
    assert status["unknown_key"] == "kept"


def test_read_task_status_returns_none_for_unusable_markers(tmp_path):
    missing = tmp_path / "missing.env"
    assert read_task_status(missing) is None

    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    assert read_task_status(empty) is None

    garbage = tmp_path / "garbage.env"
    garbage.write_text("not a marker at all\n", encoding="utf-8")
    assert read_task_status(garbage) is None

    directory = tmp_path / "directory.env"
    directory.mkdir()
    assert read_task_status(directory) is None


def test_task_exit_code_requires_an_integer() -> None:
    assert task_exit_code({"exit_code": "0"}) == 0
    assert task_exit_code({"exit_code": " 2 "}) == 2
    assert task_exit_code({"exit_code": "abc"}) is None
    assert task_exit_code({}) is None


def test_task_last_run_age_seconds_uses_finished_unixtime() -> None:
    finished = NOW.timestamp() - 120
    assert task_last_run_age_seconds({"finished_unixtime": str(finished)}, NOW) == 120.0

    # A run that finished in the future must not produce a negative age.
    future = NOW.timestamp() + 60
    assert task_last_run_age_seconds({"finished_unixtime": str(future)}, NOW) == 0.0

    for raw in ("", "abc", "0", "-5"):
        assert task_last_run_age_seconds({"finished_unixtime": raw}, NOW) is None
    assert task_last_run_age_seconds({}, NOW) is None


def test_parse_task_status_files_requires_label_safe_names() -> None:
    parsed = parse_task_status_files(
        "daily_review=/tmp/a.env, public_paper=/tmp/b.env")
    assert parsed == {"daily_review": Path("/tmp/a.env"),
                      "public_paper": Path("/tmp/b.env")}

    # Malformed entries are dropped rather than guessed at, because the name
    # becomes a Prometheus label value.
    assert parse_task_status_files("") == {}
    assert parse_task_status_files("/tmp/unnamed.env") == {}
    assert parse_task_status_files("broken=") == {}
    assert parse_task_status_files("Bad Name=/tmp/a.env") == {}
    assert parse_task_status_files("9lives=/tmp/a.env") == {}
    assert parse_task_status_files("ok=/tmp/a.env, ,=x,ok2=/tmp/b.env") == {
        "ok": Path("/tmp/a.env"),
        "ok2": Path("/tmp/b.env"),
    }
