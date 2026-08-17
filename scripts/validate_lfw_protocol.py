#!/usr/bin/env python3
"""Reconcile the registered real LFW archive, tree, and pair protocol bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
from typing import Any

from scripts.validate_resource_registry import validate_registry_file


RESOURCE_ID = "lfw-funneled-local-candidate"
PROTOCOL_ID = "lfw-local-pairs-v1"
ARCHIVE_ROOT = "lfw_funneled"
IMAGE_MANIFEST_NAME = "lfw-funneled-images-v1.jsonl"
PROTOCOL_MANIFEST_NAME = "lfw-local-pairs-v1.jsonl"
ROOT_PAIRS_NAME = "pairs.txt"
FOLD_COUNT = 10
PAIRS_PER_CLASS_PER_FOLD = 300
EXPECTED_IMAGE_COUNT = 13_233
EXPECTED_IDENTITY_DIRECTORY_COUNT = 5_749
EXPECTED_ARCHIVE_MEMBER_COUNT = 18_994
EXPECTED_PROTOCOL_FILE_COUNT = 11
EXPECTED_MATED_PAIRS = 3_000
EXPECTED_NONMATED_PAIRS = 3_000
EXPECTED_PAIR_COUNT = 6_000
EXPECTED_UNIQUE_PROTOCOL_SAMPLES = 7_701
EXPECTED_UNIQUE_PROTOCOL_SUBJECTS = 4_281
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LIMITATIONS = (
    "NOT_AN_ACQUISITION_RECEIPT",
    "NOT_LICENSE_OR_PERMISSION_EVIDENCE",
    "NOT_COMPLETE_OFFICIAL_PROTOCOL_CONFORMANCE",
    "NO_IMAGE_DECODING_OR_MODEL_EXECUTION",
)


class LFWProtocolValidationError(ValueError):
    """Raised when current registered LFW evidence contradicts the contract."""


def _fail(message: str) -> None:
    raise LFWProtocolValidationError(message)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{location} must be an object")
    return value


def _strict_keys(
    value: dict[str, Any],
    location: str,
    expected: set[str],
) -> None:
    actual = set(value)
    if actual != expected:
        _fail(
            f"{location} keys differ: missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )


def _read_json(path: Path, location: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail(f"cannot read {location} as UTF-8 JSON: {error}")
    return _mapping(value, location)


def _safe_relative_path(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{location} must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        _fail(f"{location} is not a safe relative path: {value!r}")
    normalized = path.as_posix()
    if normalized != value or normalized.startswith("/"):
        _fail(f"{location} is not normalized: {value!r}")
    return normalized


def _sha256_path(path: Path) -> str:
    if path.is_symlink():
        _fail(f"refusing symlink evidence: {path}")
    try:
        metadata = path.stat()
    except OSError as error:
        _fail(f"cannot stat evidence {path}: {error}")
    if not stat.S_ISREG(metadata.st_mode):
        _fail(f"evidence is not a regular file: {path}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        _fail(f"cannot hash evidence {path}: {error}")
    return digest.hexdigest()


def _artifact_identity(artifact: dict[str, Any], location: str) -> tuple[Path, str, int]:
    _strict_keys(artifact, location, {"role", "path", "sha256", "size_bytes", "mtime_ns"})
    path_value = artifact["path"]
    digest = artifact["sha256"]
    size = artifact["size_bytes"]
    if not isinstance(path_value, str) or not Path(path_value).is_absolute():
        _fail(f"{location}.path must be absolute")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        _fail(f"{location}.sha256 must be a lowercase SHA-256 digest")
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        _fail(f"{location}.size_bytes must be a positive integer")
    return Path(path_value), digest, size


def _find_one(
    values: list[dict[str, Any]],
    predicate: Any,
    description: str,
) -> dict[str, Any]:
    matches = [value for value in values if predicate(value)]
    if len(matches) != 1:
        _fail(f"expected exactly one {description}; found {len(matches)}")
    return matches[0]


def _load_context(registry_path: Path) -> dict[str, Any]:
    validate_registry_file(registry_path)
    registry = _read_json(registry_path, "registry")
    resources = registry.get("resources")
    if not isinstance(resources, list):
        _fail("registry.resources must be an array")
    resource = _find_one(
        [_mapping(value, f"registry.resources[{index}]") for index, value in enumerate(resources)],
        lambda value: value.get("resource_id") == RESOURCE_ID,
        f"resource {RESOURCE_ID}",
    )
    # This local-byte child never promotes the dataset. The registry state is
    # either still BLOCKED, or APPROVED_LOCAL_RESEARCH_USE backed by the owner
    # use-permission receipt with factual (non-license) third-party rights.
    state = resource.get("state")
    if state == "BLOCKED":
        if resource.get("approved_scopes") != []:
            _fail("a BLOCKED LFW resource must not have an effective approved scope")
    elif state == "APPROVED_LOCAL_RESEARCH_USE":
        owner = resource.get("owner_authorization")
        if not isinstance(owner, dict) or owner.get("decision") != "APPROVED_LOCAL_NONCOMMERCIAL":
            _fail("LFW local research use needs owner APPROVED_LOCAL_NONCOMMERCIAL")
        if owner.get("redistribution_allowed") is not False:
            _fail("LFW local research use never permits redistribution")
        if not resource.get("approved_scopes"):
            _fail("LFW local research use needs an effective approved scope")
        if not set(resource.get("approved_scopes")) <= set(owner.get("scopes") or []):
            _fail("LFW effective scopes exceed the owner grant")
    else:
        _fail(f"LFW resource has unsupported state {state}")
    if resource.get("third_party_rights_status") == "VERIFIED_LICENSE":
        _fail("LFW third-party rights cannot be VERIFIED_LICENSE; no image license exists")

    raw_artifacts = resource.get("artifacts")
    if not isinstance(raw_artifacts, list):
        _fail("LFW artifacts must be an array")
    artifacts = [
        _mapping(value, f"LFW artifacts[{index}]")
        for index, value in enumerate(raw_artifacts)
    ]
    archive_artifact = _find_one(
        artifacts,
        lambda value: value.get("role") == "DATA"
        and Path(str(value.get("path", ""))).name == "lfw-funneled.tgz",
        "registered LFW archive",
    )
    pairs_artifact = _find_one(
        artifacts,
        lambda value: value.get("role") == "PROTOCOL"
        and Path(str(value.get("path", ""))).name == ROOT_PAIRS_NAME,
        "registered root pairs file",
    )
    image_manifest_artifact = _find_one(
        artifacts,
        lambda value: value.get("role") == "MANIFEST"
        and Path(str(value.get("path", ""))).name == IMAGE_MANIFEST_NAME,
        "registered LFW image manifest",
    )
    archive_path, archive_sha256, archive_size = _artifact_identity(
        archive_artifact, "LFW archive artifact"
    )
    pairs_path, pairs_sha256, pairs_size = _artifact_identity(
        pairs_artifact, "LFW pairs artifact"
    )
    image_manifest_path, image_manifest_sha256, image_manifest_size = _artifact_identity(
        image_manifest_artifact, "LFW image-manifest artifact"
    )

    raw_checks = resource.get("checks")
    if not isinstance(raw_checks, list):
        _fail("LFW checks must be an array")
    tree_check = _find_one(
        [_mapping(value, f"LFW checks[{index}]") for index, value in enumerate(raw_checks)],
        lambda value: value.get("type") == "tree_manifest",
        "LFW tree-manifest check",
    )
    extracted_value = tree_check.get("root")
    if not isinstance(extracted_value, str) or not Path(extracted_value).is_absolute():
        _fail("LFW tree-manifest root must be absolute")
    extracted_root = Path(extracted_value)
    if extracted_root.name != ARCHIVE_ROOT:
        _fail(f"extracted root must end in {ARCHIVE_ROOT}")
    if tree_check.get("manifest_path") != str(image_manifest_path):
        _fail("tree-manifest check and image-manifest artifact differ")
    if tree_check.get("expected_count") != EXPECTED_IMAGE_COUNT:
        _fail("tree-manifest image count differs from the LFW evidence contract")
    tree_sha256 = tree_check.get("sha256")
    if not isinstance(tree_sha256, str) or not SHA256_RE.fullmatch(tree_sha256):
        _fail("tree-manifest SHA-256 is invalid")

    source = _mapping(resource.get("source"), "LFW source")
    evidence_artifacts = source.get("evidence_artifacts")
    if not isinstance(evidence_artifacts, list) or not all(
        isinstance(path, str) for path in evidence_artifacts
    ):
        _fail("LFW source evidence_artifacts must be a string array")

    return {
        "resource": resource,
        "artifacts": artifacts,
        "evidence_artifacts": evidence_artifacts,
        "archive_path": archive_path,
        "archive_sha256": archive_sha256,
        "archive_size": archive_size,
        "pairs_path": pairs_path,
        "pairs_sha256": pairs_sha256,
        "pairs_size": pairs_size,
        "image_manifest_path": image_manifest_path,
        "image_manifest_sha256": image_manifest_sha256,
        "image_manifest_size": image_manifest_size,
        "extracted_root": extracted_root,
        "tree_sha256": tree_sha256,
    }


def _load_image_manifest(path: Path) -> dict[str, tuple[int, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        _fail(f"cannot read registered image manifest: {error}")
    if len(lines) != EXPECTED_IMAGE_COUNT:
        _fail(
            f"image manifest count differs: {len(lines)} != {EXPECTED_IMAGE_COUNT}"
        )
    records: dict[str, tuple[int, str]] = {}
    previous_sort_key: bytes | None = None
    tree_digest = hashlib.sha256()
    for index, line in enumerate(lines, start=1):
        try:
            raw = json.loads(line, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, LFWProtocolValidationError) as error:
            _fail(f"image manifest line {index} is invalid JSON: {error}")
        record = _mapping(raw, f"image manifest line {index}")
        _strict_keys(
            record,
            f"image manifest line {index}",
            {"path", "sha256", "size_bytes"},
        )
        relative = _safe_relative_path(record["path"], f"image manifest line {index}.path")
        if not relative.endswith(".jpg"):
            _fail(f"image manifest path is not a JPEG: {relative}")
        sort_key = relative.encode("utf-8")
        if previous_sort_key is not None and sort_key <= previous_sort_key:
            _fail("image manifest paths must be unique and bytewise sorted")
        previous_sort_key = sort_key
        digest = record["sha256"]
        size = record["size_bytes"]
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            _fail(f"image manifest line {index}.sha256 is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or size < 1:
            _fail(f"image manifest line {index}.size_bytes is invalid")
        records[relative] = (size, digest)
        tree_digest.update(f"{digest}  {relative}\n".encode("utf-8"))
    return records | {"__tree_sha256__": (0, tree_digest.hexdigest())}


def _walk_extracted_tree(root: Path) -> tuple[set[str], set[str]]:
    if root.is_symlink() or not root.is_dir():
        _fail(f"extracted LFW root is not a real directory: {root}")
    files: set[str] = set()
    directories: set[str] = set()
    try:
        entries = root.rglob("*")
        for entry in entries:
            relative = entry.relative_to(root).as_posix()
            if entry.is_symlink():
                _fail(f"extracted LFW tree contains a symlink: {relative}")
            metadata = entry.stat()
            if stat.S_ISREG(metadata.st_mode):
                files.add(relative)
            elif stat.S_ISDIR(metadata.st_mode):
                directories.add(relative)
            else:
                _fail(f"extracted LFW tree contains a special file: {relative}")
    except OSError as error:
        _fail(f"cannot enumerate extracted LFW tree: {error}")
    return files, directories


def _archive_and_tree_bytes(
    context: dict[str, Any],
    image_records: dict[str, tuple[int, str]],
) -> dict[str, bytes]:
    archive_path = context["archive_path"]
    extracted_root = context["extracted_root"]
    if _sha256_path(archive_path) != context["archive_sha256"]:
        _fail("LFW archive SHA-256 differs from the registered identity")
    if archive_path.stat().st_size != context["archive_size"]:
        _fail("LFW archive size differs from the registered identity")

    extracted_files, extracted_directories = _walk_extracted_tree(extracted_root)
    protocol_names = {ROOT_PAIRS_NAME} | {
        f"pairs_{fold:02d}.txt" for fold in range(1, FOLD_COUNT + 1)
    }
    image_paths = set(image_records) - {"__tree_sha256__"}
    expected_files = image_paths | protocol_names
    if extracted_files != expected_files:
        _fail("extracted LFW file membership differs from images plus protocol files")
    expected_directories = {PurePosixPath(path).parts[0] for path in image_paths}
    if extracted_directories != expected_directories:
        _fail("extracted LFW directory membership differs from image identities")
    if len(extracted_directories) != EXPECTED_IDENTITY_DIRECTORY_COUNT:
        _fail("extracted LFW identity-directory count differs")

    archive_members: set[str] = set()
    archive_files: set[str] = set()
    archive_directories: set[str] = set()
    protocol_bytes: dict[str, bytes] = {}
    archive_image_count = 0
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive:
                normalized_name = member.name.rstrip("/")
                safe_name = _safe_relative_path(normalized_name, "archive member")
                if safe_name in archive_members:
                    _fail(f"archive contains duplicate member: {safe_name}")
                archive_members.add(safe_name)
                member_path = PurePosixPath(safe_name)
                if not member_path.parts or member_path.parts[0] != ARCHIVE_ROOT:
                    _fail(f"archive member is outside {ARCHIVE_ROOT}: {safe_name}")
                relative_parts = member_path.parts[1:]
                if not relative_parts:
                    if not member.isdir():
                        _fail("archive root member is not a directory")
                    continue
                relative = PurePosixPath(*relative_parts).as_posix()
                if member.isdir():
                    archive_directories.add(relative)
                    continue
                if not member.isfile():
                    _fail(f"archive contains a link or special member: {safe_name}")
                archive_files.add(relative)
                stream = archive.extractfile(member)
                if stream is None:
                    _fail(f"archive member cannot be read: {safe_name}")
                if relative in image_paths:
                    archive_image_count += 1
                    expected_size, expected_hash = image_records[relative]
                    actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
                    if member.size != expected_size or actual_hash != expected_hash:
                        _fail(f"archive image differs from registered manifest: {relative}")
                    extracted_path = extracted_root / relative
                    if extracted_path.stat().st_size != expected_size:
                        _fail(f"extracted image size differs: {relative}")
                    if _sha256_path(extracted_path) != expected_hash:
                        _fail(f"extracted image bytes differ: {relative}")
                elif relative in protocol_names:
                    data = stream.read()
                    if len(data) != member.size:
                        _fail(f"archive protocol member size differs: {relative}")
                    protocol_bytes[relative] = data
                    extracted_path = extracted_root / relative
                    try:
                        extracted_data = extracted_path.read_bytes()
                    except OSError as error:
                        _fail(f"cannot read extracted protocol file {relative}: {error}")
                    if extracted_data != data:
                        _fail(f"archive and extracted protocol bytes differ: {relative}")
                else:
                    _fail(f"archive contains an unregistered regular file: {relative}")
    except (OSError, tarfile.TarError) as error:
        _fail(f"cannot inspect registered LFW archive: {error}")

    if len(archive_members) != EXPECTED_ARCHIVE_MEMBER_COUNT:
        _fail(
            f"archive member count differs: {len(archive_members)} "
            f"!= {EXPECTED_ARCHIVE_MEMBER_COUNT}"
        )
    if archive_files != expected_files:
        _fail("archive file membership differs from the extracted tree")
    if archive_directories != expected_directories:
        _fail("archive directory membership differs from the extracted tree")
    if archive_image_count != EXPECTED_IMAGE_COUNT:
        _fail("archive image count differs")
    if set(protocol_bytes) != protocol_names:
        _fail("archive protocol-file membership differs")
    return protocol_bytes


def _parse_index(value: str, location: str) -> int:
    if not value.isdigit():
        _fail(f"{location} must be a positive decimal image index")
    index = int(value)
    if index < 1:
        _fail(f"{location} must be positive")
    return index


def _sample_path(name: str, index_value: str, location: str) -> str:
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        _fail(f"{location} has an unsafe identity name: {name!r}")
    index = _parse_index(index_value, f"{location} image index")
    return f"{name}/{name}_{index:04d}.jpg"


def _canonical_line(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def _parse_pairs(
    context: dict[str, Any],
    protocol_bytes: dict[str, bytes],
    image_records: dict[str, tuple[int, str]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    root_pairs_path = context["pairs_path"]
    if _sha256_path(root_pairs_path) != context["pairs_sha256"]:
        _fail("root pairs SHA-256 differs from the registered identity")
    if root_pairs_path.stat().st_size != context["pairs_size"]:
        _fail("root pairs size differs from the registered identity")
    try:
        root_pairs = root_pairs_path.read_bytes()
    except OSError as error:
        _fail(f"cannot read root pairs file: {error}")
    if root_pairs != protocol_bytes[ROOT_PAIRS_NAME]:
        _fail("root, archive, and extracted pairs.txt bytes are not identical")
    try:
        lines = root_pairs.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        _fail(f"pairs.txt is not UTF-8: {error}")
    if len(lines) != EXPECTED_PAIR_COUNT + 1:
        _fail(f"pairs.txt line count differs: {len(lines)}")
    if lines[0] != f"{FOLD_COUNT}\t{PAIRS_PER_CLASS_PER_FOLD}":
        _fail(f"pairs.txt header differs: {lines[0]!r}")

    rows = lines[1:]
    image_paths = set(image_records) - {"__tree_sha256__"}
    pair_records: list[dict[str, Any]] = []
    expected_folds: list[list[tuple[str, str, str]]] = []
    missing_paths: set[str] = set()
    unique_samples: set[str] = set()
    unique_subjects: set[str] = set()

    for fold_index in range(FOLD_COUNT):
        fold_number = fold_index + 1
        block_start = fold_index * PAIRS_PER_CLASS_PER_FOLD * 2
        block = rows[block_start : block_start + PAIRS_PER_CLASS_PER_FOLD * 2]
        fold_pairs: list[tuple[str, str, str]] = []
        for within_fold, row in enumerate(block):
            mated = within_fold < PAIRS_PER_CLASS_PER_FOLD
            class_index = (
                within_fold + 1
                if mated
                else within_fold - PAIRS_PER_CLASS_PER_FOLD + 1
            )
            source_order = block_start + within_fold + 1
            source_line = source_order + 1
            fields = row.split("\t")
            if mated:
                if len(fields) != 3:
                    _fail(f"pairs.txt line {source_line} is not a three-field mated row")
                name, left_index, right_index = fields
                left_path = _sample_path(name, left_index, f"pairs.txt line {source_line}")
                right_path = _sample_path(name, right_index, f"pairs.txt line {source_line}")
                label = "MATED"
                if left_path == right_path:
                    _fail(f"pairs.txt line {source_line} repeats one sample")
            else:
                if len(fields) != 4:
                    _fail(
                        f"pairs.txt line {source_line} is not a four-field non-mated row"
                    )
                left_name, left_index, right_name, right_index = fields
                if left_name == right_name:
                    _fail(f"pairs.txt line {source_line} labels one identity non-mated")
                left_path = _sample_path(
                    left_name, left_index, f"pairs.txt line {source_line} left"
                )
                right_path = _sample_path(
                    right_name, right_index, f"pairs.txt line {source_line} right"
                )
                label = "NON_MATED"
            for path in (left_path, right_path):
                if path not in image_paths:
                    missing_paths.add(path)
                unique_samples.add(path)
                unique_subjects.add(path.split("/", 1)[0])
            if missing_paths:
                continue
            left_size, left_sha256 = image_records[left_path]
            right_size, right_sha256 = image_records[right_path]
            pair_id = (
                f"lfw-local-fold-{fold_number:02d}-{label.lower().replace('_', '-')}-"
                f"{class_index:04d}"
            )
            pair_records.append(
                {
                    "class_index": class_index,
                    "fold": fold_number,
                    "label": label,
                    "left_path": left_path,
                    "left_sha256": left_sha256,
                    "left_size_bytes": left_size,
                    "pair_id": pair_id,
                    "record_type": "PAIR",
                    "right_path": right_path,
                    "right_sha256": right_sha256,
                    "right_size_bytes": right_size,
                    "source_line": source_line,
                    "source_order": source_order,
                }
            )
            fold_pairs.append((left_path, right_path, label))
        expected_folds.append(fold_pairs)

    if missing_paths:
        _fail(
            f"pairs.txt references {len(missing_paths)} paths absent from the image manifest"
        )
    if len(pair_records) != EXPECTED_PAIR_COUNT:
        _fail(f"parsed pair count differs: {len(pair_records)}")
    if len({record["pair_id"] for record in pair_records}) != EXPECTED_PAIR_COUNT:
        _fail("derived pair IDs are not unique")
    mated_count = sum(record["label"] == "MATED" for record in pair_records)
    nonmated_count = sum(record["label"] == "NON_MATED" for record in pair_records)
    if mated_count != EXPECTED_MATED_PAIRS or nonmated_count != EXPECTED_NONMATED_PAIRS:
        _fail("mated/non-mated pair counts differ")
    if len(unique_samples) != EXPECTED_UNIQUE_PROTOCOL_SAMPLES:
        _fail("unique protocol-sample count differs")
    if len(unique_subjects) != EXPECTED_UNIQUE_PROTOCOL_SUBJECTS:
        _fail("unique protocol-subject count differs")

    expanded_matches = 0
    for fold_index, expected in enumerate(expected_folds):
        fold_number = fold_index + 1
        name = f"pairs_{fold_number:02d}.txt"
        try:
            expanded_lines = protocol_bytes[name].decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            _fail(f"{name} is not UTF-8: {error}")
        if len(expanded_lines) != PAIRS_PER_CLASS_PER_FOLD * 5:
            _fail(f"{name} line count differs: {len(expanded_lines)}")
        actual: list[tuple[str, str, str]] = []
        for offset in range(0, len(expanded_lines), 5):
            left_mated, right_mated, left_nonmated, right_nonmated, separator = (
                expanded_lines[offset : offset + 5]
            )
            if separator != "":
                _fail(f"{name} block {offset // 5 + 1} lacks its blank separator")
            for path in (
                left_mated,
                right_mated,
                left_nonmated,
                right_nonmated,
            ):
                safe_path = _safe_relative_path(path, f"{name} block {offset // 5 + 1}")
                if safe_path not in image_paths:
                    _fail(f"{name} references an image absent from the manifest: {safe_path}")
            actual.append((left_mated, right_mated, "MATED"))
            actual.append((left_nonmated, right_nonmated, "NON_MATED"))
        expected_interleaved: list[tuple[str, str, str]] = []
        for class_index in range(PAIRS_PER_CLASS_PER_FOLD):
            expected_interleaved.append(expected[class_index])
            expected_interleaved.append(
                expected[PAIRS_PER_CLASS_PER_FOLD + class_index]
            )
        if actual != expected_interleaved:
            _fail(f"{name} does not semantically match fold {fold_number} in pairs.txt")
        expanded_matches += 1

    counts = {
        "folds": FOLD_COUNT,
        "mated_pairs": mated_count,
        "nonmated_pairs": nonmated_count,
        "pairs": len(pair_records),
        "unique_protocol_samples": len(unique_samples),
        "unique_protocol_subjects": len(unique_subjects),
        "missing_manifest_paths": len(missing_paths),
        "expanded_fold_matches": expanded_matches,
    }
    return pair_records, counts


def _build_manifest(context: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    if _sha256_path(context["image_manifest_path"]) != context["image_manifest_sha256"]:
        _fail("image-manifest SHA-256 differs from the registered identity")
    if context["image_manifest_path"].stat().st_size != context["image_manifest_size"]:
        _fail("image-manifest size differs from the registered identity")
    image_records = _load_image_manifest(context["image_manifest_path"])
    observed_tree_sha256 = image_records["__tree_sha256__"][1]
    if observed_tree_sha256 != context["tree_sha256"]:
        _fail("image-manifest tree digest differs from the registered check")
    protocol_bytes = _archive_and_tree_bytes(context, image_records)
    pair_records, counts = _parse_pairs(context, protocol_bytes, image_records)

    header = {
        "archive_member_count": EXPECTED_ARCHIVE_MEMBER_COUNT,
        "archive_sha256": context["archive_sha256"],
        "folds": counts["folds"],
        "image_count": EXPECTED_IMAGE_COUNT,
        "image_manifest_sha256": context["image_manifest_sha256"],
        "image_tree_sha256": context["tree_sha256"],
        "limitations": list(LIMITATIONS),
        "mated_pairs": counts["mated_pairs"],
        "nonmated_pairs": counts["nonmated_pairs"],
        "pair_count": counts["pairs"],
        "pairs_per_class_per_fold": PAIRS_PER_CLASS_PER_FOLD,
        "protocol_file_count": EXPECTED_PROTOCOL_FILE_COUNT,
        "protocol_id": PROTOCOL_ID,
        "record_type": "PROTOCOL",
        "resource_id": RESOURCE_ID,
        "schema_version": "1.0.0",
        "source_pairs_sha256": context["pairs_sha256"],
        "status": "LOCAL_BYTE_AND_PAIR_FORMAT_RECONCILIATION",
        "unique_protocol_samples": counts["unique_protocol_samples"],
        "unique_protocol_subjects": counts["unique_protocol_subjects"],
    }
    manifest = (_canonical_line(header) + "".join(_canonical_line(record) for record in pair_records)).encode(
        "utf-8"
    )
    summary: dict[str, Any] = {
        "resource_id": RESOURCE_ID,
        "archive_members": EXPECTED_ARCHIVE_MEMBER_COUNT,
        "archive_images": EXPECTED_IMAGE_COUNT,
        "protocol_files": EXPECTED_PROTOCOL_FILE_COUNT,
        **counts,
        "protocol_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "protocol_manifest_size_bytes": len(manifest),
    }
    return manifest, summary


def _publish_new_file(output: Path, data: bytes) -> None:
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"refusing to overwrite protocol manifest: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.partial")
    if partial.exists() or partial.is_symlink():
        raise FileExistsError(f"refusing to overwrite partial protocol manifest: {partial}")
    published = False
    try:
        with partial.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(partial, output)
        published = True
        partial.unlink()
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if not published and partial.exists():
            partial.unlink()


def capture_protocol_manifest(registry_path: Path, output: Path) -> dict[str, Any]:
    context = _load_context(registry_path.resolve(strict=True))
    manifest, summary = _build_manifest(context)
    _publish_new_file(output, manifest)
    return summary


def verify_protocol_manifest(registry_path: Path, manifest_path: Path) -> dict[str, Any]:
    registry_path = registry_path.resolve(strict=True)
    manifest_path = manifest_path.resolve(strict=True)
    context = _load_context(registry_path)
    manifest_artifact = _find_one(
        context["artifacts"],
        lambda value: value.get("role") == "PROTOCOL"
        and value.get("path") == str(manifest_path),
        "registered generated LFW protocol manifest",
    )
    artifact_path, artifact_sha256, artifact_size = _artifact_identity(
        manifest_artifact, "generated LFW protocol-manifest artifact"
    )
    if artifact_path.name != PROTOCOL_MANIFEST_NAME:
        _fail(f"generated protocol manifest must be named {PROTOCOL_MANIFEST_NAME}")
    if str(artifact_path) not in context["evidence_artifacts"]:
        _fail("generated protocol manifest is not a registered LFW evidence artifact")
    expected, summary = _build_manifest(context)
    try:
        actual = manifest_path.read_bytes()
    except OSError as error:
        _fail(f"cannot read generated protocol manifest: {error}")
    if actual != expected:
        _fail("generated protocol manifest differs from current real source bytes")
    if len(actual) != artifact_size or len(actual) != summary["protocol_manifest_size_bytes"]:
        _fail("generated protocol-manifest size differs from its registered identity")
    digest = hashlib.sha256(actual).hexdigest()
    if digest != artifact_sha256 or digest != summary["protocol_manifest_sha256"]:
        _fail("generated protocol-manifest SHA-256 differs from its registered identity")
    return summary


def _summary_text(action: str, summary: dict[str, Any]) -> str:
    return (
        f"LFW_PROTOCOL_{action} resource={summary['resource_id']} "
        f"archive_members={summary['archive_members']} "
        f"images={summary['archive_images']} protocol_files={summary['protocol_files']} "
        f"folds={summary['folds']} pairs={summary['pairs']} "
        f"mated={summary['mated_pairs']} nonmated={summary['nonmated_pairs']} "
        f"samples={summary['unique_protocol_samples']} "
        f"subjects={summary['unique_protocol_subjects']} "
        f"manifest_sha256={summary['protocol_manifest_sha256']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("capture", "verify"):
        subparser = subparsers.add_parser(action)
        subparser.add_argument("--registry", required=True, type=Path)
        subparser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.action == "capture":
            summary = capture_protocol_manifest(args.registry, args.manifest)
            print(_summary_text("CAPTURED", summary))
        else:
            summary = verify_protocol_manifest(args.registry, args.manifest)
            print(_summary_text("VALID", summary))
    except (FileExistsError, LFWProtocolValidationError, OSError) as error:
        print(f"LFW_PROTOCOL_INVALID {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
