#!/usr/bin/env bash
# QualSight OFIQ v1.1.2 setup: clone the official tag, verify every pinned
# archive identity, build without sudo in an isolated sibling tree, install
# under a QualSight-managed prefix, and prove the installed engine on the
# registered conformance input.  Nothing here is bundled with QualSight; every
# byte is acquired from its official source (or a byte-identical preserved
# copy) under the owner authorization recorded in
# resources/authorizations/qualsight-owner-use-permission-granted-2026-08-17.json.
set -euo pipefail

BUILD_ROOT=${QUALSIGHT_OFIQ_BUILD_ROOT:-/mnt/projects/02_perception_biometrics/OFIQ-v1.1.2-build}
PRESERVED=${QUALSIGHT_OFIQ_PRESERVED_ARCHIVES:-/mnt/projects/02_perception_biometrics/OFIQ-v1.1.2-receipts}
JOBS=${JOBS:-$(nproc)}
UV=${UV:-/home/aaron/.local/bin/uv}
CONAN_VERSION=2.18.1

OFIQ_REMOTE=https://github.com/BSI-OFIQ/OFIQ-Project.git
OFIQ_TAG=v1.1.2
OFIQ_COMMIT=d589ca27896a472446abc9eca8b62947ee47db19

MODELS_URL=https://resources.eulisa.europa.eu/research/OFIQ-Models.zip
MODELS_SHA=8ceb6cec30c95056044e9191bbf8aa26e3e29a639b40a7636a60e13063987f36
ONNX_URL=https://github.com/microsoft/onnxruntime/releases/download/v1.18.1/onnxruntime-linux-x64-1.18.1.tgz
ONNX_SHA=a0994512ec1e1debc00c18bfc7a5f16249f6ebd6a6128ff2034464cc380ea211
IMAGES_URL='https://resources.eulisa.europa.eu/research/OFIQ-ImagesConformanceTest+TargetValues.zip'
# The conformance-image archive identity is pinned after its first governed
# retrieval; pass QUALSIGHT_OFIQ_IMAGES_SHA to enforce it.
IMAGES_SHA=${QUALSIGHT_OFIQ_IMAGES_SHA:-}

SRC=$BUILD_ROOT/src
ARCHIVES=$BUILD_ROOT/archives
LOGS=$BUILD_ROOT/logs
INSTALL=$BUILD_ROOT/install
mkdir -p "$ARCHIVES" "$LOGS" "$INSTALL"
exec > >(tee -a "$LOGS/build.log") 2>&1

stamp() { date -Is; }
echo "[$(stamp)] OFIQ v1.1.2 setup starting; build root $BUILD_ROOT; jobs $JOBS"

sha_of() { sha256sum "$1" | cut -d' ' -f1; }

fetch() {
  # fetch <role> <url> <expected-sha-or-empty> <destination>
  local role=$1 url=$2 expected=$3 dest=$4 name
  name=$(basename "$dest")
  if [ -f "$dest" ]; then
    echo "[$(stamp)] $role: reusing $dest"
  elif [ -n "$expected" ] && [ -f "$PRESERVED/$name" ] && [ "$(sha_of "$PRESERVED/$name")" = "$expected" ]; then
    echo "[$(stamp)] $role: preserved copy $PRESERVED/$name matches $expected; hard-linking"
    ln "$PRESERVED/$name" "$dest" 2>/dev/null || cp "$PRESERVED/$name" "$dest"
  else
    echo "[$(stamp)] $role: retrieving $url"
    curl --fail --location --silent --show-error --tlsv1.2 \
      --dump-header "$dest.headers" --output "$dest.part" "$url"
    mv "$dest.part" "$dest"
  fi
  local actual; actual=$(sha_of "$dest")
  local size; size=$(stat -c %s "$dest")
  echo "[$(stamp)] $role: sha256=$actual size=$size"
  if [ -n "$expected" ] && [ "$actual" != "$expected" ]; then
    echo "[$(stamp)] $role: identity mismatch (expected $expected); refusing" >&2
    exit 3
  fi
}

# 1. Toolchain -------------------------------------------------------------
if ! command -v conan >/dev/null 2>&1; then
  echo "[$(stamp)] installing conan==$CONAN_VERSION as a user-local uv tool"
  "$UV" tool install "conan==$CONAN_VERSION"
  export PATH="$HOME/.local/bin:$PATH"
fi
conan --version
cmake --version | head -1
g++ --version | head -1

# 2. Source ---------------------------------------------------------------
if [ ! -d "$SRC/.git" ]; then
  echo "[$(stamp)] cloning $OFIQ_REMOTE at $OFIQ_TAG"
  git clone --branch "$OFIQ_TAG" --depth 1 "$OFIQ_REMOTE" "$SRC"
fi
HEAD=$(git -C "$SRC" rev-parse HEAD)
if [ "$HEAD" != "$OFIQ_COMMIT" ]; then
  echo "[$(stamp)] source HEAD $HEAD is not the pinned tag commit $OFIQ_COMMIT; refusing" >&2
  exit 3
fi
echo "[$(stamp)] source commit $HEAD; Version.txt: $(tr '\n' ' ' < "$SRC/Version.txt")"

# 3. Archives -------------------------------------------------------------
fetch OFIQ_MODEL_BUNDLE "$MODELS_URL" "$MODELS_SHA" "$ARCHIVES/OFIQ-Models.zip"
fetch ONNX_RUNTIME_LINUX_X64_1_18_1 "$ONNX_URL" "$ONNX_SHA" "$ARCHIVES/onnxruntime-linux-x64-1.18.1.tgz"
fetch OFIQ_CONFORMANCE_IMAGES "$IMAGES_URL" "$IMAGES_SHA" "$ARCHIVES/OFIQ-ImagesConformanceTest+TargetValues.zip"

if [ ! -d "$SRC/extern/onnxruntime-linux-x64-1.18.1" ]; then
  mkdir -p "$SRC/extern"
  tar -xzf "$ARCHIVES/onnxruntime-linux-x64-1.18.1.tgz" -C "$SRC/extern"
fi
if [ ! -d "$SRC/data/models/expression_neutrality" ]; then
  # The tagged CMake extracts this archive into <source>/data (the clone only ships a README there).
  (cd "$SRC/data" && cmake -E tar xf "$ARCHIVES/OFIQ-Models.zip" --format=zip)
fi
if [ -z "$(find "$SRC/data/tests/images" -maxdepth 1 -name '*.png' -print -quit 2>/dev/null)" ]; then
  # The tagged CMake extracts this archive into <source>/data/tests (the clone only ships a README there).
  mkdir -p "$SRC/data/tests"
  (cd "$SRC/data/tests" && cmake -E tar xf "$ARCHIVES/OFIQ-ImagesConformanceTest+TargetValues.zip" --format=zip)
fi
echo "[$(stamp)] models: $(find "$SRC/data/models" -type f | wc -l) files; conformance images: $(find "$SRC/data/tests/images" -type f -name '*.png' | wc -l) png"

# 4. Conan dependencies (no sudo, no system package installation) ---------
PROFILE=$BUILD_ROOT/conan_profile_release_linux_gcc12.txt
cat > "$PROFILE" <<'PROFILE_EOF'
[settings]
arch=x86_64
build_type=Release
compiler=gcc
compiler.libcxx=libstdc++11
compiler.version=12
compiler.cppstd=17
os=Linux
[options]
[conf]
tools.system.package_manager:mode=report
tools.system.package_manager:sudo=False
PROFILE_EOF
if [ ! -f "$SRC/build/conan/conan_toolchain.cmake" ]; then
  echo "[$(stamp)] conan install"
  (cd "$SRC/scripts" && conan install ../conan/conanfile.txt \
      --build missing \
      --profile:build "$PROFILE" \
      --profile:host "$PROFILE" \
      --output-folder=../build/conan \
      -g CMakeDeps \
      -g CMakeToolchain)
fi

# 5. Configure, build, install ---------------------------------------------
BUILD_DIR=$SRC/build/build_linux
if [ ! -f "$BUILD_DIR/CMakeCache.txt" ]; then
  echo "[$(stamp)] cmake configure"
  (cd "$SRC" && cmake -S ./ -B build/build_linux \
      -DCMAKE_INSTALL_PREFIX="$INSTALL" -DCMAKE_VERBOSE_MAKEFILE:BOOL=OFF \
      -DDOWNLOAD_ONNX=OFF -DUSE_CONAN=ON -DOS=linux -DCMAKE_BUILD_TYPE=Release \
      -DDOWNLOAD_MODELS_AND_IMAGES=OFF -DUSE_GCOV_CODECOVERAGE=false)
fi
echo "[$(stamp)] cmake build+install (-j$JOBS)"
(cd "$SRC" && cmake --build build/build_linux --target install -j "$JOBS")

# 6. Prove the installed engine ------------------------------------------
APP=$INSTALL/Release/bin/OFIQSampleApp
LIB=$INSTALL/Release/lib/libofiq_lib.so
CONFIG=$SRC/data/ofiq_config.jaxn
ls -la "$APP" "$LIB" "$CONFIG"
R01=/mnt/projects/02_perception_biometrics/OFIQ-Project/data/tests/images/r-01-frontal.png
mkdir -p "$BUILD_ROOT/smoke"
echo "[$(stamp)] smoke run on registered conformance input"
( cd "$INSTALL/Release/bin" && ./OFIQSampleApp -c "$CONFIG" -i "$R01" -o "$BUILD_ROOT/smoke/r-01-frontal.csv" ) | tee "$BUILD_ROOT/smoke/r-01-frontal.stdout"
echo "[$(stamp)] smoke csv:"; cat "$BUILD_ROOT/smoke/r-01-frontal.csv"

# 7. Receipt ---------------------------------------------------------------
python3 - "$BUILD_ROOT" "$SRC" "$INSTALL" "$ARCHIVES" "$HEAD" <<'PY'
import hashlib, json, os, subprocess, sys, time
build_root, src, install, archives, head = sys.argv[1:6]
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
def ident(p):
    st = os.stat(p)
    return {"path": p, "sha256": sha(p), "size_bytes": st.st_size, "mtime_ns": st.st_mtime_ns}
def tool(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip().splitlines()[0]
    except Exception as e:  # noqa: BLE001
        return f"UNAVAILABLE: {e}"
receipt = {
    "schema_version": "1.0.0",
    "receipt_type": "OFIQ_V1_1_2_ISOLATED_BUILD",
    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "owner_authorization_path": "/mnt/projects/ofiqgui/resources/authorizations/qualsight-owner-use-permission-granted-2026-08-17.json",
    "build_root": build_root,
    "source": {"remote": "https://github.com/BSI-OFIQ/OFIQ-Project.git", "tag": "v1.1.2", "commit": head,
               "version_txt": open(os.path.join(src, "Version.txt")).read()},
    "archives": [ident(os.path.join(archives, n)) for n in sorted(os.listdir(archives)) if not n.endswith(".headers") and not n.endswith(".part")],
    "archive_headers": [ident(os.path.join(archives, n)) for n in sorted(os.listdir(archives)) if n.endswith(".headers")],
    "toolchain": {"conan": tool(["conan", "--version"]), "cmake": tool(["cmake", "--version"]), "gxx": tool(["g++", "--version"]),
                  "conan_profile": ident(os.path.join(build_root, "conan_profile_release_linux_gcc12.txt"))},
    "installed": {
        "executable": ident(os.path.join(install, "Release/bin/OFIQSampleApp")),
        "library": ident(os.path.join(install, "Release/lib/libofiq_lib.so")),
        "configuration": ident(os.path.join(src, "data/ofiq_config.jaxn")),
        "onnxruntime": ident(os.path.join(src, "extern/onnxruntime-linux-x64-1.18.1/lib/libonnxruntime.so.1.18.1")),
    },
    "models": sorted((ident(os.path.join(dp, f)) for dp, _, fs in os.walk(os.path.join(src, "data/models")) for f in fs), key=lambda item: item["path"]),
    "smoke": {"stdout": ident(os.path.join(build_root, "smoke/r-01-frontal.stdout")), "csv": ident(os.path.join(build_root, "smoke/r-01-frontal.csv"))},
}
out = os.path.join(build_root, "build-receipt.json")
with open(out, "w") as f:
    json.dump(receipt, f, indent=2, sort_keys=True)
print("receipt written", out)
PY
echo "[$(stamp)] OFIQ v1.1.2 setup complete"
