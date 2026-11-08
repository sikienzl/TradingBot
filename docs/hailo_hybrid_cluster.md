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

The worker node contains the Hailo runtime, but not the full model compiler toolchain. Compile the HEF in the compiler image below or on another host that has the compatible Hailo SDK / Model Zoo tooling installed.

For a reproducible x86_64 compiler environment, this repository provides
`docker/hailo-compiler.Dockerfile`. The setup is adapted for this project's
Hailo-8 ONNX model and was inspired by
[How to run Hailo Dataflow Compiler using Docker on Windows](https://medium.com/@mgreiner79/how-to-run-hailo-dataflow-compiler-using-docker-on-windows-8442a0215c53).
The compiler wheel and HailoRT package are proprietary vendor-provided build
inputs. They must not be added to Git and should be obtained under the
applicable Hailo terms. Keep them outside the repository and pass them to
BuildKit as a separate named context. The following commands use the files in
`$HOME/Schreibtisch/hailo_8/`:

```text
$HOME/Schreibtisch/hailo_8/
├── hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl
└── hailort_4.24.0_amd64.deb
```

Create a private directory for only the two required build inputs. Hard links
keep the originals in place without making a full copy:

```sh
mkdir -m 700 "$HOME/Schreibtisch/hailo_8/build-inputs"
chmod 600 \
  "$HOME/Schreibtisch/hailo_8/hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl" \
  "$HOME/Schreibtisch/hailo_8/hailort_4.24.0_amd64.deb"
ln "$HOME/Schreibtisch/hailo_8/hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl" \
  "$HOME/Schreibtisch/hailo_8/build-inputs/compiler.whl"
ln "$HOME/Schreibtisch/hailo_8/hailort_4.24.0_amd64.deb" \
  "$HOME/Schreibtisch/hailo_8/build-inputs/hailort.deb"
```

The separate `hailort-pcie-driver_4.24.0_all.deb` is a host driver package,
not needed to build this compiler image; do not pass it as the HailoRT runtime
secret.

Build and start the compiler environment from an x86_64 Docker host:

```sh
DOCKER_BUILDKIT=1 docker buildx build --platform linux/amd64 \
  -f docker/hailo-compiler.Dockerfile \
  --build-context hailo_packages="$HOME/Schreibtisch/hailo_8/build-inputs" \
  -t trading-hailo-compiler:3.34.0 .

docker run --rm -it --platform linux/amd64 \
  --mount "type=bind,src=$(pwd)/model/hailo_prefilter,dst=/workspace/model/hailo_prefilter" \
  trading-hailo-compiler:3.34.0
```

Inside the container, check the installed compiler and its ONNX parser:

```sh
hailo --version
hailo parser onnx --help
```

The container opens a shell in `/workspace`; the project model is available at
`model/hailo_prefilter/`. Use the compiler/Model Zoo commands and calibration
options supported by the installed SDK version. The model must be calibrated
with representative windows that match its sequence length and feature order.
The image only supplies the compiler environment; it does not guess a
model-specific calibration command or include project trading dependencies.
The source wheel and DEB are excluded from Git and from the repository's
default Docker build context. The separate named build context contains only
the two required files and exposes them read-only to their installation steps;
they are not copied into image layers. The local BuildKit builder still
receives these proprietary packages to perform the installation. The resulting
image contains the installed proprietary compiler and runtime, so distribute
or publish that image only if your Hailo license permits it.

High-level flow:

1. Train or export the transformer model to ONNX.
2. Read `model/hailo_prefilter/hailo_compile_instructions.json`.
3. Compile `timeseries_transformer.onnx` into `timeseries_transformer.hef` on the compiler host.
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