"""Crash-safe publication for scientific result directories.

The prepared receipt is the durable boundary between immutable filesystem
artifacts and their SQLite record.  A receipt is published only with the full
directory, never added to an already-visible final directory.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Any, Iterable

from ..domain.primitives import CanonicalJson


PREPARED_OUTPUT_FILENAME = ".qualsight-prepared-output.json"
PREPARED_OUTPUT_SCHEMA_VERSION = "1.0.0"
STAGING_IDENTITY_FILENAME = ".qualsight-staging-identity.json"
STAGING_IDENTITY_SCHEMA_VERSION = "1.0.0"
AT_FDCWD = -100
RENAME_NOREPLACE = 1


class PreparedOutputError(RuntimeError):
    """Raised when a staged or promoted scientific result is not trustworthy."""


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _regular_file_identity(path: Path, *, sync: bool) -> tuple[os.stat_result, str]:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise PreparedOutputError("prepared artifact is not a regular file")
        if sync:
            os.fsync(descriptor)
        return observed, _sha256_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _read_regular_text(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PreparedOutputError("prepared scientific result receipt is invalid")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(descriptor)


def _validate_staging_identity(
    identity: Any,
    *,
    stage: Path,
    target_name: str,
    operation_kind: str,
    result_id: str,
    project_id: str,
    request_sha256: str,
) -> dict[str, str]:
    expected = {
        "schema_version": STAGING_IDENTITY_SCHEMA_VERSION,
        "target_name": target_name,
        "operation_kind": operation_kind,
        "result_id": result_id,
        "project_id": project_id,
        "request_sha256": request_sha256,
    }
    if not isinstance(identity, dict) or identity != expected:
        raise PreparedOutputError("scientific result staging identity differs")
    if (
        any(not isinstance(value, str) for value in expected.values())
        or not target_name
        or Path(target_name).name != target_name
        or not operation_kind
        or not result_id
        or not project_id
        or len(request_sha256) != 64
        or any(character not in "0123456789abcdef" for character in request_sha256)
        or not stage.name.startswith(f".{target_name}.")
        or not stage.name.endswith(".partial")
    ):
        raise PreparedOutputError("scientific result staging identity is invalid")
    return expected


def load_staging_identity(
    stage: Path,
    *,
    target_name: str,
    operation_kind: str,
    result_id: str,
    project_id: str,
    request_sha256: str,
) -> dict[str, str]:
    """Validate the durable identity of one private result stage."""

    try:
        observed = stage.lstat()
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            raise PreparedOutputError("scientific result stage is invalid")
        marker = stage / STAGING_IDENTITY_FILENAME
        text = _read_regular_text(marker)
        identity = json.loads(text)
        if text != CanonicalJson.from_value(identity).text + "\n":
            raise PreparedOutputError("scientific result staging identity is not canonical")
        return _validate_staging_identity(
            identity,
            stage=stage,
            target_name=target_name,
            operation_kind=operation_kind,
            result_id=result_id,
            project_id=project_id,
            request_sha256=request_sha256,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        if isinstance(error, PreparedOutputError):
            raise
        raise PreparedOutputError(
            "scientific result staging identity failed integrity verification"
        ) from error


def _publish_directory_no_clobber(stage: Path, target: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is None:
        raise PreparedOutputError(
            "atomic no-clobber result publication is unavailable on this workstation"
        )
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
        AT_FDCWD,
        os.fsencode(stage),
        AT_FDCWD,
        os.fsencode(target),
        RENAME_NOREPLACE,
    ) == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number), target)
    raise OSError(error_number, os.strerror(error_number), target)


def create_staging_directory(
    target: Path,
    *,
    operation_kind: str,
    result_id: str,
    project_id: str,
    request_sha256: str,
) -> Path:
    """Create a private sibling directory for one not-yet-visible result."""

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink() or target.exists():
        raise PreparedOutputError("scientific result output already exists")
    for stale in target.parent.glob(f".{target.name}.*.partial"):
        load_partial_output_identity(
            stale,
            target_name=target.name,
            operation_kind=operation_kind,
            result_id=result_id,
            project_id=project_id,
            request_sha256=request_sha256,
        )
        shutil.rmtree(stale)
        _sync_directory(target.parent)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".partial", dir=target.parent)
    )
    try:
        identity = {
            "schema_version": STAGING_IDENTITY_SCHEMA_VERSION,
            "target_name": target.name,
            "operation_kind": operation_kind,
            "result_id": result_id,
            "project_id": project_id,
            "request_sha256": request_sha256,
        }
        _validate_staging_identity(
            identity,
            stage=stage,
            target_name=target.name,
            operation_kind=operation_kind,
            result_id=result_id,
            project_id=project_id,
            request_sha256=request_sha256,
        )
        marker = stage / STAGING_IDENTITY_FILENAME
        descriptor = os.open(
            marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            encoded = (CanonicalJson.from_value(identity).text + "\n").encode("utf-8")
            written = 0
            while written < len(encoded):
                count = os.write(descriptor, encoded[written:])
                if count <= 0:
                    raise OSError("scientific result staging identity write made no progress")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _sync_directory(stage)
        _sync_directory(target.parent)
        return stage
    except BaseException:
        discard_staging_directory(stage)
        raise


def discard_staging_directory(stage: Path) -> None:
    """Remove only an unpublished sibling stage created by this module."""

    if stage.exists() and stage.name.startswith(".") and stage.name.endswith(".partial"):
        parent = stage.parent
        shutil.rmtree(stage)
        _sync_directory(parent)


def _artifact_record(stage: Path, name: str) -> dict[str, Any]:
    if not name or Path(name).name != name:
        raise PreparedOutputError("prepared artifact name is invalid")
    path = stage / name
    observed, digest = _regular_file_identity(path, sync=True)
    return {"name": name, "sha256": digest, "size_bytes": observed.st_size}


def prepare_and_publish(
    stage: Path,
    target: Path,
    *,
    operation_kind: str,
    result_id: str,
    project_id: str,
    request_sha256: str,
    artifact_names: Iterable[str],
    record: dict[str, Any],
) -> dict[str, Any]:
    """Fsync a canonical receipt and atomically expose its complete directory."""

    if stage.parent != target.parent or stage.is_symlink() or not stage.is_dir():
        raise PreparedOutputError("scientific result stage is invalid")
    load_staging_identity(
        stage,
        target_name=target.name,
        operation_kind=operation_kind,
        result_id=result_id,
        project_id=project_id,
        request_sha256=request_sha256,
    )
    expected_names = sorted(artifact_names)
    if len(expected_names) != len(set(expected_names)) or {
        path.name for path in stage.iterdir()
    } != {STAGING_IDENTITY_FILENAME, *expected_names}:
        raise PreparedOutputError("scientific result stage contains unbound members")
    artifacts = [_artifact_record(stage, name) for name in expected_names]
    receipt = {
        "schema_version": PREPARED_OUTPUT_SCHEMA_VERSION,
        "operation_kind": operation_kind,
        "result_id": result_id,
        "project_id": project_id,
        "request_sha256": request_sha256,
        "artifacts": artifacts,
        "record": record,
    }
    text = CanonicalJson.from_value(receipt).text + "\n"
    receipt_path = stage / PREPARED_OUTPUT_FILENAME
    descriptor = os.open(
        receipt_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        encoded = text.encode("utf-8")
        written = 0
        while written < len(encoded):
            count = os.write(descriptor, encoded[written:])
            if count <= 0:
                raise OSError("prepared scientific result receipt write made no progress")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _sync_directory(stage)
    (stage / STAGING_IDENTITY_FILENAME).unlink()
    _sync_directory(stage)
    _publish_directory_no_clobber(stage, target)
    _sync_directory(target.parent)
    return receipt


def load_prepared_output(
    target: Path,
    *,
    operation_kind: str,
    result_id: str,
    project_id: str,
    request_sha256: str,
    artifact_names: Iterable[str],
) -> dict[str, Any]:
    """Validate a promoted prepared receipt and every file it binds."""

    try:
        if target.is_symlink() or not target.is_dir():
            raise PreparedOutputError("prepared scientific result is unavailable")
        receipt_path = target / PREPARED_OUTPUT_FILENAME
        text = _read_regular_text(receipt_path)
        receipt = json.loads(text)
        if text != CanonicalJson.from_value(receipt).text + "\n":
            raise PreparedOutputError("prepared scientific result receipt is not canonical")
        if set(receipt) != {
            "schema_version",
            "operation_kind",
            "result_id",
            "project_id",
            "request_sha256",
            "artifacts",
            "record",
        }:
            raise PreparedOutputError("prepared scientific result receipt contract differs")
        if (
            receipt["schema_version"] != PREPARED_OUTPUT_SCHEMA_VERSION
            or receipt["operation_kind"] != operation_kind
            or receipt["result_id"] != result_id
            or receipt["project_id"] != project_id
            or receipt["request_sha256"] != request_sha256
            or not isinstance(receipt["record"], dict)
        ):
            raise PreparedOutputError("prepared scientific result identity differs")
        expected_names = sorted(artifact_names)
        if len(expected_names) != len(set(expected_names)) or {
            path.name for path in target.iterdir()
        } != {PREPARED_OUTPUT_FILENAME, *expected_names}:
            raise PreparedOutputError(
                "prepared scientific result contains unbound members"
            )
        observed_artifacts = receipt["artifacts"]
        if not isinstance(observed_artifacts, list) or [
            item.get("name") for item in observed_artifacts if isinstance(item, dict)
        ] != expected_names:
            raise PreparedOutputError("prepared scientific result artifact set differs")
        for item in observed_artifacts:
            if set(item) != {"name", "sha256", "size_bytes"}:
                raise PreparedOutputError("prepared scientific result artifact record differs")
            name = item["name"]
            if not isinstance(name, str) or Path(name).name != name:
                raise PreparedOutputError("prepared scientific result artifact name is invalid")
            path = target / name
            observed, digest = _regular_file_identity(path, sync=False)
            if (
                observed.st_size != item["size_bytes"]
                or digest != item["sha256"]
            ):
                raise PreparedOutputError("prepared scientific result artifact differs")
        return receipt
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        if isinstance(error, PreparedOutputError):
            raise
        raise PreparedOutputError(
            "prepared scientific result failed integrity verification"
        ) from error


def load_partial_output_identity(
    stage: Path,
    *,
    target_name: str,
    operation_kind: str,
    result_id: str,
    project_id: str,
    request_sha256: str,
) -> dict[str, str]:
    """Validate an interrupted stage by its marker or complete prepared receipt."""

    marker = stage / STAGING_IDENTITY_FILENAME
    if marker.is_symlink() or marker.exists():
        return load_staging_identity(
            stage,
            target_name=target_name,
            operation_kind=operation_kind,
            result_id=result_id,
            project_id=project_id,
            request_sha256=request_sha256,
        )
    try:
        receipt_path = stage / PREPARED_OUTPUT_FILENAME
        text = _read_regular_text(receipt_path)
        receipt = json.loads(text)
        if text != CanonicalJson.from_value(receipt).text + "\n":
            raise PreparedOutputError(
                "interrupted scientific result receipt is not canonical"
            )
        artifacts = receipt.get("artifacts")
        if not isinstance(artifacts, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str)
            for item in artifacts
        ):
            raise PreparedOutputError(
                "interrupted scientific result artifact contract differs"
            )
        load_prepared_output(
            stage,
            operation_kind=operation_kind,
            result_id=result_id,
            project_id=project_id,
            request_sha256=request_sha256,
            artifact_names=[item["name"] for item in artifacts],
        )
        return _validate_staging_identity(
            {
                "schema_version": STAGING_IDENTITY_SCHEMA_VERSION,
                "target_name": target_name,
                "operation_kind": operation_kind,
                "result_id": result_id,
                "project_id": project_id,
                "request_sha256": request_sha256,
            },
            stage=stage,
            target_name=target_name,
            operation_kind=operation_kind,
            result_id=result_id,
            project_id=project_id,
            request_sha256=request_sha256,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        if isinstance(error, PreparedOutputError):
            raise
        raise PreparedOutputError(
            "interrupted scientific result identity failed integrity verification"
        ) from error


def discover_partial_output_identity(stage: Path) -> dict[str, str]:
    """Discover and strictly validate the identity of one interrupted stage."""

    marker = stage / STAGING_IDENTITY_FILENAME
    try:
        if marker.is_symlink() or marker.exists():
            text = _read_regular_text(marker)
            identity = json.loads(text)
            if text != CanonicalJson.from_value(identity).text + "\n":
                raise PreparedOutputError(
                    "scientific result staging identity is not canonical"
                )
            if not isinstance(identity, dict) or set(identity) != {
                "schema_version",
                "target_name",
                "operation_kind",
                "result_id",
                "project_id",
                "request_sha256",
            }:
                raise PreparedOutputError(
                    "scientific result staging identity contract differs"
                )
            return load_partial_output_identity(
                stage,
                target_name=identity["target_name"],
                operation_kind=identity["operation_kind"],
                result_id=identity["result_id"],
                project_id=identity["project_id"],
                request_sha256=identity["request_sha256"],
            )
        receipt_text = _read_regular_text(stage / PREPARED_OUTPUT_FILENAME)
        receipt = json.loads(receipt_text)
        if not isinstance(receipt, dict) or set(receipt) != {
            "schema_version",
            "operation_kind",
            "result_id",
            "project_id",
            "request_sha256",
            "artifacts",
            "record",
        }:
            raise PreparedOutputError(
                "interrupted scientific result receipt contract differs"
            )
        stem = stage.name
        if not stem.startswith(".") or not stem.endswith(".partial"):
            raise PreparedOutputError("interrupted scientific result name is invalid")
        target_and_nonce = stem[1 : -len(".partial")]
        target_name, separator, nonce = target_and_nonce.rpartition(".")
        if not separator or not nonce or "." in nonce:
            raise PreparedOutputError("interrupted scientific result name is invalid")
        return load_partial_output_identity(
            stage,
            target_name=target_name,
            operation_kind=receipt["operation_kind"],
            result_id=receipt["result_id"],
            project_id=receipt["project_id"],
            request_sha256=receipt["request_sha256"],
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        if isinstance(error, PreparedOutputError):
            raise
        raise PreparedOutputError(
            "interrupted scientific result identity failed integrity verification"
        ) from error
