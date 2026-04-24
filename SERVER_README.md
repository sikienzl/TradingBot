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

---

## Raspberry Pi Deployment (via GitHub Releases)

The easiest way to run the bot on a Raspberry Pi is through the pre-built GitHub Release packages.

### Quick Install (one-liner)

Run this on your Pi as root:

```bash
curl -fsSL https://github.com/sikienzl/TradingBot/releases/latest/download/install_pi.sh \
  | sudo bash -s -- v0.1.0
```

Or to install a specific version:

```bash
sudo bash install_pi.sh v0.1.1
```

The installer will:
1. Install system packages (`python3-venv`, `libta-lib-dev`, …)
2. Create a restricted `trading` system user
3. Download and verify the release archive from GitHub
4. Set up `/opt/trading_2` with a Python virtual environment
5. Install and enable systemd units (`trading-bot.service`, `scorecard.timer`)

### After Install

```bash
# 1. Configure your API keys and settings
sudo nano /opt/trading_2/.env

# 2. Start the bot
sudo systemctl start trading-bot

# 3. Watch logs
sudo journalctl -u trading-bot -f
```

### Useful systemd Commands

```bash
sudo systemctl status  trading-bot      # check status
sudo systemctl restart trading-bot      # restart
sudo systemctl stop    trading-bot      # stop
sudo systemctl list-timers              # see weekly scorecard schedule
```

### Creating a New Release (from dev machine)

```bash
# Build, tag, and push a new Pi release:
bash scripts/create_pi_release.sh

# Or tag manually and let CI publish it:
git tag v0.1.1 && git push origin v0.1.1
```

The `.github/workflows/release.yml` workflow automatically builds the Pi tarball and publishes a GitHub Release whenever a `v*` tag is pushed.

### Download a Specific Release Manually

```bash
VERSION=v0.1.0
curl -LO "https://github.com/sikienzl/TradingBot/releases/download/${VERSION}/trading-bot-pi-${VERSION}.tar.gz"
curl -LO "https://github.com/sikienzl/TradingBot/releases/download/${VERSION}/trading-bot-pi-${VERSION}.tar.gz.sha256"
sha256sum -c "trading-bot-pi-${VERSION}.tar.gz.sha256"
```

---

## 6. Operational Safety
- Start with smallest possible position sizes.
- Keep DRY_RUN=true until server behavior is verified.
- Increase risk only after repeated GO/HOLD scorecards and stable forward results.
