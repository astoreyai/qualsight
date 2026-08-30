"""Closed catalog of the real capabilities admitted by this beta.

This is deliberately not a plugin loader.  Catalog records may describe only
code-owned drivers already exercised by QualSight.  A registry record, URL, or
operator string can never select an arbitrary Python module, package, command,
or remote service.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any

from .dataset_acquisition import MANIFEST_NAME as DATASET_MANIFEST_NAME, VerificationDatasetStore
from .datasets import LFW_RESOURCE_ID
from .execution.registry_binding import ResourceRegistry
from .matchers.conditions import CONDITION_DEFINITIONS, SCRFD_CONDITIONS, ConditionId
from .matchers.matrix import LEAF_IDS, SCRFD_RESOURCE_ID
from .matchers.runtime import MatcherId
from .transforms.operators import PARAMETER_CONTRACTS, PARAMETER_NAMES


CATALOG_SCHEMA_VERSION = "1.1.0"
CATALOG_MODE = "CLOSED_ALLOWLIST"

ANALYSIS_PARAMETER_CONTRACTS: dict[str, dict[str, Any]] = {
    "target_fmr": {"value_type": "number", "minimum": 0, "maximum": 1, "minimum_exclusive": True, "maximum_exclusive": True, "allow_inf_nan": False, "required": True, "guidance": "probability greater than 0 and less than 1"},
    "bootstrap_replicates": {"value_type": "integer", "minimum": 0, "maximum": 100_000, "minimum_exclusive": False, "maximum_exclusive": False, "required": True, "guidance": "whole-number count from 0 through 100,000"},
    "quality_threshold": {"value_type": "number", "allow_inf_nan": False, "required": False, "blank_behavior": "automatic 20% policy cutoff", "guidance": "finite scalar score"},
    "matcher_threshold": {"value_type": "number", "required": True, "allow_inf_nan": False, "guidance": "finite matcher score"},
    "owner_pair_seed": {"value_type": "integer", "minimum": 0, "minimum_exclusive": False, "required": True, "guidance": "non-negative whole-number seed"},
}

MATCHER_DRIVER_IDS: dict[MatcherId, str] = {
    MatcherId.ADAFACE_IR101: "qualsight.matcher.adaface-ir101.v1",
    MatcherId.ELASTICFACE_ARC_R100: "qualsight.matcher.elasticface-arc-r100.v1",
    MatcherId.MAGFACE_IRESNET100: "qualsight.matcher.magface-iresnet100.v1",
    MatcherId.TOPOFR_R100: "qualsight.matcher.topofr-r100.v1",
    MatcherId.TRANSFACE_B: "qualsight.matcher.transface-b.v1",
    MatcherId.TRANSFACE_S: "qualsight.matcher.transface-s.v1",
}

SCRFD_PREPROCESSOR_DRIVER_ID = "qualsight.preprocessor.scrfd10g-arcface112.v1"
PREALIGNED_PREPROCESSOR_DRIVER_ID = "qualsight.preprocessor.author-prealigned-arcface112.v1"
DATASET_DRIVER_IDS = {LFW_RESOURCE_ID: "qualsight.dataset.lfw-funneled.v1"}


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _registered_file_status(artifact: dict[str, Any]) -> dict[str, Any]:
    path = Path(artifact["path"])
    expected_size = int(artifact["size_bytes"])
    expected_sha256 = artifact["sha256"]
    result = {
        "path": str(path),
        "registered_sha256": expected_sha256,
        "registered_size_bytes": expected_size,
        "observed_sha256": None,
        "observed_size_bytes": None,
    }
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return {**result, "state": "MISSING", "reason": "registered file is missing"}
    except OSError as error:
        return {**result, "state": "DRIFTED", "reason": f"cannot inspect registered file: {error}"}
    result["observed_size_bytes"] = observed.st_size
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        return {**result, "state": "DRIFTED", "reason": "registered path is not a regular file"}
    if observed.st_size != expected_size:
        return {**result, "state": "DRIFTED", "reason": "registered file size differs"}
    try:
        digest = _sha256_path(path)
    except OSError as error:
        return {**result, "state": "DRIFTED", "reason": f"cannot hash registered file: {error}"}
    result["observed_sha256"] = digest
    if digest != expected_sha256:
        return {**result, "state": "DRIFTED", "reason": "registered file SHA-256 differs"}
    return {**result, "state": "VERIFIED", "reason": None}


def _lfw_tree_status(
    raw: dict[str, Any],
    tree_check: dict[str, Any],
    inventory_check: dict[str, Any],
) -> dict[str, Any]:
    manifest_artifact = next(
        item
        for item in raw["artifacts"]
        if item["role"] == "MANIFEST" and item["path"] == tree_check["manifest_path"]
    )
    manifest = _registered_file_status(manifest_artifact)
    result: dict[str, Any] = {
        "root": tree_check["root"],
        "manifest": manifest,
        "registered_tree_sha256": tree_check["sha256"],
        "registered_file_count": int(tree_check["expected_count"]),
        "registered_identity_count": int(inventory_check["identity_directory_count"]),
        "observed_tree_sha256": None,
        "observed_file_count": None,
        "observed_identity_count": None,
    }
    if manifest["state"] != "VERIFIED":
        return {
            **result,
            "state": manifest["state"],
            "reason": f"registered tree manifest is {manifest['state'].lower()}: {manifest['reason']}",
        }

    records: dict[str, tuple[int, str]] = {}
    try:
        with Path(manifest_artifact["path"]).open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if set(row) != {"path", "sha256", "size_bytes"}:
                    raise ValueError(f"manifest row {line_number} has an unexpected schema")
                relative = row["path"]
                pure = PurePosixPath(relative)
                if (
                    not isinstance(relative, str)
                    or not relative
                    or pure.is_absolute()
                    or pure.as_posix() != relative
                    or ".." in pure.parts
                    or "\\" in relative
                    or len(pure.parts) != 2
                ):
                    raise ValueError(f"manifest row {line_number} has an unsafe LFW path")
                if relative in records:
                    raise ValueError(f"manifest row {line_number} duplicates {relative}")
                size_bytes = row["size_bytes"]
                digest = row["sha256"]
                if (
                    not isinstance(size_bytes, int)
                    or isinstance(size_bytes, bool)
                    or size_bytes < 1
                    or not isinstance(digest, str)
                    or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                ):
                    raise ValueError(f"manifest row {line_number} has an invalid byte identity")
                records[relative] = (size_bytes, digest)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return {**result, "state": "DRIFTED", "reason": f"cannot parse registered tree manifest: {error}"}

    expected_count = int(tree_check["expected_count"])
    if len(records) != expected_count:
        return {**result, "state": "DRIFTED", "reason": "registered tree manifest count differs"}

    root = Path(tree_check["root"])
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return {**result, "state": "MISSING", "reason": "registered tree root is missing"}
    except OSError as error:
        return {**result, "state": "DRIFTED", "reason": f"cannot inspect registered tree root: {error}"}
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        return {**result, "state": "DRIFTED", "reason": "registered tree root is not a directory"}

    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    try:
        for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            for name in directory_names:
                directory = current_path / name
                directory_stat = directory.lstat()
                if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
                    return {**result, "state": "DRIFTED", "reason": f"tree directory is not a regular directory: {directory}"}
                observed_directories.add(directory.relative_to(root).as_posix())
            for name in file_names:
                path = current_path / name
                file_stat = path.lstat()
                if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
                    return {**result, "state": "DRIFTED", "reason": f"tree member is not a regular file: {path}"}
                if Path(name).match(inventory_check["file_glob"]):
                    observed_files.add(path.relative_to(root).as_posix())
    except OSError as error:
        return {**result, "state": "DRIFTED", "reason": f"cannot walk registered tree: {error}"}

    expected_paths = set(records)
    expected_directories = {PurePosixPath(relative).parent.as_posix() for relative in records}
    result["observed_file_count"] = len(observed_files)
    result["observed_identity_count"] = len(observed_directories)
    if observed_files != expected_paths:
        return {**result, "state": "DRIFTED", "reason": "registered tree file membership differs"}
    if observed_directories != expected_directories:
        return {**result, "state": "DRIFTED", "reason": "registered tree directory membership differs"}
    if len(observed_directories) != int(inventory_check["identity_directory_count"]):
        return {**result, "state": "DRIFTED", "reason": "registered identity-directory count differs"}

    tree_digest = hashlib.sha256()
    try:
        for relative in sorted(records, key=lambda value: value.encode("utf-8")):
            expected_size, expected_digest = records[relative]
            path = root / relative
            observed_size = path.stat().st_size
            observed_digest = _sha256_path(path)
            if observed_size != expected_size or observed_digest != expected_digest:
                return {**result, "state": "DRIFTED", "reason": f"registered tree member bytes differ: {relative}"}
            tree_digest.update(f"{observed_digest}  {relative}\n".encode("utf-8"))
    except OSError as error:
        return {**result, "state": "DRIFTED", "reason": f"cannot hash registered tree: {error}"}
    observed_tree_sha256 = tree_digest.hexdigest()
    result["observed_tree_sha256"] = observed_tree_sha256
    if observed_tree_sha256 != tree_check["sha256"]:
        return {**result, "state": "DRIFTED", "reason": "registered tree SHA-256 differs"}
    return {**result, "state": "VERIFIED", "reason": None}


def preprocessing_profiles(registry: ResourceRegistry) -> list[dict[str, Any]]:
    """Return representation-gated executable preprocessing profiles from code truth."""

    detector = registry.resource(SCRFD_RESOURCE_ID)
    raw_detector = registry.raw_resource(SCRFD_RESOURCE_ID)
    detector_model = next(item for item in raw_detector["artifacts"] if item["role"] == "MODEL")
    detector_status = _registered_file_status(detector_model)
    profiles: list[dict[str, Any]] = []
    for condition_id in ConditionId:
        definition = CONDITION_DEFINITIONS[condition_id]
        uses_detector = condition_id in SCRFD_CONDITIONS
        profiles.append(
            {
                "addon_id": condition_id.value,
                "kind": "PREPROCESSING",
                "subtype": "MATCHER_PREPROCESSING",
                "display_name": (
                    "Shared adaptive SCRFD crop · JPEG 95"
                    if condition_id is ConditionId.ADAPTIVE_LARGEST_JPEG95
                    else (
                        "Native fixed-640 SCRFD crop · in memory"
                        if condition_id is ConditionId.FIXED640_HIGHEST_INMEMORY
                        else "Publisher-prealigned ArcFace 112 · verified pass-through"
                    )
                ),
                "version": "1.0.0",
                "driver_id": SCRFD_PREPROCESSOR_DRIVER_ID if uses_detector else PREALIGNED_PREPROCESSOR_DRIVER_ID,
                "installation_state": "BUILTIN",
                "runtime_state": (
                    "READY"
                    if not uses_detector or detector_status["state"] == "VERIFIED"
                    else detector_status["state"]
                ),
                "blocking_reasons": (
                    []
                    if not uses_detector or detector_status["state"] == "VERIFIED"
                    else [detector_status["reason"]]
                ),
                "detector_resource_id": detector.resource_id if uses_detector else None,
                "detector_resource_sha256": detector.resource_record_sha256 if uses_detector else None,
                "input_representation": (
                    "SOURCE_IMAGE_REQUIRING_DETECTION"
                    if uses_detector
                    else "AUTHOR_PREALIGNED_ARCFACE112"
                ),
                "definition": dict(definition),
                "compatible_matcher_ids": [matcher_id.value for matcher_id in MatcherId],
                "leaf_ids_by_matcher": {
                    matcher_id.value: LEAF_IDS[(matcher_id, condition_id)]
                    for matcher_id in MatcherId
                },
                "selection_scope": "PER_RUN",
            }
        )
    return profiles


def degradation_operators() -> list[dict[str, Any]]:
    """Return the executable, bounded degradation operators separately from crops."""

    return [
        {
            "addon_id": f"transform:{kind}:v1",
            "kind": "PREPROCESSING",
            "subtype": "CONTROLLED_DEGRADATION",
            "display_name": kind.replace("_", " ").title(),
            "version": "1.0.0",
            "driver_id": f"qualsight.transform.{kind}.v1",
            "installation_state": "BUILTIN",
            "runtime_state": "READY",
            "operator": kind,
            "parameter": parameter,
            "parameter_contract": dict(PARAMETER_CONTRACTS[kind]),
            "selection_scope": "PER_DERIVED_SET",
        }
        for kind, parameter in PARAMETER_NAMES.items()
    ]


def dataset_addons(registry: ResourceRegistry, data_dir: Path | None = None) -> list[dict[str, Any]]:
    """Describe supported datasets from current registered bytes, not registry metadata alone."""

    resource = registry.resource(LFW_RESOURCE_ID)
    raw = registry.raw_resource(LFW_RESOURCE_ID)
    tree_check = next(item for item in raw["checks"] if item["type"] == "tree_manifest")
    inventory_check = next(
        item for item in raw["checks"] if item["type"] == "directory_inventory"
    )
    archive = next(
        item
        for item in raw["artifacts"]
        if item["role"] == "DATA" and item["path"].endswith("lfw-funneled.tgz")
    )
    archive_verification = _registered_file_status(archive)
    tree_verification = _lfw_tree_status(raw, tree_check, inventory_check)
    verification_items = (archive_verification, tree_verification)
    verified = all(item["state"] == "VERIFIED" for item in verification_items)
    installation_state = (
        "VERIFIED_EXTERNAL_PATH"
        if verified
        else (
            "MISSING"
            if any(item["state"] == "MISSING" for item in verification_items)
            else "DRIFTED"
        )
    )
    blocking_reasons = [
        f"{name}: {item['reason']}"
        for name, item in (("archive", archive_verification), ("tree", tree_verification))
        if item["state"] != "VERIFIED"
    ]
    datasets = [
        {
            "addon_id": resource.resource_id,
            "kind": "DATASET",
            "subtype": "IDENTITY_LABELED_DATASET",
            "display_name": resource.name,
            "version": resource.version,
            "driver_id": DATASET_DRIVER_IDS[resource.resource_id],
            "registry_state": resource.state,
            "resource_record_sha256": resource.resource_record_sha256,
            "installation_state": installation_state,
            "runtime_state": "READY_TO_IMPORT" if verified else "BLOCKED",
            "blocking_reasons": blocking_reasons,
            "verification": {
                "state": "VERIFIED" if verified else installation_state,
                "archive": archive_verification,
                "tree": tree_verification,
            },
            "managed": False,
            "local_root": resource.source.local_root,
            "sample_count": int(tree_check["expected_count"]),
            "identity_count": int(inventory_check["identity_directory_count"]),
            "tree_sha256": tree_check["sha256"],
            "archive": {
                "path": archive["path"],
                "sha256": archive["sha256"],
                "size_bytes": int(archive["size_bytes"]),
                "state": archive_verification["state"],
            },
            "rights": {
                "third_party": raw["third_party_rights_status"],
                "research_use": resource.research_use_status,
                "redistribution": resource.redistribution_status,
            },
            "classification": resource.data_classification,
            "approved_scopes": list(resource.approved_scopes),
            "acquisition": {
                "available": False,
                "refusal_code": "ACQUISITION_CONTRACT_MISSING",
                "source_uri": raw["source"]["uri"],
                "reason": (
                    "The current registry records existing external bytes and an HTTP source URI, "
                    "but no governed HTTPS mirror/redirect policy or acquisition receipt contract. "
                    "QualSight can verify and import the installed dataset but will not claim a safe download yet."
                ),
            },
        }
    ]
    if data_dir is not None:
        store = VerificationDatasetStore(
            Path(__file__).resolve().parents[3] / "resources" / "manifests" / DATASET_MANIFEST_NAME,
            data_dir,
        )
        for protocol in store.protocols.values():
            status = store.status(protocol.addon_id)
            datasets.append(
                {
                    "addon_id": protocol.addon_id,
                    "kind": "DATASET",
                    "subtype": "PAIR_VERIFICATION_DATASET",
                    "display_name": protocol.display_name,
                    "version": "1.0.0",
                    "driver_id": f"qualsight.dataset.{protocol.archive_directory}-pairs.v1",
                    "installation_state": status["runtime_state"],
                    "runtime_state": "READY_TO_IMPORT_AND_EXECUTE" if status["runtime_state"] == "READY" else status["runtime_state"],
                    "blocking_reasons": [] if status["runtime_state"] == "READY" else [status["extraction"]["reason"]],
                    "verification": status,
                    "managed": True,
                    "local_root": str(store.protocol_root(protocol.addon_id)),
                    "sample_count": protocol.sample_count,
                    "identity_count": None,
                    "pair_count": protocol.pair_count,
                    "mated_pair_count": protocol.mated_pair_count,
                    "nonmated_pair_count": protocol.nonmated_pair_count,
                    "rights": {
                        "third_party": "RESEARCH_NOTICE_ONLY",
                        "research_use": "OWNER_APPROVED_LOCAL_NONCOMMERCIAL_RESEARCH_PUBLISHER_DOWNLOAD_NO_REDISTRIBUTION_CLAIM",
                        "redistribution": "NOT_APPROVED_FOR_QUALSIGHT_DISTRIBUTION",
                    },
                    "classification": "SENSITIVE_BIOMETRIC_IDENTIFIABLE",
                    "approved_scopes": ["QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"],
                    "acquisition": {
                        "available": True,
                        "source_kind": "AUTHOR_PUBLISHED_HTTPS_ARCHIVE",
                        "archive_sha256": store.archive["sha256"],
                        "archive_size_bytes": store.archive["size_bytes"],
                        "shared_archive_addon_ids": list(store.protocols),
                        "rights_notice": (
                            "Local noncommercial research only. Publisher availability is not a redistribution grant; "
                            "images, crops, and embeddings remain private to this workstation."
                        ),
                    },
                }
            )
    return datasets


def catalog(
    registry: ResourceRegistry,
    matcher_assets: list[dict[str, Any]],
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Build the operator catalog from registered bytes and closed driver maps."""

    matchers = []
    for asset in matcher_assets:
        matcher_id = MatcherId(asset["matcher_id"])
        matchers.append(
            {
                "addon_id": matcher_id.value,
                "kind": "MATCHER",
                "subtype": "FACE_EMBEDDING_MATCHER",
                "driver_id": MATCHER_DRIVER_IDS[matcher_id],
                "installation_state": "VERIFIED_EXTERNAL_PATH" if asset["checkpoint"]["state"] == "VERIFIED" else asset["checkpoint"]["state"],
                **asset,
            }
        )
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "mode": CATALOG_MODE,
        "registry_id": registry.registry_id,
        "registry_sha256": registry.file_identity.sha256,
        "matchers": matchers,
        "datasets": dataset_addons(registry, data_dir),
        "preprocessing": preprocessing_profiles(registry),
        "transforms": degradation_operators(),
        "analysis_parameters": ANALYSIS_PARAMETER_CONTRACTS,
        "connections": [],
        "connection_policy": {
            "state": "NOT_ADMITTED_IN_BETA",
            "reason": (
                "No real API provider, credential reference contract, approved egress origin, or authorization to transmit "
                "biometric images or embeddings is registered. Registered external score files remain inert imports marked EXTERNALLY_COMPUTED."
            ),
            "admission_requires": [
                "a named real provider and versioned protocol driver",
                "approved HTTPS origins and OS-enforced egress policy",
                "environment-only secret references with redaction",
                "data-use authorization for every transmitted classification",
                "durable request/response provenance and exact reproduction semantics",
            ],
        },
    }
