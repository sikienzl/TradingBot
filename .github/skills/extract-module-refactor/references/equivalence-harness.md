# Behavioural equivalence harness

The claim to prove: for every interesting code path, the old and the new
implementation emit the **same log records in the same order**, call the
**same hooks in the same order**, and leave the **same state** behind.

## Shape of the harness

Write a throwaway script (keep it under `/tmp`, it is not committed) that:

1. Installs a `logging.Handler` that appends
   `(level, msg % args)` tuples to a list, attached to the logger the code
   under test uses.
2. Drives a list of named scenarios. For an execution-layer extraction the
   set that has been enough:
   - every terminal order status (`closed`, `filled`, `canceled`,
     `cancelled`, `expired`, `rejected`) and a non-terminal one that needs a
     refresh, plus an order whose refresh raises `ccxt.OrderNotFound` /
     `ccxt.InvalidOrder`, and one where `fetch_order` fails with
     `ccxt.NetworkError`
   - partial fill vs. full fill on a sell
   - a buy whose fill cannot be confirmed
   - below-exchange-minimum skip, `exchange is None`
   - each ccxt error class: `InsufficientFunds`, `InvalidOrder`,
     `NetworkError`, `ExchangeError`, `BaseError`
   - every blocked reason in the entry loop: `below_min_trade_amount`,
     `missing_price_data`, `insufficient_available_funds`, momentum filter,
     re-entry cooldown, `execute_trade_failed`
   - the risk-guardrail block, the max-open-trades break, the
     insufficient-min-cash branch
3. Records, per scenario: the log tuples, the hook-call tuples, and a
   JSON-able dump of portfolio/performance state.
4. Dumps everything with `json.dump(..., sort_keys=True, indent=2,
   default=str)` to a file.

## Determinism checklist

These are the things that produced false diffs in past runs:

- Pin `TRADING_RUN_ID` (the journal row and log lines embed a random run id).
- Pin the clock, or make the scenario avoid time (pass explicit
  `reentry_cooldown_seconds`, stub `datetime.now` through the module seam).
- Use `defaultdict(lambda: {"buys": 0, "sells": 0, "pnl": 0.0})` for
  `performance["by_source"]`, otherwise the first access differs between the
  bot path and the service path.
- Make the stub exchange's minimum large enough that the
  below-minimum branch actually trips (`min_amount=1.0`, not `0.001`).
- Give the entry scenario enough coins that *both* `missing_price_data` and
  `insufficient_available_funds` fire in one run.
- Stub `Portfolio.save_state` so no file I/O leaks into the comparison.

## Running both sides

```bash
# branch
PYTHONPATH="$PWD:$PWD/src" .venv/bin/python /tmp/equiv_harness.py /tmp/equiv_branch.json

# main, in a throwaway worktree
git worktree add --detach /tmp/main-wt main
cd /tmp/main-wt
PYTHONPATH="$PWD:$PWD/src" /home/siegfried/git/github/sikienzl/TradingBot/.venv/bin/python \
    /tmp/equiv_harness.py /tmp/equiv_main.json
cd /home/siegfried/git/github/sikienzl/TradingBot
git worktree remove --force /tmp/main-wt

diff -u /tmp/equiv_main.json /tmp/equiv_branch.json; echo "exit=$?"
```

`exit=0` is the proof. If it differs, fix the code — never relax the
harness, unless the diff is a documented, intentional change (e.g. dropping a
dead variable assignment); then say so explicitly in the PR body.
