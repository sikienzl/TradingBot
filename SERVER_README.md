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

Operational note for current Pi/server profile:
- `EXCLUDED_COINS` keeps the base stablecoin-like exclusions
- `LOSSMAKER_EXCLUDED_COINS` currently defaults to `ZEC,HYPE,TON,BTC,XRP`
- `DYNAMIC_LOSSMAKER_EXCLUSION_ENABLED=true` adds a temporary journal-driven suppression layer for recent timeout-heavy losers
- `DYNAMIC_LOSSMAKER_MIN_PNL_LOSS=0.01` is the current Pi baseline so marginal near-flat names are not excluded as aggressively as with the old `0.003` threshold
- `COIN_MAX_HOLD_SECONDS` now defaults to `LINK:420,SUI:420,ONDO:420`
- `PORTFOLIO_TRADE_AMOUNT_MULTIPLIERS` and `PORTFOLIO_MAX_OPEN_TRADES_TIERS` can scale position size and slot count upward as portfolio milestones are reached
- `UPTREND_ENTRY_MAX_RSI_BY_COIN`, `UPTREND_ENTRY_MIN_BUY_PROBA_BY_COIN`, `UPTREND_ENTRY_MAX_SELL_PROBA_BY_COIN`, `UPTREND_ENTRY_MIN_PROBA_EDGE_BY_COIN`, and `FALLBACK_MAX_RSI_BY_COIN` allow coin-specific entry tuning without loosening the global gates; current defaults keep `TRX` slightly stricter in fallback (`72`) than in the uptrend gate (`74`) so marginal re-entries do not churn as easily, while `XDC` still stays behind the stronger sell-proba guard
- Override `LOSSMAKER_EXCLUDED_COINS` in `/opt/trading_2/.env` if you want to re-allow one of these symbols after review
- Override the dynamic lossmaker knobs in `/opt/trading_2/.env` if you want a shorter or longer recent-loss memory window
- Override the portfolio scaling and coin-specific uptrend knobs in `/opt/trading_2/.env` if you want to accelerate or slow compounding in later stages

## 3. Run Bot On Server
Dry run test:

```sh
.venv/bin/python -m src.trading_bot
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

## Hailo-8 Remote Inference (Optional)

The trading bot stays on the bot host; the Hailo-8 service runs separately on
the configured Hailo host. This setup does not move exchange
credentials to the accelerator host. CatBoost remains the default backend.
The current deployment is an isolated **simulation-only shadow bot**, not a
replacement for the existing bot containers. The Hailo runtime host has
HailoRT but not the DFC compiler, and image-model HEFs are incompatible.
Karpathy's `autoresearch` is an LLM-training experiment, not the market-signal
bridge described in the main README, and does not train this trading model.
The available Hailo-8 DFC package used below targets x86-64; the Raspberry Pi
uses the compiled HEF for inference, not model training or compilation.

A second, independent candidate shadow service must not assume it can open
the same Hailo device concurrently with the existing service. HailoRT device
sharing or a separate device must be configured and validated first. Do not
replace the existing service, its model, or its simulation state to test a
research candidate. Model classification metrics are not a profitability or
live-trading guarantee.

### Current cluster shadow deployment

The current shadow deployment differs from the optional system-wide service
described below:

- The commands below use `rtk` where they are executed. The current Raspberry
  Pis do not have `rtk` installed; from a host with RTK, invoke the equivalent
  command on the appropriate Pi through `rtk proxy ssh <host> ...` instead.
- Hailo host: `~trading/trading-hailo-dryrun` holds the inference code, virtual
  environment, private `service.env`, and the matching `model/hailo_model.hef`
  and `model/metadata.json`. The enabled user unit
  `trading-hailo-dryrun.service` listens only on `127.0.0.1:18001`.
- Bot host: `trading-hailo-tunnel.service` forwards its localhost port 18001
  through SSH to the Hailo host; `trading-hailo-shadow-dryrun` uses host
  networking and writes its own state and journal under
  `~trading/trading-hailo-shadow/data`.
  It runs with `DRY_RUN=true`, `SIMULATE_DATA=true` and `SIMULATION_REGIME=mixed`.
  The current shadow portfolio was initialized once from Kraken's available
  EUR balance; subsequent runs restore its saved simulated state without
  Kraken credentials. Other deployments retain the 1,000 EUR default unless
  explicitly configured otherwise.
- Both nodes need systemd user lingering. The separate cluster Ansible
  repository configures this; otherwise an administrator can enable it for
  the service user with `sudo loginctl enable-linger trading`.

The model was trained on public Kraken hourly data for ten markets and is for
simulation research only. In an ONNX-versus-Hailo test of 1,320 validation
rows, top-class agreement was 97.95%; the DFC warned that CPU-only
optimization was reduced. This is not a profitability test or live-trading
approval. Neither the HEF nor training/calibration data, private key or bearer
token are stored in Git. For exact recovery, securely back up the matching HEF,
metadata and input data outside Git; training again on newer market data will
produce a different model.

#### Restore the user service on the Hailo host

The following assumes a repository checkout on the host. Do not overwrite an
existing deployment without backing it up. Place the validated matching HEF
and metadata in `~/trading-hailo-dryrun/model/` first. The HailoRT host Python
bindings must match the installed native runtime.

```sh
rtk proxy mkdir -p ~/trading-hailo-dryrun/src ~/.config/systemd/user
rtk proxy cp src/__init__.py src/api_models.py src/hailo_contract.py \
  src/hailo_runtime.py src/hailo_service.py ~/trading-hailo-dryrun/src/
rtk proxy python3 -m venv --system-site-packages ~/trading-hailo-dryrun/.venv
rtk proxy ~/trading-hailo-dryrun/.venv/bin/pip install -r requirements-hailo-server.txt
rtk proxy cp deploy/trading-hailo-dryrun.service ~/.config/systemd/user/
```

Create `~/trading-hailo-dryrun/service.env` with mode `0600`, containing
`HAILO_HEF_PATH` and `HAILO_METADATA_PATH` as absolute paths to those artifacts,
and `HAILO_API_TOKEN` as a long random secret. The exact same token must be
stored in the shadow env file on the bot host over a trusted encrypted channel;
do not put it in Git, command arguments, or logs. Then:

```sh
rtk proxy systemctl --user daemon-reload
rtk proxy systemctl --user enable --now trading-hailo-dryrun.service
rtk proxy curl --fail --silent --show-error http://127.0.0.1:18001/health
```

#### Restore the restricted tunnel on the bot host

Create `~/trading-hailo-shadow` with mode `0700`. Generate a dedicated
`tunnel_ed25519` SSH key there; do not overwrite an existing key. Obtain and
verify the Hailo host's ED25519 host-key fingerprint through a trusted channel,
then store its public host key in `~/trading-hailo-shadow/known_hosts`. On the
Hailo host, add only the tunnel public key to the `trading` user's
`~/.ssh/authorized_keys` with this restriction prefix:

```text
restrict,port-forwarding,permitopen="127.0.0.1:18001",command="/bin/false" ssh-ed25519 <public-key>
```

Create `~/trading-hailo-shadow/tunnel.env` with mode `0600` and set
`HAILO_SSH_HOST` to the Hailo host's DNS name or IP address. The unit always
connects as `trading`. The private key must never leave the bot host.

```sh
rtk proxy cp deploy/trading-hailo-tunnel.service ~/.config/systemd/user/
rtk proxy systemctl --user daemon-reload
rtk proxy systemctl --user enable --now trading-hailo-tunnel.service
rtk proxy curl --fail --silent --show-error http://127.0.0.1:18001/health
```

#### Restore the isolated shadow container on the bot host

The repository's `docker/hailo-shadow.Dockerfile` is a standalone rebuild path
for the same application and configuration. It does not require the
pre-existing `trading-simulation:analytics-52247d45` base image used for the
currently running container. A rebuild may resolve different base-image or
transitive dependency versions and is not bit-for-bit identical.

```sh
rtk proxy docker build \
  --build-arg TRADING_UID="$(id -u)" \
  --build-arg TRADING_GID="$(id -g)" \
  -f docker/hailo-shadow.Dockerfile -t trading-hailo-shadow:dryrun .
rtk proxy mkdir -p ~/trading-hailo-shadow/data
rtk proxy cp deploy/hailo-shadow.env.example ~/trading-hailo-shadow/shadow.env
rtk proxy chmod 600 ~/trading-hailo-shadow/shadow.env
```

Set the same bearer token in the private `shadow.env`; do not commit it. The
build arguments align the container account with the host's `trading` UID and
GID so it can write its bind-mounted data. Start only when the service and
tunnel health checks pass, and do not replace an existing container without
reviewing its state:

```sh
rtk proxy docker run -d --name trading-hailo-shadow-dryrun \
  --restart unless-stopped --network host \
  --env-file ~/trading-hailo-shadow/shadow.env \
  -e DRY_RUN=true -e SIMULATE_DATA=true \
  --mount type=bind,src="$HOME/trading-hailo-shadow/data",dst=/data \
  trading-hailo-shadow:dryrun
rtk proxy docker logs --tail 100 trading-hailo-shadow-dryrun
```

The explicit flags keep this example in simulation. Docker administrators can
inspect container environment variables, including the token; restrict access
to the Docker daemon. On startup, confirm both dry-run and simulation mode,
Hailo predictions, and the absence of prediction errors. The service and
tunnel are user units; `systemctl --user status` checks each. To stop only the
shadow bot, use `rtk proxy docker stop trading-hailo-shadow-dryrun`. Do not
enable live trading on the strength of the simulation.

#### Optional Kraken EUR starting balance

For a new simulation, set `SIMULATION_INITIAL_BALANCE_FROM_KRAKEN=true` in
the private shadow environment and supply `KRAKEN_API_KEY` and
`KRAKEN_API_SECRET` there. Prefer a dedicated Kraken API key with permission
to query funds only, without trading or withdrawal permissions. Initial
balance retrieval requires Kraken, EUR, `DRY_RUN=true` and
`SIMULATE_DATA=true`. Docker administrators can inspect credentials passed
through the container environment.

When `/data/.portfolio_state.json` is absent, the bot fetches the available
EUR balance once, excludes crypto holdings, and persists the resulting
simulation portfolio before trading. The exchange is not queried on
subsequent starts: the saved portfolio is restored and a restart is logged.
Market data and trades remain simulated. Kraken errors, invalid balances,
unreadable state or persistence failures stop the bot instead of falling back
to 1,000 EUR. An existing state, including the old 1,000 EUR simulation, is
always restored rather than replaced. To start a *new* Kraken-funded
simulation, first stop only the shadow container and securely back up its
state and journal, then move the old state out of `/data` before starting.
Never delete or replace a state file accidentally.
After initialization, recreate the container with a private environment file
that omits both Kraken credentials; they are not needed while the saved state
is present. Remove the temporary credential-bearing environment file securely.
If the state is lost, the bot cannot initialize again without credentials.

Keep `/data/.portfolio_state.json` on persistent storage and back it up
outside the cluster. It contains sensitive account-derived information and
must not be committed. Atomic writes protect the last complete state from
partial writes, but cannot protect against failed storage or a lost backup.
The restart message identifies a bot restart, not specifically a host reboot
or power outage. The simulation iteration is saved along with the portfolio;
performance counters are not restored, and the trade journal is a separate
file.

#### Separate paper run with public Kraken market data

Use a **new** private data directory, never the existing shadow directory.
The following one-shot container reads the credentials file only during
initialization, fetches the free EUR balance, and refuses to overwrite an
existing portfolio. Prefer a funds-query-only API key; the complete credentials
file is accessible to this temporary container. Its contents are not required
by the long-running bot.

```sh
rtk proxy mkdir -m 700 ~/trading-hailo-public-paper
rtk proxy mkdir -m 700 ~/trading-hailo-public-paper/data
rtk proxy docker build \
  --build-arg TRADING_UID="$(id -u)" \
  --build-arg TRADING_GID="$(id -g)" \
  -f docker/hailo-shadow.Dockerfile -t trading-hailo-public-paper:local .
rtk proxy docker run --rm --network host \
  --mount type=bind,src="$HOME/trading-hailo-public-paper/data",dst=/data \
  --mount type=bind,src=/opt/trading_2/.env,dst=/run/kraken.env,readonly \
  trading-hailo-public-paper:local \
  python3 -m src.init_kraken_paper --credentials-file /run/kraken.env
```

Copy the Hailo token from the existing private shadow configuration into a
new private environment file, omitting all Kraken credentials. Do not copy
the old portfolio or journal. Then start a separate container:

```sh
rtk proxy docker run -d --name trading-hailo-public-paper \
  --restart unless-stopped --network host \
  --env-file "$HOME/trading-hailo-public-paper/paper.env" \
  -e DRY_RUN=true -e SIMULATE_DATA=false \
  -e SIMULATION_INITIAL_BALANCE_FROM_KRAKEN=true \
  -e DRY_RUN_COSTS_ENABLED=true -e DRY_RUN_FEE_RATE=0.002 \
  -e DRY_RUN_SLIPPAGE_RATE=0.001 \
  --mount type=bind,src="$HOME/trading-hailo-public-paper/data",dst=/data \
  trading-hailo-public-paper:local
```

The paper environment needs the Hailo settings from
`deploy/hailo-shadow.env.example`, its own `/data/trade_journal.csv`, and no
Kraken credentials. In this mode the bot rejects Kraken credentials, restores
only the separate initialized portfolio and uses unauthenticated public
market endpoints. Exchange initialization failures do not enable synthetic
market data; unavailable market data skips the iteration. Orders are
simulated, with the specified fees and slippage on each side. The current
account balance is not refreshed after initialization. This experiment neither
places orders nor establishes real profitability.

#### Continuous public-market recorder (read-only)

The recorder independently observes the six EUR markets in the stopped public
paper run. It uses no `.env`, exchange credentials, account endpoints, or
portfolio/journal files. It samples public bid/ask/last every 10 seconds,
backfills up to 720 one-minute candles on first start, then records completed
one-minute candles. Keep this process separate from the paper bot; its data
directory is not a bot state or journal directory.

On the current host the recorder stores its files on the SSD at
`/mnt/nvme_data/trading-market-recorder/data`. The SSD must be mounted there
before the service starts. An administrator must create the private parent
directory, owned by the `trading` user and group, and the trading user must
create `data` inside it with mode `0700`. Do not use a directory under the
mountpoint on the root filesystem as a fallback.

The recorder and dashboard user services require
`~/trading-market-recorder/storage.env`, mode `0600`, containing
`MARKET_RECORDER_FS_UUID=<expected filesystem UUID>`. Obtain the UUID using
`findmnt -rn -M /mnt/nvme_data -o UUID` while the intended SSD is mounted;
verify it independently before saving it. Both services check this UUID and
the data directory before starting, retry on failure, and do not silently use
the root filesystem. Keep Docker's restart policy disabled for this recorder:
systemd performs the mount check on every service start. The dashboard will
also be unavailable while the SSD check fails. The files are retained without
automatic deletion; monitor both SSD space and Docker/system logs separately.

Build the image from the repository root and install the user service, without
passing any bot environment file:

```sh
rtk proxy docker build \
  --build-arg TRADING_UID="$(id -u)" \
  --build-arg TRADING_GID="$(id -g)" \
  -f docker/hailo-shadow.Dockerfile -t trading-market-recorder:local .
rtk proxy mkdir -m 700 /mnt/nvme_data/trading-market-recorder/data
rtk proxy mkdir -p ~/.config/systemd/user
rtk proxy cp deploy/public-market-recorder.service ~/.config/systemd/user/
rtk proxy systemctl --user daemon-reload
rtk proxy systemctl --user enable --now public-market-recorder.service
```

When migrating existing data, stop the old recorder first, preserve the
original directory as a rollback backup, copy all files to the SSD and verify
their checksums before starting the new service. Never run two writers against
different copies. Update and restart the dashboard service so it mounts the
same SSD directory read-only.

Verify with `systemctl --user status public-market-recorder.service` and
`docker logs --tail 30 trading-market-recorder`; output files are
`tickers.csv`, `candles_1m.csv`, and `metadata.json`. The recorder exits
explicitly if another writer holds the directory, the saved schema/settings
do not match, or a requested Kraken market is inactive. It appends to existing
data rather than replacing it. Restart it with the same image settings and
directory so candle timestamps are deduplicated and missed candles are fetched
where Kraken's OHLCV history allows. Log any
reported gaps; the service cannot recreate candles outside Kraken's available
history. Keep it running for at least 30 days before treating the captures as
a forward sample. The observations are public quotes and candles, not actual
fills or a profitability guarantee.

#### Daily paper observation (read-only)

The report uses only the isolated paper journal and bot logs; it does not
connect to Kraken, change the portfolio, or submit orders. Python 3.12+ is
required; Docker CLI access is needed only for `--container`. For a manual
report, from the checkout:

```sh
rtk proxy python3 scripts/public_paper_daily_report.py \
  --journal "$HOME/trading-hailo-public-paper/data/trade_journal.csv" \
  --container trading-hailo-public-paper \
  --config "$HOME/trading-hailo-public-paper/paper.env" \
  --date 2026-06-06 --log-timezone UTC
```

Omit `--date` for the previous UTC calendar day. Use `--log /path/to/bot.log`
instead of `--container`, or omit both if logs are unavailable. Docker's log
driver must support `docker logs` and retain the full day. The bot's log
timestamps have no timezone: set `--log-timezone` to the actual IANA timezone
of the bot process (normally UTC inside the container), not the report host.
The date window is start-inclusive/end-exclusive UTC. Log rotation, missing
or malformed timestamps and a stopped bot can make the observation incomplete;
zero observed events does not prove zero activity.

The JSON includes buys, sell events (including partial exits), realized net
PnL, average holding duration and sample count, fees and slippage for **fills
in the window**, and selected blocked-entry and Hailo prediction/gate/error
log events. Net PnL already includes modeled costs; do not subtract them again.
Costs for an entry before the window are not included in the day's fill-cost
sum, although their effect is in the closed trade's net PnL. Missing or
legacy/mixed-cost PnL is `null`, not zero. Unrealized PnL, spread beyond the
configured slippage model, actual exchange execution and unlogged blocks are
not measured. Counts are log events, not a complete or deduplicated count of
all rejected candidates; risk guardrails count iterations. Hailo decisions
are model outputs, not necessarily executed trades. Journal data is assumed to
be the isolated paper run and in its configured base currency; protect it.

For a descriptive side-by-side, add `--compare-journal /path/to/other.csv`,
optionally `--compare-log` or `--compare-container`, `--compare-config`, and
`--compare-feed "synthetic simulation"` (or a precise feed description).
The result is always **non-comparable**, even for two public Kraken bots:
independent real-time polling, timing, missing data, market selection,
execution, initial balances and configuration can differ. The report exposes
operator-supplied feed descriptions, journal run/config hashes, and differences
in an allowlist of non-secret environment settings. Env files are snapshots,
not proof of effective defaults, runtime overrides or configuration history;
unset values are explicitly unknown. It never outputs credentials or arbitrary
environment keys. A genuine controlled comparison requires replaying the
same recorded input and controlling all relevant settings; this tool does not
provide that experiment.

The regular report includes the completed previous UTC day and a separate
current UTC day-to-now section. Passing `--date YYYY-MM-DD` explicitly reports
only that completed UTC day, preserving historical comparisons.

The report also carries a `staleness` block with the newest journal row or bot log
line seen anywhere in the sources, independent of the report window. With
`--max-data-age-hours N` (wrapper default 26 via `PUBLIC_PAPER_MAX_DATA_AGE_HOURS`)
the run still writes the JSON for analysis but exits non-zero when no activity is
newer than N hours, which means a stopped collector is reported instead of a
quietly empty day. Because the wrapper stops on that failure, `latest.json` keeps
the last healthy report. Omit the option, or set 0, for manual `--date` backfills so
they never false-alarm. When only `--container` log access is available, the log
source is window-limited and staleness stays `unknown` rather than misleading;
journal rows then carry the signal.

To schedule the previous UTC day at 00:10 UTC, install the **user** units
`deploy/public-paper-daily-report.{service,timer}` into
`~/.config/systemd/user/`, edit the service's `ExecStart` to this checkout's
absolute script path, then run `systemctl --user daemon-reload` and
`systemctl --user enable --now public-paper-daily-report.timer`. This is an
optional operator action, not performed by the repository. Enable user
lingering if reports must run while logged out. The script writes private
`reports/report-YYYY-MM-DD.json` and `reports/latest.json` under the paper
directory. A persistent timer catches up once after downtime, but does not
backfill each missed day; run `--date` manually for gaps. Missing journals,
config files or bot log files cause the service to fail
instead of fabricating a report. Verify with `systemctl --user status
public-paper-daily-report.timer` and `journalctl --user -u
public-paper-daily-report.service`. The report user needs read access to the
paper data. The scheduled report reads the persistent bot log, so replacing
the container does not discard the earlier observations.

#### Optional browser dashboard (read-only)

The standalone dashboard displays the paper portfolio, recent journal events,
daily net PnL, modeled costs, and counts of ATR/Hailo entry blocks. It does not
control the bot or submit orders. It reads paper data and reports through
read-only mounts and does not mount the Docker socket. Access is protected by
HTTPS, HTTP Basic authentication, and a private CIDR allowlist. The allowlist
is private deployment configuration: include each authorized LAN or routed
VPN client subnet there, separated by commas. Never commit actual site CIDRs,
credentials, or TLS private keys.

From a checkout on the paper host:

```sh
git pull --ff-only
rtk proxy mkdir -m 700 "$HOME/trading-paper-dashboard"
rtk proxy mkdir -m 700 "$HOME/trading-paper-dashboard/tls"
cp deploy/public-paper-dashboard.env.example \
  "$HOME/trading-paper-dashboard/dashboard.env"
chmod 600 "$HOME/trading-paper-dashboard/dashboard.env"
```

Edit the private environment file. Set `DASHBOARD_BIND_HOST` to the host's
reachable interface address, `DASHBOARD_ALLOWED_NETWORKS` to the authorized
private CIDRs (including any routed VPN subnet), and choose a unique username
and long random password. The example leaves the network allowlist empty so the
service fails closed until you configure it privately. Add only client source
networks that are routed to this host, not the server's own subnet by habit.
Separate multiple IPv4 or IPv6 networks with commas. The application rejects
public networks and checks the actual peer IP; it does not trust
`X-Forwarded-For`. A VPN route or proxy must therefore preserve the client
source address. Configure the VPN routing and host/network firewall separately
to allow TCP port `8443` only from these client networks; do not expose it to
the public Internet.

Generate a private self-signed certificate whose Subject Alternative Name
matches every host address/name users will enter in their browser. For example,
when accessing by IP, replace the placeholder with that address:

```sh
openssl req -x509 -newkey rsa:3072 -nodes -days 825 \
  -keyout "$HOME/trading-paper-dashboard/tls/server.key" \
  -out "$HOME/trading-paper-dashboard/tls/server.crt" \
  -subj "/CN=<dashboard-host-address>" \
  -addext "subjectAltName=IP:<dashboard-host-address>" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign"
chmod 600 "$HOME/trading-paper-dashboard/tls/server.key"
chmod 644 "$HOME/trading-paper-dashboard/tls/server.crt"
```

Replace both `<dashboard-host-address>` placeholders with the actual address;
do not place it in repository files. Trust `server.crt` on client devices over
the trusted SSH connection (or accept the browser's certificate warning after
verifying its SHA-256 fingerprint). Never copy the private key to clients.

Build the image from the repository root and install the user service:

```sh
rtk proxy docker build \
  --build-arg TRADING_UID="$(id -u)" \
  --build-arg TRADING_GID="$(id -g)" \
  -f docker/public-paper-dashboard.Dockerfile \
  -t trading-paper-dashboard:local .
mkdir -p "$HOME/.config/systemd/user"
cp deploy/public-paper-dashboard.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now public-paper-dashboard.service
```

Open `https://<dashboard-host-address>:8443` from an allowed LAN/VPN client and
authenticate with the configured dashboard credentials. The process binds
only to `DASHBOARD_BIND_HOST`; the application independently checks the
request's source IP against every configured CIDR. The container runs as the
unprivileged `trading` UID/GID, drops all capabilities, uses a read-only root
filesystem, and has only read-only mounts for paper data, reports, and TLS
files. It also reads the public market recorder's `tickers.csv`,
`candles_1m.csv`, and `metadata.json` from a read-only `/market-data` mount.
The dashboard shows the last 24 hours where available, per-market freshness,
bid/ask spread, observed price change, candle coverage and gaps. Large CSVs
are read from a bounded recent tail and the UI reports when that limit applies.
Only the most recent 24 hours are shown; the original retained files are not
deleted or shortened by the dashboard.
These public quotes and candles are not trade executions and do not establish
profitability or executable fills. The recorder itself is neither restarted
nor modified by dashboard deployment. It does not mount the Docker socket
inside the container. The host
service uses `sg docker` because systemd user managers may not inherit the
account's supplementary Docker group; do not remove it unless the service
manager's group access is verified.

Check service health and listener:

```sh
systemctl --user status public-paper-dashboard.service
journalctl --user -u public-paper-dashboard.service -n 50 --no-pager
docker ps --filter name=trading-paper-dashboard
ss -ltn | grep ':8443'
```

The service should be enabled and active, the container should be running, and
the listener should show only the address configured in
`DASHBOARD_BIND_HOST`. Test without credentials (expected HTTP `401`) and in
the browser with credentials from an allowed client. If the service repeatedly
restarts, check that the allowlist is non-empty and private, the certificate
and key paths are readable, the bind address belongs to the host, TCP `8443`
is unused, and the account can run Docker through `sg docker`. If login works
but a client receives `403`, check its routed source IP against the allowlist;
forwarded headers do not affect the decision.

To rotate the password, edit the private `dashboard.env` with mode `0600` and
restart the service. To update the dashboard, pull the desired repository
revision, rebuild the image with the same UID/GID build arguments and tag, then
restart the user service. The dashboard itself is read-only and separate from
the paper-bot container; do not stop or recreate the paper bot to update it.
Missing legacy cost data is shown as unavailable rather than as zero.

For a reproducible Ansible deployment on an already-provisioned Debian/Ubuntu
host, see the generic starter in [`ansible/`](./ansible/) and its
[`README.md`](./ansible/README.md). For a separate, generic two-node CM5/K3s
bootstrap package, see [`cluster-starter/`](./cluster-starter/README.md); its
GitHub Actions workflow publishes validated ZIP pre-releases. Both starters
require operator-supplied inventory and secrets, and neither contains this
site's addresses or credentials. The cluster starter does not provision
hardware, VPN or firewall configuration, or deploy the TradingBot workloads.

Optional private `~/trading-hailo-public-paper/report.env` settings for the
wrapper: `PUBLIC_PAPER_DIR`, `PUBLIC_PAPER_REPORT_DIR`,
`PUBLIC_PAPER_LOG`, `PUBLIC_PAPER_LOG_TIMEZONE`, `PYTHON_BIN`, and
`PUBLIC_PAPER_COMPARE_JOURNAL`, `PUBLIC_PAPER_COMPARE_CONTAINER`,
`PUBLIC_PAPER_COMPARE_CONFIG`, `PUBLIC_PAPER_COMPARE_FEED`. Both logs must use
the same timezone for the wrapper. Keep reports and configuration outside Git.

### Prepare and compile the model

On a development machine with training dependencies, collect public Kraken
candles and derive features through the same analysis path as the bot. This
uses public market-data endpoints only, makes no trades and excludes the
still-forming candle:

```sh
rtk proxy pip install '.[hailo-export]'
rtk proxy python3 -m scripts.collect_hailo_training data/kraken_hailo_training.csv
rtk proxy python3 -m src.train_hailo_model --data-path data/kraken_hailo_training.csv \
  --output-dir model/hailo_trading_model
rtk proxy python3 -m scripts.prepare_hailo_calibration \
  data/kraken_hailo_training.csv model/hailo_trading_model
```

The collector defaults to ten markets and at most 720 hourly candles per
market; use `--coins` and `--limit` to adjust the request. Kraken supplies
recent data, so a later collection cannot recreate the original dataset.
Preserve the exact input CSV, model and metadata together outside Git.
Calibration uses only the purged training split, not the validation split.
The calibration step verifies the input CSV hash and split parameters against
the export metadata and refuses to replace existing outputs without
`--overwrite`. Models exported before provenance metadata was added must be
retrained to use this calibration tool.

The exporter writes `trading_mlp.onnx` and `metadata.json`. It does not create
a HEF. The compiler does not need to be installed on the Hailo host: compile on
an x86-64 SDK host, then place the resulting `trading_mlp.hef` and its matching
metadata on the Hailo host.

The local development host has a `trading-hailo-compiler:3.34.0` Docker image
with DFC 3.34.0 and HailoRT CLI 4.24.0. Its
`/opt/hailo/hailo_compile_onnx.py` wrapper runs parsing, calibration and HEF
compilation. The image is not distributed in this repository. A synthetic
dataset was used to train and export the actual 22-input, 32/16-hidden-unit,
3-output MLP, and the resulting ONNX was successfully compiled for Hailo-8
using this image. This is a compiler smoke test, not a model trained on real
market data or an on-device test.

#### Rebuild the compiler image

On an x86-64 Docker BuildKit host, obtain the following SDK files directly
from Hailo, subject to your license, and place them in a local `hailo-sdk/`
directory:

- `hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl`
- `hailort_4.24.0_amd64.deb`

The directory is git-ignored; SDK packages, locally saved image archives,
calibration data, HAR files and HEFs must not be committed. Do not publish
the image without verifying the Hailo license and auditing all image layers
for private information. The build uses a separate SDK context, not a copy of
the SDK in the repository. The built image still contains installed Hailo
software.

```sh
rtk proxy env DOCKER_BUILDKIT=1 docker build --platform linux/amd64 \
  --build-context hailo_sdk="$(pwd)/hailo-sdk" \
  --build-arg HAILO_UID="$(id -u)" --build-arg HAILO_GID="$(id -g)" \
  -f docker/hailo-compiler.Dockerfile \
  -t trading-hailo-compiler:3.34.0 .
```

The default user and group IDs are 1000; set the build arguments to the
unprivileged host user who will write the output directory. The Dockerfile
pins the SDK filenames but fetches Ubuntu packages and Python dependencies
during build, so rebuilding later may produce different transitive versions.
No exchange credentials or personal data are needed for the build.

The calibration preparation step writes a normalized NHWC `calibration.npy`
with shape `(samples, 1, 1, 22)` and a `calibration.alls` DFC script.
Use the same input CSV as for the model export. The default sample count is
up to 1,024; the script sets the DFC calibration count to the actual number
of samples. Without `--model-script`, DFC's default may use only 64 samples,
even when the array contains more. Do not use the synthetic smoke-test
calibration for a deployable model.

With the ONNX file and `calibration.npy` in the output directory, run on the
compiler host:

```sh
rtk proxy docker run --rm --network none \
  --mount type=bind,src="$(pwd)/model/hailo_trading_model",dst=/work \
  --workdir /work --entrypoint python trading-hailo-compiler:3.34.0 \
  /opt/hailo/hailo_compile_onnx.py \
  /work/trading_mlp.onnx /work/calibration.npy /work/trading_mlp.hef \
  --model-script /work/calibration.alls
```

The wrapper refuses to overwrite an existing HEF. The smoke test with 64
synthetic calibration samples and no GPU used DFC's reduced optimization level;
DFC warned that this is not recommended for production. The particular model
in the current shadow deployment was also checked on the Hailo host with
HailoRT 4.23.0 against its ONNX output, as described above. For every newly compiled model,
repeat the hardware and numerical validation, including tensor shapes,
normalization contract, and output logits, before deployment.
No live trading or profitability should be inferred from export-time validation
metrics.

### Install the service on the Hailo host

Install the repository files under `/opt/trading_hailo`, keeping the model
artifacts readable by the `trading` service account. Use the host-installed
HailoRT Python bindings and NumPy through a system-site-packages virtual
environment:

```sh
cd /opt/trading_hailo
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements-hailo-server.txt
```

Create `/etc/trading-hailo.env` with mode `0600` and a long random token:

```sh
HAILO_HEF_PATH=/opt/trading_hailo/model/trading_mlp.hef
HAILO_METADATA_PATH=/opt/trading_hailo/model/metadata.json
HAILO_API_TOKEN=<long-random-secret>
HAILO_BIND_HOST=<hailo-host-ip>
```

`HAILO_BIND_HOST` should be the Hailo host's private network interface address.
If omitted, the service binds to loopback only.

Install and start the supplied service unit only after the HEF and metadata
exist:

```sh
sudo cp deploy/hailo-inference.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hailo-inference.service
sudo systemctl status hailo-inference.service
```

The unit binds to the configured Hailo host address on port 8001; restrict
inbound TCP/8001 to the bot host with the host/network firewall. The bearer
token is sent over HTTP and is not encrypted, so do not expose this service outside a trusted
private network. The unauthenticated health route should also remain network
restricted. Check service logs with `sudo journalctl -u hailo-inference.service`.

### Enable on the bot host only after validation

In `/opt/trading_2/.env` on the bot host, set:

```sh
USE_TABULAR_MODEL=true
TABULAR_MODEL_BACKEND=hailo
HAILO_SERVICE_URL=http://hailo-host:8001
HAILO_API_TOKEN=<same-long-random-secret>
HAILO_TIMEOUT_SECONDS=3
```

Replace `hailo-host` with the Hailo host's private IP address or resolvable
hostname.

Keep the existing risk guards enabled and first run in simulation/dry-run.
Remote inference errors fail closed for entries on the affected coin and are
logged; they do not switch to rules-only buying. Confidence threshold
auto-tuning remains CatBoost-only. The current cluster deployment is the
separate shadow configuration described above, not this system-wide setup.

## 4b. Weekly Auto-Tuning Cycle

The repository includes a weekly tuning cycle that:
1. Computes one recommended parameter change from scorecard metrics
2. Optionally applies it to `.env` with automatic backup

Manual run:

```sh
bash scripts/run_weekly_tuning_cycle.sh
```

Default mode is dry-run. To allow actual `.env` updates, set in `/opt/trading_2/.env`:

```sh
TUNING_APPLY_CHANGES=true
```

Systemd units:
- `tuning-cycle.service`
- `tuning-cycle.timer` (Sunday 09:20, after scorecard)

Useful commands:

```sh
sudo systemctl status tuning-cycle.timer
sudo systemctl start tuning-cycle.service
sudo journalctl -u tuning-cycle.service -f
```

## 5. Updating From New Package
Before replacing code, keep a backup of:
- .env
- trade_journal.csv
- results/scorecards/

Then unpack new package, reinstall requirements if needed, and restart the service/process.

---

## Raspberry Pi Deployment (via GitHub Releases)

The easiest way to run the bot on a Raspberry Pi is through the pre-built GitHub Release packages.
The current Pi deployment profile, including the Grafana memory cap for monitoring, has been tested on a Raspberry Pi 3B+ with 1 GB RAM.

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
1. Install system packages (`python3-venv`, compiler/build tools, …)
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

### Release upgrade checks

The trade journal is retained during installation. On first startup, the bot
extends an older CSV schema atomically; pre-upgrade rows are labeled `legacy`.
Back up `.env`, `trade_journal.csv`, and any portfolio state before upgrading.

New trade events include a process `run_id`, a non-secret configuration hash,
and observed `mfe_pct` / `mae_pct` excursion values. For PostgreSQL analytics,
the bot adds these columns to an existing `trade_events` table; the configured
database role therefore needs permission to alter that table.

After an upgrade, keep `DRY_RUN=true` and verify:

1. Startup logs include `run_id` and `config_hash`.
2. Guard logs show the latest close age and any strict BUY candidates withheld.
3. New journal rows align under the `run_id`, `config_hash`, `mfe_pct`, and
   `mae_pct` CSV headers.
4. Journal reports separate runs/configurations and show exit reasons and
   observed excursions:

   ```sh
   /opt/trading_2/.venv/bin/python -m src.analyze_trade_journal \
     --file /opt/trading_2/trade_journal.csv
   ```

### PostgreSQL analytics backups

The PostgreSQL container now runs with the host `trading` account's numeric
UID and GID (`999:984` on the current bot host). The official image's
`postgres` account is UID 999 inside the container, so its database processes
map to host user and group `trading`. The existing database files remain owned
by UID 999; do not recursively change their ownership during this migration.
The NAS backup and restore-check jobs also run as host account `trading`; they
need Docker socket access and read/write access to the `nas-access` group mount
at `/nas-1/postgresql`.

Verify the container identity after deployment with:

```sh
docker inspect --format '{{.Config.User}}' postgresql-node1
docker exec postgresql-node1 ps -o uid,gid,user,group,comm -C postgres
```

The configured container user should match `id -u trading` and `id -g trading`
on the host.

Install the service units from `deploy/` and allow the `trading` group to
execute the root-managed backup scripts:

```sh
sudo chown root:trading /usr/local/sbin/postgresql-analytics-backup \
  /usr/local/sbin/postgresql-analytics-restore-check
sudo chmod 0750 /usr/local/sbin/postgresql-analytics-backup \
  /usr/local/sbin/postgresql-analytics-restore-check
sudo cp deploy/postgresql-analytics-backup.service \
  deploy/postgresql-analytics-restore-check.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start postgresql-analytics-backup.service
sudo systemctl start postgresql-analytics-restore-check.service
```

Both jobs require the expected NAS mount and fail rather than writing to an
unmounted local directory. Check each run with
`systemctl status postgresql-analytics-backup.service
postgresql-analytics-restore-check.service` and inspect its journal before
relying on the timers.

The stale-journal diagnostic does not disable or bypass the recent-PnL guard.
These metrics support review; they do not establish profitability.

### Historical rule replay

The offline replay evaluates the same shared rules-based recommendation logic
used by the live analyzer. On a development machine, collect historical public
OHLCV data and run the chronological training / holdout report:

```sh
python3 -m src.get_data
python3 -m src.historical_backtest --file full_crypto_data.csv \
  --initial-cash 19.50 --base-currency EUR
```

The collector retrieves up to 720 hourly candles per active EUR market. The
replay fills at the next candle's open and includes fee/slippage assumptions,
the recent-PnL guard, strict BUY entries, fixed-percentage stop-loss/take-profit,
and maximum hold time. It resets the portfolio at the holdout boundary and
uses earlier candles only to warm up indicators; the holdout PnL guard is
seeded from training closes, not training positions. It reports candle
freshness and warns when data is older than 24 hours. Time exits are resolved
only at candle boundaries, so hourly bars cannot model the bot's 120-second
default hold time precisely. Journal-driven coin exclusions,
fallback/force-fill entries, optional ML models, other runtime guards,
portfolio scaling tiers, ATR/trailing/partial exits, and exchange-specific
fills are not simulated.
Review the holdout and trade count; this is an offline diagnostic, not a
profitability forecast. It does not connect to the exchange or place orders.

### Useful systemd Commands

```bash
sudo systemctl status  trading-bot      # check status
sudo systemctl restart trading-bot      # restart
sudo systemctl stop    trading-bot      # stop
sudo systemctl list-timers              # see weekly scorecard schedule
```

### Optional Browser Monitoring (Prometheus + Node Exporter)

After the bot is installed, enable monitoring stack on the Pi:
These monitoring defaults are intended for the tested Raspberry Pi 3B+ baseline and can be relaxed on newer Pi models with more RAM.

```bash
sudo bash /opt/trading_2/scripts/install_monitoring_pi.sh
```

This enables:
- `scorecard-status.timer` (exports status metrics every 5 minutes)
- `node-exporter-textfile.service` (exposes metrics at port 9100)
- Prometheus scraping on port 9090
- Grafana dashboard at port 3000 (Login: admin / admin)

Low-RAM Pi defaults applied by the installer:
- Grafana memory cap via `deploy/grafana-memory-limit.conf`
- Grafana analytics and update checks disabled via `deploy/grafana-low-power.conf`
- Dashboard provisioning refresh reduced to every 300 seconds
- Dashboard auto-refresh set to 30 seconds

Browser endpoints in local network:

```text
http://<raspi-ip>:3000   ← Grafana Dashboard (Trading Bot — Pi Ops)
http://<raspi-ip>:9090   ← Prometheus
http://<raspi-ip>:9100/metrics  ← Raw metrics
```

### Creating a New Release (from dev machine)

Before tagging, update the `Unreleased` section in `CHANGELOG.md`, run the
complete test suite, and inspect the staged archive contents. The release
builder packages the `src/` modules used by the systemd service.

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
