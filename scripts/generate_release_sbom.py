#!/usr/bin/env python3
"""Generate a deterministic CycloneDX inventory from committed release evidence."""

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


def _declared_license_inventory(
    audit_path: Path,
) -> dict[tuple[str, str, str], str]:
    ecosystem: str | None = None
    inventory: dict[tuple[str, str, str], str] = {}
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        if line == "## Backend (Python)":
            ecosystem = "python"
            continue
        if line == "## Console (npm)":
            ecosystem = "javascript"
            continue
        if line.startswith("## "):
            ecosystem = None
            continue
        if ecosystem is None or not line.startswith("|"):
            continue
        columns = [column.strip() for column in line.split("|")[1:-1]]
        if len(columns) != 4 or columns[0] in {"Package", "---"}:
            continue
        name, version, declared_license, pinned = columns
        if pinned != "yes":
            raise ValueError(
                f"dependency audit contains an unpinned package: {name} {version}"
            )
        key_name = _normalise_python_name(name) if ecosystem == "python" else name
        key = (ecosystem, key_name, version)
        if key in inventory:
            raise ValueError(f"dependency audit contains a duplicate package: {key}")
        inventory[key] = declared_license
    return inventory


def _apply_declared_licenses(
    components: list[dict[str, object]],
    inventory: dict[tuple[str, str, str], str],
    javascript_runtime_references: set[str],
) -> None:
    for component in components:
        ecosystem = str(component["properties"][0]["value"])
        name = str(component["name"])
        key_name = _normalise_python_name(name) if ecosystem == "python" else name
        key = (ecosystem, key_name, str(component["version"]))
        bundled = (
            ecosystem == "javascript"
            and str(component["bom-ref"]) in javascript_runtime_references
        )
        if ecosystem == "javascript":
            distribution_class = (
                "frontend-runtime-bundled-into-console-assets"
                if bundled
                else "frontend-build-package-not-distributed-as-package"
            )
        else:
            distribution_class = "lock-resolved-package"
        component["properties"].extend(
            [
                {
                    "name": "qualsight:distribution-class",
                    "value": distribution_class,
                },
                {
                    "name": "qualsight:bundled",
                    "value": "true" if bundled else "false",
                },
            ]
        )
        if key not in inventory:
            component["properties"].extend(
                [
                    {
                        "name": "qualsight:license-evidence",
                        "value": "not-recorded-for-current-platform",
                    },
                    {
                        "name": "qualsight:license-evidence-kind",
                        "value": "locked-conditional-package-not-installed-on-audit-host",
                    },
                ]
            )
            continue
        declared_license = inventory[key]
        component["properties"].extend(
            [
                {
                    "name": "qualsight:license-evidence",
                    "value": "docs/DEPENDENCY_AUDIT.md",
                },
                {
                    "name": "qualsight:license-evidence-kind",
                    "value": "installed-package-declared-metadata-not-rights-review",
                },
            ]
        )
        if declared_license != "NOT DECLARED":
            component["licenses"] = [
                {"license": {"name": declared_license}}
            ]


def _resource_component_type(kind: str) -> str:
    if "DATASET" in kind or kind in {"CONFORMANCE_INPUT", "EXTERNAL_SCORE_ARTIFACT"}:
        return "data"
    if "MATCHER" in kind or "DETECTOR" in kind:
        return "machine-learning-model"
    if "ENGINE_RUNTIME" in kind:
        return "application"
    return "file"


BETA_REQUIRED_RESOURCE_IDS = {
    "ofiq-v1-1-2-linux-runtime",
    "ofiq-r-01-conformance-input",
    "lfw-funneled-local-candidate",
    "adaface-ir101-local-candidate",
    "elasticface-arc-r100-local-candidate",
    "transface-s-glint360k-local-candidate",
    "insightface-scrfd-10g-local-candidate",
    "external-scores-lfw-view2-insightface-buffalo-l",
}


def _resource_components(
    registry_path: Path,
    checkpoint_acquisition_path: Path,
) -> tuple[list[dict[str, object]], set[str]]:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    checkpoint_acquisition = json.loads(
        checkpoint_acquisition_path.read_text(encoding="utf-8")
    )
    downloadable_resources = {
        entry["resource_id"] for entry in checkpoint_acquisition["entries"]
    }
    registered_resources = {
        resource["resource_id"] for resource in registry["resources"]
    }
    unknown_downloads = downloadable_resources - registered_resources
    if unknown_downloads:
        raise ValueError(
            "checkpoint acquisition manifest names unregistered resources: "
            + ", ".join(sorted(unknown_downloads))
        )
    missing_beta_resources = BETA_REQUIRED_RESOURCE_IDS - registered_resources
    if missing_beta_resources:
        raise ValueError(
            "controlled beta requires unregistered resources: "
            + ", ".join(sorted(missing_beta_resources))
        )
    components: list[dict[str, object]] = []
    admitted_references: set[str] = set()
    for resource in registry["resources"]:
        resource_id = resource["resource_id"]
        kind = resource["kind"]
        state = resource["state"]
        version = resource["version"]
        reference = (
            "pkg:generic/qualsight-governed-resource/"
            f"{quote(resource_id, safe='')}@{quote(version, safe='')}"
        )
        if state == "BLOCKED" and "ENGINE_RUNTIME" in kind:
            distribution_class = "externally-blocked-native-resolution"
        elif state != "BLOCKED" and resource_id in downloadable_resources:
            distribution_class = "runtime-downloadable-resource-manifest"
        else:
            distribution_class = "external-local-resource-manifest"
        artifacts = resource.get("artifacts", [])
        roles = sorted({artifact["role"] for artifact in artifacts})
        component: dict[str, object] = {
            "type": _resource_component_type(kind),
            "scope": (
                "excluded"
                if state == "BLOCKED"
                else "required"
                if resource_id in BETA_REQUIRED_RESOURCE_IDS
                else "optional"
            ),
            "bom-ref": reference,
            "name": resource_id,
            "version": version,
            "properties": [
                {
                    "name": "qualsight:ecosystem",
                    "value": "governed-scientific-resource",
                },
                {"name": "qualsight:resource-kind", "value": kind},
                {"name": "qualsight:admission-state", "value": state},
                {
                    "name": "qualsight:distribution-class",
                    "value": distribution_class,
                },
                {
                    "name": "qualsight:redistribution-status",
                    "value": resource["redistribution_status"],
                },
                {
                    "name": "qualsight:third-party-rights-status",
                    "value": resource["third_party_rights_status"],
                },
                {"name": "qualsight:bundled", "value": "false"},
                {
                    "name": "qualsight:registered-artifact-count",
                    "value": str(len(artifacts)),
                },
                {
                    "name": "qualsight:registered-artifact-roles",
                    "value": ",".join(roles),
                },
                {
                    "name": "qualsight:license-evidence",
                    "value": "resources/resource-registry.json",
                },
            ],
        }
        if resource_id in downloadable_resources:
            component["properties"].append(
                {
                    "name": "qualsight:acquisition-evidence",
                    "value": "resources/manifests/matcher-checkpoint-acquisition-v1.json",
                }
            )
        license_names: list[str] = []
        for license_record in resource.get("licenses", []):
            expression = license_record.get("expression")
            evidence_path = license_record.get("artifact_path")
            if not isinstance(expression, str) or not expression:
                raise ValueError(f"resource {resource_id} has invalid license metadata")
            if not isinstance(evidence_path, str) or not evidence_path:
                raise ValueError(f"resource {resource_id} has license metadata without evidence")
            license_names.append(expression)
        if license_names:
            component["licenses"] = [
                {"license": {"name": name}} for name in sorted(set(license_names))
            ]
        if isinstance(version, str) and version.startswith("sha256:"):
            content = version.removeprefix("sha256:")
            if len(content) != 64:
                raise ValueError(f"resource {resource_id} has an invalid SHA-256 version")
            component["hashes"] = [{"alg": "SHA-256", "content": content.upper()}]
        components.append(component)
        if state != "BLOCKED" and resource_id in BETA_REQUIRED_RESOURCE_IDS:
            admitted_references.add(reference)
    return components, admitted_references


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


def _dependency_closure(
    graph: dict[str, set[str]],
    roots: set[str],
) -> set[str]:
    closure: set[str] = set()
    pending = list(roots)
    while pending:
        reference = pending.pop()
        if reference in closure:
            continue
        closure.add(reference)
        pending.extend(graph.get(reference, ()))
    return closure


def build_sbom(root: Path, commit_timestamp: str | None = None) -> dict[str, object]:
    root = root.resolve(strict=True)
    uv_lock = root / "uv.lock"
    pnpm_lock = root / "pnpm-lock.yaml"
    dependency_audit = root / "docs" / "DEPENDENCY_AUDIT.md"
    resource_registry = root / "resources" / "resource-registry.json"
    checkpoint_acquisition = (
        root / "resources" / "manifests" / "matcher-checkpoint-acquisition-v1.json"
    )
    backend_document = tomllib.loads(
        (root / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    )
    backend_project = backend_document["project"]
    python_components, python_graph, python_refs = _python_lock(uv_lock)
    javascript_components, javascript_graph, javascript_refs, frontend_importer = _javascript_lock(pnpm_lock)
    frontend_runtime_roots: set[str] = set()
    for name, record in frontend_importer.get("dependencies", {}).items():
        version = record["version"] if isinstance(record, dict) else record
        dependency_ref = javascript_refs.get((name, version.split("(", 1)[0]))
        if dependency_ref is None:
            raise ValueError(
                f"frontend dependency is absent from pnpm-lock.yaml: {name}@{version}"
            )
        frontend_runtime_roots.add(dependency_ref)
    javascript_runtime_references = _dependency_closure(
        javascript_graph,
        frontend_runtime_roots,
    )
    package_components = python_components + javascript_components
    _apply_declared_licenses(
        package_components,
        _declared_license_inventory(dependency_audit),
        javascript_runtime_references,
    )
    resource_components, admitted_resource_refs = _resource_components(
        resource_registry,
        checkpoint_acquisition,
    )
    build_ref = f"pkg:generic/qualsight-release-toolchain@{quote(backend_project['version'])}"
    build_component = {
        "type": "application",
        "scope": "optional",
        "bom-ref": build_ref,
        "name": "QualSight release toolchain",
        "version": backend_project["version"],
        "purl": build_ref,
        "properties": [
            {"name": "qualsight:ecosystem", "value": "build"},
            {
                "name": "qualsight:distribution-class",
                "value": "release-build-tool-not-runtime-content",
            },
        ],
    }
    components = package_components + resource_components + [build_component]
    references = [str(component["bom-ref"]) for component in components]
    if len(references) != len(set(references)):
        raise ValueError("dependency locks produced duplicate CycloneDX component references")
    identity = hashlib.sha256(
        b"\0".join(
            (
                uv_lock.read_bytes(),
                pnpm_lock.read_bytes(),
                dependency_audit.read_bytes(),
                resource_registry.read_bytes(),
                checkpoint_acquisition.read_bytes(),
            )
        )
    ).hexdigest()
    application_ref = f"pkg:pypi/{backend_project['name']}@{backend_project['version']}"
    application_dependencies: set[str] = {build_ref, *admitted_resource_refs}
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
    application_dependencies.update(frontend_runtime_roots)
    for name, record in frontend_importer.get("devDependencies", {}).items():
            version = record["version"] if isinstance(record, dict) else record
            dependency_ref = javascript_refs.get((name, version.split("(", 1)[0]))
            if dependency_ref is None:
                raise ValueError(f"frontend build dependency is absent from pnpm-lock.yaml: {name}@{version}")
            build_dependencies.add(dependency_ref)
    graph = {
        **python_graph,
        **javascript_graph,
        **{str(component["bom-ref"]): set() for component in resource_components},
        build_ref: build_dependencies,
    }
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
                    {
                        "name": "qualsight:distribution-class",
                        "value": "unsigned-nonportable-controlled-beta-source-release",
                    },
                    {"name": "qualsight:uv-lock-sha256", "value": _sha256(uv_lock)},
                    {"name": "qualsight:pnpm-lock-sha256", "value": _sha256(pnpm_lock)},
                    {
                        "name": "qualsight:dependency-audit-sha256",
                        "value": _sha256(dependency_audit),
                    },
                    {
                        "name": "qualsight:resource-registry-sha256",
                        "value": _sha256(resource_registry),
                    },
                    {
                        "name": "qualsight:checkpoint-acquisition-manifest-sha256",
                        "value": _sha256(checkpoint_acquisition),
                    },
                    {
                        "name": "qualsight:dependency-graph",
                        "value": "uv and pnpm lock edges plus admitted external resource references; pnpm peer variants unioned by package and version",
                    },
                    {
                        "name": "qualsight:inventory-boundary",
                        "value": "package locks and governed resource manifests; no resource bytes, native OS package closure, or host inventory bundled",
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
