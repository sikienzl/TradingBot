import hashlib
import io
import json
import socket
import sys
from pathlib import Path
from typing import ClassVar
from urllib.error import URLError

import pytest
import yaml

from scripts import download_onnx_model as downloader


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_main_reuses_existing_model_without_url(tmp_path, monkeypatch):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"existing")
    monkeypatch.setattr(downloader, "_load_dotenv", lambda: None)
    monkeypatch.delenv("MODEL_DOWNLOAD_URL", raising=False)
    monkeypatch.setenv("HAILO8_MODEL_PATH", str(model))
    monkeypatch.setenv("MODEL_DOWNLOAD_SHA256", _digest(b"existing"))
    monkeypatch.setattr(sys, "argv", ["download_onnx_model.py"])

    downloader.main()
    assert model.read_bytes() == b"existing"


@pytest.mark.parametrize("force,checksum", [(False, None), (True, None), (False, _digest(b"other"))])
def test_missing_url_when_download_needed_keeps_existing_file(tmp_path, force, checksum):
    model = tmp_path / "model.onnx"
    if force or checksum:
        model.write_bytes(b"existing")

    with pytest.raises(SystemExit):
        downloader.download_model("", model, expected_sha256=checksum, force=force)

    if force or checksum:
        assert model.read_bytes() == b"existing"
    else:
        assert not model.exists()


def test_file_download_and_checksum(tmp_path):
    source = tmp_path / "source.onnx"
    source.write_bytes(b"model")
    target = tmp_path / "models" / "model.onnx"
    old_socket_timeout = socket.getdefaulttimeout()

    result = downloader.download_model(source.as_uri(), target, _digest(b"model"))

    assert result == target.resolve()
    assert target.read_bytes() == b"model"
    assert socket.getdefaulttimeout() == old_socket_timeout
    assert list(target.parent.iterdir()) == [target]


def test_checksum_mismatch_on_existing_file_downloads_replacement(tmp_path):
    source = tmp_path / "source.onnx"
    source.write_bytes(b"new")
    target = tmp_path / "model.onnx"
    target.write_bytes(b"old")

    downloader.download_model(source.as_uri(), target, _digest(b"new"))
    assert target.read_bytes() == b"new"


def test_existing_unverified_file_is_reused_without_url(tmp_path, caplog):
    target = tmp_path / "model.onnx"
    target.write_bytes(b"existing")

    downloader.download_model("", target)
    assert target.read_bytes() == b"existing"
    assert "without verifying" in caplog.text


def test_checksum_mismatch_preserves_target_and_unrelated_tmp(tmp_path):
    source = tmp_path / "source.onnx"
    source.write_bytes(b"new")
    target = tmp_path / "model.onnx"
    target.write_bytes(b"old")
    other_tmp = tmp_path / "model.tmp"
    other_tmp.write_bytes(b"unrelated")

    with pytest.raises(SystemExit):
        downloader.download_model(source.as_uri(), target, _digest(b"wrong"), force=True)
    assert target.read_bytes() == b"old"
    assert other_tmp.read_bytes() == b"unrelated"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["model.onnx", "model.tmp", "source.onnx"]


@pytest.mark.parametrize("kwargs", [
    {"expected_sha256": "bad"}, {"retries": 0}, {"timeout": 0},
])
def test_invalid_settings_do_not_touch_existing_model(tmp_path, kwargs):
    target = tmp_path / "model.onnx"
    target.write_bytes(b"old")
    with pytest.raises(SystemExit):
        downloader.download_model("file:///missing", target, force=True, **kwargs)
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_retry_cleanup_and_url_not_logged(tmp_path, monkeypatch, caplog):
    target = tmp_path / "model.onnx"
    target.write_bytes(b"old")
    attempts = []

    def fail(url, path, timeout):
        attempts.append(path)
        path.write_bytes(b"partial")
        raise URLError(url)

    monkeypatch.setattr(downloader, "_download_with_progress", fail)
    monkeypatch.setattr(downloader.time, "sleep", lambda _: None)
    url = "https://user:password@example.com/model?token=secret"
    with pytest.raises(SystemExit):
        downloader.download_model(url, target, retries=2, force=True)
    assert len(set(attempts)) == 2
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]
    assert "password" not in caplog.text and "secret" not in caplog.text


def test_short_response_is_not_installed(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        headers: ClassVar[dict[str, str]] = {"Content-Length": "10"}

    seen = []

    def open_url(request, timeout):
        seen.append((request.get_header("User-agent"), timeout))
        return Response(b"short")

    monkeypatch.setattr(downloader.urllib.request, "urlopen", open_url)
    target = tmp_path / "model.onnx"
    target.write_bytes(b"old")
    with pytest.raises(SystemExit):
        downloader.download_model("https://example.com/model", target, retries=1, timeout=7, force=True)
    assert seen == [("TradingBot/1.0 model-downloader", 7)]
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_overlay_model_paths_and_runtime_mount_contract():
    root = Path(__file__).resolve().parents[1]
    overlay = root / "k8s/overlays/hailo-worker"
    manifest = yaml.safe_load((overlay / "daemonset-hailo.yaml").read_text())
    config = yaml.safe_load((root / "k8s/base/configmap-hybrid.yaml").read_text())["data"]
    pod = manifest["spec"]["template"]["spec"]
    init = pod["initContainers"][0]
    worker = pod["containers"][0]
    for container in (init, worker):
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert env["HAILO8_MODEL_PATH"] == config["HAILO8_MODEL_PATH"]
        assert any(m["name"] == "onnx-model-storage" and m["mountPath"] == "/models"
                   for m in container["volumeMounts"])
    assert config["HAILO8_MODEL_CONFIG_PATH"] == "/app/model/hailo_prefilter/model_config.json"
    image_config = json.loads((root / "model/hailo_prefilter/model_config.json").read_text())
    assert image_config.get("preprocessing_version", "legacy") == "legacy"
    volumes = {volume["name"]: volume["hostPath"] for volume in pod["volumes"] if "hostPath" in volume}
    mounts = {mount["name"]: mount for mount in worker["volumeMounts"]}
    paths = {
        "host-hailo-platform": ("/usr/lib/python3/dist-packages/hailo_platform", "Directory",
                                "/host_hailo/hailo_platform"),
        "host-libhailort-soname": ("/lib/libhailort.so.4.23.0", "File", "/lib/libhailort.so.4.23.0"),
        "host-libhailort-link": ("/lib/libhailort.so", "File", "/lib/libhailort.so"),
    }
    for name, (host_path, path_type, mount_path) in paths.items():
        assert volumes[name] == {"path": host_path, "type": path_type}
        assert mounts[name]["mountPath"] == mount_path
        assert mounts[name]["readOnly"] is True
    assert volumes["hailo-device"] == {"path": "/dev/hailo0", "type": "CharDevice"}
