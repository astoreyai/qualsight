from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal

from pydantic import model_validator

from .entities import Artifact
from .primitives import (
    ArtifactIdentity,
    AwareDateTime,
    CanonicalJson,
    ComponentBinding,
    EnvironmentIdentity,
    FiniteNumber,
    HardwareIdentity,
    NonEmptyStr,
    NonNegativeFloat,
    NonNegativeInt,
    RepositoryIdentity,
    ResourceBinding,
    Sha256,
    StrictFrozenModel,
)


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    VALIDATED = "VALIDATED"
    REPRODUCED = "REPRODUCED"


class RunAttestationKind(StrEnum):
    VALIDATED = "VALIDATED"
    REPRODUCED = "REPRODUCED"
    SCIENTIFIC_SUPPORT = "SCIENTIFIC_SUPPORT"


class ReproductionState(StrEnum):
    EXACT = "EXACT"
    WITHIN_TOLERANCE = "WITHIN_TOLERANCE"
    DIFFERENT = "DIFFERENT"
    MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
    MISSING_DATA = "MISSING_DATA"
    BLOCKED = "BLOCKED"


class ScientificSupportState(StrEnum):
    NOT_ASSESSED = "NOT_ASSESSED"
    SUPPORTED = "SUPPORTED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    BLOCKED = "BLOCKED"


class MetricObservation(StrictFrozenModel):
    metric_id: NonEmptyStr
    value: FiniteNumber
    unit: NonEmptyStr


class LogRecord(StrictFrozenModel):
    stream: Literal["stdout", "stderr"]
    sha256: Sha256
    text: str

    @model_validator(mode="after")
    def validate_digest(self) -> LogRecord:
        observed = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if observed != self.sha256:
            raise ValueError("log SHA-256 does not match its UTF-8 text")
        return self


class CompletedRun(StrictFrozenModel):
    run_id: NonEmptyStr
    supersedes_run_id: NonEmptyStr | None = None
    project_id: NonEmptyStr
    experiment_id: NonEmptyStr
    user_id: NonEmptyStr
    dataset_manifest: CanonicalJson
    dataset_hash: Sha256
    subject_protocol: ComponentBinding
    pair_search_protocol: ComponentBinding
    # A quality Run binds its engine; a matcher Run has no quality engine and
    # records the matcher/detector bytes in model_hashes instead.
    quality_engine: ResourceBinding | None
    quality_engine_configuration: ArtifactIdentity | None
    model_hashes: tuple[ArtifactIdentity, ...]
    matcher: ComponentBinding
    matcher_preprocessing: ComponentBinding
    operating_thresholds: ComponentBinding
    statistical_methods: ComponentBinding
    confidence_interval_method: ComponentBinding
    randomness: ComponentBinding
    random_seeds: tuple[int, ...]
    application_version: NonEmptyStr
    repository: RepositoryIdentity
    environment: EnvironmentIdentity
    hardware: HardwareIdentity
    start_time: AwareDateTime
    end_time: AwareDateTime
    runtime_seconds: NonNegativeFloat
    initialization_ms: NonNegativeInt
    assessment_ms: NonNegativeInt
    configuration: CanonicalJson
    metrics: tuple[MetricObservation, ...]
    artifacts: tuple[Artifact, ...]
    logs: tuple[LogRecord, ...]
    warnings: tuple[NonEmptyStr, ...]
    provenance_limits: tuple[NonEmptyStr, ...]
    exit_code: Literal[0]
    status: Literal[RunStatus.COMPLETED]
    scientific_support: Literal[ScientificSupportState.NOT_ASSESSED]

    @model_validator(mode="after")
    def validate_completed_run(self) -> CompletedRun:
        if self.end_time < self.start_time:
            raise ValueError("Run end precedes start")
        if self.dataset_hash != self.dataset_manifest.sha256:
            raise ValueError("dataset hash does not identify the canonical manifest")
        if not self.metrics:
            raise ValueError("completed Run requires observed metrics")
        if (self.quality_engine is None) != (self.quality_engine_configuration is None):
            raise ValueError("quality engine and its configuration are recorded together or not at all")
        if self.quality_engine is None and self.matcher.state.value != "CONFIGURED":
            raise ValueError("a Run without a quality engine must configure a matcher")
        if not self.model_hashes:
            raise ValueError("completed Run must bind at least one model identity")
        metric_ids = tuple(metric.metric_id for metric in self.metrics)
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("completed Run contains duplicate metric IDs")
        artifact_ids = tuple(artifact.artifact_id for artifact in self.artifacts)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("completed Run contains duplicate artifact IDs")
        if any(artifact.run_id != self.run_id for artifact in self.artifacts):
            raise ValueError("artifact Run reference does not match completed Run")
        if self.random_seeds and self.randomness.state.value != "CONFIGURED":
            raise ValueError("random seeds require a configured randomness binding")
        return self


class RunAttestation(StrictFrozenModel):
    attestation_id: NonEmptyStr
    run_id: NonEmptyStr
    kind: RunAttestationKind
    created_at: AwareDateTime
    actor_id: NonEmptyStr
    evidence_sha256: Sha256
    reproduction_state: ReproductionState | None = None

    @model_validator(mode="after")
    def validate_reproduction_state(self) -> RunAttestation:
        if self.kind is RunAttestationKind.REPRODUCED:
            if self.reproduction_state is None:
                raise ValueError("reproduction attestation requires reproduction state")
        elif self.reproduction_state is not None:
            raise ValueError("only reproduction attestations carry reproduction state")
        return self


class ScientificSupportAssessment(StrictFrozenModel):
    assessment_id: NonEmptyStr
    run_id: NonEmptyStr
    state: ScientificSupportState
    created_at: AwareDateTime
    actor_id: NonEmptyStr
    evidence: CanonicalJson
