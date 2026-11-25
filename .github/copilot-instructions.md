# Repository Architecture Rules

These rules apply to all code, tests, deployment files, documentation, and pull
requests in this repository. Preserve both release profiles as independent,
first-class deployments.

## Release profiles

- **Single-node Raspberry Pi:** The bot must run autonomously on one Pi with
  local state and file locking. It must not require a Hailo-8 device, a second
  node, K3s, PostgreSQL, a tunnel, or any cluster service. Rules-only operation
  must remain available when optional models or external APIs are unavailable.
- **CM5 cluster:** The Sipeed NanoCluster profile may distribute the bot,
  inference, monitoring, and data services across two CM5 modules. Hailo-8
  inference uses a compatible compiled `.hef`; a separate x86 worker is the
  intended place for model training, AutoResearch, and Hailo compilation.
  Cluster mode is optional and must never become a prerequisite for the Pi
  release.
- Do not silently make one profile the fallback deployment for the other.
  Document profile-specific prerequisites, service ownership, state paths, and
  failure behavior.

## Inference and decision ordering

- Route local inference through one backend abstraction with this fallback
  order: `hailo` -> CPU CatBoost/ONNX -> `rules_only`. Missing hardware,
  `hailo_platform`, drivers, remote service, model, or invalid prediction must
  be handled without crashing the single-Pi bot. Log the selected backend and
  every fallback. If no valid decision can be produced, default to `HOLD`.
- Do not import Hailo-only packages at module import time on the general bot
  path. Hardware discovery and driver access must be optional and isolated.
- Keep the decision pyramid ordered: inexpensive technical gates first
  (including ATR/volatility, spread, and minimum volume), local inference
  second, and paid LLM/news checks last. Do not incur an API call unless the
  prior stages have produced a candidate that could otherwise trade. A news or
  LLM result may veto a trade but may not bypass technical or risk gates.
- Missing, stale, malformed, or contradictory market/model/news data must not
  create a BUY. Fail closed to `HOLD` or skip the candidate with a reason.

## Data integrity and model lifecycle

- Use chronological splits and walk-forward evaluation for time-series
  training. Purge training labels whose forward horizon overlaps validation.
  Fit normalization, feature selection, and other learned transforms on the
  training fold only. Never shuffle time-series rows across the boundary.
- News records used in decisions or evaluation must preserve both source
  `published_at` and local `fetched_at` timestamps. Backtests may only use
  information fetched and published by the simulated decision time.
- Training/export workflows must produce a paired, versioned CPU artifact
  (`.cbm` or `.onnx`) and Hailo artifact (`.hef`) from the same model contract,
  with metadata, feature/label contract, provenance, checksums, and validation
  results. Keep the prior known-good pair available and support an atomic
  rollback; never replace only one side of a pair.
- Keep training and AutoResearch isolated from order execution. The future x86
  worker may build candidates, but deployment requires validation and explicit
  promotion of immutable artifacts.

## Independent risk and execution controls

- Hard risk guardrails and the kill switch are independent of rules, ML,
  Hailo, LLM, and news signals. They must be evaluated immediately before
  every order and always be able to veto it. Do not weaken them based on model
  confidence or a scorecard verdict.
- Enforce position sizing, exposure limits, cooldowns, loss limits, and
  volatility limits before execution. Unknown balances, incomplete state,
  failed reconciliation, or risk-service errors must block new orders.
- Keep one execution owner per account in cluster mode. Use an explicit
  leader/lease or equivalent distributed coordination before enabling more
  than one bot process; local file locks alone do not coordinate nodes.
- Reconcile persisted positions and open orders against Kraken balances/fills
  at startup before permitting new orders. Do not infer exchange truth from a
  local journal alone.
- Account for exchange-reported fees and bid/ask spread in live trade
  accounting. Bound retries, respect rate limits, and make retries idempotent
  so a timeout cannot submit duplicate orders.

## Validation and pull requests

- Keep both profiles covered by tests: Pi with no Hailo packages/device, CPU
  model unavailable, rules-only operation, and cluster Hailo available/unavailable
  paths. Tests must prove paid checks are skipped when earlier gates reject a
  candidate and prove risk guardrails override every signal source.
- Test missing/stale data, restart recovery, lock contention, partial fills,
  API timeouts, and rollback wherever the changed code affects those paths.
- Update `docs/CLUSTER_ARCHITECTURE.md` and `docs/ARCHITECTURE_AUDIT.md` when
  topology, service ownership, ports, model lifecycle, or architecture status
  changes. Keep observed deployment facts distinct from intended or planned
  behavior.
- Pull requests must state which release profile(s) they affect, the relevant
  tests, operational/configuration changes, and any migration or rollback
  requirements.<!-- rtk-instructions v2 -->
# RTK — Token-Optimized CLI

**rtk** is a CLI proxy that filters and compresses command outputs, saving 60-90% tokens.

## Rule

Always prefix shell commands with `rtk`:

```bash
# Instead of:              Use:
git status                 rtk git status
git log -10                rtk git log -10
cargo test                 rtk cargo test
docker ps                  rtk docker ps
kubectl get pods           rtk kubectl get pods
```

## Meta commands (use directly)

```bash
rtk gain              # Token savings dashboard
rtk gain --history    # Per-command savings history
rtk discover          # Find missed rtk opportunities
rtk proxy <cmd>       # Run raw (no filtering) but track usage
```
<!-- /rtk-instructions -->
