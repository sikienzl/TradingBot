#!/usr/bin/env bash
set -euo pipefail
# scripts/compile_hef.sh — Template to compile an ONNX model to a Hailo HEF on an SDK host
#
# This is a template helper that documents the typical steps to produce a HEF
# artifact from an ONNX model using the Hailo SDK. It does NOT invoke any
# specific vendor tool by default — you must set `HAILO_SDK_COMPILE_CMD` to the
# appropriate compile command for your installed Hailo SDK (or run this script
# on an SDK machine where that command is available).
#
# Usage examples:
#  # Example (SDK command supplied via env):
#  HAILO_SDK_COMPILE_CMD="hailo_compile --onnx $PWD/model.onnx --out $PWD/model.hef --quantize=dynamic" \
#    bash scripts/compile_hef.sh model/hailo_prefilter/timeseries_transformer.onnx model/hailo_prefilter/timeseries_transformer.hef
#
#  # Or run interactively on an SDK host where `hailo_compile` (or equivalent)
#  # is available on PATH:
#  bash scripts/compile_hef.sh

ONNX_PATH=${1:-model/hailo_prefilter/timeseries_transformer.onnx}
OUT_HEF=${2:-model/hailo_prefilter/timeseries_transformer.hef}

echo "ONNX input: ${ONNX_PATH}"
echo "HEF output: ${OUT_HEF}"

if [ ! -f "${ONNX_PATH}" ]; then
  echo "ERROR: ONNX model not found: ${ONNX_PATH}" >&2
  exit 2
fi

mkdir -p "$(dirname "${OUT_HEF}")"

if [ -n "${HAILO_SDK_COMPILE_CMD:-}" ]; then
  echo "Running Hailo SDK compile command..."
  eval "${HAILO_SDK_COMPILE_CMD}"
  echo "Compile command finished. Verify ${OUT_HEF}."
  if [ -f "${OUT_HEF}" ]; then
    echo "HEF created: ${OUT_HEF}"
    exit 0
  else
    echo "ERROR: HEF was not created by the compile command." >&2
    exit 3
  fi
else
  cat <<'EOF'
No Hailo SDK compile command provided.

To produce a HEF you must run the Hailo SDK compiler on an SDK host. Examples
will vary by SDK version — replace the placeholder below with the correct
command for your environment.

Example template (replace with your SDK tool):

  HAILO_SDK_COMPILE_CMD="hailo_compile --onnx ${ONNX_PATH} --out ${OUT_HEF} --quantize=dynamic"
  HAILO_SDK_COMPILE_CMD="$HAILO_SDK_COMPILE_CMD" bash scripts/compile_hef.sh ${ONNX_PATH} ${OUT_HEF}

After successful compilation, copy the HEF to the cluster node (worker) and
place it at `model/hailo_prefilter/timeseries_transformer.hef` or set
`HAILO8_HEF_PATH` to point at the HEF on the node.

EOF
  exit 1
fi
