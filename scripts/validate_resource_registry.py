#!/usr/bin/env python3
"""Validate QualSight's resource registry against real local artifacts."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import struct
import subprocess
import sys
import tarfile
from typing import Any
import zipfile


MAX_REGISTRY_BYTES = 2_000_000
MAX_RESOURCES = 256
MAX_ARTIFACTS_PER_RESOURCE = 512
MAX_CHECKS_PER_RESOURCE = 64
ALLOWED_STATES = {"APPROVED_LOCAL_RESEARCH_USE", "BLOCKED", "VERIFIED_LIMITED_USE"}
# APPROVED_LOCAL_RESEARCH_USE: the project owner has granted local, non-redistributed
# noncommercial research use of exact registered bytes and has explicitly accepted the
# residual third-party rights uncertainty; the third-party rights status stays factual and
# the resource must never be bundled or published with QualSight.
LOCAL_RESEARCH_USE_RESEARCH_STATUS_PREFIX = "OWNER_APPROVED_LOCAL_NONCOMMERCIAL_RESEARCH"
ALLOWED_THIRD_PARTY_RIGHTS_STATUSES = {
    "NO_EXPLICIT_LICENSE_PUBLIC_RESEARCH_DATASET",
    "NO_LICENSE_FOUND",
    "REPOSITORY_LICENSE_ONLY",
    "RESEARCH_NOTICE_ONLY",
    "UNRESOLVED",
    "VERIFIED_LICENSE",
}
ALLOWED_OWNER_DECISIONS = {
    "APPROVED_EVIDENCE_ACQUISITION",
    "APPROVED_LIMITED_USE",
    "APPROVED_LOCAL_NONCOMMERCIAL",
    "NOT_APPROVED",
}
ALLOWED_CLASSIFICATIONS = {
    "PUBLIC",
    "PRIVATE",
    "RESTRICTED",
    "SENSITIVE_BIOMETRIC_DERIVED",
}
ALLOWED_ARTIFACT_ROLES = {
    "ARCHIVE",
    "AUTHORIZATION",
    "BINARY",
    "CONFIGURATION",
    "DATA",
    "DOCUMENTATION",
    "EMBEDDINGS",
    "IMPLEMENTATION",
    "LICENSE",
    "MANIFEST",
    "MODEL",
    "PROTOCOL",
    "RUNTIME_LIBRARY",
    "SOURCE_RECEIPT",
    "VERSION_EVIDENCE",
}
LICENSE_COVERAGE_REQUIRED_ROLES = ALLOWED_ARTIFACT_ROLES - {
    "AUTHORIZATION",
    "LICENSE",
    "SOURCE_RECEIPT",
}
EXECUTABLE_CHECK_REQUIRED_ROLES = {
    "BINARY",
    "CONFIGURATION",
    "DATA",
    "EMBEDDINGS",
    "IMPLEMENTATION",
    "MANIFEST",
    "MODEL",
    "PROTOCOL",
    "RUNTIME_LIBRARY",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RESOURCE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REQUIREMENT_ID_RE = re.compile(r"^QS-REQ-[0-9]{3}[A-Z]?$|^QS-SRC-[0-9]{3}$")
CONSUMER_ID_RE = re.compile(r"^QS-[A-Z][0-9]{2}(?:\.[A-Z0-9]+)?$")
OFIQ_V112_ACQUISITION_SCOPE = "ACQUIRE_OFIQ_V1_1_2_FOR_EVIDENCE_REVIEW"
OFIQ_V112_ONNX_STATIC_REVIEW_SCOPE = (
    "STATIC_REVIEW_OFIQ_V1_1_2_ONNX_RUNTIME_EVIDENCE"
)
OFIQ_V112_ONNX_STATIC_REVIEW_RESTRICTIONS = [
    "Acquisition authority remains limited to acquisition, hashing, archive validation, and license review; a separate registered owner decision permits only non-executing static review of the preserved ONNX Runtime archive, package text, headers, licenses, notices, hashes, and ELF structures.",
    "Do not run the tagged build script or permit system-package installation with sudo.",
    "Do not install, execute, or redistribute the quarantined model and runtime archives until all registry blockers clear.",
]
OFIQ_V112_RESEARCH_USE_STATUS = (
    "OWNER_APPROVED_FOR_EVIDENCE_ACQUISITION_AND_NONEXECUTING_STATIC_REVIEW_ONLY"
)
OFIQ_V112_ONNX_RUNTIME_VERSION = "1.18.1"
OFIQ_V112_ONNX_RUNTIME_COMMIT = "387127404e6c1d84b3468c387d864877ed1c67fe"
OFIQ_V112_ONNX_RUNTIME_MANIFEST = (
    "7c9039d120bfffd1cb02a92846bab9e5b618da026c12f6d1335259b197a150b4"
)
OFIQ_V112_ONNX_RUNTIME_LIBRARY_SHA256 = (
    "1147caa734d19f7549f90a659aa9ca444366eeb80a596900d19d242145a9f4c9"
)
OFIQ_V112_ONNX_RUNTIME_LICENSE_SHA256 = (
    "2f07c72751aed99790b8a4869cf2311df85a860b22ded05fa22803587a48922c"
)
OFIQ_V112_ONNX_RUNTIME_NOTICES_SHA256 = (
    "10fc98a2f4d478e981813d594e23d7e31b0430e66501a658c1ecab2a226a4b52"
)
OFIQ_V112_ONNX_RUNTIME_CMAKE_SHA256 = (
    "5671027df260e8a43ea043c4cf47a9e245401ac2550e5bbcf07340289ae0f964"
)
OFIQ_V112_ONNX_RUNTIME_ROLES = {
    "GIT_COMMIT_ID": "VERSION_EVIDENCE",
    "LICENSE": "LICENSE",
    "Privacy.md": "DOCUMENTATION",
    "README.md": "DOCUMENTATION",
    "ThirdPartyNotices.txt": "LICENSE",
    "VERSION_NUMBER": "VERSION_EVIDENCE",
    "include/cpu_provider_factory.h": "IMPLEMENTATION",
    "include/onnxruntime_c_api.h": "IMPLEMENTATION",
    "include/onnxruntime_cxx_api.h": "IMPLEMENTATION",
    "include/onnxruntime_cxx_inline.h": "IMPLEMENTATION",
    "include/onnxruntime_float16.h": "IMPLEMENTATION",
    "include/onnxruntime_lite_custom_op.h": "IMPLEMENTATION",
    "include/onnxruntime_run_options_config_keys.h": "IMPLEMENTATION",
    "include/onnxruntime_session_options_config_keys.h": "IMPLEMENTATION",
    "include/onnxruntime_training_c_api.h": "IMPLEMENTATION",
    "include/onnxruntime_training_cxx_api.h": "IMPLEMENTATION",
    "include/onnxruntime_training_cxx_inline.h": "IMPLEMENTATION",
    "include/provider_options.h": "IMPLEMENTATION",
    "lib/libonnxruntime.so.1.18.1": "RUNTIME_LIBRARY",
}
OFIQ_V112_ONNX_RUNTIME_SYMLINKS = {
    "lib/libonnxruntime.so": "libonnxruntime.so.1.18.1"
}
OFIQ_V112_ONNX_RUNTIME_NEEDED = (
    "libdl.so.2",
    "librt.so.1",
    "libpthread.so.0",
    "libstdc++.so.6",
    "libm.so.6",
    "libgcc_s.so.1",
    "libc.so.6",
    "ld-linux-x86-64.so.2",
)
OFIQ_V112_ONNX_RUNTIME_REQUIRED_EXPORTS = {
    "OrtGetApiBase": "VERS_1.18.1",
    "OrtSessionOptionsAppendExecutionProvider_CPU": "VERS_1.18.1",
}
OFIQ_V112_ONNX_RUNTIME_REQUIRED_VERSIONS = {
    "ld-linux-x86-64.so.2": frozenset({"GLIBC_2.3"}),
    "libc.so.6": frozenset(
        {
            "GLIBC_2.2.5",
            "GLIBC_2.3",
            "GLIBC_2.3.3",
            "GLIBC_2.3.4",
            "GLIBC_2.4",
            "GLIBC_2.6",
            "GLIBC_2.14",
        }
    ),
    "libdl.so.2": frozenset({"GLIBC_2.2.5"}),
    "libgcc_s.so.1": frozenset({"GCC_3.0", "GCC_3.3.1", "GCC_4.0.0"}),
    "libm.so.6": frozenset({"GLIBC_2.2.5", "GLIBC_2.27"}),
    "libpthread.so.0": frozenset({"GLIBC_2.2.5", "GLIBC_2.3.4"}),
    "libstdc++.so.6": frozenset(
        {
            "CXXABI_1.3",
            "CXXABI_1.3.2",
            "CXXABI_1.3.5",
            "CXXABI_1.3.7",
            "CXXABI_1.3.8",
            "CXXABI_1.3.9",
            "CXXABI_1.3.11",
            "GLIBCXX_3.4",
            "GLIBCXX_3.4.9",
            "GLIBCXX_3.4.11",
            "GLIBCXX_3.4.14",
            "GLIBCXX_3.4.15",
            "GLIBCXX_3.4.17",
            "GLIBCXX_3.4.18",
            "GLIBCXX_3.4.19",
            "GLIBCXX_3.4.20",
            "GLIBCXX_3.4.21",
            "GLIBCXX_3.4.22",
        }
    ),
}
OFIQ_V112_MODEL_LICENSE_CONTRACT = frozenset(
    {
        (
            "MIT AND OFIQ-MIT",
            "expression_neutrality/grimmer/LICENSE.md",
            ("expression_neutrality/grimmer/hse_1_2_C_adaboost.yml.gz",),
        ),
        (
            "Apache-2.0 AND OFIQ-MIT",
            "expression_neutrality/hsemotion/LICENSE.md",
            (
                "expression_neutrality/hsemotion/enet_b0_8_best_vgaf_embed_zeroed.onnx",
                "expression_neutrality/hsemotion/enet_b2_8_embed_zeroed.onnx",
            ),
        ),
        (
            "MIT AND OFIQ-MIT",
            "face_detection/LICENSE.md",
            (
                "face_detection/ssd_facedetect.caffemodel",
                "face_detection/ssd_facedetect.prototxt.txt",
            ),
        ),
        (
            "MIT AND OFIQ-MIT",
            "face_landmark_estimation/LICENSE.md",
            ("face_landmark_estimation/ADNet.onnx",),
        ),
        (
            "MIT AND OFIQ-MIT",
            "face_occlusion_segmentation/LICENSE.md",
            (
                "face_occlusion_segmentation/face_occlusion_segmentation_ort.onnx",
            ),
        ),
        (
            "MIT AND OFIQ-MIT",
            "face_parsing/LICENSE.md",
            ("face_parsing/bisenet_400.onnx",),
        ),
        (
            "MIT AND OFIQ-MIT",
            "head_pose_estimation/LICENSE.md",
            ("head_pose_estimation/mb1_120x120.onnx",),
        ),
        (
            "OFIQ-MIT",
            "no_compression_artifacts/LICENSE.md",
            ("no_compression_artifacts/ssim_248_model.onnx",),
        ),
        (
            "OFIQ-MIT",
            "sharpness/LICENSE.md",
            ("sharpness/face_sharpness_rtree.xml.gz",),
        ),
        (
            "Apache-2.0 AND OFIQ-MIT",
            "unified_quality_score/LICENSE.md",
            ("unified_quality_score/magface_iresnet50_norm.onnx",),
        ),
    }
)
MODEL_REFERENCE_RE = re.compile(
    r'"(?:model_path|prototxt_path|cnn1_model_path|cnn2_model_path|adaboost_model_path)"'
    r"\s*:\s*\"([^\"]+)\""
)
FORBIDDEN_MARKER_RE = re.compile(
    r"\b(?:TODO|FIXME|TBD)\b|\[date to be inserted\]|\bN records\b|\bX met\b",
    re.IGNORECASE,
)


class RegistryValidationError(ValueError):
    """Raised when registry structure or recorded evidence is invalid."""


class ArtifactCache:
    """Cache expensive byte identities during one validation run."""

    def __init__(self) -> None:
        self._values: dict[Path, tuple[str, int, int]] = {}

    def identity(self, path: Path) -> tuple[str, int, int]:
        resolved = path.resolve(strict=True)
        cached = self._values.get(resolved)
        if cached is not None:
            return cached
        if not resolved.is_file():
            raise RegistryValidationError(f"artifact is not a regular file: {path}")
        digest = hashlib.sha256()
        with resolved.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        stat = resolved.stat()
        value = (digest.hexdigest(), stat.st_size, stat.st_mtime_ns)
        self._values[resolved] = value
        return value


def _fail(location: str, message: str) -> None:
    raise RegistryValidationError(f"{location}: {message}")


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(location, "must be an object")
    return value


def _sequence(value: Any, location: str, maximum: int) -> list[Any]:
    if not isinstance(value, list):
        _fail(location, "must be an array")
    if len(value) > maximum:
        _fail(location, f"contains {len(value)} items; maximum is {maximum}")
    return value


def _string(value: Any, location: str, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        _fail(location, "must be a string")
    if not value or value != value.strip():
        _fail(location, "must be nonempty and have no surrounding whitespace")
    if len(value) > maximum:
        _fail(location, f"exceeds {maximum} characters")
    if FORBIDDEN_MARKER_RE.search(value):
        _fail(location, "contains an unresolved work marker")
    return value


def _strings(value: Any, location: str, maximum_items: int = 256) -> list[str]:
    items = _sequence(value, location, maximum_items)
    strings = [_string(item, f"{location}[{index}]") for index, item in enumerate(items)]
    if len(strings) != len(set(strings)):
        _fail(location, "contains duplicate values")
    return strings


def _strict_keys(
    value: dict[str, Any],
    location: str,
    allowed: set[str],
    required: set[str],
) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        _fail(location, f"unknown fields: {', '.join(unknown)}")
    if missing:
        _fail(location, f"missing fields: {', '.join(missing)}")


def _absolute_path(value: Any, location: str, expect_directory: bool = False) -> Path:
    text = _string(value, location)
    path = Path(text)
    if not path.is_absolute():
        _fail(location, "must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        _fail(location, f"cannot resolve {path}: {error}")
    if expect_directory and not resolved.is_dir():
        _fail(location, f"is not a directory: {path}")
    return resolved


def _read_utf8(path: Path, location: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        _fail(location, f"cannot read UTF-8 text from {path}: {error}")


def _safe_relative_path(value: Any, location: str) -> PurePosixPath:
    text = _string(value, location)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or path.as_posix() != text
    ):
        _fail(location, "must be a normalized relative path")
    return path


def _no_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RegistryValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise RegistryValidationError(f"registry does not exist: {path}") from None
    if size > MAX_REGISTRY_BYTES:
        raise RegistryValidationError(
            f"registry size {size} exceeds {MAX_REGISTRY_BYTES} bytes"
        )
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(text, object_pairs_hook=_no_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RegistryValidationError(f"invalid UTF-8 JSON: {error}") from None
    return _mapping(value, "registry")


def _validate_artifact(
    value: Any,
    location: str,
    cache: ArtifactCache,
) -> tuple[Path, str]:
    artifact = _mapping(value, location)
    allowed = {"role", "path", "sha256", "size_bytes", "mtime_ns"}
    _strict_keys(artifact, location, allowed, allowed)
    role = _string(artifact["role"], f"{location}.role")
    if role not in ALLOWED_ARTIFACT_ROLES:
        _fail(f"{location}.role", f"unsupported role {role}")
    path = _absolute_path(artifact["path"], f"{location}.path")
    expected_hash = _string(artifact["sha256"], f"{location}.sha256", 64)
    if not SHA256_RE.fullmatch(expected_hash):
        _fail(f"{location}.sha256", "must be a lowercase SHA-256 digest")
    expected_size = artifact["size_bytes"]
    expected_mtime = artifact["mtime_ns"]
    if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 1:
        _fail(f"{location}.size_bytes", "must be a positive integer")
    if not isinstance(expected_mtime, int) or isinstance(expected_mtime, bool) or expected_mtime < 1:
        _fail(f"{location}.mtime_ns", "must be a positive integer")
    actual_hash, actual_size, actual_mtime = cache.identity(path)
    if actual_hash != expected_hash:
        _fail(location, f"SHA-256 drift for {path}: {actual_hash}")
    if actual_size != expected_size:
        _fail(location, f"size drift for {path}: {actual_size}")
    if actual_mtime != expected_mtime:
        _fail(location, f"mtime drift for {path}: {actual_mtime}")
    return path, role


def _validate_pinned_source_receipt(
    receipt_path: Path,
    location: str,
    source_uri: str,
    upstream_commit: str,
    evidence_paths: set[str],
    cache: ArtifactCache,
) -> None:
    try:
        receipt = json.loads(
            receipt_path.read_text(encoding="utf-8"),
            object_pairs_hook=_no_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail(location, f"source receipt is not valid UTF-8 JSON: {error}")
    receipt = _mapping(receipt, location)
    fields = {
        "schema_version",
        "receipt_type",
        "recorded_at",
        "retrieval_method",
        "artifacts",
    }
    _strict_keys(receipt, location, fields, fields)
    if _string(receipt["schema_version"], f"{location}.schema_version", 32) != "1.1.0":
        _fail(f"{location}.schema_version", "unsupported version")
    if _string(receipt["receipt_type"], f"{location}.receipt_type", 64) != "PINNED_GIT_RAW_FILES":
        _fail(f"{location}.receipt_type", "unsupported receipt type")
    recorded_at = _string(receipt["recorded_at"], f"{location}.recorded_at", 64)
    try:
        timestamp = datetime.fromisoformat(recorded_at)
    except ValueError:
        _fail(f"{location}.recorded_at", "must be an ISO-8601 timestamp")
    if timestamp.tzinfo is None:
        _fail(f"{location}.recorded_at", "must include a UTC offset")
    _string(receipt["retrieval_method"], f"{location}.retrieval_method")

    entries = _sequence(receipt["artifacts"], f"{location}.artifacts", 256)
    if not entries:
        _fail(f"{location}.artifacts", "must not be empty")
    seen_paths: set[str] = set()
    matched_paths: set[str] = set()
    for index, raw_entry in enumerate(entries):
        entry_location = f"{location}.artifacts[{index}]"
        entry = _mapping(raw_entry, entry_location)
        entry_fields = {
            "project",
            "repository",
            "commit",
            "source_path",
            "retrieved_path",
            "sha256",
            "size_bytes",
        }
        _strict_keys(entry, entry_location, entry_fields, entry_fields)
        _string(entry["project"], f"{entry_location}.project", 128)
        repository = _string(entry["repository"], f"{entry_location}.repository")
        commit = _string(entry["commit"], f"{entry_location}.commit", 40)
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            _fail(f"{entry_location}.commit", "must be a full lowercase Git commit")
        _safe_relative_path(entry["source_path"], f"{entry_location}.source_path")
        retrieved_path = _absolute_path(
            entry["retrieved_path"], f"{entry_location}.retrieved_path"
        )
        retrieved_text = str(retrieved_path)
        if retrieved_text in seen_paths:
            _fail(entry_location, f"duplicate retrieved path {retrieved_text}")
        seen_paths.add(retrieved_text)
        expected_hash = _string(entry["sha256"], f"{entry_location}.sha256", 64)
        if not SHA256_RE.fullmatch(expected_hash):
            _fail(f"{entry_location}.sha256", "must be a lowercase SHA-256 digest")
        expected_size = entry["size_bytes"]
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 1
        ):
            _fail(f"{entry_location}.size_bytes", "must be a positive integer")
        actual_hash, actual_size, _mtime = cache.identity(retrieved_path)
        if actual_hash != expected_hash:
            _fail(entry_location, f"retrieved-file SHA-256 drift: {actual_hash}")
        if actual_size != expected_size:
            _fail(entry_location, f"retrieved-file size drift: {actual_size}")
        if repository == source_uri and commit == upstream_commit:
            if retrieved_text not in evidence_paths:
                _fail(
                    entry_location,
                    "matching receipt artifact is absent from source evidence",
                )
            matched_paths.add(retrieved_text)
    if not matched_paths:
        _fail(location, "receipt has no artifact for the declared source and commit")


def _sha256_stream(handle: Any) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _validate_quarantine_matches_archive(
    archive_path: Path,
    archive_format: str,
    quarantine_path: Path,
    location: str,
    cache: ArtifactCache,
) -> None:
    expected_paths: set[str] = set()

    def register_path(relative: str, member_location: str) -> Path:
        pure = PurePosixPath(relative)
        parents = [parent for parent in pure.parents if parent != PurePosixPath(".")]
        expected_paths.add(relative)
        expected_paths.update(parent.as_posix() for parent in parents)
        for parent in parents:
            extracted_parent = quarantine_path / parent.as_posix()
            if not extracted_parent.is_dir() or extracted_parent.is_symlink():
                _fail(
                    member_location,
                    f"quarantined parent directory drift: {parent.as_posix()}",
                )
        return quarantine_path / relative

    if archive_format == "ZIP":
        with zipfile.ZipFile(archive_path) as handle:
            for index, info in enumerate(handle.infolist()):
                member_location = f"{location}.members[{index}]"
                relative = _archive_member_path(info.filename, member_location)
                extracted = register_path(relative, member_location)
                if info.is_dir():
                    if not extracted.is_dir() or extracted.is_symlink():
                        _fail(member_location, f"quarantined directory drift: {relative}")
                    continue
                if not extracted.is_file() or extracted.is_symlink():
                    _fail(member_location, f"quarantined file drift: {relative}")
                with handle.open(info) as archive_member:
                    archive_hash = _sha256_stream(archive_member)
                extracted_hash, extracted_size, _mtime = cache.identity(extracted)
                if extracted_hash != archive_hash or extracted_size != info.file_size:
                    _fail(member_location, f"quarantined file bytes differ: {relative}")
    else:
        with tarfile.open(archive_path, mode="r:gz") as handle:
            for index, info in enumerate(handle.getmembers()):
                member_location = f"{location}.members[{index}]"
                relative = _archive_member_path(info.name, member_location)
                extracted = register_path(relative, member_location)
                if info.isdir():
                    if not extracted.is_dir() or extracted.is_symlink():
                        _fail(member_location, f"quarantined directory drift: {relative}")
                elif info.isfile():
                    if not extracted.is_file() or extracted.is_symlink():
                        _fail(member_location, f"quarantined file drift: {relative}")
                    archive_member = handle.extractfile(info)
                    if archive_member is None:
                        _fail(member_location, f"cannot read archive member: {relative}")
                    with archive_member:
                        archive_hash = _sha256_stream(archive_member)
                    extracted_hash, extracted_size, _mtime = cache.identity(extracted)
                    if extracted_hash != archive_hash or extracted_size != info.size:
                        _fail(member_location, f"quarantined file bytes differ: {relative}")
                elif info.issym():
                    if not extracted.is_symlink():
                        _fail(member_location, f"quarantined symlink drift: {relative}")
                    if os.readlink(extracted) != info.linkname:
                        _fail(member_location, f"quarantined symlink target drift: {relative}")
                else:
                    _fail(member_location, f"unsupported archive member: {relative}")
    observed_paths = {
        path.relative_to(quarantine_path).as_posix()
        for path in quarantine_path.rglob("*")
    }
    if observed_paths != expected_paths:
        _fail(location, "quarantine membership differs from the archive")


def _validate_ofiq_acquisition_receipt(
    receipt_path: Path,
    location: str,
    resource_id: str,
    resource_version: str,
    source: dict[str, Any],
    owner_authorization: dict[str, Any],
    artifact_paths: set[str],
    artifact_roles: dict[str, str],
    checks: list[Any],
    cache: ArtifactCache,
) -> None:
    try:
        receipt = json.loads(
            receipt_path.read_text(encoding="utf-8"),
            object_pairs_hook=_no_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail(location, f"acquisition receipt is not valid UTF-8 JSON: {error}")
    receipt = _mapping(receipt, location)
    fields = {
        "schema_version",
        "receipt_type",
        "receipt_id",
        "owner_authorization_path",
        "retrieval_evidence_status",
        "source_clone",
        "retrieval_tool",
        "retrievals",
        "archive_validation",
        "observations",
        "build_status",
    }
    _strict_keys(receipt, location, fields, fields)
    if _string(receipt["schema_version"], f"{location}.schema_version", 32) != "1.0.0":
        _fail(f"{location}.schema_version", "unsupported version")
    if _string(receipt["receipt_type"], f"{location}.receipt_type", 64) != "OFIQ_CMAKE_RESOURCE_ACQUISITION":
        _fail(f"{location}.receipt_type", "unsupported receipt type")
    _string(receipt["receipt_id"], f"{location}.receipt_id", 128)
    if (
        _string(
            receipt["retrieval_evidence_status"],
            f"{location}.retrieval_evidence_status",
            128,
        )
        != "PROJECT_RECORDED_WITHOUT_RAW_TRANSFER_LOG"
    ):
        _fail(
            f"{location}.retrieval_evidence_status",
            "unsupported evidence status",
        )

    authorization_path = _absolute_path(
        receipt["owner_authorization_path"],
        f"{location}.owner_authorization_path",
    )
    authorization_text = str(authorization_path)
    if authorization_text not in artifact_paths:
        _fail(location, "owner authorization is not a registered artifact")
    if artifact_roles[authorization_text] != "AUTHORIZATION":
        _fail(location, "owner authorization artifact has the wrong role")
    if owner_authorization["evidence_artifact_path"] != authorization_text:
        _fail(location, "receipt authorization differs from the resource authorization")
    if owner_authorization["decision"] != "APPROVED_EVIDENCE_ACQUISITION":
        _fail(location, "resource is not approved for evidence acquisition")
    if owner_authorization["scopes"] != [OFIQ_V112_ACQUISITION_SCOPE]:
        _fail(location, "resource does not have the exact OFIQ acquisition scope")
    if owner_authorization["redistribution_allowed"] is not False:
        _fail(location, "evidence acquisition cannot authorize redistribution")
    try:
        authorization = json.loads(
            authorization_path.read_text(encoding="utf-8"),
            object_pairs_hook=_no_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail(location, f"authorization is not valid UTF-8 JSON: {error}")
    authorization = _mapping(authorization, f"{location}.authorization")
    authorization_recorded_at = _string(
        authorization.get("recorded_at"),
        f"{location}.authorization.recorded_at",
        64,
    )
    if authorization_recorded_at != owner_authorization["recorded_at"]:
        _fail(location, "authorization time differs from the resource authorization")
    try:
        authorized_at = datetime.fromisoformat(authorization_recorded_at)
    except ValueError:
        _fail(f"{location}.authorization.recorded_at", "must be an ISO-8601 timestamp")
    if authorized_at.tzinfo is None:
        _fail(f"{location}.authorization.recorded_at", "must include a UTC offset")
    grants = _sequence(
        authorization.get("grants"),
        f"{location}.authorization.grants",
        256,
    )
    matching_grants = [
        grant
        for grant in grants
        if isinstance(grant, dict) and grant.get("resource_id") == resource_id
    ]
    if (
        len(matching_grants) != 1
        or matching_grants[0].get("decision") != "APPROVED_EVIDENCE_ACQUISITION"
        or matching_grants[0].get("scopes") != [OFIQ_V112_ACQUISITION_SCOPE]
    ):
        _fail(location, "receipt is not backed by one evidence-acquisition grant")

    clone = _mapping(receipt["source_clone"], f"{location}.source_clone")
    clone_fields = {
        "path",
        "remote",
        "tag",
        "commit",
        "clean_at_verification",
        "cmake_sha256",
        "version_file_sha256",
    }
    _strict_keys(clone, f"{location}.source_clone", clone_fields, clone_fields)
    clone_path = _absolute_path(
        clone["path"], f"{location}.source_clone.path", expect_directory=True
    )
    expected_clone_values = {
        "path": str(Path(source["local_root"]).resolve(strict=True)),
        "remote": source["uri"],
        "tag": source["tag"],
        "commit": source["commit"],
    }
    observed_clone_values = {
        "path": str(clone_path),
        "remote": _string(clone["remote"], f"{location}.source_clone.remote"),
        "tag": _string(clone["tag"], f"{location}.source_clone.tag", 128),
        "commit": _string(clone["commit"], f"{location}.source_clone.commit", 40),
    }
    if observed_clone_values != expected_clone_values:
        _fail(f"{location}.source_clone", "clone identity differs from resource source")
    if clone["clean_at_verification"] is not True:
        _fail(f"{location}.source_clone.clean_at_verification", "must be true")
    status = subprocess.run(
        [
            "git",
            "-C",
            str(clone_path),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if status.returncode != 0 or status.stdout:
        _fail(f"{location}.source_clone", "source clone is not currently clean")
    cmake_path = clone_path / "CMakeLists.txt"
    version_path = clone_path / "Version.txt"
    for field, path in {
        "cmake_sha256": cmake_path,
        "version_file_sha256": version_path,
    }.items():
        if str(path) not in artifact_paths:
            _fail(f"{location}.source_clone.{field}", f"unregistered source file {path}")
        expected_hash = _string(clone[field], f"{location}.source_clone.{field}", 64)
        actual_hash, _size, _mtime = cache.identity(path)
        if actual_hash != expected_hash:
            _fail(f"{location}.source_clone.{field}", f"source-file drift: {actual_hash}")
    version_text = version_path.read_text(encoding="utf-8")
    version_parts: list[str] = []
    for field in ("MAJOR", "MINOR", "PATCH"):
        matches = re.findall(rf"^VERSION_{field}\s+([0-9]+)$", version_text, re.MULTILINE)
        if len(matches) != 1:
            _fail(f"{location}.source_clone", f"Version.txt has no unique VERSION_{field}")
        version_parts.append(str(int(matches[0])))
    suffixes = re.findall(r"^VERSION_SUFFIX(?:\s+(\S+))?$", version_text, re.MULTILINE)
    if len(suffixes) != 1 or suffixes[0]:
        _fail(f"{location}.source_clone", "Version.txt is not an unsuffixed release")
    tagged_version = ".".join(version_parts)
    if tagged_version != resource_version or clone["tag"] != f"v{resource_version}":
        _fail(location, "resource version differs from tagged Version.txt")

    tool = _mapping(receipt["retrieval_tool"], f"{location}.retrieval_tool")
    tool_fields = {
        "name",
        "version",
        "minimum_tls",
        "redirects_followed",
        "http_failure_is_fatal",
    }
    _strict_keys(tool, f"{location}.retrieval_tool", tool_fields, tool_fields)
    for field in ("name", "version", "minimum_tls"):
        _string(tool[field], f"{location}.retrieval_tool.{field}", 128)
    for field in ("redirects_followed", "http_failure_is_fatal"):
        if tool[field] is not True:
            _fail(f"{location}.retrieval_tool.{field}", "must be true")

    cmake_text = cmake_path.read_text(encoding="utf-8")
    model_urls = re.findall(r"set\(MODEL_URL\s+(https://[^)\s]+)\)", cmake_text)
    onnx_urls = re.findall(
        r"set\(ONNX_URL\s+(https://[^)\s]*onnxruntime-linux-x64-1\.18\.1\.tgz)\)",
        cmake_text,
    )
    if len(model_urls) != 1 or len(onnx_urls) != 1:
        _fail(location, "tagged CMake does not declare one expected model and x64 ONNX URL")
    expected_urls = {
        "OFIQ_MODEL_BUNDLE": model_urls[0],
        "ONNX_RUNTIME_LINUX_X64_1_18_1": onnx_urls[0],
    }
    retrievals = _sequence(receipt["retrievals"], f"{location}.retrievals", 8)
    if len(retrievals) != 2:
        _fail(f"{location}.retrievals", "must contain exactly two acquisitions")
    retrieval_by_path: dict[str, dict[str, Any]] = {}
    roles_seen: set[str] = set()
    for index, raw_retrieval in enumerate(retrievals):
        item_location = f"{location}.retrievals[{index}]"
        retrieval = _mapping(raw_retrieval, item_location)
        item_fields = {
            "role",
            "requested_uri",
            "retrieved_at_start",
            "retrieved_at_end",
            "http_status",
            "content_type",
            "path",
            "sha256",
            "size_bytes",
            "mtime_ns",
        }
        _strict_keys(retrieval, item_location, item_fields, item_fields)
        role = _string(retrieval["role"], f"{item_location}.role", 128)
        if role in roles_seen or role not in expected_urls:
            _fail(f"{item_location}.role", f"unexpected or duplicate role {role}")
        roles_seen.add(role)
        if _string(retrieval["requested_uri"], f"{item_location}.requested_uri") != expected_urls[role]:
            _fail(f"{item_location}.requested_uri", "does not match tagged CMake")
        timestamps = []
        for field in ("retrieved_at_start", "retrieved_at_end"):
            value = _string(retrieval[field], f"{item_location}.{field}", 64)
            try:
                timestamp = datetime.fromisoformat(value)
            except ValueError:
                _fail(f"{item_location}.{field}", "must be an ISO-8601 timestamp")
            if timestamp.tzinfo is None:
                _fail(f"{item_location}.{field}", "must include a UTC offset")
            timestamps.append(timestamp)
        if timestamps[1] < timestamps[0]:
            _fail(item_location, "retrieval ends before it starts")
        if timestamps[0] < authorized_at:
            _fail(item_location, "retrieval began before owner authorization")
        if retrieval["http_status"] != 200:
            _fail(f"{item_location}.http_status", "must record HTTP 200")
        _string(retrieval["content_type"], f"{item_location}.content_type", 256)
        archive_path = _absolute_path(retrieval["path"], f"{item_location}.path")
        archive_text = str(archive_path)
        if archive_text in retrieval_by_path:
            _fail(item_location, f"duplicate archive path {archive_text}")
        if archive_text not in artifact_paths or artifact_roles[archive_text] != "ARCHIVE":
            _fail(item_location, "retrieved archive is not a registered ARCHIVE")
        expected_hash = _string(retrieval["sha256"], f"{item_location}.sha256", 64)
        if not SHA256_RE.fullmatch(expected_hash):
            _fail(f"{item_location}.sha256", "must be a lowercase SHA-256 digest")
        expected_size = retrieval["size_bytes"]
        expected_mtime = retrieval["mtime_ns"]
        for field, value in {
            "size_bytes": expected_size,
            "mtime_ns": expected_mtime,
        }.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                _fail(f"{item_location}.{field}", "must be a positive integer")
        actual_hash, actual_size, actual_mtime = cache.identity(archive_path)
        if (expected_hash, expected_size, expected_mtime) != (
            actual_hash,
            actual_size,
            actual_mtime,
        ):
            _fail(item_location, "retrieval identity differs from current archive bytes")
        start_ns = int(timestamps[0].timestamp()) * 1_000_000_000
        end_ns_exclusive = (int(timestamps[1].timestamp()) + 1) * 1_000_000_000
        if not start_ns <= expected_mtime < end_ns_exclusive:
            _fail(item_location, "archive mtime falls outside the recorded retrieval interval")
        retrieval_by_path[archive_text] = retrieval
    if roles_seen != set(expected_urls):
        _fail(f"{location}.retrievals", "missing a CMake-selected acquisition")

    validations = _sequence(
        receipt["archive_validation"], f"{location}.archive_validation", 8
    )
    if len(validations) != 2:
        _fail(f"{location}.archive_validation", "must contain two archive validations")
    archive_checks = {
        check.get("archive_path"): check
        for check in checks
        if isinstance(check, dict) and check.get("type") == "archive_inventory"
    }
    if set(archive_checks) != set(retrieval_by_path):
        _fail(location, "archive checks do not cover exactly the acquired archives")
    quarantine_by_format: dict[str, Path] = {}
    for index, raw_validation in enumerate(validations):
        item_location = f"{location}.archive_validation[{index}]"
        validation = _mapping(raw_validation, item_location)
        item_fields = {
            "path",
            "format",
            "integrity_check",
            "members",
            "files",
            "directories",
            "symlinks",
            "uncompressed_file_bytes",
            "listing_sha256",
            "unsafe_members",
            "quarantine_path",
        }
        _strict_keys(validation, item_location, item_fields, item_fields)
        archive_path = _absolute_path(validation["path"], f"{item_location}.path")
        archive_text = str(archive_path)
        if archive_text not in retrieval_by_path:
            _fail(item_location, "archive validation has no retrieval")
        archive_format = _string(validation["format"], f"{item_location}.format", 32)
        if archive_format not in {"ZIP", "TAR_GZIP"}:
            _fail(f"{item_location}.format", "must be ZIP or TAR_GZIP")
        if validation["integrity_check"] != "PASS" or validation["unsafe_members"] != 0:
            _fail(item_location, "archive integrity and safety must pass")
        check = archive_checks[archive_text]
        receipt_to_check = {
            "format": "format",
            "members": "expected_members",
            "files": "expected_files",
            "directories": "expected_directories",
            "symlinks": "expected_symlinks",
            "uncompressed_file_bytes": "expected_uncompressed_file_bytes",
            "listing_sha256": "listing_sha256",
        }
        for receipt_field, check_field in receipt_to_check.items():
            if validation[receipt_field] != check.get(check_field):
                _fail(item_location, f"{receipt_field} differs from archive check")
        quarantine_path = _absolute_path(
            validation["quarantine_path"],
            f"{item_location}.quarantine_path",
            expect_directory=True,
        )
        if archive_format in quarantine_by_format:
            _fail(item_location, f"duplicate quarantine format {archive_format}")
        quarantine_by_format[archive_format] = quarantine_path
        _validate_quarantine_matches_archive(
            archive_path,
            archive_format,
            quarantine_path,
            item_location,
            cache,
        )
    if set(quarantine_by_format) != {"ZIP", "TAR_GZIP"}:
        _fail(f"{location}.archive_validation", "must contain ZIP and TAR_GZIP")

    observations = _mapping(receipt["observations"], f"{location}.observations")
    observation_fields = {
        "model_tree_manifest_sha256",
        "onnxruntime_library_sha256",
        "bundle_edition_status",
    }
    _strict_keys(observations, f"{location}.observations", observation_fields, observation_fields)
    model_root = quarantine_by_format["ZIP"]
    model_paths = sorted(
        (path for path in model_root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(model_root).as_posix().encode("utf-8"),
    )
    model_digest = hashlib.sha256()
    for path in model_paths:
        file_hash, _size, _mtime = cache.identity(path)
        relative = path.relative_to(model_root).as_posix()
        model_digest.update(f"{file_hash}  ./{relative}\n".encode("utf-8"))
    expected_model_digest = _string(
        observations["model_tree_manifest_sha256"],
        f"{location}.observations.model_tree_manifest_sha256",
        64,
    )
    if model_digest.hexdigest() != expected_model_digest:
        _fail(f"{location}.observations", "quarantined model-tree digest drift")
    onnx_root = quarantine_by_format["TAR_GZIP"]
    onnx_library = (
        onnx_root
        / "onnxruntime-linux-x64-1.18.1/lib/libonnxruntime.so.1.18.1"
    )
    onnx_hash, _size, _mtime = cache.identity(onnx_library)
    if onnx_hash != observations["onnxruntime_library_sha256"]:
        _fail(f"{location}.observations", "quarantined ONNX library digest drift")
    edition_status = _string(
        observations["bundle_edition_status"],
        f"{location}.observations.bundle_edition_status",
        256,
    )
    if edition_status != "ARCHIVE_README_POINTS_TO_ISO_IEC_29794_5_EDITION_2_NOT_INDEPENDENTLY_CROSS_HASHED":
        _fail(f"{location}.observations.bundle_edition_status", "unsupported status")
    model_readme_path = model_root / "models/README.md"
    model_readme = _read_utf8(
        model_readme_path, f"{location}.observations.model_readme"
    )
    if "iso-iec/29794/-5/ed-2/en/OFIQ-MODELS.zip" not in model_readme:
        _fail(f"{location}.observations", "model README does not support edition status")

    build_status = _mapping(receipt["build_status"], f"{location}.build_status")
    build_fields = {"state", "scope", "verification_basis", "host", "blockers"}
    _strict_keys(build_status, f"{location}.build_status", build_fields, build_fields)
    if build_status["state"] != "NOT_OBSERVED_IN_SOURCE_CLONE":
        _fail(f"{location}.build_status.state", "unsupported build-evidence state")
    if build_status["scope"] != str(clone_path):
        _fail(f"{location}.build_status.scope", "must be the registered source clone")
    if (
        build_status["verification_basis"]
        != "CLEAN_GIT_TREE_AND_NO_LOCAL_CONFIGURATION_OR_BUILD_OUTPUT_MARKERS"
    ):
        _fail(f"{location}.build_status.verification_basis", "unsupported basis")
    _string(build_status["host"], f"{location}.build_status.host", 256)
    if not _strings(build_status["blockers"], f"{location}.build_status.blockers", 32):
        _fail(f"{location}.build_status.blockers", "must not be empty")
    marker_names = {"CMakeCache.txt", "Makefile", "build.ninja", "OFIQSampleApp"}
    marker_directory_names = {
        "build",
        "build_linux",
        "cmake-build-debug",
        "cmake-build-release",
        "install_x86_64_linux",
    }
    observed_markers = sorted(
        str(path)
        for path in clone_path.rglob("*")
        if (path.is_file() and path.name in marker_names)
        or (path.is_dir() and path.name in marker_directory_names)
    )
    if observed_markers:
        _fail(
            f"{location}.build_status",
            "local configuration or build markers exist: " + ", ".join(observed_markers),
        )


def _validate_source(
    value: Any,
    location: str,
    resource_id: str,
    resource_version: str | None,
    owner_authorization: dict[str, Any],
    artifact_paths: set[str],
    artifact_roles: dict[str, str],
    checks: list[Any],
    cache: ArtifactCache,
) -> None:
    source = _mapping(value, location)
    allowed = {
        "uri",
        "local_root",
        "commit",
        "upstream_commit",
        "tag",
        "evidence_artifacts",
        "notes",
    }
    required = {"uri", "local_root", "evidence_artifacts", "notes"}
    _strict_keys(source, location, allowed, required)
    source_uri = _string(source["uri"], f"{location}.uri")
    _absolute_path(source["local_root"], f"{location}.local_root", expect_directory=True)
    if "commit" in source:
        commit = _string(source["commit"], f"{location}.commit", 64)
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            _fail(f"{location}.commit", "must be a full lowercase Git commit")
    if "tag" in source:
        _string(source["tag"], f"{location}.tag", 128)
    if "upstream_commit" in source:
        upstream_commit = _string(
            source["upstream_commit"], f"{location}.upstream_commit", 40
        )
        if not re.fullmatch(r"[0-9a-f]{40}", upstream_commit):
            _fail(f"{location}.upstream_commit", "must be a full lowercase Git commit")
        if "commit" in source:
            _fail(location, "commit and upstream_commit are mutually exclusive")
    evidence = _strings(source["evidence_artifacts"], f"{location}.evidence_artifacts")
    if not evidence:
        _fail(f"{location}.evidence_artifacts", "must contain real source evidence")
    for path in evidence:
        if path not in artifact_paths:
            _fail(f"{location}.evidence_artifacts", f"unregistered artifact {path}")
    if "upstream_commit" in source:
        receipts = [
            Path(path)
            for path in evidence
            if artifact_roles[path] == "SOURCE_RECEIPT"
        ]
        if len(receipts) != 1:
            _fail(
                f"{location}.evidence_artifacts",
                "a pinned upstream source needs exactly one source receipt",
            )
        _validate_pinned_source_receipt(
            receipts[0],
            f"{location}.source_receipt",
            source_uri,
            source["upstream_commit"],
            set(evidence),
            cache,
        )
    elif "commit" in source:
        acquisition_receipts = [
            Path(path)
            for path in evidence
            if artifact_roles[path] == "SOURCE_RECEIPT"
        ]
        if len(acquisition_receipts) > 1:
            _fail(
                f"{location}.evidence_artifacts",
                "a Git source may name at most one acquisition receipt",
            )
        if acquisition_receipts:
            _validate_ofiq_acquisition_receipt(
                acquisition_receipts[0],
                f"{location}.acquisition_receipt",
                resource_id,
                resource_version or "",
                source,
                owner_authorization,
                artifact_paths,
                artifact_roles,
                checks,
                cache,
            )
    _strings(source["notes"], f"{location}.notes", 32)


def _validate_licenses(
    value: Any,
    location: str,
    artifact_paths: set[str],
    artifact_roles: dict[str, str],
) -> set[str]:
    licenses = _sequence(value, location, 64)
    seen: set[tuple[str, str]] = set()
    covered_paths: set[str] = set()
    for index, raw_license in enumerate(licenses):
        item_location = f"{location}[{index}]"
        license_record = _mapping(raw_license, item_location)
        allowed = {"expression", "artifact_path", "applies_to"}
        _strict_keys(license_record, item_location, allowed, allowed)
        expression = _string(license_record["expression"], f"{item_location}.expression", 256)
        artifact_path = _string(
            license_record["artifact_path"], f"{item_location}.artifact_path"
        )
        if artifact_path not in artifact_paths:
            _fail(item_location, f"license artifact is not registered: {artifact_path}")
        if artifact_roles[artifact_path] != "LICENSE":
            _fail(item_location, "license evidence must have role LICENSE")
        applies_to = _strings(license_record["applies_to"], f"{item_location}.applies_to")
        if not applies_to:
            _fail(f"{item_location}.applies_to", "must name at least one artifact")
        for path in applies_to:
            if path not in artifact_paths:
                _fail(item_location, f"license target is not registered: {path}")
            covered_paths.add(path)
        identity = (expression, artifact_path)
        if identity in seen:
            _fail(item_location, "duplicates a license record")
        seen.add(identity)
    return covered_paths


def _validate_source_check_consistency(
    source: dict[str, Any],
    checks: list[Any],
    location: str,
) -> None:
    if "commit" not in source and "tag" not in source:
        return
    identities = [
        check
        for check in checks
        if isinstance(check, dict) and check.get("type") == "git_identity"
    ]
    if len(identities) != 1:
        _fail(location, "a Git-backed source must have exactly one git_identity check")
    identity = identities[0]
    source_root = Path(source["local_root"]).resolve(strict=True)
    check_root = Path(identity["root"]).resolve(strict=True)
    if source_root != check_root:
        _fail(location, "source local_root differs from git_identity root")
    for source_field, check_field in {
        "uri": "remote",
        "commit": "commit",
        "tag": "tag",
    }.items():
        if source_field in source and source[source_field] != identity.get(check_field):
            _fail(location, f"source {source_field} differs from git_identity {check_field}")


def _validate_runtime_requirements(value: Any, location: str) -> None:
    runtime = _mapping(value, location)
    allowed = {"platform", "environment", "notes"}
    required = {"platform", "environment", "notes"}
    _strict_keys(runtime, location, allowed, required)
    _string(runtime["platform"], f"{location}.platform", 128)
    environment = _mapping(runtime["environment"], f"{location}.environment")
    if set(environment) - {"LD_LIBRARY_PATH"}:
        _fail(f"{location}.environment", "only LD_LIBRARY_PATH is supported")
    for key, raw_value in environment.items():
        _string(key, f"{location}.environment key", 64)
        value_text = _string(raw_value, f"{location}.environment.{key}")
        for index, directory in enumerate(value_text.split(os.pathsep)):
            _absolute_path(
                directory,
                f"{location}.environment.{key}[{index}]",
                expect_directory=True,
            )
    _strings(runtime["notes"], f"{location}.notes", 32)


def _validate_owner_authorization(
    value: Any,
    location: str,
    resource_id: str,
    artifact_paths: set[str],
    artifact_roles: dict[str, str],
) -> tuple[str, set[str]]:
    authorization = _mapping(value, location)
    allowed = {
        "decision",
        "authority",
        "recorded_at",
        "scopes",
        "redistribution_allowed",
        "evidence_artifact_path",
    }
    _strict_keys(authorization, location, allowed, allowed)
    decision = _string(authorization["decision"], f"{location}.decision", 64)
    if decision not in ALLOWED_OWNER_DECISIONS:
        _fail(f"{location}.decision", f"unsupported decision {decision}")
    _string(authorization["authority"], f"{location}.authority", 256)
    recorded_at = _string(
        authorization["recorded_at"], f"{location}.recorded_at", 64
    )
    try:
        timestamp = datetime.fromisoformat(recorded_at)
    except ValueError:
        _fail(f"{location}.recorded_at", "must be an ISO-8601 timestamp")
    if timestamp.tzinfo is None:
        _fail(f"{location}.recorded_at", "must include a UTC offset")
    scopes = set(_strings(authorization["scopes"], f"{location}.scopes", 64))
    redistribution_allowed = authorization["redistribution_allowed"]
    if not isinstance(redistribution_allowed, bool):
        _fail(f"{location}.redistribution_allowed", "must be a boolean")
    evidence_path = _string(
        authorization["evidence_artifact_path"],
        f"{location}.evidence_artifact_path",
    )
    if evidence_path not in artifact_paths:
        _fail(location, f"authorization evidence is not registered: {evidence_path}")
    if artifact_roles[evidence_path] != "AUTHORIZATION":
        _fail(location, "authorization evidence must have role AUTHORIZATION")

    try:
        receipt = json.loads(
            Path(evidence_path).read_text(encoding="utf-8"),
            object_pairs_hook=_no_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail(location, f"authorization evidence is not valid UTF-8 JSON: {error}")
    receipt = _mapping(receipt, f"{location}.evidence")
    receipt_fields = {
        "schema_version",
        "authorization_id",
        "authority",
        "recorded_at",
        "decision_evidence",
        "grants",
        "permitted_actions",
        "prohibited_actions",
        "redistribution_allowed",
    }
    _strict_keys(receipt, f"{location}.evidence", receipt_fields, receipt_fields)
    if _string(
        receipt["schema_version"], f"{location}.evidence.schema_version", 32
    ) != "1.0.0":
        _fail(f"{location}.evidence.schema_version", "unsupported version")
    _string(receipt["authorization_id"], f"{location}.evidence.authorization_id", 128)
    if _string(receipt["authority"], f"{location}.evidence.authority", 256) != authorization["authority"]:
        _fail(location, "authority differs from the authorization receipt")
    if _string(receipt["recorded_at"], f"{location}.evidence.recorded_at", 64) != recorded_at:
        _fail(location, "recorded_at differs from the authorization receipt")
    grants = _sequence(receipt["grants"], f"{location}.evidence.grants", 256)
    parsed_grants: dict[str, tuple[str, set[str]]] = {}
    for index, raw_grant in enumerate(grants):
        grant_location = f"{location}.evidence.grants[{index}]"
        grant = _mapping(raw_grant, grant_location)
        grant_fields = {"resource_id", "decision", "scopes"}
        _strict_keys(grant, grant_location, grant_fields, grant_fields)
        grant_resource_id = _string(
            grant["resource_id"], f"{grant_location}.resource_id", 128
        )
        if not RESOURCE_ID_RE.fullmatch(grant_resource_id):
            _fail(f"{grant_location}.resource_id", "must be lowercase kebab-case")
        if grant_resource_id in parsed_grants:
            _fail(grant_location, f"duplicate grant for {grant_resource_id}")
        grant_decision = _string(
            grant["decision"], f"{grant_location}.decision", 64
        )
        if grant_decision not in ALLOWED_OWNER_DECISIONS:
            _fail(f"{grant_location}.decision", f"unsupported decision {grant_decision}")
        grant_scopes = set(
            _strings(grant["scopes"], f"{grant_location}.scopes", 64)
        )
        if grant_decision == "NOT_APPROVED" and grant_scopes:
            _fail(f"{grant_location}.scopes", "a non-approved grant cannot name scopes")
        if grant_decision != "NOT_APPROVED" and not grant_scopes:
            _fail(f"{grant_location}.scopes", "an approved grant must name a scope")
        parsed_grants[grant_resource_id] = (grant_decision, grant_scopes)
    receipt_grant = parsed_grants.get(resource_id)
    if receipt_grant is None:
        _fail(location, f"authorization receipt does not name resource {resource_id}")
    receipt_decision, receipt_scopes = receipt_grant
    if decision != receipt_decision:
        _fail(location, "decision differs from the resource-specific receipt grant")
    if scopes != receipt_scopes:
        _fail(location, "scopes differ from the resource-specific receipt grant")
    for field in ("decision_evidence", "permitted_actions", "prohibited_actions"):
        if not _strings(receipt[field], f"{location}.evidence.{field}", 64):
            _fail(f"{location}.evidence.{field}", "must not be empty")
    receipt_redistribution = receipt["redistribution_allowed"]
    if not isinstance(receipt_redistribution, bool):
        _fail(
            f"{location}.evidence.redistribution_allowed",
            "must be a boolean",
        )
    if receipt_redistribution != redistribution_allowed:
        _fail(location, "redistribution decision differs from the receipt")
    if decision == "NOT_APPROVED" and scopes:
        _fail(f"{location}.scopes", "a non-approved decision cannot grant scopes")
    if decision != "NOT_APPROVED" and not scopes:
        _fail(f"{location}.scopes", "an approved decision must grant a scope")
    return decision, scopes


def _manifest_digest(
    paths: list[Path],
    relative_to: Path,
    cache: ArtifactCache,
    location: str,
) -> str:
    base = relative_to.resolve(strict=True)
    entries: list[tuple[bytes, str, Path]] = []
    for path in paths:
        resolved = path.resolve(strict=True)
        try:
            relative = resolved.relative_to(base).as_posix()
        except ValueError:
            _fail(location, f"{resolved} is outside manifest root {base}")
        entries.append((relative.encode("utf-8"), relative, resolved))
    digest = hashlib.sha256()
    for _sort_key, relative, path in sorted(entries):
        file_hash, _size, _mtime = cache.identity(path)
        digest.update(f"{file_hash}  {relative}\n".encode("utf-8"))
    return digest.hexdigest()


def _archive_member_path(value: str, location: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        _fail(location, f"unsafe archive member name {value!r}")
    trimmed = value[:-1] if value.endswith("/") else value
    path = PurePosixPath(trimmed)
    if (
        path.is_absolute()
        or not path.parts
        or "." in path.parts
        or ".." in path.parts
        or path.as_posix() != trimmed
    ):
        _fail(location, f"unsafe archive member path {value!r}")
    return path.as_posix()


def _read_binary(path: Path, location: str, maximum_bytes: int = 64_000_000) -> bytes:
    try:
        size = path.stat().st_size
        if size < 1 or size > maximum_bytes:
            _fail(location, f"binary size {size} is outside 1..{maximum_bytes}")
        return path.read_bytes()
    except OSError as error:
        _fail(location, f"cannot read binary {path}: {error}")


def _elf_unpack(
    format_string: str,
    data: bytes,
    offset: int,
    location: str,
) -> tuple[Any, ...]:
    size = struct.calcsize(format_string)
    if not isinstance(offset, int) or offset < 0 or offset + size > len(data):
        _fail(location, f"ELF structure is outside the file at offset {offset}")
    try:
        return struct.unpack_from(format_string, data, offset)
    except struct.error as error:
        _fail(location, f"cannot decode ELF structure at offset {offset}: {error}")


def _parse_elf_static(path: Path, location: str) -> dict[str, Any]:
    data = _read_binary(path, location)
    if len(data) < 64:
        _fail(location, "ELF header is truncated")
    header = _elf_unpack("<16sHHIQQQIHHHHHH", data, 0, location)
    ident = header[0]
    if ident[:4] != b"\x7fELF":
        _fail(location, "missing ELF magic")
    if ident[4] != 2:
        _fail(location, f"unsupported ELF class {ident[4]}")
    if ident[5] != 1:
        _fail(location, f"unsupported ELF byte order {ident[5]}")
    if ident[6] != 1 or header[3] != 1:
        _fail(location, "unsupported ELF version")
    if header[1] != 3:
        _fail(location, f"ELF type is not ET_DYN: {header[1]}")
    if header[2] != 62:
        _fail(location, f"ELF machine is not EM_X86_64: {header[2]}")
    program_offset = header[5]
    header_size = header[8]
    program_entry_size = header[9]
    program_count = header[10]
    if header_size != 64 or program_entry_size != 56:
        _fail(location, "unexpected ELF64 header or program-header size")
    if not 1 <= program_count <= 1024:
        _fail(location, f"unsafe program-header count {program_count}")
    if program_offset < header_size or program_offset + program_count * 56 > len(data):
        _fail(location, "program-header table is outside the file")

    program_headers: list[tuple[int, int, int, int, int, int, int, int]] = []
    load_headers: list[tuple[int, int, int, int, int, int, int, int]] = []
    dynamic_headers: list[tuple[int, int, int, int, int, int, int, int]] = []
    note_headers: list[tuple[int, int, int, int, int, int, int, int]] = []
    has_interpreter = False
    for index in range(program_count):
        entry_location = f"{location}.program_headers[{index}]"
        values = _elf_unpack(
            "<IIQQQQQQ", data, program_offset + index * 56, entry_location
        )
        program_header = tuple(int(value) for value in values)
        p_type, _flags, p_offset, _vaddr, _paddr, p_filesz, p_memsz, _align = (
            program_header
        )
        if p_offset > len(data) or p_filesz > len(data) - p_offset:
            _fail(entry_location, "file segment is outside the ELF bytes")
        if p_type == 1:
            if p_filesz > p_memsz:
                _fail(entry_location, "PT_LOAD file size exceeds memory size")
            load_headers.append(program_header)
        elif p_type == 2:
            dynamic_headers.append(program_header)
        elif p_type == 3:
            has_interpreter = True
        elif p_type == 4:
            note_headers.append(program_header)
        program_headers.append(program_header)
    if len(load_headers) < 1:
        _fail(location, "ELF has no PT_LOAD segment")
    if len(dynamic_headers) != 1:
        _fail(location, f"ELF must have one PT_DYNAMIC segment; found {len(dynamic_headers)}")

    def virtual_offset(address: int, size: int, item_location: str) -> int:
        if address < 0 or size < 1:
            _fail(item_location, "invalid virtual-address request")
        candidates: list[int] = []
        for header_record in load_headers:
            _type, _flags, file_offset, virtual_address, _physical, file_size, _memory, _align = header_record
            if (
                address >= virtual_address
                and address - virtual_address <= file_size
                and size <= file_size - (address - virtual_address)
            ):
                candidates.append(file_offset + (address - virtual_address))
        if len(candidates) != 1:
            _fail(
                item_location,
                f"virtual address 0x{address:x} has {len(candidates)} file mappings",
            )
        offset = candidates[0]
        if offset > len(data) or size > len(data) - offset:
            _fail(item_location, "virtual-address mapping is outside the file")
        return offset

    dynamic = dynamic_headers[0]
    dynamic_offset = dynamic[2]
    dynamic_size = dynamic[5]
    if dynamic_size < 16 or dynamic_size % 16:
        _fail(location, "PT_DYNAMIC size is not a positive multiple of 16")
    if dynamic_size // 16 > 4096:
        _fail(location, "PT_DYNAMIC contains too many entries")
    dynamic_entries: list[tuple[int, int]] = []
    terminated = False
    for index in range(dynamic_size // 16):
        tag, value = _elf_unpack(
            "<qQ", data, dynamic_offset + index * 16, f"{location}.dynamic[{index}]"
        )
        if tag == 0:
            terminated = True
            break
        dynamic_entries.append((int(tag), int(value)))
    if not terminated:
        _fail(location, "PT_DYNAMIC has no DT_NULL terminator")

    by_tag: dict[int, list[int]] = {}
    for tag, value in dynamic_entries:
        by_tag.setdefault(tag, []).append(value)

    def singleton(tag: int, name: str, required: bool = True) -> int | None:
        values = by_tag.get(tag, [])
        if len(values) > 1:
            _fail(location, f"duplicate dynamic tag {name}")
        if required and len(values) != 1:
            _fail(location, f"missing dynamic tag {name}")
        return values[0] if values else None

    string_address = singleton(5, "DT_STRTAB")
    string_size = singleton(10, "DT_STRSZ")
    if string_address is None or string_size is None or string_size < 1:
        _fail(location, "invalid dynamic string table")
    string_offset = virtual_offset(
        string_address, string_size, f"{location}.dynamic_string_table"
    )
    string_table = data[string_offset : string_offset + string_size]

    def dynamic_string(index: int, item_location: str) -> str:
        if index < 0 or index >= len(string_table):
            _fail(item_location, f"string offset {index} is outside DT_STRTAB")
        end = string_table.find(b"\x00", index)
        if end < 0:
            _fail(item_location, "dynamic string has no terminator")
        try:
            value = string_table[index:end].decode("ascii")
        except UnicodeDecodeError as error:
            _fail(item_location, f"dynamic string is not ASCII: {error}")
        if not value:
            _fail(item_location, "dynamic string is empty")
        return value

    needed = [
        dynamic_string(index, f"{location}.needed[{position}]")
        for position, index in enumerate(by_tag.get(1, []))
    ]
    if not needed or len(needed) != len(set(needed)):
        _fail(location, "DT_NEEDED entries must be nonempty and unique")
    soname_index = singleton(14, "DT_SONAME")
    runpath_index = singleton(29, "DT_RUNPATH")
    rpath_index = singleton(15, "DT_RPATH", required=False)
    soname = dynamic_string(soname_index, f"{location}.soname") if soname_index is not None else None
    runpath = dynamic_string(runpath_index, f"{location}.runpath") if runpath_index is not None else None
    rpath = dynamic_string(rpath_index, f"{location}.rpath") if rpath_index is not None else None

    build_ids: list[str] = []
    for segment_index, note in enumerate(note_headers):
        cursor = note[2]
        end = note[2] + note[5]
        while cursor < end:
            if end - cursor < 12:
                if any(data[cursor:end]):
                    _fail(f"{location}.notes[{segment_index}]", "truncated note header")
                break
            namesz, descsz, note_type = _elf_unpack(
                "<III", data, cursor, f"{location}.notes[{segment_index}]"
            )
            cursor += 12
            name_end = cursor + namesz
            padded_name_end = cursor + ((namesz + 3) & ~3)
            desc_end = padded_name_end + descsz
            padded_desc_end = padded_name_end + ((descsz + 3) & ~3)
            if name_end > end or desc_end > end or padded_desc_end > end:
                _fail(f"{location}.notes[{segment_index}]", "note payload is outside PT_NOTE")
            name = data[cursor:name_end]
            description = data[padded_name_end:desc_end]
            if name == b"GNU\x00" and note_type == 3:
                if not description:
                    _fail(location, "GNU build-id is empty")
                build_ids.append(description.hex())
            cursor = padded_desc_end
    if len(build_ids) != 1:
        _fail(location, f"ELF must contain one GNU build-id; found {len(build_ids)}")

    verneed_address = singleton(0x6FFFFFFE, "DT_VERNEED")
    verneed_count = singleton(0x6FFFFFFF, "DT_VERNEEDNUM")
    if verneed_address is None or verneed_count is None or not 1 <= verneed_count <= 256:
        _fail(location, "invalid version-need table")
    required_versions: dict[str, set[str]] = {}
    cursor = 0
    visited_need: set[int] = set()
    for need_index in range(verneed_count):
        if cursor in visited_need:
            _fail(location, "cyclic version-need chain")
        visited_need.add(cursor)
        offset = virtual_offset(
            verneed_address + cursor,
            16,
            f"{location}.version_needs[{need_index}]",
        )
        version, count, file_index, aux_offset, next_offset = _elf_unpack(
            "<HHIII", data, offset, f"{location}.version_needs[{need_index}]"
        )
        if version != 1 or not 1 <= count <= 256 or aux_offset < 16:
            _fail(f"{location}.version_needs[{need_index}]", "invalid Elf64_Verneed")
        provider = dynamic_string(file_index, f"{location}.version_needs[{need_index}].file")
        if provider not in needed or provider in required_versions:
            _fail(f"{location}.version_needs[{need_index}]", "invalid or duplicate provider")
        versions: set[str] = set()
        aux_cursor = aux_offset
        visited_aux: set[int] = set()
        for aux_index in range(count):
            if aux_cursor in visited_aux:
                _fail(location, "cyclic version-need auxiliary chain")
            visited_aux.add(aux_cursor)
            aux_file_offset = virtual_offset(
                verneed_address + cursor + aux_cursor,
                16,
                f"{location}.version_needs[{need_index}].aux[{aux_index}]",
            )
            _hash, _flags, _other, name_index, aux_next = _elf_unpack(
                "<IHHII",
                data,
                aux_file_offset,
                f"{location}.version_needs[{need_index}].aux[{aux_index}]",
            )
            name = dynamic_string(
                name_index, f"{location}.version_needs[{need_index}].aux[{aux_index}].name"
            )
            if name in versions:
                _fail(location, f"duplicate required version {provider}:{name}")
            versions.add(name)
            if aux_index + 1 < count:
                if aux_next < 16:
                    _fail(location, "version-need auxiliary chain ended early")
                aux_cursor += aux_next
            elif aux_next != 0:
                _fail(location, "version-need auxiliary chain has trailing entries")
        required_versions[provider] = versions
        if need_index + 1 < verneed_count:
            if next_offset < 16:
                _fail(location, "version-need chain ended early")
            cursor += next_offset
        elif next_offset != 0:
            _fail(location, "version-need chain has trailing entries")

    verdef_address = singleton(0x6FFFFFFC, "DT_VERDEF")
    verdef_count = singleton(0x6FFFFFFD, "DT_VERDEFNUM")
    if verdef_address is None or verdef_count is None or not 1 <= verdef_count <= 256:
        _fail(location, "invalid version-definition table")
    version_definitions: list[str] = []
    definition_indices: dict[int, str] = {}
    cursor = 0
    visited_definitions: set[int] = set()
    for definition_index in range(verdef_count):
        if cursor in visited_definitions:
            _fail(location, "cyclic version-definition chain")
        visited_definitions.add(cursor)
        offset = virtual_offset(
            verdef_address + cursor,
            20,
            f"{location}.version_definitions[{definition_index}]",
        )
        version, _flags, version_index, count, _hash, aux_offset, next_offset = _elf_unpack(
            "<HHHHIII",
            data,
            offset,
            f"{location}.version_definitions[{definition_index}]",
        )
        if version != 1 or not 1 <= count <= 256 or aux_offset < 20:
            _fail(
                f"{location}.version_definitions[{definition_index}]",
                "invalid Elf64_Verdef",
            )
        aux_cursor = aux_offset
        for aux_index in range(count):
            aux_file_offset = virtual_offset(
                verdef_address + cursor + aux_cursor,
                8,
                f"{location}.version_definitions[{definition_index}].aux[{aux_index}]",
            )
            name_index, aux_next = _elf_unpack(
                "<II",
                data,
                aux_file_offset,
                f"{location}.version_definitions[{definition_index}].aux[{aux_index}]",
            )
            name = dynamic_string(
                name_index,
                f"{location}.version_definitions[{definition_index}].aux[{aux_index}].name",
            )
            if aux_index == 0:
                if name in version_definitions or version_index in definition_indices:
                    _fail(location, f"duplicate version definition {name}")
                version_definitions.append(name)
                definition_indices[version_index] = name
            if aux_index + 1 < count:
                if aux_next < 8:
                    _fail(location, "version-definition auxiliary chain ended early")
                aux_cursor += aux_next
            elif aux_next != 0:
                _fail(location, "version-definition auxiliary chain has trailing entries")
        if definition_index + 1 < verdef_count:
            if next_offset < 20:
                _fail(location, "version-definition chain ended early")
            cursor += next_offset
        elif next_offset != 0:
            _fail(location, "version-definition chain has trailing entries")

    section_offset = header[6]
    section_entry_size = header[11]
    section_count = header[12]
    section_name_index = header[13]
    if section_entry_size != 64 or not 1 <= section_count <= 8192:
        _fail(location, "unexpected or unsafe section-header table")
    if section_offset < 64 or section_offset + section_count * 64 > len(data):
        _fail(location, "section-header table is outside the file")
    if section_name_index >= section_count:
        _fail(location, "section-name string-table index is outside the table")
    section_headers: list[tuple[int, int, int, int, int, int, int, int, int, int]] = []
    for index in range(section_count):
        section_location = f"{location}.section_headers[{index}]"
        values = tuple(
            int(value)
            for value in _elf_unpack(
                "<IIQQQQIIQQ",
                data,
                section_offset + index * 64,
                section_location,
            )
        )
        section_type, file_offset, file_size = values[1], values[4], values[5]
        if section_type != 8 and (
            file_offset > len(data) or file_size > len(data) - file_offset
        ):
            _fail(section_location, "section bytes are outside the file")
        section_headers.append(values)

    shstr = section_headers[section_name_index]
    if shstr[1] != 3 or shstr[5] < 1:
        _fail(location, "section-name table is not a nonempty string table")
    section_names_data = data[shstr[4] : shstr[4] + shstr[5]]

    def section_name(index: int, item_location: str) -> str:
        if index < 0 or index >= len(section_names_data):
            _fail(item_location, "section-name offset is outside .shstrtab")
        end = section_names_data.find(b"\x00", index)
        if end < 0:
            _fail(item_location, "section name has no terminator")
        try:
            return section_names_data[index:end].decode("ascii")
        except UnicodeDecodeError as error:
            _fail(item_location, f"section name is not ASCII: {error}")

    named_sections = [
        section_name(section[0], f"{location}.section_headers[{index}].name")
        for index, section in enumerate(section_headers)
    ]
    dynsym_indices = [
        index
        for index, (name, section) in enumerate(zip(named_sections, section_headers))
        if name == ".dynsym" and section[1] == 11
    ]
    versym_indices = [
        index
        for index, (name, section) in enumerate(zip(named_sections, section_headers))
        if name == ".gnu.version" and section[1] == 0x6FFFFFFF
    ]
    if len(dynsym_indices) != 1 or len(versym_indices) != 1:
        _fail(location, "ELF must contain one .dynsym and one .gnu.version section")
    dynsym_index = dynsym_indices[0]
    dynsym = section_headers[dynsym_index]
    if dynsym[9] != 24 or dynsym[5] % 24 or not 1 <= dynsym[5] // 24 <= 1_000_000:
        _fail(location, "invalid .dynsym entry size or count")
    if dynsym[6] >= section_count:
        _fail(location, ".dynsym links outside the section table")
    linked_dynstr = section_headers[dynsym[6]]
    if (
        linked_dynstr[1] != 3
        or linked_dynstr[4] != string_offset
        or linked_dynstr[5] != string_size
    ):
        _fail(location, ".dynsym string table differs from DT_STRTAB")
    versym = section_headers[versym_indices[0]]
    symbol_count = dynsym[5] // 24
    if versym[6] != dynsym_index or versym[5] != symbol_count * 2:
        _fail(location, ".gnu.version does not map every dynamic symbol")

    required_exports: dict[str, str] = {}
    for symbol_index in range(symbol_count):
        symbol_location = f"{location}.dynamic_symbols[{symbol_index}]"
        name_index, info, visibility, defined_section, _value, symbol_size = _elf_unpack(
            "<IBBHQQ",
            data,
            dynsym[4] + symbol_index * 24,
            symbol_location,
        )
        if name_index == 0:
            continue
        name = dynamic_string(name_index, f"{symbol_location}.name")
        if name not in OFIQ_V112_ONNX_RUNTIME_REQUIRED_EXPORTS:
            continue
        if name in required_exports:
            _fail(symbol_location, f"duplicate required export {name}")
        if info >> 4 != 1 or info & 0xF != 2 or visibility & 0x3 != 0:
            _fail(symbol_location, f"required export {name} is not GLOBAL DEFAULT FUNC")
        if defined_section == 0 or symbol_size < 1:
            _fail(symbol_location, f"required export {name} is undefined or empty")
        (raw_version_index,) = _elf_unpack(
            "<H",
            data,
            versym[4] + symbol_index * 2,
            f"{symbol_location}.version",
        )
        export_version = definition_indices.get(raw_version_index & 0x7FFF)
        if export_version is None:
            _fail(symbol_location, f"required export {name} has no version definition")
        required_exports[name] = export_version
    if required_exports != OFIQ_V112_ONNX_RUNTIME_REQUIRED_EXPORTS:
        _fail(location, f"required ONNX exports differ: {required_exports}")

    maximums: dict[str, str] = {}
    for names in required_versions.values():
        for name in names:
            match = re.fullmatch(r"(CXXABI|GLIBC|GLIBCXX)_([0-9]+(?:\.[0-9]+)*)", name)
            if match is None:
                continue
            namespace, number = match.groups()
            current = maximums.get(namespace)
            key = tuple(int(part) for part in number.split("."))
            if current is None or key > tuple(int(part) for part in current.split(".")):
                maximums[namespace] = number

    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "elf_class": "ELF64",
        "elf_data": "LITTLE_ENDIAN",
        "elf_type": "ET_DYN",
        "elf_machine": "EM_X86_64",
        "has_interpreter": has_interpreter,
        "soname": soname,
        "runpath": runpath,
        "rpath": rpath,
        "needed": needed,
        "build_id": build_ids[0],
        "version_definitions": version_definitions,
        "required_exports": dict(sorted(required_exports.items())),
        "required_versions": {
            provider: sorted(versions)
            for provider, versions in sorted(required_versions.items())
        },
        "max_required_versions": dict(sorted(maximums.items())),
    }


def _validate_onnx_static_review_authorization(
    path: Path,
    location: str,
    registry_recorded_at: datetime | None = None,
) -> dict[str, str]:
    try:
        value = json.loads(
            _read_utf8(path, location),
            object_pairs_hook=_no_duplicate_json_keys,
        )
    except json.JSONDecodeError as error:
        _fail(location, f"static-review authorization is not valid JSON: {error}")
    authorization = _mapping(value, location)
    fields = {
        "schema_version",
        "authorization_id",
        "authority",
        "recorded_at",
        "resource_id",
        "decision",
        "scope",
        "decision_evidence",
        "permitted_actions",
        "prohibited_actions",
        "redistribution_allowed",
    }
    _strict_keys(authorization, location, fields, fields)
    fixed_scalars = {
        "schema_version": "1.0.0",
        "authorization_id": "qualsight-owner-onnxruntime-static-review-2026-08-14",
        "authority": "QualSight project owner (interactive user)",
        "resource_id": "ofiq-v1-1-2-cmake-runtime-candidate",
        "decision": "APPROVED_STATIC_EVIDENCE_REVIEW",
        "scope": OFIQ_V112_ONNX_STATIC_REVIEW_SCOPE,
    }
    for field, expected in fixed_scalars.items():
        actual = _string(authorization[field], f"{location}.{field}", 256)
        if actual != expected:
            _fail(f"{location}.{field}", f"must equal {expected}")
    recorded_at = _string(authorization["recorded_at"], f"{location}.recorded_at", 64)
    try:
        timestamp = datetime.fromisoformat(recorded_at)
    except ValueError:
        _fail(f"{location}.recorded_at", "must be an ISO-8601 timestamp")
    if timestamp.tzinfo is None:
        _fail(f"{location}.recorded_at", "must include a UTC offset")
    if registry_recorded_at is not None and timestamp > registry_recorded_at:
        _fail(location, "static-review authorization postdates the registry record")
    expected_decision_evidence = [
        "The project owner answered yes when asked whether QualSight may perform the narrowly defined static ONNX Runtime review.",
        "The question limited review to the preserved ONNX archive, headers, licenses, notices, hashes, and ELF metadata and explicitly excluded installation, loading, execution, networking, redistribution, and third-party-rights claims.",
    ]
    expected_permitted = [
        "Read and hash the preserved ONNX Runtime archive and its receipt-bound quarantine bytes.",
        "Parse the registered package text, headers, license and notice bytes, and ELF structures without loading or executing the target library.",
    ]
    expected_prohibited = [
        "Do not install, load, link, execute, or invoke symbols from ONNX Runtime, OFIQ, a model, or an image pipeline.",
        "Do not use ldd, dlopen, ctypes, a runtime loader, network retrieval, automatic downloads, or unregistered resource bytes.",
        "Do not treat this project-owner decision as third-party license sufficiency, redistribution permission, host compatibility, or runtime validation.",
    ]
    observed_lists = {
        "decision_evidence": _strings(
            authorization["decision_evidence"], f"{location}.decision_evidence", 8
        ),
        "permitted_actions": _strings(
            authorization["permitted_actions"], f"{location}.permitted_actions", 8
        ),
        "prohibited_actions": _strings(
            authorization["prohibited_actions"], f"{location}.prohibited_actions", 8
        ),
    }
    expected_lists = {
        "decision_evidence": expected_decision_evidence,
        "permitted_actions": expected_permitted,
        "prohibited_actions": expected_prohibited,
    }
    for field, expected in expected_lists.items():
        if observed_lists[field] != expected:
            _fail(f"{location}.{field}", "differs from the recorded owner decision")
    if authorization["redistribution_allowed"] is not False:
        _fail(f"{location}.redistribution_allowed", "must be false")
    return {
        "path": str(path),
        "recorded_at": recorded_at,
        "decision": fixed_scalars["decision"],
        "scope": fixed_scalars["scope"],
    }


def _prevalidate_onnx_static_review_authorization(
    resources: list[Any],
    registry_recorded_at: datetime,
    cache: ArtifactCache,
) -> dict[str, str]:
    candidates = [
        _mapping(resource, f"registry.resources[{index}]")
        for index, resource in enumerate(resources)
        if isinstance(resource, dict)
        and resource.get("resource_id") == "ofiq-v1-1-2-cmake-runtime-candidate"
    ]
    if len(candidates) != 1:
        _fail(
            "registry.resources",
            f"must contain one OFIQ v1.1.2 candidate; found {len(candidates)}",
        )
    candidate = candidates[0]
    checks = [
        _mapping(check, f"registry.resources[ofiq-v1-1-2-cmake-runtime-candidate].checks[{index}]")
        for index, check in enumerate(
            _sequence(
                candidate.get("checks"),
                "registry.resources[ofiq-v1-1-2-cmake-runtime-candidate].checks",
                MAX_CHECKS_PER_RESOURCE,
            )
        )
        if isinstance(check, dict)
        and check.get("type") == "onnx_runtime_static_preflight"
    ]
    if len(checks) != 1:
        _fail(
            "registry.resources[ofiq-v1-1-2-cmake-runtime-candidate]",
            f"must declare one ONNX static preflight; found {len(checks)}",
        )
    authorization_path = _absolute_path(
        checks[0].get("static_review_authorization_path"),
        "registry.resources[ofiq-v1-1-2-cmake-runtime-candidate].static_review_authorization_path",
    )
    expected_authorization_path = _absolute_path(
        str(
            Path(__file__).resolve().parents[1]
            / "resources"
            / "authorizations"
            / "qualsight-onnxruntime-static-review-2026-08-14.json"
        ),
        "registry.resources[ofiq-v1-1-2-cmake-runtime-candidate].static_review_authorization",
    )
    if authorization_path != expected_authorization_path:
        _fail(
            "registry.resources[ofiq-v1-1-2-cmake-runtime-candidate]",
            "ONNX preflight does not bind the canonical owner review decision",
        )
    declared_authorizations = [
        (index, artifact)
        for index, artifact in enumerate(_sequence(
            candidate.get("artifacts"),
            "registry.resources[ofiq-v1-1-2-cmake-runtime-candidate].artifacts",
            MAX_ARTIFACTS_PER_RESOURCE,
        ))
        if isinstance(artifact, dict)
        and artifact.get("role") == "AUTHORIZATION"
        and artifact.get("path") == str(authorization_path)
    ]
    if len(declared_authorizations) != 1:
        _fail(
            "registry.resources[ofiq-v1-1-2-cmake-runtime-candidate]",
            "static-review decision is not declared as one AUTHORIZATION artifact",
        )
    artifact_index, authorization_artifact = declared_authorizations[0]
    validated_path, validated_role = _validate_artifact(
        authorization_artifact,
        "registry.resources[ofiq-v1-1-2-cmake-runtime-candidate].artifacts"
        f"[{artifact_index}]",
        cache,
    )
    if validated_path != authorization_path or validated_role != "AUTHORIZATION":
        _fail(
            "registry.resources[ofiq-v1-1-2-cmake-runtime-candidate]",
            "static-review decision artifact identity differs",
        )
    return _validate_onnx_static_review_authorization(
        authorization_path,
        "registry.resources[ofiq-v1-1-2-cmake-runtime-candidate].static_review_authorization",
        registry_recorded_at,
    )


def _validate_onnxruntime_distribution_check(
    check: dict[str, Any],
    location: str,
    artifact_paths: set[str],
    artifact_roles: dict[str, str],
    cache: ArtifactCache,
) -> tuple[dict[str, Any], set[str]]:
    fields = {
        "type",
        "root",
        "archive_path",
        "tagged_cmake_path",
        "static_review_authorization_path",
        "expected_regular_files",
        "expected_directories",
        "expected_regular_file_bytes",
        "sha256",
        "symlinks",
        "version_path",
        "version",
        "git_commit_path",
        "git_commit",
        "library_path",
        "elf",
    }
    _strict_keys(check, location, fields, fields)
    declared_root = Path(_string(check["root"], f"{location}.root"))
    if not declared_root.is_absolute():
        _fail(f"{location}.root", "must be an absolute path")
    try:
        declared_root_mode = declared_root.lstat().st_mode
    except OSError as error:
        _fail(f"{location}.root", f"cannot inspect package root: {error}")
    if stat.S_ISLNK(declared_root_mode):
        _fail(f"{location}.root", "must not be a symlink")
    root = _absolute_path(check["root"], f"{location}.root", expect_directory=True)
    archive_path = _absolute_path(check["archive_path"], f"{location}.archive_path")
    cmake_path = _absolute_path(
        check["tagged_cmake_path"], f"{location}.tagged_cmake_path"
    )
    static_authorization_path = _absolute_path(
        check["static_review_authorization_path"],
        f"{location}.static_review_authorization_path",
    )
    for path, role, field in (
        (archive_path, "ARCHIVE", "archive_path"),
        (cmake_path, "CONFIGURATION", "tagged_cmake_path"),
        (
            static_authorization_path,
            "AUTHORIZATION",
            "static_review_authorization_path",
        ),
    ):
        if str(path) not in artifact_paths or artifact_roles.get(str(path)) != role:
            _fail(f"{location}.{field}", f"must be a registered {role} artifact")

    fixed_numbers = {
        "expected_regular_files": 19,
        "expected_directories": 3,
        "expected_regular_file_bytes": 16_203_903,
    }
    for field, expected in fixed_numbers.items():
        value = check[field]
        if not isinstance(value, int) or isinstance(value, bool) or value != expected:
            _fail(f"{location}.{field}", f"must equal {expected}")
    expected_manifest = _string(check["sha256"], f"{location}.sha256", 64)
    if expected_manifest != OFIQ_V112_ONNX_RUNTIME_MANIFEST:
        _fail(f"{location}.sha256", "differs from the governed ONNX manifest")

    declared_symlinks = _mapping(check["symlinks"], f"{location}.symlinks")
    normalized_symlinks = {
        _safe_relative_path(path, f"{location}.symlinks.path").as_posix(): _string(
            target, f"{location}.symlinks[{path}]", 256
        )
        for path, target in declared_symlinks.items()
    }
    if normalized_symlinks != OFIQ_V112_ONNX_RUNTIME_SYMLINKS:
        _fail(f"{location}.symlinks", "differs from the governed ONNX symlink")

    relative_contract = {
        "version_path": "VERSION_NUMBER",
        "git_commit_path": "GIT_COMMIT_ID",
        "library_path": "lib/libonnxruntime.so.1.18.1",
    }
    relative_paths: dict[str, str] = {}
    for field, expected in relative_contract.items():
        relative = _safe_relative_path(check[field], f"{location}.{field}").as_posix()
        if relative != expected:
            _fail(f"{location}.{field}", f"must equal {expected}")
        relative_paths[field] = relative
    if _string(check["version"], f"{location}.version", 64) != OFIQ_V112_ONNX_RUNTIME_VERSION:
        _fail(f"{location}.version", "differs from the governed package version")
    commit = _string(check["git_commit"], f"{location}.git_commit", 40)
    if commit != OFIQ_V112_ONNX_RUNTIME_COMMIT:
        _fail(f"{location}.git_commit", "differs from the package-recorded commit")

    observed_directories: set[str] = set()
    observed_regular: dict[str, Path] = {}
    observed_symlinks: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        try:
            mode = path.lstat().st_mode
        except OSError as error:
            _fail(location, f"cannot inspect {path}: {error}")
        if stat.S_ISDIR(mode):
            observed_directories.add(relative)
        elif stat.S_ISREG(mode):
            observed_regular[relative] = path
        elif stat.S_ISLNK(mode):
            try:
                observed_symlinks[relative] = os.readlink(path)
            except OSError as error:
                _fail(location, f"cannot read symlink {path}: {error}")
        else:
            _fail(location, f"unsupported filesystem entry {relative}")
    if observed_directories != {"include", "lib"}:
        _fail(location, f"ONNX directory closure differs: {sorted(observed_directories)}")
    if set(observed_regular) != set(OFIQ_V112_ONNX_RUNTIME_ROLES):
        _fail(location, "ONNX regular-file closure differs from the governed package")
    if observed_symlinks != OFIQ_V112_ONNX_RUNTIME_SYMLINKS:
        _fail(location, "ONNX symlink closure differs from the governed package")

    registered_tree: dict[str, str] = {}
    for path_text in artifact_paths:
        path = Path(path_text)
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        registered_tree[relative] = artifact_roles[path_text]
    if registered_tree != OFIQ_V112_ONNX_RUNTIME_ROLES:
        _fail(location, "registered ONNX artifact paths or roles are incomplete")

    regular_bytes = 0
    for relative, path in observed_regular.items():
        _digest, size, _mtime = cache.identity(path)
        regular_bytes += size
    if regular_bytes != fixed_numbers["expected_regular_file_bytes"]:
        _fail(location, f"ONNX regular-file byte count drift: {regular_bytes}")
    manifest_sha256 = _manifest_digest(
        list(observed_regular.values()), root, cache, f"{location}.manifest"
    )
    if manifest_sha256 != expected_manifest:
        _fail(location, f"ONNX regular-file manifest drift: {manifest_sha256}")

    for relative, target in observed_symlinks.items():
        if not target or "\\" in target or "\x00" in target or PurePosixPath(target).is_absolute():
            _fail(f"{location}.symlinks[{relative}]", "unsafe symlink target")
        normalized_target = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
        if normalized_target == ".." or normalized_target.startswith("../"):
            _fail(f"{location}.symlinks[{relative}]", "symlink escapes the package root")
        target_path = root / normalized_target
        try:
            target_mode = target_path.lstat().st_mode
        except OSError as error:
            _fail(f"{location}.symlinks[{relative}]", f"cannot inspect target: {error}")
        if not stat.S_ISREG(target_mode) or normalized_target != relative_paths["library_path"]:
            _fail(f"{location}.symlinks[{relative}]", "target is not the registered library")

    version_path = root / relative_paths["version_path"]
    git_commit_path = root / relative_paths["git_commit_path"]
    if _read_utf8(version_path, f"{location}.version_path") != f"{OFIQ_V112_ONNX_RUNTIME_VERSION}\n":
        _fail(f"{location}.version_path", "package version bytes differ")
    if _read_utf8(git_commit_path, f"{location}.git_commit_path") != f"{OFIQ_V112_ONNX_RUNTIME_COMMIT}\n":
        _fail(f"{location}.git_commit_path", "package commit bytes differ")
    api_header_path = root / "include" / "onnxruntime_c_api.h"
    api_versions = re.findall(
        r"^#define ORT_API_VERSION ([0-9]+)$",
        _read_utf8(api_header_path, f"{location}.api_header"),
        re.MULTILINE,
    )
    if api_versions != ["18"]:
        _fail(f"{location}.api_header", f"ORT_API_VERSION differs: {api_versions}")

    cmake_hash, _cmake_size, _cmake_mtime = cache.identity(cmake_path)
    if cmake_hash != OFIQ_V112_ONNX_RUNTIME_CMAKE_SHA256:
        _fail(f"{location}.tagged_cmake_path", "tagged Linux CMake bytes differ")
    cmake_text = _read_utf8(cmake_path, f"{location}.tagged_cmake_path")
    try:
        conan_branch = cmake_text.split("if(USE_CONAN)", 1)[1].split("else(USE_CONAN)", 1)[0]
    except IndexError:
        _fail(f"{location}.tagged_cmake_path", "missing unique USE_CONAN branch")
    cmake_contract = (
        "IMPORTED_IMPLIB ${CMAKE_CURRENT_SOURCE_DIR}/extern/onnxruntime-linux-x64-1.18.1/lib/libonnxruntime.so",
        "IMPORTED_LOCATION ${CMAKE_CURRENT_SOURCE_DIR}/extern/onnxruntime-linux-x64-1.18.1/lib/libonnxruntime.so.1.18.1",
        "INTERFACE_INCLUDE_DIRECTORIES ${CMAKE_CURRENT_SOURCE_DIR}/extern/onnxruntime-linux-x64-1.18.1/include",
    )
    if any(conan_branch.count(line) != 1 for line in cmake_contract):
        _fail(f"{location}.tagged_cmake_path", "USE_CONAN ONNX import layout differs")
    if cmake_text.count(
        "get_property(IMPORTED_LIB_LOCATION TARGET onnxruntime PROPERTY IMPORTED_LOCATION)"
    ) != 1 or cmake_text.count('install(FILES "${IMPORTED_LIB_LOCATION}"') != 4:
        _fail(f"{location}.tagged_cmake_path", "ONNX imported-library install layout differs")

    library_path = root / relative_paths["library_path"]
    elf = _parse_elf_static(library_path, f"{location}.elf_observed")
    if elf["sha256"] != OFIQ_V112_ONNX_RUNTIME_LIBRARY_SHA256:
        _fail(f"{location}.library_path", "runtime-library bytes differ")
    declared_elf = _mapping(check["elf"], f"{location}.elf")
    elf_fields = {
        "elf_class",
        "elf_data",
        "elf_type",
        "elf_machine",
        "has_interpreter",
        "soname",
        "runpath",
        "rpath",
        "needed",
        "build_id",
        "version_definitions",
        "required_exports",
        "required_versions",
        "max_required_versions",
    }
    _strict_keys(declared_elf, f"{location}.elf", elf_fields, elf_fields)
    declared_required_raw = _mapping(
        declared_elf["required_versions"], f"{location}.elf.required_versions"
    )
    declared_required = {
        _string(provider, f"{location}.elf.required_versions.provider", 128): frozenset(
            _strings(versions, f"{location}.elf.required_versions[{provider}]", 128)
        )
        for provider, versions in declared_required_raw.items()
    }
    declared_maximums_raw = _mapping(
        declared_elf["max_required_versions"],
        f"{location}.elf.max_required_versions",
    )
    declared_maximums = {
        _string(namespace, f"{location}.elf.max_required_versions.namespace", 32): _string(
            version, f"{location}.elf.max_required_versions[{namespace}]", 32
        )
        for namespace, version in declared_maximums_raw.items()
    }
    fixed_scalars = {
        "elf_class": "ELF64",
        "elf_data": "LITTLE_ENDIAN",
        "elf_type": "ET_DYN",
        "elf_machine": "EM_X86_64",
        "has_interpreter": False,
        "soname": "libonnxruntime.so.1.18.1",
        "runpath": "$ORIGIN",
        "rpath": None,
        "build_id": "bc5995be4e80f2fcbb80ae9141067094eac52943",
    }
    for field, expected in fixed_scalars.items():
        if declared_elf[field] != expected or elf[field] != expected:
            _fail(f"{location}.elf.{field}", f"must equal {expected!r}")
    declared_needed = _strings(declared_elf["needed"], f"{location}.elf.needed", 64)
    if tuple(declared_needed) != OFIQ_V112_ONNX_RUNTIME_NEEDED or elf["needed"] != declared_needed:
        _fail(f"{location}.elf.needed", "DT_NEEDED order differs")
    declared_definitions = _strings(
        declared_elf["version_definitions"],
        f"{location}.elf.version_definitions",
        32,
    )
    fixed_definitions = ["libonnxruntime.so.1.18.1", "VERS_1.18.1"]
    if declared_definitions != fixed_definitions or elf["version_definitions"] != fixed_definitions:
        _fail(f"{location}.elf.version_definitions", "version definitions differ")
    declared_exports_raw = _mapping(
        declared_elf["required_exports"], f"{location}.elf.required_exports"
    )
    declared_exports = {
        _string(name, f"{location}.elf.required_exports.name", 128): _string(
            version, f"{location}.elf.required_exports[{name}]", 128
        )
        for name, version in declared_exports_raw.items()
    }
    if (
        declared_exports != OFIQ_V112_ONNX_RUNTIME_REQUIRED_EXPORTS
        or elf["required_exports"] != OFIQ_V112_ONNX_RUNTIME_REQUIRED_EXPORTS
    ):
        _fail(f"{location}.elf.required_exports", "required exported ABI differs")
    observed_required = {
        provider: frozenset(versions)
        for provider, versions in elf["required_versions"].items()
    }
    if (
        declared_required != OFIQ_V112_ONNX_RUNTIME_REQUIRED_VERSIONS
        or observed_required != OFIQ_V112_ONNX_RUNTIME_REQUIRED_VERSIONS
    ):
        _fail(f"{location}.elf.required_versions", "version requirements differ")
    fixed_maximums = {"CXXABI": "1.3.11", "GLIBC": "2.27", "GLIBCXX": "3.4.22"}
    if declared_maximums != fixed_maximums or elf["max_required_versions"] != fixed_maximums:
        _fail(f"{location}.elf.max_required_versions", "version maxima differ")

    summary = {
        **elf,
        "root": str(root),
        "archive_path": str(archive_path),
        "tagged_cmake_path": str(cmake_path),
        "static_review_authorization_path": str(static_authorization_path),
        "regular_files": len(observed_regular),
        "regular_file_bytes": regular_bytes,
        "regular_manifest_sha256": manifest_sha256,
        "role_counts": dict(sorted(Counter(registered_tree.values()).items())),
        "symlinks": observed_symlinks,
        "version": OFIQ_V112_ONNX_RUNTIME_VERSION,
        "api_version": 18,
        "package_recorded_git_commit": OFIQ_V112_ONNX_RUNTIME_COMMIT,
        "regular_relative_paths": sorted(observed_regular),
    }
    return summary, {str(path.resolve(strict=True)) for path in observed_regular.values()}


def _validate_check(
    value: Any,
    location: str,
    artifact_paths: set[str],
    artifact_roles: dict[str, str],
    cache: ArtifactCache,
) -> set[str]:
    check = _mapping(value, location)
    check_type = _string(check.get("type"), f"{location}.type", 64)

    if check_type == "git_identity":
        allowed = {"type", "root", "commit", "tag", "remote"}
        _strict_keys(check, location, allowed, allowed)
        root = _absolute_path(check["root"], f"{location}.root", expect_directory=True)
        expected = {
            "commit": _string(check["commit"], f"{location}.commit", 64),
            "tag": _string(check["tag"], f"{location}.tag", 128),
            "remote": _string(check["remote"], f"{location}.remote"),
        }
        commands = {
            "commit": ["git", "-C", str(root), "rev-parse", "HEAD"],
            "tag": ["git", "-C", str(root), "describe", "--tags", "--exact-match", "HEAD"],
            "remote": ["git", "-C", str(root), "remote", "get-url", "origin"],
        }
        for field, command in commands.items():
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                _fail(location, f"{field} command failed with exit {result.returncode}")
            actual = result.stdout.strip()
            if actual != expected[field]:
                _fail(location, f"{field} drift: {actual}")
        return set()

    if check_type in {"manifest", "glob_manifest"}:
        common = {"type", "base_path", "relative_to", "expected_count", "sha256"}
        allowed = common | ({"relative_paths"} if check_type == "manifest" else {"glob"})
        _strict_keys(check, location, allowed, allowed)
        base = _absolute_path(check["base_path"], f"{location}.base_path", expect_directory=True)
        relative_to = _absolute_path(
            check["relative_to"], f"{location}.relative_to", expect_directory=True
        )
        expected_count = check["expected_count"]
        if not isinstance(expected_count, int) or isinstance(expected_count, bool) or expected_count < 1:
            _fail(f"{location}.expected_count", "must be a positive integer")
        expected_hash = _string(check["sha256"], f"{location}.sha256", 64)
        if not SHA256_RE.fullmatch(expected_hash):
            _fail(f"{location}.sha256", "must be a lowercase SHA-256 digest")
        if check_type == "manifest":
            relatives = [
                _safe_relative_path(item, f"{location}.relative_paths[{index}]")
                for index, item in enumerate(
                    _sequence(check["relative_paths"], f"{location}.relative_paths", 1024)
                )
            ]
            paths = [(base / Path(relative.as_posix())).resolve(strict=True) for relative in relatives]
            if len(paths) != len(set(paths)):
                _fail(f"{location}.relative_paths", "contains duplicate paths")
            for path in paths:
                try:
                    path.relative_to(base)
                except ValueError:
                    _fail(location, f"manifest path escapes base: {path}")
                if str(path) not in artifact_paths:
                    _fail(location, f"manifest path is not a registered artifact: {path}")
        else:
            pattern = _string(check["glob"], f"{location}.glob", 256)
            if ".." in PurePosixPath(pattern).parts or Path(pattern).is_absolute():
                _fail(f"{location}.glob", "must be a safe relative glob")
            paths = sorted(path for path in base.glob(pattern) if path.is_file())
            resolved_paths: list[Path] = []
            for path in paths:
                resolved = path.resolve(strict=True)
                try:
                    resolved.relative_to(base)
                except ValueError:
                    _fail(location, f"glob match escapes base: {resolved}")
                if str(resolved) not in artifact_paths:
                    _fail(location, f"glob match is not a registered artifact: {resolved}")
                resolved_paths.append(resolved)
            paths = resolved_paths
        if len(paths) != expected_count:
            _fail(location, f"file count drift: {len(paths)}")
        actual_hash = _manifest_digest(paths, relative_to, cache, location)
        if actual_hash != expected_hash:
            _fail(location, f"manifest SHA-256 drift: {actual_hash}")
        return {str(path) for path in paths}

    if check_type == "jaxn_model_closure":
        allowed = {"type", "config_path", "data_root", "relative_paths", "sha256"}
        _strict_keys(check, location, allowed, allowed)
        config_path = _absolute_path(check["config_path"], f"{location}.config_path")
        if str(config_path) not in artifact_paths:
            _fail(location, f"configuration is not a registered artifact: {config_path}")
        if artifact_roles.get(str(config_path)) != "CONFIGURATION":
            _fail(location, "configuration artifact must have role CONFIGURATION")
        data_root = _absolute_path(check["data_root"], f"{location}.data_root", expect_directory=True)
        declared = [
            _safe_relative_path(item, f"{location}.relative_paths[{index}]").as_posix()
            for index, item in enumerate(
                _sequence(check["relative_paths"], f"{location}.relative_paths", 256)
            )
        ]
        if len(declared) != len(set(declared)):
            _fail(f"{location}.relative_paths", "contains duplicate paths")
        observed = sorted(
            set(
                MODEL_REFERENCE_RE.findall(
                    _read_utf8(config_path, f"{location}.config_path")
                )
            )
        )
        if observed != sorted(declared):
            _fail(location, f"configuration closure drift: observed {observed}")
        paths: list[Path] = []
        for index, declared_path in enumerate(declared):
            path = _absolute_path(
                str(data_root / declared_path),
                f"{location}.resolved_paths[{index}]",
            )
            try:
                path.relative_to(data_root)
            except ValueError:
                _fail(location, f"configuration path escapes data root: {path}")
            if str(path) not in artifact_paths:
                _fail(location, f"configuration path is not registered: {path}")
            paths.append(path)
        expected_hash = _string(check["sha256"], f"{location}.sha256", 64)
        actual_hash = _manifest_digest(paths, data_root.parent, cache, location)
        if actual_hash != expected_hash:
            _fail(location, f"configured payload manifest drift: {actual_hash}")
        return {str(config_path), *(str(path) for path in paths)}

    if check_type == "directory_inventory":
        allowed = {"type", "root", "file_glob", "file_count", "identity_directory_count"}
        _strict_keys(check, location, allowed, allowed)
        root = _absolute_path(check["root"], f"{location}.root", expect_directory=True)
        pattern = _string(check["file_glob"], f"{location}.file_glob", 128)
        if ".." in PurePosixPath(pattern).parts or Path(pattern).is_absolute():
            _fail(f"{location}.file_glob", "must be a safe relative glob")
        expected_files = check["file_count"]
        expected_directories = check["identity_directory_count"]
        for field, number in {
            "file_count": expected_files,
            "identity_directory_count": expected_directories,
        }.items():
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                _fail(f"{location}.{field}", "must be a positive integer")
        actual_files = sum(1 for path in root.rglob(pattern) if path.is_file())
        actual_directories = sum(1 for path in root.iterdir() if path.is_dir())
        if actual_files != expected_files:
            _fail(location, f"file inventory drift: {actual_files}")
        if actual_directories != expected_directories:
            _fail(location, f"identity-directory inventory drift: {actual_directories}")
        return set()

    if check_type == "archive_inventory":
        fields = {
            "type",
            "archive_path",
            "format",
            "expected_members",
            "expected_files",
            "expected_directories",
            "expected_symlinks",
            "expected_uncompressed_file_bytes",
            "listing_sha256",
            "required_relative_paths",
        }
        _strict_keys(check, location, fields, fields)
        archive_path = _absolute_path(
            check["archive_path"], f"{location}.archive_path"
        )
        archive_text = str(archive_path)
        if archive_text not in artifact_paths:
            _fail(location, f"archive is not a registered artifact: {archive_path}")
        if artifact_roles[archive_text] != "ARCHIVE":
            _fail(location, "archive inventory evidence must have role ARCHIVE")
        archive_format = _string(check["format"], f"{location}.format", 32)
        if archive_format not in {"TAR_GZIP", "ZIP"}:
            _fail(f"{location}.format", f"unsupported format {archive_format}")

        expected_values: dict[str, int] = {}
        for field in (
            "expected_members",
            "expected_files",
            "expected_directories",
            "expected_symlinks",
            "expected_uncompressed_file_bytes",
        ):
            number = check[field]
            if (
                not isinstance(number, int)
                or isinstance(number, bool)
                or number < 0
            ):
                _fail(f"{location}.{field}", "must be a nonnegative integer")
            expected_values[field] = number
        if expected_values["expected_members"] < 1:
            _fail(f"{location}.expected_members", "must be positive")
        if expected_values["expected_files"] < 1:
            _fail(f"{location}.expected_files", "must be positive")
        expected_listing_hash = _string(
            check["listing_sha256"], f"{location}.listing_sha256", 64
        )
        if not SHA256_RE.fullmatch(expected_listing_hash):
            _fail(
                f"{location}.listing_sha256",
                "must be a lowercase SHA-256 digest",
            )
        required_paths = {
            _safe_relative_path(path, f"{location}.required_relative_paths[{index}]").as_posix()
            for index, path in enumerate(
                _sequence(
                    check["required_relative_paths"],
                    f"{location}.required_relative_paths",
                    256,
                )
            )
        }
        if not required_paths:
            _fail(f"{location}.required_relative_paths", "must not be empty")

        records: list[str] = []
        names: set[str] = set()
        link_targets: list[tuple[str, str]] = []
        file_count = 0
        directory_count = 0
        symlink_count = 0
        uncompressed_bytes = 0
        if archive_format == "ZIP":
            try:
                with zipfile.ZipFile(archive_path) as handle:
                    failed_member = handle.testzip()
                    if failed_member is not None:
                        _fail(location, f"ZIP integrity failure at {failed_member}")
                    for index, info in enumerate(handle.infolist()):
                        member_location = f"{location}.members[{index}]"
                        name = _archive_member_path(info.filename, member_location)
                        if name in names:
                            _fail(member_location, f"duplicate archive path {name}")
                        names.add(name)
                        mode = info.external_attr >> 16
                        mode_type = stat.S_IFMT(mode)
                        if stat.S_ISLNK(mode):
                            _fail(member_location, f"ZIP symlink is not allowed: {name}")
                        if mode_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                            _fail(member_location, f"unsupported ZIP member type: {name}")
                        if info.flag_bits & 1:
                            _fail(member_location, f"encrypted ZIP member is not allowed: {name}")
                        if info.is_dir():
                            entry_kind = "dir"
                            directory_count += 1
                        else:
                            entry_kind = "file"
                            file_count += 1
                            uncompressed_bytes += info.file_size
                        records.append(f"{entry_kind}\t{info.file_size}\t{name}\n")
            except zipfile.BadZipFile as error:
                _fail(location, f"invalid ZIP archive: {error}")
        else:
            try:
                with tarfile.open(archive_path, mode="r:gz") as handle:
                    for index, info in enumerate(handle.getmembers()):
                        member_location = f"{location}.members[{index}]"
                        name = _archive_member_path(info.name, member_location)
                        if name in names:
                            _fail(member_location, f"duplicate archive path {name}")
                        names.add(name)
                        if info.isdir():
                            entry_kind = "dir"
                            directory_count += 1
                        elif info.isfile():
                            entry_kind = "file"
                            file_count += 1
                            uncompressed_bytes += info.size
                        elif info.issym():
                            entry_kind = "symlink"
                            symlink_count += 1
                            if not info.linkname or "\\" in info.linkname or "\x00" in info.linkname:
                                _fail(member_location, f"unsafe symlink target {info.linkname!r}")
                            target = posixpath.normpath(
                                posixpath.join(posixpath.dirname(name), info.linkname)
                            )
                            if target == ".." or target.startswith("../") or target.startswith("/"):
                                _fail(member_location, f"escaping symlink {name} -> {info.linkname}")
                            link_targets.append((name, target))
                        else:
                            _fail(member_location, f"unsupported TAR member type: {name}")
                        records.append(
                            f"{entry_kind}\t{info.size}\t{name}\t{info.linkname}\n"
                        )
            except (tarfile.TarError, EOFError) as error:
                _fail(location, f"invalid TAR_GZIP archive: {error}")
        for name, target in link_targets:
            if target not in names:
                _fail(location, f"symlink target is absent from archive: {name} -> {target}")
        actual_values = {
            "expected_members": len(records),
            "expected_files": file_count,
            "expected_directories": directory_count,
            "expected_symlinks": symlink_count,
            "expected_uncompressed_file_bytes": uncompressed_bytes,
        }
        for field, expected in expected_values.items():
            if actual_values[field] != expected:
                _fail(
                    f"{location}.{field}",
                    f"archive inventory drift: {actual_values[field]}",
                )
        missing_required = sorted(required_paths - names)
        if missing_required:
            _fail(
                f"{location}.required_relative_paths",
                "archive members are missing: " + ", ".join(missing_required),
            )
        actual_listing_hash = hashlib.sha256(
            "".join(records).encode("utf-8")
        ).hexdigest()
        if actual_listing_hash != expected_listing_hash:
            _fail(
                f"{location}.listing_sha256",
                f"archive listing drift: {actual_listing_hash}",
            )
        return {archive_text}

    if check_type == "onnx_runtime_static_preflight":
        _summary, checked_paths = _validate_onnxruntime_distribution_check(
            check,
            location,
            artifact_paths,
            artifact_roles,
            cache,
        )
        return checked_paths

    if check_type == "tree_manifest":
        allowed = {
            "type",
            "root",
            "manifest_path",
            "file_glob",
            "expected_count",
            "sha256",
        }
        _strict_keys(check, location, allowed, allowed)
        root = _absolute_path(check["root"], f"{location}.root", expect_directory=True)
        manifest_path = _absolute_path(
            check["manifest_path"], f"{location}.manifest_path"
        )
        manifest_text = str(manifest_path)
        if manifest_text not in artifact_paths:
            _fail(location, f"tree manifest is not a registered artifact: {manifest_path}")
        if artifact_roles[manifest_text] != "MANIFEST":
            _fail(location, "tree manifest evidence must have role MANIFEST")
        pattern = _string(check["file_glob"], f"{location}.file_glob", 128)
        if ".." in PurePosixPath(pattern).parts or Path(pattern).is_absolute():
            _fail(f"{location}.file_glob", "must be a safe relative glob")
        expected_count = check["expected_count"]
        if (
            not isinstance(expected_count, int)
            or isinstance(expected_count, bool)
            or expected_count < 1
        ):
            _fail(f"{location}.expected_count", "must be a positive integer")
        expected_hash = _string(check["sha256"], f"{location}.sha256", 64)
        if not SHA256_RE.fullmatch(expected_hash):
            _fail(f"{location}.sha256", "must be a lowercase SHA-256 digest")

        observed_paths: list[str] = []
        digest = hashlib.sha256()
        try:
            lines = manifest_path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            _fail(location, "tree manifest must be UTF-8")
        if len(lines) != expected_count:
            _fail(location, f"tree manifest record count drift: {len(lines)}")
        previous_sort_key: bytes | None = None
        for index, line in enumerate(lines):
            entry_location = f"{location}.manifest[{index}]"
            try:
                raw_entry = json.loads(line, object_pairs_hook=_no_duplicate_json_keys)
            except (json.JSONDecodeError, RegistryValidationError) as error:
                _fail(entry_location, f"invalid JSON record: {error}")
            entry = _mapping(raw_entry, entry_location)
            fields = {"path", "sha256", "size_bytes"}
            _strict_keys(entry, entry_location, fields, fields)
            relative = _safe_relative_path(
                entry["path"], f"{entry_location}.path"
            ).as_posix()
            if not PurePosixPath(relative).match(pattern):
                _fail(entry_location, f"path does not match {pattern}: {relative}")
            sort_key = relative.encode("utf-8")
            if previous_sort_key is not None and sort_key <= previous_sort_key:
                _fail(entry_location, "paths must be unique and bytewise sorted")
            previous_sort_key = sort_key
            expected_file_hash = _string(
                entry["sha256"], f"{entry_location}.sha256", 64
            )
            if not SHA256_RE.fullmatch(expected_file_hash):
                _fail(
                    f"{entry_location}.sha256",
                    "must be a lowercase SHA-256 digest",
                )
            expected_size = entry["size_bytes"]
            if (
                not isinstance(expected_size, int)
                or isinstance(expected_size, bool)
                or expected_size < 1
            ):
                _fail(f"{entry_location}.size_bytes", "must be a positive integer")
            path = (root / relative).resolve(strict=True)
            try:
                path.relative_to(root)
            except ValueError:
                _fail(entry_location, f"path escapes manifest root: {path}")
            actual_hash, actual_size, _mtime = cache.identity(path)
            if actual_hash != expected_file_hash:
                _fail(entry_location, f"SHA-256 drift for {path}: {actual_hash}")
            if actual_size != expected_size:
                _fail(entry_location, f"size drift for {path}: {actual_size}")
            observed_paths.append(relative)
            digest.update(f"{actual_hash}  {relative}\n".encode("utf-8"))

        discovered_paths = sorted(
            (
                path.resolve(strict=True).relative_to(root).as_posix()
                for path in root.rglob(pattern)
                if path.is_file()
            ),
            key=lambda item: item.encode("utf-8"),
        )
        if discovered_paths != observed_paths:
            _fail(location, "tree manifest membership differs from the real file tree")
        actual_digest = digest.hexdigest()
        if actual_digest != expected_hash:
            _fail(location, f"tree manifest SHA-256 drift: {actual_digest}")
        return {manifest_text}

    _fail(f"{location}.type", f"unsupported check type {check_type}")


def _validate_resource(value: Any, location: str, cache: ArtifactCache) -> tuple[str, int, int]:
    resource = _mapping(value, location)
    allowed = {
        "resource_id",
        "kind",
        "name",
        "state",
        "version",
        "source",
        "licenses",
        "use_restrictions",
        "redistribution_status",
        "research_use_status",
        "citations",
        "installation_or_access_method",
        "data_classification",
        "runtime_requirements",
        "consumers",
        "approved_scopes",
        "blocked_reasons",
        "owner_authorization",
        "third_party_rights_status",
        "artifacts",
        "checks",
    }
    required = allowed - {"version"}
    _strict_keys(resource, location, allowed, required)
    resource_id = _string(resource["resource_id"], f"{location}.resource_id", 128)
    if not RESOURCE_ID_RE.fullmatch(resource_id):
        _fail(f"{location}.resource_id", "must be lowercase kebab-case")
    _string(resource["kind"], f"{location}.kind", 128)
    _string(resource["name"], f"{location}.name", 256)
    state = _string(resource["state"], f"{location}.state", 64)
    if state not in ALLOWED_STATES:
        _fail(f"{location}.state", f"unsupported state {state}")
    if "version" in resource:
        _string(resource["version"], f"{location}.version", 256)
    elif state != "BLOCKED":
        _fail(location, "a usable resource must have a verified version")

    artifacts = _sequence(
        resource["artifacts"], f"{location}.artifacts", MAX_ARTIFACTS_PER_RESOURCE
    )
    if not artifacts:
        _fail(f"{location}.artifacts", "must contain real evidence")
    artifact_paths: set[str] = set()
    artifact_roles: dict[str, str] = {}
    for index, artifact in enumerate(artifacts):
        path, role = _validate_artifact(artifact, f"{location}.artifacts[{index}]", cache)
        path_text = str(path)
        if path_text in artifact_paths:
            _fail(f"{location}.artifacts[{index}]", f"duplicate artifact path {path_text}")
        artifact_paths.add(path_text)
        artifact_roles[path_text] = role

    authorization_decision, authorized_scopes = _validate_owner_authorization(
        resource["owner_authorization"],
        f"{location}.owner_authorization",
        resource_id,
        artifact_paths,
        artifact_roles,
    )
    _validate_source(
        resource["source"],
        f"{location}.source",
        resource_id,
        resource.get("version"),
        resource["owner_authorization"],
        artifact_paths,
        artifact_roles,
        resource["checks"],
        cache,
    )
    license_coverage = _validate_licenses(
        resource["licenses"],
        f"{location}.licenses",
        artifact_paths,
        artifact_roles,
    )
    use_restrictions = _strings(
        resource["use_restrictions"], f"{location}.use_restrictions", 64
    )
    if not use_restrictions:
        _fail(f"{location}.use_restrictions", "must state the use boundary")
    _string(resource["redistribution_status"], f"{location}.redistribution_status", 256)
    _string(resource["research_use_status"], f"{location}.research_use_status", 256)
    citations = _strings(resource["citations"], f"{location}.citations", 64)
    if state == "VERIFIED_LIMITED_USE" and not citations:
        _fail(f"{location}.citations", "a usable resource must have a citation")
    _string(
        resource["installation_or_access_method"],
        f"{location}.installation_or_access_method",
    )
    classification = _string(
        resource["data_classification"], f"{location}.data_classification", 128
    )
    if classification not in ALLOWED_CLASSIFICATIONS:
        _fail(f"{location}.data_classification", f"unsupported classification {classification}")
    _validate_runtime_requirements(
        resource["runtime_requirements"], f"{location}.runtime_requirements"
    )
    rights_status = _string(
        resource["third_party_rights_status"],
        f"{location}.third_party_rights_status",
        64,
    )
    if rights_status not in ALLOWED_THIRD_PARTY_RIGHTS_STATUSES:
        _fail(
            f"{location}.third_party_rights_status",
            f"unsupported status {rights_status}",
        )
    consumers = _strings(resource["consumers"], f"{location}.consumers", 64)
    if not consumers:
        _fail(f"{location}.consumers", "must identify graph consumers")
    for consumer in consumers:
        if not CONSUMER_ID_RE.fullmatch(consumer):
            _fail(
                f"{location}.consumers",
                f"invalid canonical graph-node ID {consumer}",
            )
    approved_scopes = _strings(
        resource["approved_scopes"], f"{location}.approved_scopes", 64
    )
    blocked_reasons = _strings(
        resource["blocked_reasons"], f"{location}.blocked_reasons", 64
    )
    if state == "BLOCKED":
        if approved_scopes:
            _fail(f"{location}.approved_scopes", "blocked resources cannot have approved scopes")
        if not blocked_reasons:
            _fail(f"{location}.blocked_reasons", "blocked resources must name a reason")
    elif state == "APPROVED_LOCAL_RESEARCH_USE":
        if not approved_scopes:
            _fail(f"{location}.approved_scopes", "an approved local-research resource needs an approved scope")
        if blocked_reasons:
            _fail(f"{location}.blocked_reasons", "an approved local-research resource cannot retain blockers")
        if authorization_decision != "APPROVED_LOCAL_NONCOMMERCIAL":
            _fail(location, "local research use needs owner APPROVED_LOCAL_NONCOMMERCIAL")
        missing_owner_scopes = sorted(set(approved_scopes) - authorized_scopes)
        if missing_owner_scopes:
            _fail(
                f"{location}.approved_scopes",
                "effective scopes exceed owner authorization: "
                + ", ".join(missing_owner_scopes),
            )
        if resource["owner_authorization"]["redistribution_allowed"] is not False:
            _fail(location, "local research use never permits redistribution")
        if rights_status == "UNRESOLVED":
            _fail(
                f"{location}.third_party_rights_status",
                "local research use requires a factual terminal rights status, not UNRESOLVED",
            )
        if not str(resource["research_use_status"]).startswith(
            LOCAL_RESEARCH_USE_RESEARCH_STATUS_PREFIX
        ):
            _fail(
                f"{location}.research_use_status",
                f"must start with {LOCAL_RESEARCH_USE_RESEARCH_STATUS_PREFIX}",
            )
        if not citations:
            _fail(f"{location}.citations", "an approved local-research resource must have a citation")
    else:
        if not approved_scopes:
            _fail(f"{location}.approved_scopes", "limited-use resources need an approved scope")
        if blocked_reasons:
            _fail(f"{location}.blocked_reasons", "usable resources cannot retain blockers")
        if authorization_decision not in {
            "APPROVED_LIMITED_USE",
            "APPROVED_LOCAL_NONCOMMERCIAL",
        }:
            _fail(location, "a usable resource needs owner limited-use approval")
        missing_owner_scopes = sorted(set(approved_scopes) - authorized_scopes)
        if missing_owner_scopes:
            _fail(
                f"{location}.approved_scopes",
                "effective scopes exceed owner authorization: "
                + ", ".join(missing_owner_scopes),
            )
        if rights_status != "VERIFIED_LICENSE":
            _fail(
                f"{location}.third_party_rights_status",
                "a usable resource needs verified third-party license evidence",
            )

    checks = _sequence(resource["checks"], f"{location}.checks", MAX_CHECKS_PER_RESOURCE)
    checked_paths: set[str] = set()
    for index, check in enumerate(checks):
        checked_paths.update(
            _validate_check(
                check,
                f"{location}.checks[{index}]",
                artifact_paths,
                artifact_roles,
                cache,
            )
        )
    _validate_source_check_consistency(
        resource["source"], checks, f"{location}.source"
    )
    if state == "VERIFIED_LIMITED_USE":
        if not resource["licenses"]:
            _fail(f"{location}.licenses", "a usable resource must have license evidence")
        if not checks:
            _fail(f"{location}.checks", "a usable resource must have executable checks")
        required_coverage = {
            path
            for path, role in artifact_roles.items()
            if role in LICENSE_COVERAGE_REQUIRED_ROLES
        }
        uncovered = sorted(required_coverage - license_coverage)
        if uncovered:
            _fail(
                f"{location}.licenses",
                f"usable artifacts lack license coverage: {', '.join(uncovered)}",
            )
        required_checks = {
            path
            for path, role in artifact_roles.items()
            if role in EXECUTABLE_CHECK_REQUIRED_ROLES
        }
        unchecked = sorted(required_checks - checked_paths)
        if unchecked:
            _fail(
                f"{location}.checks",
                f"usable artifacts lack an executable check: {', '.join(unchecked)}",
            )
    return resource_id, len(artifacts), len(checks)


def _model_tree_check(
    resource: dict[str, Any],
    location: str,
    expected_glob: str,
) -> dict[str, Any]:
    matches = [
        _mapping(check, f"{location}.checks[{index}]")
        for index, check in enumerate(
            _sequence(resource["checks"], f"{location}.checks", MAX_CHECKS_PER_RESOURCE)
        )
        if isinstance(check, dict) and check.get("type") == "glob_manifest"
    ]
    if len(matches) != 1:
        _fail(location, f"must have exactly one model-tree glob_manifest; found {len(matches)}")
    check = matches[0]
    if check.get("glob") != expected_glob or check.get("expected_count") != 24:
        _fail(location, "model-tree glob_manifest must bind all 24 regular files")
    return check


def _registered_tree(
    resource: dict[str, Any],
    root: Path,
    location: str,
) -> dict[str, tuple[str, str, int]]:
    records: dict[str, tuple[str, str, int]] = {}
    for index, raw_artifact in enumerate(
        _sequence(
            resource["artifacts"],
            f"{location}.artifacts",
            MAX_ARTIFACTS_PER_RESOURCE,
        )
    ):
        artifact_location = f"{location}.artifacts[{index}]"
        artifact = _mapping(raw_artifact, artifact_location)
        path = _absolute_path(
            artifact["path"], f"{artifact_location}.path"
        )
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if relative in records:
            _fail(location, f"duplicate normalized model-tree path {relative}")
        records[relative] = (
            _string(artifact["role"], f"{artifact_location}.role"),
            _string(artifact["sha256"], f"{artifact_location}.sha256", 64),
            artifact["size_bytes"],
        )
    return records


def _normalized_model_license_map(
    resource: dict[str, Any],
    root: Path,
    tree: dict[str, tuple[str, str, int]],
    location: str,
) -> tuple[
    set[tuple[str, str, tuple[str, ...]]],
    Counter[str],
    Counter[str],
]:
    records: set[tuple[str, str, tuple[str, ...]]] = set()
    coverage: Counter[str] = Counter()
    expressions: Counter[str] = Counter()
    for index, raw_license in enumerate(
        _sequence(resource["licenses"], f"{location}.licenses", 64)
    ):
        item_location = f"{location}.licenses[{index}]"
        license_record = _mapping(raw_license, item_location)
        artifact_path = _absolute_path(
            license_record["artifact_path"], f"{item_location}.artifact_path"
        )
        target_paths = [
            _absolute_path(target, f"{item_location}.applies_to[{target_index}]")
            for target_index, target in enumerate(
                _strings(license_record["applies_to"], f"{item_location}.applies_to")
            )
        ]
        try:
            license_relative = artifact_path.relative_to(root).as_posix()
        except ValueError:
            for target_index, target_path in enumerate(target_paths):
                try:
                    target_path.relative_to(root)
                except ValueError:
                    continue
                _fail(
                    f"{item_location}.applies_to[{target_index}]",
                    "license outside the model tree targets a model-tree artifact",
                )
            continue
        if tree.get(license_relative, (None, None, None))[0] != "LICENSE":
            _fail(item_location, "model license artifact is not a registered LICENSE")
        target_relatives: list[str] = []
        for target_index, target_path in enumerate(target_paths):
            try:
                target_relative = target_path.relative_to(root).as_posix()
            except ValueError:
                _fail(
                    f"{item_location}.applies_to[{target_index}]",
                    "model license target is outside the registered model tree",
                )
            if tree.get(target_relative, (None, None, None))[0] != "MODEL":
                _fail(
                    f"{item_location}.applies_to[{target_index}]",
                    "model license target is not a registered MODEL",
                )
            target_relatives.append(target_relative)
            coverage[target_relative] += 1
        if len(target_relatives) != len(set(target_relatives)):
            _fail(item_location, "model license record repeats a payload")
        license_text = _read_utf8(artifact_path, f"{item_location}.artifact_path")
        for target_relative in target_relatives:
            payload_name = PurePosixPath(target_relative).name
            if payload_name not in license_text:
                _fail(
                    item_location,
                    f"bundled license document does not name payload {payload_name}",
                )
        marker = "# OFIQ License"
        if license_text.count(marker) != 1:
            _fail(item_location, "bundled license document must contain one OFIQ License section")
        original_text, ofiq_text = license_text.split(marker, maxsplit=1)
        if "Permission is hereby granted, free of charge" not in ofiq_text:
            _fail(item_location, "OFIQ License section does not contain the MIT grant")
        if "[Apache 2.0 license](" in original_text:
            documented_expression = "Apache-2.0 AND OFIQ-MIT"
        elif re.search(r"\[MIT(?: license)?\]\(", original_text):
            documented_expression = "MIT AND OFIQ-MIT"
        elif "# Original License" in original_text:
            _fail(item_location, "original license label is not recognized")
        else:
            documented_expression = "OFIQ-MIT"
        declared_expression = _string(
            license_record["expression"], f"{item_location}.expression", 256
        )
        if declared_expression != documented_expression:
            _fail(
                item_location,
                f"license expression differs from bundled text: {documented_expression}",
            )
        normalized = (
            declared_expression,
            license_relative,
            tuple(sorted(target_relatives)),
        )
        if normalized in records:
            _fail(item_location, "duplicates a normalized model license record")
        records.add(normalized)
        expressions[declared_expression] += 1
    return records, coverage, expressions


def _ofiq_quarantine_binding(
    candidate: dict[str, Any],
    location: str,
    archive_format: str,
    retrieval_role: str,
) -> tuple[Path, Path]:
    receipt_paths = [
        _absolute_path(artifact["path"], f"{location}.artifacts[{index}].path")
        for index, artifact in enumerate(
            _sequence(
                candidate["artifacts"],
                f"{location}.artifacts",
                MAX_ARTIFACTS_PER_RESOURCE,
            )
        )
        if isinstance(artifact, dict) and artifact.get("role") == "SOURCE_RECEIPT"
    ]
    if len(receipt_paths) != 1:
        _fail(location, f"must have exactly one acquisition receipt; found {len(receipt_paths)}")
    try:
        receipt = json.loads(
            _read_utf8(receipt_paths[0], f"{location}.acquisition_receipt"),
            object_pairs_hook=_no_duplicate_json_keys,
        )
    except json.JSONDecodeError as error:
        _fail(location, f"acquisition receipt is not valid UTF-8 JSON: {error}")
    receipt = _mapping(receipt, f"{location}.acquisition_receipt")
    retrievals = [
        _mapping(item, f"{location}.acquisition_receipt.retrievals[{index}]")
        for index, item in enumerate(
            _sequence(
                receipt.get("retrievals"),
                f"{location}.acquisition_receipt.retrievals",
                16,
            )
        )
        if isinstance(item, dict) and item.get("role") == retrieval_role
    ]
    if len(retrievals) != 1:
        _fail(
            location,
            f"acquisition receipt must identify one {retrieval_role} retrieval",
        )
    archive_path = _absolute_path(
        retrievals[0].get("path"),
        f"{location}.acquisition_receipt.retrieval.path",
    )
    archive_validations = [
        _mapping(item, f"{location}.acquisition_receipt.archive_validation[{index}]")
        for index, item in enumerate(
            _sequence(
                receipt.get("archive_validation"),
                f"{location}.acquisition_receipt.archive_validation",
                16,
            )
        )
        if (
            isinstance(item, dict)
            and item.get("format") == archive_format
            and item.get("path") == str(archive_path)
        )
    ]
    if len(archive_validations) != 1:
        _fail(
            location,
            f"acquisition receipt must bind one {archive_format} quarantine to {retrieval_role}",
        )
    quarantine = _absolute_path(
        archive_validations[0].get("quarantine_path"),
        f"{location}.acquisition_receipt.archive_validation.quarantine_path",
        expect_directory=True,
    )
    return archive_path, quarantine


def _ofiq_model_quarantine_root(
    candidate: dict[str, Any],
    location: str,
) -> Path:
    _archive_path, quarantine = _ofiq_quarantine_binding(
        candidate,
        location,
        "ZIP",
        "OFIQ_MODEL_BUNDLE",
    )
    model_root = _absolute_path(
        str(quarantine / "models"),
        f"{location}.acquisition_receipt.model_quarantine",
        expect_directory=True,
    )
    if not model_root.is_dir():
        _fail(location, f"model quarantine is not a directory: {model_root}")
    return model_root


def _validate_ofiq_v112_model_bundle_equivalence(
    resources_by_id: dict[str, dict[str, Any]],
    cache: ArtifactCache,
) -> dict[str, Any]:
    reference_id = "ofiq-v1-1-0-build-runtime"
    candidate_id = "ofiq-v1-1-2-cmake-runtime-candidate"
    try:
        reference = resources_by_id[reference_id]
        candidate = resources_by_id[candidate_id]
    except KeyError as error:
        _fail("registry.resources", f"missing OFIQ model comparison resource {error.args[0]}")

    reference_location = f"registry.resources[{reference_id}]"
    candidate_location = f"registry.resources[{candidate_id}]"
    reference_check = _model_tree_check(
        reference, reference_location, "data/models/**/*"
    )
    candidate_check = _model_tree_check(candidate, candidate_location, "**/*")
    reference_base = _absolute_path(
        reference_check["base_path"],
        f"{reference_location}.model_tree.base_path",
        expect_directory=True,
    )
    reference_source = _mapping(
        reference["source"], f"{reference_location}.source"
    )
    reference_source_root = _absolute_path(
        reference_source["local_root"],
        f"{reference_location}.source.local_root",
        expect_directory=True,
    )
    if reference_base != reference_source_root:
        _fail(reference_location, "historical model manifest is not source-root-bound")
    reference_relative_to = _absolute_path(
        reference_check["relative_to"],
        f"{reference_location}.model_tree.relative_to",
        expect_directory=True,
    )
    if reference_relative_to != reference_source_root:
        _fail(reference_location, "historical model manifest is not source-normalized")
    reference_root = _absolute_path(
        str(reference_base / "data" / "models"),
        f"{reference_location}.model_root",
        expect_directory=True,
    )
    if not reference_root.is_dir():
        _fail(reference_location, f"historical model root is not a directory: {reference_root}")
    candidate_root = _absolute_path(
        candidate_check["base_path"],
        f"{candidate_location}.model_tree.base_path",
        expect_directory=True,
    )
    if candidate_root != _ofiq_model_quarantine_root(candidate, candidate_location):
        _fail(candidate_location, "model-tree check does not use the receipt-bound quarantine")
    candidate_relative_to = _absolute_path(
        candidate_check["relative_to"],
        f"{candidate_location}.model_tree.relative_to",
        expect_directory=True,
    )
    if candidate_relative_to != candidate_root:
        _fail(candidate_location, "candidate model manifest must be root-normalized")

    reference_tree = _registered_tree(reference, reference_root, reference_location)
    candidate_tree = _registered_tree(candidate, candidate_root, candidate_location)
    if set(reference_tree) != set(candidate_tree):
        _fail(candidate_location, "candidate and historical model-tree paths differ")
    if len(candidate_tree) != 24:
        _fail(candidate_location, f"model tree must contain 24 registered files; found {len(candidate_tree)}")
    for relative in sorted(reference_tree):
        if candidate_tree[relative] != reference_tree[relative]:
            _fail(candidate_location, f"model-tree role or byte identity differs: {relative}")

    role_counts = Counter(record[0] for record in candidate_tree.values())
    expected_roles = Counter({"DOCUMENTATION": 2, "LICENSE": 10, "MODEL": 12})
    if role_counts != expected_roles:
        _fail(candidate_location, f"model-tree role counts differ: {dict(role_counts)}")

    candidate_paths = [candidate_root / relative for relative in candidate_tree]
    manifest_sha256 = _manifest_digest(
        candidate_paths,
        candidate_root,
        cache,
        f"{candidate_location}.model_tree",
    )
    if candidate_check.get("sha256") != manifest_sha256:
        _fail(candidate_location, "candidate model-tree normalized manifest differs")
    reference_manifest_sha256 = _manifest_digest(
        [reference_root / relative for relative in reference_tree],
        reference_root,
        cache,
        f"{reference_location}.model_tree",
    )
    if reference_manifest_sha256 != manifest_sha256:
        _fail(candidate_location, "candidate and historical normalized manifests differ")

    reference_licenses, reference_coverage, reference_expressions = (
        _normalized_model_license_map(
            reference, reference_root, reference_tree, reference_location
        )
    )
    candidate_licenses, candidate_coverage, candidate_expressions = (
        _normalized_model_license_map(
            candidate, candidate_root, candidate_tree, candidate_location
        )
    )
    if reference_licenses != OFIQ_V112_MODEL_LICENSE_CONTRACT:
        _fail(reference_location, "historical model license map differs from the governed contract")
    if candidate_licenses != OFIQ_V112_MODEL_LICENSE_CONTRACT:
        _fail(candidate_location, "candidate model license map differs from the governed contract")
    if candidate_licenses != reference_licenses:
        _fail(candidate_location, "candidate model license map differs from exact bundled evidence")
    if candidate_expressions != reference_expressions:
        _fail(candidate_location, "candidate documented license expressions differ")
    model_paths = {
        relative for relative, record in candidate_tree.items() if record[0] == "MODEL"
    }
    expected_coverage = Counter({relative: 1 for relative in model_paths})
    if reference_coverage != expected_coverage or candidate_coverage != expected_coverage:
        _fail(candidate_location, "each model payload must have exactly one bundled license record")
    if len(candidate_licenses) != 10:
        _fail(candidate_location, f"expected 10 model license records; found {len(candidate_licenses)}")

    bundle_readme = candidate_root / "README.md"
    if candidate_tree.get("README.md", (None, None, None))[0] != "DOCUMENTATION":
        _fail(candidate_location, "bundle README is not registered documentation")
    readme_text = _read_utf8(bundle_readme, f"{candidate_location}.bundle_readme")
    for relative in sorted(model_paths | {record[1] for record in candidate_licenses}):
        if relative not in readme_text:
            _fail(candidate_location, f"bundle README does not inventory {relative}")

    closure_checks = [
        _mapping(check, f"{candidate_location}.checks[{index}]")
        for index, check in enumerate(
            _sequence(
                candidate["checks"],
                f"{candidate_location}.checks",
                MAX_CHECKS_PER_RESOURCE,
            )
        )
        if isinstance(check, dict) and check.get("type") == "jaxn_model_closure"
    ]
    if len(closure_checks) != 1:
        _fail(candidate_location, "candidate must have one tagged-configuration model closure")
    closure = closure_checks[0]
    source = _mapping(candidate["source"], f"{candidate_location}.source")
    source_root = _absolute_path(
        source["local_root"],
        f"{candidate_location}.source.local_root",
        expect_directory=True,
    )
    expected_config_path = _absolute_path(
        str(source_root / "data" / "ofiq_config.jaxn"),
        f"{candidate_location}.expected_config_path",
    )
    configuration_path = _absolute_path(
        closure.get("config_path"), f"{candidate_location}.configuration_path"
    )
    if configuration_path != expected_config_path:
        _fail(candidate_location, "model closure does not use the tagged source configuration")
    artifact_roles = {
        str(
            _absolute_path(
                artifact["path"], f"{candidate_location}.artifacts[{index}].path"
            )
        ): _string(artifact["role"], f"{candidate_location}.artifacts[{index}].role")
        for index, artifact in enumerate(
            _sequence(
                candidate["artifacts"],
                f"{candidate_location}.artifacts",
                MAX_ARTIFACTS_PER_RESOURCE,
            )
        )
        if isinstance(artifact, dict)
    }
    if artifact_roles.get(str(configuration_path)) != "CONFIGURATION":
        _fail(candidate_location, "tagged source configuration is not registered as CONFIGURATION")
    evidence_paths = {
        str(
            _absolute_path(
                path, f"{candidate_location}.source.evidence_artifacts[{index}]"
            )
        )
        for index, path in enumerate(
            _strings(
                source["evidence_artifacts"],
                f"{candidate_location}.source.evidence_artifacts",
            )
        )
    }
    if str(configuration_path) not in evidence_paths:
        _fail(candidate_location, "tagged source configuration is not source evidence")
    configuration_data_root = _absolute_path(
        closure.get("data_root"),
        f"{candidate_location}.configuration_data_root",
        expect_directory=True,
    )
    if configuration_data_root != candidate_root.parent:
        _fail(candidate_location, "model closure data root is not the receipt-bound quarantine")
    configured_relatives: set[str] = set()
    for index, path in enumerate(
        _sequence(
            closure.get("relative_paths"),
            f"{candidate_location}.configured_paths",
            256,
        )
    ):
        normalized = _safe_relative_path(
            path, f"{candidate_location}.configured_paths[{index}]"
        )
        try:
            relative = normalized.relative_to("models").as_posix()
        except ValueError:
            _fail(
                f"{candidate_location}.configured_paths[{index}]",
                "configured model path must be below models/",
            )
        configured_relatives.add(relative)
    if configured_relatives != model_paths:
        _fail(candidate_location, "tagged configuration does not consume the 12 registered payloads")
    configured_manifest_sha256 = _string(
        closure.get("sha256"), f"{candidate_location}.configured_manifest_sha256", 64
    )

    if candidate.get("state") != "BLOCKED":
        _fail(candidate_location, "model evidence must not promote the candidate")
    if candidate.get("third_party_rights_status") != "UNRESOLVED":
        _fail(candidate_location, "bundle-internal license mapping is not rights closure")
    if candidate.get("approved_scopes") != []:
        _fail(candidate_location, "model evidence must not authorize execution")

    return {
        "reference_resource_id": reference_id,
        "candidate_resource_id": candidate_id,
        "manifest_sha256": manifest_sha256,
        "files": len(candidate_tree),
        "role_counts": dict(sorted(role_counts.items())),
        "license_records": len(candidate_licenses),
        "covered_models": len(candidate_coverage),
        "configured_models": len(configured_relatives),
        "configured_manifest_sha256": configured_manifest_sha256,
        "configuration_path": str(configuration_path),
        "configuration_data_root": str(configuration_data_root),
        "reference_model_root": str(reference_root),
        "documented_license_expressions": dict(sorted(candidate_expressions.items())),
        "all_candidate_model_target_edges": sum(candidate_coverage.values()),
        "candidate_state": candidate["state"],
        "candidate_rights_status": candidate["third_party_rights_status"],
        "candidate_approved_scopes": candidate["approved_scopes"],
    }


def _validate_ofiq_v112_onnxruntime_static_preflight(
    resources_by_id: dict[str, dict[str, Any]],
    cache: ArtifactCache,
    static_authorization: dict[str, str],
) -> dict[str, Any]:
    candidate_id = "ofiq-v1-1-2-cmake-runtime-candidate"
    try:
        candidate = resources_by_id[candidate_id]
    except KeyError as error:
        _fail("registry.resources", f"missing OFIQ ONNX candidate {error.args[0]}")
    candidate_location = f"registry.resources[{candidate_id}]"

    checks = [
        _mapping(check, f"{candidate_location}.checks[{index}]")
        for index, check in enumerate(
            _sequence(
                candidate["checks"],
                f"{candidate_location}.checks",
                MAX_CHECKS_PER_RESOURCE,
            )
        )
        if isinstance(check, dict)
        and check.get("type") == "onnx_runtime_static_preflight"
    ]
    if len(checks) != 1:
        _fail(candidate_location, f"must have one ONNX static preflight; found {len(checks)}")
    check = checks[0]
    artifact_paths: set[str] = set()
    artifact_roles: dict[str, str] = {}
    artifact_records: dict[str, dict[str, Any]] = {}
    for index, raw_artifact in enumerate(
        _sequence(
            candidate["artifacts"],
            f"{candidate_location}.artifacts",
            MAX_ARTIFACTS_PER_RESOURCE,
        )
    ):
        artifact_location = f"{candidate_location}.artifacts[{index}]"
        artifact = _mapping(raw_artifact, artifact_location)
        path = _absolute_path(artifact["path"], f"{artifact_location}.path")
        path_text = str(path)
        if path_text in artifact_paths:
            _fail(artifact_location, f"duplicate artifact path {path_text}")
        artifact_paths.add(path_text)
        artifact_roles[path_text] = _string(
            artifact["role"], f"{artifact_location}.role"
        )
        artifact_records[path_text] = artifact
    summary, _checked_paths = _validate_onnxruntime_distribution_check(
        check,
        f"{candidate_location}.onnx_runtime_static_preflight",
        artifact_paths,
        artifact_roles,
        cache,
    )

    receipt_archive, receipt_quarantine = _ofiq_quarantine_binding(
        candidate,
        candidate_location,
        "TAR_GZIP",
        "ONNX_RUNTIME_LINUX_X64_1_18_1",
    )
    expected_root = _absolute_path(
        str(receipt_quarantine / "onnxruntime-linux-x64-1.18.1"),
        f"{candidate_location}.onnx_runtime_root",
        expect_directory=True,
    )
    if summary["archive_path"] != str(receipt_archive):
        _fail(candidate_location, "ONNX preflight archive is not receipt-bound")
    if summary["root"] != str(expected_root):
        _fail(candidate_location, "ONNX preflight root is not receipt-bound")
    expected_static_authorization = _absolute_path(
        str(
            Path(__file__).resolve().parents[1]
            / "resources"
            / "authorizations"
            / "qualsight-onnxruntime-static-review-2026-08-14.json"
        ),
        f"{candidate_location}.static_review_authorization",
    )
    if summary["static_review_authorization_path"] != str(
        expected_static_authorization
    ):
        _fail(candidate_location, "ONNX preflight does not bind the owner review decision")
    if static_authorization["path"] != str(expected_static_authorization):
        _fail(candidate_location, "prevalidated owner review decision differs")

    source = _mapping(candidate["source"], f"{candidate_location}.source")
    source_root = _absolute_path(
        source["local_root"],
        f"{candidate_location}.source.local_root",
        expect_directory=True,
    )
    expected_cmake = _absolute_path(
        str(source_root / "OFIQlib" / "CMakeLists.ubuntu.cmake"),
        f"{candidate_location}.tagged_linux_cmake",
    )
    if summary["tagged_cmake_path"] != str(expected_cmake):
        _fail(candidate_location, "ONNX preflight does not use tagged Linux CMake")
    evidence_paths = {
        str(_absolute_path(path, f"{candidate_location}.source.evidence_artifacts[{index}]"))
        for index, path in enumerate(
            _strings(
                source["evidence_artifacts"],
                f"{candidate_location}.source.evidence_artifacts",
            )
        )
    }
    if str(expected_cmake) not in evidence_paths:
        _fail(candidate_location, "tagged Linux CMake is not registered source evidence")

    root_license = expected_root / "LICENSE"
    notices_path = expected_root / "ThirdPartyNotices.txt"
    library_path = expected_root / "lib" / "libonnxruntime.so.1.18.1"
    root_artifact_paths = {str(expected_root / relative) for relative in OFIQ_V112_ONNX_RUNTIME_ROLES}
    non_license_paths = {
        str(expected_root / relative)
        for relative, role in OFIQ_V112_ONNX_RUNTIME_ROLES.items()
        if role != "LICENSE"
    }
    onnx_license_records: list[tuple[str, str, frozenset[str]]] = []
    for index, raw_license in enumerate(
        _sequence(candidate["licenses"], f"{candidate_location}.licenses", 128)
    ):
        item_location = f"{candidate_location}.licenses[{index}]"
        license_record = _mapping(raw_license, item_location)
        artifact_path = _absolute_path(
            license_record["artifact_path"], f"{item_location}.artifact_path"
        )
        targets = frozenset(
            str(_absolute_path(target, f"{item_location}.applies_to[{target_index}]"))
            for target_index, target in enumerate(
                _strings(license_record["applies_to"], f"{item_location}.applies_to")
            )
        )
        if str(artifact_path) in root_artifact_paths:
            if not targets <= root_artifact_paths:
                _fail(item_location, "ONNX license evidence has a cross-root target")
            onnx_license_records.append(
                (
                    _string(license_record["expression"], f"{item_location}.expression", 256),
                    str(artifact_path),
                    targets,
                )
            )
        elif targets & root_artifact_paths:
            _fail(item_location, "license outside ONNX package targets its artifacts")
    expected_license_records = {
        ("MIT", str(root_license), frozenset(non_license_paths)),
        (
            "LicenseRef-ONNXRuntime-ThirdParty-Notices",
            str(notices_path),
            frozenset({str(library_path)}),
        ),
    }
    if len(onnx_license_records) != 2 or set(onnx_license_records) != expected_license_records:
        _fail(candidate_location, "ONNX package license/notice map differs")
    license_hash, _license_size, _license_mtime = cache.identity(root_license)
    notices_hash, _notices_size, _notices_mtime = cache.identity(notices_path)
    if license_hash != OFIQ_V112_ONNX_RUNTIME_LICENSE_SHA256:
        _fail(candidate_location, "ONNX MIT license bytes differ")
    if notices_hash != OFIQ_V112_ONNX_RUNTIME_NOTICES_SHA256:
        _fail(candidate_location, "ONNX third-party notice bytes differ")
    license_text = _read_utf8(root_license, f"{candidate_location}.onnx_license")
    if (
        not license_text.startswith("MIT License\n")
        or "Permission is hereby granted, free of charge" not in license_text
        or 'THE SOFTWARE IS PROVIDED "AS IS"' not in license_text
    ):
        _fail(candidate_location, "ONNX root license is not the complete MIT grant")
    notices_text = _read_utf8(notices_path, f"{candidate_location}.onnx_notices")
    if (
        not notices_text.startswith("THIRD PARTY SOFTWARE NOTICES AND INFORMATION\n")
        or "This software incorporates material from third parties." not in notices_text
    ):
        _fail(candidate_location, "ONNX third-party notice title or scope differs")

    if candidate.get("state") != "BLOCKED":
        _fail(candidate_location, "static ONNX evidence must not promote the candidate")
    if candidate.get("third_party_rights_status") != "UNRESOLVED":
        _fail(candidate_location, "bundled notices are not rights closure")
    if candidate.get("approved_scopes") != []:
        _fail(candidate_location, "static ONNX evidence must not authorize execution")
    owner = _mapping(candidate["owner_authorization"], f"{candidate_location}.owner_authorization")
    if (
        owner.get("decision") != "APPROVED_EVIDENCE_ACQUISITION"
        or owner.get("scopes") != [OFIQ_V112_ACQUISITION_SCOPE]
    ):
        _fail(candidate_location, "ONNX static review exceeds the acquisition-only grant")
    if owner.get("evidence_artifact_path") == static_authorization["path"]:
        _fail(candidate_location, "static review and historical acquisition decisions must remain separate")
    restrictions = _strings(
        candidate.get("use_restrictions"),
        f"{candidate_location}.use_restrictions",
    )
    if restrictions != OFIQ_V112_ONNX_STATIC_REVIEW_RESTRICTIONS:
        _fail(candidate_location, "ONNX review restrictions differ from both owner decisions")
    if candidate.get("research_use_status") != OFIQ_V112_RESEARCH_USE_STATUS:
        _fail(candidate_location, "research-use status omits or exceeds the two owner decisions")

    return {
        **summary,
        "package_mit_targets": len(non_license_paths),
        "third_party_notice_targets": 1,
        "license_sha256": license_hash,
        "third_party_notices_sha256": notices_hash,
        "static_review_authorization_path": static_authorization["path"],
        "static_review_authorization_recorded_at": static_authorization[
            "recorded_at"
        ],
        "static_review_authorization_decision": static_authorization["decision"],
        "static_review_authorization_scope": static_authorization["scope"],
        "candidate_state": candidate["state"],
        "candidate_rights_status": candidate["third_party_rights_status"],
        "candidate_approved_scopes": candidate["approved_scopes"],
    }


def validate_registry(registry: dict[str, Any]) -> dict[str, Any]:
    allowed = {"schema_version", "registry_id", "recorded_at", "source_requirements", "resources"}
    _strict_keys(registry, "registry", allowed, allowed)
    schema_version = _string(registry["schema_version"], "registry.schema_version", 32)
    if schema_version != "1.2.0":
        _fail("registry.schema_version", f"unsupported version {schema_version}")
    registry_id = _string(registry["registry_id"], "registry.registry_id", 128)
    if not RESOURCE_ID_RE.fullmatch(registry_id):
        _fail("registry.registry_id", "must be lowercase kebab-case")
    recorded_at = _string(registry["recorded_at"], "registry.recorded_at", 64)
    try:
        timestamp = datetime.fromisoformat(recorded_at)
    except ValueError:
        _fail("registry.recorded_at", "must be an ISO-8601 timestamp")
    if timestamp.tzinfo is None:
        _fail("registry.recorded_at", "must include a UTC offset")
    requirements = _strings(registry["source_requirements"], "registry.source_requirements", 128)
    if not requirements:
        _fail("registry.source_requirements", "must not be empty")
    for requirement in requirements:
        if not REQUIREMENT_ID_RE.fullmatch(requirement):
            _fail("registry.source_requirements", f"invalid requirement ID {requirement}")
    resources = _sequence(registry["resources"], "registry.resources", MAX_RESOURCES)
    if not resources:
        _fail("registry.resources", "must contain at least one real resource")

    cache = ArtifactCache()
    static_authorization = _prevalidate_onnx_static_review_authorization(
        resources,
        timestamp,
        cache,
    )
    resource_ids: list[str] = []
    artifact_count = 0
    check_count = 0
    states: Counter[str] = Counter()
    for index, resource in enumerate(resources):
        resource_id, artifacts, checks = _validate_resource(
            resource, f"registry.resources[{index}]", cache
        )
        resource_ids.append(resource_id)
        artifact_count += artifacts
        check_count += checks
        states[resource["state"]] += 1
    if len(resource_ids) != len(set(resource_ids)):
        _fail("registry.resources", "contains duplicate resource IDs")
    resources_by_id = {
        resource_id: _mapping(resource, f"registry.resources[{index}]")
        for index, (resource_id, resource) in enumerate(zip(resource_ids, resources))
    }
    ofiq_v112_model_bundle = _validate_ofiq_v112_model_bundle_equivalence(
        resources_by_id,
        cache,
    )
    ofiq_v112_onnxruntime_static_preflight = (
        _validate_ofiq_v112_onnxruntime_static_preflight(
            resources_by_id,
            cache,
            static_authorization,
        )
    )
    return {
        "registry_id": registry_id,
        "resources": len(resources),
        "artifacts": artifact_count,
        "checks": check_count,
        "states": dict(sorted(states.items())),
        "ofiq_v112_model_bundle": ofiq_v112_model_bundle,
        "ofiq_v112_onnxruntime_static_preflight": (
            ofiq_v112_onnxruntime_static_preflight
        ),
    }


def validate_registry_file(path: Path) -> dict[str, Any]:
    return validate_registry(_load_json(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a QualSight resource registry against local bytes."
    )
    parser.add_argument("registry", type=Path)
    args = parser.parse_args(argv)
    try:
        summary = validate_registry_file(args.registry)
    except (OSError, RegistryValidationError, subprocess.SubprocessError) as error:
        print(f"RESOURCE_REGISTRY_INVALID: {error}", file=sys.stderr)
        return 1
    states = ",".join(f"{key}={value}" for key, value in summary["states"].items())
    print(
        "RESOURCE_REGISTRY_VALID "
        f"id={summary['registry_id']} resources={summary['resources']} "
        f"artifacts={summary['artifacts']} checks={summary['checks']} states={states}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
