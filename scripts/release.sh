#!/usr/bin/env bash
# Build a controlled-workstation beta release from one clean, committed tree.
# No source images, checkpoints, runtime binaries, embeddings, operational records, or recorded result fixtures are included.
set -euo pipefail
umask 022
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
  echo "release refused: the worktree contains tracked or untracked changes" >&2
  exit 2
fi
REFERENCE_PYTHON="/usr/bin/python3.11"
UV="$(type -P uv)"
NODE="$(type -P node)"
PNPM="$(type -P pnpm)"
"$REFERENCE_PYTHON" scripts/validate_install_preflight.py \
  --python "$REFERENCE_PYTHON" --uv "$UV" --node "$NODE" --pnpm "$PNPM"
OUT="dist"
mkdir -p "$OUT"
exec 9>"${OUT}/.release.lock"
if ! flock -n 9; then
  echo "release refused: another release process holds ${OUT}/.release.lock" >&2
  exit 3
fi
COMMIT="$(git rev-parse HEAD)"
TREE="$(git rev-parse "${COMMIT}^{tree}")"
COMMIT_TIMESTAMP="$(git show -s --format=%cI "$COMMIT")"
STAGE="$(mktemp -d "${OUT}/.release.stage.XXXXXX")"
trap 'rm -rf -- "$STAGE"' EXIT
BUILD_ROOT="${STAGE}/committed-tree"
mkdir -p "$BUILD_ROOT"
git archive "$COMMIT" | tar -x -C "$BUILD_ROOT"
VERSION="$("$REFERENCE_PYTHON" -c 'import sys,tomllib; print(tomllib.load(open(sys.argv[1],"rb"))["project"]["version"])' "$BUILD_ROOT/backend/pyproject.toml")"
NAME="qualsight-${VERSION}"
FINAL_DIR="${OUT}/${NAME}"
if [ -e "$FINAL_DIR" ]; then
  echo "release refused: destination already exists: ${FINAL_DIR}" >&2
  exit 3
fi
"$UV" sync --frozen --all-groups --directory "$BUILD_ROOT/backend"
RELEASE_PYTHON="${BUILD_ROOT}/.venv/bin/python"
"$PNPM" --dir "$BUILD_ROOT" run verify:toolchain
"$PNPM" --dir "$BUILD_ROOT" install --frozen-lockfile
SET="${STAGE}/${NAME}"
mkdir "$SET"
SOURCE_ARCHIVE="${SET}/${NAME}-src.tar.gz"
CONSOLE_ARCHIVE="${SET}/${NAME}-console-dist.tar.gz"
SBOM="${SET}/${NAME}-SBOM.cdx.json"
VULNERABILITIES="${SET}/${NAME}-VULNERABILITY-AUDIT.json"
BETA_GATE="${SET}/${NAME}-BETA-GATE.json"
MANIFEST="${SET}/${NAME}-RELEASE.json"
CHECKSUMS="${SET}/${NAME}-SHA256SUMS"
COMMIT_FILE="${SET}/${NAME}-COMMIT"
if ! "$RELEASE_PYTHON" "$BUILD_ROOT/scripts/run_release_beta_gate.py" \
  --root "$BUILD_ROOT" --deployment-root "$ROOT" \
  --output "$BETA_GATE" --python "$RELEASE_PYTHON" \
  --commit "$COMMIT" --tree "$TREE"; then
  FAILURE_ROOT="${OUT}/.release-failures"
  mkdir -m 700 -p "$FAILURE_ROOT"
  chmod 700 "$FAILURE_ROOT"
  FAILURE_ATTEMPT="$(mktemp -d "${FAILURE_ROOT}/${NAME}.${COMMIT}.XXXXXX")"
  chmod 700 "$FAILURE_ATTEMPT"
  install -m 600 "$BETA_GATE" "$FAILURE_ATTEMPT/BETA-GATE.json"
  echo "release failed; private gate diagnostic retained at ${FAILURE_ATTEMPT}/BETA-GATE.json" >&2
  exit 1
fi
"$RELEASE_PYTHON" "$BUILD_ROOT/scripts/create_release_source_archive.py" \
  --root "$ROOT" --output "$SOURCE_ARCHIVE" --commit "$COMMIT" --prefix "$NAME" \
  --policy "$BUILD_ROOT/resources/security/release-package-policy-v2.json"
PACKAGE_VALIDATION_ROOT="${STAGE}/package-validation"
mkdir "$PACKAGE_VALIDATION_ROOT"
tar --extract --gzip --file "$SOURCE_ARCHIVE" --directory "$PACKAGE_VALIDATION_ROOT" \
  --no-same-owner --no-same-permissions
"$RELEASE_PYTHON" "$BUILD_ROOT/scripts/validate_release_package.py" --root "$PACKAGE_VALIDATION_ROOT/$NAME" --expected-commit "$COMMIT"
"$RELEASE_PYTHON" "$BUILD_ROOT/scripts/validate_release_package.py" \
  --console-root "$BUILD_ROOT/frontend/dist"
tar --sort=name --mtime="@$(git show -s --format=%ct "$COMMIT")" --owner=0 --group=0 --numeric-owner \
  -czf "$CONSOLE_ARCHIVE" -C "$BUILD_ROOT/frontend" dist
"$RELEASE_PYTHON" "$BUILD_ROOT/scripts/generate_release_sbom.py" \
  --root "$BUILD_ROOT" --output "$SBOM" --commit-timestamp "$COMMIT_TIMESTAMP"
"$RELEASE_PYTHON" "$BUILD_ROOT/scripts/audit_vulnerabilities.py" \
  --root "$BUILD_ROOT" --output "$VULNERABILITIES"
"$RELEASE_PYTHON" "$BUILD_ROOT/scripts/generate_release_manifest.py" \
  --root "$BUILD_ROOT" --output "$MANIFEST" --version "$VERSION" --commit "$COMMIT" --tree "$TREE" \
  "$SOURCE_ARCHIVE" "$CONSOLE_ARCHIVE" "$SBOM" "$VULNERABILITIES" "$BETA_GATE"
printf '%s\n' "$COMMIT" > "$COMMIT_FILE"
(cd "$SET" && sha256sum "$(basename "$SOURCE_ARCHIVE")" "$(basename "$CONSOLE_ARCHIVE")" \
  "$(basename "$SBOM")" "$(basename "$VULNERABILITIES")" "$(basename "$BETA_GATE")" \
  "$(basename "$MANIFEST")" "$(basename "$COMMIT_FILE")" > "$(basename "$CHECKSUMS")")
"$RELEASE_PYTHON" "$BUILD_ROOT/scripts/validate_release_package.py" \
  --policy "$BUILD_ROOT/resources/security/release-package-policy-v2.json" \
  --sidecar "$SBOM" --sidecar "$VULNERABILITIES" --sidecar "$BETA_GATE" \
  --sidecar "$MANIFEST" --sidecar "$COMMIT_FILE" --sidecar "$CHECKSUMS"
mv -T -n "$SET" "$FINAL_DIR"
if [ -e "$SET" ] || [ ! -d "$FINAL_DIR" ]; then
  echo "release refused: atomic no-clobber publication failed for ${FINAL_DIR}" >&2
  exit 3
fi
cat "${FINAL_DIR}/${NAME}-SHA256SUMS"
echo "release artifacts in ${FINAL_DIR}/ for ${NAME} at ${COMMIT}"
