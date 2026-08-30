"""Exclusive process ownership for one QualSight application data directory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import threading

from .local_storage import PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE


LEASE_FILENAME = ".qualsight-process.lock"
_MAX_METADATA_BYTES = 4096


class DataDirectoryLeaseError(RuntimeError):
    """Raised when exclusive ownership of the application data directory is unavailable."""


@dataclass(slots=True)
class _ProcessLeaseRecord:
    file_descriptor: int
    pid: int
    references: int = 1


_HELD_LEASES: dict[Path, _ProcessLeaseRecord] = {}
_HELD_LEASES_LOCK = threading.Lock()
_HELD_LEASES_PID = os.getpid()


def _discard_inherited_lease_records() -> None:
    global _HELD_LEASES_PID

    current_pid = os.getpid()
    if current_pid == _HELD_LEASES_PID:
        return
    for record in _HELD_LEASES.values():
        os.close(record.file_descriptor)
    _HELD_LEASES.clear()
    _HELD_LEASES_PID = current_pid


def _read_holder_metadata(file_descriptor: int) -> dict[str, object] | None:
    os.lseek(file_descriptor, 0, os.SEEK_SET)
    payload = os.read(file_descriptor, _MAX_METADATA_BYTES)
    if not payload:
        return None
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_all(file_descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(file_descriptor, payload[offset:])
        if written == 0:
            raise DataDirectoryLeaseError("data-directory lease metadata write made no progress")
        offset += written


@dataclass(slots=True)
class DataDirectoryLease:
    """A process-held, kernel-enforced exclusive lease released by ``close``."""

    path: Path
    owner: str
    _record: _ProcessLeaseRecord
    _closed: bool = False

    @classmethod
    def acquire(cls, data_dir: Path, *, owner: str) -> "DataDirectoryLease":
        owner = owner.strip()
        if not owner or len(owner) > 128:
            raise DataDirectoryLeaseError("lease owner must contain 1 to 128 characters")

        root = Path(os.path.abspath(data_dir))
        if root == Path(root.anchor):
            raise DataDirectoryLeaseError(f"refusing filesystem root as application data directory: {root}")
        if root.is_symlink():
            raise DataDirectoryLeaseError(f"refusing symlinked application data directory: {root}")
        root.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
        if not root.is_dir():
            raise DataDirectoryLeaseError(f"application data path is not a directory: {root}")
        os.chmod(root, PRIVATE_DIRECTORY_MODE, follow_symlinks=False)

        lease_path = root / LEASE_FILENAME
        with _HELD_LEASES_LOCK:
            _discard_inherited_lease_records()
            existing = _HELD_LEASES.get(lease_path)
            if existing is not None:
                existing.references += 1
                return cls(path=lease_path, owner=owner, _record=existing)

            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            try:
                directory_descriptor = os.open(root, directory_flags)
            except OSError as error:
                raise DataDirectoryLeaseError(f"cannot open application data directory securely: {root}: {error}") from error
            try:
                lock_flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
                try:
                    file_descriptor = os.open(LEASE_FILENAME, lock_flags, PRIVATE_FILE_MODE, dir_fd=directory_descriptor)
                except OSError as error:
                    raise DataDirectoryLeaseError(f"cannot open data-directory lease securely: {lease_path}: {error}") from error
            finally:
                os.close(directory_descriptor)

            try:
                file_status = os.fstat(file_descriptor)
                if not stat.S_ISREG(file_status.st_mode) or file_status.st_nlink != 1:
                    raise DataDirectoryLeaseError(f"data-directory lease is not a private regular file: {lease_path}")
                if file_status.st_uid != os.geteuid():
                    raise DataDirectoryLeaseError(f"data-directory lease is not owned by the current user: {lease_path}")
                os.fchmod(file_descriptor, PRIVATE_FILE_MODE)
                try:
                    fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN):
                        raise DataDirectoryLeaseError(f"cannot lock application data directory: {root}: {error}") from error
                    holder = _read_holder_metadata(file_descriptor)
                    details = "holder metadata unavailable"
                    if holder is not None:
                        details = ", ".join(
                            f"{key}={holder[key]}"
                            for key in ("pid", "owner", "acquired_at")
                            if key in holder
                        )
                    raise DataDirectoryLeaseError(f"application data directory is already in use: {root} ({details})") from error

                metadata = {
                    "acquired_at": datetime.now(timezone.utc).isoformat(),
                    "data_dir": str(root),
                    "owner": owner,
                    "pid": os.getpid(),
                    "version": 1,
                }
                payload = (json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                os.ftruncate(file_descriptor, 0)
                os.lseek(file_descriptor, 0, os.SEEK_SET)
                _write_all(file_descriptor, payload)
                os.fsync(file_descriptor)
                record = _ProcessLeaseRecord(file_descriptor=file_descriptor, pid=os.getpid())
                _HELD_LEASES[lease_path] = record
                return cls(path=lease_path, owner=owner, _record=record)
            except BaseException:
                os.close(file_descriptor)
                raise

    def close(self) -> None:
        with _HELD_LEASES_LOCK:
            if self._closed:
                return
            self._closed = True
            _discard_inherited_lease_records()
            held = _HELD_LEASES.get(self.path)
            if held is not self._record:
                return
            held.references -= 1
            if held.references > 0:
                return
            del _HELD_LEASES[self.path]
            try:
                fcntl.flock(held.file_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(held.file_descriptor)

    def __enter__(self) -> "DataDirectoryLease":
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()
