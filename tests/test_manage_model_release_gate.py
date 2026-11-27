"""The promotion CLI must refuse to swap the active model without a GO verdict.

Promotion is the point where a bad trading record gets baked into the next
decision, so ``scripts/manage_model_release.py`` consults
:mod:`src.scorecard_gate` before touching the ``active`` symlink. Rollback is
the escape hatch and stays available even when the scorecard says ``NO-GO``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.manage_model_release import main

FEATURES = ["rsi", "macd"]
LABEL_MAP = {"verkaufen": 0, "halten": 1, "kaufen": 2}
DATA_HASH = "b" * 64


def _validation(accuracy: float, macro_f1: float) -> dict[str, float]:
    return {
        "rows": 250,
        "accuracy": accuracy,
        "macro_f1": macro_f1,
        "threshold_macro_f1": macro_f1 - 0.01,
        "threshold_coverage": 0.6,
    }


def _artifacts(root: Path) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    cpu_model = root / "cpu.cbm"
    cpu_model.write_bytes(b"cpu model")
    hef = root / "model.hef"
    hef.write_bytes(b"hailo model")
    cpu_metadata = root / "cpu.json"
    cpu_metadata.write_text(json.dumps({
        "feature_contract": "hailo",
        "features": FEATURES,
        "label_map": LABEL_MAP,
        "training_data_sha256": DATA_HASH,
        "label_horizon": 6,
        "train_rows": 1000,
        "validation_rows": 250,
        "validation": _validation(0.8, 0.7),
    }), encoding="utf-8")
    hailo_metadata = root / "hailo.json"
    hailo_metadata.write_text(json.dumps({
        "feature_contract": "hailo",
        "features": FEATURES,
        "label_map": LABEL_MAP,
        "data_provenance": {
            "training_data_sha256": DATA_HASH,
            "label_map": LABEL_MAP,
            "label_horizon": 6,
            "train_rows": 1000,
            "validation_rows": 250,
        },
        "validation": _validation(0.79, 0.69),
    }), encoding="utf-8")
    return [cpu_model, cpu_metadata, hef, hailo_metadata]


def write_marker(path: Path, *, verdict: str, age: timedelta) -> Path:
    stamp = datetime.now(UTC) - age
    path.write_text(
        f"TIMESTAMP_UTC={stamp.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        "RUN_MODE=standard\n"
        f"VERDICT={verdict}\n",
        encoding="utf-8",
    )
    return path


def promote_argv(tmp_path: Path, marker: Path | None) -> list[str]:
    artifacts = _artifacts(tmp_path / "artifacts")
    argv = [
        "manage_model_release.py", "promote",
        "--release-root", str(tmp_path / "deploy"),
        "--release-id", "release-1",
        "--cpu-model", str(artifacts[0]),
        "--cpu-metadata", str(artifacts[1]),
        "--hailo-hef", str(artifacts[2]),
        "--hailo-metadata", str(artifacts[3]),
        "--scorecard-status-json", str(marker if marker else tmp_path / "absent.json"),
        "--scorecard-status-env", str(tmp_path / "absent.env"),
    ]
    return argv


def rollback_argv(tmp_path: Path) -> list[str]:
    return [
        "manage_model_release.py", "rollback",
        "--release-root", str(tmp_path / "deploy"),
        "--release-id", "release-1",
    ]


def validate_argv(tmp_path: Path, release_id: str = "release-1") -> list[str]:
    return [
        "manage_model_release.py", "validate",
        "--release-root", str(tmp_path / "deploy"),
        "--release-id", release_id,
    ]


def run(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr("sys.argv", argv)
    return main()


def test_promote_refuses_without_scorecard_marker(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as excinfo:
        run(monkeypatch, promote_argv(tmp_path, None))
    assert "no scorecard status marker was found" in str(excinfo.value)
    assert not (tmp_path / "deploy" / "active").exists()


@pytest.mark.parametrize("verdict", ["HOLD", "NO-GO", "ERROR"])
def test_promote_refuses_non_go_verdict(tmp_path, monkeypatch, capsys, verdict):
    marker = write_marker(tmp_path / "status.env", verdict=verdict,
                          age=timedelta(hours=1))
    with pytest.raises(SystemExit) as excinfo:
        run(monkeypatch, promote_argv(tmp_path, marker))
    assert f"scorecard verdict is {verdict}" in str(excinfo.value)
    assert not (tmp_path / "deploy" / "active").exists()


def test_promote_refuses_stale_go(tmp_path, monkeypatch, capsys):
    marker = write_marker(tmp_path / "status.env", verdict="GO",
                          age=timedelta(days=30))
    with pytest.raises(SystemExit) as excinfo:
        run(monkeypatch, promote_argv(tmp_path, marker))
    assert "stale" in str(excinfo.value)
    assert not (tmp_path / "deploy" / "active").exists()


def test_promote_accepts_fresh_go(tmp_path, monkeypatch, capsys):
    marker = write_marker(tmp_path / "status.env", verdict="GO",
                          age=timedelta(hours=1))
    assert run(monkeypatch, promote_argv(tmp_path, marker)) == 0
    out = capsys.readouterr().out
    assert "Scorecard gate accepted: verdict=GO" in out
    assert (tmp_path / "deploy" / "active").resolve().name == "release-1"


def test_promote_max_age_flag_is_respected(tmp_path, monkeypatch, capsys):
    marker = write_marker(tmp_path / "status.env", verdict="GO",
                          age=timedelta(hours=6))
    argv = promote_argv(tmp_path, marker) + ["--scorecard-max-age-seconds", "3600"]
    with pytest.raises(SystemExit) as excinfo:
        run(monkeypatch, argv)
    assert "stale" in str(excinfo.value)


def test_promote_maintenance_override_needs_explicit_flag(tmp_path, monkeypatch):
    marker = tmp_path / "status.env"
    stamp = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    marker.write_text(
        f"TIMESTAMP_UTC={stamp}\nRUN_MODE=maintenance_override\nVERDICT=GO\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as excinfo:
        run(monkeypatch, promote_argv(tmp_path, marker))
    assert "maintenance override" in str(excinfo.value)

    argv = promote_argv(tmp_path, marker) + ["--allow-maintenance-override"]
    assert run(monkeypatch, argv) == 0


def test_promote_operator_override_prints_warning(tmp_path, monkeypatch, capsys):
    argv = promote_argv(tmp_path, None) + ["--no-require-go-verdict"]
    assert run(monkeypatch, argv) == 0
    out = capsys.readouterr().out
    assert "scorecard GO gate disabled" in out
    assert (tmp_path / "deploy" / "active").resolve().name == "release-1"


def test_rollback_is_never_gated(tmp_path, monkeypatch, capsys):
    fresh_go = write_marker(tmp_path / "status.env", verdict="GO",
                            age=timedelta(hours=1))
    assert run(monkeypatch, promote_argv(tmp_path, fresh_go)) == 0
    capsys.readouterr()
    # A NO-GO verdict must not block the way back to a known-good release.
    write_marker(tmp_path / "status.env", verdict="NO-GO", age=timedelta(hours=1))
    assert run(monkeypatch, rollback_argv(tmp_path)) == 0
    assert (tmp_path / "deploy" / "active").resolve().name == "release-1"


def test_validate_prints_release_contract_and_reports(tmp_path, monkeypatch, capsys):
    marker = write_marker(tmp_path / "status.env", verdict="GO", age=timedelta(hours=1))
    assert run(monkeypatch, promote_argv(tmp_path, marker)) == 0
    capsys.readouterr()

    assert run(monkeypatch, validate_argv(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "Release: release-1" in out
    assert "Feature contract: hailo (2 features)" in out
    assert f"Training data sha256: {DATA_HASH}" in out
    assert "cpu: rows=250 accuracy=0.8000 macro_f1=0.7000" in out
    assert "hailo: rows=250 accuracy=0.7900 macro_f1=0.6900" in out
    assert "walk-forward: not recorded" in out
    assert "artifact hailo_hef: hailo/model.hef sha256=" in out
    assert "Model release validated:" in out


def test_validate_is_never_gated_and_never_activates(tmp_path, monkeypatch, capsys):
    marker = write_marker(tmp_path / "status.env", verdict="GO", age=timedelta(hours=1))
    assert run(monkeypatch, promote_argv(tmp_path, marker)) == 0
    second = _artifacts(tmp_path / "artifacts-2")
    argv = [
        "manage_model_release.py", "promote",
        "--release-root", str(tmp_path / "deploy"),
        "--release-id", "release-2",
        "--cpu-model", str(second[0]),
        "--cpu-metadata", str(second[1]),
        "--hailo-hef", str(second[2]),
        "--hailo-metadata", str(second[3]),
        "--no-require-go-verdict",
    ]
    assert run(monkeypatch, argv) == 0
    capsys.readouterr()

    # Inspecting an inactive release is a read-only operation: no verdict is
    # consulted and the active pointer must not move.
    write_marker(tmp_path / "status.env", verdict="NO-GO", age=timedelta(hours=1))
    assert run(monkeypatch, validate_argv(tmp_path, "release-1")) == 0
    assert "Release: release-1" in capsys.readouterr().out
    assert (tmp_path / "deploy" / "active").resolve().name == "release-2"


def test_validate_reports_tampered_release(tmp_path, monkeypatch, capsys):
    marker = write_marker(tmp_path / "status.env", verdict="GO", age=timedelta(hours=1))
    assert run(monkeypatch, promote_argv(tmp_path, marker)) == 0
    (tmp_path / "deploy" / "releases" / "release-1" / "cpu" / "catboost_model.cbm").write_bytes(
        b"tampered"
    )

    assert run(monkeypatch, validate_argv(tmp_path)) == 1
    assert "checksum mismatch" in capsys.readouterr().err
    assert (tmp_path / "deploy" / "active").resolve().name == "release-1"


def test_validate_reports_missing_release(tmp_path, monkeypatch, capsys):
    assert run(monkeypatch, validate_argv(tmp_path, "never-promoted")) == 1
    assert "never-promoted" in capsys.readouterr().err
