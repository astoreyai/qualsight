#!/usr/bin/env bash
# Build the beta release archive: source tree at HEAD (git archive), SHA-256 sums, and the shipped
# console bundle. No dataset, model, runtime, or derived biometric bytes are included (they are
# registered by hash and acquired on the workstation by scripts/install.sh).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("backend/pyproject.toml","rb"))["project"]["version"])')"
OUT="dist"
mkdir -p "$OUT"
NAME="qualsight-${VERSION}"
git archive --format=tar.gz --prefix="${NAME}/" -o "${OUT}/${NAME}-src.tar.gz" HEAD
if [ -d frontend/dist ]; then tar -czf "${OUT}/${NAME}-console-dist.tar.gz" -C frontend dist; fi
( cd "$OUT" && sha256sum "${NAME}"-*.tar.gz > "${NAME}-SHA256SUMS" && cat "${NAME}-SHA256SUMS" )
git rev-parse HEAD > "${OUT}/${NAME}-COMMIT"
echo "release artifacts in ${OUT}/ for ${NAME} at $(cat "${OUT}/${NAME}-COMMIT")"
