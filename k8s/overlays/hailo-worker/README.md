# Hailo worker: pinned ONNX bundle

The init container publishes the three files already in the worker image:
`model/hailo_prefilter/timeseries_transformer.onnx`, its external data
`timeseries_transformer.onnx.data`, and `model_config.json`. It does **not**
download a URL or install a HEF. The checked-in model refers to that exact
external-data filename. Its configuration has no `preprocessing_version`,
so the worker interprets it as `legacy`; do not change that without
retraining and validating the artifacts.

The bundle ID in the DaemonSet is SHA-256 of the UTF-8/ASCII bytes below
(one newline after each line, in this order; file digests are lowercase):

```
timeseries_transformer.onnx c613fd55278dfc9e00ceeb4602b873cc39fde3eaecc50520bcda28779685732f
timeseries_transformer.onnx.data 73369301ae5636d829fddae09cc0e678b051dbbef1a9c91959455c551b27fb3d
model_config.json 073e969df36dec6f005ce109135da77278d3d896c9b91994c993d90d4b3198b3
```

The ID is `885570e115b82d3236ba9fc3f214fb89d76c386a9f6857881f86baad92130898`.
The init container verifies the source against it, copies into a private
staging directory on the PVC, verifies again, then renames the entire directory
into `/models/bundles/<ID>` under a shared advisory file lock. Repeated and
concurrent cooperating initializers verify and reuse an identical bundle.
An incomplete, modified, symlinked, or extra-file-containing destination
fails closed and is never repaired or overwritten. Existing files elsewhere
on the PVC, including older flat-layout models and HEFs, are left untouched.
The worker mounts the PVC read-only and uses ONNX, config and the default
nonexistent HEF path within the same pinned directory; it may use ONNX/CPU.

For an update, build and test an image containing all three matching artifacts,
compute the new ID using the exact algorithm above (for example
`python3 -c 'from pathlib import Path; from scripts.download_onnx_model import _bundle_id; print(_bundle_id(Path("model/hailo_prefilter"), exact=False))'`),
and change the init `--bundle-id` and **all three** worker paths together.
Pin the image by immutable digest in the deployment: the example's `latest`
tag is not reproducible. A mismatched cached/image version fails init rather
than replacing a bundle. Keep old bundles while old pods or rollbacks need
them; after verifying no consumer uses them, remove them manually. A crash
can leave `.bundle-*` staging directories: remove only after ensuring no
initializer is running. Do not remove `.install.lock` while any initializer
can run. The old single-file downloader mode remains available outside this
overlay; its URL, force and checksum settings do not affect bundle mode.

## Boundaries and operational checks

The content ID detects a mismatch, not provenance, model/config semantic
compatibility, preprocessing correctness, or HEF compatibility. Obtain and
review the artifact and image digests from a trusted source; test model
behavior and actual provider/device inference before rollout. This protocol
assumes a trusted image and PVC, atomic same-filesystem directory rename and
working cross-process `flock`; it does not protect against non-cooperating or
malicious writers, later PVC modification, or storage corruption after init.
Mounting the worker read-only does not prevent other pods or administrators
from writing to the claim. Use storage and access controls accordingly.
The ReadWriteOnce local-path claim requires scheduling/storage support for
the intended workers; it is not a multi-node sharing guarantee. Rollout
between versions may temporarily run different pinned versions.

Before deploying, inspect the actual worker node and container image. This
overlay requires `/dev/hailo0`, the host Python package at
`/usr/lib/python3/dist-packages/hailo_platform`, and both host library paths
`/lib/libhailort.so.4.23.0` and `/lib/libhailort.so`. Verify files, symlink
targets, Python ABI, and HailoRT/driver compatibility on the intended node.
Mounting files or importing the module alone does not prove device inference.
