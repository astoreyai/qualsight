from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .primitives import (
    ArtifactIdentity,
    AwareDateTime,
    CanonicalJson,
    ComponentBinding,
    CountEvidence,
    EvidenceText,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    Sha256,
    StrictFrozenModel,
)


class Modality(StrEnum):
    FACE = "face"
    FINGERPRINT = "fingerprint"
    IRIS = "iris"
    VOICE = "voice"
    MULTIMODAL = "multimodal"


class DataClassification(StrEnum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"
    RESTRICTED = "RESTRICTED"
    SENSITIVE_BIOMETRIC_DERIVED = "SENSITIVE_BIOMETRIC_DERIVED"


class ProjectLifecycle(StrEnum):
    NEW = "NEW"
    OPEN = "OPEN"
    SAVED = "SAVED"
    ARCHIVED = "ARCHIVED"


class AccessPolicy(StrEnum):
    LOCAL_SINGLE_USER = "LOCAL_SINGLE_USER"
    PROJECT_MEMBERS = "PROJECT_MEMBERS"


class PermissionState(StrEnum):
    ALLOWED = "ALLOWED"
    PROHIBITED = "PROHIBITED"
    LIMITED = "LIMITED"
    NOT_RECORDED = "NOT_RECORDED"


class StorageBackend(StrEnum):
    SQLITE = "SQLITE"
    PARQUET = "PARQUET"
    FILESYSTEM = "FILESYSTEM"


class Project(StrictFrozenModel):
    project_id: NonEmptyStr
    name: NonEmptyStr
    description: NonEmptyStr
    lifecycle: ProjectLifecycle
    owner_user_id: NonEmptyStr
    access_policy: AccessPolicy
    classification: DataClassification
    redistribution_permission: PermissionState
    display_permission: PermissionState
    use_restrictions: tuple[NonEmptyStr, ...]
    tags: tuple[NonEmptyStr, ...]
    notes: tuple[NonEmptyStr, ...]
    metadata: CanonicalJson
    created_at: AwareDateTime
    updated_at: AwareDateTime

    @model_validator(mode="after")
    def validate_project(self) -> Project:
        if self.updated_at < self.created_at:
            raise ValueError("project update precedes creation")
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("project tags must be unique")
        return self


class Dataset(StrictFrozenModel):
    dataset_id: NonEmptyStr
    project_id: NonEmptyStr
    name: NonEmptyStr
    version: NonEmptyStr
    source_uri: NonEmptyStr
    # A dataset without a license document (LFW) records ``license=None`` and a
    # factual ``rights_status`` taken from the resource registry; a licensed
    # dataset binds the exact license artifact bytes.
    license: ArtifactIdentity | None
    rights_status: NonEmptyStr = "VERIFIED_LICENSE"
    rights_evidence: tuple[ArtifactIdentity, ...] = ()
    retrieval: EvidenceText
    manifest: CanonicalJson
    subject_count: CountEvidence
    sample_count: CountEvidence
    modality: Modality
    identity_schema: ComponentBinding
    metadata_schema: ComponentBinding
    redistribution_permission: PermissionState
    display_permission: PermissionState
    derived_artifact_policy: NonEmptyStr
    classification: DataClassification


class DatasetStatisticsState(StrEnum):
    COMPUTED = "COMPUTED"
    NOT_ASSESSED = "NOT_ASSESSED"


class Sample(StrictFrozenModel):
    sample_id: NonEmptyStr
    dataset_id: NonEmptyStr
    subject_id: EvidenceText
    modality: Modality
    source_path: NonEmptyStr
    media_type: NonEmptyStr
    sha256: Sha256
    size_bytes: NonNegativeInt
    mtime_ns: NonNegativeInt
    metadata: CanonicalJson


class ProtocolKind(StrEnum):
    SINGLE_SAMPLE_QUALITY = "SINGLE_SAMPLE_QUALITY"
    VERIFICATION = "VERIFICATION"
    IDENTIFICATION = "IDENTIFICATION"


class IdentificationMode(StrEnum):
    OPEN_SET = "OPEN_SET"
    CLOSED_SET = "CLOSED_SET"


class SingleSampleQualityProtocol(StrictFrozenModel):
    kind: Literal[ProtocolKind.SINGLE_SAMPLE_QUALITY]
    sample_ids: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_samples(self) -> SingleSampleQualityProtocol:
        if not self.sample_ids:
            raise ValueError("single-sample protocol requires at least one real sample")
        if len(self.sample_ids) != len(set(self.sample_ids)):
            raise ValueError("single-sample protocol contains duplicate sample IDs")
        return self


class VerificationProtocol(StrictFrozenModel):
    kind: Literal[ProtocolKind.VERIFICATION]
    protocol_id: NonEmptyStr
    pair_manifest: ArtifactIdentity
    identity_schema: ComponentBinding


class IdentificationProtocol(StrictFrozenModel):
    kind: Literal[ProtocolKind.IDENTIFICATION]
    mode: IdentificationMode
    protocol_id: NonEmptyStr
    gallery_manifest: ArtifactIdentity
    probe_manifest: ArtifactIdentity
    rank_depth: PositiveInt
    identity_schema: ComponentBinding


EvaluationProtocol = Annotated[
    SingleSampleQualityProtocol | VerificationProtocol | IdentificationProtocol,
    Field(discriminator="kind"),
]


class Experiment(StrictFrozenModel):
    experiment_id: NonEmptyStr
    project_id: NonEmptyStr
    dataset_id: NonEmptyStr
    name: NonEmptyStr
    created_by: NonEmptyStr
    created_at: AwareDateTime
    protocol: EvaluationProtocol
    configuration: CanonicalJson


class ArtifactKind(StrEnum):
    SOURCE_INPUT = "SOURCE_INPUT"
    METRICS = "METRICS"
    LOG = "LOG"
    FIGURE = "FIGURE"
    TABLE = "TABLE"
    REPORT = "REPORT"
    REPRODUCTION_BUNDLE = "REPRODUCTION_BUNDLE"


class Artifact(StrictFrozenModel):
    artifact_id: NonEmptyStr
    run_id: NonEmptyStr
    kind: ArtifactKind
    storage_backend: StorageBackend
    uri: NonEmptyStr
    sha256: Sha256
    size_bytes: NonNegativeInt
    media_type: NonEmptyStr
    classification: DataClassification
    created_at: AwareDateTime

    @model_validator(mode="after")
    def validate_storage_assignment(self) -> Artifact:
        if self.storage_backend is StorageBackend.SQLITE:
            raise ValueError("scientific artifacts cannot be stored in SQLite")
        if self.kind not in {ArtifactKind.METRICS, ArtifactKind.TABLE}:
            if self.storage_backend is not StorageBackend.FILESYSTEM:
                raise ValueError(
                    f"{self.kind.value} artifacts require filesystem storage"
                )
        return self


class AuditEventType(StrEnum):
    PROJECT_CREATED = "PROJECT_CREATED"
    DATASET_IMPORTED = "DATASET_IMPORTED"
    CONFIGURATION_CHANGED = "CONFIGURATION_CHANGED"
    EXPERIMENT_CREATED = "EXPERIMENT_CREATED"
    RUN_STARTED = "RUN_STARTED"
    RUN_COMPLETED = "RUN_COMPLETED"
    ARTIFACT_EXPORTED = "ARTIFACT_EXPORTED"
    ANNOTATION_MODIFIED = "ANNOTATION_MODIFIED"
    DATASET_PURGED = "DATASET_PURGED"


class AuditEvent(StrictFrozenModel):
    audit_event_id: NonEmptyStr
    project_id: NonEmptyStr
    actor_id: NonEmptyStr
    event_type: AuditEventType
    subject_type: NonEmptyStr
    subject_id: NonEmptyStr
    occurred_at: AwareDateTime
    details: CanonicalJson
    previous_evidence_sha256: Sha256 | None = None
    current_evidence_sha256: Sha256
    local_only: Literal[True]
