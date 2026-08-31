#!/usr/bin/env bash
# Build or validate the governed OFIQ v1.1.2 Linux runtime.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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

CONAN_LOCK="$ROOT/resources/locks/ofiq-v1.1.2-conan.lock"
CONAN_PROFILE="$ROOT/resources/profiles/ofiq-v1.1.2-gcc12-release.profile"
CONAN_CLOSURE="$ROOT/resources/manifests/ofiq-v1.1.2-conan-closure-v1.json"
CONAN_VALIDATOR="$ROOT/scripts/validate_ofiq_conan_closure.py"

usage() {
  cat >&2 <<'EOF'
usage:
  ofiq_v112_build.sh --build-root PATH [--archive-dir PATH] [--source-dir PATH]
                      [--offline] [--conan PATH] [--jobs COUNT]
  ofiq_v112_build.sh --validate-lock [--recipe PATH]
  ofiq_v112_build.sh --validate-existing [--registry PATH]

The build root must not already exist. Online mode downloads missing verified
archives and locked Conan packages. Offline mode requires --archive-dir and
--source-dir and resolves Conan packages only from the local cache.
EOF
}

if [ "${1:-}" = "--validate-existing" ]; then
  shift
  REGISTRY="$ROOT/resources/resource-registry.json"
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

MODE=build
BUILD_ROOT=
ARCHIVE_DIR=
SOURCE_DIR=
OFFLINE=false
CONAN_BIN=conan
RECIPE=
# OFIQ conformance workers each load the full model stack. Keep the unattended
# default within the memory envelope established by the governed rebuild; an
# operator may raise it explicitly after sizing the host.
JOBS=4

while [ "$#" -gt 0 ]; do
  case "$1" in
    --build-root)
      [ "$#" -ge 2 ] || { echo "--build-root requires a path" >&2; exit 2; }
      BUILD_ROOT=$2
      shift 2
      ;;
    --archive-dir)
      [ "$#" -ge 2 ] || { echo "--archive-dir requires a path" >&2; exit 2; }
      ARCHIVE_DIR=$2
      shift 2
      ;;
    --source-dir)
      [ "$#" -ge 2 ] || { echo "--source-dir requires a path" >&2; exit 2; }
      SOURCE_DIR=$2
      shift 2
      ;;
    --offline)
      OFFLINE=true
      shift
      ;;
    --conan)
      [ "$#" -ge 2 ] || { echo "--conan requires a path" >&2; exit 2; }
      CONAN_BIN=$2
      shift 2
      ;;
    --jobs)
      [ "$#" -ge 2 ] || { echo "--jobs requires a count" >&2; exit 2; }
      JOBS=$2
      shift 2
      ;;
    --validate-lock)
      MODE=validate-lock
      shift
      ;;
    --recipe)
      [ "$#" -ge 2 ] || { echo "--recipe requires a path" >&2; exit 2; }
      RECIPE=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown OFIQ build argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

VALIDATE_ARGS=(
  --manifest "$CONAN_CLOSURE"
  --lock "$CONAN_LOCK"
  --profile "$CONAN_PROFILE"
)
if [ -n "$RECIPE" ]; then
  VALIDATE_ARGS+=(--recipe "$RECIPE")
fi
python3 "$CONAN_VALIDATOR" "${VALIDATE_ARGS[@]}"
if [ "$MODE" = validate-lock ]; then
  exit 0
fi
if [ -n "$RECIPE" ]; then
  echo "--recipe is valid only with --validate-lock" >&2
  exit 2
fi
if [ -z "$BUILD_ROOT" ]; then
  echo "--build-root is required" >&2
  usage
  exit 2
fi
case "$JOBS" in
  ''|*[!0-9]*) echo "--jobs must be a positive integer" >&2; exit 2 ;;
esac
if [ "$JOBS" -lt 1 ]; then
  echo "--jobs must be a positive integer" >&2
  exit 2
fi
if [ "$OFFLINE" = true ] && { [ -z "$ARCHIVE_DIR" ] || [ -z "$SOURCE_DIR" ]; }; then
  echo "offline OFIQ build requires both --archive-dir and --source-dir" >&2
  exit 2
fi
if [ -e "$BUILD_ROOT" ] || [ -L "$BUILD_ROOT" ]; then
  echo "OFIQ build refused: build root already exists: $BUILD_ROOT" >&2
  exit 3
fi

for tool in python3 git cmake g++ tar sha256sum stat; do
  command -v "$tool" >/dev/null || { echo "required build tool not found: $tool" >&2; exit 4; }
done
if [[ "$CONAN_BIN" == */* ]]; then
  [ -x "$CONAN_BIN" ] || { echo "Conan executable not found: $CONAN_BIN" >&2; exit 4; }
else
  CONAN_BIN="$(command -v "$CONAN_BIN")" || { echo "Conan executable not found" >&2; exit 4; }
fi
if [ "$($CONAN_BIN --version)" != "Conan version 2.18.1" ]; then
  echo "OFIQ build requires Conan version 2.18.1" >&2
  exit 4
fi
if [ "$(g++ -dumpversion | cut -d. -f1)" != "12" ]; then
  echo "OFIQ build requires GCC major version 12 for the governed profile" >&2
  exit 4
fi
CMAKE_VERSION="$(cmake --version | sed -n '1s/^cmake version //p')"
python3 - "$CMAKE_VERSION" <<'PY'
import sys

def version(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError as error:
        raise SystemExit(f"invalid CMake version: {value}") from error

if version(sys.argv[1]) < version("3.26"):
    raise SystemExit("OFIQ build requires CMake 3.26 or newer")
PY
if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != x86_64 ]; then
  echo "the governed OFIQ build profile supports only linux-x86_64" >&2
  exit 4
fi
if [ "$OFFLINE" = false ]; then
  command -v curl >/dev/null || { echo "online OFIQ build requires curl" >&2; exit 4; }
fi

mkdir -p "$(dirname "$BUILD_ROOT")"
mkdir "$BUILD_ROOT"
BUILD_ROOT="$(cd "$BUILD_ROOT" && pwd -P)"
SOURCE="$BUILD_ROOT/src"
ARCHIVES="$BUILD_ROOT/archives"
CONAN_OUTPUT="$BUILD_ROOT/conan"
NATIVE_BUILD="$SOURCE/build/build_linux"
INSTALL="$BUILD_ROOT/install"
LOGS="$BUILD_ROOT/logs"
SMOKE="$BUILD_ROOT/smoke"
mkdir "$ARCHIVES" "$LOGS" "$SMOKE"

if [ -n "$SOURCE_DIR" ]; then
  SOURCE_DIR="$(cd "$SOURCE_DIR" && pwd -P)"
  if [ "$(git -C "$SOURCE_DIR" rev-parse HEAD)" != "$OFIQ_COMMIT" ]; then
    echo "OFIQ source HEAD does not match $OFIQ_COMMIT" >&2
    exit 5
  fi
  if [ "$(git -C "$SOURCE_DIR" rev-parse "${OFIQ_TAG}^{commit}")" != "$OFIQ_COMMIT" ]; then
    echo "OFIQ source tag does not resolve to the governed commit" >&2
    exit 5
  fi
  mkdir "$SOURCE"
  git -C "$SOURCE_DIR" archive "$OFIQ_COMMIT" | tar -x -C "$SOURCE"
  SOURCE_ACQUISITION=verified-local-git-archive
else
  git -c advice.detachedHead=false clone --branch "$OFIQ_TAG" --depth 1 "$OFIQ_REMOTE" "$SOURCE"
  if [ "$(git -C "$SOURCE" rev-parse HEAD)" != "$OFIQ_COMMIT" ]; then
    echo "downloaded OFIQ tag does not match $OFIQ_COMMIT" >&2
    exit 5
  fi
  SOURCE_ACQUISITION=verified-network-git-clone
fi

python3 "$CONAN_VALIDATOR" "${VALIDATE_ARGS[@]}" --recipe "$SOURCE/conan/conanfile.txt"

acquire_archive() {
  local name=$1
  local url=$2
  local expected_sha=$3
  local expected_size=$4
  local destination="$ARCHIVES/$name"
  local partial="${destination}.part"
  if [ -n "$ARCHIVE_DIR" ] && [ -f "$ARCHIVE_DIR/$name" ] && [ ! -L "$ARCHIVE_DIR/$name" ]; then
    cp --reflink=auto -- "$ARCHIVE_DIR/$name" "$partial"
  elif [ "$OFFLINE" = true ]; then
    echo "verified offline archive not found: $ARCHIVE_DIR/$name" >&2
    exit 6
  else
    curl --proto '=https' --tlsv1.2 --fail --location --retry 3 --output "$partial" "$url"
  fi
  local observed_size observed_sha
  observed_size="$(stat -c %s "$partial")"
  observed_sha="$(sha256sum "$partial" | cut -d' ' -f1)"
  if [ "$observed_size" != "$expected_size" ] || [ "$observed_sha" != "$expected_sha" ]; then
    echo "OFIQ archive identity mismatch: $name size=$observed_size sha256=$observed_sha" >&2
    exit 6
  fi
  mv "$partial" "$destination"
  printf 'OFIQ_ARCHIVE_VALID name=%s bytes=%s sha256=%s\n' "$name" "$observed_size" "$observed_sha"
}

acquire_archive OFIQ-Models.zip "$MODELS_URL" "$MODELS_SHA" "$MODELS_SIZE"
acquire_archive onnxruntime-linux-x64-1.18.1.tgz "$ONNX_URL" "$ONNX_SHA" "$ONNX_SIZE"
acquire_archive OFIQ-ImagesConformanceTest+TargetValues.zip "$IMAGES_URL" "$IMAGES_SHA" "$IMAGES_SIZE"

mkdir -p "$SOURCE/extern" "$SOURCE/data/tests"
(cd "$SOURCE/data" && cmake -E tar xf "$ARCHIVES/OFIQ-Models.zip")
(cd "$SOURCE/data/tests" && cmake -E tar xf "$ARCHIVES/OFIQ-ImagesConformanceTest+TargetValues.zip")
(cd "$SOURCE/extern" && cmake -E tar xf "$ARCHIVES/onnxruntime-linux-x64-1.18.1.tgz")

CONAN_RESOLUTION=(--build=missing)
if [ "$OFFLINE" = true ]; then
  CONAN_RESOLUTION=(--no-remote --build=never)
fi
"$CONAN_BIN" install "$SOURCE/conan/conanfile.txt" \
  --lockfile "$CONAN_LOCK" \
  --profile:build "$CONAN_PROFILE" \
  --profile:host "$CONAN_PROFILE" \
  --output-folder "$CONAN_OUTPUT" \
  "${CONAN_RESOLUTION[@]}" \
  --format=json >"$LOGS/conan-graph.json" 2> >(tee "$LOGS/conan-install.log" >&2)
python3 "$CONAN_VALIDATOR" "${VALIDATE_ARGS[@]}" \
  --recipe "$SOURCE/conan/conanfile.txt" --graph "$LOGS/conan-graph.json"

cmake -S "$SOURCE" -B "$NATIVE_BUILD" \
  -DCMAKE_INSTALL_PREFIX="$INSTALL" \
  -DCMAKE_TOOLCHAIN_FILE="$CONAN_OUTPUT/conan_toolchain.cmake" \
  -DCMAKE_BUILD_TYPE=Release \
  -DDOWNLOAD_ONNX=OFF \
  -DDOWNLOAD_MODELS_AND_IMAGES=OFF \
  -DUSE_CONAN=ON \
  -DOS=linux 2>&1 | tee "$LOGS/cmake-configure.log"
cmake --build "$NATIVE_BUILD" --target install --parallel "$JOBS" 2>&1 | tee "$LOGS/cmake-build.log"
ctest --test-dir "$NATIVE_BUILD" --parallel "$JOBS" --output-on-failure 2>&1 | tee "$LOGS/ctest.log"
grep -Fx '100% tests passed, 0 tests failed out of 787' "$LOGS/ctest.log" >/dev/null || {
  echo "OFIQ conformance suite did not report the governed 787/787 result" >&2
  exit 7
}

APP="$INSTALL/Release/bin/OFIQSampleApp"
LIBRARY="$INSTALL/Release/lib/libofiq_lib.so"
ONNX_LIBRARY="$SOURCE/extern/onnxruntime-linux-x64-1.18.1/lib/libonnxruntime.so.1.18.1"
for artifact in "$APP" "$LIBRARY" "$ONNX_LIBRARY"; do
  [ -f "$artifact" ] && [ ! -L "$artifact" ] || {
    echo "OFIQ build output missing or unsafe: $artifact" >&2
    exit 7
  }
done
LD_LIBRARY_PATH="$INSTALL/Release/lib:$SOURCE/extern/onnxruntime-linux-x64-1.18.1/lib" \
  ldd "$APP" >"$LOGS/ldd.log"
if grep -F 'not found' "$LOGS/ldd.log"; then
  echo "OFIQ runtime has unresolved shared libraries" >&2
  exit 7
fi

LD_LIBRARY_PATH="$INSTALL/Release/lib:$SOURCE/extern/onnxruntime-linux-x64-1.18.1/lib" \
  "$APP" -c "$SOURCE/data/ofiq_config.jaxn" \
  -i "$SOURCE/data/tests/images/r-01-frontal.png" \
  -o "$SMOKE/r-01-frontal.csv" >"$SMOKE/r-01-frontal.stdout"
grep -Fx 'OFIQ library version: 1.1.2' "$SMOKE/r-01-frontal.stdout" >/dev/null
if [ "$(wc -l < "$SMOKE/r-01-frontal.csv")" -ne 2 ]; then
  echo "OFIQ smoke output does not contain one real result row" >&2
  exit 7
fi

python3 - "$BUILD_ROOT" "$SOURCE_ACQUISITION" "$OFIQ_REMOTE" "$OFIQ_TAG" "$OFIQ_COMMIT" \
  "$CONAN_BIN" "$CONAN_LOCK" "$CONAN_PROFILE" "$SOURCE/conan/conanfile.txt" "$LOGS/conan-graph.json" \
  "$ARCHIVES/OFIQ-Models.zip" "$ARCHIVES/onnxruntime-linux-x64-1.18.1.tgz" \
  "$ARCHIVES/OFIQ-ImagesConformanceTest+TargetValues.zip" \
  "$APP" "$LIBRARY" "$ONNX_LIBRARY" "$LOGS/ctest.log" "$LOGS/ldd.log" \
  "$SMOKE/r-01-frontal.csv" "$SMOKE/r-01-frontal.stdout" "$OFFLINE" <<'PY'
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def identity(path_value: str) -> dict[str, object]:
    path = Path(path_value).resolve(strict=True)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path), "size_bytes": path.stat().st_size, "sha256": digest.hexdigest()}


(
    build_root,
    source_acquisition,
    remote,
    tag,
    commit,
    conan_bin,
    lock,
    profile,
    recipe,
    graph,
    models_archive,
    onnx_archive,
    conformance_archive,
    app,
    library,
    onnx,
    ctest_log,
    ldd_log,
    smoke_csv,
    smoke_stdout,
    offline,
) = sys.argv[1:]
receipt = {
    "schema_version": "1.0.0",
    "receipt_type": "OFIQ_V1_1_2_GOVERNED_BUILD",
    "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "source": {
        "acquisition": source_acquisition,
        "remote": remote,
        "tag": tag,
        "commit": commit,
    },
    "network_policy": "offline" if offline == "true" else "verified-downloads-allowed",
    "toolchain": {
        "conan": subprocess.check_output([conan_bin, "--version"], text=True).strip(),
        "cmake": subprocess.check_output(["cmake", "--version"], text=True).splitlines()[0],
        "gxx": subprocess.check_output(["g++", "--version"], text=True).splitlines()[0],
    },
    "governed_inputs": {
        "lock": identity(lock),
        "profile": identity(profile),
        "recipe": identity(recipe),
        "resolved_graph": identity(graph),
        "archives": {
            "OFIQ-Models.zip": identity(models_archive),
            "onnxruntime-linux-x64-1.18.1.tgz": identity(onnx_archive),
            "OFIQ-ImagesConformanceTest+TargetValues.zip": identity(conformance_archive),
        },
    },
    "verification": {
        "ctest": identity(ctest_log),
        "ctest_summary": "100% tests passed, 0 tests failed out of 787",
        "dynamic_linkage": identity(ldd_log),
        "smoke_csv": identity(smoke_csv),
        "smoke_stdout": identity(smoke_stdout),
    },
    "installed": {
        "executable": identity(app),
        "library": identity(library),
        "onnxruntime": identity(onnx),
    },
}
receipt_path = Path(build_root) / "build-receipt.json"
receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"OFIQ_BUILD_RECEIPT path={receipt_path} sha256={identity(str(receipt_path))['sha256']}")
PY

printf 'OFIQ_BUILD_VALID root=%s app_sha256=%s library_sha256=%s\n' \
  "$BUILD_ROOT" "$(sha256sum "$APP" | cut -d' ' -f1)" "$(sha256sum "$LIBRARY" | cut -d' ' -f1)"
