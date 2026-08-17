"""Re-pin the resource-registry identity inside its exact-byte consumers.

Three evidence manifests and their validators bind the exact SHA-256 and size of
``resources/resource-registry.json`` at the moment their evidence was accepted.
Whenever the registry legitimately changes (a new resource, an owner decision,
a state transition), those pins must move with it and every consumer validator
must be re-run; otherwise the consumers report drift.  This tool rewrites only
the pinned registry identity fields and prints what it changed, so the
re-validation and the ledger append remain explicit, separate steps.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "resources" / "resource-registry.json"
MANIFEST_FIELDS = {
    ROOT / "resources" / "manifests" / "lfw-official-protocol-authority-v1.json": (
        "resource_boundary",
        "registry",
    ),
    ROOT / "resources" / "manifests" / "insightface-buffalo-l-release-provenance-v1.json": (
        "registered_detector",
        "resource_registry",
    ),
    ROOT / "resources" / "manifests" / "lfw-full-v1-shared-crop-lineage-reconciliation.json": (
        "resource_boundary",
        "registry",
    ),
}
VALIDATORS = [
    ROOT / "scripts" / "validate_scrfd_release_provenance.py",
    ROOT / "scripts" / "validate_shared_crop_lineage.py",
]
SHA_RE = re.compile(r'(REGISTRY_SHA256 = \(?\s*")([0-9a-f]{64})(")')
SIZE_RE = re.compile(r"(REGISTRY_SIZE = )(\d+)")


def main() -> int:
    data = REGISTRY.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    size = len(data)
    changed: list[str] = []
    for path, (outer, inner) in MANIFEST_FIELDS.items():
        raw = path.read_text(encoding="utf-8")
        manifest = json.loads(raw)
        identity = manifest[outer][inner]
        if identity["path"] != str(REGISTRY):
            raise SystemExit(f"{path}: unexpected registry path {identity['path']}")
        if identity["sha256"] != sha or identity["size_bytes"] != size:
            identity["sha256"] = sha
            identity["size_bytes"] = size
            path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            changed.append(str(path.relative_to(ROOT)))
    for path in VALIDATORS:
        text = path.read_text(encoding="utf-8")
        new_text = SHA_RE.sub(lambda m: m.group(1) + sha + m.group(3), text, count=1)
        new_text = SIZE_RE.sub(lambda m: m.group(1) + str(size), new_text, count=1)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            changed.append(str(path.relative_to(ROOT)))
    print(f"registry sha256={sha} size={size}")
    print("changed: " + (", ".join(changed) if changed else "nothing"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
