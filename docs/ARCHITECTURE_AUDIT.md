# Architecture Audit

Static repository review plus read-only inspection of both CM5 nodes on
2026-11-25. Status describes current evidence, not the desired architecture.
The live cluster workload inspected is paper/shadow only.

## Status by profile

| # | Core area | Single Raspberry Pi | CM5 cluster |
|---|---|---|---|
| 1 | Data basis and model lifecycle | **[⚠️ Teilweise]** Chronological CatBoost split, purging and Walk-Forward exist; no paired versioned HEF/CPU export or rollback. | **[⚠️ Teilweise]** Hailo ONNX export and live HEF inference exist; compiler/promotion is separate and CPU/Hailo artifacts are not paired automatically. |
| 2 | Decision pyramid and hardware fallback | **[⚠️ Teilweise]** Rules and optional CPU model work without Hailo; ATR gate exists, but no spread/min-volume first-stage gate and no backend fallback chain. | **[⚠️ Teilweise]** Remote Hailo is available and errors fail closed per candidate; no automatic CPU fallback or final LLM/news veto stage. |
| 3 | News integration | **[⚠️ Teilweise]** Optional RSS/FNG research collector exists; no per-item `fetched_at`, durable deduplicated item store, or structured veto schema. | **[⚠️ Teilweise]** Same collector/runtime limitations; Hailo feature contract does not include news. |
| 4 | Hard risk and kill switch | **[⚠️ Teilweise]** Position/risk gates, daily-loss threshold and loss-streak pause exist; no independent persistent emergency kill switch. | **[⚠️ Teilweise]** Same bot-level controls, but no cluster-wide switch or single-owner coordination is evidenced. |
| 5 | Execution and costs | **[⚠️ Teilweise]** Dry-run costs and market-data retries exist; live exchange fees and spread are not fully included in accounting/approval. | **[⚠️ Teilweise]** Current cluster is paper/shadow; the same live-execution accounting gaps remain if enabled. |
| 6 | Single-Pi vs cluster operations | **[⚠️ Teilweise]** Standalone service/state are supported, but no bot interprocess lock or full Kraken startup reconciliation. | **[⚠️ Teilweise]** Roles and tunnel are deployed, but no distributed bot lock/leader or duplicate-order prevention. |
| 7 | Evaluation and scorecard | **[✅ Implementiert]** GO/HOLD/NO-GO and HOLD for insufficient data are implemented, with Pi systemd automation. | **[⚠️ Teilweise]** Scorecard code and monitoring rules exist, but the inspected cluster deployment does not show scorecard automation as part of its active bot topology. |
| 8 | Monitoring and alerting | **[⚠️ Teilweise]** Read-only dashboard and Prometheus scorecard alerts are available but optional; no critical-error/kill-switch notification channel is configured here. | **[⚠️ Teilweise]** Read-only dashboard and shadow service are running on `.74`; alert coverage/receivers and Hailo-failure escalation remain incomplete. |

## Findings and evidence

### 1. Data basis and model lifecycle

`src.train_catboost_model.train_val_split_time`,
`_purge_training_boundary`, `generate_walk_forward_splits`, and
`run_walk_forward_evaluation` provide chronological validation and a forward
label purge. `train_model` saves a CatBoost model and metadata. The separate
`src.train_hailo_model.prepare_hailo_data` repeats a chronological purged split;
`train_and_export` emits ONNX plus metadata for a later DFC compilation step.
The two training/export paths are not one paired, immutable release with
checksums and rollback. `scripts/update_autoresearch_signal.py` and the tuning
cycle support research/parameter updates, but do not establish a separate x86
training worker or artifact promotion workflow.

**Next:** Add one release manifest tying `.cbm`/CPU ONNX and `.hef` to the same
data hash, feature/label contract, validation report, and model ID. Publish
immutable versions and a tested atomic promote/rollback command. Add an
end-to-end test from training outputs through runtime contract validation.

### 2. Decision pyramid and hardware agnosticism

`src.coin_analysis.analyze_coin` calculates rules, optional general ML, and an
optional tabular prediction. `src.entry_filters.entry_volatility_guard`
enforces ATR availability/minimum volatility. The entry path does not currently
apply a universal spread or minimum-volume gate before model work. The
Mammouth integration in `src.ai_copilot_service.py` is a bounded parameter
copilot, not the requested last-stage news veto.

`src.bot_bootstrap.load_tabular_predictor` selects either remote Hailo or local
CatBoost from configuration. It does not automatically move from Hailo to CPU;
Hailo prediction failures suppress that candidate rather than crashing the
whole bot. CPU model load errors are logged, and rules can remain active.
`tests/test_hailo_inference.py` covers inference/service contracts, while the
CI Hailo export job is separate from the slim CPU test job. There is no tested
backend-selection matrix proving Hailo available -> Hailo, Hailo unavailable ->
CPU, then CPU unavailable -> rules-only in one abstraction.

**Next:** Move backend discovery/selection behind an injected inference
interface; add a CI matrix with no Hailo package/device, a failing Hailo
endpoint, CPU fallback, and rules-only fallback. Put ATR, spread, and volume
checks before any model/API call and test that rejected candidates incur no
paid call.

### 3. News feed and veto schema

`scripts/fetch_research_signal.py` supports RSS/Atom and Fear & Greed. It emits
aggregate `timestamp_utc`, sentiment, confidence, risk, and citations. RSS
publication dates are parsed for lookback filtering, but undated items pass the
filter; per-item `published_at` and local `fetched_at` are not retained. Links
are deduplicated in the aggregate citations, but scored feed items are not
deduplicated. `src.api_models.ResearchSignalPayload` has no `event_risk` or
`veto_buy` fields. The feed uses keyword sentiment, not a structured LLM news
decision. News is not a final veto in `src.coin_analysis.analyze_coin`.

**Next:** Persist each feed item with source ID/link, `published_at`,
`fetched_at`, and content hash; reject unknown/stale timestamps for historical
evaluation. Add a validated schema containing `sentiment_score`, `event_risk`,
`veto_buy`, and `reason`, and call it only after local trade gates pass.

### 4. Hard risk and kill switch

`src.trading_bot.CryptoTradingBot._can_open_new_positions`,
`_refresh_daily_anchor`, and close-performance bookkeeping enforce configured
entry limits, daily-loss checks, cooldowns, and loss-streak pauses. Important
risk state such as the daily anchor and loss streak is process-local. No
independent persistent emergency kill switch that overrides all signal paths
was found.

**Next:** Persist risk state and an operator-controlled kill-switch latch;
evaluate it immediately before every order, fail closed on unreadable state,
and test restart persistence and precedence over rules, CPU, Hailo, and LLM.

### 5. Execution and fee logic

`src.dry_run_execution` models configurable fees and slippage. `src.market_data`
retries transient market-data errors, and `src.execution_service` catches
Kraken/CCXT exchange errors. Live order fill confirmation records quantity,
average price, and cost, but `_execute_real_buy`/`_execute_real_sell` do not
carry exchange-reported fee data into the journal; live close performance is
computed without net proceeds/fees. No mandatory spread ceiling or separate
maker/taker schedule is enforced before a live order.

**Next:** Account using exchange fill/fee records, preserve partial fills, and
include spread and fee estimates in pre-trade approval. Add timeout/idempotency
tests that prove a retry cannot create a duplicate order.

### 6. Runtime modes, locking, and reconciliation

The Pi deployment packages the bot as a standalone service. Portfolio state
uses atomic file replacement in `src.portfolio.Portfolio.save_state`, but
`docs/USAGE.md` explicitly states there is no interprocess lock. Live balance
refresh is not a complete reconciliation of local positions, open orders, and
Kraken fills before new entries. The market recorder has its own file lock;
that does not protect bot state.

The inspected cluster separates `.74` bot/paper workloads from `.75` Hailo
inference through a restricted localhost SSH tunnel. The current shadow run is
isolated and simulated, but there is no distributed lease or leader guard
preventing two bot instances from submitting orders. `cluster-starter/` is
generic infrastructure and its sample inventory does not map the production
addresses.

**Next:** Require one execution owner per Kraken account. Add a local lock for
the Pi and a distributed lease/fencing token for multi-node execution. On
startup, reconcile open orders and balances with Kraken before enabling buys;
quarantine ambiguous state for operator review.

### 7. Evaluation and scorecard

`src.go_no_go_scorecard.evaluate_verdict` implements GO/HOLD/NO-GO; hard
failures produce NO-GO and soft/insufficient evidence produces HOLD. The
scorecard service/timer and status exporter are included in `deploy/`, with
Prometheus alerts in `deploy/trading-alerts.yml`. Pi release documentation
includes the scorecard timer. The active `.74`/`.75` service inspection did
not show a cluster scorecard status unit; the Hailo shadow runbook does not
make scorecard status a promotion gate.

**Next:** Make a fresh GO an explicit prerequisite for model promotion and
live-mode enablement; wire scorecard status into the cluster operational
dashboard and test stale/missing metrics as HOLD.

### 8. Monitoring and alerting

`src.public_paper_dashboard` exposes a read-only dashboard and has access
control/read-only tests. The dashboard container was active on `.74` during
inspection. Prometheus rules alert on scorecard NO-GO/HOLD/missing and stale
portfolio snapshots. The checked-in alert rules define conditions, but no
receiver/webhook/Telegram/ntfy configuration was found; they do not cover the
kill switch, Hailo failure rate, daily report failure, or market-data
staleness.

**Next:** Configure a secured notification receiver and alerts for kill-switch
activation, bot/service health, stale data, Hailo errors, report failures, and
disk/database health. Keep the dashboard read-only and deployable without
cluster services on a single Pi.

## Prioritized implementation order

1. **P0: Prevent duplicate or unsafe orders.** Add a persistent independent
   kill switch, startup exchange reconciliation, and one-process/local plus
   cluster-wide execution ownership.
2. **P0: Fix live accounting and gates.** Include exchange fees and spread in
   live approval/journaling; make retries idempotent and preserve partial-fill
   state.
3. **P1: Finish inference fallback.** Implement and test Hailo -> CPU -> rules
   selection without importing Hailo dependencies on the Pi path; keep missing
   inputs at HOLD.
4. **P1: Make news backtest-safe.** Persist per-item published/fetched times,
   deduplicate items, validate structured veto output, and enforce final-stage
   invocation.
5. **P1: Establish paired model releases.** Build a single versioned artifact
   manifest and atomic deployment/rollback for CPU and Hailo artifacts; assign
   training/compilation to the future x86 worker only after it exists.
6. **P2: Close operational visibility gaps.** Add alert receivers, cluster
   scorecard promotion gating, and staleness/kill-switch/Hailo alerts; retain a
   tested standalone Pi dashboard/scorecard path.