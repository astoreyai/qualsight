"""Process settings for the local control plane (environment-driven, no defaults invented)."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess

from ..execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)

DEFAULT_INPUT_RESOURCE_IDS = (OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate")
_APPLICATION_ROOT = Path(__file__).resolve().parents[4]
_PACKAGE_CONTENTS_NAME = "QUALSIGHT-PACKAGE-CONTENTS.json"
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")


def _git_output(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise ValueError("QualSight runtime authority requires an accessible Git checkout")
    return result.stdout.strip()


def _validate_committed_stage(
    application_root: Path,
    repository_root: Path,
    registry_path: Path,
) -> bool:
    stage_root_value = os.environ.get("QUALSIGHT_COMMITTED_STAGE_ROOT")
    stage_commit = os.environ.get("QUALSIGHT_COMMITTED_STAGE_COMMIT")
    if stage_root_value is None and stage_commit is None:
        return False
    if stage_root_value is None or stage_commit is None:
        raise ValueError("committed release-stage authority is incomplete")
    stage_root_path = Path(stage_root_value)
    if (
        not stage_root_path.is_absolute()
        or stage_root_path.is_symlink()
        or stage_root_path.resolve(strict=True) != application_root
        or _COMMIT_RE.fullmatch(stage_commit) is None
    ):
        raise ValueError("committed release-stage authority is invalid")
    git_root = Path(_git_output(repository_root, "rev-parse", "--show-toplevel")).resolve(
        strict=True
    )
    if git_root != repository_root:
        raise ValueError("committed release-stage repository is not the controlled Git root")
    if _git_output(repository_root, "rev-parse", "--verify", "HEAD^{commit}") != stage_commit:
        raise ValueError("committed release-stage commit differs from the controlled repository")
    if _git_output(
        repository_root, "status", "--porcelain=v1", "--untracked-files=no"
    ):
        raise ValueError("committed release-stage controlled repository has tracked changes")
    listing = subprocess.run(
        ["git", "-C", str(repository_root), "ls-files", "-z"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if listing.returncode != 0:
        raise ValueError("committed release-stage tree cannot be read")
    tracked_files = 0
    settings_relative = Path("backend/src/qualsight/api/settings.py")
    settings_seen = False
    for path_bytes in listing.stdout.split(b"\0"):
        if not path_bytes:
            continue
        try:
            relative = Path(path_bytes.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise ValueError("committed release-stage path is not UTF-8") from error
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("committed release-stage path is unsafe")
        path = application_root / relative
        controlled_path = repository_root / relative
        if (
            path.is_symlink()
            or not path.is_file()
            or controlled_path.is_symlink()
            or not controlled_path.is_file()
        ):
            raise ValueError(f"committed release-stage file is unavailable or unsafe: {relative}")
        resolved = path.resolve(strict=True)
        controlled_resolved = controlled_path.resolve(strict=True)
        if not resolved.is_relative_to(application_root) or not controlled_resolved.is_relative_to(
            repository_root
        ):
            raise ValueError(f"committed release-stage file escapes its root: {relative}")
        if hashlib.sha256(resolved.read_bytes()).digest() != hashlib.sha256(
            controlled_resolved.read_bytes()
        ).digest():
            raise ValueError(f"committed release-stage file differs: {relative}")
        if bool(resolved.stat().st_mode & 0o111) != bool(
            controlled_resolved.stat().st_mode & 0o111
        ):
            raise ValueError(f"committed release-stage file mode differs: {relative}")
        tracked_files += 1
        settings_seen = settings_seen or relative == settings_relative
    if tracked_files == 0 or not settings_seen:
        raise ValueError("committed release-stage tree is incomplete")

    controlled_registry_path = repository_root / "resources" / "resource-registry.json"
    if controlled_registry_path.is_symlink() or not controlled_registry_path.is_file():
        raise ValueError("committed release-stage controlled registry is unavailable or unsafe")
    if hashlib.sha256(registry_path.read_bytes()).digest() != hashlib.sha256(
        controlled_registry_path.read_bytes()
    ).digest():
        raise ValueError("committed release-stage registry differs from the controlled repository")
    if _git_output(repository_root, "rev-parse", "--verify", "HEAD^{commit}") != stage_commit:
        raise ValueError("committed release-stage controlled repository changed during validation")
    if _git_output(
        repository_root, "status", "--porcelain=v1", "--untracked-files=no"
    ):
        raise ValueError("committed release-stage controlled repository changed during validation")
    return True


def _validate_runtime_authority(
    application_root: Path,
    repository_root: Path,
    registry_path: Path,
) -> None:
    manifest_path = application_root / _PACKAGE_CONTENTS_NAME
    if not manifest_path.exists():
        try:
            git_root = Path(
                _git_output(application_root, "rev-parse", "--show-toplevel")
            ).resolve(strict=True)
        except ValueError:
            git_root = None
        if git_root == application_root:
            return
        if _validate_committed_stage(application_root, repository_root, registry_path):
            return
        raise ValueError(
            "QualSight application root is neither a controlled Git checkout, a byte-exact committed stage, nor a validated release package"
        )
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("installed package contents manifest must be a regular non-symlink file")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("installed package contents manifest is unreadable or invalid") from error
    if not isinstance(manifest, dict) or set(manifest) != {
        "files",
        "schema_version",
        "source_commit",
    }:
        raise ValueError("installed package contents manifest fields differ")
    if manifest["schema_version"] != "qualsight-source-package-contents-v1":
        raise ValueError("installed package contents manifest schema differs")
    source_commit = manifest["source_commit"]
    if not isinstance(source_commit, str) or _COMMIT_RE.fullmatch(source_commit) is None:
        raise ValueError("installed package source commit is invalid")

    git_root = Path(_git_output(repository_root, "rev-parse", "--show-toplevel")).resolve(
        strict=True
    )
    if git_root != repository_root:
        raise ValueError("installed package repository root is not the controlled Git root")
    controlled_commit = _git_output(
        repository_root, "rev-parse", "--verify", "HEAD^{commit}"
    )
    if controlled_commit != source_commit:
        raise ValueError(
            "installed package source commit differs from the controlled repository"
        )
    if _git_output(
        repository_root, "status", "--porcelain=v1", "--untracked-files=no"
    ):
        raise ValueError("installed package controlled repository has tracked changes")

    controlled_registry_path = repository_root / "resources" / "resource-registry.json"
    if controlled_registry_path.is_symlink() or not controlled_registry_path.is_file():
        raise ValueError(
            "installed package controlled repository registry is unavailable or unsafe"
        )
    controlled_registry = controlled_registry_path.resolve(strict=True)
    if hashlib.sha256(registry_path.read_bytes()).digest() != hashlib.sha256(
        controlled_registry.read_bytes()
    ).digest():
        raise ValueError(
            "installed package registry differs from the controlled repository registry"
        )
    if _git_output(repository_root, "rev-parse", "--verify", "HEAD^{commit}") != source_commit:
        raise ValueError("installed package controlled repository changed during validation")
    if _git_output(
        repository_root, "status", "--porcelain=v1", "--untracked-files=no"
    ):
        raise ValueError("installed package controlled repository changed during validation")


@dataclass(frozen=True, slots=True)
class Settings:
    repository_root: Path
    registry_path: Path
    data_dir: Path
    runtime_resource_id: str
    input_resource_ids: tuple[str, ...]
    source_cache_root: Path | None = None
    console_root: Path | None = None
    actor_id: str = ""  # configured single user (QUALSIGHT_ACTOR); empty → local-user:<login>
    # Direct construction remains opt-in so isolated tests do not mutate their
    # host process's sockets. Environment-driven production startup is guarded
    # by default and requires an explicit false value to permit network access.
    network_guard: bool = False
    scientific_workers: int = 4
    scientific_pending: int = 4
    detector_workers: int = 2
    native_execution_timeout_seconds: float = 7200.0
    native_output_limit_bytes: int = 16 * 1024 * 1024
    shutdown_drain_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.scientific_workers < 1:
            raise ValueError("scientific_workers must be at least one")
        if self.scientific_pending < 0:
            raise ValueError("scientific_pending cannot be negative")
        if self.detector_workers < 1 or self.detector_workers > 4:
            raise ValueError("detector_workers must be between one and four")
        if (
            not math.isfinite(self.native_execution_timeout_seconds)
            or self.native_execution_timeout_seconds <= 0
        ):
            raise ValueError(
                "native_execution_timeout_seconds must be finite and positive"
            )
        if self.native_output_limit_bytes < 1:
            raise ValueError("native_output_limit_bytes must be positive")
        if self.shutdown_drain_seconds < 0:
            raise ValueError("shutdown_drain_seconds cannot be negative")

    @classmethod
    def from_environment(cls, repository_root: Path | None = None) -> Settings:
        root = Path(
            os.environ.get("QUALSIGHT_REPOSITORY_ROOT")
            or repository_root
            or Path(__file__).resolve().parents[4]
        ).resolve(strict=True)
        registry_path = Path(
            os.environ.get("QUALSIGHT_REGISTRY_PATH")
            or root / "resources" / "resource-registry.json"
        )
        if not registry_path.is_absolute() or registry_path.is_symlink():
            raise ValueError(
                "QUALSIGHT_REGISTRY_PATH must name an absolute, non-symlink file"
            )
        registry = registry_path.resolve(strict=True)
        if not registry.is_file():
            raise ValueError(
                "QUALSIGHT_REGISTRY_PATH must name an absolute, non-symlink file"
            )
        data_dir = Path(os.environ.get("QUALSIGHT_DATA_DIR") or root / "var")
        runtime = os.environ.get("QUALSIGHT_RUNTIME_RESOURCE_ID") or OFIQ_V112_RUNTIME_RESOURCE_ID
        inputs_raw = os.environ.get("QUALSIGHT_INPUT_RESOURCE_IDS")
        inputs = tuple(item for item in inputs_raw.split(",") if item) if inputs_raw else DEFAULT_INPUT_RESOURCE_IDS
        source_cache_value = os.environ.get("QUALSIGHT_SOURCE_CACHE_ROOT")
        source_cache_root = None
        if source_cache_value is not None:
            source_cache_path = Path(source_cache_value)
            if not source_cache_path.is_absolute() or source_cache_path.is_symlink():
                raise ValueError(
                    "QUALSIGHT_SOURCE_CACHE_ROOT must name an absolute, non-symlink directory"
                )
            source_cache_root = source_cache_path.resolve(strict=True)
            if not source_cache_root.is_dir():
                raise ValueError(
                    "QUALSIGHT_SOURCE_CACHE_ROOT must name an absolute, non-symlink directory"
                )
        console_root_value = os.environ.get("QUALSIGHT_CONSOLE_ROOT")
        console_root = None
        if console_root_value is not None:
            console_root_path = Path(console_root_value)
            if not console_root_path.is_absolute() or console_root_path.is_symlink():
                raise ValueError(
                    "QUALSIGHT_CONSOLE_ROOT must name an absolute, non-symlink directory"
                )
            console_root = console_root_path.resolve(strict=True)
            if not console_root.is_dir() or not (
                console_root / "frontend" / "dist" / "index.html"
            ).is_file():
                raise ValueError(
                    "QUALSIGHT_CONSOLE_ROOT must contain frontend/dist/index.html"
                )
        network_value = os.environ.get("QUALSIGHT_NETWORK_GUARD")
        network_guard = network_value is None or network_value.lower() not in {"0", "false", "no", "off"}
        try:
            scientific_workers = int(os.environ.get("QUALSIGHT_SCIENTIFIC_WORKERS", "4"))
            scientific_pending = int(os.environ.get("QUALSIGHT_SCIENTIFIC_PENDING", "4"))
            detector_workers = int(os.environ.get("QUALSIGHT_DETECTOR_WORKERS", "2"))
            native_execution_timeout_seconds = float(
                os.environ.get("QUALSIGHT_NATIVE_EXECUTION_TIMEOUT_SECONDS", "7200")
            )
            native_output_limit_bytes = int(
                os.environ.get("QUALSIGHT_NATIVE_OUTPUT_LIMIT_BYTES", str(16 * 1024 * 1024))
            )
            shutdown_drain_seconds = float(
                os.environ.get("QUALSIGHT_SHUTDOWN_DRAIN_SECONDS", "30")
            )
        except ValueError as error:
            raise ValueError("scientific and native execution limits are invalid") from error
        _validate_runtime_authority(_APPLICATION_ROOT, root, registry)
        return cls(
            repository_root=root,
            registry_path=registry,
            data_dir=data_dir,
            runtime_resource_id=runtime,
            input_resource_ids=inputs,
            source_cache_root=source_cache_root,
            console_root=console_root,
            actor_id=os.environ.get("QUALSIGHT_ACTOR", ""),
            network_guard=network_guard,
            scientific_workers=scientific_workers,
            scientific_pending=scientific_pending,
            detector_workers=detector_workers,
            native_execution_timeout_seconds=native_execution_timeout_seconds,
            native_output_limit_bytes=native_output_limit_bytes,
            shutdown_drain_seconds=shutdown_drain_seconds,
        )

    @property
    def actor(self) -> str:
        import getpass

        return self.actor_id or f"local-user:{getpass.getuser()}"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "qualsight.sqlite3"

    @property
    def protected_roots(self) -> tuple[Path, ...]:
        roots = [self.repository_root]
        if self.console_root is not None:
            roots.append(self.console_root)
        roots.append(Path.home())
        return tuple(roots)

    @property
    def upstream_cache_root(self) -> Path:
        return self.source_cache_root or self.repository_root / "var" / "upstream"
