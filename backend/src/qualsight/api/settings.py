"""Process settings for the local control plane (environment-driven, no defaults invented)."""

from __future__ import annotations

from dataclasses import dataclass
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
        return cls(
            repository_root=root,
            registry_path=registry.resolve(strict=True),
            data_dir=data_dir,
            runtime_resource_id=runtime,
            input_resource_ids=inputs,
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / "qualsight.sqlite3"
