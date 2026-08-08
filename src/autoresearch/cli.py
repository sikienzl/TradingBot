"""Small CLI to run autoresearch experiments."""
import argparse
import os

import numpy as np

from .runner import AutoResearchRunner
from .search import grid_search
from .storage import FileStorage
from .writer import make_canonical, write_canonical, write_prom


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", default="example")
    p.add_argument("--out", default="results/autoresearch_results.json")
    p.add_argument("--news", default=None, help="path to news JSON/CSV for demo ingestion")
    p.add_argument("--fallback-neutral", action="store_true", help="write neutral fallback instead of failing")
    args = p.parse_args(argv)

    storage = FileStorage(args.out)
    runner = AutoResearchRunner(storage)
    # demo: run a small grid-search when strategy == example
    if args.strategy == "example-search":
        # build synthetic price DataFrame with minute index
        import pandas as pd
        idx = pd.date_range("2026-08-08T09:00:00Z", periods=50, freq="T", tz="UTC")
        prices = pd.DataFrame({"close": np.linspace(1.0, 1.2, 50)}, index=idx)
        gs = grid_search({"short": [3, 5], "long": [10, 20]}, prices["close"].values)
        storage.save_result(gs["best"])
        best = gs["best"]
        print("Best:", best)
        # optionally write canonical output for bridge
        out_path = os.environ.get("AUTORESEARCH_OUTPUT_PATH", args.out)
        # include news if provided
        if args.news:
            best["signals"] = {**best.get("signals", {}), "news_path": args.news, "bucket_minutes": 5}
        canonical = make_canonical(best.get("signals", {}))
        from .bridge import is_fresh, neutral_payload
        # if fallback-neutral flag set, write neutral and exit
        prom_path = os.environ.get("AUTORESEARCH_PROM_PATH", "results/scorecards/textfile/autoresearch_signal.prom")
        if args.fallback_neutral:
            _fb = neutral_payload()
            write_canonical(out_path, _fb)
            write_prom(prom_path, _fb)
            print("Wrote neutral fallback to", out_path)
            return
        # otherwise validate freshness according to env
        if not is_fresh(canonical, int(os.environ.get("AUTORESEARCH_MAX_AGE_MINUTES", "180"))):
            if os.environ.get("AUTORESEARCH_WRITE_NEUTRAL_FALLBACK", "true").lower() == "true":
                _fb = neutral_payload()
                write_canonical(out_path, _fb)
                write_prom(prom_path, _fb)
                print("Canonical payload not fresh; wrote neutral fallback to", out_path)
                return
            else:
                raise SystemExit("Canonical payload too old and fallback disabled")
        write_canonical(out_path, canonical)
        write_prom(prom_path, canonical)
        print("Wrote canonical signal to", out_path)
        return

    # market_data placeholder: None for now
    res = runner.run_experiment(args.strategy, {}, None)
    print("Saved:", res)
    out_path = os.environ.get("AUTORESEARCH_OUTPUT_PATH", args.out)
    canonical = make_canonical(res.get("signals", {}))
    from .bridge import is_fresh, neutral_payload
    prom_path = os.environ.get("AUTORESEARCH_PROM_PATH", "results/scorecards/textfile/autoresearch_signal.prom")
    # if fallback-neutral flag set, write neutral and exit
    if args.fallback_neutral:
        _fb = neutral_payload()
        write_canonical(out_path, _fb)
        write_prom(prom_path, _fb)
        print("Wrote neutral fallback to", out_path)
        return
    max_age = int(os.environ.get("AUTORESEARCH_MAX_AGE_MINUTES", "180"))
    if not is_fresh(canonical, max_age):
        if os.environ.get("AUTORESEARCH_WRITE_NEUTRAL_FALLBACK", "true").lower() == "true":
            _fb = neutral_payload()
            write_canonical(out_path, _fb)
            write_prom(prom_path, _fb)
            print("Canonical payload not fresh; wrote neutral fallback to", out_path)
            return
        else:
            raise SystemExit("Canonical payload too old and fallback disabled")
    write_canonical(out_path, canonical)
    write_prom(prom_path, canonical)
    print("Wrote canonical signal to", out_path)


if __name__ == "__main__":
    main()
