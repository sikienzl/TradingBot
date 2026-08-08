"""
Trading Bot Health & Metrics HTTP endpoint.

Exposes a simple HTTP server on TRADING_BOT_METRICS_PORT (default 9204) with:
  GET /metrics   – Prometheus text format
  GET /health    – 200 OK / 503 with JSON body

Metrics:
  trading_bot_up                          – 1 when bot is running
  trading_bot_iterations_total            – loop counter
  trading_bot_last_heartbeat_seconds      – unix timestamp of last heartbeat

Usage (standalone, or import and call start_health_server()):
  python -m src.utils.health_endpoint
"""

from __future__ import annotations

import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

logger = logging.getLogger(__name__)

# --- State shared with the trading bot loop ---
_state: dict = {
    "up": 1,
    "iterations": 0,
    "last_heartbeat": time.time(),
    "start_time": time.time(),
}


def heartbeat() -> None:
    """Call this from the main trading loop on every iteration."""
    _state["last_heartbeat"] = time.time()
    _state["iterations"] += 1


def mark_down() -> None:
    """Call when the bot shuts down or errors out."""
    _state["up"] = 0


def _prometheus_text() -> str:
    """Render metrics in Prometheus text exposition format."""
    now = time.time()
    lines = [
        "# HELP trading_bot_up 1 if the trading bot main loop is running.",
        "# TYPE trading_bot_up gauge",
        f"trading_bot_up {_state['up']}",
        "",
        "# HELP trading_bot_iterations_total Total trading loop iterations since start.",
        "# TYPE trading_bot_iterations_total counter",
        f"trading_bot_iterations_total {_state['iterations']}",
        "",
        "# HELP trading_bot_last_heartbeat_seconds Unix timestamp of last heartbeat.",
        "# TYPE trading_bot_last_heartbeat_seconds gauge",
        f"trading_bot_last_heartbeat_seconds {_state['last_heartbeat']:.3f}",
        "",
        "# HELP trading_bot_uptime_seconds Seconds since bot process started.",
        "# TYPE trading_bot_uptime_seconds gauge",
        f"trading_bot_uptime_seconds {now - _state['start_time']:.1f}",
    ]
    return "\n".join(lines) + "\n"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:  # silence access log
        pass

    def do_GET(self) -> None:
        if self.path == "/metrics":
            body = _prometheus_text().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path in ("/health", "/healthz", "/livez"):
            staleness = time.time() - _state["last_heartbeat"]
            max_stale = float(os.getenv("HEALTH_STALE_THRESHOLD_SECONDS", "120"))
            healthy = _state["up"] == 1 and staleness < max_stale
            payload = json.dumps(
                {
                    "up": bool(_state["up"]),
                    "iterations": _state["iterations"],
                    "last_heartbeat_age_seconds": round(staleness, 1),
                    "healthy": healthy,
                }
            ).encode()
            code = 200 if healthy else 503
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        else:
            self.send_response(404)
            self.end_headers()


def start_health_server(port: int | None = None, daemon: bool = True) -> Thread:
    """
    Start the health/metrics server in a background daemon thread.

    Returns the Thread so callers can join() if needed.
    """
    port = port or int(os.getenv("TRADING_BOT_METRICS_PORT", "9204"))
    server = HTTPServer(("0.0.0.0", port), _Handler)
    thread = Thread(target=server.serve_forever, daemon=daemon, name="health-server")
    thread.start()
    logger.info("Health endpoint listening on :%d  /health  /metrics", port)
    return thread


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    t = start_health_server(daemon=False)
    t.join()
