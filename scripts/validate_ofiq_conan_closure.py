#!/usr/bin/env python3
"""Validate the governed OFIQ v1.1.2 Conan inputs and observed graph."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"OFIQ_CONAN_CLOSURE_INVALID: cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise SystemExit(f"OFIQ_CONAN_CLOSURE_INVALID: {label} must be a JSON object")
    return value


def _identity(path: Path) -> tuple[int, str]:
    if not path.is_file() or path.is_symlink():
        raise SystemExit(
            f"OFIQ_CONAN_CLOSURE_INVALID: {path} must be a regular non-symlink file"
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return path.stat().st_size, digest.hexdigest()


def _require_identity(path: Path, record: dict[str, Any], label: str) -> None:
    size, digest = _identity(path)
    if size != record.get("size_bytes") or digest != record.get("sha256"):
        raise SystemExit(
            f"OFIQ_CONAN_CLOSURE_INVALID: {label} identity mismatch: "
            f"size={size} sha256={digest}"
        )


def _lock_refs(lock: dict[str, Any], key: str) -> list[str]:
    values = lock.get(key)
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise SystemExit(f"OFIQ_CONAN_CLOSURE_INVALID: lock {key} must be a string array")
    return sorted(value.split("%", 1)[0] for value in values)


def _dependency_key(record: dict[str, Any]) -> tuple[str, str, str]:
    try:
        return record["context"], record["ref"], record["package_id"]
    except (KeyError, TypeError) as error:
        raise SystemExit(
            "OFIQ_CONAN_CLOSURE_INVALID: dependency records require context, ref, and package_id"
        ) from error


def validate(
    manifest_path: Path,
    lock_path: Path,
    profile_path: Path,
    recipe_path: Path | None,
    graph_path: Path | None,
) -> None:
    manifest = _load_object(manifest_path, "manifest")
    if manifest.get("schema_version") != "1.0.0":
        raise SystemExit("OFIQ_CONAN_CLOSURE_INVALID: unsupported manifest schema")

    lock_record = manifest.get("lock")
    profile_record = manifest.get("profile")
    source_record = manifest.get("source")
    dependencies = manifest.get("dependencies")
    if not isinstance(lock_record, dict) or not isinstance(profile_record, dict):
        raise SystemExit("OFIQ_CONAN_CLOSURE_INVALID: lock and profile records are required")
    if not isinstance(source_record, dict) or not isinstance(dependencies, list):
        raise SystemExit("OFIQ_CONAN_CLOSURE_INVALID: source and dependencies are required")

    _require_identity(lock_path, lock_record, "lock")
    _require_identity(profile_path, profile_record, "profile")
    if recipe_path is not None:
        _require_identity(
            recipe_path,
            {
                "size_bytes": source_record.get("recipe_size_bytes"),
                "sha256": source_record.get("recipe_sha256"),
            },
            "recipe",
        )

    lock = _load_object(lock_path, "lock")
    if lock.get("version") != lock_record.get("format_version"):
        raise SystemExit("OFIQ_CONAN_CLOSURE_INVALID: lock format version mismatch")
    expected_host = sorted(
        record["ref"] for record in dependencies if record.get("context") == "host"
    )
    expected_build = sorted(
        record["ref"] for record in dependencies if record.get("context") == "build"
    )
    if _lock_refs(lock, "requires") != expected_host:
        raise SystemExit("OFIQ_CONAN_CLOSURE_INVALID: host references differ from manifest")
    if _lock_refs(lock, "build_requires") != expected_build:
        raise SystemExit("OFIQ_CONAN_CLOSURE_INVALID: build references differ from manifest")
    if len({_dependency_key(record) for record in dependencies}) != len(dependencies):
        raise SystemExit("OFIQ_CONAN_CLOSURE_INVALID: duplicate dependency identity")

    if graph_path is not None:
        graph_document = _load_object(graph_path, "observed graph")
        graph = graph_document.get("graph")
        if not isinstance(graph, dict) or graph.get("error") not in (None, ""):
            raise SystemExit("OFIQ_CONAN_CLOSURE_INVALID: Conan reported a graph error")
        nodes_value = graph.get("nodes")
        if isinstance(nodes_value, dict):
            nodes = list(nodes_value.values())
        elif isinstance(nodes_value, list):
            nodes = nodes_value
        else:
            raise SystemExit("OFIQ_CONAN_CLOSURE_INVALID: observed graph nodes are missing")
        observed_records = [
            node
            for node in nodes
            if isinstance(node, dict) and node.get("ref") not in (None, "conanfile")
        ]
        expected_keys = sorted(_dependency_key(record) for record in dependencies)
        observed_keys = sorted(_dependency_key(record) for record in observed_records)
        if observed_keys != expected_keys:
            raise SystemExit(
                "OFIQ_CONAN_CLOSURE_INVALID: observed refs, contexts, or package IDs differ"
            )
        expected_revisions = {
            _dependency_key(record): record.get("observed_package_revision")
            for record in dependencies
        }
        for record in observed_records:
            binary = record.get("binary")
            if binary not in {"Build", "Cache", "Download", "Skip"}:
                raise SystemExit(
                    f"OFIQ_CONAN_CLOSURE_INVALID: unacceptable binary state {binary!r}"
                )
            if binary != "Build" and record.get("prev") != expected_revisions[_dependency_key(record)]:
                raise SystemExit(
                    f"OFIQ_CONAN_CLOSURE_INVALID: package revision changed for {record.get('ref')}"
                )

    print(
        "OFIQ_CONAN_CLOSURE_VALID "
        f"dependencies={len(dependencies)} lock_sha256={lock_record['sha256']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--recipe", type=Path)
    parser.add_argument("--graph", type=Path)
    args = parser.parse_args()
    validate(args.manifest, args.lock, args.profile, args.recipe, args.graph)


if __name__ == "__main__":
    main()
