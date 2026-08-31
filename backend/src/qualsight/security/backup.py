"""Offline, hash-verified backup and no-clobber restore for the local beta store."""

from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import TYPE_CHECKING, Any

from .local_storage import PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE
from .process_lease import DataDirectoryLease, LEASE_FILENAME

if TYPE_CHECKING:
    from ..api.store import RunStore


BACKUP_SCHEMA = "qualsight-local-backup-v1"
DATABASE_SNAPSHOT_NAME = "database.snapshot.sqlite3"
MANIFEST_NAME = "manifest.json"
AT_FDCWD = -100
RENAME_NOREPLACE = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_directory(path: Path) -> None:
    path.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=False)
    os.chmod(path, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)


def _copy_private(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"backup source is not a regular non-symlink file: {source}")
    destination.parent.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    os.chmod(destination.parent, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    os.chmod(destination, PRIVATE_FILE_MODE, follow_symlinks=False)


def _managed_files(data_dir: Path, database_path: Path) -> list[Path]:
    excluded = {
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
        data_dir / LEASE_FILENAME,
    }
    managed: list[Path] = []
    for candidate in sorted(data_dir.rglob("*")):
        if candidate.is_symlink():
            raise ValueError(f"managed backup scope contains a symlink: {candidate}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ValueError(f"managed backup scope contains a non-regular file: {candidate}")
        if candidate not in excluded:
            managed.append(candidate)
    return managed


def _read_manifest(backup_dir: Path) -> dict[str, Any]:
    root = backup_dir.absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"backup is not a real directory: {root}")
    manifest_path = root / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError(f"backup manifest is not a regular non-symlink file: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"backup manifest is unreadable: {error}") from error
    if manifest.get("schema") != BACKUP_SCHEMA:
        raise ValueError(f"unsupported backup schema: {manifest.get('schema')!r}")
    return manifest


def _verified_managed_file(files_root: Path, relative: Path) -> Path:
    """Resolve one manifest file while rejecting symlinks in its entire path."""

    if not relative.parts or relative == Path("."):
        raise ValueError("backup managed file path is empty")
    current = files_root
    if current.is_symlink() or not current.is_dir():
        raise ValueError("backup managed files root is missing or symlinked")
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"backup managed file has a symlinked ancestor: {relative}")
        if not current.is_dir():
            raise ValueError(f"backup managed file ancestor is not a directory: {relative}")
    copied = current / relative.parts[-1]
    if copied.is_symlink() or not copied.is_file():
        raise ValueError(f"backup managed file is missing or unsafe: {relative}")
    return copied


def _publish_directory_no_clobber(stage: Path, target: Path) -> None:
    """Atomically publish a staged directory without replacing any target."""

    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is None:
        raise OSError(
            errno.ENOSYS,
            "atomic no-clobber directory publication requires renameat2",
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


def create_backup(
    store: RunStore,
    data_dir: Path,
    destination: Path,
    *,
    lease: DataDirectoryLease | None = None,
) -> dict[str, Any]:
    """Copy the offline store while the caller owns its process lease."""

    source_root = data_dir.absolute()
    if source_root.is_symlink() or not source_root.is_dir():
        raise ValueError(f"data directory is not a real directory: {source_root}")
    expected_lease_path = source_root / LEASE_FILENAME
    if (
        lease is None
        or lease.path != expected_lease_path
        or lease._closed
    ):
        raise ValueError(
            "backup creation requires the caller's active exclusive data-directory lease"
        )
    database_path = store.database_path.absolute()
    try:
        database_path.relative_to(source_root)
    except ValueError as error:
        raise ValueError("RunStore database is outside the data directory") from error

    target = destination.absolute()
    try:
        target.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError("backup destination must be outside the live data directory")
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)

    stage = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".partial", dir=target.parent)
    )
    os.chmod(stage, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
    published = False
    try:
        database = store.snapshot_database(stage / DATABASE_SNAPSHOT_NAME)
        files_root = stage / "files"
        _private_directory(files_root)
        files: list[dict[str, Any]] = []
        for source in _managed_files(source_root, database_path):
            relative = source.relative_to(source_root)
            copied = files_root / relative
            _copy_private(source, copied)
            files.append(
                {
                    "path": relative.as_posix(),
                    "sha256": _sha256(copied),
                    "size_bytes": copied.stat().st_size,
                }
            )
        manifest = {
            "schema": BACKUP_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_data_dir": str(source_root),
            "database": {
                "path": DATABASE_SNAPSHOT_NAME,
                "sha256": database["sha256"],
                "size_bytes": database["size_bytes"],
            },
            "files": files,
        }
        manifest_path = stage / MANIFEST_NAME
        payload = (
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            manifest_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC,
            PRIVATE_FILE_MODE,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
        _publish_directory_no_clobber(stage, target)
        published = True
    finally:
        if not published:
            shutil.rmtree(stage, ignore_errors=True)

    verified = verify_backup(target)
    return {
        **verified,
        "path": str(target),
        "manifest_sha256": _sha256(target / MANIFEST_NAME),
    }


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    """Verify manifest identities, every copied file, and SQLite integrity."""

    root = backup_dir.absolute()
    manifest = _read_manifest(root)
    database_record = manifest.get("database")
    if not isinstance(database_record, dict) or database_record.get("path") != DATABASE_SNAPSHOT_NAME:
        raise ValueError("backup database manifest entry is invalid")
    database = root / DATABASE_SNAPSHOT_NAME
    if database.is_symlink() or not database.is_file():
        raise ValueError("backup database snapshot is not a regular non-symlink file")
    if database.stat().st_size != database_record.get("size_bytes") or _sha256(database) != database_record.get("sha256"):
        raise ValueError("backup database snapshot does not match its manifest evidence")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise sqlite3.DatabaseError("backup database integrity check failed")
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()

    records = manifest.get("files")
    if not isinstance(records, list):
        raise ValueError("backup file manifest is invalid")
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ValueError("backup file manifest entry is invalid")
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() in seen:
            raise ValueError(f"backup file path is unsafe or repeated: {relative}")
        seen.add(relative.as_posix())
        copied = _verified_managed_file(root / "files", relative)
        if copied.stat().st_size != record.get("size_bytes") or _sha256(copied) != record.get("sha256"):
            raise ValueError(f"backup managed file does not match its manifest evidence: {relative}")
    return {
        "verified": True,
        "file_count": len(records),
        "schema_version": schema_version,
        "source_data_dir": manifest["source_data_dir"],
    }


def restore_backup(backup_dir: Path, destination: Path) -> dict[str, Any]:
    """Restore a verified backup only to its absent original data-directory path."""

    verified = verify_backup(backup_dir)
    root = backup_dir.absolute()
    manifest = _read_manifest(root)
    target = destination.absolute()
    if str(target) != manifest.get("source_data_dir"):
        raise ValueError("backup can only be restored to its recorded source data directory")
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".restore", dir=target.parent)
    )
    os.chmod(stage, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)
    published = False
    try:
        _copy_private(root / DATABASE_SNAPSHOT_NAME, stage / "qualsight.sqlite3")
        for record in manifest["files"]:
            relative = Path(record["path"])
            _copy_private(root / "files" / relative, stage / relative)
        if _sha256(stage / "qualsight.sqlite3") != manifest["database"]["sha256"]:
            raise ValueError("restored database differs from its backup evidence")
        for record in manifest["files"]:
            if _sha256(stage / record["path"]) != record["sha256"]:
                raise ValueError(f"restored managed file differs from backup evidence: {record['path']}")
        _publish_directory_no_clobber(stage, target)
        published = True
    finally:
        if not published:
            shutil.rmtree(stage, ignore_errors=True)
    return {**verified, "path": str(target)}
