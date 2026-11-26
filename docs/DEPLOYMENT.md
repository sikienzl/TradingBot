# Deployment

Run commands from the repository root unless stated otherwise. The following
setups are alternatives; none is required to use the others.

## Local computer

Use a regular computer for development, data collection, backtesting, and
simulation. Rules run without dedicated acceleration; CatBoost and the local
LLM are optional. LLM fine-tuning has substantially higher hardware
requirements. Start with the [local quickstart](../README.md#quickstart) and
the [usage guide](USAGE.md).

## Raspberry Pi 4 without Hailo

Run the rule-based bot on a single Raspberry Pi without a Hailo accelerator.
The optional Mammouth.ai API copilot is a separate external service; it is not
the local LLM or the Hailo backend and needs network access and a private API
key. Without it, rules can still run. The copilot proposes bounded parameter
changes; it does not place exchange orders independently. In the example
profile it is disabled (`AI_COPILOT_ENABLED=false`), and shadow mode
(`AI_COPILOT_SHADOW_MODE=true`) logs suggestions without applying them.
Outside shadow mode, the bot can apply a bounded parameter change.
Configure credentials only in the private environment, never in the repository. See the
[Raspberry Pi installation guide](../SERVER_README.md#raspberry-pi-deployment-via-github-releases)
and [configuration](CONFIGURATION.md). The low-RAM Pi profile described in
the usage guide was tested on a Pi 3B+; that does not constitute a separate
Pi 4 performance or live-trading validation.

## Sipeed Nano cluster with two CM5 modules

The two-node CM5 setup can separate the bot host from an optional Hailo
inference host. The documented current Hailo setup is an isolated
simulation/shadow deployment, not a claim that the hardware or model is ready
for live trading. A compatible accelerator, compiled and validated trading
model, private service token, and protected network connection are required
for remote Hailo inference; the cluster alone does not provide them.
Compiling the trading model requires a compatible Hailo Dataflow Compiler
environment; the documented compiler package requires an x86-64 host.

For the Hailo service, SSH tunnel, model preparation and operational
constraints, see the [cluster shadow runbook](../SERVER_README.md#current-cluster-shadow-deployment)
and [Hailo inference guide](../SERVER_README.md#hailo-8-remote-inference-optional).
The separate [cluster starter](../cluster-starter/README.md) provides a
generic two-node K3s bootstrap and does not by itself deploy the trading bot.

## General server quick runbook

This example uses the live profile (`DRY_RUN=false`). Review the configuration
and credentials before starting the bot; use `.env.example` instead for a
dry-run profile.

```sh
# on server
git clone <your-repo-url> /opt/trading_2
cd /opt/trading_2
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.live.example .env
```

Then run bot/scorecard from that server only. With `DRY_RUN=false` the bot also
requires a fresh `GO` scorecard verdict before it starts, so run
`scripts/run_weekly_scorecard.sh` (or enable `scorecard.timer`) first; see
[scorecard gating](MONITORING.md#scorecard-gating-of-live-trading-and-model-promotion).

For the complete server, dashboard, and Hailo runbook, see [SERVER_README.md](../SERVER_README.md).
