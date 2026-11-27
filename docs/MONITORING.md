# Scorecard and monitoring

## Weekly Go/No-Go scorecard

Run the automated decision gate:
```sh
bash scripts/run_weekly_scorecard.sh
```
Saves a timestamped report to `results/scorecards/` and updates `results/scorecards/latest_scorecard.txt`.
Also writes `results/scorecards/latest_status.env` (override via `STATUS_FILE`).
Optionally writes `results/scorecards/latest_status.json` when `STATUS_JSON_ENABLED=true`.
Optionally writes `results/scorecards/latest_status.prom` when `STATUS_PROM_ENABLED=true`.
Exit codes: `0` = GO, `2` = HOLD, `3` = NO-GO.
Exit code `1` can indicate an early failure, including a required AutoResearch
precheck or bridge failure. Since v1.0.31 such failures are published: the
script installs an `EXIT` trap, so an abort replaces `latest_status.env` with an
`ERROR`/`HOLD` marker for the current run and appends a verdict-history record
instead of leaving the previous verdict standing. The timestamped report of the
aborted run is still the place to read the cause. A scorecard process that
returns an unexpected code after it has run is exported with `VERDICT=ERROR`.
Missing, invalid, or empty journal inputs are treated as `HOLD` because they indicate insufficient usable data rather than a scorecard execution failure.
When strict maintenance override is used (`AUTORESEARCH_STRICT_ALLOW_MAINTENANCE_FALLBACK=true`),
weekly automation returns a dedicated degraded-mode code (default `4`) for the run.
This override disables the required hard-fail and enables neutral fallback for
that run; it does not replace a successful producer signal with a neutral one.

Machine-readable report markers are included for log parsers:
- `RUN_MODE=standard|maintenance_override`
- `UNDERLYING_EXIT_CODE=<int>` (only in maintenance override mode)
- `FINAL_EXIT_CODE=<int>`

`latest_status.env` fields:
- `TIMESTAMP_UTC`
- `RUN_MODE`
- `VERDICT`
- `UNDERLYING_EXIT_CODE`
- `FINAL_EXIT_CODE`
- `REPORT_FILE`
- `LATEST_REPORT_LINK`
- `exit_code`, `started_unixtime`, `finished_unixtime` (lowercase:
  the scheduled-task contract read by `src.task_status`, which is what lets the
  `TradingScheduledTask*` alerts cover `scorecard.timer`)

`results/scorecards/verdict_history.jsonl` (append-only, one record per run;
`SCORECARD_VERDICT_HISTORY_FILE` relocates it, `off` disables it):
- `schema_version`, `timestamp_utc`, `verdict`, `run_mode`
- `underlying_exit_code`, `final_exit_code`, `primary_reason`, `profile`
- `report_file`, and the scorecard `metrics` object

The history is the last evidence fallback for the live gate, the tuning chain
and the health exporter when both markers are missing, and it is the source the
tuning cycle's recent-`NO-GO` guard reads. Read it with
`python -m scripts.scorecard_verdict_history show --limit 5` or `... stats`.
The file stays append-only for every writer, and `jsonl-retention.timer` bounds
it to an age horizon plus a record floor above that reader window - see
"JSONL store retention" below.

`latest_status.json` fields (when enabled):
- `timestamp_utc`
- `run_mode`
- `verdict`
- `primary_reason`
- `underlying_exit_code`
- `final_exit_code`
- `report_file`
- `latest_report_link`
- `metrics.closed_trades`
- `metrics.win_rate`
- `metrics.realized_pnl`
- `metrics.avg_pnl`
- `metrics.profit_factor`
- `metrics.max_drawdown_base`
- `metrics.max_drawdown_pct`

`latest_status.prom` metrics (when enabled; this file also includes AI and
runtime metrics, not just scorecard status):
- `trading_scorecard_final_exit_code`
- `trading_scorecard_underlying_exit_code`
- `trading_scorecard_timestamp_seconds`
- `trading_scorecard_run_mode{mode="..."}`
- `trading_scorecard_verdict{verdict="..."}`
- `trading_scorecard_fail_reason{reason="..."}`
- `trading_scorecard_closed_trades`
- `trading_scorecard_win_rate_percent`
- `trading_scorecard_realized_pnl`
- `trading_scorecard_avg_pnl_per_sell`
- `trading_scorecard_profit_factor`
- `trading_scorecard_max_drawdown_base`
- `trading_scorecard_max_drawdown_pct`
- `trading_ai_copilot_daily_calls`
- `trading_ai_copilot_monthly_calls`
- `trading_ai_copilot_monthly_spend_usd`
- `trading_ai_copilot_budget_used_pct`
- `trading_ai_copilot_benchmark_daily_calls`
- `trading_ai_copilot_benchmark_monthly_calls`
- `trading_ai_copilot_benchmark_monthly_spend_usd`
- `trading_ai_copilot_benchmark_budget_used_pct`
- `trading_ai_copilot_model_info{role="...",model="..."}` (one series each
  for `primary` and `benchmark`)
- `trading_runtime_portfolio_value`
- `trading_runtime_portfolio_start_value`

The status JSON's `metrics` object contains scorecard metrics only.
Prometheus additionally reports AI usage/model and portfolio runtime data;
missing state files can yield zero-valued AI/runtime metrics. The scorecard
profit-factor metric uses `-1` to represent an infinite factor (no losses).

Monitoring extraction examples:

```sh
# Read the final status directly from latest_status.env
grep -E '^(RUN_MODE|VERDICT|FINAL_EXIT_CODE)=' results/scorecards/latest_status.env

# Source in shell scripts
set -a; . results/scorecards/latest_status.env; set +a
echo "mode=$RUN_MODE verdict=$VERDICT exit=$FINAL_EXIT_CODE"

# Enable and read JSON status
STATUS_JSON_ENABLED=true bash scripts/run_weekly_scorecard.sh
cat results/scorecards/latest_status.json

# Enable and read Prometheus textfile status
STATUS_PROM_ENABLED=true bash scripts/run_weekly_scorecard.sh
cat results/scorecards/latest_status.prom
```

The report can contain `RUN_MODE` more than once: an initial `standard`
marker and the final mode. Use `latest_status.env` for the final status rather
than assuming the first report marker is final.

Or run the scorecard manually with custom thresholds:
```sh
python3 -m src.go_no_go_scorecard --file trade_journal.csv \
  --lookback-days 7 --starting-capital 20 \
  --min-closed-trades 200 --min-win-rate 45 \
  --min-profit-factor 1.2 --max-drawdown-pct 10
```

## Server example

```sh
PYTHON_BIN=./.venv/bin/python \
JOURNAL_FILE=./trade_journal.csv \
LOOKBACK_DAYS=7 \
MIN_CLOSED_TRADES=200 \
MIN_WIN_RATE=45 \
MIN_PROFIT_FACTOR=1.2 \
MAX_DRAWDOWN_PCT=10 \
bash scripts/run_weekly_scorecard.sh
```

The weekly script uses `PYTHON_BIN` if set, otherwise `.venv/bin/python`, otherwise `python3`. The VS Code task `Scorecard: Weekly Go/No-Go` runs the same automation.

## Scorecard gating of live trading and model promotion

The verdict in `results/scorecards/latest_status.env` / `latest_status.json` is
not only a report: `src.scorecard_gate` makes a fresh `GO` a prerequisite for
two actions.

- `DRY_RUN=false` is refused at startup (`src.bot_bootstrap`) and again
  immediately before the trading loop (`src.bot_runtime`), so the bot exits
  before an exchange client or the execution lock exists.
- `scripts/manage_model_release.py promote` refuses to activate a new model
  pair. `rollback` stays available without a verdict on purpose - going back to
  a known-good release must always work, especially after a `NO-GO`.

An action is allowed only when the marker exists, parses, reports `GO`, has a
usable timestamp no older than `SCORECARD_GATE_MAX_AGE_SECONDS` (default eight
days, i.e. one weekly cycle plus slack), and was not a `maintenance_override`
run. Everything else - missing file, unreadable file, `HOLD`, `NO-GO`, `ERROR`,
stale verdict - fails closed and the process refuses to start with the reason in
the message. The JSON marker is preferred and the env marker is the fallback, so
a Pi that only runs the weekly timer is covered without enabling JSON export.

Deliberate operator overrides, both of which log a warning because they remove
the last pre-trade gate:

```sh
# bot: skip the live-mode prerequisite
SCORECARD_GATE_ENABLED=false

# promotion CLI: skip it for one promotion
python3 scripts/manage_model_release.py promote ... --no-require-go-verdict
```

Check the evidence the gate sees:

```sh
grep -E '^(TIMESTAMP_UTC|RUN_MODE|VERDICT)=' results/scorecards/latest_status.env
python3 - <<'PY'
from src.scorecard_gate import scorecard_gate_settings
print(scorecard_gate_settings().evaluate())
PY
```

## Node Exporter textfile collector

Relative path setup:

```sh
# write metrics to a collector subfolder in this repo
STATUS_PROM_ENABLED=true \
STATUS_PROM_FILE=./results/scorecards/textfile/trading_scorecard.prom \
bash scripts/run_weekly_scorecard.sh

# run node_exporter from this repo root with relative collector directory
node_exporter --collector.textfile --collector.textfile.directory=./results/scorecards/textfile
```

Helper script (one command, relative paths):

```sh
bash scripts/run_node_exporter_textfile_example.sh

# optional overrides
COLLECTOR_DIR=./results/scorecards/textfile \
STATUS_PROM_FILE=./results/scorecards/textfile/trading_scorecard.prom \
NODE_EXPORTER_CMD=node_exporter \
bash scripts/run_node_exporter_textfile_example.sh
```

The helper runs the scorecard only once before starting Node Exporter. It
continues even if that run fails, so check the output and metric file.
Schedule separate scorecard runs with the same `STATUS_PROM_FILE` to refresh
metrics, and monitor their timestamp for staleness.

## Daily dry-run review

The Pi package also carries `deploy/daily-dryrun-review.service` and
`.timer`, which run `scripts/run_daily_dryrun_review.sh` once a day over the
local journal, bot log and AI-copilot state, writing
`results/daily_review/latest_review.{json,txt,html}`. The unit reads
`/opt/trading_2/.env`, so it only applies where the bot itself runs. It is a
reporting convenience and is not installed by `scripts/install_pi.sh` or
`scripts/install_monitoring_pi.sh`; enable it explicitly with
`sudo systemctl enable --now daily-dryrun-review.timer`. A successful run over
an empty window still exits 0, so treat the review as output to read, not as a
health signal - the staleness gauges below are the health signal.

Every attempt - success or failure - also rewrites
`results/daily_review/latest_status.env` (override with
`DAILY_REVIEW_STATUS_FILE`):

- `exit_code` - exit status of the last wrapper run
- `started_unixtime` / `finished_unixtime` - UTC epoch seconds of that run

The operational-health exporter turns that marker into the task gauges below, so
a wrapper that exits non-zero, or a timer that never starts, becomes visible
without reading systemd state or the log file. `scripts/run_public_paper_daily_report.sh`
writes the same marker to `$PUBLIC_PAPER_REPORT_DIR/latest_status.env`
(override with `PUBLIC_PAPER_STATUS_FILE`); the public-paper profile keeps its
own units out of the Pi package, so that marker is only exported on a host that
also runs the health exporter.

## Runtime health and alert delivery

The Pi monitoring installer installs a 30-second operational-health timer. It
publishes `trading_kill_switch_active`, optional Hailo health, research signal
presence/age, and data-staleness gauges for the bot's own inputs -
`trading_journal_present` / `trading_journal_age_seconds`,
`trading_bot_log_present` / `trading_bot_log_age_seconds`, and, when a recorder
directory is configured, `trading_market_data_present` /
`trading_market_data_age_seconds` - to the Node Exporter textfile collector.
The staleness gauges are what make a silently stopped collector or bot process
observable: a daily report over an empty window still exits 0, so without them
every downstream metric simply reads as zero. Each probe reads only a bounded
tail of its file and fails closed to "absent" on a missing or unreadable file,
which keeps "not collected" distinguishable from "stale".

Scheduled-task, capacity, and analytics gauges come from the same timer:

- `TRADING_TASK_STATUS_FILES=name=path,name=path` exports each wrapper marker as
  `trading_task_status_present{task="..."}` (0 when the marker is missing or
  unreadable), `trading_task_last_exit_code{task="..."}` (0 when the marker has
  no parseable code, so garbage cannot raise a failure alert), and
  `trading_task_last_run_age_seconds{task="..."}`. Only listed tasks are
  published, so a profile that does not run a timer exports nothing for it.
  The Pi unit lists `daily_review`, `scorecard`
  (`results/scorecards/latest_status.env`), `weekly_tuning`
  (`results/tuning/latest_status.env`) and `jsonl_retention`
  (`results/jsonl_retention/latest_status.env`): each wrapper writes a marker on
  every attempt, including a crash before its main step, so a stopped timer is
  visible as a stale or failed task instead of as silence.
- `TRADING_DISK_PATHS` (comma-separated) exports `trading_disk_total_bytes`,
  `trading_disk_free_bytes`, and `trading_disk_used_percent` per path using
  `shutil.disk_usage`, so no extra exporter is needed on the Pi. It defaults to
  `TRADING_DATA_DIR`, the volume the bot writes journal, state, and results to.
  An unreadable path publishes nothing rather than a false zero.
- `ANALYTICS_DB_URL` (the same variable the bot uses for PostgreSQL analytics)
  exports `trading_analytics_database_enabled`, `trading_analytics_database_up`,
  and - only when the probe answers - `trading_analytics_database_size_bytes`.
  The probe imports `psycopg` lazily and is not used at all when no URL is
  configured, so the single-node Raspberry Pi release never needs the driver.
- The live-trading scorecard gate (`src.scorecard_gate`) exports
  `trading_scorecard_gate_enabled` (0 only when an operator set
  `SCORECARD_GATE_ENABLED=false`), `trading_scorecard_gate_live_enabled`
  (1 when the deployment `.env` sets `DRY_RUN=false`),
  `trading_scorecard_gate_allowed` (1 only for a fresh `GO` verdict), and
  `trading_scorecard_gate_age_seconds` when the marker has a usable timestamp.
  The gate reads `results/scorecards/latest_status.json` first and falls back to
  `results/scorecards/latest_status.env`, so it needs no extra configuration
  under either release profile as long as the unit runs from the deployment
  root. `TRADING_DEPLOYMENT_ENV_FILE` points at the `.env` that `DRY_RUN` is
  read from; a profile that never trades live publishes `live_enabled 0` and can
  never match a live-mode alert.
- Fill reconciliation (`src.fill_reconciliation`) exports
  `trading_fill_reconciliation_enabled` (1 when the deployment is expected to
  reconcile, which is the `DRY_RUN=false` default),
  `trading_fill_reconciliation_present` (0 when no parseable marker exists),
  `trading_fill_reconciliation_complete` (0 when the last pass verified nothing,
  for example missing credentials or a failing exchange call),
  `trading_fill_reconciliation_mismatches` (orders of the last complete pass
  whose filled quantity or fee differs from the journal; an order the exchange
  does not know is counted once, inside the quantity counter) and
  `trading_fill_reconciliation_age_seconds`. The exporter only reads the marker
  that the bot and `fill-reconciliation.timer` write, so it needs no credentials
  and never calls the exchange. `TRADING_FILL_RECONCILIATION_REPORT` selects the
  marker; a host that does not run reconciliation publishes only `enabled 0` and
  `present 0`.
- JSONL store retention (`src.jsonl_retention`) exports
  `trading_jsonl_store_bytes{store="news"|"verdict"}` from the size of each
  append-only store on disk, plus `trading_jsonl_store_records` and
  `trading_jsonl_store_pruned` for the last prune pass. The values come from the
  `results/jsonl_retention/latest_status.env` marker that
  `scripts/prune_jsonl_stores.py` writes, so the exporter never opens a store,
  never takes its lock and never rewrites anything. A store that retention does
  not manage (disabled policy, or a marker without that store) publishes nothing
  rather than a false zero, and `TRADING_JSONL_RETENTION_REPORT` selects the
  marker path for both the pruner and the exporter.
- Tabular inference backend mix (`src.inference_decision_stats`) exports
  `trading_inference_decisions_total{backend="hailo"|"cpu"|"rules_only"|"other"}`
  and `trading_inference_backend_share{backend=...}` from the counter file the
  bot writes for every tabular decision, plus `trading_inference_stats_present`,
  `trading_inference_stats_age_seconds`, `trading_inference_model_enabled`
  (`USE_TABULAR_MODEL` from the bot deployment `.env`) and
  `trading_inference_expected_backend_hailo` (`TABULAR_MODEL_BACKEND=hailo`).
  This is what makes a silent `hailo -> cpu -> rules_only` fallback visible: the
  bot keeps producing decisions, only the backend mix changes. The bot owns the
  counter file (`results/inference/backend_stats.json`, override with
  `TRADING_INFERENCE_STATS_FILE`, `off` disables it) and the exporter only ever
  reads it, so both release profiles publish their own mix and a `catboost`
  deployment can never match the Hailo-fallback alert. A deployment with the
  tabular model switched off publishes `model_enabled 0` and no share gauge at
  all, and a corrupt counter file is reported as `stats_present 0` rather than
  as a fresh zero.

The health unit uses `/etc/trading-health.env`, not the bot's
credential-bearing `.env`; copy `deploy/trading-health.env.example` there.
`TRADING_JOURNAL_FILE` and `TRADING_BOT_LOG_FILE` default to the journal and
bot log under `TRADING_DATA_DIR`. `TRADING_MARKET_DATA_DIR` stays empty on
hosts that do not run the public market recorder, so no market metric is
published at all instead of a false absence. `TRADING_LOG_TIMEZONE` is the zone
of the naive `asctime` stamps in the bot log and defaults to `UTC`; set it to
match `--log-timezone` of `scripts/public_paper_daily_report.py`. A Hailo
health endpoint is only added when configured, and it must be loopback or a
trusted private address.

Prometheus alerts cover scorecard GO/HOLD/NO-GO and staleness, portfolio
snapshot staleness, kill-switch activation, configured Hailo health failures,
missing/stale research inputs, a missing or stale bot log, a stale trade
journal, missing or stale market recorder data, missing operational
metrics, a failed / never-reported / overdue scheduled task
(`TradingScheduledTaskFailed`, `TradingScheduledTaskMarkerMissing`,
`TradingScheduledTaskStale`), a weekly tuning cycle that ran but was refused by
its safety guard (`TradingTuningCycleApplyBlocked`, warning - the verdict was
missing, older than `TUNING_MAX_VERDICT_AGE_SECONDS`, or preceded by a `NO-GO`,
and `results/scorecards/latest_tuning_cycle_summary.json` records the reason),
low or nearly full disk
(`TradingDiskSpaceLow`, `TradingDiskUsedPercentHigh`), and an unreachable
analytics database (`TradingAnalyticsDatabaseUnavailable`), live trading that is
configured without a fresh `GO` verdict (`TradingLiveModeScorecardGateBlocked`,
critical) and live trading that runs with the gate switched off
(`TradingLiveModeWithoutScorecardGate`, warning), journalled fills that do not
match the exchange (`TradingFillReconciliationMismatch`, critical), a
reconciliation pass that verified nothing
(`TradingFillReconciliationIncomplete`), an overdue reconciliation marker
(`TradingFillReconciliationStale`) and a reconciliation marker that never
appeared (`TradingFillReconciliationMarkerMissing`), an append-only JSONL store
that keeps growing past 16 MiB for a day
(`TradingJsonlStoreGrowing`, warning) and one that passed the 256 MiB retention
size guard (`TradingJsonlStoreOverRetentionGuard`, critical - the pruner refuses
a store that large, so it needs an operator). A missing or overdue retention
marker is reported by the generic `TradingScheduledTask*` rules through the
`jsonl_retention` task. A tabular deployment that recorded no backend counter at
all (`TradingInferenceBackendCountersMissing`, warning), a
`TABULAR_MODEL_BACKEND=hailo` deployment whose decisions are mostly served by
the CPU or rules-only fallback (`TradingInferenceBackendFallback`, warning) and
any deployment where more than 5% of decisions had neither a Hailo nor a CPU
prediction (`TradingInferenceRulesOnlyDecisions`, critical) are the inference
fallback alerts. `tests/test_alert_rules.py`
fails when one of those rules references a metric the exporter does not emit.
The installer
also configures local Alertmanager and a loopback-only relay. Before installing
monitoring, create `/etc/trading-alert-relay.env` from
`deploy/trading-alert-relay.env.example` with the organization's private HTTPS
webhook URL, owned by `root:trading` with mode `0640`. Never commit the URL or
provider credentials. The relay binds only to `127.0.0.1:9095`; Alertmanager
posts to that local endpoint, and the relay forwards only to HTTPS. Test
delivery with a staging receiver before relying on alerts operationally.

Useful checks after installation:

```sh
systemctl status trading-operational-health.timer trading-alert-relay.service prometheus-alertmanager.service
cat /opt/trading_2/results/scorecards/textfile/trading_health.prom
promtool check rules /etc/prometheus/rules/trading-alerts.yml
amtool check-config /etc/alertmanager/alertmanager.yml
```

## JSONL store retention

Both append-only JSONL stores grow on every run and are never rewritten by their
writers, so `src/jsonl_retention.py` owns the policy and
`scripts/prune_jsonl_stores.py` applies it once a day from
`jsonl-retention.timer` (`03:30`, `Persistent=true` so a missed pass runs at the
next boot). The Pi installer enables that timer; the cluster profile has no
retention unit and stays an operator decision, because the store lock is a
host-local `flock` and only one node per store may prune it.

For each store the pruner takes the same exclusive lock the appender uses, keeps
the file inode (truncate and write, never a rename, so a concurrent appender
cannot lose a record), copies the previous content to `<store>.bak-<UTC stamp>`
first, and then drops only records that are older than the age horizon **and**
beyond the record floor. The two limits are independent and `0` turns one off.
A record with no usable timestamp is always kept, malformed lines are dropped, a
missing store is reported and not created, and a store larger than 256 MiB is
refused rather than rewritten. The default floors (news 8192 records, verdict 512
records) stay above the dedupe/read window of each store, so pruning never lets a
re-fetched headline through and never removes the evidence the tuning cycle reads.

```sh
# inspect what a pass would do, without touching either store
python -m scripts.prune_jsonl_stores --dry-run
# prune now and print the counts
python -m scripts.prune_jsonl_stores
systemctl start jsonl-retention.service
systemctl status jsonl-retention.timer
```

Exit codes: `0` pruned or unchanged, `1` a store could not be pruned, `2`
retention is disabled. The unit accepts all three as a clean unit exit; the
marker and the alerts carry the distinction. Read the last pass with
`cat /opt/trading_2/results/jsonl_retention/latest_status.env` - it holds the
`exit_code`/`started_unixtime`/`finished_unixtime` scheduled-task contract plus
the per-store `news_*`/`verdict_*` counts, and the health exporter lists it as the
`jsonl_retention` task.

Rollback is configuration only: `TRADING_JSONL_RETENTION_ENABLED=0` in the
deployment `.env` stops every rewrite, and the last `<store>.bak-<stamp>` copy
restores a store byte for byte. Nothing in the bot, the scorecard or the
dashboard depends on retention being enabled.
