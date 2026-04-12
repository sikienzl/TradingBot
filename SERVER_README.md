# Server Deployment Guide

This guide is for running the trading bot on a separate server (not on the workstation).

## 1. Package Contents
The server package contains only runtime-relevant files:
- Bot and helper scripts
- Model files
- Environment examples
- Scorecard automation script

It excludes workstation artifacts such as:
- local virtual environment
- large local data dumps
- logs and temporary files
- historical dry-run journals

## 2. Install On Server
Example target path:

```sh
mkdir -p /opt/trading_2
cd /opt/trading_2
```

Unzip the package there, then create environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Create runtime env file:

```sh
cp .env.live.example .env
```

Set API keys and all required parameters in .env.

## 3. Run Bot On Server
Dry run test:

```sh
.venv/bin/python trading_bot.py
```

If live mode is configured in .env, monitor logs carefully and keep trade size small.

## 4. Weekly Go/No-Go Scorecard
Run manually:

```sh
bash scripts/run_weekly_scorecard.sh
```

Override defaults if needed:

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

Reports are written to:
- results/scorecards/
- results/scorecards/latest_scorecard.txt

## 5. Updating From New Package
Before replacing code, keep a backup of:
- .env
- trade_journal.csv
- results/scorecards/

Then unpack new package, reinstall requirements if needed, and restart the service/process.

## 6. Operational Safety
- Start with smallest possible position sizes.
- Keep DRY_RUN=true until server behavior is verified.
- Increase risk only after repeated GO/HOLD scorecards and stable forward results.
