"""Immutable paired CPU/Hailo model releases with atomic activation and rollback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")

# The paired artifact set a release must contain, as staged inside the release
# directory. Both release profiles load exactly these four files, so a manifest
# that names anything else is not a release this repository can activate.
EXPECTED_ARTIFACTS: dict[str, str] = {
    "cpu_model": "cpu/catboost_model.cbm",
    "cpu_metadata": "cpu/metadata.json",
    "hailo_hef": "hailo/model.hef",
    "hailo_metadata": "hailo/metadata.json",
}

# Keys the release CLI and the runtime need in order to describe an active
# release without opening the per-side metadata files again.
REQUIRED_MANIFEST_KEYS: tuple[str, ...] = (
    "release_id",
    "feature_contract",
    "features",
    "label_map",
    "training_data_sha256",
    "validation",
    "validation_reports",
    "artifacts",
)

# Every training run in this repository reports these metrics on its validation
# fold, so a report without them is not evidence a release can be judged on.
REQUIRED_VALIDATION_METRICS: tuple[str, ...] = (
    "rows",
    "accuracy",
    "macro_f1",
    "threshold_macro_f1",
    "threshold_coverage",
)

# Provenance the paired-artifact contract is proven from. ``training_data_sha256``
# is checked separately because the CPU metadata keeps it at the top level.
PROVENANCE_KEYS: tuple[str, ...] = (
    "label_map",
    "label_horizon",
    "train_rows",
    "validation_rows",
)


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
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError("Both model metadata files must include a SHA-256 training_data_sha256")
    return value


def _feature_list(metadata: dict[str, Any], label: str) -> list[str]:
    """Feature contract of one side as a list of non-empty names."""
    features = metadata.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError(f"{label} artifact is missing its feature list")
    if not all(isinstance(feature, str) and feature for feature in features):
        raise ValueError(f"{label} artifact has an invalid feature list")
    return list(features)


def _label_map(metadata: dict[str, Any], label: str) -> dict[str, Any]:
    """Label contract of one side as a non-empty mapping."""
    label_map = metadata.get("label_map")
    if not isinstance(label_map, dict) or not label_map:
        raise ValueError(f"{label} artifact is missing its label map")
    return label_map


def _validation_report(metadata: dict[str, Any], label: str) -> dict[str, Any]:
    """Validation report of one training side, with its required metrics.

    Promotion is the only place a candidate pair is judged as a pair, so the
    report that gets baked into the release manifest has to be checked here
    rather than discovered later by an operator running ``validate``.
    """
    validation = metadata.get("validation")
    if not isinstance(validation, dict) or not validation:
        raise ValueError(f"{label} artifact is missing validation results")
    missing = [metric for metric in REQUIRED_VALIDATION_METRICS if metric not in validation]
    if missing:
        raise ValueError(
            f"{label} artifact validation results are missing: {', '.join(missing)}"
        )
    return validation


def _provenance(metadata: dict[str, Any], label: str) -> dict[str, Any]:
    """Training provenance of one side, normalised to one shape.

    The CatBoost trainer keeps provenance at the top level of its metadata while
    the Hailo trainer nests it under ``data_provenance``. The manifest stores one
    shape per side so a reviewer can compare both sides key by key.
    """
    raw = metadata.get("data_provenance")
    provenance: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    for key in ("label_map", "label_horizon", "train_rows", "validation_rows"):
        if key not in provenance and key in metadata:
            provenance[key] = metadata[key]
    provenance["training_data_sha256"] = _training_hash(metadata)
    missing = [key for key in PROVENANCE_KEYS if key not in provenance]
    if missing:
        raise ValueError(
            f"{label} artifact is missing training provenance: {', '.join(missing)}"
        )
    return provenance


def _walk_forward(metadata: dict[str, Any], label: str) -> list[dict[str, Any]]:
    """Walk-forward folds of one side, when the trainer recorded them.

    Chronological walk-forward evaluation is the only evidence in this repository
    that a time-series model generalises forward, so the manifest carries the
    per-fold metrics instead of leaving them in a training-workspace CSV.
    """
    folds = metadata.get("walk_forward")
    if folds is None:
        return []
    if not isinstance(folds, list) or not all(isinstance(fold, dict) for fold in folds):
        raise ValueError(f"{label} artifact has an invalid walk-forward report")
    return [dict(fold) for fold in folds]


def _report_side(
    metadata: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    """Full validation record of one side: metrics, provenance and thresholds.

    ``validation`` alone is only five numbers; the release manifest keeps the
    whole record so a promotion can be re-examined - or an old release rolled
    back to - long after the training workspace is gone.
    """
    side: dict[str, Any] = {
        "feature_contract": metadata.get("feature_contract"),
        "features": _feature_list(metadata, label),
        "label_map": _label_map(metadata, label),
        "validation": _validation_report(metadata, label),
        "provenance": _provenance(metadata, label),
        "walk_forward": _walk_forward(metadata, label),
    }
    for key in ("model_id", "confidence_threshold", "recommended_confidence_threshold"):
        value = metadata.get(key)
        if value is not None:
            side[key] = value
    stats = metadata.get("recommended_confidence_threshold_stats")
    if isinstance(stats, dict) and stats:
        side["confidence_threshold_stats"] = stats
    summary = metadata.get("walk_forward_summary")
    if isinstance(summary, dict) and summary:
        side["walk_forward_summary"] = summary
    return side


def _validate_contract(cpu_metadata: dict[str, Any], hailo_metadata: dict[str, Any]) -> None:
    if cpu_metadata.get("feature_contract") != "hailo":
        raise ValueError("CPU artifact was not trained with the fixed Hailo feature contract")
    if hailo_metadata.get("feature_contract") != "hailo":
        raise ValueError("Hailo artifact has an invalid feature contract")
    if _feature_list(cpu_metadata, "CPU") != _feature_list(hailo_metadata, "Hailo"):
        raise ValueError("CPU and Hailo feature contracts do not match")
    if _label_map(cpu_metadata, "CPU") != _label_map(hailo_metadata, "Hailo"):
        raise ValueError("CPU and Hailo label contracts do not match")
    if _training_hash(cpu_metadata) != _training_hash(hailo_metadata):
        raise ValueError("CPU and Hailo artifacts were not trained from the same data")
    for label, metadata in (("CPU", cpu_metadata), ("Hailo", hailo_metadata)):
        _validation_report(metadata, label)


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
            "features": _feature_list(cpu_meta, "CPU"),
            "label_map": _label_map(cpu_meta, "CPU"),
            "training_data_sha256": _training_hash(cpu_meta),
            "validation": {
                "cpu": _validation_report(cpu_meta, "CPU"),
                "hailo": _validation_report(hailo_meta, "Hailo"),
            },
            # The full reports behind those numbers: provenance, thresholds and
            # walk-forward folds, kept per side so a release can be re-judged
            # without the training workspace that produced it.
            "validation_reports": {
                "cpu": _report_side(cpu_meta, "CPU"),
                "hailo": _report_side(hailo_meta, "Hailo"),
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


def _validate_manifest_release_id(manifest: dict[str, Any], release: Path) -> None:
    """Reject a manifest whose release ID cannot be trusted to name the release."""
    release_id = manifest["release_id"]
    if not isinstance(release_id, str) or not _RELEASE_ID.fullmatch(release_id):
        raise ValueError("Model release manifest has an invalid release ID")
    if release_id != release.name:
        raise ValueError(
            f"Model release manifest ID {release_id} does not match the "
            f"release directory {release.name}"
        )


def _validate_manifest_contract(manifest: dict[str, Any]) -> tuple[list[str], dict[str, Any], str]:
    """Return the release-wide feature, label and training-data contract.

    The three values are returned because both validation sides and the artifact
    pass have to agree with them; re-reading them from the manifest at each use
    site hides which check belongs to which contract field.
    """
    if manifest["feature_contract"] != "hailo":
        raise ValueError("Model release manifest does not use the Hailo feature contract")

    features = manifest["features"]
    if not isinstance(features, list) or not features or not all(
        isinstance(feature, str) and feature for feature in features
    ):
        raise ValueError("Model release manifest has an invalid feature contract")
    if not isinstance(manifest["label_map"], dict) or not manifest["label_map"]:
        raise ValueError("Model release manifest has an invalid label map")

    training_hash = manifest["training_data_sha256"]
    if not isinstance(training_hash, str) or not _SHA256.fullmatch(training_hash):
        raise ValueError("Model release manifest has an invalid training data checksum")

    return features, manifest["label_map"], training_hash


def _validate_side_validation_report(
    manifest: dict[str, Any],
    side_name: str,
    features: list[str],
    label_map: dict[str, Any],
    training_hash: str,
) -> None:
    """Require one side to carry evidence for exactly the release contract."""
    reports = manifest["validation_reports"].get(side_name)
    if not isinstance(reports, dict):
        raise ValueError(  # noqa: TRY004
            f"Model release manifest is missing the {side_name} validation report"
        )
    report = reports.get("validation")
    if not isinstance(report, dict) or not report:
        raise ValueError(
            f"Model release manifest has no {side_name} validation results"
        )
    missing_metrics = [
        metric for metric in REQUIRED_VALIDATION_METRICS if metric not in report
    ]
    if missing_metrics:
        raise ValueError(
            f"Model release manifest {side_name} validation results are missing: "
            f"{', '.join(missing_metrics)}"
        )
    if not isinstance(reports.get("provenance"), dict):
        raise ValueError(  # noqa: TRY004
            f"Model release manifest is missing {side_name} training provenance"
        )
    if list(reports.get("features", [])) != list(features):
        raise ValueError(
            f"Model release manifest {side_name} feature contract does not match "
            "the release contract"
        )
    if reports.get("label_map") != label_map:
        raise ValueError(
            f"Model release manifest {side_name} label map does not match "
            "the release contract"
        )
    side_provenance = reports["provenance"]
    if side_provenance.get("training_data_sha256") != training_hash:
        raise ValueError(
            f"Model release manifest {side_name} training data checksum does not "
            "match the release contract"
        )


def _artifact_set_details(artifacts: dict[str, Any]) -> list[str]:
    """Describe how the artifact map differs from the required paired set."""
    missing_artifacts = sorted(set(EXPECTED_ARTIFACTS) - set(artifacts))
    unknown = sorted(set(artifacts) - set(EXPECTED_ARTIFACTS))
    details = []
    if missing_artifacts:
        details.append(f"missing {', '.join(missing_artifacts)}")
    if unknown:
        details.append(f"unexpected {', '.join(unknown)}")
    return details


def _validate_manifest_artifact(name: str, artifact: Any) -> None:
    """Require one artifact entry to point at its contract path with a real checksum."""
    if not isinstance(artifact, dict):
        raise ValueError(  # noqa: TRY004
            f"Model release manifest artifact {name} is invalid"
        )
    if artifact.get("path") != EXPECTED_ARTIFACTS[name]:
        raise ValueError(f"Model release manifest artifact {name} has an unexpected path")
    checksum = artifact.get("sha256")
    if not isinstance(checksum, str) or not _SHA256.fullmatch(checksum):
        raise ValueError(
            f"Model release manifest artifact {name} has an invalid checksum"
        )


def _validate_manifest_artifacts(manifest: dict[str, Any]) -> None:
    """Require exactly the four paired artifacts, each individually well formed."""
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, dict):
        raise TypeError("Invalid model release manifest")
    if set(artifacts) != set(EXPECTED_ARTIFACTS):
        details = _artifact_set_details(artifacts)
        raise ValueError(
            f"Model release manifest artifact set is invalid: {', '.join(details)}"
        )
    for name, artifact in artifacts.items():
        _validate_manifest_artifact(name, artifact)


def _validate_manifest_shape(manifest: Any, release: Path) -> dict[str, Any]:
    """Reject a manifest that does not describe a paired release.

    ``validate_release`` is the check an operator runs before trusting a release
    - usually right after a ``NO-GO``, when the fastest way back to a known-good
    model is to roll back to an older one. A manifest that only carries the
    artifact map would let a release activate without any record of which
    contract it implements or which validation evidence backed it, so the whole
    shape is checked here and not left to the checksum pass.

    The checks run in contract order - identity, release-wide contract, both
    validation sides, then the artifact map - so a broken manifest always
    reports its first defect and never its least specific one.
    """
    if not isinstance(manifest, dict):
        raise TypeError("Invalid model release manifest")
    missing = [key for key in REQUIRED_MANIFEST_KEYS if key not in manifest]
    if missing:
        raise ValueError(
            f"Model release manifest is missing: {', '.join(missing)}"
        )

    _validate_manifest_release_id(manifest, release)
    features, label_map, training_hash = _validate_manifest_contract(manifest)

    for side_name in ("cpu", "hailo"):
        _validate_side_validation_report(
            manifest, side_name, features, label_map, training_hash
        )

    _validate_manifest_artifacts(manifest)
    return manifest


def release_directory(release_root: str | Path, release_id: str) -> Path:
    """Directory of one stored release, without creating anything.

    The CLI inspects releases without activating them, so the ID has to be
    checked here too - otherwise a mistyped ``--release-id`` would let an
    operator point a read-only command at an arbitrary directory.
    """
    if not _RELEASE_ID.fullmatch(release_id):
        raise ValueError("Invalid release ID")
    return Path(release_root).resolve() / "releases" / release_id


def validate_release(release_path: str | Path) -> dict[str, Any]:
    candidate = Path(release_path)
    if not candidate.is_dir():
        # Name the release that is missing instead of the nearest existing
        # ancestor, which is what a strict resolve() error would report.
        raise ValueError(f"Model release directory not found: {candidate}")
    release = candidate.resolve(strict=True)
    with (release / "manifest.json").open("r", encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
    manifest = _validate_manifest_shape(manifest, release)
    for artifact in manifest["artifacts"].values():
        relative = Path(artifact["path"])
        path = (release / relative).resolve(strict=True)
        if release not in path.parents or not path.is_file():
            raise ValueError("Model release artifact path escapes the release directory")
        if sha256_file(path) != artifact.get("sha256"):
            raise ValueError(f"Model artifact checksum mismatch: {relative}")
    return manifest


def rollback_release(release_root: str | Path, release_id: str) -> Path:
    root = Path(release_root).resolve()
    release = release_directory(root, release_id)
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


# ---------------------------------------------------------------------------
# Reading the release a deployment is actually running
# ---------------------------------------------------------------------------

#: Environment override for the release root the runtime and monitoring read.
#: Unset means "this deployment does not use paired model releases", which keeps
#: the strict-sandbox health exporter from probing a path that does not exist.
RELEASE_ROOT_ENV = "TRADING_MODEL_RELEASE_ROOT"

#: Values that mean "no paired model releases are published here".
_DISABLED_VALUES = frozenset({"", "off", "none", "false", "no", "disabled"})


def resolve_release_root(environ: Mapping[str, str] | None = None) -> str:
    """Where paired model releases live; an empty string disables the lookup."""
    raw = (os.environ if environ is None else environ).get(RELEASE_ROOT_ENV)
    if raw is None:
        return ""
    if raw.strip().lower() in _DISABLED_VALUES:
        return ""
    return raw.strip()


@dataclass(frozen=True)
class ActiveRelease:
    """What a release root's ``active`` symlink currently points at.

    ``artifacts_verified`` is the integrity verdict: a manifest that describes a
    paired release but whose files no longer match it is still reported, because
    "the release was tampered with" has to stay distinguishable from "there is
    no release" for both the operator log and the alert rules.
    """

    release_id: str
    release_path: Path
    feature_contract: str
    features: tuple[str, ...]
    training_data_sha256: str
    artifact_checksums: dict[str, str]
    manifest_updated_utc: datetime
    artifacts_verified: bool
    #: Why the artifacts are not verified, empty when they are.
    problem: str = ""


def read_active_release(release_root: str | Path | None) -> ActiveRelease | None:
    """Describe the active release of ``release_root`` without ever raising.

    Promotion, rollback and ``validate`` are all explicit operator commands; the
    runtime and the monitoring exporter need the read-only side of the same
    contract, because today nothing anywhere reports which paired release is
    live. The lookup fails closed: a missing, dangling, non-symlink or
    malformed active release returns ``None`` rather than inventing a release,
    and it never follows an ``active`` link that leaves ``releases/``.
    """
    root_text = str(release_root or "").strip()
    if not root_text:
        return None
    root = Path(root_text)
    active = root / "active"
    if not active.is_symlink():
        # Covers "no active link at all" and "a directory or file an operator
        # placed by hand", which _activate() refuses to replace as well.
        return None
    try:
        target = Path(os.readlink(active))
    except OSError:
        return None
    if target.is_absolute() or target.parent != Path("releases"):
        return None
    release_id = target.name
    if not _RELEASE_ID.fullmatch(release_id):
        return None
    release = root / "releases" / release_id
    manifest_path = release / "manifest.json"
    try:
        with manifest_path.open("r", encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
        manifest = _validate_manifest_shape(manifest, release)
        manifest_updated = datetime.fromtimestamp(
            manifest_path.stat().st_mtime, UTC)
    except (OSError, TypeError, ValueError):
        return None

    checksums: dict[str, str] = {}
    verified = True
    problem = ""
    for name, artifact in manifest["artifacts"].items():
        checksums[name] = artifact["sha256"]
        try:
            actual = sha256_file(release / artifact["path"])
        except OSError:
            verified = False
            problem = problem or f"{name} is not readable"
            continue
        if actual != artifact["sha256"]:
            verified = False
            problem = problem or f"{name} checksum mismatch"
    return ActiveRelease(
        release_id=release_id,
        release_path=release,
        feature_contract=manifest["feature_contract"],
        features=tuple(manifest["features"]),
        training_data_sha256=manifest["training_data_sha256"],
        artifact_checksums=checksums,
        manifest_updated_utc=manifest_updated,
        artifacts_verified=verified,
        problem=problem,
    )