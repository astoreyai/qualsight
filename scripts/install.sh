#!/usr/bin/env bash
# QualSight one-command installation from pinned resources (PRD §§46–48; QS-H72).
#
# Network is used ONLY here (dependency wheels by uv.lock hashes, pnpm-lock.yaml
# packages, the OFIQ v1.1.2 tag + model archives by pinned SHA-256, and the pinned
# upstream matcher sources). After this script succeeds the product runs with the
# network disabled — verify with `python3 scripts/offline_gate.py`.
#
# Preconditions on the controlled workstation (checked below): Linux x86_64,
# Python 3.11, uv, Node 20 + pnpm 10, GCC 12 toolchain for the OFIQ build,
# and the registered LFW / matcher checkpoint bytes at their registry paths
# (owner-approved local research use; never bundled with QualSight).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
UV="${UV:-$HOME/.local/bin/uv}"
export PATH="/usr/bin:$PATH"   # Node 20 lives in /usr/bin on the reference workstation; ~/.local/bin/node (v26) must not shadow it

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
if ! "$UV" run --directory backend --frozen --offline python ../scripts/validate_resource_registry.py ../resources/resource-registry.json >/dev/null 2>&1; then
  bash scripts/ofiq_v112_build.sh
fi

step "pinned upstream matcher sources (setup-time acquisition into the data directory)"
"$UV" run --directory backend --frozen --offline python -m qualsight.cli setup

step "registry validation (every registered artifact hash)"
"$UV" run --directory backend --frozen --offline python ../scripts/validate_resource_registry.py ../resources/resource-registry.json

step "governance validation"
python3 scripts/validate_metric_registry.py metrics/metric-registry.json
python3 scripts/validate_atg_documents.py

step "installed"
echo "Start the product with: $UV run --directory backend --frozen --offline python -m qualsight.cli serve   (console + API on http://127.0.0.1:8480)"
echo "Verify offline operation with: python3 scripts/offline_gate.py"
