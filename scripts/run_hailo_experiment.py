"""Run one offline Hailo proposal against a fixed baseline on a chronological holdout."""

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from scripts.prepare_hailo_calibration import prepare as prepare_calibration
from src.hailo_contract import HAILO_FEATURES, HAILO_LABEL_MAP
from src.train_hailo_model import train_and_export

BASELINE = {"epochs": 150, "patience": 20, "learning_rate": 0.001}
MAX_DATA_BYTES = 64 * 1024 * 1024
MAX_ROWS = 100_000
HOLDOUT_FRACTION = 0.2
DERIVED_FEATURES = {"ret_1", "ret_3", "ret_6", "vol_6"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate proposal key: {key}")
        result[key] = value
    return result


def load_proposal(path: Path) -> dict:
    if path.stat().st_size > 4096:
        raise ValueError("Proposal exceeds 4096 bytes")
    proposal = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Invalid number: {value}")),
    )
    if not isinstance(proposal, dict) or set(proposal) != set(BASELINE):
        raise ValueError("Proposal must contain exactly epochs, patience, and learning_rate")
    for key, maximum in (("epochs", 300), ("patience", 50)):
        if type(proposal[key]) is not int or not 1 <= proposal[key] <= maximum:
            raise ValueError(f"{key} must be an integer between 1 and {maximum}")
    rate = proposal["learning_rate"]
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0.00001 <= rate <= 0.01:
        raise ValueError("learning_rate must be a finite number between 0.00001 and 0.01")
    if proposal["patience"] > proposal["epochs"]:
        raise ValueError("patience must not exceed epochs")
    return proposal


def split_data(source: bytes, development_path: Path):
    from io import BytesIO

    from src.train_catboost_model import (
        LABEL_HORIZON,
        add_features,
        create_profit_labels,
    )

    df = pd.read_csv(BytesIO(source))
    if len(df) > MAX_ROWS:
        raise ValueError(f"Data exceeds {MAX_ROWS} rows")
    raw_features = set(HAILO_FEATURES) - DERIVED_FEATURES
    missing = ({"timestamp", "coin", "close"} | raw_features) - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {', '.join(sorted(missing))}")
    for feature in sorted(raw_features):
        df[feature] = pd.to_numeric(df[feature], errors="coerce")
        if not np.isfinite(df[feature]).all():
            raise ValueError(f"Feature {feature} must contain finite numeric values")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    if df["coin"].isna().any() or df["timestamp"].isna().any() or df["close"].isna().any():
        raise ValueError("Invalid timestamp, coin, or close")
    df["coin"] = df["coin"].astype(str).str.strip().str.upper()
    if df["coin"].eq("").any() or not np.isfinite(df["close"]).all() or (df["close"] <= 0).any():
        raise ValueError("Coin must be nonempty and close finite and positive")
    if df.duplicated(["coin", "timestamp"]).any():
        raise ValueError("Duplicate coin and timestamp")
    timestamps = df["timestamp"].drop_duplicates().sort_values().tolist()
    split = int(len(timestamps) * (1 - HOLDOUT_FRACTION))
    if split < 1 or split >= len(timestamps):
        raise ValueError("Insufficient timestamps for holdout")
    holdout_start = timestamps[split]
    development = df.loc[df["timestamp"] < holdout_start].sort_values(
        ["timestamp", "coin"], kind="stable"
    )
    # The trainer labels this truncated file, so no forward label can enter holdout.
    development.to_csv(development_path, index=False)
    labelled = create_profit_labels(add_features(df), horizon=LABEL_HORIZON)
    holdout = labelled.loc[
        (labelled["timestamp"] >= holdout_start) & labelled["future_net_return"].notna()
    ].sort_values(["timestamp", "coin"], kind="stable")
    if holdout.empty:
        raise ValueError("No fully labelled holdout rows")
    features = holdout.reindex(columns=HAILO_FEATURES).replace(
        [np.inf, -np.inf], np.nan
    ).fillna(0.0).to_numpy(dtype=np.float32)
    labels = holdout["label"].map(HAILO_LABEL_MAP).to_numpy(dtype=np.int64)
    return features, labels, holdout_start, len(development)


def evaluate(model_dir: Path, features: np.ndarray, labels: np.ndarray) -> dict:
    from onnx.reference import ReferenceEvaluator
    from sklearn.metrics import f1_score

    metadata = json.loads((model_dir / "metadata.json").read_text(encoding="utf-8"))
    means = np.asarray(metadata["means"])
    scales = np.asarray(metadata["scales"])
    if means.shape != (len(HAILO_FEATURES),) or scales.shape != means.shape:
        raise ValueError("Invalid model normalization shape")
    if not np.isfinite(means).all() or not np.isfinite(scales).all() or (scales <= 0).any():
        raise ValueError("Invalid model normalization")
    evaluator = ReferenceEvaluator(str(model_dir / "trading_mlp.onnx"))
    predictions = []
    for start in range(0, len(features), 1024):
        inputs = ((features[start:start + 1024] - means) / scales).astype(np.float32)
        logits = np.asarray(evaluator.run(None, {"features": inputs})[0])
        if logits.shape != (len(inputs), len(HAILO_LABEL_MAP)) or not np.isfinite(logits).all():
            raise ValueError("Model produced invalid holdout logits")
        predictions.extend(logits.argmax(axis=1).tolist())
    return {
        "macro_f1": float(f1_score(labels, predictions, labels=list(HAILO_LABEL_MAP.values()),
                                   average="macro", zero_division=0)),
        "rows": len(labels),
    }


def run(data_path: Path, proposal_path: Path, output_dir: Path) -> dict:
    proposal = load_proposal(proposal_path)
    if data_path.stat().st_size > MAX_DATA_BYTES:
        raise ValueError("Data exceeds 64 MiB")
    with data_path.open("rb") as handle:
        source = handle.read(MAX_DATA_BYTES + 1)
    if len(source) > MAX_DATA_BYTES:
        raise ValueError("Data exceeds 64 MiB")
    source_hash = hashlib.sha256(source).hexdigest()
    # Exclusive creation ensures no existing experiment or user files are overwritten.
    output_dir.mkdir(parents=False, exist_ok=False)
    development_path = output_dir / "development.csv"
    features, labels, holdout_start, development_rows = split_data(source, development_path)
    del source
    for name, params in (("baseline", BASELINE), ("candidate", proposal)):
        model_dir = output_dir / name
        model_dir.mkdir()
        params_hash = hashlib.sha256(
            json.dumps(params, sort_keys=True).encode("utf-8")
        ).hexdigest()
        model_id = f"research-{name}-{source_hash[:12]}-{params_hash[:12]}"
        train_and_export(str(development_path), str(model_dir), model_id=model_id, **params)
        prepare_calibration(development_path, model_dir)
    baseline = evaluate(output_dir / "baseline", features, labels)
    candidate = evaluate(output_dir / "candidate", features, labels)
    result = {
        "status": "completed",
        "purpose": "offline research only; not a deployment recommendation",
        "source_sha256": source_hash,
        "development_sha256": hashlib.sha256(development_path.read_bytes()).hexdigest(),
        "holdout_start_utc": holdout_start.isoformat(),
        "development_raw_rows": development_rows,
        "evaluation": "three-class macro-F1 on raw ONNX argmax; no holdout threshold tuning",
        "baseline_parameters": BASELINE,
        "candidate_parameters": proposal,
        "baseline": baseline,
        "candidate": candidate,
        "macro_f1_delta": candidate["macro_f1"] - baseline["macro_f1"],
    }
    with (output_dir / "result.json").open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write("\n")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="New experiment directory; its parent must exist")
    args = parser.parse_args(argv)
    try:
        result = run(args.data, args.proposal, args.output_dir)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, ImportError) as exc:
        print(f"Experiment failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
