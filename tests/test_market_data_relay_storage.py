from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "k8s" / "overlays" / "market-data-relay"


def test_relay_uses_single_writer_persistent_ssd_claim():
    deployment = yaml.safe_load((OVERLAY / "deployment-relay.yaml").read_text())
    resources = list(yaml.safe_load_all((OVERLAY / "tick-storage.yaml").read_text()))
    storage_class, claim = resources
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]

    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert pod["nodeSelector"]["trading-role"] == "master-ssd"
    assert {"name": "HAILO8_LOCAL_TICK_PATH", "value": "/var/lib/trading-ticks"} in container["env"]
    assert {"name": "tick-storage", "mountPath": "/var/lib/trading-ticks"} in container["volumeMounts"]
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == claim["metadata"]["name"]
    assert claim["spec"]["storageClassName"] == storage_class["metadata"]["name"]
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert claim["spec"]["resources"]["requests"]["storage"] == "20Gi"
    assert storage_class["reclaimPolicy"] == "Retain"
    assert storage_class["volumeBindingMode"] == "WaitForFirstConsumer"
    assert storage_class["parameters"]["nodePath"] == "/mnt/nvme_data/trading_db"
    kustomization = yaml.safe_load((OVERLAY / "kustomization.yaml").read_text())
    assert "tick-storage.yaml" in kustomization["resources"]
