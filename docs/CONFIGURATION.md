# Configuration and risk guardrails

## Configuration

The bot reads settings from environment variables and, when present, `.env`.
The table gives **code defaults when a setting is absent**, not necessarily
the values in `.env.example` or other profiles. For example, `.env.example`
sets `TRADE_AMOUNT=10`, `MAX_OPEN_TRADES=4`, `USE_TABULAR_MODEL=true`,
`AUTO_TUNE_TABULAR_CONFIDENCE=true`, and
`DYNAMIC_LOSSMAKER_MIN_PNL_LOSS=0.01`. Inspect the chosen profile and any
environment overrides before starting the bot.

| Variable | Default | Description |
|---|---|---|
| `EXCHANGE_NAME` | `kraken` | Exchange (ccxt identifier) |
| `BASE_CURRENCY` | `EUR` | Quote currency |
| `DRY_RUN` | `true` | Simulate trades (no real orders) |
| `SIMULATE_DATA` | `false` | Use generated market data; other enabled features can still access the network |
| `SIMULATION_SEED` | `42` | Deterministic seed for generated simulation data |
| `SIMULATION_REGIME` | `neutral` | Synthetic market regime: `neutral`, `uptrend`, `downtrend`, `sideways`, `crash`, `recovery`, `mixed` |
| `EXCLUDED_COINS` | `USDC,USDT,EURT,EURC,DAI,TUSD,USDP,FDUSD,USDE` | Exclude symbols from new-entry candidates; open positions can still be analyzed and managed |
| `LOSSMAKER_EXCLUDED_COINS` | `ZEC,HYPE,TON,BTC,XRP` | Additional default exclusions for symbols with persistently weak realized PnL |
| `DYNAMIC_LOSSMAKER_EXCLUSION_ENABLED` | `true` | Temporarily exclude symbols with repeated recent timeout-heavy losses in the journal |
| `DYNAMIC_LOSSMAKER_WINDOW` | `80` | Number of most recent journal rows inspected for temporary lossmaker exclusions |
| `DYNAMIC_LOSSMAKER_MIN_SELLS` | `3` | Minimum recent sells required before a symbol can be temporarily suppressed |
| `DYNAMIC_LOSSMAKER_MIN_PNL_LOSS` | `0.003` | Minimum negative summed realized PnL before the temporary suppression applies |
| `RECENT_PNL_GUARD_STALE_HOURS` | `24` | Age after which guard logs flag the latest close as stale; this is diagnostic and does not disable the guard |
| `TRADING_RUN_ID` | generated | Optional run identifier for grouping journal and analytics events |
| `COIN_MAX_HOLD_SECONDS` | `LINK:420,SUI:420,ONDO:420` | Coin-specific hold-time overrides for weak follow-through symbols |
| `TRADE_AMOUNT` | `20` | Base currency per trade |
| `PORTFOLIO_TRADE_AMOUNT_MULTIPLIERS` | `50:1.10,100:1.25,200:1.50,300:1.75,500:2.20,1000:3.00` | Scales the base `TRADE_AMOUNT` upward once the portfolio reaches each milestone |
| `MAX_OPEN_TRADES` | `3` | Base maximum concurrent positions |
| `PORTFOLIO_MAX_OPEN_TRADES_TIERS` | `100:3,200:4,500:5,1000:6` | Raises the concurrent slot count in later portfolio stages without changing the early small-account regime |
| `OHLCV_TIMEFRAME` | `1h` | Candle interval |
| `MAX_ENTRY_SPREAD_BPS` | `100` | Maximum bid/ask spread for new entries; missing or wider quotes are skipped before OHLCV/model analysis and checked again at the final candidate gate |
| `MAX_ENTRY_COST_BPS` | `120` | Pre-trade cost budget in basis points: estimated taker fee + spread + slippage. Values below `1` disable the gate; a blocked candidate never reaches a paid news/LLM check |
| `LIVE_TAKER_FEE_RATE` | market `taker` | Fallback taker fee as a fraction (e.g. `0.0026`) when the parsed market does not report one |
| `LIVE_SLIPPAGE_RATE` | `0.001` | Assumed slippage as a fraction of the entry price, used only for the cost estimate |
| `TICKER_BATCH_SIZE` | `80` | Symbols per `fetch_tickers` request (smaller is more stable) |
| `TICKER_FETCH_RETRIES` | `2` | Retries per ticker batch on exchange/network errors |
| `USE_TABULAR_MODEL` | `false` | Enable tabular-model signal filter |
| `TABULAR_MODEL_BACKEND` | `catboost` | Tabular backend: local `catboost` or remote `hailo`. With `hailo` the bot falls back to the local CatBoost predictor when the Hailo service, package or prediction is unusable, and to rules-only when the CPU model is also unavailable (`src.inference_backend`) |
| `HAILO_SERVICE_URL` | `http://127.0.0.1:8001` | Hailo inference endpoint; set the host address when using a separate server |
| `HAILO_API_TOKEN` | unset | Required bearer token when Hailo is enabled |
| `HAILO_TIMEOUT_SECONDS` | `3` | Maximum remote inference request time |
| `TABULAR_MIN_CONFIDENCE` | `0.45` | Minimum tabular-model confidence required for non-HOLD signal |
| `UPTREND_ENTRY_MAX_RSI_BY_COIN` | `XDC:78,TRX:74` | Coin-specific RSI override for uptrend entry gating |
| `UPTREND_ENTRY_MIN_BUY_PROBA_BY_COIN` | `TRX:0.150` | Coin-specific CatBoost buy-probability override for uptrend entry gating |
| `UPTREND_ENTRY_MAX_SELL_PROBA_BY_COIN` | `ONDO:0.445` | Coin-specific CatBoost sell-probability override for uptrend entry gating |
| `UPTREND_ENTRY_MIN_PROBA_EDGE_BY_COIN` | `TRX:-0.13,ONDO:-0.055` | Coin-specific probability-edge override for uptrend entry gating |
| `TABULAR_RESEARCH_SIGNAL_PATH` | `./data/research_signal_latest.json` | Latest AutoResearch JSON used as model input features |
| `RESEARCH_NEWS_STORE_PATH` | `./data/research_news_items.jsonl` | Append-only per-item news store written by `scripts/fetch_research_signal.py` (`none` disables it). The bot reads it back through `integration.news_store_path` in the signal file, so no bot restart is needed to change it |
| `AUTO_TUNE_TABULAR_CONFIDENCE` | `false` | Auto-adjust CatBoost threshold |
| `USE_ML_MODEL` | `false` | Enable LLM signal (GPU recommended) |

### Simulation regimes

When `SIMULATE_DATA=true`, the bot can emulate different market states while staying deterministic for a fixed `SIMULATION_SEED`. This does not disable network access by other enabled features.

- `neutral`: balanced oscillating market with moderate volume
- `uptrend`: rising prices with healthier trend persistence
- `downtrend`: falling prices with somewhat heavier defensive volume
- `sideways`: flat range-bound action with lower average activity
- `crash`: sharp downside move with distinctly higher volume
- `recovery`: selloff followed by rebound behavior
- `mixed`: assigns different regimes per coin so the same run contains both stronger and weaker assets

Quick examples:

```sh
DRY_RUN=true SIMULATE_DATA=true SIMULATION_REGIME=crash python3 -m src.trading_bot --backtest
SIMULATE_DATA=true SIMULATION_REGIME=uptrend python3 -m scripts.compare_simulation_regimes --coins BTC,ETH,SOL
PI_HOST=local bash scripts/run_compare_simulation_standard_profiles_on_pi.sh
```

The backtest reads `training_data.csv`; the simulation regime applies to the
bot that starts afterward. See the [backtesting warning](USAGE.md#4-backtesting).

### Default lossmaker exclusions

The runtime now merges `EXCLUDED_COINS` with a second default blocklist, `LOSSMAKER_EXCLUDED_COINS`.

- Base stablecoin-like exclusions stay in `EXCLUDED_COINS`
- PnL-driven exclusions currently default to `ZEC,HYPE,TON,BTC,XRP`
- Override `LOSSMAKER_EXCLUDED_COINS` in `.env` if you want to narrow or expand this list explicitly

Example:

```sh
LOSSMAKER_EXCLUDED_COINS=ZEC,HYPE,TON
```

### Dynamic lossmaker throttling

The runtime can temporarily suppress fresh entries for coins that are currently producing repeated timeout-heavy losses in the recent journal window.

- The filter reads the latest `DYNAMIC_LOSSMAKER_WINDOW` journal rows
- A symbol is only suppressed when it has enough recent sells, negative summed PnL, weak win rate, and a high `MAX-HOLD-TIME` share
- Open positions are still analyzed and managed normally; the filter only blocks new candidate selection

Example override:

```sh
DYNAMIC_LOSSMAKER_EXCLUSION_ENABLED=true
DYNAMIC_LOSSMAKER_WINDOW=120
DYNAMIC_LOSSMAKER_MIN_SELLS=4
DYNAMIC_LOSSMAKER_MIN_PNL_LOSS=0.005
COIN_MAX_HOLD_SECONDS=LINK:420,SUI:420,ONDO:420
```

### Portfolio step scaling

The runtime can grow position size and slot count in stages as the portfolio compounds.

- `TRADE_AMOUNT` remains the base amount for the smallest account stage
- `PORTFOLIO_TRADE_AMOUNT_MULTIPLIERS` scales that base amount once the portfolio crosses each milestone
- `MAX_OPEN_TRADES` remains the base slot count
- `PORTFOLIO_MAX_OPEN_TRADES_TIERS` only increases slots in later stages, so the small account stays concentrated

Example override:

```sh
TRADE_AMOUNT=10
PORTFOLIO_TRADE_AMOUNT_MULTIPLIERS=50:1.10,100:1.30,200:1.60,300:1.90,500:2.40,1000:3.20
MAX_OPEN_TRADES=2
PORTFOLIO_MAX_OPEN_TRADES_TIERS=100:3,250:4,500:5,1000:6
```

### Coin-specific uptrend entry tuning

The uptrend entry gate can now be loosened selectively for specific coins instead of changing the global defaults for every symbol.

- `UPTREND_ENTRY_MAX_RSI_BY_COIN` is useful when one coin trends hotter before follow-through appears
- `UPTREND_ENTRY_MIN_BUY_PROBA_BY_COIN` is useful when a coin repeatedly misses entry by a small CatBoost buy-probability margin
- `UPTREND_ENTRY_MAX_SELL_PROBA_BY_COIN` is useful when one coin is consistently blocked by a slightly too-high sell probability

Example override:

```sh
UPTREND_ENTRY_MAX_RSI_BY_COIN=XDC:78,TRX:74
UPTREND_ENTRY_MIN_BUY_PROBA_BY_COIN=TRX:0.150
UPTREND_ENTRY_MAX_SELL_PROBA_BY_COIN=ONDO:0.445
UPTREND_ENTRY_MIN_PROBA_EDGE_BY_COIN=TRX:-0.13,ONDO:-0.055
```

If a coin already passes the uptrend quality gate but still misses fallback entries because the generic fallback RSI ceiling is too low, use `FALLBACK_MAX_RSI_BY_COIN` for a narrow override instead of lowering the global fallback limit.

```sh
FALLBACK_MAX_RSI=68
FALLBACK_MAX_RSI_BY_COIN=TRX:72
```

## Risk guardrails

Optional hard limits — set to `0` to disable:

| Variable | Description |
|---|---|
| `MAX_DAILY_LOSS_PCT` | Block new BUYs once daily drawdown reaches this % |
| `MAX_BUYS_PER_HOUR` | Maximum BUY trades in the last 60 minutes |
| `LOSS_STREAK_PAUSE_THRESHOLD` | Consecutive losses before pausing entries |
| `LOSS_STREAK_PAUSE_SECONDS` | Pause duration after a loss streak |
| `TRADING_RISK_STATE_FILE` | `.trading_risk_state.json` | Durable store for the daily anchor, loss streak, BUY pause, hourly BUY timestamps, and re-entry cooldowns. Restored at startup, rewritten after every close, BUY, and daily anchor. A missing file starts neutral; an unreadable or invalid file blocks new entries, and a file that cannot be written blocks entries in dry-run mode and latches the kill switch in live mode |

Live execution also uses `TRADING_KILL_SWITCH_FILE` (default
`.trading_kill_switch`), `TRADING_EXECUTION_LOCK_FILE` (default
`.trading_execution.lock`), and `LIVE_PORTFOLIO_STATE_FILE` (default
`.live_portfolio_state.json`). An ambiguous order submission/fill outcome
automatically creates the kill-switch marker; inspect Kraken orders, balances,
and local state before an operator removes it. Every live order carries a
deterministic Kraken `cl_ord_id`; after a network timeout the bot looks the
order up instead of resubmitting it, and only an outcome that stays unknown
latches the switch.

For multi-node execution only, set `TRADING_CLUSTER_EXECUTION_LEASE_ENABLED=true`,
`TRADING_CLUSTER_EXECUTION_LEASE_DATABASE_URL`, and a stable
`TRADING_EXECUTION_ACCOUNT_ID`. The bot holds a PostgreSQL session advisory lock
for that account in addition to the local file lock. The single-Pi profile
leaves this disabled and has no PostgreSQL requirement. Every cluster execution
node must use the same reachable database; inability to acquire the lease blocks
startup.

### Scorecard prerequisite for live trading

`DRY_RUN=false` is additionally gated on the weekly Go/No-Go verdict. The bot
refuses to start, and `scripts/manage_model_release.py promote` refuses to
activate a model pair, unless the scorecard marker reports `GO` from a run that
is recent enough. A missing, unreadable, stale, `HOLD`, `NO-GO` or `ERROR`
marker fails closed.

| Variable | Default | Description |
|---|---|---|
| `SCORECARD_GATE_ENABLED` | `true` | Set `false` to trade live without scorecard evidence; the bot logs a warning and the monitoring alert `TradingLiveModeWithoutScorecardGate` fires |
| `SCORECARD_GATE_MAX_AGE_SECONDS` | `691200` | Maximum accepted age of the verdict (eight days: one weekly cycle plus slack) |
| `SCORECARD_STATUS_JSON` | `results/scorecards/latest_status.json` | JSON marker written when `STATUS_JSON_ENABLED=true`; preferred when readable |
| `SCORECARD_STATUS_ENV` | `results/scorecards/latest_status.env` | Env marker the weekly script always writes; used as fallback |

Paths are relative to the working directory, which is the deployment root under
both release profiles, so the defaults need no configuration. Promotion-only
overrides are the CLI flags `--scorecard-status-json`, `--scorecard-status-env`,
`--scorecard-max-age-seconds`, `--allow-maintenance-override` and
`--no-require-go-verdict`. `rollback` is never gated so a known-good release can
always be restored. See [Monitoring](MONITORING.md).

### Scorecard verdict history

`latest_status.json` and `latest_status.env` only ever hold the *last* run, so a
scorecard cycle that aborts early leaves the previous verdict standing, and
nothing records how often the gate recently said `NO-GO`.
`src.scorecard_verdict_history` keeps that history: `scripts/run_weekly_scorecard.sh`
appends one JSONL record per run - verdict, run mode, exit codes, reason,
profile, report file and the scorecard metrics - to an append-only file. The
`(timestamp_utc, verdict, report_file)` triple is the record identity, so
re-running the same report appends nothing. A torn final line is repaired inside
an exclusive lock, and every reader only reads a bounded tail (line limit and
byte cap) and fails closed to "no evidence" instead of raising.

| Variable | Default | Description |
|---|---|---|
| `SCORECARD_VERDICT_HISTORY_FILE` | `results/scorecards/verdict_history.jsonl` | Append-only verdict history. A relative path is resolved against the deployment root; `off`, `none`, `false`, `no` or `disabled` turns it off |

```bash
# Newest verdicts, oldest first
.venv/bin/python -m scripts.scorecard_verdict_history show --limit 5
# Streaks, verdict counts and the age of the newest record
.venv/bin/python -m scripts.scorecard_verdict_history stats
```

`resolve_scorecard_status()` is the shared reader used by both tuning scripts: it
prefers the JSON marker, falls back to the env marker, and falls back again to
the newest history record, tagging the result with the `source` it used. A
deployment that only has the env marker - the default Raspberry Pi profile - is
therefore no longer treated as having no evidence. The live-trading gate in
`src/scorecard_gate.py` deliberately keeps reading only the two markers: after a
scorecard cycle aborts early, the stale `GO` marker is not evidence about the
current record, so the gate fails closed instead of falling back to history.

### Weekly tuning cycle evidence chain

The tuning cycle may only change parameters when the scorecard evidence supports
it, so it reads the same three sources in the same order. `--apply` is refused
(`exit 3`, `apply_effective: false`, the deployment `.env` untouched) when no
source is usable, when the verdict is not `GO`, when the newest verdict is older
than the age limit, or when a recent verdict was `NO-GO`.

| Variable | Default | Description |
|---|---|---|
| `TUNING_STATUS_JSON` | `results/scorecards/latest_status.json` | JSON verdict marker; absent on the default Pi profile |
| `TUNING_STATUS_ENV` | `results/scorecards/latest_status.env` | Env verdict marker the scorecard always writes |
| `TUNING_VERDICT_HISTORY_FILE` | `$SCORECARD_VERDICT_HISTORY_FILE`, else `results/scorecards/verdict_history.jsonl` | History fallback and the source of the recent `NO-GO` guard; `off` disables it and reverts the guard to scraping report filenames |
| `TUNING_MAX_VERDICT_AGE_SECONDS` | `777600` | Nine days: one missed weekly scorecard plus slack. `0` disables the age check |
| `TUNING_STATUS_FILE` | `results/tuning/latest_status.env` | Scheduled-task marker for this unit, exported by the health exporter |
| `TUNING_CYCLE_TIMEOUT_SECONDS` | `1800` | `timeout` wrapper around the cycle so a hung run still leaves its marker; `0` disables |

The wrapper exits with the cycle's code: `0` ran, `3` the safety guard refused
the apply, anything else a failed step. `deploy/tuning-cycle.service` accepts
`0` and `3` as clean unit exits and records the code in the task marker, which
is what `TradingScheduledTaskFailed` and the new `TradingTuningCycleApplyBlocked`
alert read. See [Monitoring](MONITORING.md).

## Fill reconciliation against the exchange

The journal records what the bot *submitted*. Reconciliation asks what the
exchange *did*: it reads the recent live rows of the journal, fetches the
account order history, and reports every order that is missing there or whose
filled quantity or fee differs. It is read-only — it never places or cancels an
order, never rewrites the journal, and never touches the portfolio state — so it
complements the risk guardrails instead of interacting with them.

On the single-node Raspberry Pi profile the pass runs twice: once from the bot
at startup, inside the execution-owner lock and after the scorecard gate, and
once per hour from `fill-reconciliation.timer`. A paper deployment (`DRY_RUN=true`)
is skipped by design because its journal has no exchange side, and the manual
command refuses unless `--allow-dry-run` is passed deliberately.

| Variable | Default | Description |
|---|---|---|
| `TRADING_FILL_RECONCILIATION_ENABLED` | `DRY_RUN=false` | Automatic pass at bot startup. The manual CLI is always enabled when an operator runs it |
| `TRADING_FILL_RECONCILIATION_LOOKBACK_DAYS` | `30` | Only journal rows newer than this are checked |
| `TRADING_FILL_RECONCILIATION_PAGE_LIMIT` | `100` | Orders requested per exchange history page (Kraken's own page size) |
| `TRADING_FILL_RECONCILIATION_SYMBOLS` | derived from the journal | Explicit unified-symbol scope, comma-separated; caps the number of private history calls |
| `TRADING_FILL_RECONCILIATION_REPORT` | unset | `KEY=value` marker path; a `.json` report is written next to it |
| `TRADING_JOURNAL_FILE` | `$PERFORMANCE_LOG_FILE` | Journal to read; the reconciliation falls back to the bot's own journal variable |
| `TRADING_JOURNAL_READ_LIMIT` | `4194304` | Bytes of journal tail read, so a multi-year journal stays cheap |

Missing credentials, an unreadable journal, or a failing exchange call make the
pass *incomplete* rather than clean: nothing is verified, the reason is recorded
in the marker, and the `TradingFillReconciliationIncomplete` alert fires. A
quantity or fee difference, or an order the exchange does not know, is a
mismatch and fires `TradingFillReconciliationMismatch`. The bot never stops
trading because of a reconciliation result — the finding is logged and alerted,
and the operator decides.

```bash
# Manual pass, human-readable summary; exit 0 clean, 1 mismatch, 2 incomplete
.venv/bin/python -m scripts.reconcile_fills
.venv/bin/python -m scripts.reconcile_fills --lookback-days 7 --json
.venv/bin/python -m scripts.reconcile_fills --symbol ETH/EUR \
    --report results/fill_reconciliation/latest_status.env
```

## Operational health exporter

`scripts/export_operational_health.py` runs from the 30-second
`trading-operational-health.timer` and reads `/etc/trading-health.env`, never
the bot's credential-bearing `.env`. These settings are separate from the bot
settings above.

| Variable | Default | Description |
|---|---|---|
| `TRADING_DATA_DIR` | `.` | Base directory used for the defaults below |
| `TRADING_KILL_SWITCH_FILE` | `.trading_kill_switch` | Kill-switch marker whose existence is exported |
| `TRADING_JOURNAL_FILE` | `$TRADING_DATA_DIR/trade_journal.csv` | Journal probed for `trading_journal_age_seconds` |
| `TRADING_BOT_LOG_FILE` | `$TRADING_DATA_DIR/trading_bot.log` | Bot log probed for `trading_bot_log_age_seconds`; the systemd unit points at `logs/bot.log` |
| `TRADING_MARKET_DATA_DIR` | unset | Market recorder directory. Empty means the host does not run the recorder, so no market metric is published instead of a false absence |
| `TRADING_LOG_TIMEZONE` | `UTC` | Zone of the naive `asctime` stamps in the bot log; keep it equal to `--log-timezone` of the daily report |
| `RESEARCH_SIGNAL_PATH` | `$TRADING_DATA_DIR/research_signal_latest.json` | Research/news feed probed for presence and age |
| `HAILO_HEALTH_URL` | unset | Optional Hailo health endpoint; loopback or trusted private address only |
| `TRADING_HEALTH_METRICS_FILE` | `results/scorecards/textfile/trading_health.prom` | Textfile-collector fragment written atomically |
| `TRADING_DEPLOYMENT_ENV_FILE` | `/opt/trading_2/.env` | Deployment `.env` whose `DRY_RUN` drives `trading_scorecard_gate_live_enabled`; only the run mode is read |
| `TRADING_FILL_RECONCILIATION_REPORT` | unset | Reconciliation marker to export as presence/complete/mismatch/age gauges; unset means this host publishes none |

Each staleness probe reads only a bounded tail of its file and reports
`*_present 0` with no age when the source is missing or unreadable, so a probe
failure never crashes the timer and "not collected" stays distinguishable from
"stale". See [Monitoring](MONITORING.md) for the matching alert rules.
