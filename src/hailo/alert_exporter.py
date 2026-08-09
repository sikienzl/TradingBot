"""
Hailo Alert Exporter

Reads /tmp/hailo_alerts.jsonl and exposes Prometheus metrics:
  - hailo_score (latest score per coin)
  - hailo_inference_latency_seconds (latency per coin)
  - hailo_alert_total (total alerts per signal_type)

Usage:
  python3 -m src.hailo.alert_exporter
  # Metrics available at http://localhost:9205/metrics
"""

import json
import logging
import os
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

logger = logging.getLogger(__name__)

# Prometheus metrics storage
_metrics: dict[str, Any] = {
    "hailo_score_latest": {},  # {coin: score}
    "hailo_signal_type_latest": {},  # {coin: signal_type}
    "hailo_latency_latest": {},  # {coin: latency_ms}
    "hailo_alert_total": {},  # {signal_type: count}
    "hailo_alerts_read": 0,
    "last_update": 0.0,
}


def _load_alerts(alerts_file: str = "/tmp/hailo_alerts.jsonl") -> None:
    """Load alerts from JSONL file and update metrics."""
    global _metrics
    
    try:
        p = Path(alerts_file)
        if not p.exists():
            logger.debug(f"Alerts file not found: {alerts_file}")
            return
        
        content = p.read_text(encoding="utf-8", errors="ignore")
        lines = [line.strip() for line in content.split("\n") if line.strip()]
        
        # Reset metrics
        _metrics["hailo_score_latest"].clear()
        _metrics["hailo_signal_type_latest"].clear()
        _metrics["hailo_latency_latest"].clear()
        _metrics["hailo_alert_total"].clear()
        
        for line in lines:
            try:
                alert = json.loads(line)
                coin = alert.get("coin", "")
                score = float(alert.get("hailo_score", 0))
                signal_type = alert.get("signal_type", "unknown")
                
                # Track latest score per coin
                _metrics["hailo_score_latest"][coin] = score
                _metrics["hailo_signal_type_latest"][coin] = signal_type
                
                # Extract latency if available
                if "inference_latency_ms" in alert:
                    _metrics["hailo_latency_latest"][coin] = float(alert["inference_latency_ms"])
                
                # Track signal type totals
                if signal_type not in _metrics["hailo_alert_total"]:
                    _metrics["hailo_alert_total"][signal_type] = 0
                _metrics["hailo_alert_total"][signal_type] += 1
                
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                logger.debug(f"Failed to parse alert: {e}")
                continue
        
        _metrics["hailo_alerts_read"] = len(lines)
        _metrics["last_update"] = time.time()
        logger.info(f"Loaded {len(lines)} Hailo alerts from {alerts_file}")
        
    except (OSError, IOError) as e:
        logger.warning(f"Could not read alerts file: {e}")


def _prometheus_format() -> str:
    """Generate Prometheus text format metrics."""
    lines = [
        "# HELP hailo_score Latest anomaly score (0-100) for each coin",
        "# TYPE hailo_score gauge",
    ]
    
    for coin, score in _metrics["hailo_score_latest"].items():
        lines.append(f'hailo_score{{coin="{coin}"}} {score}')
    
    lines.extend([
        "",
        "# HELP hailo_inference_latency_seconds Inference latency in seconds for each coin",
        "# TYPE hailo_inference_latency_seconds gauge",
    ])
    
    for coin, latency_ms in _metrics["hailo_latency_latest"].items():
        latency_sec = latency_ms / 1000.0
        lines.append(f'hailo_inference_latency_seconds{{coin="{coin}"}} {latency_sec}')
    
    lines.extend([
        "",
        "# HELP hailo_alert_total Total number of alerts by signal type",
        "# TYPE hailo_alert_total counter",
    ])
    
    for signal_type, count in _metrics["hailo_alert_total"].items():
        lines.append(f'hailo_alert_total{{signal_type="{signal_type}"}} {count}')
    
    lines.extend([
        "",
        "# HELP hailo_alerts_loaded Total alerts loaded from file",
        "# TYPE hailo_alerts_loaded gauge",
        f"hailo_alerts_loaded {_metrics['hailo_alerts_read']}",
        "",
        "# HELP hailo_exporter_last_update Unix timestamp of last metrics update",
        "# TYPE hailo_exporter_last_update gauge",
        f"hailo_exporter_last_update {_metrics['last_update']}",
    ])
    
    return "\n".join(lines) + "\n"


class _AlertHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        pass  # Suppress access logs
    
    def do_GET(self) -> None:
        if self.path == "/metrics":
            _load_alerts()  # Refresh metrics on each scrape
            body = _prometheus_format().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/health", "/healthz"):
            health = json.dumps({
                "status": "healthy",
                "alerts_loaded": _metrics["hailo_alerts_read"],
                "last_update": _metrics["last_update"],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(health)))
            self.end_headers()
            self.wfile.write(health)
        else:
            self.send_response(404)
            self.end_headers()


def start_exporter(port: int = 9205, alerts_file: str = "/tmp/hailo_alerts.jsonl") -> None:
    """Start the Hailo alert exporter HTTP server."""
    server = HTTPServer(("0.0.0.0", port), _AlertHandler)
    logger.info(f"Hailo alert exporter listening on 0.0.0.0:{port}")
    logger.info(f"Metrics available at http://localhost:{port}/metrics")
    server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    port = int(os.getenv("HAILO_EXPORTER_PORT", "9205"))
    alerts_file = os.getenv("AI_COPILOT_HAILO_ALERTS_FILE", "/tmp/hailo_alerts.jsonl")
    start_exporter(port=port, alerts_file=alerts_file)
