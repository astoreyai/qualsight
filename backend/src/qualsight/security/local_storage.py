"""Restrictive local storage controls for biometric application data."""

from __future__ import annotations

import os
from pathlib import Path
import stat


PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class LocalStorageSecurityError(RuntimeError):
    """Raised when the application data tree cannot be secured safely."""


def activate_private_umask() -> int:
    """Make all subsequently created application files private by default."""

    return os.umask(0o077)


_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK


def _open_directory_component(parent_fd: int, name: str, path: Path) -> int:
    created = False
    try:
        os.mkdir(name, mode=PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    try:
        descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise LocalStorageSecurityError(
            f"refusing non-directory or linked application path component: {path}"
        ) from error
    mode = os.fstat(descriptor).st_mode
    if not stat.S_ISDIR(mode):
        os.close(descriptor)
        raise LocalStorageSecurityError(f"refusing non-directory application path component: {path}")
    if created:
        os.fchmod(descriptor, PRIVATE_DIRECTORY_MODE)
    return descriptor


def _secure_open_directory(descriptor: int, path: Path) -> tuple[int, int]:
    os.fchmod(descriptor, PRIVATE_DIRECTORY_MODE)
    secured_directories = 1
    secured_files = 0
    try:
        with os.scandir(descriptor) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError as error:
        raise LocalStorageSecurityError(f"cannot enumerate application data directory: {path}") from error

    for name in names:
        child_path = path / name
        try:
            child_mode = os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode
        except OSError as error:
            raise LocalStorageSecurityError(f"cannot inspect application data entry: {child_path}") from error
        if stat.S_ISLNK(child_mode):
            raise LocalStorageSecurityError(f"refusing symlink in application data tree: {child_path}")
        if stat.S_ISDIR(child_mode):
            try:
                child_descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
            except OSError as error:
                raise LocalStorageSecurityError(
                    f"refusing replaced or linked application directory: {child_path}"
                ) from error
            try:
                directories, files = _secure_open_directory(child_descriptor, child_path)
            finally:
                os.close(child_descriptor)
            secured_directories += directories
            secured_files += files
            continue
        if not stat.S_ISREG(child_mode):
            raise LocalStorageSecurityError(f"unexpected non-regular application data file: {child_path}")
        try:
            file_descriptor = os.open(name, _FILE_OPEN_FLAGS, dir_fd=descriptor)
        except OSError as error:
            raise LocalStorageSecurityError(
                f"refusing replaced or linked application data file: {child_path}"
            ) from error
        try:
            if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                raise LocalStorageSecurityError(
                    f"refusing non-regular application data file after open: {child_path}"
                )
            os.fchmod(file_descriptor, PRIVATE_FILE_MODE)
        finally:
            os.close(file_descriptor)
        secured_files += 1
    return secured_directories, secured_files


def secure_data_tree(root: Path, *, protected_roots: tuple[Path, ...] = ()) -> dict[str, int]:
    """Create or remediate one narrowly scoped application tree without following links."""

    root = Path(os.path.abspath(root))
    if root == Path(root.anchor):
        raise LocalStorageSecurityError(f"refusing filesystem root as application data tree: {root}")
    for protected in protected_roots:
        protected = Path(os.path.abspath(protected))
        if root == protected or root in protected.parents:
            raise LocalStorageSecurityError(
                f"refusing application data tree that is or contains protected path {protected}: {root}"
            )
    current_path = Path(root.anchor)
    try:
        descriptor = os.open(root.anchor, _DIRECTORY_OPEN_FLAGS)
    except OSError as error:
        raise LocalStorageSecurityError(f"cannot open application path anchor: {root.anchor}") from error
    try:
        for name in root.parts[1:]:
            current_path /= name
            child_descriptor = _open_directory_component(descriptor, name, current_path)
            os.close(descriptor)
            descriptor = child_descriptor
        secured_directories, secured_files = _secure_open_directory(descriptor, root)
    finally:
        os.close(descriptor)
    return {"directories": secured_directories, "files": secured_files}
