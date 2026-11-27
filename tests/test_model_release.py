from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.model_release import (
    EXPECTED_ARTIFACTS,
    promote_release,
    release_directory,
    rollback_release,
    validate_release,
)

FEATURES = ["rsi", "macd"]
LABEL_MAP = {"verkaufen": 0, "halten": 1, "kaufen": 2}
DATA_HASH = "a" * 64


def _validation(*, accuracy: float, macro_f1: float) -> dict[str, float]:
    """Validation block exactly as both trainers emit it."""
    return {
        "rows": 250,
        "accuracy": accuracy,
        "macro_f1": macro_f1,
        "threshold_macro_f1": macro_f1 - 0.02,
        "threshold_coverage": 0.62,
    }


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
        "label_horizon": 6,
        "train_rows": 1000,
        "validation_rows": 250,
        "validation": _validation(accuracy=0.8, macro_f1=0.7),
        "recommended_confidence_threshold": 0.55,
        "recommended_confidence_threshold_stats": {"macro_f1": 0.68, "coverage": 0.62},
        "walk_forward": [
            {"fold": 1, "train_size": 400, "val_size": 120,
             "accuracy": 0.74, "macro_f1": 0.66, "weighted_f1": 0.7},
            {"fold": 2, "train_size": 520, "val_size": 120,
             "accuracy": 0.78, "macro_f1": 0.69, "weighted_f1": 0.72},
        ],
        "walk_forward_summary": {"folds": 2.0, "accuracy": 0.76, "macro_f1": 0.675,
                                 "weighted_f1": 0.71},
    }), encoding="utf-8")
    hailo_metadata = root / "hailo.json"
    hailo_metadata.write_text(json.dumps({
        "model_id": "trading-mlp-h8-v1",
        "feature_contract": "hailo",
        "features": FEATURES,
        "label_map": LABEL_MAP,
        "confidence_threshold": 0.5,
        "data_provenance": {
            "training_data_sha256": data_hash,
            "label_map": LABEL_MAP,
            "label_horizon": 6,
            "train_fraction": 0.8,
            "train_rows": 1000,
            "validation_rows": 250,
        },
        "validation": _validation(accuracy=0.79, macro_f1=0.69),
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


def _manifest(release: Path) -> dict:
    return json.loads((release / "manifest.json").read_text(encoding="utf-8"))


def _rewrite_manifest(release: Path, manifest: dict) -> None:
    (release / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_manifest_carries_full_validation_reports(tmp_path):
    release = _promote(tmp_path / "deploy", "release-1", _inputs(tmp_path / "artifacts"))
    manifest = _manifest(release)

    cpu_report = manifest["validation_reports"]["cpu"]
    assert cpu_report["provenance"]["training_data_sha256"] == DATA_HASH
    assert cpu_report["provenance"]["label_horizon"] == 6
    assert cpu_report["provenance"]["train_rows"] == 1000
    assert cpu_report["confidence_threshold_stats"] == {"macro_f1": 0.68, "coverage": 0.62}
    assert manifest["validation"]["cpu"]["threshold_coverage"] == 0.62

    # Chronological walk-forward evidence travels with the release instead of
    # staying behind in a training-workspace CSV.
    assert [fold["fold"] for fold in cpu_report["walk_forward"]] == [1, 2]
    assert cpu_report["walk_forward"][1]["macro_f1"] == 0.69
    assert cpu_report["walk_forward_summary"]["accuracy"] == 0.76

    hailo_report = manifest["validation_reports"]["hailo"]
    assert hailo_report["model_id"] == "trading-mlp-h8-v1"
    assert hailo_report["confidence_threshold"] == 0.5
    assert hailo_report["provenance"]["train_fraction"] == 0.8
    assert hailo_report["walk_forward"] == []


def test_promotion_requires_validation_metrics_on_both_sides(tmp_path):
    root = tmp_path / "deploy"
    cpu_model, cpu_meta, hef, hailo_meta = _inputs(tmp_path / "artifacts")
    payload = json.loads(cpu_meta.read_text(encoding="utf-8"))
    del payload["validation"]["threshold_coverage"]
    cpu_meta.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="threshold_coverage"):
        promote_release(root, "thin", cpu_model, cpu_meta, hef, hailo_meta)
    assert not (root / "releases" / "thin").exists()
    assert not (root / "active").exists()


def test_promotion_requires_training_provenance(tmp_path):
    root = tmp_path / "deploy"
    cpu_model, cpu_meta, hef, hailo_meta = _inputs(tmp_path / "artifacts")
    payload = json.loads(cpu_meta.read_text(encoding="utf-8"))
    del payload["label_horizon"]
    cpu_meta.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="label_horizon"):
        promote_release(root, "unproven", cpu_model, cpu_meta, hef, hailo_meta)
    assert not (root / "active").exists()


def test_promotion_rejects_malformed_walk_forward_report(tmp_path):
    root = tmp_path / "deploy"
    cpu_model, cpu_meta, hef, hailo_meta = _inputs(tmp_path / "artifacts")
    payload = json.loads(cpu_meta.read_text(encoding="utf-8"))
    payload["walk_forward"] = "fold 1 accuracy 0.74"
    cpu_meta.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="walk-forward"):
        promote_release(root, "broken", cpu_model, cpu_meta, hef, hailo_meta)
    assert not (root / "active").exists()


def test_validate_rejects_manifest_missing_validation_reports(tmp_path):
    release = _promote(tmp_path / "deploy", "release-1", _inputs(tmp_path / "artifacts"))
    manifest = _manifest(release)
    del manifest["validation_reports"]
    _rewrite_manifest(release, manifest)

    with pytest.raises(ValueError, match="validation_reports"):
        validate_release(release)


def test_validate_rejects_manifest_without_validation_evidence(tmp_path):
    release = _promote(tmp_path / "deploy", "release-1", _inputs(tmp_path / "artifacts"))
    manifest = _manifest(release)
    manifest["validation_reports"]["hailo"]["validation"] = {"accuracy": 0.79}
    _rewrite_manifest(release, manifest)

    with pytest.raises(ValueError, match="hailo validation results are missing"):
        validate_release(release)


def test_validate_rejects_manifest_that_disagrees_with_release_contract(tmp_path):
    root = tmp_path / "deploy"
    inputs = _inputs(tmp_path / "artifacts")

    training_hash_release = _promote(root, "training-hash", inputs)
    manifest = _manifest(training_hash_release)
    manifest["validation_reports"]["cpu"]["provenance"]["training_data_sha256"] = "b" * 64
    _rewrite_manifest(training_hash_release, manifest)
    with pytest.raises(ValueError, match="cpu training data checksum does not match"):
        validate_release(training_hash_release)

    label_map_release = _promote(root, "label-map", inputs)
    manifest = _manifest(label_map_release)
    manifest["validation_reports"]["hailo"]["label_map"] = {"halten": 1}
    _rewrite_manifest(label_map_release, manifest)
    with pytest.raises(ValueError, match="hailo label map does not match"):
        validate_release(label_map_release)

    features_release = _promote(root, "features", inputs)
    manifest = _manifest(features_release)
    manifest["validation_reports"]["cpu"]["features"] = ["rsi"]
    _rewrite_manifest(features_release, manifest)
    with pytest.raises(ValueError, match="cpu feature contract does not match"):
        validate_release(features_release)


def test_validate_rejects_release_id_mismatch(tmp_path):
    release = _promote(tmp_path / "deploy", "release-1", _inputs(tmp_path / "artifacts"))
    manifest = _manifest(release)
    manifest["release_id"] = "release-2"
    _rewrite_manifest(release, manifest)

    with pytest.raises(ValueError, match="does not match the"):
        validate_release(release)


def test_validate_rejects_incomplete_or_swapped_artifact_set(tmp_path):
    root = tmp_path / "deploy"
    inputs = _inputs(tmp_path / "artifacts")

    missing_release = _promote(root, "missing", inputs)
    manifest = _manifest(missing_release)
    del manifest["artifacts"]["hailo_hef"]
    _rewrite_manifest(missing_release, manifest)
    with pytest.raises(ValueError, match="missing hailo_hef"):
        validate_release(missing_release)

    swapped_release = _promote(root, "swapped", inputs)
    manifest = _manifest(swapped_release)
    manifest["artifacts"]["hailo_hef"]["path"] = "hailo/other.hef"
    _rewrite_manifest(swapped_release, manifest)
    with pytest.raises(ValueError, match="hailo_hef has an unexpected path"):
        validate_release(swapped_release)

    checksum_release = _promote(root, "checksum", inputs)
    manifest = _manifest(checksum_release)
    manifest["artifacts"]["hailo_metadata"]["sha256"] = "not-a-checksum"
    _rewrite_manifest(checksum_release, manifest)
    with pytest.raises(ValueError, match="hailo_metadata has an invalid checksum"):
        validate_release(checksum_release)

    assert set(EXPECTED_ARTIFACTS) == {
        "cpu_model", "cpu_metadata", "hailo_hef", "hailo_metadata",
    }


def test_release_directory_rejects_invalid_release_id(tmp_path):
    with pytest.raises(ValueError, match="Invalid release ID"):
        release_directory(tmp_path / "deploy", "../outside")
    assert release_directory(tmp_path / "deploy", "release-1") == (
        tmp_path / "deploy" / "releases" / "release-1"
    )
