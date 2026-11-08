import numpy as np
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("pandas")
pytest.importorskip("einops")

from src.models import train_timeseries_transformer as training


@pytest.fixture
def config():
    return training.TimeSeriesTransformerConfig(
        seq_length=4, n_features=3, hidden_dim=8, n_layers=1,
        n_heads=2, ff_dim=16, dropout=0.0,
    )


@pytest.mark.parametrize("supports_dynamo", [False, True])
def test_export_selects_legacy_exporter(monkeypatch, tmp_path, config, supports_dynamo):
    calls = []

    def old_export(model, input_tensor, path, *, input_names, output_names,
                   opset_version, do_constant_folding):
        calls.append((input_tensor.shape, opset_version, input_names, output_names))

    def new_export(model, input_tensor, path, *, input_names, output_names,
                   opset_version, do_constant_folding, dynamo=True):
        assert dynamo is False
        old_export(model, input_tensor, path, input_names=input_names,
                   output_names=output_names, opset_version=opset_version,
                   do_constant_folding=do_constant_folding)

    monkeypatch.setattr(torch.onnx, "export", new_export if supports_dynamo else old_export)
    training.export_to_onnx(
        training.TimeSeriesTransformer(config), str(tmp_path / "model.onnx"),
        config.seq_length, config.n_features,
    )
    assert calls == [((1, 4, 3), 14, ["input"], ["anomaly_score", "confidence"])]


def test_export_failure_is_raised(monkeypatch, tmp_path, config, caplog):
    error = RuntimeError("export failed")

    def failing_export(*args, dynamo=True, **kwargs):
        raise error

    monkeypatch.setattr(torch.onnx, "export", failing_export)
    with pytest.raises(RuntimeError, match="export failed") as raised:
        training.export_to_onnx(training.TimeSeriesTransformer(config), str(tmp_path / "bad.onnx"))
    assert raised.value is error
    assert "ONNX export failed" in caplog.text


@pytest.mark.parametrize("export_fails", [False, True])
def test_training_forwards_dimensions_and_stops_on_export_failure(
    monkeypatch, tmp_path, config, export_fails
):
    monkeypatch.setattr(training.pd, "read_csv", lambda _: training.pd.DataFrame({"x": range(10)}))
    monkeypatch.setattr(training, "TimeSeriesDataset", lambda *args, **kwargs: [0])
    monkeypatch.setattr(training, "DataLoader", lambda dataset, **kwargs: dataset)

    class FakeTrainer:
        def __init__(self, *args, **kwargs):
            pass

        def fit(self, *args):
            return {"train_losses": []}

    monkeypatch.setattr(training, "TimeSeriesTransformerTrainer", FakeTrainer)
    calls = []
    error = RuntimeError("export failed")

    def fake_export(model, path, *, seq_length, n_features):
        calls.append((seq_length, n_features))
        if export_fails:
            raise error

    monkeypatch.setattr(training, "export_to_onnx", fake_export)
    if export_fails:
        with pytest.raises(RuntimeError, match="export failed") as raised:
            training.train_transformer("unused.csv", str(tmp_path), config)
        assert raised.value is error
        assert not (tmp_path / "hailo_compile_instructions.json").exists()
    else:
        training.train_transformer("unused.csv", str(tmp_path), config)
        assert (tmp_path / "hailo_compile_instructions.json").exists()
    assert calls == [(config.seq_length, config.n_features)]


def test_onnx_opset_and_both_outputs_match_pytorch(tmp_path, config):
    onnx = pytest.importorskip("onnx")
    ort = pytest.importorskip("onnxruntime")
    torch.manual_seed(7)
    model = training.TimeSeriesTransformer(config).eval()
    path = tmp_path / "model.onnx"
    training.export_to_onnx(model, str(path), config.seq_length, config.n_features)

    graph = onnx.load(str(path))
    onnx.checker.check_model(graph)
    assert {item.domain: item.version for item in graph.opset_import}[""] == 14
    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    assert [output.name for output in session.get_outputs()] == ["anomaly_score", "confidence"]
    inputs = torch.randn(1, config.seq_length, config.n_features)
    with torch.no_grad():
        expected = model(inputs)
    actual = session.run(None, {"input": inputs.numpy()})
    for onnx_output, torch_output in zip(actual, expected, strict=True):
        np.testing.assert_allclose(onnx_output, torch_output.numpy(), rtol=1e-4, atol=1e-5)
