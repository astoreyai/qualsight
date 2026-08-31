#!/usr/bin/env bash
# QualSight reference-workstation installation from pinned resources (PRD §§46–48; QS-H72).
#
# Installation may use the network for dependency wheels bound by uv.lock,
# packages bound by pnpm-lock.yaml. Scientific jobs then run without network
# access. Checkpoints, matcher sources, and pair datasets are separate explicit
# System actions and are never downloaded implicitly by this script.
#
# Preconditions on the controlled workstation (checked below): Linux x86_64,
# Python 3.11, uv, Node 20 + pnpm 10, the admitted OFIQ runtime, and every
# deployment-local path required by the selected local workflows.
# This script is not a relocatable cold-start installer: the registry deliberately
# binds controlled scientific inputs and runtimes to absolute paths and hashes.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
UV="${UV:-$(command -v uv || true)}"
export PATH="/usr/bin:$PATH"   # Node 20 lives in /usr/bin on the reference workstation; ~/.local/bin/node (v26) must not shadow it

if [ -z "$UV" ]; then
  echo "installation refused: uv is not available on PATH; set UV to its absolute executable path" >&2
  exit 2
fi

step() { printf '\n== %s\n' "$*"; }

step "toolchain"
python3.11 --version
"$UV" --version
node --version
pnpm --version

step "backend dependencies (uv.lock, hashes pinned)"
"$UV" sync --frozen --directory backend

step "frontend dependencies and console build (pnpm-lock.yaml)"
pnpm install --frozen-lockfile
pnpm --dir frontend run build

step "OFIQ v1.1.2 runtime (skipped when the registered runtime already validates)"
if ! bash scripts/ofiq_v112_build.sh --validate-existing --registry resources/resource-registry.json; then
  echo "registered OFIQ runtime validation failed; the native rebuild remains blocked without a reviewed Conan lock" >&2
  bash scripts/ofiq_v112_build.sh
fi

step "governance validation"
python3 scripts/validate_metric_registry.py metrics/metric-registry.json
python3 scripts/validate_atg_documents.py

step "installed"
echo "Start the product with: env PYTHONPATH=$ROOT/backend/src:$ROOT $UV run --directory backend --frozen --offline python -m qualsight.cli serve   (console + API on http://127.0.0.1:8480)"
echo "Verify offline operation with: python3 scripts/offline_gate.py"
