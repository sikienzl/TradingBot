"""Build Hailo calibration data and a DFC model script from purged training rows only.

Calibration samples come exclusively from the purged training split, so neither the
holdout/validation rows nor the purged boundary rows influence quantization.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np

from src.hailo_contract import HAILO_FEATURES
from src.train_hailo_model import TRAIN_FRACTION, file_sha256, prepare_hailo_data

DEFAULT_SIZE = 1024


def _save_npy(path: Path, array: np.ndarray) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        np.save(handle, array, allow_pickle=False)
    os.replace(temporary, path)


def _write_text(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    ) as handle:
        temporary = Path(handle.name)
        handle.write(text)
    os.replace(temporary, path)


ARTIFACTS = ("calibration.npy", "validation_inputs.npy", "calibration.alls")


def _verify_provenance(metadata: dict, data_path: Path, rows: tuple[int, int]) -> None:
    from src.train_catboost_model import LABEL_HORIZON

    provenance = metadata.get("data_provenance")
    if not isinstance(provenance, dict):
        raise ValueError(  # noqa: TRY004
            "Model metadata has no data_provenance; retrain with the current "
            "train_hailo_model.py so the calibration data can be verified"
        )
    if provenance.get("training_data_sha256") != file_sha256(data_path):
        raise ValueError("Training CSV differs from the data used for the model export")
    if provenance.get("label_horizon") != LABEL_HORIZON:
        raise ValueError("Label horizon differs from the model export")
    if provenance.get("train_fraction") != TRAIN_FRACTION:
        raise ValueError("Split fraction differs from the model export")
    if (provenance.get("train_rows"), provenance.get("validation_rows")) != rows:
        raise ValueError("Split row counts differ from the model export")


def prepare(
    data_path: Path, model_dir: Path, size: int = DEFAULT_SIZE, overwrite: bool = False
) -> dict:
    if size < 1:
        raise ValueError("size must be positive")
    existing = [name for name in ARTIFACTS if (model_dir / name).exists()]
    if existing and not overwrite:
        raise FileExistsError(
            f"Refusing to overwrite {', '.join(existing)}; use --overwrite"
        )
    metadata = json.loads((model_dir / "metadata.json").read_text(encoding="utf-8"))
    if list(metadata.get("features", [])) != list(HAILO_FEATURES):
        raise ValueError("Model metadata features do not match the Hailo contract")
    mean = np.asarray(metadata["means"], dtype=np.float64)
    scale = np.asarray(metadata["scales"], dtype=np.float64)
    if mean.shape != (len(HAILO_FEATURES),) or scale.shape != mean.shape:
        raise ValueError("Model metadata normalization has the wrong shape")
    if not (np.isfinite(mean).all() and np.isfinite(scale).all() and (scale > 0).all()):
        raise ValueError("Model metadata means must be finite and scales finite and positive")

    x_train, _, x_validation, _, _ = prepare_hailo_data(str(data_path))
    _verify_provenance(metadata, data_path, (len(x_train), len(x_validation)))
    train = ((x_train - mean) / scale).astype(np.float32)
    validation = ((x_validation - mean) / scale).astype(np.float32)
    indices = np.linspace(0, len(train) - 1, min(size, len(train)), dtype=int)
    calibration = train[indices].reshape(-1, 1, 1, len(HAILO_FEATURES))
    if not np.isfinite(calibration).all():
        raise ValueError("Calibration contains non-finite values")

    _save_npy(model_dir / "calibration.npy", calibration)
    _save_npy(model_dir / "validation_inputs.npy", validation)
    _write_text(
        model_dir / "calibration.alls",
        f"model_optimization_config(calibration, calibset_size={len(calibration)})\n",
    )
    return {
        "train": len(train),
        "calibration": len(calibration),
        "validation": len(validation),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path, help="Training CSV from collect_hailo_training.py")
    parser.add_argument("model_dir", type=Path, help="Directory with metadata.json from training")
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE)
    parser.add_argument("--overwrite", action="store_true", help="Replace existing artifacts")
    args = parser.parse_args(argv)
    result = prepare(args.data.resolve(strict=True), args.model_dir.resolve(strict=True), args.size, args.overwrite)
    print(
        f"Training samples: {result['train']}, calibration samples: {result['calibration']}, "
        f"validation samples: {result['validation']} (validation not used for calibration)"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
