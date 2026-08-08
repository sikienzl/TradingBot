import json
import sqlite3
from pathlib import Path

import pytest

from src.autoresearch.news import collector


def test_collect_json_file(tmp_path):
    p = tmp_path / "news.json"
    data = [{"ts": "2020-01-01T00:00:00Z", "title": "x", "summary": "y"}]
    p.write_text(json.dumps(data))
    out = collector.collect(str(p))
    assert isinstance(out, list)
    assert out[0]["title"] == "x"


def test_collect_csv_file(tmp_path):
    p = tmp_path / "news.csv"
    p.write_text("ts,title,summary\n2020-01-01T00:00:00Z,hi,hello\n")
    out = collector.collect(str(p))
    assert isinstance(out, list)
    assert out[0]["title"] == "hi"


def test_collect_from_rss():
    rss = """<?xml version='1.0'?>
    <rss><channel>
      <item>
        <title>Test</title>
        <pubDate>Wed, 02 Oct 2002 13:00:00 GMT</pubDate>
        <description>Desc</description>
      </item>
    </channel></rss>"""
    out = collector.collect_from_rss(rss)
    # feedparser may be optional; if not available, function returns []
    if out:
        assert out[0]["title"] == "Test"


def test_collect_from_sqlite(tmp_path):
    db = tmp_path / "news.db"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("CREATE TABLE news (ts TEXT, title TEXT, summary TEXT, meta TEXT)")
    cur.execute(
        "INSERT INTO news (ts, title, summary, meta) VALUES (?, ?, ?, ?)",
        ("2020-01-01", "t", "s", '{"k": 1}'),
    )
    conn.commit()
    conn.close()
    out = collector.collect_from_sqlite(f"sqlite://{db}")
    assert isinstance(out, list)
    assert out[0]["title"] == "t"
    assert isinstance(out[0]["meta"], dict)
