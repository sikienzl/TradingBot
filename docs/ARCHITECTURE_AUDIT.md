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
| 1 | Data basis and model lifecycle | **[⚠️ Partial]** Paired contract/checksum manifest, full per-side validation reports and training provenance stored in the manifest, a read-only `validate` inspection command, atomic activation/rollback and a fail-closed read of which release is live are implemented, and the release CLI is exercised end to end by deployment CI. Candidate training and DFC compilation remain separate operator steps. | **[⚠️ Partial]** Same paired release tooling, manifest reports, validation command and active-release reader; DFC and target-device validation remain external, and no x86 worker is deployed. |
| 2 | Decision pyramid and hardware fallback | **[✅ Implemented]** Volume, spread, and ATR gates run before any model or paid call, and the paid news veto runs after the pre-trade cost budget; the order is asserted end to end. Backend selection sits behind `src.inference_backend` with injected backends, and a dedicated CI job runs the Hailo→CPU→rules-only matrix with no Hailo package, device or model files installed. The backend that served each decision is counted on disk and exported as Prometheus gauges. | **[⚠️ Partial]** Same cascade, gates and per-decision backend counters; target-cluster service/device availability is not continuously exercised by CI, so a cluster fallback is detected from the exported backend mix rather than a readiness probe. |
| 3 | News integration | **[✅ Implemented]** RSS items retain `published_at`/`fetched_at`, are deduplicated, and are additionally appended to an immutable per-item JSONL store (`src/news_item_store.py`) with a point-in-time replay helper; the opt-in structured final veto reads that store and fails closed when it has no fresh item. The store is bounded by the shared retention policy (`src/jsonl_retention.py`, `deploy/jsonl-retention.timer`). | **[⚠️ Partial]** Same code is available, but the inspected cluster has no configured paid provider/credentials and no retention unit installed; Hailo features do not consume news. |
| 4 | Hard risk and kill switch | **[✅ Implemented]** The daily anchor, loss-streak pause, hourly BUY budget and re-entry cooldowns are persisted in `TRADING_RISK_STATE_FILE` and restored at startup; unreadable or unwritable risk state blocks new entries and latches the persistent kill switch in live mode. | **[⚠️ Partial]** Same persisted state per node, but the file and the stop marker are host-local; no cluster-wide shared switch or distributed state store is implemented. |
| 5 | Execution and costs | **[✅ Implemented]** Exchange fill/fee records, a pre-trade taker-fee + spread + slippage cost budget (`MAX_ENTRY_COST_BPS`), and a Kraken `cl_ord_id` per submission are in place; a submission timeout is reconciled instead of resubmitted and an unknown outcome latches the stop marker. Partial fills keep their requested amount in the journal, and `src.fill_reconciliation` now compares every journalled live order against the Kraken order history automatically (bot startup plus an hourly timer) and reports unmatched orders and quantity/fee differences. | **[⚠️ Partial]** Same controls and the same reconciliation code, but live exchange use remains unvalidated on the cluster. |
| 6 | Single-Pi vs cluster operations | **[⚠️ Partial]** Local process lock, persisted live snapshot, startup balance comparison, open-order refusal, uncertain-order latch, and automatic journal-to-exchange fill reconciliation with alerting exist; deriving a trusted position snapshot from Kraken fills still needs operator review. | **[⚠️ Partial]** Optional account-scoped PostgreSQL advisory lease supplements local locks; shared PostgreSQL config and cluster deployment are operator prerequisites, the reconciliation marker is host-local, and fill-to-position reconstruction remains manual. |
| 7 | Evaluation and scorecard | **[✅ Implemented]** GO/HOLD/NO-GO and HOLD for insufficient data are implemented, with Pi systemd automation. A fresh `GO` verdict is a prerequisite for `DRY_RUN=false` and for `scripts/manage_model_release.py promote`, and every run is additionally appended to an immutable per-run verdict history (`src.scorecard_verdict_history.py`) that the weekly tuning cycle reads as its evidence chain. | **[⚠️ Partial]** Same gate code and monitoring rules; the inspected cluster deployment still does not show scorecard automation as part of its active bot topology, so the gate reads the marker produced by the Pi/cluster `scorecard-status` timer. |
| 8 | Monitoring and alerting | **[⚠️ Partial]** Pi installer configures Alertmanager, a private HTTPS webhook relay, and kill-switch/Hailo/research health alerts plus bounded journal/bot-log/market-data staleness probes, scheduled-task exit-status markers for the daily review, weekly scorecard and weekly tuning units, disk-capacity gauges, fill-reconciliation presence/completeness/mismatch/staleness gauges, per-decision tabular inference backend mix gauges with fallback and rules-only alerts, opt-in active-model-release presence/verification gauges with missing-release and artifact-mismatch alerts, and an optional analytics-database probe; operator webhook configuration and staging delivery validation remain required. | **[⚠️ Partial]** Alert rules and relay are available in the repository, but cluster-specific installation/configuration and live receiver validation remain outstanding. |

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
checksums, and atomically activates the release. The manifest also carries a
`validation_reports` side for CPU and Hailo - each side's feature contract,
features, label map, validation metrics, training provenance and walk-forward
folds as recorded by the trainer - so an operator can review what a release
actually measured after promotion instead of only before it. Promotion requires
the validation metrics and provenance to be present on both sides, and
`validate_release` re-checks the stored manifest against the release contract and
the artifact checksums without touching the active release.
`rollback_release` verifies checksums before switching back.
`scripts/manage_model_release.py` exposes `promote`, `validate` and `rollback`,
ships inside the Pi package, and `.github/scripts/tests/test_model_release_cli.py`
drives a full promote/validate/rollback cycle in the release tooling job, which
also proves the CLI stays importable without numpy, pandas or CatBoost.
Which release is live is now readable without a shell command.
`src.model_release.read_active_release` resolves `TRADING_MODEL_RELEASE_ROOT`,
follows only an `active` symlink that points at a directory directly under
`releases/`, re-checks the manifest shape and re-hashes every artifact, and never
raises: a missing, dangling, absolute or out-of-tree link, a malformed manifest,
or a tampered artifact is reported rather than treated as a valid release.
`src.bot_bootstrap.log_active_release` uses it at startup to log the release ID,
contract, feature count, training-data hash and verification result, to log an
error when the live artifacts no longer match their manifest, and to warn when
`TABULAR_MODEL_PATH` points outside the active release.
`scripts/export_operational_health.py` publishes the same state as gauges and
`deploy/trading-alerts.yml` pages a missing release or an artifact mismatch. The
root is opt-in per host, so a profile that never promotes paired releases
publishes no release gauge and cannot match either alert. Observed on the
inspected deployments: `TRADING_MODEL_RELEASE_ROOT` is not set there yet, so this
visibility is implemented and tested but not yet active on the live cluster.
`scripts/update_autoresearch_signal.py` and the tuning cycle support research/parameter updates, but do not establish a
separate x86 training worker or automatic DFC/promotion workflow.

**Next:** Automate candidate training, DFC compilation, target-Hailo validation,
and explicit promotion on the planned x86 worker. Set
`TRADING_MODEL_RELEASE_ROOT` on a deployment that has promoted a paired release
and confirm the release gauges and both new alerts on the live receiver.

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

The selected backend is additionally recorded per decision. Every tabular
prediction returned by `analyze_coin` is counted by
`src.inference_decision_stats.py`, which reuses the `backend` tag the fallback
cascade already emits and keeps a cumulative counter file
(`results/inference/backend_stats.json` by default, written atomically and
throttled so a fast loop cannot hammer the SD card). A candidate that never
reaches inference - rejected by the volume, spread or ATR gate - produces no
counter, so the mix describes real inference decisions. A missing, unreadable or
wrong-version counter file is treated as unknown and never invents a backend,
and a counter write failure is logged once and never changes a decision.
`scripts/export_operational_health.py` publishes the mix as
`trading_inference_decisions_total`, `trading_inference_backend_share` and
`trading_inference_stats_age_seconds`, reading only, so it stays compatible with
the exporter's `ProtectSystem=strict`. `deploy/trading-alerts.yml` then alerts
when a deployment with tabular inference enabled has no counters at all
(`TradingInferenceBackendCountersMissing`), when a Hailo-profile deployment
served most decisions from CPU or rules-only
(`TradingInferenceBackendFallback`), and when any deployment drifts into
rules-only (`TradingInferenceRulesOnlyDecisions`). A CatBoost-only Pi publishes
`trading_inference_expected_backend_hailo=0`, so the fallback alert cannot fire
on a profile that never expects Hailo. `tests/test_alert_rules.py` asserts the
gauge set per profile, and the CI inference-matrix job runs the counter tests on
the no-package profile.

**Next:** Exercise the target-cluster Hailo service and device availability
continuously (readiness probe plus alert on the cluster profile), so a fallback
is detected before the exported backend mix has drifted rather than after.

### 3. News feed and veto schema

`scripts/fetch_research_signal.py` supports RSS/Atom and Fear & Greed. RSS
records now require a valid, non-future publication timestamp, retain per-item
`published_at` and `fetched_at`, and deduplicate by link or content/time hash.
Every scored item is additionally appended to an immutable per-item store
(`src/news_item_store.py`, default `data/research_news_items.jsonl`). Each line
is a canonical JSON record with both timestamps, the provider, a content hash
and a stable `record_id`, so re-running the hourly fetch never duplicates an
item and never rewrites history; `replay_news_items()` returns the point-in-time
view a simulated decision would have seen.
`src.api_models.NewsVetoPayload` validates `sentiment_score`, `event_risk`,
`veto_buy`, and `reason`. `src.news_veto.evaluate_news_veto` can run only after
momentum and cooldown checks, is disabled by default, uses a persisted daily
call budget, and fails closed on stale/missing data or malformed/provider
errors. When the signal file points at a store (`integration.news_store_path`,
or `RESEARCH_NEWS_STORE_PATH`), the veto reads the durable per-item records
instead of the aggregate file and fails closed if that store has no fresh item.
A store failure never fails the research signal itself.

**Next:** Validate the selected provider's structured-output support and pricing
in staging before enabling the paid veto. The per-item store is no longer an
operator growth risk: `src/jsonl_retention.py` rewrites it under the store's own
exclusive lock to an age horizon plus a record floor that keeps the dedupe window
intact, keeps a rotating backup, and publishes a `src.task_status` marker; the Pi
profile installs it as `jsonl-retention.timer`. The inspected cluster has no such
unit, and only one writer per store may prune it.

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

Journal rows are now checked against the exchange automatically.
`src.fill_reconciliation` reads the recent live rows of the journal
(`src.journal_order_reader`, bounded tail, `dry_run=false` only), fetches the
account order history (`src/exchange_order_history`, one page per symbol scope
with a scope cap, plus an explicit `clientOrderId` lookup for an order outside
the window), and classifies each row as `matched`, `partial_fill`, `unmatched`
or `not_checkable` while comparing filled quantity and fee in the base currency
(`src/fill_reconciliation_report` writes the summary marker and JSON report).
`src.bot_runtime.run_fill_reconciliation` runs one pass inside the execution-
owner lock after the live scorecard gate and before the trading loop, and
`deploy/fill-reconciliation.timer` repeats it hourly; `scripts/reconcile_fills.py`
is the manual command with stable exit codes. The pass is read-only and never
places, cancels, or rewrites anything, so it cannot interact with the risk
guardrails; a missing credential, unreadable journal, or failing exchange call
makes the pass `incomplete` ("nothing verified") rather than clean, and a
finding is logged and alerted instead of vetoing a trade.

**Next:** Validate the cost budget, idempotency path, and reconciliation against
the live exchange on a staging account before enabling live trading on either
profile, and decide the retention policy for the reconciliation reports.

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
order checks are implemented and journal fills are reconciled against the
exchange automatically, but the reconciliation marker is written and read on one
host only: it is not shared between nodes, and exact fill-to-position
reconstruction remains an operator step.

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

Since `src.scorecard_verdict_history` the verdict is also durable history rather
than only the last snapshot. `scripts/run_weekly_scorecard.sh` appends one JSONL
record per run (verdict, run mode, exit codes, reason, profile, report file and
the scorecard metrics) to `results/scorecards/verdict_history.jsonl`, and the
same wrapper installs an `EXIT` trap so an early abort publishes an
`ERROR`/`HOLD` marker for the current run instead of leaving the previous
verdict standing. The `(timestamp_utc, verdict, report_file)` triple is the
record identity, a torn final line is repaired inside an exclusive lock, and
every reader only reads a bounded tail (line limit plus byte cap) and fails
closed to "no evidence" instead of raising. `resolve_scorecard_status()` prefers
the JSON marker, then the env marker, then the newest history record and tags
the result with the `source` used; the weekly tuning chain reads through it, so
the default Pi profile - where `STATUS_JSON_ENABLED=false` and only the env
marker exists - is no longer treated as having no evidence. The live-trading
gate deliberately keeps reading only the two markers: after an aborted cycle the
stale `GO` marker is not evidence about the current record, so it fails closed
rather than falling back to history. `tests/test_scorecard_verdict_history.py`
and `tests/test_run_tuning_cycle.py` cover append/dedupe, torn-tail recovery,
the three-source precedence, the age limit, the recent `NO-GO` guard and the
wrapper marker.

**Next:** Run the weekly scorecard on the cluster profile itself (the inspected
`.74`/`.75` topology has no scorecard unit) so the gate has local evidence there,
and keep the Hailo shadow runbook aligned with the promotion gate. Verdict
history retention is now repository code rather than an operator step:
`src/jsonl_retention.py` prunes the append-only history to an age horizon and a
record floor above the reader window, and the Pi profile runs it from
`jsonl-retention.timer`; the inspected cluster has no retention unit installed.

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

The scheduled-task probe covered only the two daily report wrappers, so the
weekly scorecard and weekly tuning units could fail - or never start - without
any signal, and `scorecard.timer`/`tuning-cycle.timer` did not name their
`Unit=`. Both weekly wrappers now write the same lowercase `exit_code` marker
through an `EXIT` trap, the Pi health unit lists `daily_review`, `scorecard` and
`weekly_tuning` in `TRADING_TASK_STATUS_FILES`, both timers declare `Unit=` with
`AccuracySec=30s`, and `deploy/tuning-cycle.service` accepts exit `3` (the safety
guard refused to change parameters) as a clean unit exit under a
`TimeoutStartSec=2100` backstop above the wrapper's own cycle timeout. The
matching `TradingTuningCycleApplyBlocked` rule names the refusal explicitly;
`TradingScheduledTaskFailed` still fires for exit `3` as well, which is
deliberate - the task marker and the tuning cycle summary distinguish the cause.
`tests/test_run_tuning_cycle.py` drives the wrapper itself and asserts the marker
contents, including the missing-interpreter case that previously exited `127`
with no marker at all.

Both append-only JSONL stores (`data/research_news_items.jsonl` and
`results/scorecards/verdict_history.jsonl`) grow on every run and had no bound, so
`src/jsonl_retention.py` now owns the policy and `scripts/prune_jsonl_stores.py`
applies it: each store is rewritten in place under its own exclusive `flock`,
reusing the file inode so a concurrent appender cannot lose a record, keeping a
rotating `<store>.bak-<UTC stamp>` first, and keeping every record that has no
usable timestamp. The age horizon and the record floor are independent, `0` turns
either off, and the default floors stay above each store's dedupe window so a
pruned store still rejects a re-fetched headline and the tuning cycle still reads
its evidence chain. The command publishes a `src.task_status` marker at
`results/jsonl_retention/latest_status.env`, which the Pi health unit lists as the
`jsonl_retention` task, and the exporter publishes
`trading_jsonl_store_bytes`/`_records`/`_pruned` from that marker; the matching
`TradingJsonlStoreGrowing` and `TradingJsonlStoreOverRetentionGuard` rules make an
unbounded store alertable instead of a slow disk exhaustion. Retention is enabled
by default and `TRADING_JSONL_RETENTION_ENABLED=0` is the rollback; the inspected
cluster has no retention unit installed, and because the lock is host-local only
one node per store may run the pruner.

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

Observed packaging defect (found while wiring the verdict history): the release
staging list carried `scripts/run_weekly_tuning_cycle.sh` but not the
`scripts/run_tuning_cycle.py` it execs, so a packaged Pi install would have
failed the tuning timer with a missing file. The list now carries that script and
the verdict-history helper, and the manifest test additionally scans every
staged wrapper for `${ROOT_DIR}/scripts/...` calls and `-m scripts.<name>`
modules, so a wrapper whose helper is unstaged now fails the packaging test.

Observed packaging gap (found while exercising the release CLI in deployment CI):
`docs/USAGE.md` tells an operator to run
`python3 -m scripts.manage_model_release promote|rollback` on the Pi, but the
release staging list never copied `scripts/manage_model_release.py`, so the
documented model-swap path was not available in the published Pi package. The
script and `src/model_release.py` are now staged and release-blocking, and
`.github/scripts/tests/test_model_release_cli.py` runs a real
promote/validate/rollback cycle in the release tooling job - the only CI job
without third-party dependencies, which is also what keeps the release CLI free
of numpy, pandas and CatBoost imports.

**Next:** Configure the private receiver on each deployment and validate
delivery in staging. Keep the dashboard read-only and the Pi monitoring path
independent from cluster services.

## Prioritized implementation order

1. **P0: Prevent duplicate or unsafe orders.** A persistent independent kill
   switch, startup exchange reconciliation, one-process/local plus cluster-wide
   execution ownership, and per-submission `cl_ord_id` idempotency are
   implemented; the remaining gap is a cluster-wide shared switch.
2. **P0: Fix live accounting and gates.** Exchange fees and spread are included
   in live approval and journaling, retries are idempotent, partial-fill state is
   preserved, and journal rows are reconciled against Kraken fill history
   automatically with alerting; validating that reconciliation against the live
   exchange on a staging account remains.
3. **P1: Finish inference fallback.** Implement and test Hailo -> CPU -> rules
   selection without importing Hailo dependencies on the Pi path; keep missing
   inputs at HOLD.
4. **P1: Make news backtest-safe.** Persist per-item published/fetched times,
   deduplicate items, validate structured veto output, and enforce final-stage
   invocation; immutable per-item storage, point-in-time replay and a bounded
   store retention policy are implemented, provider staging validation remains.
5. **P1: Establish paired model releases.** Build a single versioned artifact
   manifest and atomic deployment/rollback for CPU and Hailo artifacts; assign
   training/compilation to the future x86 worker only after it exists.
6. **P2: Close operational visibility gaps.** Alert receivers, kill-switch,
   Hailo, research-feed, scorecard, portfolio-snapshot, data-staleness,
   scheduled-task exit-status (daily review, weekly scorecard, weekly tuning and
   JSONL store retention), disk-capacity, analytics-database, live-mode
   scorecard-gate, fill-reconciliation, tuning-apply-refusal and JSONL
   store-size alerts are implemented; running
   the weekly scorecard on the cluster profile and staging delivery evidence for
   the private webhook remain. Retain a tested standalone Pi
   dashboard/scorecard path.