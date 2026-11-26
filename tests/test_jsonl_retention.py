"""Tests for the append-only JSONL store retention policy.

The news item store and the scorecard verdict history are append-only and had
no pruning step, so both grew forever. ``src/jsonl_retention.py`` supplies the
policy and ``scripts/prune_jsonl_stores.py`` is the only thing that rewrites a
store. These tests pin the contract that makes that safe:

* a record inside the age horizon, inside the record floor, or without a
  usable timestamp is always kept (fail closed);
* the rewrite happens on the store's own inode under ``flock(LOCK_EX)``, so a
  concurrent appender never loses a record and no reader is cut off;
* the previous content is copied to a rotating backup before the rewrite;
* the run leaves a ``src/task_status.py`` marker so the generic scheduled-task
  alerts cover it.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.jsonl_retention import (
    DEFAULT_BACKUPS,
    DEFAULT_NEWS_DAYS,
    DEFAULT_NEWS_RECORDS,
    DEFAULT_VERDICT_DAYS,
    DEFAULT_VERDICT_RECORDS,
    MARKER_KEYS,
    RETENTION_SCHEMA_VERSION,
    StoreRetention,
    env_flag,
    env_non_negative_int,
    prune_jsonl_store,
    read_report_marker,
    report_int,
    report_marker_lines,
    resolve_backups,
    resolve_news_retention,
    resolve_report_path,
    resolve_verdict_retention,
    retention_enabled,
    write_report_marker,
)
from src.news_item_store import (
    DEFAULT_DEDUPE_WINDOW as NEWS_DEDUPE_WINDOW,
)
from src.news_item_store import (
    append_news_items,
    news_store_stats,
    replay_news_items,
)
from src.scorecard_verdict_history import DEFAULT_DEDUPE_WINDOW as VERDICT_DEDUPE_WINDOW
from src.scorecard_verdict_history import append_verdict_record, read_verdict_history
from src.task_status import read_task_status

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prune_jsonl_stores.py"

# Fixed clock: every fixture timestamp is relative to this so the tests never
# drift the way a "now - 2h" fixture does around UTC midnight.
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def _iso(moment: datetime | None) -> str:
    if moment is None:
        return ""
    return moment.astimezone(UTC).isoformat()


def news_line(
    tmp_path: Path,
    *,
    title: str,
    age_days: float | None = 1.0,
    fetched_at: str | None = None,
    stored_at: str | None = None,
    record_id: str | None = None,
) -> str:
    """One news store line, aged ``age_days`` before :data:`NOW`."""
    moment = NOW - timedelta(days=age_days) if age_days is not None else None
    payload = {
        "schema_version": 1,
        "record_id": record_id or f"sha256:{title}",
        "provider": "test",
        "title": title,
        "link": f"https://example.test/{title}",
        "source_url": "",
        "published_at": _iso(moment) if moment else "",
        "fetched_at": fetched_at if fetched_at is not None else _iso(moment),
        "content_sha256": "",
        "stored_at": stored_at if stored_at is not None else _iso(moment),
    }
    return json.dumps(payload, sort_keys=True)


def verdict_line(
    *,
    verdict: str = "GO",
    age_days: float | None = 1.0,
    timestamp_utc: str | None = None,
    report_file: str | None = None,
) -> str:
    moment = NOW - timedelta(days=age_days) if age_days is not None else None
    stamp = timestamp_utc if timestamp_utc is not None else _iso(moment)
    name = report_file or f"weekly_scorecard_paper_{stamp or 'undated'}_{verdict}.txt"
    payload = {
        "schema_version": 1,
        "record_id": f"report:{name}",
        "timestamp_utc": stamp,
        "verdict": verdict,
        "run_mode": "standard",
        "report_file": name,
    }
    return json.dumps(payload, sort_keys=True)


def write_store(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{line}\n" for line in lines)
    path.write_text(body, encoding="utf-8")


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def news_rule(path: Path, *, days: int = 90, min_records: int = 8) -> StoreRetention:
    return StoreRetention(
        store="news", path=str(path), days=days, min_records=min_records
    )


def verdict_rule(
    path: Path, *, days: int = 365, min_records: int = 4
) -> StoreRetention:
    return StoreRetention(
        store="verdict", path=str(path), days=days, min_records=min_records
    )


# ---------------------------------------------------------------------------
# defaults must never cut into what the owning store still needs
# ---------------------------------------------------------------------------


def test_default_floors_cover_the_store_dedupe_windows():
    """A prune must not remove records a store would still deduplicate against."""
    assert DEFAULT_NEWS_RECORDS >= NEWS_DEDUPE_WINDOW
    assert DEFAULT_VERDICT_RECORDS >= VERDICT_DEDUPE_WINDOW
    # The horizon has to be wider than the replay window the research signal
    # evaluates over, otherwise backtests would lose news they must replay.
    assert DEFAULT_NEWS_DAYS >= 180
    assert DEFAULT_VERDICT_DAYS >= 730


def test_rules_resolve_from_the_store_paths(monkeypatch):
    monkeypatch.delenv("TRADING_JSONL_RETENTION_NEWS_DAYS", raising=False)
    monkeypatch.delenv("TRADING_JSONL_RETENTION_NEWS_RECORDS", raising=False)
    monkeypatch.delenv("TRADING_JSONL_RETENTION_VERDICT_DAYS", raising=False)
    monkeypatch.delenv("TRADING_JSONL_RETENTION_VERDICT_RECORDS", raising=False)
    monkeypatch.delenv("RESEARCH_NEWS_STORE_PATH", raising=False)
    monkeypatch.delenv("SCORECARD_VERDICT_HISTORY_FILE", raising=False)

    news = resolve_news_retention()
    verdict = resolve_verdict_retention()
    assert news.enabled is True
    assert news.path == "data/research_news_items.jsonl"
    assert news.days == DEFAULT_NEWS_DAYS
    assert news.min_records == DEFAULT_NEWS_RECORDS
    assert news.timestamp_keys == ("fetched_at", "stored_at")
    assert verdict.path == "results/scorecards/verdict_history.jsonl"
    assert verdict.days == DEFAULT_VERDICT_DAYS
    assert verdict.min_records == DEFAULT_VERDICT_RECORDS
    assert verdict.timestamp_keys == ("timestamp_utc",)


def test_store_paths_follow_the_store_environment(monkeypatch):
    monkeypatch.setenv("RESEARCH_NEWS_STORE_PATH", "data/custom_news.jsonl")
    monkeypatch.setenv("SCORECARD_VERDICT_HISTORY_FILE", "run/verdicts.jsonl")
    assert resolve_news_retention().path == "data/custom_news.jsonl"
    assert resolve_verdict_retention().path == "run/verdicts.jsonl"


def test_malformed_retention_environment_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("TRADING_JSONL_RETENTION_NEWS_DAYS", "not-a-number")
    monkeypatch.setenv("TRADING_JSONL_RETENTION_NEWS_RECORDS", "-5")
    monkeypatch.setenv("TRADING_JSONL_RETENTION_VERDICT_DAYS", "")
    assert resolve_news_retention().days == DEFAULT_NEWS_DAYS
    assert resolve_news_retention().min_records == DEFAULT_NEWS_RECORDS
    assert resolve_verdict_retention().days == DEFAULT_VERDICT_DAYS


def test_zero_disables_one_dimension_without_touching_the_other(monkeypatch):
    monkeypatch.setenv("TRADING_JSONL_RETENTION_NEWS_DAYS", "0")
    monkeypatch.setenv("TRADING_JSONL_RETENTION_NEWS_RECORDS", "16")
    rule = resolve_news_retention()
    assert rule.days == 0
    assert rule.min_records == 16


def test_retention_switch_and_report_plumbing(monkeypatch):
    monkeypatch.setenv("TRADING_JSONL_RETENTION_ENABLED", "0")
    assert retention_enabled() is False
    monkeypatch.setenv("TRADING_JSONL_RETENTION_ENABLED", "true")
    assert retention_enabled() is True
    monkeypatch.delenv("TRADING_JSONL_RETENTION_ENABLED", raising=False)
    assert retention_enabled() is True

    monkeypatch.setenv("TRADING_JSONL_RETENTION_REPORT", "")
    assert resolve_report_path() == ""
    monkeypatch.delenv("TRADING_JSONL_RETENTION_REPORT", raising=False)
    assert resolve_report_path() == "results/jsonl_retention/latest_status.env"

    monkeypatch.setenv("TRADING_JSONL_RETENTION_BACKUPS", "3")
    assert resolve_backups() == 3
    monkeypatch.delenv("TRADING_JSONL_RETENTION_BACKUPS", raising=False)
    assert resolve_backups() == DEFAULT_BACKUPS

    assert env_flag("NOPE_MISSING", True) is True
    assert env_non_negative_int("NOPE_MISSING", 7) == 7


# ---------------------------------------------------------------------------
# what gets pruned
# ---------------------------------------------------------------------------


def test_news_prunes_only_beyond_the_age_horizon(tmp_path):
    path = tmp_path / "research_news_items.jsonl"
    write_store(
        path,
        [
            news_line(tmp_path, title="old-1", age_days=400),
            news_line(tmp_path, title="old-2", age_days=200),
            news_line(tmp_path, title="fresh", age_days=5),
        ],
    )
    result = prune_jsonl_store(news_rule(path, days=90, min_records=0), now=NOW)
    assert result["reason"] == "pruned"
    assert (result["scanned"], result["pruned"], result["kept"]) == (3, 2, 1)
    assert [json.loads(line)["title"] for line in read_lines(path)] == ["fresh"]
    assert result["records"] == 1


def test_verdict_prunes_only_beyond_the_age_horizon(tmp_path):
    path = tmp_path / "verdict_history.jsonl"
    write_store(
        path,
        [
            verdict_line(age_days=900),
            verdict_line(verdict="NO-GO", age_days=400),
            verdict_line(verdict="HOLD", age_days=3),
        ],
    )
    result = prune_jsonl_store(verdict_rule(path, days=365, min_records=0), now=NOW)
    assert (result["scanned"], result["pruned"], result["kept"]) == (3, 2, 1)
    assert len(read_verdict_history(str(path))) == 1


def test_record_floor_protects_the_newest_records_whatever_their_age(tmp_path):
    path = tmp_path / "research_news_items.jsonl"
    write_store(
        path,
        [
            news_line(tmp_path, title=f"old-{index}", age_days=900)
            for index in range(5)
        ],
    )
    result = prune_jsonl_store(news_rule(path, days=90, min_records=3), now=NOW)
    assert (result["pruned"], result["kept"]) == (2, 3)
    assert [json.loads(line)["title"] for line in read_lines(path)] == [
        "old-2",
        "old-3",
        "old-4",
    ]


def test_zero_record_floor_prunes_every_aged_record(tmp_path):
    """Regression: a floor of 0 used to protect the whole store."""
    path = tmp_path / "research_news_items.jsonl"
    write_store(
        path, [news_line(tmp_path, title="old", age_days=900) for _ in range(3)]
    )
    result = prune_jsonl_store(news_rule(path, days=90, min_records=0), now=NOW)
    assert (result["pruned"], result["kept"]) == (3, 0)
    assert path.read_text(encoding="utf-8") == ""


def test_zero_days_prunes_nothing_even_when_every_record_is_ancient(tmp_path):
    path = tmp_path / "research_news_items.jsonl"
    write_store(
        path, [news_line(tmp_path, title="old", age_days=9000) for _ in range(3)]
    )
    result = prune_jsonl_store(news_rule(path, days=0, min_records=0), now=NOW)
    assert result["reason"] == "unchanged"
    assert result["pruned"] == 0
    assert len(read_lines(path)) == 3


def test_undated_records_are_kept_and_malformed_lines_are_dropped(tmp_path):
    path = tmp_path / "research_news_items.jsonl"
    write_store(
        path,
        [
            news_line(tmp_path, title="undated", age_days=None),
            "this is not json at all",
            json.dumps({"title": "no record_id"}),
            news_line(tmp_path, title="old", age_days=500),
        ],
    )
    result = prune_jsonl_store(news_rule(path, days=90, min_records=0), now=NOW)
    assert result["malformed"] == 2
    assert result["undated"] == 1
    assert result["pruned"] == 1
    assert result["kept"] == 1
    remaining = read_lines(path)
    assert len(remaining) == 1
    assert json.loads(remaining[0])["title"] == "undated"


def test_naive_verdict_timestamps_are_still_pruned(tmp_path):
    """The history store accepts naive stamps, so retention must read them too."""
    path = tmp_path / "verdict_history.jsonl"
    write_store(
        path,
        [
            verdict_line(timestamp_utc="2020-01-01T00:00:00"),
            verdict_line(timestamp_utc=_iso(NOW - timedelta(days=1))),
        ],
    )
    result = prune_jsonl_store(verdict_rule(path, days=365, min_records=0), now=NOW)
    assert (result["pruned"], result["kept"], result["undated"]) == (1, 1, 0)


def test_news_records_without_any_usable_stamp_count_as_undated(tmp_path):
    path = tmp_path / "research_news_items.jsonl"
    write_store(
        path, [news_line(tmp_path, title="broken", fetched_at="", stored_at="")]
    )
    result = prune_jsonl_store(news_rule(path, days=1, min_records=0), now=NOW)
    assert (result["undated"], result["pruned"], result["kept"]) == (1, 0, 1)


# ---------------------------------------------------------------------------
# reasons that skip the rewrite
# ---------------------------------------------------------------------------


def test_missing_store_is_reported_not_created(tmp_path):
    path = tmp_path / "absent.jsonl"
    result = prune_jsonl_store(news_rule(path), now=NOW)
    assert result["reason"] == "missing"
    assert result["exists"] == 0
    assert not path.exists()


def test_empty_store_reports_empty(tmp_path):
    path = tmp_path / "research_news_items.jsonl"
    write_store(path, [])
    result = prune_jsonl_store(news_rule(path), now=NOW)
    assert result["reason"] == "empty"
    assert result["unchanged"] == 1


def test_disabled_rule_writes_nothing(tmp_path):
    """An empty store path switches the store off; the file is left alone."""
    path = tmp_path / "research_news_items.jsonl"
    write_store(path, [news_line(tmp_path, title="old", age_days=900)])
    before = path.read_bytes()
    rule = StoreRetention(store="news", path="", days=0, min_records=0)
    assert rule.enabled is False
    result = prune_jsonl_store(rule, now=NOW)
    assert result["reason"] == "disabled"
    assert result["enabled"] == 0
    assert path.read_bytes() == before


def test_store_beyond_the_size_guard_is_refused(tmp_path, monkeypatch):
    import src.jsonl_retention as retention

    monkeypatch.setattr(retention, "MAX_STORE_BYTES", 64)
    path = tmp_path / "research_news_items.jsonl"
    write_store(
        path,
        [news_line(tmp_path, title=f"t{index}", age_days=900) for index in range(5)],
    )
    result = prune_jsonl_store(news_rule(path, days=90, min_records=0), now=NOW)
    assert result["reason"] == "too_large"
    assert result["error"]
    assert len(read_lines(path)) == 5


def test_unreadable_store_reports_error_without_touching_the_file(tmp_path, monkeypatch):
    path = tmp_path / "research_news_items.jsonl"
    write_store(path, [news_line(tmp_path, title="old", age_days=900)])
    before = path.read_bytes()

    def boom(*_args, **_kwargs):
        raise OSError("device on fire")

    monkeypatch.setattr("builtins.open", boom)
    result = prune_jsonl_store(news_rule(path), now=NOW)
    assert result["reason"] == "error"
    assert "device on fire" in result["error"]
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# dry run, backups and inode stability
# ---------------------------------------------------------------------------


def test_dry_run_reports_counts_without_touching_the_store(tmp_path):
    path = tmp_path / "research_news_items.jsonl"
    write_store(
        path,
        [
            news_line(tmp_path, title="old", age_days=500),
            news_line(tmp_path, title="fresh", age_days=1),
        ],
    )
    before = path.read_bytes()
    result = prune_jsonl_store(
        news_rule(path, days=90, min_records=0), now=NOW, dry_run=True
    )
    assert result["reason"] == "dry_run"
    assert (result["pruned"], result["kept"]) == (1, 1)
    assert result["dry_run"] == 1
    assert path.read_bytes() == before
    assert list(path.parent.glob("*.bak-*")) == []


def test_second_pass_is_unchanged_and_idempotent(tmp_path):
    path = tmp_path / "research_news_items.jsonl"
    write_store(
        path,
        [
            news_line(tmp_path, title="old", age_days=500),
            news_line(tmp_path, title="fresh", age_days=1),
        ],
    )
    rule = news_rule(path, days=90, min_records=0)
    first = prune_jsonl_store(rule, now=NOW)
    second = prune_jsonl_store(rule, now=NOW)
    assert first["reason"] == "pruned"
    assert second["reason"] == "unchanged"
    assert second["unchanged"] == 1
    assert second["pruned"] == 0
    assert len(list(path.parent.glob("*.bak-*"))) == 1


def test_backup_holds_the_previous_content_and_rotates(tmp_path):
    path = tmp_path / "research_news_items.jsonl"
    rule = news_rule(path, days=90, min_records=0)

    for round_index in range(3):
        write_store(
            path,
            [
                news_line(tmp_path, title=f"round-{round_index}", age_days=500),
                news_line(tmp_path, title=f"keep-{round_index}", age_days=1),
            ],
        )
        result = prune_jsonl_store(rule, now=NOW + timedelta(days=round_index), backups=2)
        assert result["reason"] == "pruned"
        assert result["backup"]

    backups = sorted(path.parent.glob("research_news_items.jsonl.bak-*"))
    assert len(backups) == 2
    oldest = backups[0].read_text(encoding="utf-8")
    assert "round-1" in oldest
    assert all("round-0" not in backup.read_text(encoding="utf-8") for backup in backups)
    assert backups[1].read_text(encoding="utf-8").count("\n") == 2


def test_backups_can_be_switched_off(tmp_path):
    path = tmp_path / "research_news_items.jsonl"
    write_store(
        path,
        [
            news_line(tmp_path, title="old", age_days=500),
            news_line(tmp_path, title="fresh", age_days=1),
        ],
    )
    result = prune_jsonl_store(
        news_rule(path, days=90, min_records=0), now=NOW, backups=0
    )
    assert result["reason"] == "pruned"
    assert result["backup"] == ""
    assert list(path.parent.glob("*.bak-*")) == []


def test_pruning_a_store_written_by_the_history_itself(tmp_path):
    """Real ``append_verdict_record`` output must be prunable, not just fixtures."""
    path = tmp_path / "verdict_history.jsonl"
    for age_days in (900, 500, 2):
        stamp = NOW - timedelta(days=age_days)
        outcome = append_verdict_record(
            str(path),
            {
                "verdict": "GO",
                "timestamp_utc": stamp.isoformat(),
                "run_mode": "standard",
                "report_file": f"weekly_scorecard_paper_{stamp.date()}.txt",
            },
        )
        assert outcome == "appended"
    assert len(read_verdict_history(str(path))) == 3
    result = prune_jsonl_store(
        verdict_rule(path, days=DEFAULT_VERDICT_DAYS, min_records=0), now=NOW
    )
    # Only the 900-day record is outside the default two-year horizon.
    assert (result["scanned"], result["pruned"], result["kept"]) == (3, 1, 2)
    assert len(read_verdict_history(str(path))) == 2


def test_pruning_keeps_the_store_inode(tmp_path):
    """A reader holding the file open must stay on the same content."""
    path = tmp_path / "research_news_items.jsonl"
    write_store(
        path,
        [
            news_line(tmp_path, title="old", age_days=500),
            news_line(tmp_path, title="fresh", age_days=1),
        ],
    )
    before = os.stat(path)
    result = prune_jsonl_store(news_rule(path, days=90, min_records=0), now=NOW)
    after = os.stat(path)
    assert result["reason"] == "pruned"
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


def test_concurrent_appender_and_pruner_lose_nothing(tmp_path):
    """The store lock serialises a prune and an append, whichever wins."""
    path = tmp_path / "research_news_items.jsonl"
    write_store(
        path, [news_line(tmp_path, title=f"old-{index}", age_days=500) for index in range(3)]
    )
    item = {
        "title": "appended",
        "link": "https://example.test/appended",
        "published_at": _iso(NOW),
        "fetched_at": _iso(NOW),
    }
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child process
        append_news_items(str(path), [item], stored_at=NOW)
        os._exit(0)
    result = prune_jsonl_store(
        news_rule(path, days=90, min_records=0), now=NOW, backups=0
    )
    status = os.waitpid(pid, 0)[1]
    assert status == 0
    assert result["reason"] in ("pruned", "unchanged")

    lines = read_lines(path)
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["title"] == "appended"
    stats = news_store_stats(str(path))
    assert stats["records"] == 1


def test_prune_waits_for_an_exclusive_store_lock(tmp_path):
    path = tmp_path / "research_news_items.jsonl"
    write_store(
        path,
        [
            news_line(tmp_path, title="old", age_days=500),
            news_line(tmp_path, title="fresh", age_days=1),
        ],
    )
    child = tmp_path / "child.py"
    child.write_text(
        "import sys\n"
        "from datetime import datetime\n"
        "from src.jsonl_retention import StoreRetention, prune_jsonl_store\n"
        "rule = StoreRetention(store='news', path=sys.argv[1], days=90, min_records=0)\n"
        "now = datetime.fromisoformat(sys.argv[2])\n"
        "out = prune_jsonl_store(rule, now=now, backups=0)\n"
        "print(out['reason'])\n",
        encoding="utf-8",
    )
    with open(path, "a+", encoding="utf-8") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc = subprocess.Popen(
            [sys.executable, str(child), str(path), NOW.isoformat()],
            cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                proc.wait(timeout=3)
            # The child is still blocked on the store lock, so nothing changed.
            assert len(read_lines(path)) == 2
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            returncode = proc.wait(timeout=30)
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    assert returncode == 0
    assert proc.stdout.read().strip().splitlines()[-1] == "pruned"
    assert len(read_lines(path)) == 1


# ---------------------------------------------------------------------------
# the replay contract the news store depends on
# ---------------------------------------------------------------------------


def test_pruning_never_drops_a_record_inside_the_replay_horizon(tmp_path):
    """Backtests replay over the default horizon, so that window must survive."""
    path = tmp_path / "research_news_items.jsonl"
    ages = [index * 20 for index in range(12)]  # 0 .. 220 days
    write_store(
        path,
        [news_line(tmp_path, title=f"news-{age}", age_days=age) for age in ages],
    )
    as_of = NOW
    before = {record["record_id"] for record in replay_news_items(str(path), as_of)}
    inside = {
        f"sha256:news-{age}"
        for age in ages
        if age <= DEFAULT_NEWS_DAYS
    }
    assert inside < before

    prune_jsonl_store(
        StoreRetention(
            store="news",
            path=str(path),
            days=DEFAULT_NEWS_DAYS,
            min_records=0,
        ),
        now=as_of,
    )
    after = {record["record_id"] for record in replay_news_items(str(path), as_of)}
    assert after == inside
    assert len(read_lines(path)) == len(inside)


# ---------------------------------------------------------------------------
# marker contract
# ---------------------------------------------------------------------------


def test_marker_round_trips_through_the_task_status_reader(tmp_path):
    path = tmp_path / "results" / "jsonl_retention" / "latest_status.env"
    values = {
        "schema_version": RETENTION_SCHEMA_VERSION,
        "status": "complete",
        "reason": "ok",
        "exit_code": 0,
        "started_unixtime": int(NOW.timestamp()),
        "finished_unixtime": int(NOW.timestamp()),
        "backups_kept": 1,
        "news_enabled": 1,
        "news_path": "data/research_news_items.jsonl",
        "news_days": 180,
        "news_min_records": 8192,
        "news_scanned": 10,
        "news_pruned": 4,
        "news_kept": 6,
        "news_undated": 0,
        "news_malformed": 0,
        "news_records": 6,
        "news_bytes": 1024,
        "news_error": "",
        "verdict_enabled": 1,
        "verdict_path": "results/scorecards/verdict_history.jsonl",
        "verdict_days": 730,
        "verdict_min_records": 512,
        "verdict_scanned": 3,
        "verdict_pruned": 1,
        "verdict_kept": 2,
        "verdict_undated": 0,
        "verdict_malformed": 0,
        "verdict_records": 2,
        "verdict_bytes": 512,
        "verdict_error": "",
    }
    assert write_report_marker(path, values) is True
    marker = read_report_marker(path)
    assert marker is not None
    assert marker["exit_code"] == "0"
    assert marker["news_pruned"] == "4"
    assert marker["verdict_path"] == "results/scorecards/verdict_history.jsonl"
    assert read_task_status(path)["status"] == "complete"
    assert report_int(marker, "news_pruned") == 4
    # An empty error field is not a number: a reader must not read it as zero.
    assert report_int(marker, "news_error") is None
    assert report_int({}, "missing") is None
    assert read_report_marker(tmp_path / "absent.env") is None
    assert read_report_marker("") is None


def test_marker_lines_are_lowercase_key_value_and_filtered():
    lines = report_marker_lines(
        {"exit_code": 0, "news_pruned": 2, "store": "news", "nope": 1, "news_error": ""}
    )
    text = "\n".join(lines)
    assert text.startswith("schema_version=1\n")
    assert "exit_code=0\n" in text
    assert "news_pruned=2\n" in text
    assert "news_error=" in text
    assert text.endswith("news_error=")
    assert "nope" not in text
    assert "store=news" not in text
    assert all(line.split("=", 1)[0].islower() for line in lines)
    assert all(line.split("=", 1)[0] in MARKER_KEYS for line in lines)


# ---------------------------------------------------------------------------
# the scheduled entry point
# ---------------------------------------------------------------------------


def run_cli(tmp_path: Path, *args: str, env: dict[str, str] | None = None):
    environ = {
        "PYTHONPATH": str(ROOT),
        "PATH": os.environ.get("PATH", ""),
        "TRADING_JSONL_RETENTION_NEWS_DAYS": "90",
        "TRADING_JSONL_RETENTION_NEWS_RECORDS": "0",
        "TRADING_JSONL_RETENTION_VERDICT_DAYS": "365",
        "TRADING_JSONL_RETENTION_VERDICT_RECORDS": "0",
        "TRADING_JSONL_RETENTION_REPORT": str(tmp_path / "latest_status.env"),
    }
    environ.update(env or {})
    return subprocess.run(  # noqa: PLW1510 - the exit codes are the contract here
        [sys.executable, str(SCRIPT), *args],
        cwd=str(tmp_path),
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_prunes_both_stores_and_writes_a_marker(tmp_path):
    news = tmp_path / "data" / "research_news_items.jsonl"
    verdict = tmp_path / "results" / "scorecards" / "verdict_history.jsonl"
    write_store(
        news,
        [
            news_line(tmp_path, title="old", age_days=500),
            news_line(tmp_path, title="fresh", age_days=1),
        ],
    )
    write_store(
        verdict,
        [verdict_line(age_days=900), verdict_line(verdict="HOLD", age_days=2)],
    )
    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert len(read_lines(news)) == 1
    assert len(read_lines(verdict)) == 1
    marker = read_task_status(tmp_path / "latest_status.env")
    assert marker["exit_code"] == "0"
    assert marker["news_pruned"] == "1"
    assert marker["verdict_pruned"] == "1"
    assert marker["news_days"] == "90"
    assert marker["verdict_days"] == "365"
    assert int(marker["finished_unixtime"]) >= int(marker["started_unixtime"])
    assert list(news.parent.glob("research_news_items.jsonl.bak-*"))
    assert list(verdict.parent.glob("verdict_history.jsonl.bak-*"))


def test_cli_second_run_is_unchanged(tmp_path):
    news = tmp_path / "data" / "research_news_items.jsonl"
    write_store(
        news,
        [
            news_line(tmp_path, title="old", age_days=500),
            news_line(tmp_path, title="fresh", age_days=1),
        ],
    )
    assert run_cli(tmp_path).returncode == 0
    second = run_cli(tmp_path)
    assert second.returncode == 0
    assert "unchanged" in second.stdout
    assert len(read_lines(news)) == 1


def test_cli_dry_run_leaves_the_stores_byte_identical(tmp_path):
    news = tmp_path / "data" / "research_news_items.jsonl"
    write_store(news, [news_line(tmp_path, title="old", age_days=500)])
    before = news.read_bytes()
    proc = run_cli(tmp_path, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "dry_run" in proc.stdout
    assert news.read_bytes() == before
    assert list(news.parent.glob("*.bak-*")) == []


def test_cli_missing_stores_are_not_created(tmp_path):
    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "missing" in proc.stdout
    assert not (tmp_path / "data" / "research_news_items.jsonl").exists()


def test_cli_disabled_retention_exits_two_without_writing(tmp_path):
    news = tmp_path / "data" / "research_news_items.jsonl"
    write_store(news, [news_line(tmp_path, title="old", age_days=500)])
    before = news.read_bytes()
    proc = run_cli(
        tmp_path, env={"TRADING_JSONL_RETENTION_ENABLED": "0"}
    )
    assert proc.returncode == 2
    assert "disabled" in proc.stderr
    assert news.read_bytes() == before
    assert not (tmp_path / "latest_status.env").exists()


def test_cli_no_report_skips_the_marker(tmp_path):
    proc = run_cli(tmp_path, "--no-report")
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "latest_status.env").exists()


def test_cli_store_failed_exits_one_and_marks_the_marker(tmp_path):
    news = tmp_path / "data" / "research_news_items.jsonl"
    news.mkdir(parents=True)
    # A directory where the store file must be: forces an open() failure.
    (news / "blocker").write_text("x", encoding="utf-8")
    proc = run_cli(
        tmp_path,
        env={"RESEARCH_NEWS_STORE_PATH": str(news)},
    )
    assert proc.returncode == 1
    marker = read_task_status(tmp_path / "latest_status.env")
    assert marker["exit_code"] == "1"
    assert marker["news_error"]


def test_cli_command_line_overrides_win_over_the_environment(tmp_path):
    news = tmp_path / "data" / "research_news_items.jsonl"
    write_store(
        news,
        [
            news_line(tmp_path, title="old", age_days=500),
            news_line(tmp_path, title="mid", age_days=20),
        ],
    )
    proc = run_cli(tmp_path, "--news-days", "30", "--news-records", "1")
    assert proc.returncode == 0, proc.stderr
    titles = [json.loads(line)["title"] for line in read_lines(news)]
    assert titles == ["mid"]
    marker = read_task_status(tmp_path / "latest_status.env")
    assert marker["news_days"] == "30"
    assert marker["news_min_records"] == "1"


def test_cli_custom_store_paths_are_pruned(tmp_path):
    news = tmp_path / "run" / "news.jsonl"
    verdict = tmp_path / "run" / "verdicts.jsonl"
    write_store(news, [news_line(tmp_path, title="old", age_days=500)])
    write_store(verdict, [verdict_line(age_days=900)])
    proc = run_cli(
        tmp_path,
        "--news-store",
        str(news),
        "--verdict-store",
        str(verdict),
    )
    assert proc.returncode == 0, proc.stderr
    assert news.read_text(encoding="utf-8") == ""
    assert verdict.read_text(encoding="utf-8") == ""
