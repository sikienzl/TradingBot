"""Immutable paired CPU/Hailo model releases with atomic activation and rollback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for block in iter(lambda: artifact.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_metadata(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as metadata_file:
        data = json.load(metadata_file)
    if not isinstance(data, dict):
        raise TypeError(f"Model metadata must be a JSON object: {path}")
    return data


def _training_hash(metadata: dict[str, Any]) -> str:
    value = metadata.get("training_data_sha256")
    if value is None:
        value = metadata.get("data_provenance", {}).get("training_data_sha256")
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("Both model metadata files must include a SHA-256 training_data_sha256")
    return value


def _validate_contract(cpu_metadata: dict[str, Any], hailo_metadata: dict[str, Any]) -> None:
    if cpu_metadata.get("feature_contract") != "hailo":
        raise ValueError("CPU artifact was not trained with the fixed Hailo feature contract")
    if hailo_metadata.get("feature_contract") != "hailo":
        raise ValueError("Hailo artifact has an invalid feature contract")
    if list(cpu_metadata.get("features", [])) != list(hailo_metadata.get("features", [])):
        raise ValueError("CPU and Hailo feature contracts do not match")
    if cpu_metadata.get("label_map") != hailo_metadata.get("label_map"):
        raise ValueError("CPU and Hailo label contracts do not match")
    if _training_hash(cpu_metadata) != _training_hash(hailo_metadata):
        raise ValueError("CPU and Hailo artifacts were not trained from the same data")
    for label, metadata in (("CPU", cpu_metadata), ("Hailo", hailo_metadata)):
        validation = metadata.get("validation")
        if not isinstance(validation, dict) or not validation:
            raise ValueError(f"{label} artifact is missing validation results")


def _artifact(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise ValueError(f"{label} artifact must be a non-empty file: {path}")
    return resolved


def promote_release(
    release_root: str | Path,
    release_id: str,
    cpu_model: str | Path,
    cpu_metadata: str | Path,
    hailo_hef: str | Path,
    hailo_metadata: str | Path,
) -> Path:
    if not _RELEASE_ID.fullmatch(release_id):
        raise ValueError("Invalid release ID")
    root = Path(release_root).resolve()
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    final = releases / release_id
    if final.exists():
        raise FileExistsError(f"Model release already exists: {release_id}")

    cpu_model_path = _artifact(Path(cpu_model), "CPU model")
    cpu_metadata_path = _artifact(Path(cpu_metadata), "CPU metadata")
    hef_path = _artifact(Path(hailo_hef), "Hailo HEF")
    hailo_metadata_path = _artifact(Path(hailo_metadata), "Hailo metadata")
    cpu_meta = _read_metadata(cpu_metadata_path)
    hailo_meta = _read_metadata(hailo_metadata_path)
    _validate_contract(cpu_meta, hailo_meta)

    staging = Path(tempfile.mkdtemp(prefix=f".{release_id}-", dir=releases))
    try:
        (staging / "cpu").mkdir()
        (staging / "hailo").mkdir()
        shutil.copy2(cpu_model_path, staging / "cpu" / "catboost_model.cbm")
        shutil.copy2(cpu_metadata_path, staging / "cpu" / "metadata.json")
        shutil.copy2(hef_path, staging / "hailo" / "model.hef")
        shutil.copy2(hailo_metadata_path, staging / "hailo" / "metadata.json")
        manifest = {
            "release_id": release_id,
            "feature_contract": "hailo",
            "features": list(cpu_meta["features"]),
            "label_map": cpu_meta["label_map"],
            "training_data_sha256": _training_hash(cpu_meta),
            "validation": {
                "cpu": cpu_meta["validation"],
                "hailo": hailo_meta["validation"],
            },
            "artifacts": {
                "cpu_model": {"path": "cpu/catboost_model.cbm", "sha256": sha256_file(staging / "cpu" / "catboost_model.cbm")},
                "cpu_metadata": {"path": "cpu/metadata.json", "sha256": sha256_file(staging / "cpu" / "metadata.json")},
                "hailo_hef": {"path": "hailo/model.hef", "sha256": sha256_file(staging / "hailo" / "model.hef")},
                "hailo_metadata": {"path": "hailo/metadata.json", "sha256": sha256_file(staging / "hailo" / "metadata.json")},
            },
        }
        manifest_path = staging / "manifest.json"
        with manifest_path.open("w", encoding="utf-8") as manifest_file:
            json.dump(manifest, manifest_file, ensure_ascii=True, indent=2, sort_keys=True)
            manifest_file.write("\n")
            manifest_file.flush()
            os.fsync(manifest_file.fileno())
        _sync_directory(staging)
        os.replace(staging, final)
        _sync_directory(releases)
        _activate(root, release_id)
        return final
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def validate_release(release_path: str | Path) -> dict[str, Any]:
    release = Path(release_path).resolve(strict=True)
    with (release / "manifest.json").open("r", encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("artifacts"), dict):
        raise TypeError("Invalid model release manifest")
    for artifact in manifest["artifacts"].values():
        relative = Path(artifact["path"])
        path = (release / relative).resolve(strict=True)
        if release not in path.parents or not path.is_file():
            raise ValueError("Model release artifact path escapes the release directory")
        if sha256_file(path) != artifact.get("sha256"):
            raise ValueError(f"Model artifact checksum mismatch: {relative}")
    return manifest


def rollback_release(release_root: str | Path, release_id: str) -> Path:
    if not _RELEASE_ID.fullmatch(release_id):
        raise ValueError("Invalid release ID")
    root = Path(release_root).resolve()
    release = root / "releases" / release_id
    validate_release(release)
    _activate(root, release_id)
    return release


def _activate(root: Path, release_id: str) -> None:
    active = root / "active"
    if os.path.lexists(active) and not active.is_symlink():
        raise RuntimeError("Refusing to replace a non-symlink active model path")
    temporary_link = root / f".active-{os.getpid()}"
    if os.path.lexists(temporary_link):
        temporary_link.unlink()
    os.symlink(Path("releases") / release_id, temporary_link)
    os.replace(temporary_link, active)
    _sync_directory(root)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)