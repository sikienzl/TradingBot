#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import sys


def _run_json(*args: str) -> dict:
    output = subprocess.check_output(args, text=True)
    return json.loads(output)


def _service_cluster_ip(namespace: str, service_name: str) -> str:
    data = _run_json("kubectl", "-n", namespace, "get",
                     "svc", service_name, "-o", "json")
    cluster_ip = data["spec"].get("clusterIP")
    if not cluster_ip or cluster_ip == "None":
        raise RuntimeError(f"service {service_name} has no clusterIP")
    return cluster_ip


def _node_internal_ip(node_name: str) -> str:
    data = _run_json("kubectl", "get", "node", node_name, "-o", "json")
    for address in data["status"].get("addresses", []):
        if address.get("type") == "InternalIP":
            return address["address"]
    raise RuntimeError(f"node {node_name} has no InternalIP")


def render(namespace: str, worker_node_name: str) -> str:
    edge_ip = _service_cluster_ip(namespace, "trading-hailo-worker")
    strategist_ip = _service_cluster_ip(namespace, "cloud-strategist")
    relay_ip = _service_cluster_ip(namespace, "trading-market-data-relay")
    worker_ip = _node_internal_ip(worker_node_name)

    return f"""global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: \"prometheus\"
    static_configs:
      - targets: [\"localhost:9090\"]

  - job_name: \"trading_pnl_exporter\"
    scrape_interval: 30s
    scrape_timeout: 25s
    static_configs:
      - targets: [\"localhost:9200\"]

  - job_name: \"trading_node_exporter\"
    static_configs:
      - targets:
          - \"localhost:9100\"
          - \"{worker_ip}:9100\"

  - job_name: \"trading_hybrid_edge_filter\"
    scrape_interval: 15s
    static_configs:
      - targets: [\"{edge_ip}:9201\"]

  - job_name: \"trading_hybrid_strategist\"
    scrape_interval: 15s
    static_configs:
      - targets: [\"{strategist_ip}:9202\"]

  - job_name: \"trading_hybrid_market_relay\"
    scrape_interval: 15s
    static_configs:
      - targets: [\"{relay_ip}:9203\"]

rule_files:
  - /etc/prometheus/rules/trading-alerts.yml
"""


def main() -> int:
    namespace = sys.argv[1] if len(sys.argv) > 1 else "trading-bot"
    worker_node_name = sys.argv[2] if len(sys.argv) > 2 else "raspi-cm5-node-2"
    sys.stdout.write(render(namespace, worker_node_name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
