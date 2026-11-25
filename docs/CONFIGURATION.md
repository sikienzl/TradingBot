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
| `TICKER_BATCH_SIZE` | `80` | Symbols per `fetch_tickers` request (smaller is more stable) |
| `TICKER_FETCH_RETRIES` | `2` | Retries per ticker batch on exchange/network errors |
| `USE_TABULAR_MODEL` | `false` | Enable tabular-model signal filter |
| `TABULAR_MODEL_BACKEND` | `catboost` | Tabular backend: local `catboost` or remote `hailo` |
| `HAILO_SERVICE_URL` | `http://127.0.0.1:8001` | Hailo inference endpoint; set the host address when using a separate server |
| `HAILO_API_TOKEN` | unset | Required bearer token when Hailo is enabled |
| `HAILO_TIMEOUT_SECONDS` | `3` | Maximum remote inference request time |
| `TABULAR_MIN_CONFIDENCE` | `0.45` | Minimum tabular-model confidence required for non-HOLD signal |
| `UPTREND_ENTRY_MAX_RSI_BY_COIN` | `XDC:78,TRX:74` | Coin-specific RSI override for uptrend entry gating |
| `UPTREND_ENTRY_MIN_BUY_PROBA_BY_COIN` | `TRX:0.150` | Coin-specific CatBoost buy-probability override for uptrend entry gating |
| `UPTREND_ENTRY_MAX_SELL_PROBA_BY_COIN` | `ONDO:0.445` | Coin-specific CatBoost sell-probability override for uptrend entry gating |
| `UPTREND_ENTRY_MIN_PROBA_EDGE_BY_COIN` | `TRX:-0.13,ONDO:-0.055` | Coin-specific probability-edge override for uptrend entry gating |
| `TABULAR_RESEARCH_SIGNAL_PATH` | `./data/research_signal_latest.json` | Latest AutoResearch JSON used as model input features |
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

Live execution also uses `TRADING_KILL_SWITCH_FILE` (default
`.trading_kill_switch`), `TRADING_EXECUTION_LOCK_FILE` (default
`.trading_execution.lock`), and `LIVE_PORTFOLIO_STATE_FILE` (default
`.live_portfolio_state.json`). An ambiguous order submission/fill outcome
automatically creates the kill-switch marker; inspect Kraken orders, balances,
and local state before an operator removes it.

For multi-node execution only, set `TRADING_CLUSTER_EXECUTION_LEASE_ENABLED=true`,
`TRADING_CLUSTER_EXECUTION_LEASE_DATABASE_URL`, and a stable
`TRADING_EXECUTION_ACCOUNT_ID`. The bot holds a PostgreSQL session advisory lock
for that account in addition to the local file lock. The single-Pi profile
leaves this disabled and has no PostgreSQL requirement. Every cluster execution
node must use the same reachable database; inability to acquire the lease blocks
startup.
