"""Contemporaneous provenance capture for scientific Runs.

Every value here is observed from the running host at capture time; nothing is
copied from a prior record.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import platform
import subprocess
import sys
import tomllib

from pydantic.version import VERSION as PYDANTIC_VERSION

from .domain.primitives import (
    ArtifactIdentity,
    EnvironmentIdentity,
    EvidenceState,
    EvidenceText,
    HardwareIdentity,
    KeyValue,
    RepositoryIdentity,
    RepositoryState,
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_identity(path: Path) -> ArtifactIdentity:
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    return ArtifactIdentity(
        path=str(resolved),
        sha256=sha256_file(resolved),
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def repository_identity(root: Path) -> RepositoryIdentity:
    """Observe the Git state of ``root`` (unborn HEAD is a legitimate state)."""

    commit_result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
        check=False,
        capture_output=True,
    )
    status_result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        check=True,
        capture_output=True,
    )
    if commit_result.returncode == 0:
        state = RepositoryState.COMMITTED
        commit: str | None = commit_result.stdout.decode("ascii").strip()
    else:
        state = RepositoryState.UNBORN
        commit = None
    return RepositoryIdentity(
        root=str(root.resolve()),
        state=state,
        commit=commit,
        dirty=bool(status_result.stdout),
        status_sha256=sha256_bytes(status_result.stdout),
    )


def environment_identity(
    dependency_manifest: Path,
    variables: dict[str, str],
) -> EnvironmentIdentity:
    return EnvironmentIdentity(
        operating_system=platform.platform(),
        hostname=platform.node(),
        machine=platform.machine(),
        python_version=platform.python_version(),
        pydantic_version=PYDANTIC_VERSION,
        dependency_manifest=artifact_identity(dependency_manifest),
        variables=tuple(KeyValue(key=key, value=value) for key, value in sorted(variables.items())),
    )


def hardware_identity(device_reason: str) -> HardwareIdentity:
    return HardwareIdentity(
        platform=sys.platform,
        architecture=platform.machine(),
        logical_cpu_count=os.cpu_count() or 1,
        device=EvidenceText(state=EvidenceState.NOT_RECORDED, reason=device_reason),
    )


def application_version(backend_pyproject: Path) -> str:
    value = tomllib.loads(backend_pyproject.read_text(encoding="utf-8"))
    return str(value["project"]["version"])
