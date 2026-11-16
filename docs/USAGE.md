# Setup and usage

## Setup

**Requirements:** Python 3.12+ and a virtual environment.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> On Debian Bookworm (Raspberry Pi), `libta-lib-dev` is usually unavailable.
> Install dependencies with `pip install -r requirements-pi.txt` first; if TA-Lib fails to build,
> compile the TA-Lib C library from source and retry.

The current low-RAM Raspberry Pi runtime profile in this repository has been tested on a Raspberry Pi 3B+ with 1 GB RAM.

Copy and edit the example config:
```sh
cp .env.example .env        # default / dry-run profile
# or
cp .env.live.example .env   # conservative live profile
# or
cp .env.small_account.example .env   # conservative small-account profile
```

Optional market-signal bridge overlay for servers:
```sh
cp .env.live.example .env
cat .env.autoresearch.example >> .env
```

Optional market-signal producer overlay for external command execution (strict mode):
```sh
cp .env.live.example .env
cat .env.autoresearch.active.example >> .env
```

The weekly scorecard script does not read `.env` automatically; see the
[AutoResearch guide](AUTORESEARCH.md#weekly-automation-with-autoresearch) for
environment-based examples.

## CI / Quality Gates

GitHub Actions pipeline in `.github/workflows/ci.yml` runs on push and pull requests with Python 3.12 and 3.13.

- Syntax gate: `py_compile` on tracked Python files
- Lint gate: `ruff check` with annotations in GitHub Checks
- Test gate: `pytest` with JUnit XML (`pytest-report.xml`) and artifact upload
- Explicit gate steps fail the job when lint/tests are not green
- Deployment check: rejects personal service accounts and hard-coded host IPs

Enable the matching local pre-commit hook once per clone:

```sh
git config core.hooksPath .githooks
```

Weekly decision automation is separated in `.github/workflows/weekly-scorecard.yml`.

An optional read-only paper dashboard is documented in
[SERVER_README.md](../SERVER_README.md). It requires HTTPS, HTTP Basic
authentication, and an operator-configured private CIDR allowlist; network
ranges and credentials belong only in the private deployment environment.
An optional generic Ansible starter for deploying that dashboard to a
pre-provisioned Debian/Ubuntu host is included under [ansible/](../ansible/);
it intentionally contains no site-specific inventory or secrets.
A separate generic CM5/K3s cluster starter is available under
[cluster-starter/](../cluster-starter/). Its manually triggered GitHub Actions
workflow validates and publishes versioned ZIP pre-releases; it does not
provision hardware or deploy to hosts.

## Project structure

| File | Purpose |
|---|---|
| `src/trading_bot.py` | Main bot loop, backtester, risk guardrails |
| `src/get_data.py` | Fetch OHLCV data from Kraken |
| `src/data_preperation.py` | Feature engineering, training data export |
| `src/predict.py` | LLM ensemble inference (optional) |
| `src/predict_catboost.py` | CatBoost tabular inference |
| `src/train_trading_model.py` | Fine-tune the LLM (GPU required) |
| `src/train_catboost_model.py` | Train/walk-forward the CatBoost model |
| `src/go_no_go_scorecard.py` | Weekly Go/No-Go decision gate |
| `src/analyze_trade_journal.py` | Trade journal statistics |
| `src/get_account_balance.py` | Fetch live Kraken balance |

## Usage

### Typed API with FastAPI

The repository now includes a typed API layer based on FastAPI and Pydantic.

Start it locally:

```sh
uvicorn src.fastapi_app:app --reload
```

Available endpoints:

- `GET /health` for a simple readiness check
- `GET /research-signal` to read and validate the current research JSON
- `POST /research-signal/normalize` to normalize arbitrary research payloads into the fixed feature schema
- `POST /predict/catboost` to run a typed single-row CatBoost inference via a strict, validated feature schema
- `GET /scorecard` to calculate the weekly Go/No-Go scorecard as a typed JSON response

Swagger UI is available at `http://127.0.0.1:8000/docs`.

### 1. Data collection
```sh
python3 -m src.get_data
python3 -m src.data_preperation
```
Outputs `full_crypto_data.csv` and `training_data.csv`.

### 2. Model training (optional)

Train the CatBoost model:
```sh
python3 -m src.train_catboost_model
```
Outputs a ready-to-use model in `model/catboost_trading_model/`.

The training pipeline sorts timestamped data chronologically, purges training
rows whose forward label window overlaps the validation boundary, and runs an
expanding-window walk-forward evaluation when there is enough data. The
validation set is also used for model selection and confidence-threshold tuning,
so its metrics are not a final untouched test. Provide causally computed,
time-aligned input features and assess the result on a separate fresh period;
these safeguards do not eliminate every source of lookahead bias.

Fine-tune the LLM (requires a GPU with ≥ 16 GB VRAM):
```sh
python3 -m src.train_trading_model
```
Outputs a LoRA adapter in `model/fine_tuned_trading_model/`.

Train CatBoost using historical, timestamp-aligned AutoResearch features (optional):
```sh
python3 -m src.train_catboost_model
```
If the training CSV contains all six `research_*` feature columns, they are used as supplied. The latest research JSON is for inference and is deliberately not copied across historical rows, which would leak future information into training.

### 3. Dry-run / live trading
```sh
python3 -m src.trading_bot
```
- Docker's `SIGTERM` is handled through the bot's normal shutdown path so its
  analytics writer can close. Allow a sufficient grace period for
  `docker stop --time 30`; forced `SIGKILL` cannot run shutdown cleanup.
- Set `DRY_RUN=true` (default) to simulate trades without spending real funds.
- Set `SIMULATE_DATA=true` to use synthetic market data. For an offline run,
  also disable network-dependent features, including
  `SIMULATION_INITIAL_BALANCE_FROM_KRAKEN` and the external AI copilot.
  When Kraken balance initialization is enabled and no saved portfolio exists,
  startup can fetch the account balance.
- Set `SIMULATION_REGIME=uptrend|downtrend|sideways|crash|recovery|neutral|mixed` to shape the offline market regime.
- Trades are recorded to `trade_journal.csv`.

#### Public Kraken paper-trade recovery

For isolated public Kraken paper mode (`DRY_RUN=true`, `SIMULATE_DATA=false`,
`SIMULATION_INITIAL_BALANCE_FROM_KRAKEN=true`), a previously initialized
portfolio, no Kraken credentials, and `PERFORMANCE_LOG_ENABLED=true` are
required. `.env.paper_kraken.example` does not enable this mode by itself:
set `SIMULATION_INITIAL_BALANCE_FROM_KRAKEN=true` in a private configuration
and leave the Kraken credentials empty. Initialize a separate portfolio first
and use its matching state and journal paths. See the
[public paper runbook](../SERVER_README.md#separate-paper-run-with-public-kraken-market-data)
for initialization and isolation.

Run **only one writer** for a portfolio and journal, including during recovery.
There is no interprocess lock: another bot or process must not modify either
file. Before a paper trade is committed, the bot durably writes an intent next
to the portfolio as `.portfolio_state.json.trade-transaction.json`. The intent
contains the resulting portfolio, the exact journal row, and a hash of the
preceding journal. The bot then writes and syncs the journal, atomically saves
and syncs the portfolio, and removes the intent. On restart, a pending intent
is replayed before trading; a complete or partial row is handled without
duplicating the trade.

If persistence or replay fails, stop trading and restart with the **same**
portfolio and journal paths. A missing, altered, or incompatible journal, or a
damaged intent, causes recovery to fail closed. Keep all files intact for
investigation; do not delete the intent or edit the journal to force a restart.
If recovery cannot complete, reconcile the portfolio and journal from trusted
backups before resuming.

New entries exclude `EURC` by default through `EXCLUDED_COINS`; overriding
that list is configurable and does not liquidate existing holdings.
`MIN_ENTRY_ATR_PCT=0.012` requires ATR(14)/price to be at least 1.2% for a
new-entry candidate. This is a volatility proxy for screening, not an expected
or guaranteed return; set it to `0` to disable the guard. If ATR data is
unavailable, the candidate is withheld.

With `TABULAR_BLOCK_ON_SELL=true`, a tabular prediction vetoes a new entry only
when its decision is SELL and both its sell probability and confidence meet
`TABULAR_SELL_BLOCK_MIN_PROBA` (0.50 in the paper example). A HOLD/HALTEN
decision or a low-confidence prediction does not trigger the veto. To use
Hailo for this filter, set `TABULAR_MODEL_BACKEND` to `hailo` and configure
the Hailo service settings; otherwise the configured CatBoost backend remains
in use.

This protocol assumes filesystem support for durable file and directory
`fsync` and atomic replacement. It cannot repair inconsistencies predating the
protocol, and it does not transactionally persist analytics-database writes or
in-memory risk and performance counters. It hashes the entire journal for
each paper trade, so cost grows with journal size. Other trading modes do not
use this transaction protocol.

Simulation helper scripts:
```sh
bash scripts/start_sim_bot.sh
bash scripts/stop_sim_bot.sh
```
Defaults:
- Uses `.env.simulation.example`
- Writes logs to `logs/sim_bot.log`
- Stores PID, process start time and Python path in `logs/sim_bot.pid`

These helpers are for Linux with `/proc`. Safe stopping also requires Linux
5.3+ with pidfd support and Python 3.9+ with `os.pidfd_open` and
`signal.pidfd_send_signal`; otherwise the stop helper refuses to signal the
process. Legacy PID files are rejected: inspect the recorded process and file
before removing or migrating one. The start helper checks that the process
survives its first second, not that the bot is fully ready; inspect the log.
Use only one start/stop operator for a given PID file.

Simulation regime comparison helper:
```sh
python3 scripts/compare_simulation_regimes.py --coins BTC,ETH,SOL,CHZ,VVV
```
This runs the bot's own market analysis against multiple synthetic regimes and prints the top recommendations side by side.

Pi wrappers for the comparison helper:
```sh
PI_HOST=local bash scripts/run_compare_simulation_regimes_on_pi.sh --coins BTC,ETH,SOL --regimes uptrend,crash,mixed --top 3
PI_HOST=local bash scripts/run_compare_simulation_standard_profiles_on_pi.sh
```
The first wrapper runs the comparison on the Raspberry Pi with the bot's service
user and virtualenv. The second wrapper executes the common default profile set
(`uptrend,crash,mixed`) for the standard coin basket. The examples run on the Pi;
from another host, set `PI_HOST` or `TRADING_HOST` to the SSH destination instead.

### 4. Backtesting

**Important:** `--backtest` reads `training_data.csv` and then continues into
the normal bot startup; it does not exit after the report. With live mode
configured, this can place real orders. Use a separate dry-run/simulation
configuration and stop the process after the report if you do not want the bot
to continue.

```sh
DRY_RUN=true SIMULATE_DATA=true python3 -m src.trading_bot --backtest
```
The example backtester evaluates each signal on the next bar, processes bars in
timestamp order, marks holdings using the latest price for each coin, and
estimates exchange fees and slippage (defaults: 0.2% and 0.1% per side).
Override the assumptions with `--backtest-fee-rate` and
`--backtest-slippage`. These results are only as representative as the input
data and strategy; they are not a live-performance forecast.

For an offline replay of the bot's shared rule-based recommendations against
historical OHLCV candles, collect data and run the chronological training /
holdout report:

```sh
python3 -m src.get_data
python3 -m src.historical_backtest --file full_crypto_data.csv \
  --initial-cash 19.50 --base-currency EUR
```

The collector currently retrieves up to 720 hourly candles per active EUR
market. The replay uses the same rule recommendation helper as live analysis,
executes on the next candle's open, and models fees, slippage, strict BUY
entries, the recent-PnL guard, fixed-percentage stop-loss/take-profit, and
maximum hold time. It resets the portfolio at the chronological holdout
boundary while retaining earlier candles for indicator warm-up; the holdout
PnL guard is seeded from training closes, not training positions. The report
shows candle freshness and warns for data older than 24 hours. Time exits are
resolved only at candle boundaries, so hourly bars cannot model the bot's
120-second code-default hold time precisely (`.env.example` sets 300 seconds).
It does not
model journal-driven coin exclusions, fallback/force-fill entries, optional
ML models, other runtime guards, ATR/trailing/partial exits, portfolio scaling
tiers, or exchange-specific fills. Small samples are
descriptive only; this replay is not a profitability forecast or a live-trading
recommendation.

For finer, continuing public-market observations, start the read-only recorder
in a dedicated data directory:

```sh
python3 -m src.public_market_recorder \
  --data-dir data/market_observations \
  --symbols ADA/EUR,ETH/EUR,LINK/EUR,NEAR/EUR,SOL/EUR,SUI/EUR
```

The recorder uses Kraken's public ticker and OHLCV endpoints only; it does not
load `.env`, use API credentials, read portfolio state, or submit orders. It
backfills up to 720 one-minute candles per market, then appends newly completed
candles and bid/ask/last ticker snapshots every 10 seconds. The output
directory holds `tickers.csv`, `candles_1m.csv`, and a `metadata.json` file
that records the schema and sampling settings. Existing data is never
overwritten; a configuration/schema mismatch or a second writer fails
explicitly. Run the recorder continuously for at least 30 days before using
the data for a forward evaluation. Ticker snapshots and OHLCV remain
observations of public quotes/candles, not proof of executable fills. The
recorder's host deployment and isolated replay procedure are in the
[server runbook](../SERVER_README.md).

Trade-journal confidence-threshold analysis selects a candidate on the first
70% of chronological trades and reports its result on the final 30% holdout.
The report also warns when journal events are older than 24 hours, since stale
trade metrics should not be mistaken for current bot activity.

Dry-run execution costs are opt-in. Set `DRY_RUN_COSTS_ENABLED=true` to apply
`DRY_RUN_FEE_RATE=0.002` and `DRY_RUN_SLIPPAGE_RATE=0.001` per side; both rates
are fractions, not percentages. Slippage is a combined execution-cost
allowance, not a separately measured bid-ask spread. Buy fees are included in
the requested budget; sell proceeds are reduced by the sell fee. Simulated
cash, position basis and realized PnL include these assumptions, including
partial exits. Live order execution is unaffected. Existing positions retain
their recorded entry basis: enabling costs cannot reconstruct historical
entry fees, so only later execution costs are applied. Use a separate new
simulation and journal when comparing gross legacy and cost-aware results.

The CSV journal labels the execution mode and records reference prices,
fees and slippage for new trades. It also records gross reference-price PnL
and net PnL separately when both entry and exit references are available.
Legacy records are not retroactively made
net of costs; the existing PostgreSQL trade schema does not record these
additional cost fields. Cost assumptions are estimates, not actual exchange
fills or fees.

Win rate counts strictly positive sell-event PnL divided by all sell events.
Zero-PnL events are reported separately as break-even; partial sells each count
as an event rather than a fully closed position. These metrics describe the
journal's recorded PnL, which can contain historical gross results. A win
rate by itself does not establish profitability.

The recent PnL guard reports its state, latest close and journal freshness
even when inactive. Staleness is diagnostic and does not disable the guard.
`MAX_HOLD_SECONDS` is checked during the trading loop, not at a precise
deadline: market data, sequential analysis, ATR retrieval, retries and the
configured sleep all affect the delay. The bot logs iteration work before
position monitoring and, when a limit is reached, the effective per-coin
limit, overdue time and ATR-fetch duration. Missing market data or a failed
sell can delay an exit further. These diagnostics do not change exit priority
or relax any guard.

Each process gets a generated `run_id` (override with `TRADING_RUN_ID`) and a
SHA-256 fingerprint of non-secret bot settings. These are stored on CSV and
Postgres trade events to keep runs/configurations separate; credentials and
URLs are excluded from the fingerprint. Existing CSV journals are extended
atomically and historical rows are marked `legacy`. Closed trades also record
observed `mfe_pct` (maximum favorable excursion) and `mae_pct` (maximum adverse
excursion) from prices seen by the bot; these are sampled observations, not
intrabar extremes.

### 5. Prediction (standalone)
```sh
python3 -m src.predict # LLM + rule-based ensemble
```
CatBoost inference is available through `CatBoostTradingPredictor` in
`src.predict_catboost` and is integrated into the bot when
`USE_TABULAR_MODEL=true`; the module does not currently provide a standalone
command-line entry point.

### Optional Hailo-8 inference

The bot can use a separate Hailo-8 inference service without moving trading or
exchange credentials off the bot host. Configure the bot and Hailo service host
addresses for your own network.
CatBoost remains the default backend; no Hailo model is enabled by default.

The exporter trains a small tabular MLP and creates ONNX plus a strict
feature/normalization metadata contract:

```sh
pip install '.[hailo-export]'
python3 -m src.train_hailo_model --data-path training_data.csv
```

This only creates an ONNX model, not a Hailo HEF. Hailo Dataflow Compiler (DFC)
is not included on the Hailo runtime host; compile and validate the ONNX model
in a compatible DFC environment before configuring the inference service. The
existing image-classification HEFs on the cluster are not trading models.
Hailo must be explicitly enabled on the bot with
`USE_TABULAR_MODEL=true`, `TABULAR_MODEL_BACKEND=hailo`, `HAILO_SERVICE_URL`,
and a strong `HAILO_API_TOKEN`. A service
failure blocks entries for the affected coin; it does not fall back to
rules-only buying. Keep the token and service on a trusted, firewalled network:
the example HTTP endpoint does not encrypt traffic. The Hailo option is
preparation only and is not a profitability claim or live-trading
recommendation.

### Offline Hailo research experiments

`scripts/run_hailo_experiment.py` provides a first, agent-independent
experiment interface inspired by autoresearch. It does not run an AI agent:
the caller supplies a JSON proposal with exactly these bounded parameters:

```json
{"epochs": 100, "patience": 15, "learning_rate": 0.001}
```

`epochs` must be an integer from 1 to 300, `patience` from 1 to 50 and no
larger than `epochs`, and `learning_rate` from 0.00001 to 0.01. The model
architecture, features, labeling and evaluation are fixed. On a development
host with the `hailo-export` dependencies installed, use a chronological CSV
from `scripts/collect_hailo_training.py` and a new output directory:

```sh
python3 scripts/run_hailo_experiment.py \
  --data data/kraken_hailo_training.csv \
  --proposal proposal.json \
  --output-dir results/hailo-experiment-001
```

The output directory's parent must already exist; an existing output directory
is never overwritten. The runner trains both the fixed default baseline and
the proposed candidate on the same development data. It evaluates each ONNX
model's raw class predictions with three-class macro-F1 on a later holdout
which is excluded from training, early stopping, normalization and
calibration. `result.json` records metrics, parameters and data hashes; each
model directory contains its ONNX, metadata and calibration files. Protect
these potentially sensitive datasets and artifacts outside Git. All raw Hailo
input features must be present and finite; the return and volatility features
are derived from close prices by the training pipeline.

This is **offline research only**: no agent loop, HEF compilation, hardware
comparison, automatic shadow deployment or live promotion is implemented.
Repeating experiments on the same holdout can overfit it; use a fresh,
untouched time period for final assessment. Input features must be computed
causally. Macro-F1 does not measure trading profitability or risk. Before any
separate shadow test, compile and validate the model as described in the
server guide; never replace the existing shadow service or its state.

## Tests

```sh
.venv/bin/python -m pytest tests/ -v
```

## Troubleshooting

- TA-Lib import/build issues:
  - Debian Bookworm (Raspberry Pi): `libta-lib-dev` is typically unavailable. Build TA-Lib C from source if `pip install TA-Lib` fails.
- CatBoost runtime issues on Linux CI:
  - Ensure `libgomp1` is installed on the runner
- CI import errors like `ModuleNotFoundError` for project modules:
  - Run tests with `PYTHONPATH` set to the repository root
- Reproduce CI test command locally:
  - `.venv/bin/python -m pytest tests/ -v --maxfail=1 --junitxml=pytest-report.xml`

## Public Release Safety

Before changing repository visibility from private to public:

- Rotate all exchange API keys used during development.
- Confirm `.env` remains untracked and ignored.
- Run a secret scan over tracked files.
- Verify local test env files are not staged.
- Use least-privilege API permissions (no withdrawals).

See `SECURITY.md` for the full policy and reporting guidance.

## Community and Collaboration

- Code of Conduct: `CODE_OF_CONDUCT.md`
- Contributing Guide: `CONTRIBUTING.md`
- Issue templates: `.github/ISSUE_TEMPLATE/`
- Pull request template: `.github/pull_request_template.md`

## Disclaimer & Risk Warning

⚠️ USE THIS TRADING BOT AT YOUR OWN RISK!

This trading bot is provided "as is" without any warranties, express or implied. The author(s) and contributors are not liable for any financial losses, damages, or other negative consequences resulting from the use of this software.
Key Risks to Consider:

- Market Volatility: Cryptocurrency and financial markets are highly unpredictable. Past performance does not guarantee future results.
- Technical Failures: Bugs, connectivity issues, or exchange API changes may lead to unexpected behavior.
- Financial Loss: Trading involves risk, and you may lose part or all of your invested capital.
- No Guarantees: The bot’s strategies may not always be profitable. Always test thoroughly in a simulated environment before using real funds.

Important Notes:

- Backtest & Paper Trade First: Never deploy the bot with real money without extensive testing.
- Monitor Performance: Regularly check the bot’s activity to ensure it behaves as expected.
- Legal & Tax Compliance: Ensure compliance with local regulations regarding automated trading and taxation.

By using this bot, you acknowledge and accept full responsibility for all outcomes, including potential losses. The author(s) disclaim all liability for any damages arising from its use.

Trade responsibly!
