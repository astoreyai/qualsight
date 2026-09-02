"""Immutable evidence for one directly admitted owner-pair comparison.

This record is deliberately separate from :class:`CompletedRun`: a direct
pair references two existing upload datasets and has no verification protocol
or population from which error-rate statistics could honestly be computed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from .owner_pair import OwnerPairRelationship, OperatorRelationshipAssertion
from .primitives import (
    AwareDateTime,
    CanonicalJson,
    FiniteNumber,
    NonEmptyStr,
    NonNegativeFloat,
    NonNegativeInt,
    Sha256,
    StrictFrozenModel,
)


class DirectPairDecision(StrEnum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"


class DirectPairDecisionReason(StrEnum):
    SCORE_GE_FIXED_THRESHOLD = "SCORE_GE_FIXED_THRESHOLD"
    SCORE_LT_FIXED_THRESHOLD = "SCORE_LT_FIXED_THRESHOLD"
    NO_SCORE_PROCESSING_FAILURE = "NO_SCORE_PROCESSING_FAILURE"


class DirectPairThreshold(StrictFrozenModel):
    contract_id: Literal["matcher_threshold"]
    kind: Literal["FIXED_VALUE"]
    value: FiniteNumber
    rule: Literal["MATCH_WHEN_SCORE_GE_THRESHOLD"] = "MATCH_WHEN_SCORE_GE_THRESHOLD"


class DirectPairArtifact(StrictFrozenModel):
    artifact: NonEmptyStr
    path: NonEmptyStr
    sha256: Sha256
    size_bytes: NonNegativeInt
    media_type: NonEmptyStr


class DirectPairSideEvidence(StrictFrozenModel):
    side: Literal["left", "right"]
    sample_id: NonEmptyStr
    dataset_id: NonEmptyStr
    source_sha256: Sha256
    face_count: NonNegativeInt | None
    detection: CanonicalJson | None
    crop_pixel_sha256: Sha256 | None
    embedding_value_sha256: Sha256 | None
    embedding_shape: tuple[Literal[512], ...] | None
    embedding_dtype: Literal["float32"] | None
    failure: NonEmptyStr | None
    quality: CanonicalJson | None
    artifacts: tuple[DirectPairArtifact, ...]

    @model_validator(mode="after")
    def validate_artifacts(self) -> DirectPairSideEvidence:
        names = tuple(item.artifact for item in self.artifacts)
        if len(names) != len(set(names)) or f"{self.side}-source" not in names:
            raise ValueError("side evidence requires one unique source artifact")
        return self


class DirectPairResult(StrictFrozenModel):
    score: FiniteNumber | None
    direction: Literal["HIGHER_IS_MORE_SIMILAR"] = "HIGHER_IS_MORE_SIMILAR"
    failure: NonEmptyStr | None
    failure_side: Literal["left", "right", "left+right", "both"] | None
    decision: DirectPairDecision | None
    decision_reason: DirectPairDecisionReason
    agreement_with_operator_assertion: bool | None

    @model_validator(mode="after")
    def validate_decision(self) -> DirectPairResult:
        if self.score is None:
            if (
                self.failure is None
                or self.failure_side is None
                or self.decision is not None
                or self.decision_reason is not DirectPairDecisionReason.NO_SCORE_PROCESSING_FAILURE
            ):
                raise ValueError("a scoreless comparison requires a processing failure and no decision")
        elif self.failure is not None or self.failure_side is not None or self.decision is None:
            raise ValueError("a scored comparison requires a decision and forbids failure evidence")
        return self


class DirectPairComparisonRun(StrictFrozenModel):
    schema_version: Literal["1.0.0"]
    comparison_id: NonEmptyStr
    run_id: NonEmptyStr
    project_id: NonEmptyStr
    pair_id: NonEmptyStr
    actor_id: NonEmptyStr
    left_sample_id: NonEmptyStr
    right_sample_id: NonEmptyStr
    left_dataset_id: NonEmptyStr
    right_dataset_id: NonEmptyStr
    left_source_sha256: Sha256
    right_source_sha256: Sha256
    relationship: OwnerPairRelationship
    relationship_assertion: OperatorRelationshipAssertion
    matcher_id: NonEmptyStr
    matcher_checkpoint_sha256: Sha256
    matcher_architecture_sha256: tuple[Sha256, ...]
    matcher_device: NonEmptyStr
    preprocessing_id: NonEmptyStr
    preprocessing_definition_sha256: Sha256
    detector_sha256: Sha256
    threshold: DirectPairThreshold
    request_sha256: Sha256
    output_directory: NonEmptyStr
    started_at: AwareDateTime
    ended_at: AwareDateTime
    runtime_seconds: NonNegativeFloat
    left: DirectPairSideEvidence
    right: DirectPairSideEvidence
    result: DirectPairResult
    application_version: NonEmptyStr
    population_error_claims_allowed: Literal[False]
    outcome_correctness_allowed: Literal[False]
    ground_truth_authority: Literal[False]
    prohibited_claims: tuple[
        Literal["FMR", "FNMR", "EER", "ROC", "DET", "OPERATING_POINT"], ...
    ] = ("FMR", "FNMR", "EER", "ROC", "DET", "OPERATING_POINT")
    source_kind: Literal["OWNER_SUPPLIED_LOCAL_INPUT"]
    local_only: Literal[True]

    @model_validator(mode="after")
    def validate_claim_boundary(self) -> DirectPairComparisonRun:
        if self.ended_at < self.started_at:
            raise ValueError("comparison end time precedes its start time")
        if self.left.side != "left" or self.right.side != "right":
            raise ValueError("side evidence is reversed")
        if (
            self.left.sample_id != self.left_sample_id
            or self.right.sample_id != self.right_sample_id
            or self.left.dataset_id != self.left_dataset_id
            or self.right.dataset_id != self.right_dataset_id
            or self.left.source_sha256 != self.left_source_sha256
            or self.right.source_sha256 != self.right_source_sha256
        ):
            raise ValueError("side evidence differs from the frozen pair inputs")
        expected_agreement: bool | None = None
        if (
            self.result.decision is not None
            and self.relationship is not OwnerPairRelationship.NOT_RECORDED
        ):
            asserted_match = self.relationship is OwnerPairRelationship.SAME_PERSON
            expected_agreement = (self.result.decision is DirectPairDecision.MATCH) == asserted_match
        if self.result.agreement_with_operator_assertion != expected_agreement:
            raise ValueError("decision agreement differs from the operator assertion")
        if self.relationship_assertion.relationship is not self.relationship:
            raise ValueError("relationship assertion differs from the admitted pair")
        return self
