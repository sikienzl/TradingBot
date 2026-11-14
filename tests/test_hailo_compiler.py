import sys
from pathlib import Path

import numpy as np
import pytest

from scripts import hailo_compile_onnx


def _arguments(monkeypatch, model, calibration, output):
    monkeypatch.setattr(
        sys, "argv",
        ["hailo_compile_onnx.py", str(model), str(calibration), str(output)],
    )


def test_compiler_runs_pipeline_and_publishes_hef(tmp_path, monkeypatch):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"test")
    calibration = tmp_path / "calibration.npy"
    np.save(calibration, np.zeros((2, 1, 1, 22), dtype=np.float32))
    output = tmp_path / "model.hef"
    _arguments(monkeypatch, model, calibration, output)
    calls = []

    def run(command):
        calls.append(command)
        if command[:2] == ["hailo", "compiler"]:
            Path(command[command.index("--output-dir") + 1], "hailo_model.hef").write_bytes(
                b"hef"
            )

    monkeypatch.setattr(hailo_compile_onnx, "run", run)
    assert hailo_compile_onnx.main() == 0
    assert [command[:2] for command in calls] == [
        ["hailo", "parser"], ["hailo", "optimize"],
        ["hailo", "compiler"], ["hailortcli", "parse-hef"],
    ]
    assert output.read_bytes() == b"hef"


def test_compiler_rejects_nonfinite_calibration(tmp_path, monkeypatch):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"test")
    calibration = tmp_path / "calibration.npy"
    np.save(calibration, np.full((1, 1, 1, 22), np.nan))
    output = tmp_path / "model.hef"
    _arguments(monkeypatch, model, calibration, output)
    with pytest.raises(ValueError, match="finite"):
        hailo_compile_onnx.main()
    assert not output.exists()


def test_compiler_refuses_to_overwrite_hef(tmp_path, monkeypatch):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"test")
    calibration = tmp_path / "calibration.npy"
    np.save(calibration, np.zeros((1, 1, 1, 22)))
    output = tmp_path / "model.hef"
    output.write_bytes(b"existing")
    _arguments(monkeypatch, model, calibration, output)
    with pytest.raises(FileExistsError):
        hailo_compile_onnx.main()
    assert output.read_bytes() == b"existing"
