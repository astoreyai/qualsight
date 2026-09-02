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

if [ ! -x "$PYTHON" ] || [ -z "$UV" ] || [ -z "$NODE" ] || [ -z "$PNPM" ]; then
  echo "installation refused: python3.11, uv, node, and pnpm must all be available" >&2
  exit 2
fi

step() { printf '\n== %s\n' "$*"; }

step "toolchain"
"$PYTHON" scripts/validate_install_preflight.py \
  --python "$PYTHON" --uv "$UV" --node "$NODE" --pnpm "$PNPM"

if [ ! -d "$ROOT/.git" ] || [ "$VALIDATE_PACKAGE_ONLY" = true ]; then
  step "release package boundary"
  "$PYTHON" scripts/validate_release_package.py --root "$ROOT"
fi

if [ "$VALIDATE_PACKAGE_ONLY" = true ]; then
  echo "release package and exact workstation preflight passed; no installation mutation performed"
  exit 0
fi

REGISTRY="${QUALSIGHT_REGISTRY_PATH:-$ROOT/resources/resource-registry.json}"
if grep -q '/opt/qualsight/'"external" "$ROOT/resources/resource-registry.json" && [ -z "${QUALSIGHT_REGISTRY_PATH:-}" ]; then
  echo "installation refused: this sanitized package requires QUALSIGHT_REGISTRY_PATH to the reviewed controlled-host registry" >&2
  exit 2
fi
if [ ! -f "$REGISTRY" ]; then
  echo "installation refused: controlled-host registry is unavailable: $REGISTRY" >&2
  exit 2
fi

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
echo "Start the product with: env QUALSIGHT_REGISTRY_PATH=$REGISTRY PYTHONPATH=$ROOT/backend/src:$ROOT $UV run --directory backend --frozen --offline python -m qualsight.cli serve   (console + API on http://127.0.0.1:8480)"
echo "Verify offline operation with: env QUALSIGHT_REGISTRY_PATH=$REGISTRY $PYTHON scripts/offline_gate.py --observation \"$ROOT/var/offline-gate-install.json\" --data-dir \"$ROOT/var\""
