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

## New models: shared tick preprocessing and calibration

The existing model and worker continue to use `legacy` preprocessing by
default. Its training and inference preprocessing differ, and the historical
training statistics were not saved. Do not switch an existing checkpoint to
`tick-window-v1`: train a new model with that version and validate it before
use. The new version uses the same window preprocessing for training,
calibration and worker inference: nine features in order `bid`, `ask`, `mid`,
`bid_size`, `ask_size`, `last_trade_price`, `last_trade_size`,
`bid_size / max(ask_size, 1e-6)`, and `spread` (provided or `ask - bid`);
each window is independently z-score normalized to float32. Ticks are grouped
by symbol; the worker rejects invalid or backward-moving timestamps rather
than mixing symbols or padding incomplete windows.

The new training CSV must contain real recorded ticks with columns
`timestamp` (finite Unix seconds), `symbol`, the first seven features above,
and `is_anomaly` (0 or 1 for the last tick of each window). Optional `spread`
must be finite. Values must be complete, and bid/ask sizes nonnegative. Label
the data using a documented, independently validated method: the training
path does not generate labels for this version. OHLCV candles alone do not
contain the required order-book quantities. The existing root
`training_data.csv` is OHLCV and is not suitable for this workflow.

Train into a **new, separate** directory on a host with the project's training
dependencies:

```sh
python -m src.models.train_timeseries_transformer \
  --train-data /path/to/labeled_ticks.csv \
  --output-dir /path/to/new_model \
  --preprocessing-version tick-window-v1 \
  --seq-length 60
```

Rows are stably ordered by timestamp before the 80/20 chronological split,
with equal timestamps kept on the same side. Each symbol must have at least
`seq_length` ticks in both splits. Windows do not cross the split, but labels
must also be checked for future-data leakage independently. The output
directory must not already contain model artifacts; the configuration records
the preprocessing version. The training code does not directly train the
confidence head, so its output is not yet a validated confidence estimate.

Create calibration windows from representative recorded data, ideally a
training/calibration period separate from the final evaluation period:

```sh
python -m scripts.hailo_prepare_calibration \
  --ticks /path/to/recorded_ticks.csv \
  --model-config /path/to/new_model/model_config.json \
  --symbol BTC/USD --symbol ETH/USD \
  --output /path/to/new_model/calibration.npy \
  --max-windows-per-symbol 1024 --stride 1
```

`--ticks` also accepts the Kraken tick SQLite database (`ticks` table); it is
read without modification. CSV requires the same tick columns as training,
but does not require labels. All requested symbols must have enough ticks.
For each symbol, eligible chronological windows are sampled evenly over the
available recording; `stride` controls the spacing of eligible window ends and
the limit applies per symbol. Selection is deterministic for the same ordered
records, including equal-timestamp ordering. The output is float32 with shape
`(number_of_windows, 1, seq_length, 9)`. A companion
`calibration.npy.json` records the preprocessing version, shape, per-symbol
counts, selection parameters and SHA-256 of the `.npy` file. Neither output is
overwritten. Review the recording's coverage, ordering and quality before
using it; determinism alone does not make it representative.

The worker reads `model_config.json` beside the ONNX file, or from
`HAILO8_MODEL_CONFIG_PATH`. The model, matching configuration and eventual
HEF must be deployed together; an absent configuration defaults to legacy.
Do not replace the live model until the new ONNX and compiled HEF have been
independently validated. This workflow does not yet package or deploy them.

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
`$HOME/hailo_8/`:

```text
$HOME/hailo_8/
├── hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl
└── hailort_4.24.0_amd64.deb
```

Create a private directory for only the two required build inputs. Hard links
keep the originals in place without making a full copy:

```sh
mkdir -m 700 "$HOME/hailo_8/build-inputs"
chmod 600 \
  "$HOME/hailo_8/hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl" \
  "$HOME/hailo_8/hailort_4.24.0_amd64.deb"
ln "$HOME/hailo_8/hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl" \
  "$HOME/hailo_8/build-inputs/compiler.whl"
ln "$HOME/hailo_8/hailort_4.24.0_amd64.deb" \
  "$HOME/hailo_8/build-inputs/hailort.deb"
```

The separate `hailort-pcie-driver_4.24.0_all.deb` is a host driver package,
not needed to build this compiler image; do not pass it as the HailoRT runtime
secret.

Build and start the compiler environment from an x86_64 Docker host:

```sh
DOCKER_BUILDKIT=1 docker buildx build --platform linux/amd64 --load \
  -f docker/hailo-compiler.Dockerfile \
  --build-context hailo_packages="$HOME/hailo_8/build-inputs" \
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

The compiler image was built with Hailo Dataflow Compiler 3.34.0, and
`hailortcli --version` reports HailoRT 4.24.0. The compiler's startup status
probe still reports HailoRT as `Not Installed`; no physical Hailo device is
available in this build environment. Parsing the current
`timeseries_transformer.onnx` was also tested, but the parser exits inside a
`ReduceMean` conversion with `KeyError: batch`; specifying the sequence input
format (`NGC`) and shape (`[1,60,9]`) instead reaches the same conversion and
fails with `KeyError: groups`. This file has opset 18. An isolated re-export of
the same checkpoint using the PyTorch legacy exporter and opset 14 parsed both
outputs successfully, and was optimized and compiled to a two-context Hailo-8
HEF using synthetic calibration data. Across five synthetic test windows, both
re-exported ONNX outputs matched the PyTorch checkpoint within 1.6e-7 and
matched the existing ONNX model exactly. The synthetic HEF
was created only inside a disposable container and is **not suitable for
deployment**. DFC also warned about unexpected matmul input shapes.

The existing ONNX file is not replaced automatically. On a host with this
project's training dependencies installed, re-export the existing checkpoint
to a new path:

```sh
python -m scripts.hailo_export_onnx \
  model/hailo_prefilter \
  model/hailo_prefilter/timeseries_transformer_hailo.onnx
```

The command loads `model_config.json` and `timeseries_transformer_state.pt`,
checks the state dictionary strictly, and refuses to overwrite an existing
file. Validate both model outputs against PyTorch and the existing ONNX model
on representative data before relying on a new export.

The container opens a shell in `/workspace`; the project model is available at
`model/hailo_prefilter/`. Use the compiler/Model Zoo commands and calibration
options supported by the installed SDK version. The model must be calibrated
with representative windows that match its sequence length and feature order.
The image only supplies the compiler environment; it does not guess a
model-specific calibration command or include project trading dependencies.

For a DFC-compatible ONNX model, the image includes a noninteractive compiler
wrapper. Place the model and a **representative, preprocessed** calibration
array in the mounted model directory, then run inside the container:

```sh
python /opt/hailo/hailo_compile_onnx.py \
  model/hailo_prefilter/timeseries_transformer_hailo.onnx \
  model/hailo_prefilter/calibration.npy \
  model/hailo_prefilter/timeseries_transformer_hailo.hef
```

The calibration file must be a non-empty four-dimensional NHWC NumPy array,
with the layout, values and preprocessing expected by the parsed model.
For the tested Transformer export, DFC reported input shape `[N, 1, 60, 9]`;
the 60-by-9 values must follow the actual inference preprocessing and feature
order. Confirm the shape again for models with different configurations.
Random or synthetic calibration data is useful only for pipeline smoke tests,
not for trading or accuracy assessment. Use `--arch hailo8r` or `--arch
hailo8l` if the target is not a Hailo-8. The output must not already exist.
Temporary HARs are created next to the output and removed after the attempt;
ensure sufficient free space and write permissions there.

The wrapper does not pass `-y` to the parser: automatic acceptance of parser
recommendations can truncate a model to an intermediate layer. Unsupported
graphs therefore fail rather than publishing a partial HEF. It validates the
result with `hailortcli parse-hef` before publishing it, but a parseable HEF
does not guarantee equivalent inference results. Verify output shapes,
quantized accuracy against the ONNX model, and execution on the actual device
before deployment. The tested CPU-only optimizer reduced its optimization level
to zero and warned that accuracy may be suboptimal.

The test HEF reports input `NHWC(1x60x9)` and outputs named `fc3` and `fc4`,
not the ONNX tensor names. The current worker's HEF path must be checked and,
if needed, adapted for this shape and for the mapping of those outputs to
anomaly score and confidence before deploying the HEF. Even for a new v1
model, preprocessing and accuracy must be validated on representative data.
The source wheel and DEB are excluded from Git and from the repository's
default Docker build context. The separate named build context contains only
the two required files and exposes them read-only to their installation steps;
they are not copied into image layers. The local BuildKit builder still
receives these proprietary packages to perform the installation. The resulting
image contains the installed proprietary compiler and runtime, so distribute
or publish that image only if your Hailo license permits it.

The compiler Dockerfile also removes apt metadata in the same layer that
installs system packages, disables pip's download cache, and installs the
compiler wheel from a read-only BuildKit mount. The virtual environment setup
and compiler installation share one layer so the pre-upgrade pip/setuptools
files are not retained in a lower image layer. The compiler and its runtime
dependencies still determine most of the final image size; removing SDK
libraries without checking the compiler's runtime requirements can make the
image unusable.

BuildKit's reusable build cache takes additional disk space beyond the image.
Inspect it with `docker buildx du`; when space is needed, `docker buildx prune`
removes cache for the selected builder after confirmation. Avoid `--no-cache`
as a disk-cleanup option: it forces rebuilds but does not remove existing cache.

High-level flow: collect and label real ticks, train a new v1 model, create
representative calibration windows, compile with the wrapper above, compare
the ONNX and HEF on held-out data, and test the actual device before rollout.
The installed HailoRT version on the target must also be compatible with the
compiler and HEF.

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