# Hailo Hybrid Cluster

This repository now supports two execution modes for the edge worker on the Hailo node:

1. ONNX runtime fallback
2. Direct Hailo HEF execution via `hailo_platform`

The worker will prefer real Hailo execution only when a compiled HEF artifact is present.

## Runtime Selection

The edge worker checks these inputs in order:

1. `HAILO8_HEF_PATH`
2. `model/hailo_prefilter/timeseries_transformer.hef`
3. ONNX runtime providers
4. CPU fallback

Behavior:

- If a HEF exists and the host Hailo runtime is mounted into the pod, the worker uses direct `pyhailort` inference.
- If no HEF exists, the worker keeps using the ONNX model.
- If `onnxruntime` has no `HailoExecutionProvider`, the ONNX path stays on CPU.

The current rollout is therefore safe before compilation: absence of the HEF degrades to the already working CPU path.

## Required Artifact

The deployment expects a compiled artifact at:

```text
model/hailo_prefilter/timeseries_transformer.hef
```

The source ONNX and metadata already live beside it:

```text
model/hailo_prefilter/timeseries_transformer.onnx
model/hailo_prefilter/model_config.json
model/hailo_prefilter/hailo_compile_instructions.json
```

## Compile Workflow

The worker node contains the Hailo runtime, but not the full model compiler toolchain. Compile the HEF on a host that has the Hailo SDK / Model Zoo tooling installed.

High-level flow:

1. Train or export the transformer model to ONNX.
2. Read `model/hailo_prefilter/hailo_compile_instructions.json`.
3. Compile `timeseries_transformer.onnx` into `timeseries_transformer.hef` on the SDK host.
4. Copy the resulting HEF into `model/hailo_prefilter/` in this repo.
5. Re-run `bash scripts/rollout_hybrid_cluster.sh`.

Representative commands on the SDK host:

```sh
python3 -m src.train_timeseries_transformer --training-data training_data.csv --output-model-dir model/hailo_prefilter

# Then compile with the Hailo SDK / Model Zoo tooling on the compile host.
# Use the generated ONNX, model_config.json and representative market windows.
```

This repo intentionally does not hardcode a single `hailomz compile` invocation because the exact calibration dataset, parser options and target architecture belong to the SDK host setup.

## Rollout

After the HEF is present, deploy with:

```sh
REMOTE_USER=siegfried NODE1_IP=192.168.62.74 NODE2_IP=192.168.62.75 \
bash scripts/rollout_hybrid_cluster.sh
```

Expected worker behavior after a successful HEF deployment:

- the smoke test still completes
- worker metrics remain on `:9201`
- runtime stats report `provider=HailoHEF`

## Verification

Check the worker logs:

```sh
ssh siegfried@192.168.62.74 \
  "sudo kubectl logs -n trading-bot -l component=hailo-worker -c hailo-worker --tail=120"
```

Healthy direct-Hailo startup should mention the HEF path and `HailoHEF` provider.

If the HEF is missing or invalid, the worker logs a warning and falls back to the existing ONNX path without breaking the cluster rollout.