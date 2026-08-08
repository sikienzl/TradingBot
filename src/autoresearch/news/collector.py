"""News collector: normalize simple JSON/CSV/RSS inputs into canonical records."""
# ruff: noqa: PERF402
import csv
import json
from pathlib import Path

try:
    from dateutil import parser as dateparser
except ImportError:
    dateparser = None
try:
    import requests
except ImportError:  # pragma: no cover - optional
    requests = None
try:
    import feedparser
except ImportError:  # pragma: no cover - optional
    feedparser = None
import sqlite3


def load_json_lines(path: str) -> list[dict]:
    p = Path(path)
    raw = json.loads(p.read_text()) if p.exists() else []
    # expect list of objects
    return raw


def load_csv(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with p.open() as fh:
        rdr = csv.DictReader(fh)
        for r in rdr:
            out.append(r)
    return out


def collect(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    # support HTTP(S) URLs
    if str(path).lower().startswith(("http://", "https://")):
        return collect_from_url(path)
    # support sqlite URIs: sqlite:///absolute/path.db or sqlite://relative.db
    if str(path).lower().startswith("sqlite://"):
        return collect_from_sqlite(path)
    if p.suffix.lower() in (".json", ".jsonl"):
        recs_src = load_json_lines(str(p))
        if isinstance(recs_src, list):
            recs = recs_src.copy()
        else:
            recs = list(recs_src)
        # normalize timestamps
        import logging
        logger = logging.getLogger(__name__)

        for r in recs:
            if "ts" in r:
                if dateparser:
                    try:
                        r["timestamp_utc"] = dateparser.parse(r["ts"]).astimezone().isoformat()
                    except (ValueError, TypeError) as exc:
                        logger.debug("timestamp parse failed for %s: %s", r.get("ts"), exc)
                        r["timestamp_utc"] = r.get("ts")
                else:
                    r["timestamp_utc"] = r.get("ts")
        return recs
    if p.suffix.lower() in (".csv",):
        return load_csv(str(p))
    # fallback: attempt JSON
    try:
        return load_json_lines(str(p))
    except json.JSONDecodeError:
        return []


def collect_from_url(url: str) -> list[dict]:
    """Fetch a URL and attempt to parse JSON; fall back to RSS if appropriate."""
    if not requests:
        return []
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except (requests.RequestException, ValueError):
        return []
    ct = resp.headers.get("content-type", "").lower()
    text = resp.text
    if "application/json" in ct or text.strip().startswith("[") or text.strip().startswith("{"):
        try:
            raw = json.loads(text)
            return list(raw) if isinstance(raw, list) else [raw]
        except (json.JSONDecodeError, TypeError):
            return []
    # fallback to RSS/Atom
    if feedparser:
        return collect_from_rss(text)
    return []


def collect_from_rss(feed_text: str) -> list[dict]:
    """Parse RSS/Atom feed text into list of records with 'ts' and 'title'/'summary'."""
    if not feedparser:
        return []
    parsed = feedparser.parse(feed_text)
    out = []
    for e in parsed.entries:
        rec = {
            "ts": getattr(e, "published", getattr(e, "updated", None)),
            "title": getattr(e, "title", None),
            "summary": getattr(e, "summary", None),
            "link": getattr(e, "link", None),
        }
        out.append(rec)
    return out


def collect_from_sqlite(uri: str) -> list[dict]:
    """Read a simple table named 'news' from a sqlite DB URI.

    URI format: sqlite:///absolute/path.db or sqlite://relative.db
    The table is expected to have columns `ts`, `title`, `summary`, `meta`.
    """
    # strip prefix
    path = uri.split("sqlite://", 1)[-1]
    if path.startswith("/"):
        dbpath = path
    else:
        dbpath = Path(path).as_posix()
    try:
        conn = sqlite3.connect(dbpath)
        cur = conn.cursor()
        cur.execute("SELECT ts, title, summary, meta FROM news")
        rows = cur.fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    out = []
    for ts, title, summary, meta in rows:
        rec = {"ts": ts, "title": title, "summary": summary}
        if meta:
            try:
                rec["meta"] = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                rec["meta"] = meta
        out.append(rec)
    return out
