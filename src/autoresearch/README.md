Autoresearch scaffold

Quick start:

Run example job:

```bash
python3 -m src.autoresearch.cli
```
Run example grid-search demo:

```bash
python3 -m src.autoresearch.cli --strategy example-search
```

This package provides scaffolding for automated signal generation, grid search, backtesting, and a news pipeline.

Quick usage: supplying news and price sources
 - File (JSON/CSV): pass the path to `--news` when running the CLI.
 - HTTP(S) JSON: `--news https://example.com/feed.json` (will attempt JSON then RSS).
 - RSS/Atom: `--news https://example.com/feed.atom` (requires `feedparser` in the environment).
 - SQLite: `--news sqlite:///path/to/news.db` (expects a table named `news` with `ts,title,summary,meta`).

Example CLI (with rtk):
```bash
rtk python3 -m src.autoresearch.cli --strategy example-search --news data/sample_news.json --prices path/to/ohlcv.csv
```

Notes:
- Optional dependencies: `feedparser`, `requests`. The collector is defensive if they are missing.
- New importers are implemented in `src/autoresearch/news/collector.py`.
