"""Deployment paths shared by both PnL exporters.

``export_pnl_metrics`` (the one-shot CLI) and ``pnl_exporter`` (the HTTP
server) read the same files on the deployment host, so the defaults live here
once. Callers override them per call, and the HTTP exporter keeps them as
module attributes so tests can point them at temporary files.
"""
from __future__ import annotations

BOT_LOG_PATH = '/opt/trading_2/logs/bot.log'
ENV_PATH = '/opt/trading_2/.env'
AI_COPILOT_STATE_PATH = '/opt/trading_2/ai_copilot_state.json'
PORTFOLIO_STATE_PATH = '/opt/trading_2/.portfolio_state.json'
