#!/usr/bin/env python3
"""Generate a deterministic CycloneDX SBOM from the repository's real lock files."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import yaml


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _commit_timestamp(root: Path) -> str:
    value = subprocess.run(
        ["git", "show", "-s", "--format=%cI", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _python_ref(name: str, version: str) -> str:
    return f"pkg:pypi/{quote(name)}@{quote(version)}"


def _normalise_python_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _python_lock(
    lock_path: Path,
) -> tuple[list[dict[str, object]], dict[str, set[str]], dict[str, str]]:
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    components: list[dict[str, object]] = []
    package_by_name: dict[str, tuple[str, str]] = {}
    for package in lock["package"]:
        if "registry" not in package.get("source", {}):
            continue
        normalised = _normalise_python_name(package["name"])
        if normalised in package_by_name:
            raise ValueError(f"uv lock contains multiple versions of {normalised}")
        package_by_name[normalised] = (package["name"], package["version"])
    graph: dict[str, set[str]] = {}
    for package in lock["package"]:
        source = package.get("source", {})
        if "registry" not in source:
            continue
        name = package["name"]
        version = package["version"]
        hashes: set[str] = set()
        artifacts = [package.get("sdist"), *package.get("wheels", [])]
        for artifact in artifacts:
            if not artifact:
                continue
            algorithm, separator, content = artifact["hash"].partition(":")
            if separator != ":" or algorithm.lower() != "sha256" or len(content) != 64:
                raise ValueError(f"unsupported uv artifact hash for {name} {version}")
            hashes.add(content.upper())
        component: dict[str, object] = {
            "type": "library",
            "bom-ref": _python_ref(name, version),
            "name": name,
            "version": version,
            "purl": _python_ref(name, version),
            "properties": [{"name": "qualsight:ecosystem", "value": "python"}],
        }
        if hashes:
            component["hashes"] = [
                {"alg": "SHA-256", "content": content} for content in sorted(hashes)
            ]
        components.append(component)
        dependencies: set[str] = set()
        for dependency in package.get("dependencies", []):
            target = package_by_name.get(_normalise_python_name(dependency["name"]))
            if target is None:
                raise ValueError(
                    f"uv dependency {name} -> {dependency['name']} has no locked registry package"
                )
            dependencies.add(_python_ref(*target))
        graph[_python_ref(name, version)] = dependencies
    refs_by_name = {
        normalised: _python_ref(name, version)
        for normalised, (name, version) in package_by_name.items()
    }
    return components, graph, refs_by_name


def _pnpm_identity(key: str) -> tuple[str, str]:
    identity = key.split("(", 1)[0]
    boundary = identity.rfind("@")
    if boundary <= 0 or boundary == len(identity) - 1:
        raise ValueError(f"unsupported pnpm package identity: {key!r}")
    return identity[:boundary], identity[boundary + 1 :]


def _javascript_ref(name: str, version: str) -> str:
    encoded_name = "/".join(quote(part, safe="") for part in name.split("/"))
    return f"pkg:npm/{encoded_name}@{quote(version, safe='')}"


def _javascript_lock(
    lock_path: Path,
) -> tuple[list[dict[str, object]], dict[str, set[str]], dict[tuple[str, str], str], dict[str, object]]:
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    components: list[dict[str, object]] = []
    refs_by_identity: dict[tuple[str, str], str] = {}
    for key, package in lock["packages"].items():
        name, version = _pnpm_identity(key)
        purl = _javascript_ref(name, version)
        if (name, version) in refs_by_identity:
            raise ValueError(f"pnpm lock contains duplicate package identity: {name}@{version}")
        refs_by_identity[(name, version)] = purl
        integrity = package.get("resolution", {}).get("integrity")
        if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
            raise ValueError(f"pnpm package lacks a SHA-512 integrity value: {key}")
        try:
            digest = base64.b64decode(integrity.removeprefix("sha512-"), validate=True)
        except ValueError as error:
            raise ValueError(f"invalid pnpm integrity value for {key}") from error
        if len(digest) != 64:
            raise ValueError(f"pnpm integrity digest has the wrong length for {key}")
        components.append(
            {
                "type": "library",
                "bom-ref": purl,
                "name": name,
                "version": version,
                "purl": purl,
                "hashes": [{"alg": "SHA-512", "content": digest.hex().upper()}],
                "properties": [{"name": "qualsight:ecosystem", "value": "javascript"}],
            }
        )
    graph = {reference: set() for reference in refs_by_identity.values()}
    for snapshot_key, snapshot in lock["snapshots"].items():
        source_identity = _pnpm_identity(snapshot_key)
        source_ref = refs_by_identity.get(source_identity)
        if source_ref is None:
            raise ValueError(f"pnpm snapshot has no package identity: {snapshot_key}")
        for group in ("dependencies", "optionalDependencies"):
            for dependency_name, dependency_version in snapshot.get(group, {}).items():
                target_identity = (dependency_name, dependency_version.split("(", 1)[0])
                target_ref = refs_by_identity.get(target_identity)
                if target_ref is None:
                    raise ValueError(
                        f"pnpm dependency {snapshot_key} -> {dependency_name}@{dependency_version} is unresolved"
                    )
                graph[source_ref].add(target_ref)
    return components, graph, refs_by_identity, lock["importers"]["frontend"]


def _normalise_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def build_sbom(root: Path, commit_timestamp: str | None = None) -> dict[str, object]:
    root = root.resolve(strict=True)
    uv_lock = root / "uv.lock"
    pnpm_lock = root / "pnpm-lock.yaml"
    backend_document = tomllib.loads(
        (root / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    )
    backend_project = backend_document["project"]
    python_components, python_graph, python_refs = _python_lock(uv_lock)
    javascript_components, javascript_graph, javascript_refs, frontend_importer = _javascript_lock(pnpm_lock)
    build_ref = f"pkg:generic/qualsight-release-toolchain@{quote(backend_project['version'])}"
    build_component = {
        "type": "application",
        "scope": "optional",
        "bom-ref": build_ref,
        "name": "QualSight release toolchain",
        "version": backend_project["version"],
        "purl": build_ref,
        "properties": [{"name": "qualsight:ecosystem", "value": "build"}],
    }
    components = python_components + javascript_components + [build_component]
    references = [str(component["bom-ref"]) for component in components]
    if len(references) != len(set(references)):
        raise ValueError("dependency locks produced duplicate CycloneDX component references")
    identity = hashlib.sha256(
        uv_lock.read_bytes() + b"\0" + pnpm_lock.read_bytes()
    ).hexdigest()
    application_ref = f"pkg:pypi/{backend_project['name']}@{backend_project['version']}"
    application_dependencies: set[str] = {build_ref}
    build_dependencies: set[str] = set()
    for requirement in backend_project["dependencies"]:
        match = re.match(r"[A-Za-z0-9_.-]+", requirement)
        if match is None:
            raise ValueError(f"unsupported backend dependency requirement: {requirement}")
        dependency_ref = python_refs.get(_normalise_python_name(match.group(0)))
        if dependency_ref is None:
            raise ValueError(f"backend dependency is absent from uv.lock: {requirement}")
        application_dependencies.add(dependency_ref)
    for requirement in backend_document.get("dependency-groups", {}).get("dev", []):
        match = re.match(r"[A-Za-z0-9_.-]+", requirement)
        if match is None:
            raise ValueError(f"unsupported backend build dependency requirement: {requirement}")
        dependency_ref = python_refs.get(_normalise_python_name(match.group(0)))
        if dependency_ref is None:
            raise ValueError(f"backend build dependency is absent from uv.lock: {requirement}")
        build_dependencies.add(dependency_ref)
    for name, record in frontend_importer.get("dependencies", {}).items():
        version = record["version"] if isinstance(record, dict) else record
        dependency_ref = javascript_refs.get((name, version.split("(", 1)[0]))
        if dependency_ref is None:
            raise ValueError(f"frontend dependency is absent from pnpm-lock.yaml: {name}@{version}")
        application_dependencies.add(dependency_ref)
    for name, record in frontend_importer.get("devDependencies", {}).items():
            version = record["version"] if isinstance(record, dict) else record
            dependency_ref = javascript_refs.get((name, version.split("(", 1)[0]))
            if dependency_ref is None:
                raise ValueError(f"frontend build dependency is absent from pnpm-lock.yaml: {name}@{version}")
            build_dependencies.add(dependency_ref)
    graph = {**python_graph, **javascript_graph, build_ref: build_dependencies}
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, identity)}",
        "version": 1,
        "metadata": {
            "timestamp": _normalise_timestamp(commit_timestamp) if commit_timestamp else _commit_timestamp(root),
            "component": {
                "type": "application",
                "bom-ref": application_ref,
                "name": "QualSight",
                "version": backend_project["version"],
                "properties": [
                    {"name": "qualsight:uv-lock-sha256", "value": _sha256(uv_lock)},
                    {"name": "qualsight:pnpm-lock-sha256", "value": _sha256(pnpm_lock)},
                    {
                        "name": "qualsight:dependency-graph",
                        "value": "uv and pnpm lock edges; pnpm peer variants unioned by package and version",
                    },
                ],
            },
        },
        "components": sorted(components, key=lambda item: str(item["bom-ref"])),
        "dependencies": [
            {"ref": application_ref, "dependsOn": sorted(application_dependencies)},
            *(
                {"ref": reference, "dependsOn": sorted(graph[reference])}
                for reference in sorted(references)
            ),
        ],
    }


def write_sbom(root: Path, output: Path, commit_timestamp: str | None = None) -> None:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite SBOM: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    value = build_sbom(root, commit_timestamp)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit-timestamp")
    args = parser.parse_args()
    write_sbom(args.root, args.output, args.commit_timestamp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
