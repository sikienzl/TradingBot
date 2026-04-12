# Crypto Trading Bot

A modular pipeline for crypto trading: data collection, feature engineering, CatBoost/LLM ensemble prediction, and live/simulated trading via the Kraken exchange. Designed for research, simulation, and (optionally) live trading.

## Setup

**Requirements:** Python 3.12+ and a virtual environment.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> TA-Lib requires the native C library. On Ubuntu/Debian: `sudo apt install libta-lib-dev`

Copy and edit the example config:
```sh
cp .env.example .env        # default / dry-run profile
# or
cp .env.live.example .env   # conservative live profile
```

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

### 3. Dry-run / live trading
```sh
python3 trading_bot.py
```
- Set `DRY_RUN=true` (default) to simulate trades without spending real funds.
- Set `SIMULATE_DATA=true` to run fully offline without an exchange connection.
- Trades are recorded to `trade_journal.csv`.

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
| `USE_TABULAR_MODEL` | `true` | Enable CatBoost signal filter |
| `AUTO_TUNE_TABULAR_CONFIDENCE` | `true` | Auto-adjust CatBoost threshold |
| `USE_ML_MODEL` | `false` | Enable LLM signal (GPU recommended) |

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
Exit codes: `0` = GO, `2` = HOLD, `3` = NO-GO.

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

## Disclaimer

This project is for research and educational purposes. Use at your own risk.


Verdicts:
- `GO` (exit code `0`)
- `HOLD` (exit code `2`)
- `NO-GO` (exit code `3`)

Weekly automation:

```sh
bash scripts/run_weekly_scorecard.sh
```

- Writes timestamped reports to `results/scorecards/`
- Updates `results/scorecards/latest_scorecard.txt`
- Returns the scorecard exit code for automation
- Uses `PYTHON_BIN` if set, otherwise `.venv/bin/python`, otherwise `python3`

Server example:

```sh
PYTHON_BIN=/opt/trading_2/.venv/bin/python \
JOURNAL_FILE=/opt/trading_2/trade_journal.csv \
LOOKBACK_DAYS=7 \
MIN_CLOSED_TRADES=200 \
MIN_WIN_RATE=45 \
MIN_PROFIT_FACTOR=1.2 \
MAX_DRAWDOWN_PCT=10 \
bash scripts/run_weekly_scorecard.sh
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
