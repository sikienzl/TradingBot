# Hailo worker overlay: artifact and runtime checks

The init container only downloads the ONNX file. It does not download,
validate, or bind a matching configuration or HEF. The worker's ONNX path is
`/models/timeseries_transformer.onnx` in both containers; the base ConfigMap
sets `HAILO8_MODEL_CONFIG_PATH` to the configuration in the image. Verify
that this configuration belongs to the ONNX file before deployment. An
absent or unreadable configuration is treated by the worker as legacy;
do not change an existing model's preprocessing version without retraining
and validating the corresponding artifacts. A HEF, if supplied separately,
must also match. The worker defaults to looking for it at
`/models/timeseries_transformer.hef`, and may fall back to ONNX/CPU.

An existing nonempty model is reused without a URL unless forced or its
configured SHA-256 does not match. Set `MODEL_DOWNLOAD_SHA256` from a
trusted source to check the file contents; this does not establish model
compatibility or provenance. The download replaces only the ONNX file, not
its configuration or HEF. Avoid concurrent model updates and coordinate
artifact changes externally; the PVC and replacement are not a multi-file
transaction. The shared ReadWriteOnce claim also requires scheduling and
storage support appropriate to the number of workers.

Before deploying, inspect the actual worker node and container image. This
overlay currently requires `/dev/hailo0`, the host Python package at
`/usr/lib/python3/dist-packages/hailo_platform`, and both host library paths
`/lib/libhailort.so.4.23.0` and `/lib/libhailort.so`. These are explicit
manifest requirements, not a claim about the installed node version. Verify
the files, symlink targets, Python ABI and HailoRT/driver compatibility on
the intended node; adjust the overlay only for that verified installation.
Mounting the files or importing the module alone does not prove that device
inference works. Validate the matching artifacts and actual provider/device
behavior in a suitable non-production environment before any rollout.
