from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return value


AwareDateTime = Annotated[datetime, AfterValidator(_aware)]
NonEmptyStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitCommit = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40,64}$")]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]


def _finite_number(value: int | float) -> int | float:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("numeric evidence must be finite")
    return value


FiniteNumber = Annotated[int | float, AfterValidator(_finite_number)]


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class EvidenceState(StrEnum):
    RECORDED = "RECORDED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_RECORDED = "NOT_RECORDED"


class EvidenceText(StrictFrozenModel):
    state: EvidenceState
    value: NonEmptyStr | None = None
    reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> EvidenceText:
        if self.state is EvidenceState.RECORDED:
            if self.value is None or self.reason is not None:
                raise ValueError("recorded evidence requires value and forbids reason")
        elif self.value is not None or self.reason is None:
            raise ValueError("non-recorded evidence requires reason and forbids value")
        return self


class CountEvidence(StrictFrozenModel):
    state: EvidenceState
    value: NonNegativeInt | None = None
    reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> CountEvidence:
        if self.state is EvidenceState.RECORDED:
            if self.value is None or self.reason is not None:
                raise ValueError("recorded count requires value and forbids reason")
        elif self.value is not None or self.reason is None:
            raise ValueError("non-recorded count requires reason and forbids value")
        return self


class ComponentState(StrEnum):
    CONFIGURED = "CONFIGURED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_RECORDED = "NOT_RECORDED"


class ComponentBinding(StrictFrozenModel):
    state: ComponentState
    component_id: NonEmptyStr | None = None
    version: NonEmptyStr | None = None
    configuration_sha256: Sha256 | None = None
    artifact_hashes: tuple[Sha256, ...] = ()
    reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> ComponentBinding:
        configured_values = (
            self.component_id,
            self.version,
            self.configuration_sha256,
        )
        if self.state is ComponentState.CONFIGURED:
            if self.component_id is None or self.version is None or self.reason is not None:
                raise ValueError(
                    "configured component requires identity/version and forbids reason"
                )
        elif any(value is not None for value in configured_values) or self.artifact_hashes:
            raise ValueError("non-configured component cannot carry configuration values")
        elif self.reason is None:
            raise ValueError("non-configured component requires an explicit reason")
        return self


class CanonicalJson(StrictFrozenModel):
    text: NonEmptyStr
    sha256: Sha256

    @staticmethod
    def serialize(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_value(cls, value: Any) -> CanonicalJson:
        text = cls.serialize(value)
        return cls(text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())

    @model_validator(mode="after")
    def validate_canonical_bytes(self) -> CanonicalJson:
        parsed = json.loads(self.text)
        if self.serialize(parsed) != self.text:
            raise ValueError("JSON text is not in canonical form")
        observed = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if observed != self.sha256:
            raise ValueError("canonical JSON hash does not match its bytes")
        return self


class ArtifactIdentity(StrictFrozenModel):
    path: NonEmptyStr
    sha256: Sha256
    size_bytes: NonNegativeInt
    mtime_ns: NonNegativeInt


class KeyValue(StrictFrozenModel):
    key: NonEmptyStr
    value: NonEmptyStr


class ResourceBinding(StrictFrozenModel):
    resource_id: NonEmptyStr
    version: NonEmptyStr
    registry_state: NonEmptyStr
    artifacts: tuple[ArtifactIdentity, ...]

    @model_validator(mode="after")
    def validate_artifacts(self) -> ResourceBinding:
        if not self.artifacts:
            raise ValueError("resource binding requires byte-identified artifacts")
        paths = tuple(artifact.path for artifact in self.artifacts)
        if len(paths) != len(set(paths)):
            raise ValueError("resource binding contains duplicate artifact paths")
        return self


class RepositoryState(StrEnum):
    UNBORN = "UNBORN"
    COMMITTED = "COMMITTED"


class RepositoryIdentity(StrictFrozenModel):
    root: NonEmptyStr
    state: RepositoryState
    commit: GitCommit | None = None
    dirty: bool
    status_sha256: Sha256

    @model_validator(mode="after")
    def validate_commit_state(self) -> RepositoryIdentity:
        if self.state is RepositoryState.UNBORN and self.commit is not None:
            raise ValueError("unborn repository cannot identify a commit")
        if self.state is RepositoryState.COMMITTED and self.commit is None:
            raise ValueError("committed repository requires a commit identity")
        return self


class EnvironmentIdentity(StrictFrozenModel):
    operating_system: NonEmptyStr
    hostname: NonEmptyStr
    machine: NonEmptyStr
    python_version: NonEmptyStr
    pydantic_version: NonEmptyStr
    dependency_manifest: ArtifactIdentity
    variables: tuple[KeyValue, ...]


class HardwareIdentity(StrictFrozenModel):
    platform: NonEmptyStr
    architecture: NonEmptyStr
    logical_cpu_count: PositiveInt
    device: EvidenceText
