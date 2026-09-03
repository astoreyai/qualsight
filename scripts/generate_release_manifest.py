#!/usr/bin/env python3
"""Generate an integrity manifest for an already staged QualSight release."""

from __future__ import annotations

import argparse
import hashlib
import json
import tomllib
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    *,
    root: Path,
    version: str,
    commit: str,
    tree: str,
    artifacts: list[Path],
) -> dict[str, object]:
    root = root.resolve(strict=True)
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError("commit must be a lowercase 40-character Git object id")
    if len(tree) != 40 or any(character not in "0123456789abcdef" for character in tree):
        raise ValueError("tree must be a lowercase 40-character Git object id")
    backend_project = tomllib.loads(
        (root / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    workspace = json.loads((root / "package.json").read_text(encoding="utf-8"))
    frontend = json.loads((root / "frontend" / "package.json").read_text(encoding="utf-8"))
    release_policy = json.loads(
        (root / "resources" / "security" / "release-package-policy-v2.json").read_text(
            encoding="utf-8"
        )
    )
    if version != backend_project["version"]:
        raise ValueError("release version differs from backend/pyproject.toml")
    records = []
    for path in artifacts:
        resolved = path.resolve(strict=True)
        if not resolved.is_file() or resolved.is_symlink():
            raise ValueError(f"release artifact must be a regular non-symlink file: {path}")
        records.append(
            {
                "name": path.name,
                "sha256": _sha256(resolved),
                "size_bytes": resolved.stat().st_size,
            }
        )
    if len({record["name"] for record in records}) != len(records):
        raise ValueError("release artifact names must be unique")
    return {
        "schema_version": "qualsight-release-manifest-v1",
        "product": "QualSight",
        "version": version,
        "git_commit": commit,
        "git_tree": tree,
        "component_versions": {
            "backend_pep440": backend_project["version"],
            "console_semver": frontend["version"],
            "workspace_semver": workspace["version"],
            "node": workspace["engines"]["node"],
            "pnpm": workspace["engines"]["pnpm"],
            "vite": frontend["devDependencies"]["vite"],
        },
        "governed_inputs": {
            "resource_registry_sha256": _sha256(root / "resources" / "resource-registry.json"),
            "uv_lock_sha256": _sha256(root / "uv.lock"),
            "pnpm_lock_sha256": _sha256(root / "pnpm-lock.yaml"),
            "release_package_policy_sha256": _sha256(
                root / "resources" / "security" / "release-package-policy-v2.json"
            ),
        },
        "artifacts": sorted(records, key=lambda record: str(record["name"])),
        "signature": {
            "status": "NOT_PROVIDED_LOCAL_BETA",
            "verification": "Validate every artifact against the separately shipped SHA256SUMS file.",
        },
        "deployment_boundary": {
            "classification": "CONTROLLED_SINGLE_USER_WORKSTATION_BETA",
            "network_binding": "LOOPBACK_ONLY",
            "portable_turnkey_install": False,
            "reason": "The validated resource registry records deployment-local absolute paths for controlled scientific inputs and runtimes.",
        },
        "bundled_biometric_assets": False,
        "source_archive_policy": {
            "policy_id": release_policy["policy_id"],
            "excluded_repository_paths": release_policy["source_archive_excluded_paths"],
            "forbidden_suffixes": release_policy["source_archive_forbidden_suffixes"],
            "forbidden_names": release_policy["source_archive_forbidden_names"],
            "forbidden_name_prefixes": release_policy[
                "source_archive_forbidden_name_prefixes"
            ],
            "forbidden_path_terms": release_policy["source_archive_forbidden_path_terms"],
            "secret_scan": release_policy["source_archive_secret_scan"],
            "excluded_content_classes": release_policy["excluded_content_classes"],
            "included_metadata_boundary": release_policy["included_metadata_boundary"],
        },
    }


def write_manifest(
    *, root: Path, output: Path, version: str, commit: str, tree: str, artifacts: list[Path]
) -> None:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite release manifest: {output}")
    value = build_manifest(root=root, version=version, commit=commit, tree=tree, artifacts=artifacts)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("artifacts", nargs="+", type=Path)
    args = parser.parse_args()
    write_manifest(
        root=args.root,
        output=args.output,
        version=args.version,
        commit=args.commit,
        tree=args.tree,
        artifacts=args.artifacts,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
