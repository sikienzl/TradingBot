from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.model_release import promote_release, rollback_release, validate_release

FEATURES = ["rsi", "macd"]
LABEL_MAP = {"verkaufen": 0, "halten": 1, "kaufen": 2}
DATA_HASH = "a" * 64


def _inputs(root: Path, *, cpu_features=None, data_hash=DATA_HASH):
    root.mkdir(parents=True, exist_ok=True)
    cpu_model = root / "cpu.cbm"
    cpu_model.write_bytes(b"cpu model")
    hef = root / "model.hef"
    hef.write_bytes(b"hailo model")
    cpu_metadata = root / "cpu.json"
    cpu_metadata.write_text(json.dumps({
        "feature_contract": "hailo",
        "features": cpu_features or FEATURES,
        "label_map": LABEL_MAP,
        "training_data_sha256": data_hash,
        "validation": {"accuracy": 0.8, "macro_f1": 0.7},
    }), encoding="utf-8")
    hailo_metadata = root / "hailo.json"
    hailo_metadata.write_text(json.dumps({
        "feature_contract": "hailo",
        "features": FEATURES,
        "label_map": LABEL_MAP,
        "data_provenance": {"training_data_sha256": data_hash},
        "validation": {"accuracy": 0.79, "macro_f1": 0.69},
    }), encoding="utf-8")
    return cpu_model, cpu_metadata, hef, hailo_metadata


def _promote(release_root, release_id, inputs):
    return promote_release(release_root, release_id, *inputs)


def test_promotes_immutable_pair_and_rolls_back(tmp_path):
    inputs = _inputs(tmp_path / "artifacts")
    root = tmp_path / "deploy"

    first = _promote(root, "release-1", inputs)
    (tmp_path / "artifacts" / "cpu.cbm").write_bytes(b"replacement")
    second_inputs = _inputs(tmp_path / "artifacts-2")
    second = _promote(root, "release-2", second_inputs)

    assert (root / "active").resolve() == second.resolve()
    assert validate_release(first)["release_id"] == "release-1"
    rollback_release(root, "release-1")
    assert (root / "active").resolve() == first.resolve()
    assert (first / "cpu" / "catboost_model.cbm").read_bytes() == b"cpu model"


def test_rejects_contract_mismatch_without_changing_active_release(tmp_path):
    root = tmp_path / "deploy"
    valid = _inputs(tmp_path / "valid")
    first = _promote(root, "good", valid)
    invalid = _inputs(tmp_path / "invalid", cpu_features=["rsi"], data_hash="b" * 64)

    with pytest.raises(ValueError, match="feature contracts do not match"):
        _promote(root, "bad", invalid)

    assert (root / "active").resolve() == first.resolve()
    assert not (root / "releases" / "bad").exists()


def test_rejects_training_data_hash_mismatch(tmp_path):
    root = tmp_path / "deploy"
    cpu_model, cpu_meta, hef, hailo_meta = _inputs(tmp_path / "artifacts")
    hailo_payload = json.loads(hailo_meta.read_text(encoding="utf-8"))
    hailo_payload["data_provenance"]["training_data_sha256"] = "b" * 64
    hailo_meta.write_text(json.dumps(hailo_payload), encoding="utf-8")

    with pytest.raises(ValueError, match="same data"):
        promote_release(root, "mismatch", cpu_model, cpu_meta, hef, hailo_meta)

    assert not (root / "active").exists()


def test_detects_artifact_corruption_and_refuses_rollback(tmp_path):
    root = tmp_path / "deploy"
    release = _promote(root, "release-1", _inputs(tmp_path / "artifacts"))
    (release / "hailo" / "model.hef").write_bytes(b"corrupt")

    with pytest.raises(ValueError, match="checksum mismatch"):
        rollback_release(root, "release-1")


def test_refuses_to_replace_non_symlink_active_path(tmp_path):
    root = tmp_path / "deploy"
    root.mkdir()
    (root / "active").mkdir()

    with pytest.raises(RuntimeError, match="non-symlink"):
        _promote(root, "release-1", _inputs(tmp_path / "artifacts"))
