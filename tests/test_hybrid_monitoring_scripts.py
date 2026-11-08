import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_hybrid_monitoring_configures_prometheus_without_grafana():
    script = (ROOT / "scripts/rollout_hybrid_cluster.sh").read_text()
    functions = script.split("reload_monitoring() {", 1)[1].split("show_status() {", 1)[0]
    harness = """
NODE1_IP=test-node
APP_DIR=/app
KUBE_NAMESPACE=trading-bot
HAILO_LABEL_VALUE=hailo-worker
info() { :; }
remote_sudo() { printf '%s\\n' "$*"; }
reload_monitoring() {
""" + functions + """
reload_monitoring
verify_monitoring
"""
    result = subprocess.run(
        ["bash", "-c", harness], check=True, capture_output=True, text=True
    )
    assert "systemctl restart prometheus" in result.stdout
    assert "render_cluster_prometheus_config.py" in result.stdout
    assert "trading_hybrid_edge_filter" in result.stdout
    assert "trading_hybrid_strategist" in result.stdout
    assert "trading_hybrid_market_relay" in result.stdout
    assert "grafana" not in result.stdout.lower()
    assert "127.0.0.1:3000" not in result.stdout


def test_host_monitoring_installer_keeps_prometheus_and_leaves_grafana_alone():
    script = (ROOT / "scripts/install_monitoring_host.sh").read_text()
    assert "prometheus-node-exporter" in script
    assert "systemctl restart prometheus" in script
    assert "grafana" not in script.lower()
