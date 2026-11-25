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
precheck or bridge failure. Such failures occur before status export and can
leave the previous `latest_status.*` files and `latest_scorecard.txt` link
untouched. Check the current run's console output and, if created, its
timestamped report rather than treating those status files as fresh. A
scorecard process that returns an unexpected code after it has run is instead
exported with `VERDICT=ERROR`.
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

## Runtime health and alert delivery

The Pi monitoring installer now installs a 30-second operational-health timer.
It publishes `trading_kill_switch_active`, optional Hailo health, and research
signal presence/age to the Node Exporter textfile collector. The health unit
uses `/etc/trading-health.env`, not the bot's credential-bearing `.env`; copy
`deploy/trading-health.env.example` there only when a Hailo health endpoint is
configured. The endpoint must be loopback or a trusted private address.

Prometheus alerts cover scorecard GO/HOLD/NO-GO and staleness, portfolio
snapshot staleness, kill-switch activation, configured Hailo health failures,
missing/stale research inputs, and missing operational metrics. The installer
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
