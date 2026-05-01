# Crypto Trading Bot

[![CI](https://github.com/sikienzl/TradingBot/actions/workflows/ci.yml/badge.svg)](https://github.com/sikienzl/TradingBot/actions/workflows/ci.yml)

A modular pipeline for crypto trading: data collection, feature engineering, CatBoost/LLM ensemble prediction, and live/simulated trading via the Kraken exchange. Designed for research, simulation, and (optionally) live trading.

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

Copy and edit the example config:
```sh
cp .env.example .env        # default / dry-run profile
# or
cp .env.live.example .env   # conservative live profile
```

Optional AutoResearch overlay for servers:
```sh
cp .env.live.example .env
cat .env.autoresearch.example >> .env
```

Optional AutoResearch overlay for real command execution (strict mode):
```sh
cp .env.live.example .env
cat .env.autoresearch.active.example >> .env
```

## Quickstart (60 seconds)

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 get_data.py
python3 data_preperation.py
python3 trading_bot.py --backtest
```

## CI / Quality Gates

GitHub Actions pipeline in `.github/workflows/ci.yml` runs on push and pull requests with Python 3.12 and 3.13.

- Syntax gate: `py_compile` on tracked Python files
- Lint gate: `ruff check` with annotations in GitHub Checks
- Test gate: `pytest` with JUnit XML (`pytest-report.xml`) and artifact upload
- Explicit gate steps fail the job when lint/tests are not green

Weekly decision automation is separated in `.github/workflows/weekly-scorecard.yml`.

## Project structure

| File | Purpose |
|---|---|
| `trading_bot.py` | Main bot loop, backtester, risk guardrails |
| `get_data.py` | Fetch OHLCV data from Kraken |
| `data_preperation.py` | Feature engineering, training data export |
| `predict.py` | LLM ensemble inference (optional) |
| `predict_catboost.py` | CatBoost tabular inference |
| `train_trading_model.py` | Fine-tune the LLM (GPU required) |
| `train_catboost_model.py` | Train/walk-forward the CatBoost model |
| `go_no_go_scorecard.py` | Weekly Go/No-Go decision gate |
| `analyze_trade_journal.py` | Trade journal statistics |
| `get_account_balance.py` | Fetch live Kraken balance |

## Usage

### 1. Data collection
```sh
python3 get_data.py
python3 data_preperation.py
```
Outputs `full_crypto_data.csv` and `training_data.csv`.

### 2. Model training (optional)

Train the CatBoost model:
```sh
python3 train_catboost_model.py
```
Outputs a ready-to-use model in `model/catboost_trading_model/`.

Fine-tune the LLM (requires a GPU with ≥ 16 GB VRAM):
```sh
python3 train_trading_model.py
```
Outputs a LoRA adapter in `model/fine_tuned_trading_model/`.

Train CatBoost with AutoResearch features (optional):
```sh
python3 train_catboost_model.py --research-signal-path data/research_signal_latest.json
```
If omitted, the model uses neutral research defaults.

### 3. Dry-run / live trading
```sh
python3 trading_bot.py
```
- Set `DRY_RUN=true` (default) to simulate trades without spending real funds.
- Set `SIMULATE_DATA=true` to run fully offline without an exchange connection.
- Trades are recorded to `trade_journal.csv`.

Simulation helper scripts:
```sh
bash scripts/start_sim_bot.sh
bash scripts/stop_sim_bot.sh
```
Defaults:
- Uses `.env.simulation.example`
- Writes logs to `logs/sim_bot.log`
- Stores PID in `logs/sim_bot.pid`

### 4. Backtesting
```sh
python3 trading_bot.py --backtest
```
Prints a summary report (total return, max drawdown, number of trades).

### 5. Prediction (standalone)
```sh
python3 predict.py          # LLM + rule-based ensemble
python3 predict_catboost.py # CatBoost only
```

## Configuration

All parameters are set via `.env` (see `.env.example` for the full list). Key variables:

| Variable | Default | Description |
|---|---|---|
| `EXCHANGE_NAME` | `kraken` | Exchange (ccxt identifier) |
| `BASE_CURRENCY` | `EUR` | Quote currency |
| `DRY_RUN` | `true` | Simulate trades (no real orders) |
| `SIMULATE_DATA` | `false` | Use generated data instead of live API |
| `TRADE_AMOUNT` | `10` | EUR per trade |
| `MAX_OPEN_TRADES` | `4` | Maximum concurrent positions |
| `OHLCV_TIMEFRAME` | `1h` | Candle interval |
| `TICKER_BATCH_SIZE` | `80` | Symbols per `fetch_tickers` request (smaller is more stable) |
| `TICKER_FETCH_RETRIES` | `2` | Retries per ticker batch on exchange/network errors |
| `USE_TABULAR_MODEL` | `true` | Enable CatBoost signal filter |
| `TABULAR_MIN_CONFIDENCE` | `0.45` | Minimum CatBoost confidence required for non-HOLD signal |
| `TABULAR_RESEARCH_SIGNAL_PATH` | `./data/research_signal_latest.json` | Latest AutoResearch JSON used as model input features |
| `AUTO_TUNE_TABULAR_CONFIDENCE` | `true` | Auto-adjust CatBoost threshold |
| `USE_ML_MODEL` | `false` | Enable LLM signal (GPU recommended) |

## AutoResearch -> AI model features

The CatBoost model can consume a normalized AutoResearch signal vector at training and inference time.

Expected JSON file (example path: `data/research_signal_latest.json`):

```json
{
  "timestamp_utc": "2026-04-18T09:00:00Z",
  "sentiment_score": 0.28,
  "confidence": 0.73,
  "risk_score": 0.31,
  "market_regime": "bull",
  "citations": [
    "https://example.com/source-1",
    "https://example.com/source-2"
  ]
}
```

Mapped model features:

- `research_sentiment_score` in `[-1, 1]`
- `research_confidence` in `[0, 1]`
- `research_risk_score` in `[0, 1]`
- `research_regime_bull` / `research_regime_bear` / `research_regime_sideways`

Implementation points:

- Shared normalization logic: `research_signal.py`
- Training integration: `train_catboost_model.py`
- Live inference integration: `predict_catboost.py` and `trading_bot.py`

### AutoResearch bridge script

Use the bridge to generate/refresh the canonical signal file that the model consumes:

```sh
python3 scripts/update_autoresearch_signal.py \
  --command "<your-autoresearch-command>" \
  --source data/research_signal_raw.json \
  --output data/research_signal_latest.json
```

If your AutoResearch command writes directly to the output path, you can omit `--source`.

The bridge:

- Runs your AutoResearch command (optional)
- Validates payload age using `timestamp_utc`
- Normalizes values to model-safe ranges
- Writes canonical JSON used by training and inference

Fallback mode (recommended when repo/path is unknown):

```sh
python3 scripts/update_autoresearch_signal.py \
  --output data/research_signal_latest.json \
  --fallback-neutral
```

This writes a neutral signal (`sentiment=0`, `confidence=0`, `regime=sideways`) when AutoResearch is unavailable.

### Weekly automation with AutoResearch

`scripts/run_weekly_scorecard.sh` can run the bridge before scoring.

Relevant environment variables:

- `AUTORESEARCH_ENABLED=true|false` (default `false`)
- `AUTORESEARCH_REQUIRED=true|false` fail closed when bridge fails (default `false`)
- `AUTORESEARCH_CMD="..."` command that executes AutoResearch
- `AUTORESEARCH_SOURCE_PATH=...` JSON produced by your AutoResearch run
- `AUTORESEARCH_OUTPUT_PATH=...` canonical model signal path (default `data/research_signal_latest.json`)
- `AUTORESEARCH_MAX_AGE_MINUTES=180` freshness window
- `AUTORESEARCH_ALLOW_STALE=true|false` allow old payloads
- `AUTORESEARCH_WRITE_NEUTRAL_FALLBACK=true|false` write neutral signal when AutoResearch fails (default `true`)
- `AUTORESEARCH_PRECHECK=true|false` run setup validation before bridge (default `true`)
- `AUTORESEARCH_PRECHECK_DRY_RUN=true|false` run command/source and validate JSON contract (default `false`)
- `AUTORESEARCH_PRECHECK_TIMEOUT_SEC=90` timeout for dry-run precheck execution
- In strict mode (`AUTORESEARCH_REQUIRED=true`), weekly automation forces dry-run precheck on.
- `AUTORESEARCH_STRICT_ALLOW_MAINTENANCE_FALLBACK=true|false` allow one-run fallback continuation after strict precheck failure (default `false`)
- `AUTORESEARCH_MAINTENANCE_EXIT_CODE=4` dedicated exit code when maintenance override is active
- Pre-filled template: `.env.autoresearch.example`
- Strict execution template: `.env.autoresearch.active.example`

Manual precheck:

```sh
bash scripts/check_autoresearch_setup.sh
```

Manual precheck with dry-run validation:

```sh
AUTORESEARCH_ENABLED=true \
AUTORESEARCH_PRECHECK_DRY_RUN=true \
bash scripts/check_autoresearch_setup.sh
```

Maintenance override example (strict mode, but continue with forced fallback when precheck fails):

```sh
AUTORESEARCH_ENABLED=true \
AUTORESEARCH_REQUIRED=true \
AUTORESEARCH_STRICT_ALLOW_MAINTENANCE_FALLBACK=true \
bash scripts/run_weekly_scorecard.sh
```

## Risk guardrails

Optional hard limits — set to `0` to disable:

| Variable | Description |
|---|---|
| `MAX_DAILY_LOSS_PCT` | Block new BUYs once daily drawdown reaches this % |
| `MAX_BUYS_PER_HOUR` | Maximum BUY trades in the last 60 minutes |
| `LOSS_STREAK_PAUSE_THRESHOLD` | Consecutive losses before pausing entries |
| `LOSS_STREAK_PAUSE_SECONDS` | Pause duration after a loss streak |

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
When strict maintenance override is used (`AUTORESEARCH_STRICT_ALLOW_MAINTENANCE_FALLBACK=true`),
weekly automation returns a dedicated degraded-mode code (default `4`) for the run.

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
- `underlying_exit_code`
- `final_exit_code`
- `report_file`
- `latest_report_link`

`latest_status.prom` metrics (when enabled):
- `trading_scorecard_final_exit_code`
- `trading_scorecard_underlying_exit_code`
- `trading_scorecard_timestamp_seconds`
- `trading_scorecard_run_mode{mode="..."}`
- `trading_scorecard_verdict{verdict="..."}`

Monitoring extraction examples:

```sh
# Using grep (prints full key=value lines)
grep '^RUN_MODE=' results/scorecards/latest_scorecard.txt
grep '^FINAL_EXIT_CODE=' results/scorecards/latest_scorecard.txt

# Using awk (prints only values)
awk -F= '/^RUN_MODE=/{print $2}' results/scorecards/latest_scorecard.txt
awk -F= '/^FINAL_EXIT_CODE=/{print $2}' results/scorecards/latest_scorecard.txt

# Single-line status for cron/monitoring
awk -F= '/^RUN_MODE=/{m=$2} /^FINAL_EXIT_CODE=/{e=$2} END{printf "run_mode=%s exit_code=%s\n", m, e}' \
  results/scorecards/latest_scorecard.txt

# Read directly from latest_status.env
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

Or run the scorecard manually with custom thresholds:
```sh
python3 go_no_go_scorecard.py --file trade_journal.csv \
  --lookback-days 7 --starting-capital 20 \
  --min-closed-trades 200 --min-win-rate 45 \
  --min-profit-factor 1.2 --max-drawdown-pct 10
```

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

## Disclaimer

This project is for research and educational purposes. Use at your own risk.


Verdicts:
- `GO` (exit code `0`)
- `HOLD` (exit code `2`)
- `NO-GO` (exit code `3`)
- `MAINTENANCE-OVERRIDE` (exit code `4` by default, configurable)

Weekly automation:

```sh
bash scripts/run_weekly_scorecard.sh
```

- Writes timestamped reports to `results/scorecards/`
- Updates `results/scorecards/latest_scorecard.txt`
- Updates `results/scorecards/latest_status.env`
- Optionally updates `results/scorecards/latest_status.json` when `STATUS_JSON_ENABLED=true`
- Optionally updates `results/scorecards/latest_status.prom` when `STATUS_PROM_ENABLED=true`
- Returns the scorecard exit code for automation
- Uses `PYTHON_BIN` if set, otherwise `.venv/bin/python`, otherwise `python3`

Server example:

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

Server example with AutoResearch defaults:

```sh
cp .env.live.example .env
cat .env.autoresearch.example >> .env

PYTHON_BIN=./.venv/bin/python \
AUTORESEARCH_ENABLED=true \
AUTORESEARCH_WRITE_NEUTRAL_FALLBACK=true \
bash scripts/run_weekly_scorecard.sh
```

Server example with real AutoResearch command (strict):

```sh
cp .env.live.example .env
cat .env.autoresearch.active.example >> .env

# then edit placeholders in .env:
# - AUTORESEARCH_REPO_PATH
# - AUTORESEARCH_CMD

PYTHON_BIN=./.venv/bin/python \
AUTORESEARCH_ENABLED=true \
AUTORESEARCH_REQUIRED=true \
bash scripts/run_weekly_scorecard.sh
```

`AUTORESEARCH_CMD` templates:

```sh
# Python repo (entry script)
AUTORESEARCH_CMD=./external/autoresearch/.venv/bin/python main.py --output ./data/research_signal_raw.json

# Python repo (module)
AUTORESEARCH_CMD=./external/autoresearch/.venv/bin/python -m autoresearch --output ./data/research_signal_raw.json

# Node repo (npm script)
AUTORESEARCH_CMD="npm run research -- --output ./data/research_signal_raw.json"

# Node repo (direct node entry)
AUTORESEARCH_CMD="node index.js --output ./data/research_signal_raw.json"
```

Node Exporter textfile collector (relative path setup):

```sh
# write metrics to a collector subfolder in this repo
STATUS_PROM_ENABLED=true
STATUS_PROM_FILE=./results/scorecards/textfile/trading_scorecard.prom
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

Deploy on another server (quick runbook):

```sh
# on server
git clone <your-repo-url> /opt/trading_2
cd /opt/trading_2
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.live.example .env
```

Then run bot/scorecard from that server only.

VS Code task:
- `Scorecard: Weekly Go/No-Go` (from `.vscode/tasks.json`)

🚨 Disclaimer & Risk Warning

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
