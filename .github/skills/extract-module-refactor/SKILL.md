---
name: extract-module-refactor
description: "Extract a cohesive collaborator module out of src/trading_bot.py (or another oversized module) without changing behaviour. Use when asked to shrink the God class, move methods into a service, reduce cyclomatic complexity, or add a layer-1 module. Covers auditing complexity, splicing code verbatim, proving behavioural equivalence, the repo test gates and the CHANGELOG/architecture ratchet."
argument-hint: 'method or region to extract, e.g. "_manage_open_trades"'
---

# Behaviour-preserving module extraction

Every extraction in this repo follows the same recipe. One extraction = one
PR, and the PR must prove that nothing observable changed.

## Hard rules

- **No rollout.** Never bump a version, never add a `release_notes/vX.txt`,
  never tag, never touch the deployment host (`192.168.62.74`) or run
  `ansible`. Releases are a separate, explicit decision by the maintainer.
- Copy logic **verbatim**: same log messages, same exception order, same
  mutation order, same return values. Refactoring is not the place for
  cosmetic cleanups; note improvements in the PR body instead.
- Keep the bot's public/private method names as **thin delegators**. Tests
  patch bot methods (`bot._fetch_ohlcv_data`, `bot._execute_trade`, ...) and
  import names from the `src.trading_bot` namespace, so re-export anything a
  test touches (`# noqa: F401`).
- Bot-owned state stays with the bot. The extracted module receives an
  injected client (e.g. the ccxt exchange) plus explicit arguments or a
  frozen `@dataclass` of callbacks (`RealTradeHooks`, `EntryHooks`).

## Procedure

1. **Audit.** Pick the target with
   [complexity_report.py](./scripts/complexity_report.py) — it prints
   `CC <n> <lines>L L<line> <name>` per function. Prefer a cluster of
   related high-CC functions over one isolated function.
2. **Snapshot.** `cp src/trading_bot.py /tmp/trading_bot.py.bak` before any
   surgery. Splice with a deterministic Python script (assert every anchor
   literal), never with fuzzy insert tools — they have injected unrelated
   edits into this file before.
3. **New module.** Layer 1, no shebang, `from __future__ import annotations`,
   module-level `logger = logging.getLogger(__name__)`, no import of
   `trading_bot` (layer 2 must stay a leaf).
4. **Register.** Add the module to `LAYERS[1]` in
   `tests/test_architecture.py` and **lower** the `LINE_CEILINGS` entry for
   the file you shrank. The ceiling is a ratchet: it may only go down.
5. **Unit tests.** New `tests/test_<module>.py` against stub clients and
   recording hooks. Assert exact call order and exact state, not just
   "no exception".
6. **Prove equivalence.** Build the harness described in
   [equivalence-harness.md](./references/equivalence-harness.md), run it on
   the branch, run it in a `git worktree` of `main`, then
   `diff -u branch.json main.json` must exit 0.
7. **Gates** (all must match the recorded baseline, see below).
8. **CHANGELOG.** Add an `### Added` bullet for the new module and a
   `### Changed` bullet with the line delta and the equivalence statement,
   under `## Unreleased`.

## Gates

```bash
.venv/bin/python -m pytest tests/ -q --no-header -p no:cacheprovider
PYTHONPATH="$PWD:$PWD/src" .venv/bin/python .github/skills/extract-module-refactor/scripts/ci_env_sim.py tests/ -q --no-header -p no:cacheprovider
.venv/bin/python -m unittest discover -s .github/scripts/tests
.venv/bin/python -m unittest discover -s cluster-starter/tests
.venv/bin/ruff check .
.venv/bin/python scripts/check_deployment_config.py
.venv/bin/mypy src/
```

`ci_env_sim.py` hides `talib`/`torch`/`transformers`/`onnx` so the local run
matches the slim CI test job. Baseline on `main` (2026-11-24, `898caa0`):
pytest **747 passed**, CI-sim **721 passed / 2 skipped**, unittest **15** and
**5**, ruff clean, deploy check exit 0, mypy **11 errors in 5 files / 43
files** (informational job — the count must not grow).

## Git flow

English imperative commit, no `Co-Authored-By`. Before committing, scan the
staged diff for stray German text and for unrelated added lines:

```bash
git add -A
git --no-pager diff --cached | grep -inE 'ä|ö|ü|ß|\b(und|nicht|für|der|die|das)\b'
git --no-pager diff --cached | grep -n '^+' | less   # audit every added line
```

Then `git push -u origin <branch>`, `gh pr create`, poll with
`GH_PAGER=cat gh pr checks <n>` (never `--watch` — it wedges the terminal),
`gh pr merge <n> --merge --delete-branch`, and re-run pytest on `main` to
record the new baseline.

## Done

Update `tests/test_architecture.py` ceilings, the CHANGELOG, and the
refactoring notes (module list, line counts, suite counts, next candidates).
