# Architecture Audit

Static repository review plus read-only inspection of both CM5 nodes on
2026-11-25. Status describes current evidence, not the desired architecture.
The live cluster workload inspected is paper/shadow only.

## Status by profile

| # | Core area | Single Raspberry Pi | CM5 cluster |
|---|---|---|---|
| 1 | Data basis and model lifecycle | **[⚠️ Partial]** Paired contract/checksum manifest and atomic activation/rollback are implemented; candidate training and DFC compilation remain separate operator steps. | **[⚠️ Partial]** Same paired release tooling; DFC and target-device validation remain external, and no x86 worker is deployed. |
| 2 | Decision pyramid and hardware fallback | **[⚠️ Partial]** Volume, spread, and ATR gates now precede optional inference; Hailo→CPU→rules fallback is tested. | **[⚠️ Partial]** Same cascade and gates; target-cluster service/device availability is not continuously exercised by CI. |
| 3 | News integration | **[⚠️ Partial]** RSS items now retain `published_at`/`fetched_at`, are deduplicated, and can drive an opt-in structured final veto with a daily call limit; persistent historical item storage is not implemented. | **[⚠️ Partial]** Same code is available, but the inspected cluster has no configured paid provider/credentials; Hailo features do not consume news. |
| 4 | Hard risk and kill switch | **[⚠️ Partial]** A persistent file kill switch now blocks live orders; daily anchor/loss-streak state remains process-local. | **[⚠️ Partial]** The file switch is host-local; no cluster-wide shared switch or distributed ownership is implemented. |
| 5 | Execution and costs | **[⚠️ Partial]** Fees and spread ceiling are accounted for; ambiguous order outcomes latch the stop marker instead of retrying. Maker/taker estimates and exchange-native idempotency remain absent. | **[⚠️ Partial]** Same controls, but live exchange use remains unvalidated on the cluster. |
| 6 | Single-Pi vs cluster operations | **[⚠️ Partial]** Local process lock, persisted live snapshot, startup balance comparison, open-order refusal, and uncertain-order latch exist; fill reconstruction needs operator review. | **[⚠️ Partial]** Optional account-scoped PostgreSQL advisory lease supplements local locks; shared PostgreSQL config and cluster deployment are operator prerequisites, and fill reconstruction remains manual. |
| 7 | Evaluation and scorecard | **[✅ Implemented]** GO/HOLD/NO-GO and HOLD for insufficient data are implemented, with Pi systemd automation. | **[⚠️ Partial]** Scorecard code and monitoring rules exist, but the inspected cluster deployment does not show scorecard automation as part of its active bot topology. |
| 8 | Monitoring and alerting | **[⚠️ Partial]** Pi installer configures Alertmanager, a private HTTPS webhook relay, and kill-switch/Hailo/research health alerts plus bounded journal/bot-log/market-data staleness probes; operator webhook configuration and staging delivery validation remain required. | **[⚠️ Partial]** Alert rules and relay are available in the repository, but cluster-specific installation/configuration and live receiver validation remain outstanding. |

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

`src.bot_bootstrap.load_tabular_predictor` wraps remote Hailo with lazy CPU
CatBoost fallback, then emits a HOLD result that preserves the existing rules
signal if CPU loading/prediction also fails. Invalid predictions are rejected.
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

`src.trading_bot.CryptoTradingBot._can_open_new_positions`,
`_refresh_daily_anchor`, and close-performance bookkeeping enforce configured
entry limits, daily-loss checks, cooldowns, and loss-streak pauses.
`src.execution_service.execute_real_trade` checks the persistent marker
configured by `TRADING_KILL_SWITCH_FILE` immediately before all live orders.
The daily anchor and loss-streak state remain process-local; the kill switch
blocks both buys and sells.

**Next:** Persist risk state and an operator-controlled kill-switch latch;
evaluate it immediately before every order, fail closed on unreadable state,
and test restart persistence and precedence over rules, CPU, Hailo, and LLM.

### 5. Execution and fee logic

`src.dry_run_execution` models configurable fees and slippage. `src.market_data`
retries transient market-data errors, and `src.execution_service` catches
Kraken/CCXT exchange errors. Live fill confirmation preserves the final order
record and includes quote/traded-asset fees in entry basis and net sell proceeds
when they can be converted. Unknown fee assets are preserved and leave journal
net-PnL fields blank. The configured spread ceiling is checked before detailed
analysis and again before entry. Ambiguous order submission/fill outcomes
persistently activate the kill switch instead of being blindly retried.
Separate maker/taker schedules and exchange-native idempotency keys remain
absent.

**Next:** Account using exchange fill/fee records, preserve partial fills, and
include spread and fee estimates in pre-trade approval. Add timeout/idempotency
tests that prove a retry cannot create a duplicate order.

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

**Next:** Make a fresh GO an explicit prerequisite for model promotion and
live-mode enablement; wire scorecard status into the cluster operational
dashboard and test stale/missing metrics as HOLD.

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
probe reads a bounded tail and fails closed to "absent". No
organization-specific webhook credential or staging delivery evidence is
included in Git; daily-report exit status and disk/database capacity remain
uninstrumented.

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

**Next:** Configure the private receiver on each deployment, validate delivery
in staging, and add daily-report exit-status and disk/database capacity alerts.
Keep the dashboard read-only and the Pi monitoring path independent from cluster
services.

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
6. **P2: Close operational visibility gaps.** Alert receivers, kill-switch,
   Hailo, research-feed, scorecard, portfolio-snapshot, and data-staleness
   alerts are implemented; cluster scorecard promotion gating, daily-report
   exit status, and disk/database capacity alerts remain. Retain a tested
   standalone Pi dashboard/scorecard path.