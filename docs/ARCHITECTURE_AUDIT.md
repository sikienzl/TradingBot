# Architecture Audit

Static repository review plus read-only inspection of both CM5 nodes on
2026-11-25. Status describes current evidence, not the desired architecture.
The live cluster workload inspected is paper/shadow only. Rows marked
**[✅ Implemented]** describe repository code that is covered by tests; where the
inspected cluster does not itself exercise that code path, the cluster column
stays **[⚠️ Partial]**.

## Status by profile

| # | Core area | Single Raspberry Pi | CM5 cluster |
|---|---|---|---|
| 1 | Data basis and model lifecycle | **[⚠️ Partial]** Paired contract/checksum manifest and atomic activation/rollback are implemented; candidate training and DFC compilation remain separate operator steps. | **[⚠️ Partial]** Same paired release tooling; DFC and target-device validation remain external, and no x86 worker is deployed. |
| 2 | Decision pyramid and hardware fallback | **[✅ Implemented]** Volume, spread, and ATR gates run before any model or paid call, and the paid news veto runs after the pre-trade cost budget; the order is asserted end to end. Backend selection sits behind `src.inference_backend` with injected backends, and a dedicated CI job runs the Hailo→CPU→rules-only matrix with no Hailo package, device or model files installed. | **[⚠️ Partial]** Same cascade and gates; target-cluster service/device availability is not continuously exercised by CI, and a cluster fallback is only visible in the bot log. |
| 3 | News integration | **[⚠️ Partial]** RSS items now retain `published_at`/`fetched_at`, are deduplicated, and can drive an opt-in structured final veto with a daily call limit; persistent historical item storage is not implemented. | **[⚠️ Partial]** Same code is available, but the inspected cluster has no configured paid provider/credentials; Hailo features do not consume news. |
| 4 | Hard risk and kill switch | **[✅ Implemented]** The daily anchor, loss-streak pause, hourly BUY budget and re-entry cooldowns are persisted in `TRADING_RISK_STATE_FILE` and restored at startup; unreadable or unwritable risk state blocks new entries and latches the persistent kill switch in live mode. | **[⚠️ Partial]** Same persisted state per node, but the file and the stop marker are host-local; no cluster-wide shared switch or distributed state store is implemented. |
| 5 | Execution and costs | **[✅ Implemented]** Exchange fill/fee records, a pre-trade taker-fee + spread + slippage cost budget (`MAX_ENTRY_COST_BPS`), and a Kraken `cl_ord_id` per submission are in place; a submission timeout is reconciled instead of resubmitted and an unknown outcome latches the stop marker. Partial fills keep their requested amount in the journal. | **[⚠️ Partial]** Same controls, but live exchange use remains unvalidated on the cluster. |
| 6 | Single-Pi vs cluster operations | **[⚠️ Partial]** Local process lock, persisted live snapshot, startup balance comparison, open-order refusal, and uncertain-order latch exist; fill reconstruction needs operator review. | **[⚠️ Partial]** Optional account-scoped PostgreSQL advisory lease supplements local locks; shared PostgreSQL config and cluster deployment are operator prerequisites, and fill reconstruction remains manual. |
| 7 | Evaluation and scorecard | **[✅ Implemented]** GO/HOLD/NO-GO and HOLD for insufficient data are implemented, with Pi systemd automation. A fresh `GO` verdict is now a prerequisite for `DRY_RUN=false` and for `scripts/manage_model_release.py promote`. | **[⚠️ Partial]** Same gate code and monitoring rules; the inspected cluster deployment still does not show scorecard automation as part of its active bot topology, so the gate reads the marker produced by the Pi/cluster `scorecard-status` timer. |
| 8 | Monitoring and alerting | **[⚠️ Partial]** Pi installer configures Alertmanager, a private HTTPS webhook relay, and kill-switch/Hailo/research health alerts plus bounded journal/bot-log/market-data staleness probes, scheduled-task exit-status markers, disk-capacity gauges, and an optional analytics-database probe; operator webhook configuration and staging delivery validation remain required. | **[⚠️ Partial]** Alert rules and relay are available in the repository, but cluster-specific installation/configuration and live receiver validation remain outstanding. |

## Findings and evidence

### 1. Data basis and model lifecycle

`src.train_catboost_model.train_val_split_time`,
`_purge_training_boundary`, `generate_walk_forward_splits`, and
`run_walk_forward_evaluation` provide chronological validation and a forward
label purge. `train_model` saves a CatBoost model and metadata, including a
training-data hash and optional fixed Hailo feature contract. The separate
`src.train_hailo_model.prepare_hailo_data` repeats a chronological purged split;
`train_and_export` emits ONNX plus metadata for a later DFC compilation step.
`src.model_release.promote_release` verifies paired CPU/Hailo feature and label
contracts plus the training-data hash, copies immutable artifacts, records
checksums, and atomically activates the release. `rollback_release` verifies
checksums before switching back. `scripts/update_autoresearch_signal.py` and
the tuning cycle support research/parameter updates, but do not establish a
separate x86 training worker or automatic DFC/promotion workflow.

**Next:** Automate candidate training, DFC compilation, target-Hailo validation,
and explicit promotion on the planned x86 worker. Include full validation
reports in the release manifest and exercise the release CLI in deployment CI.

### 2. Decision pyramid and hardware agnosticism

`src.coin_analysis.analyze_coin` calculates rules, optional general ML, and an
optional tabular prediction. `src.market_analysis.select_analysis_coins`
applies minimum volume and configured spread gates before OHLCV/model work;
`analyze_coin` suppresses optional inference when ATR is unavailable or below
the configured threshold. The Mammouth integration
in `src.ai_copilot_service.py` remains a bounded parameter copilot, separate
from the opt-in structured news veto.

`src.inference_backend.build_tabular_predictor` is the single selection point
for the optional tabular model. It receives the logger and config and, for the
Hailo backend, builds `src.inference_fallback.TabularInferenceFallback`, which
tries the remote Hailo predictor, falls back to a lazily constructed CPU
CatBoost predictor, and finally emits a HOLD result with `backend=rules_only`
that preserves the existing rules signal. Invalid predictions are rejected at
every stage. The Hailo and CatBoost modules are imported only inside the default
factories, so importing the selection module pulls in no backend package, and
both backends are injectable, which is what makes the cascade testable without a
Hailo package, device or reachable endpoint.

`tests/test_inference_backend.py` covers the matrix: Hailo available -> Hailo, a
failing Hailo endpoint or invalid Hailo prediction -> CPU, no Hailo
package/device -> CPU, and CPU unavailable -> rules-only, including the log line
for each transition and that a construction error outside the documented
fallback set still propagates. `tests/test_decision_pyramid_ordering.py` walks
the real collaborators and asserts the recorded stage order
volume -> spread -> ATR -> inference -> cost budget -> paid news veto -> order,
so a candidate rejected by a technical gate performs no model call and no paid
call. The CI `Inference fallback matrix` job runs both files with only
pytest/numpy/pandas/ccxt installed and asserts that `hailo_platform`, `catboost`
and `sklearn` are not importable, i.e. it is the single-Pi no-device profile.
`tests/test_hailo_inference.py` still covers the inference/service contracts and
the CI Hailo export job is separate.

**Next:** Exercise the target-cluster Hailo service and device availability
continuously (readiness probe plus alert on the cluster profile) and record the
selected backend per decision, so a silent cluster fallback to CPU or
rules-only is visible in monitoring rather than only in the bot log.

### 3. News feed and veto schema

`scripts/fetch_research_signal.py` supports RSS/Atom and Fear & Greed. RSS
records now require a valid, non-future publication timestamp, retain per-item
`published_at` and `fetched_at`, and deduplicate by link or content/time hash.
`src.api_models.NewsVetoPayload` validates `sentiment_score`, `event_risk`,
`veto_buy`, and `reason`. `src.news_veto.evaluate_news_veto` can run only after
momentum and cooldown checks, is disabled by default, uses a persisted daily
call budget, and fails closed on stale/missing data or malformed/provider
errors. Item storage is still only in the latest aggregate signal file.

**Next:** Persist immutable per-item news records for historical replay and
validate the selected provider's structured-output support and pricing in
staging before enabling the paid veto.

### 4. Hard risk and kill switch

`src.risk_guardrails.can_open_new_positions` enforces the daily-loss budget,
loss-streak pause, and hourly BUY budget, and
`resolve_reentry_cooldown_seconds`/`is_coin_in_reentry_cooldown` scale the
per-coin re-entry cooldown with the ATR regime. The state behind those gates is
now durable: `src.risk_state.read_risk_state`/`write_risk_state` store the daily
anchor, loss streak, BUY pause, hourly BUY timestamps, and last-sell timestamps
in `TRADING_RISK_STATE_FILE` through an atomic replace, and
`CryptoTradingBot._restore_risk_state` reloads them at startup while every close,
BUY, and anchor refresh persists them again. A missing file stays neutral; an
unreadable or invalid file sets `risk_state_unreadable`, which blocks every new
entry with an explicit reason. If the file cannot be written, dry-run mode keeps
entries blocked, and live mode additionally latches the persistent kill switch
(`src.kill_switch.activate_kill_switch`, `TRADING_KILL_SWITCH_FILE`) and stops.
`src.execution_service.execute_real_trade` checks that marker before all live
orders, so the guardrails and the switch always veto any signal source.

**Next:** Keep the switch and the risk-state file host-local on the Pi, and give
the CM5 profile a shared store plus an operator-controlled latch before running
more than one bot process against one account.

### 5. Execution and fee logic

`src.dry_run_execution` models configurable fees and slippage. `src.market_data`
retries transient market-data errors, and `src.execution_service` catches
Kraken/CCXT exchange errors. Live fill confirmation preserves the final order
record and includes quote/traded-asset fees in entry basis and net sell proceeds
when they can be converted. Unknown fee assets are preserved and leave journal
net-PnL fields blank. The configured spread ceiling is checked before detailed
analysis and again before entry. `src.execution_costs` then estimates the taker
fee (from the parsed market, or `LIVE_TAKER_FEE_RATE`), the bid/ask spread, and
`LIVE_SLIPPAGE_RATE`, and `execution_cost_block_reason` rejects an entry whose
total exceeds `MAX_ENTRY_COST_BPS`; the estimate is journaled in the `est_*`
columns. A quote that cannot be matched to the reference price contributes no
spread instead of an invented one. `src.order_submission.submit_market_order`
sends a deterministic `cl_ord_id` and, after a `ccxt.NetworkError`, looks the
order up in the open/closed lists instead of resubmitting; an outcome that stays
unknown latches the kill switch rather than risking a duplicate. Partial fills
keep `requested_amount_coin` and `partial_fill` in the journal.

**Next:** Reconcile journal rows against Kraken fill history automatically, and
validate the cost budget and idempotency path against the live exchange before
enabling live trading on either profile.

### 6. Runtime modes, locking, and reconciliation

The Pi deployment packages the bot as a standalone service. Live mode holds a
local `fcntl` process lock, persists a strict portfolio snapshot, compares it
with Kraken free balances at startup, and refuses startup on open orders or
untracked holdings. Every order checks for an existing open order. This does
not reconstruct positions from Kraken fills; operators must reconcile
mismatches and establish a trusted snapshot. The market recorder has a
separate file lock.

The inspected cluster separates `.74` bot/paper workloads from `.75` Hailo
inference through a restricted localhost SSH tunnel. The current shadow run is
isolated and simulated. An optional PostgreSQL session advisory lease is now
available for candidate execution nodes and must be explicitly pointed at a
shared database before cluster use. The single-Pi profile does not require
PostgreSQL. `cluster-starter/` is
generic infrastructure and its sample inventory does not map the production
addresses.

**Next:** Configure and exercise the shared PostgreSQL lease on the actual
cluster before enabling multiple execution candidates. Startup balance/open-
order checks are implemented, but exact fill-to-position reconstruction
remains an operator reconciliation step.

### 7. Evaluation and scorecard

`src.go_no_go_scorecard.evaluate_verdict` implements GO/HOLD/NO-GO; hard
failures produce NO-GO and soft/insufficient evidence produces HOLD. The
scorecard service/timer and status exporter are included in `deploy/`, with
Prometheus alerts in `deploy/trading-alerts.yml`. Pi release documentation
includes the scorecard timer. The active `.74`/`.75` service inspection did
not show a cluster scorecard status unit; the Hailo shadow runbook does not
make scorecard status a promotion gate.

Since `src.scorecard_gate` the verdict is also an execution prerequisite
rather than only a report. The gate reads the marker that
`scripts/run_weekly_scorecard.sh` already writes - `results/scorecards/latest_status.env`
with uppercase keys on every run, plus `results/scorecards/latest_status.json`
when `STATUS_JSON_ENABLED=true` - and allows an action only when the verdict is
`GO`, the timestamp parses, the age is within `SCORECARD_GATE_MAX_AGE_SECONDS`
(default eight days, one weekly cycle plus slack), and the run was not a
`maintenance_override`. A missing, empty, malformed, stale or non-`GO` marker
fails closed, and every reader returns `None` instead of raising so a broken
marker can never be read as approval. `src.bot_bootstrap.validate_startup_config`
refuses `DRY_RUN=false` before the exchange client or the execution lock is
created, `src.bot_runtime.run_bot` re-checks the same gate immediately before the
trading loop, and `scripts/manage_model_release.py promote` refuses to swap the
active model pair. `rollback` is deliberately not gated: reverting to a
previously validated release must stay possible, especially after a `NO-GO`.
The explicit operator overrides (`SCORECARD_GATE_ENABLED=false` for the bot,
`--no-require-go-verdict` for promotion) are logged loudly because they remove
the last pre-trade gate. The operational-health exporter publishes
`trading_scorecard_gate_enabled`/`_live_enabled`/`_allowed`/`_age_seconds`, and
`TradingLiveModeScorecardGateBlocked` (critical) plus
`TradingLiveModeWithoutScorecardGate` (warning) alert when a deployment is
configured for real orders without the evidence; a `DRY_RUN=true` profile
publishes `live_enabled 0` and can never match either rule.
`tests/test_scorecard_gate.py`, the gate cases in `tests/test_bot_bootstrap.py`,
`tests/test_bot_runtime.py`, `tests/test_manage_model_release_gate.py` and
`tests/test_alert_rules.py` cover missing, stale, `HOLD`/`NO-GO`/`ERROR`,
maintenance-override and marker-precedence paths.

**Next:** Run the weekly scorecard on the cluster profile itself (the inspected
`.74`/`.75` topology has no scorecard unit) so the gate has local evidence there,
and keep the Hailo shadow runbook aligned with the promotion gate.

### 8. Monitoring and alerting

`src.public_paper_dashboard` exposes a read-only dashboard and has access
control/read-only tests. The dashboard container was active on `.74` during
inspection. Prometheus rules alert on scorecard NO-GO/HOLD/missing and stale
portfolio snapshots. The Pi monitoring installer deploys a 30-second
operational-health textfile exporter, Alertmanager, and a loopback-only relay
to a private HTTPS webhook. Rules cover kill-switch activation, configured
Hailo health, missing/stale research, and missing health metrics in addition to
scorecard/snapshot status. Since `src.data_staleness` the exporter also probes
the bot's own inputs - the trade journal, the bot log, and, when configured,
the market recorder directory - so a silently stopped collector or bot process
is observable instead of reading as zero everywhere; the matching
`TradingBotLogMissing/Stale`, `TradingJournalStale`, and
`TradingMarketDataMissing/Stale` rules close the 2026-11-16 class of outage
where the collector died for days while the daily report still exited 0. Each
probe reads a bounded tail and fails closed to "absent". Since the
`src.task_status` reader, the scheduled wrappers
(`scripts/run_daily_dryrun_review.sh`, `scripts/run_public_paper_daily_report.sh`)
rewrite a `exit_code`/`started_unixtime`/`finished_unixtime` marker after every
attempt, and the same exporter publishes `trading_task_status_present`,
`trading_task_last_exit_code`, and `trading_task_last_run_age_seconds` per
configured task, plus `trading_disk_total_bytes`/`trading_disk_free_bytes`/
`trading_disk_used_percent` capacity gauges from `shutil.disk_usage` and - only
when `ANALYTICS_DB_URL` is set - `trading_analytics_database_enabled`/`_up`/
`_size_bytes` through a lazily imported `psycopg` probe. The matching
`TradingScheduledTaskFailed/MarkerMissing/Stale`, `TradingDiskSpaceLow`,
`TradingDiskUsedPercentHigh`, and `TradingAnalyticsDatabaseUnavailable` rules
make a failed or never-started timer and a full volume alertable;
`tests/test_alert_rules.py` rejects a rule that references a metric no exporter
emits. Disk capacity needs no extra exporter, so the Pi profile is covered by
the same 30 s timer, and the analytics probe stays optional there. No
organization-specific webhook credential or staging delivery evidence is
included in Git.

Observed packaging defect (v1.0.28, fixed in v1.0.29): the Pi release archive did
not stage `deploy/trading-operational-health.service`/`.timer`,
`deploy/trading-alert-relay.service`, the two `deploy/*.env.example` templates,
`deploy/alertmanager.yml`, `deploy/grafana-low-power.conf`, or the two scripts
those units exec, so `scripts/install_monitoring_pi.sh` aborted on a fresh
v1.0.28 install and the standalone Pi monitoring path was not installable from
the published package. The staging list now carries them, and
`.github/scripts/tests/test_pi_package_manifest.py` compares the release.yml
staging list against the paths both Pi installers read, so the two profiles stay
independently installable.

**Next:** Configure the private receiver on each deployment and validate
delivery in staging. Keep the dashboard read-only and the Pi monitoring path
independent from cluster services.

## Prioritized implementation order

1. **P0: Prevent duplicate or unsafe orders.** A persistent independent kill
   switch, startup exchange reconciliation, one-process/local plus cluster-wide
   execution ownership, and per-submission `cl_ord_id` idempotency are
   implemented; the remaining gap is a cluster-wide shared switch.
2. **P0: Fix live accounting and gates.** Exchange fees and spread are included
   in live approval and journaling, retries are idempotent, and partial-fill
   state is preserved; automatic reconciliation of journal rows against Kraken
   fill history remains.
3. **P1: Finish inference fallback.** Implement and test Hailo -> CPU -> rules
   selection without importing Hailo dependencies on the Pi path; keep missing
   inputs at HOLD.
4. **P1: Make news backtest-safe.** Persist per-item published/fetched times,
   deduplicate items, validate structured veto output, and enforce final-stage
   invocation.
5. **P1: Establish paired model releases.** Build a single versioned artifact
   manifest and atomic deployment/rollback for CPU and Hailo artifacts; assign
   training/compilation to the future x86 worker only after it exists.
6. **P2: Close operational visibility gaps.** Alert receivers, kill-switch,
   Hailo, research-feed, scorecard, portfolio-snapshot, data-staleness,
   scheduled-task exit-status, disk-capacity, analytics-database and live-mode
   scorecard-gate alerts are implemented; running the weekly scorecard on the
   cluster profile and staging delivery evidence for the private webhook remain.
   Retain a tested standalone Pi dashboard/scorecard path.