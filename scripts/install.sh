#!/usr/bin/env bash
# QualSight reference-workstation installation from pinned resources (PRD §§46–48; QS-H72).
#
# Installation may use the network for dependency wheels bound by uv.lock,
# packages bound by pnpm-lock.yaml. Scientific jobs then run without network
# access. Checkpoints, matcher sources, and pair datasets are separate explicit
# System actions and are never downloaded implicitly by this script.
#
# Preconditions on the controlled workstation (checked below): Linux x86_64,
# Python 3.11, uv, Node 26 + pnpm 10, the admitted OFIQ runtime, and every
# deployment-local path required by the selected local workflows.
# This script is not a relocatable cold-start installer: the registry deliberately
# binds controlled scientific inputs and runtimes to absolute paths and hashes.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
UV="${UV:-$(command -v uv || true)}"
PYTHON="/usr/bin/python3.11"
NODE="$(command -v node || true)"
PNPM="$(command -v pnpm || true)"
VALIDATE_PACKAGE_ONLY=false

if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--validate-package-only" ]; }; then
  echo "usage: bash scripts/install.sh [--validate-package-only]" >&2
  exit 2
fi
if [ "$#" -eq 1 ]; then
  VALIDATE_PACKAGE_ONLY=true
fi

PACKAGE_MANIFEST="$ROOT/QUALSIGHT-PACKAGE-CONTENTS.json"
if [ -e "$PACKAGE_MANIFEST" ] && { [ ! -f "$PACKAGE_MANIFEST" ] || [ -L "$PACKAGE_MANIFEST" ]; }; then
  echo "installation refused: package contents manifest is not a regular non-symlink file" >&2
  exit 2
fi
HAS_PACKAGE_MANIFEST=false
if [ -f "$PACKAGE_MANIFEST" ]; then
  HAS_PACKAGE_MANIFEST=true
fi
GIT_TOP=""
if command -v git >/dev/null 2>&1; then
  if GIT_TOP="$(git -C "$ROOT" rev-parse --show-toplevel 2>/dev/null)"; then
    GIT_TOP="$(cd "$GIT_TOP" && pwd -P)"
  else
    GIT_TOP=""
  fi
fi
if [ "$HAS_PACKAGE_MANIFEST" = true ] && [ "$GIT_TOP" = "$ROOT" ]; then
  echo "installation refused: root contains both package manifest and Git checkout authority" >&2
  exit 2
fi
IS_RELEASE_PACKAGE=false
if [ "$HAS_PACKAGE_MANIFEST" = true ]; then
  IS_RELEASE_PACKAGE=true
elif [ "$GIT_TOP" != "$ROOT" ]; then
  echo "installation refused: root is neither a release package nor a Git checkout" >&2
  exit 2
fi

if [ ! -x "$PYTHON" ] || [ -z "$UV" ] || [ -z "$NODE" ] || [ -z "$PNPM" ]; then
  echo "installation refused: python3.11, uv, node, and pnpm must all be available" >&2
  exit 2
fi

step() { printf '\n== %s\n' "$*"; }

step "toolchain"
"$PYTHON" scripts/validate_install_preflight.py \
  --python "$PYTHON" --uv "$UV" --node "$NODE" --pnpm "$PNPM"

if [ "$IS_RELEASE_PACKAGE" = true ] || [ "$VALIDATE_PACKAGE_ONLY" = true ]; then
  step "release package boundary"
  "$PYTHON" scripts/validate_release_package.py --root "$ROOT"
fi

if [ "$VALIDATE_PACKAGE_ONLY" = true ] && \
  [ -z "${QUALSIGHT_REPOSITORY_ROOT:-}" ] && [ -z "${QUALSIGHT_REGISTRY_PATH:-}" ]; then
  echo "release package and toolchain preflight passed; deployment authority was not requested; no installation mutation performed"
  exit 0
fi

CONTROLLED_ROOT="${QUALSIGHT_REPOSITORY_ROOT:-$ROOT}"
if [ "${CONTROLLED_ROOT#/}" = "$CONTROLLED_ROOT" ] || [ -L "$CONTROLLED_ROOT" ] || [ ! -d "$CONTROLLED_ROOT" ]; then
  echo "installation refused: controlled repository root is unavailable: $CONTROLLED_ROOT" >&2
  exit 2
fi
CONTROLLED_ROOT="$(cd "$CONTROLLED_ROOT" && pwd -P)"
REGISTRY="${QUALSIGHT_REGISTRY_PATH:-$CONTROLLED_ROOT/resources/resource-registry.json}"
if grep -q '/opt/qualsight/'"external" "$ROOT/resources/resource-registry.json" && \
  { [ -z "${QUALSIGHT_REPOSITORY_ROOT:-}" ] || [ -z "${QUALSIGHT_REGISTRY_PATH:-}" ]; }; then
  echo "installation refused: this sanitized package requires QUALSIGHT_REPOSITORY_ROOT and QUALSIGHT_REGISTRY_PATH to the reviewed controlled deployment" >&2
  exit 2
fi
if [ "${REGISTRY#/}" = "$REGISTRY" ]; then
  echo "installation refused: QUALSIGHT_REGISTRY_PATH must be absolute" >&2
  exit 2
fi
if [ -L "$REGISTRY" ] || [ ! -f "$REGISTRY" ]; then
  echo "installation refused: controlled-host registry is unavailable: $REGISTRY" >&2
  exit 2
fi
REGISTRY="$(cd "$(dirname "$REGISTRY")" && pwd -P)/$(basename "$REGISTRY")"
if [ "$IS_RELEASE_PACKAGE" = true ]; then
  if ! command -v git >/dev/null; then
    echo "installation refused: git is required to bind the package to its controlled deployment commit" >&2
    exit 2
  fi
  PACKAGE_COMMIT="$("$PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["source_commit"])' "$ROOT/QUALSIGHT-PACKAGE-CONTENTS.json")"
  CONTROLLED_GIT_ROOT="$(git -C "$CONTROLLED_ROOT" rev-parse --show-toplevel)"
  CONTROLLED_GIT_ROOT="$(cd "$CONTROLLED_GIT_ROOT" && pwd -P)"
  if [ "$CONTROLLED_GIT_ROOT" != "$CONTROLLED_ROOT" ]; then
    echo "installation refused: QUALSIGHT_REPOSITORY_ROOT is not the controlled Git root" >&2
    exit 2
  fi
  CONTROLLED_COMMIT="$(git -C "$CONTROLLED_ROOT" rev-parse HEAD)"
  if [ "$PACKAGE_COMMIT" != "$CONTROLLED_COMMIT" ]; then
    echo "installation refused: package source commit differs from the controlled deployment" >&2
    exit 2
  fi
  if [ -n "$(git -C "$CONTROLLED_ROOT" status --porcelain=v1 --untracked-files=no)" ]; then
    echo "installation refused: controlled deployment has tracked changes" >&2
    exit 2
  fi
  CONTROLLED_REGISTRY="$CONTROLLED_ROOT/resources/resource-registry.json"
  if [ -L "$CONTROLLED_REGISTRY" ] || [ ! -f "$CONTROLLED_REGISTRY" ]; then
    echo "installation refused: controlled repository registry is unavailable or unsafe" >&2
    exit 2
  fi
  CONTROLLED_REGISTRY_SHA="$("$PYTHON" -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$CONTROLLED_REGISTRY")"
  REGISTRY_SHA="$("$PYTHON" -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$REGISTRY")"
  if [ "$CONTROLLED_REGISTRY_SHA" != "$REGISTRY_SHA" ]; then
    echo "installation refused: controlled-host registry differs from the controlled repository registry" >&2
    exit 2
  fi
fi

if [ "$VALIDATE_PACKAGE_ONLY" = true ]; then
  echo "release package, toolchain preflight, and deployment authority passed; resource availability was not claimed; no installation mutation performed"
  exit 0
fi

export QUALSIGHT_REPOSITORY_ROOT="$CONTROLLED_ROOT"
export QUALSIGHT_REGISTRY_PATH="$REGISTRY"
"$PYTHON" scripts/validate_resource_registry.py "$REGISTRY"

step "backend dependencies (uv.lock, hashes pinned)"
"$UV" sync --frozen --directory backend

step "frontend dependencies and console build (pnpm-lock.yaml)"
"$PNPM" install --frozen-lockfile
"$PNPM" --dir frontend run build

step "OFIQ v1.1.2 runtime (skipped when the registered runtime already validates)"
if ! bash scripts/ofiq_v112_build.sh --validate-existing --registry "$REGISTRY"; then
  echo "registered OFIQ runtime validation failed" >&2
  echo "the source package includes the reviewed Conan lock/profile/closure and an online/offline builder" >&2
  echo "run bash scripts/ofiq_v112_build.sh --help, then invoke it with --build-root set to a new absolute external directory" >&2
  echo "then review and repin the deployment-local runtime registry before rerunning installation" >&2
  exit 4
fi

step "governance validation"
"$PYTHON" scripts/validate_metric_registry.py metrics/metric-registry.json

step "installed"
echo "Start the product with: env QUALSIGHT_REPOSITORY_ROOT=$CONTROLLED_ROOT QUALSIGHT_REGISTRY_PATH=$REGISTRY QUALSIGHT_CONSOLE_ROOT=$ROOT PYTHONPATH=$ROOT/backend/src:$ROOT $ROOT/.venv/bin/python -m qualsight.cli serve   (installed backend + console on http://127.0.0.1:8480)"
echo "Verify offline operation with: env QUALSIGHT_REPOSITORY_ROOT=$CONTROLLED_ROOT QUALSIGHT_REGISTRY_PATH=$REGISTRY $ROOT/.venv/bin/python $ROOT/scripts/offline_gate.py --observation \"$ROOT/var/offline-gate-install.json\" --data-dir \"$ROOT/var\" --source-root \"$ROOT\" --repository-root \"$CONTROLLED_ROOT\" --console-index \"$ROOT/frontend/dist/index.html\" --python \"$ROOT/.venv/bin/python\""
