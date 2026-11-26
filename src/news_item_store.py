"""Append-only store for the individual news items behind a research signal.

The canonical research signal file only carries the news items of the most
recent fetch, so a decision can never be replayed against the news that was
visible at an earlier point in time, and a re-fetched headline silently replaces
history. This module keeps every scored item as an immutable JSONL record that
preserves both the source ``published_at`` and the local ``fetched_at``
timestamp, which is what a backtest needs to avoid lookahead.

Contract:

* One JSON object per line, ``sort_keys=True``, UTF-8, never rewritten.
* ``record_id`` is the stable identity of the item (link, else content hash),
  so re-running the hourly fetch is idempotent.
* Records are only appended. Retention/rotation stays an operator task, like
  the trade journal.
* A line that cannot be parsed is skipped by every reader, so a torn write
  from a killed process cannot make the store unreadable.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.env_parsing import env_str

NEWS_ITEM_STORE_SCHEMA_VERSION = 1
NEWS_ITEM_STORE_PATH_ENV = "RESEARCH_NEWS_STORE_PATH"
DEFAULT_NEWS_ITEM_STORE_PATH = "data/research_news_items.jsonl"
DEFAULT_NEWS_ITEM_PROVIDER = "rss_news"

# Identity look-back when appending. Duplicates only ever reappear inside the
# RSS lookback window (hours, not months), so a bounded tail keeps the append
# cheap even after years of hourly fetches.
DEFAULT_DEDUPE_WINDOW = 4096
# Upper bound for readers that only need the recent tail (the news veto).
DEFAULT_READ_LIMIT = 1000

_TAIL_CHUNK_BYTES = 65536


def _iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_store_timestamp(value: Any) -> datetime | None:
    """Parses a store timestamp. Naive or malformed values are rejected."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def news_item_identity(item: Mapping[str, Any]) -> str:
    """Stable identity for one news item: the link, else a content/time hash.

    Empty when the item has neither a link nor a title, i.e. when it cannot be
    identified and therefore must not be stored.
    """
    link = str(item.get("link") or "").strip()
    if link:
        return f"url:{link.casefold()}"
    title = str(item.get("title") or "").strip()
    if not title:
        return ""
    published = str(item.get("published_at") or "").strip()
    digest = f"{title}{published}".encode()
    return f"sha256:{_sha256(digest)}"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def to_store_record(
    item: Mapping[str, Any],
    *,
    stored_at: datetime,
    provider: str = DEFAULT_NEWS_ITEM_PROVIDER,
) -> dict[str, Any] | None:
    """Normalises one fetched news item into an immutable store record.

    Returns ``None`` for items without an identity or without both timestamps,
    because such a record could never be replayed safely.
    """
    published_at = str(item.get("published_at") or "").strip()
    fetched_at = str(item.get("fetched_at") or "").strip()
    if parse_store_timestamp(published_at) is None:
        return None
    if parse_store_timestamp(fetched_at) is None:
        return None
    record_id = news_item_identity(item)
    if not record_id:
        return None
    return {
        "schema_version": NEWS_ITEM_STORE_SCHEMA_VERSION,
        "record_id": record_id,
        "provider": str(provider or DEFAULT_NEWS_ITEM_PROVIDER),
        "title": str(item.get("title") or ""),
        "link": str(item.get("link") or "").strip(),
        "source_url": str(item.get("source_url") or "").strip(),
        "published_at": published_at,
        "fetched_at": fetched_at,
        "content_sha256": str(item.get("content_sha256") or ""),
        "stored_at": _iso_utc(stored_at),
    }


def resolve_store_path(signal: Mapping[str, Any] | None = None) -> str:
    """Where the per-item store lives: env override, else the signal payload.

    ``scripts/fetch_research_signal.py`` records the path it wrote into the
    signal payload, so the bot needs no extra configuration to find the store.
    An empty result means the store is not in use.
    """
    configured = env_str(NEWS_ITEM_STORE_PATH_ENV, "").strip()
    if configured:
        return configured
    if isinstance(signal, Mapping):
        for section in ("integration", "source_details"):
            value = signal.get(section)
            if isinstance(value, Mapping):
                candidate = value.get("news_store_path")
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
    return ""


def _iter_tail_lines(path: str, limit: int) -> list[bytes]:
    """Reads at most ``limit`` trailing lines without loading the whole file."""
    if limit <= 0:
        return []
    try:
        with open(path, "rb") as raw:
            raw.seek(0, os.SEEK_END)
            position = raw.tell()
            if position == 0:
                return []
            collected: list[bytes] = []
            partial = b""
            while position > 0 and len(collected) < limit:
                step = min(_TAIL_CHUNK_BYTES, position)
                position -= step
                raw.seek(position)
                block = raw.read(step) + partial
                parts = block.split(b"\n")
                partial = parts[0]
                collected[0:0] = [part for part in parts[1:] if part.strip()]
            if position == 0 and partial:
                collected.insert(0, partial)
            return collected[-limit:]
    except OSError:
        return []


def _iter_records(lines: Iterable[bytes | str]) -> Iterable[dict[str, Any]]:
    for line in lines:
        if not isinstance(line, bytes):
            line = line.encode("utf-8")
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(record, dict) and record.get("record_id"):
            yield record


def read_news_items(path: str, *, limit: int = DEFAULT_READ_LIMIT) -> list[dict[str, Any]]:
    """Reads the most recent store records, skipping unreadable lines."""
    if not path or not os.path.exists(path):
        return []
    lines = _iter_tail_lines(path, limit if limit > 0 else DEFAULT_READ_LIMIT)
    return list(_iter_records(lines))


def replay_news_items(path: str, as_of: datetime) -> list[dict[str, Any]]:
    """Point-in-time view of the store for historical replay.

    Only records that were both published and fetched at or before ``as_of``
    are returned, so a replay never sees news the live bot could not have seen.
    """
    if not path or not os.path.exists(path):
        return []
    cutoff = as_of.astimezone(UTC) if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    replayable: list[tuple[datetime, dict[str, Any]]] = []
    # Streaming scan: replay windows are bounded by the backtest, not by the
    # tail window a live decision needs.
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for record in _iter_records(handle):
            published = parse_store_timestamp(record.get("published_at"))
            fetched = parse_store_timestamp(record.get("fetched_at"))
            if published is None or fetched is None:
                continue
            if published > cutoff or fetched > cutoff:
                continue
            replayable.append((published, record))
    replayable.sort(key=lambda entry: entry[0])
    return [record for _published, record in replayable]


def news_store_stats(path: str) -> dict[str, Any]:
    """Small summary used for logging and monitoring of the store itself."""
    stats: dict[str, Any] = {
        "path": path,
        "exists": bool(path) and os.path.exists(path),
        "records": 0,
        "newest_fetched_at": None,
    }
    if not stats["exists"]:
        return stats
    newest: datetime | None = None
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for record in _iter_records(handle):
            stats["records"] += 1
            fetched = parse_store_timestamp(record.get("fetched_at"))
            if fetched is not None and (newest is None or fetched > newest):
                newest = fetched
    stats["newest_fetched_at"] = _iso_utc(newest) if newest else None
    return stats


def _ensure_trailing_newline(path: str) -> bool:
    """Repairs a torn final line before appending. Returns True when repaired."""
    try:
        with open(path, "rb") as raw:
            raw.seek(0, os.SEEK_END)
            if raw.tell() == 0:
                return False
            raw.seek(-1, os.SEEK_END)
            if raw.read(1) == b"\n":
                return False
    except OSError:
        return False
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n")
    return True


def append_news_items(
    path: str,
    items: Sequence[Mapping[str, Any]],
    *,
    stored_at: datetime | None = None,
    provider: str = DEFAULT_NEWS_ITEM_PROVIDER,
    dedupe_window: int = DEFAULT_DEDUPE_WINDOW,
) -> dict[str, int]:
    """Appends newsworthy items as immutable records, skipping known identities.

    Never rewrites existing lines. Returns counters so the caller can report
    what happened without re-reading the file.
    """
    moment = stored_at or datetime.now(UTC)
    records = [
        record
        for record in (
            to_store_record(item, stored_at=moment, provider=provider)
            for item in items
        )
        if record is not None
    ]
    counts = {
        "appended": 0,
        "duplicates": 0,
        "invalid": len(items) - len(records),
    }
    if not records:
        return counts

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    with open(path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            _ensure_trailing_newline(path)
            known = {
                str(record.get("record_id"))
                for record in _iter_records(_iter_tail_lines(path, dedupe_window))
            }
            for record in records:
                record_id = str(record.get("record_id"))
                if record_id in known:
                    counts["duplicates"] += 1
                    continue
                known.add(record_id)
                handle.write(json.dumps(record, sort_keys=True, ensure_ascii=True))
                handle.write("\n")
                counts["appended"] += 1
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return counts
