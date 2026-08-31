#!/usr/bin/env bash
# OFIQ v1.1.2 native-build admission gate.
#
# The three upstream archives have real identities recorded in the resource
# registry. The native Conan dependency graph does not have a committed,
# reviewed lockfile. A fresh build must therefore stop before network access,
# filesystem mutation, package resolution, or tool installation. The existing
# registered OFIQ v1.1.2 runtime remains the controlled-beta execution path.
set -euo pipefail

OFIQ_REMOTE=https://github.com/BSI-OFIQ/OFIQ-Project.git
OFIQ_TAG=v1.1.2
OFIQ_COMMIT=d589ca27896a472446abc9eca8b62947ee47db19

MODELS_URL=https://resources.eulisa.europa.eu/research/OFIQ-Models.zip
MODELS_SHA=8ceb6cec30c95056044e9191bbf8aa26e3e29a639b40a7636a60e13063987f36
MODELS_SIZE=422400019
ONNX_URL=https://github.com/microsoft/onnxruntime/releases/download/v1.18.1/onnxruntime-linux-x64-1.18.1.tgz
ONNX_SHA=a0994512ec1e1debc00c18bfc7a5f16249f6ebd6a6128ff2034464cc380ea211
ONNX_SIZE=5744161
IMAGES_URL='https://resources.eulisa.europa.eu/research/OFIQ-ImagesConformanceTest+TargetValues.zip'
IMAGES_SHA=11aaebeb180810e0b2b42a5dafb356938c92ddf01592411d1d8f219988448399
IMAGES_SIZE=374153032

if [ "${1:-}" = "--validate-existing" ]; then
  shift
  REGISTRY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/resources/resource-registry.json"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --registry)
        if [ "$#" -lt 2 ]; then
          echo "--registry requires a path" >&2
          exit 2
        fi
        REGISTRY=$2
        shift 2
        ;;
      *)
        echo "unknown OFIQ validation argument: $1" >&2
        exit 2
        ;;
    esac
  done
  python3 - "$REGISTRY" <<'PY'
import hashlib
import json
from pathlib import Path
import stat
import sys

registry_path = Path(sys.argv[1]).resolve(strict=True)
registry = json.loads(registry_path.read_text(encoding="utf-8"))
matches = [
    resource
    for resource in registry.get("resources", [])
    if resource.get("resource_id") == "ofiq-v1-1-2-linux-runtime"
]
if len(matches) != 1:
    raise SystemExit("OFIQ_RUNTIME_INVALID: registry must contain exactly one admitted OFIQ v1.1.2 Linux runtime")
runtime = matches[0]
if runtime.get("state") != "APPROVED_LOCAL_RESEARCH_USE":
    raise SystemExit(
        "OFIQ_RUNTIME_INVALID: registered OFIQ v1.1.2 Linux runtime is not approved for local research use"
    )
artifacts = runtime.get("artifacts")
if not isinstance(artifacts, list) or not artifacts:
    raise SystemExit("OFIQ_RUNTIME_INVALID: registered runtime has no artifacts")
total_bytes = 0
for index, artifact in enumerate(artifacts):
    try:
        path = Path(artifact["path"])
        expected_size = artifact["size_bytes"]
        expected_hash = artifact["sha256"]
    except (KeyError, TypeError) as error:
        raise SystemExit(
            f"OFIQ_RUNTIME_INVALID: artifact {index} has incomplete identity"
        ) from error
    try:
        metadata = path.lstat()
    except OSError as error:
        raise SystemExit(f"OFIQ_RUNTIME_INVALID: {path}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise SystemExit(f"OFIQ_RUNTIME_INVALID: {path} is not a regular non-symlink file")
    if metadata.st_size != expected_size:
        raise SystemExit(
            f"OFIQ_RUNTIME_INVALID: {path} size {metadata.st_size} != {expected_size}"
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    observed_hash = digest.hexdigest()
    if observed_hash != expected_hash:
        raise SystemExit(
            f"OFIQ_RUNTIME_INVALID: {path} sha256 {observed_hash} != {expected_hash}"
        )
    total_bytes += metadata.st_size
print(
    "OFIQ_RUNTIME_VALID "
    f"resource_id={runtime['resource_id']} artifacts={len(artifacts)} bytes={total_bytes}"
)
PY
  exit 0
fi

if [ "$#" -ne 0 ]; then
  echo "usage: $0 [--validate-existing [--registry PATH]]" >&2
  exit 2
fi

echo "OFIQ v1.1.2 native build refused before acquisition or mutation." >&2
echo "No Conan lockfile is recorded in the current QualSight or OFIQ v1.1.2 evidence." >&2
echo "Register and independently review the exact Conan dependency closure before changing this gate." >&2
exit 4
