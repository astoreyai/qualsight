"""Process settings for the local control plane (environment-driven, no defaults invented)."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path

from ..execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)

DEFAULT_INPUT_RESOURCE_IDS = (OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate")


@dataclass(frozen=True, slots=True)
class Settings:
    repository_root: Path
    registry_path: Path
    data_dir: Path
    runtime_resource_id: str
    input_resource_ids: tuple[str, ...]
    source_cache_root: Path | None = None
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
        registry = Path(os.environ.get("QUALSIGHT_REGISTRY_PATH") or root / "resources" / "resource-registry.json")
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
        return cls(
            repository_root=root,
            registry_path=registry.resolve(strict=True),
            data_dir=data_dir,
            runtime_resource_id=runtime,
            input_resource_ids=inputs,
            source_cache_root=source_cache_root,
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
    def upstream_cache_root(self) -> Path:
        return self.source_cache_root or self.repository_root / "var" / "upstream"
