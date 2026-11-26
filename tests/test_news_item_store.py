"""Tests for the append-only per-item news store in :mod:`src.news_item_store`.

The store exists so a decision can be replayed against the news that was
visible at that point in time, so the tests focus on the properties that make
that safe: immutability, idempotent identity, both timestamps surviving a
round-trip, and point-in-time reads.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.news_item_store import (
    DEFAULT_NEWS_ITEM_STORE_PATH,
    NEWS_ITEM_STORE_SCHEMA_VERSION,
    append_news_items,
    news_item_identity,
    news_store_stats,
    parse_store_timestamp,
    read_news_items,
    replay_news_items,
    resolve_store_path,
    to_store_record,
)

PUBLISHED = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
FETCHED = datetime(2026, 3, 1, 12, 5, tzinfo=UTC)
STORED = datetime(2026, 3, 1, 12, 6, tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _item(
    title: str = "ETF approved for spot index",
    link: str = "https://news.test/etf",
    *,
    published_at: str | None = None,
    fetched_at: str | None = None,
) -> dict[str, str]:
    return {
        "title": title,
        "link": link,
        "source_url": "https://feed.test/rss",
        "published_at": _iso(PUBLISHED) if published_at is None else published_at,
        "fetched_at": _iso(FETCHED) if fetched_at is None else fetched_at,
        "content_sha256": "abc123",
    }


def _lines(path) -> list[str]:
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_default_store_path_lives_next_to_the_signal_file() -> None:
    assert DEFAULT_NEWS_ITEM_STORE_PATH == "data/research_news_items.jsonl"


def test_parse_store_timestamp_requires_timezone_aware_iso_values() -> None:
    assert parse_store_timestamp(_iso(FETCHED)) == FETCHED
    assert parse_store_timestamp("2026-03-01T12:05:00") is None
    assert parse_store_timestamp("not-a-time") is None
    assert parse_store_timestamp("") is None
    assert parse_store_timestamp(None) is None


def test_identity_prefers_the_link_and_falls_back_to_content_hash() -> None:
    assert news_item_identity(_item()) == "url:https://news.test/etf"
    assert news_item_identity(_item(link="HTTPS://News.test/ETF")) == news_item_identity(_item())
    hashed = news_item_identity({"title": "Headline", "published_at": _iso(PUBLISHED)})
    assert hashed.startswith("sha256:")
    assert news_item_identity({}) == ""


def test_to_store_record_keeps_both_timestamps_and_adds_provenance() -> None:
    record = to_store_record(_item(), stored_at=STORED)

    assert record is not None
    assert record["schema_version"] == NEWS_ITEM_STORE_SCHEMA_VERSION
    assert record["provider"] == "rss_news"
    assert record["published_at"] == _iso(PUBLISHED)
    assert record["fetched_at"] == _iso(FETCHED)
    assert record["stored_at"] == _iso(STORED)
    assert record["content_sha256"] == "abc123"
    assert record["source_url"] == "https://feed.test/rss"


@pytest.mark.parametrize(
    "item",
    [
        _item(published_at=""),
        _item(published_at="2026-03-01T12:00:00"),
        _item(fetched_at=""),
        _item(fetched_at="yesterday"),
        {"title": "", "link": "", "published_at": _iso(PUBLISHED), "fetched_at": _iso(FETCHED)},
    ],
)
def test_to_store_record_rejects_items_that_cannot_be_replayed(item: dict[str, str]) -> None:
    assert to_store_record(item, stored_at=STORED) is None


def test_append_writes_one_sorted_json_object_per_line(tmp_path) -> None:
    path = str(tmp_path / "news.jsonl")

    counts = append_news_items(path, [_item()], stored_at=STORED)

    assert counts == {"appended": 1, "duplicates": 0, "invalid": 0}
    lines = _lines(tmp_path / "news.jsonl")
    assert len(lines) == 1
    assert json.loads(lines[0]) == json.loads(json.dumps(json.loads(lines[0]), sort_keys=True))


def test_append_is_idempotent_for_the_same_item(tmp_path) -> None:
    path = str(tmp_path / "news.jsonl")
    first = _item()
    same_link = _item(title="Retitled headline")

    append_news_items(path, [first], stored_at=STORED)
    second = append_news_items(path, [same_link], stored_at=STORED)

    assert second["appended"] == 0
    assert second["duplicates"] == 1
    assert len(_lines(tmp_path / "news.jsonl")) == 1


def test_append_never_rewrites_existing_records(tmp_path) -> None:
    path = str(tmp_path / "news.jsonl")
    append_news_items(path, [_item(link="https://news.test/one")], stored_at=STORED)
    before = _lines(tmp_path / "news.jsonl")

    append_news_items(path, [_item(link="https://news.test/two")], stored_at=STORED)

    after = _lines(tmp_path / "news.jsonl")
    assert after[: len(before)] == before
    assert len(after) == 2


def test_append_skips_invalid_items_and_counts_them(tmp_path) -> None:
    path = str(tmp_path / "news.jsonl")

    counts = append_news_items(
        path,
        [_item(), _item(published_at="")],
        stored_at=STORED,
    )

    assert counts == {"appended": 1, "duplicates": 0, "invalid": 1}


def test_append_creates_the_directory_and_repairs_a_torn_final_line(tmp_path) -> None:
    path = tmp_path / "store" / "news.jsonl"
    path.parent.mkdir()
    path.write_text('{"record_id": "url:https://news.test/one"}', encoding="utf-8")

    counts = append_news_items(str(path), [_item()], stored_at=STORED)

    lines = _lines(path)
    assert counts["appended"] == 1
    assert json.loads(lines[0]) == {"record_id": "url:https://news.test/one"}
    assert json.loads(lines[1])["record_id"] == news_item_identity(_item())


def test_read_news_items_skips_unreadable_lines(tmp_path) -> None:
    path = tmp_path / "news.jsonl"
    record = to_store_record(_item(), stored_at=STORED)
    assert record is not None
    path.write_text(
        "\n".join(
            [
                json.dumps(record, sort_keys=True),
                "{torn write",
                "",
            ]
        ),
        encoding="utf-8",
    )

    items = read_news_items(str(path))

    assert [item["record_id"] for item in items] == [record["record_id"]]


def test_read_news_items_returns_the_recent_tail_in_order(tmp_path) -> None:
    path = str(tmp_path / "news.jsonl")
    links = [f"https://news.test/{index}" for index in range(5)]
    append_news_items(path, [_item(link=link) for link in links], stored_at=STORED)

    items = read_news_items(path, limit=2)

    assert [item["link"] for item in items] == links[-2:]


def test_read_news_items_handles_a_missing_or_empty_store(tmp_path) -> None:
    assert read_news_items(str(tmp_path / "missing.jsonl")) == []
    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    assert read_news_items(str(tmp_path / "empty.jsonl")) == []


def test_read_news_items_reads_a_tail_written_across_chunk_boundaries(tmp_path) -> None:
    path = tmp_path / "news.jsonl"
    rows = []
    for index in range(400):
        record = to_store_record(
            _item(link=f"https://news.test/{index}", title="x" * 200),
            stored_at=STORED,
        )
        assert record is not None
        rows.append(record)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    items = read_news_items(str(path), limit=3)

    assert [item["link"] for item in items] == [row["link"] for row in rows[-3:]]


def test_replay_news_items_only_returns_records_visible_at_the_decision_time(tmp_path) -> None:
    path = str(tmp_path / "news.jsonl")
    early = _item(
        link="https://news.test/early",
        published_at=_iso(PUBLISHED),
        fetched_at=_iso(FETCHED),
    )
    late_fetch = _item(
        link="https://news.test/late-fetch",
        published_at=_iso(PUBLISHED - timedelta(days=1)),
        fetched_at=_iso(FETCHED + timedelta(hours=2)),
    )
    late_publish = _item(
        link="https://news.test/late-publish",
        published_at=_iso(PUBLISHED + timedelta(hours=3)),
        fetched_at=_iso(FETCHED + timedelta(hours=3)),
    )
    append_news_items(
        path,
        [late_fetch, early, late_publish],
        stored_at=STORED,
    )

    replayed = replay_news_items(path, FETCHED)

    assert [item["link"] for item in replayed] == ["https://news.test/early"]


def test_replay_news_items_sorts_by_publication_time(tmp_path) -> None:
    path = str(tmp_path / "news.jsonl")
    older = _item(
        link="https://news.test/older",
        published_at=_iso(PUBLISHED - timedelta(hours=1)),
    )
    newer = _item(link="https://news.test/newer", published_at=_iso(PUBLISHED))
    append_news_items(path, [newer, older], stored_at=STORED)

    replayed = replay_news_items(path, FETCHED + timedelta(days=1))

    assert [item["link"] for item in replayed] == [
        "https://news.test/older",
        "https://news.test/newer",
    ]


def test_replay_news_items_accepts_naive_decision_times_and_missing_stores(tmp_path) -> None:
    path = str(tmp_path / "news.jsonl")
    append_news_items(path, [_item()], stored_at=STORED)

    naive = FETCHED.replace(tzinfo=None)
    assert len(replay_news_items(path, naive)) == 1
    assert replay_news_items(str(tmp_path / "missing.jsonl"), FETCHED) == []


def test_replay_news_items_skips_unreadable_lines(tmp_path) -> None:
    path = tmp_path / "news.jsonl"
    record = to_store_record(_item(), stored_at=STORED)
    assert record is not None
    path.write_text(
        "garbage\n" + json.dumps(record, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert len(replay_news_items(str(path), FETCHED)) == 1


def test_news_store_stats_summarises_the_store(tmp_path) -> None:
    path = str(tmp_path / "news.jsonl")
    append_news_items(path, [_item(), _item()], stored_at=STORED)

    stats = news_store_stats(path)

    assert stats["exists"] is True
    assert stats["records"] == 1
    assert stats["newest_fetched_at"] == _iso(FETCHED)


def test_news_store_stats_reports_a_missing_store(tmp_path) -> None:
    stats = news_store_stats(str(tmp_path / "missing.jsonl"))

    assert stats["exists"] is False
    assert stats["records"] == 0
    assert stats["newest_fetched_at"] is None


def test_resolve_store_path_prefers_the_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RESEARCH_NEWS_STORE_PATH", str(tmp_path / "env.jsonl"))

    assert resolve_store_path({"integration": {"news_store_path": "ignored"}}) == str(
        tmp_path / "env.jsonl"
    )


def test_resolve_store_path_falls_back_to_the_signal_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RESEARCH_NEWS_STORE_PATH", raising=False)
    expected = str(tmp_path / "payload.jsonl")

    assert resolve_store_path({"integration": {"news_store_path": expected}}) == expected
    assert resolve_store_path({"source_details": {"news_store_path": expected}}) == expected
    assert resolve_store_path({"integration": {}}) == ""
    assert resolve_store_path(None) == ""


def test_resolve_store_path_ignores_blank_environment_values(monkeypatch) -> None:
    monkeypatch.setenv("RESEARCH_NEWS_STORE_PATH", "  ")

    assert resolve_store_path({"integration": {"news_store_path": "data/x.jsonl"}}) == (
        "data/x.jsonl"
    )
