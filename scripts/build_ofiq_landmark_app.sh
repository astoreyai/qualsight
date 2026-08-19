#!/usr/bin/env bash
# QS-Z90: build OFIQLandmarkApp from scripts/ofiq_landmark_app.cpp against the
# REGISTERED OFIQ v1.1.2 runtime (headers from the registered source tag,
# linked against the registered libofiq_lib.so). No network, no sudo. The
# resulting binary is registered as the runtime resource's AUXILIARY_BINARY
# artifact; re-run scripts/repin_registry_identity.py after rebuilding.
set -euo pipefail

BUILD_ROOT="/mnt/projects/02_perception_biometrics/OFIQ-v1.1.2-build"
SRC_DIR="${BUILD_ROOT}/src"
BIN_DIR="${BUILD_ROOT}/install/Release/bin"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="${REPO_ROOT}/scripts/ofiq_landmark_app.cpp"
TARGET="${BIN_DIR}/OFIQLandmarkApp"

test -f "${SRC_DIR}/OFIQlib/include/ofiq_lib.h"
test -f "${BIN_DIR}/libofiq_lib.so"

g++ -std=c++17 -O2 \
    -I "${SRC_DIR}/OFIQlib/include" \
    -I "${SRC_DIR}/OFIQlib/modules/utils" \
    "${SOURCE}" \
    -L "${BIN_DIR}" -lofiq_lib -Wl,-rpath,"${BIN_DIR}" \
    -o "${TARGET}"

echo "compiler: $(g++ --version | head -1)"
echo "source sha256:  $(sha256sum "${SOURCE}" | cut -d' ' -f1)"
echo "binary sha256:  $(sha256sum "${TARGET}" | cut -d' ' -f1)"
echo "built ${TARGET}"
