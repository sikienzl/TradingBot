from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_deployment_is_loopback_only_and_uses_external_secret():
    overlay = ROOT / "k8s" / "overlays" / "trading-dashboard"
    kustomization = yaml.safe_load((overlay / "kustomization.yaml").read_text())
    assert "deployment-dashboard.yaml" in kustomization["resources"]
    assert not any("service" in item.lower() for item in kustomization["resources"])
    deployment = yaml.safe_load((overlay / "deployment-dashboard.yaml").read_text())
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False
    assert container["envFrom"] == [{"secretRef": {"name": "trading-dashboard-credentials"}}]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    dockerfile = (ROOT / "docker" / "trading-dashboard.Dockerfile").read_text()
    assert '"--host", "127.0.0.1"' in dockerfile
