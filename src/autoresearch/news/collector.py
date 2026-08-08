"""News collector: normalize simple JSON/CSV/RSS inputs into canonical records."""
# ruff: noqa: PERF402
import csv
import json
from pathlib import Path

try:
    from dateutil import parser as dateparser
except ImportError:
    dateparser = None


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
