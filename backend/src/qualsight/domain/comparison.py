"""Verification comparison and protocol schemas (PRD §§15–18, 21, 24; QS-C20).

These entities keep four things that are often conflated strictly apart:

* the *label* of a pair (MATED / NON_MATED) comes from the protocol, never
  from a score or a threshold;
* a *score* exists only when both templates were produced and compared;
  otherwise the comparison carries an explicit *processing failure* and no
  number;
* an *operating threshold* is a policy applied when metrics are computed; it is
  never stored on a comparison;
* the *provenance marker* separates QualSight-executed comparisons from
  imported, externally computed scores permanently.

The identification-compatible entities (candidate lists) share the same sample
and subject identity model so that 1:N work later does not need a schema break.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import model_validator

from .primitives import (
    ArtifactIdentity,
    AwareDateTime,
    CanonicalJson,
    FiniteNumber,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    Sha256,
    StrictFrozenModel,
)


class ComparisonLabel(StrEnum):
    MATED = "MATED"
    NON_MATED = "NON_MATED"


class PairPolicy(StrEnum):
    OFFICIAL_PROTOCOL_LIST = "OFFICIAL_PROTOCOL_LIST"
    ALL_CROSS_SUBJECT_PAIRS = "ALL_CROSS_SUBJECT_PAIRS"
    ENROLLMENT_PROBE = "ENROLLMENT_PROBE"


class ProtocolCategory(StrEnum):
    UNRESTRICTED_WITH_LABELED_OUTSIDE_DATA = "UNRESTRICTED_WITH_LABELED_OUTSIDE_DATA"
    UNRESTRICTED_WITH_NO_OUTSIDE_DATA = "UNRESTRICTED_WITH_NO_OUTSIDE_DATA"
    IMAGE_RESTRICTED = "IMAGE_RESTRICTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ProcessingFailureKind(StrEnum):
    FAILURE_TO_ACQUIRE = "FAILURE_TO_ACQUIRE"
    FAILURE_TO_DETECT = "FAILURE_TO_DETECT"
    FAILURE_TO_TEMPLATE = "FAILURE_TO_TEMPLATE"
    FAILURE_TO_COMPARE = "FAILURE_TO_COMPARE"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    NOT_EXECUTED = "NOT_EXECUTED"


class ProvenanceMarker(StrEnum):
    QUALSIGHT_EXECUTED = "QUALSIGHT_EXECUTED"
    EXTERNALLY_COMPUTED = "EXTERNALLY_COMPUTED"


class ScoreDirection(StrEnum):
    HIGHER_IS_MORE_SIMILAR = "HIGHER_IS_MORE_SIMILAR"
    LOWER_IS_MORE_SIMILAR = "LOWER_IS_MORE_SIMILAR"


class ThresholdPolicyKind(StrEnum):
    FIXED_VALUE = "FIXED_VALUE"
    TARGET_FMR = "TARGET_FMR"
    TARGET_FNMR = "TARGET_FNMR"
    EQUAL_ERROR_RATE = "EQUAL_ERROR_RATE"


class ProtocolPair(StrictFrozenModel):
    """One frozen pair of the protocol; the label is protocol authority."""

    pair_id: NonEmptyStr
    protocol_id: NonEmptyStr
    fold: PositiveInt | None
    source_order: PositiveInt
    label: ComparisonLabel
    left_sample_id: NonEmptyStr
    right_sample_id: NonEmptyStr
    left_subject_id: NonEmptyStr | None
    right_subject_id: NonEmptyStr | None
    left_sha256: Sha256
    right_sha256: Sha256

    @model_validator(mode="after")
    def validate_label_against_subjects(self) -> ProtocolPair:
        subjects_recorded = self.left_subject_id is not None and self.right_subject_id is not None
        if (self.left_subject_id is None) != (self.right_subject_id is None):
            raise ValueError("pair subject identities must either both be recorded or both be absent")
        if subjects_recorded:
            same_subject = self.left_subject_id == self.right_subject_id
            if self.label is ComparisonLabel.MATED and not same_subject:
                raise ValueError("a MATED pair must join two samples of the same recorded subject")
            if self.label is ComparisonLabel.NON_MATED and same_subject:
                raise ValueError("a NON_MATED pair must join two samples of different recorded subjects")
        if self.left_sample_id == self.right_sample_id:
            raise ValueError("a pair cannot compare a sample with itself")
        return self


class VerificationProtocolRecord(StrictFrozenModel):
    """A frozen 1:1 protocol over recorded dataset samples."""

    protocol_id: NonEmptyStr
    project_id: NonEmptyStr
    dataset_id: NonEmptyStr
    name: NonEmptyStr
    pair_policy: PairPolicy
    category: ProtocolCategory
    pair_manifest: ArtifactIdentity
    protocol_authority: tuple[ArtifactIdentity, ...]
    pair_count: PositiveInt
    mated_count: NonNegativeInt
    nonmated_count: NonNegativeInt
    fold_count: PositiveInt | None
    unique_sample_count: PositiveInt
    unique_subject_count: PositiveInt | None
    pairs_sha256: Sha256
    limitations: tuple[NonEmptyStr, ...]
    created_at: AwareDateTime
    metadata: CanonicalJson

    @model_validator(mode="after")
    def validate_counts(self) -> VerificationProtocolRecord:
        if self.mated_count + self.nonmated_count != self.pair_count:
            raise ValueError("mated and non-mated counts must sum to the pair count")
        return self


class Comparison(StrictFrozenModel):
    """One executed (or explicitly failed) comparison of a protocol pair."""

    comparison_id: NonEmptyStr
    run_id: NonEmptyStr
    protocol_id: NonEmptyStr
    pair_id: NonEmptyStr
    label: ComparisonLabel
    left_sample_id: NonEmptyStr
    right_sample_id: NonEmptyStr
    score: FiniteNumber | None
    score_direction: ScoreDirection
    failure: ProcessingFailureKind | None
    failure_detail: NonEmptyStr | None = None
    provenance: ProvenanceMarker
    matcher_component_id: NonEmptyStr
    preprocessing_condition_id: NonEmptyStr

    @model_validator(mode="after")
    def validate_score_or_failure(self) -> Comparison:
        if (self.score is None) == (self.failure is None):
            raise ValueError("a comparison carries either a finite score or an explicit processing failure")
        if self.failure is None and self.failure_detail is not None:
            raise ValueError("failure detail requires a failure")
        return self


class ThresholdPolicy(StrictFrozenModel):
    """An operating-threshold policy, applied at metric time, never stored on comparisons."""

    policy_id: NonEmptyStr
    kind: ThresholdPolicyKind
    value: FiniteNumber
    score_direction: ScoreDirection
    description: NonEmptyStr

    @model_validator(mode="after")
    def validate_value(self) -> ThresholdPolicy:
        if self.kind in {ThresholdPolicyKind.TARGET_FMR, ThresholdPolicyKind.TARGET_FNMR} and not 0 < self.value < 1:
            raise ValueError("target error-rate policies need a value strictly between 0 and 1")
        return self


class CandidateListEntry(StrictFrozenModel):
    """1:N-compatible entity: one ranked gallery candidate for a probe."""

    run_id: NonEmptyStr
    probe_sample_id: NonEmptyStr
    gallery_sample_id: NonEmptyStr
    rank: PositiveInt
    score: FiniteNumber
    score_direction: ScoreDirection
    mated: bool | None
    provenance: ProvenanceMarker
    kind: Literal["CANDIDATE_LIST_ENTRY"] = "CANDIDATE_LIST_ENTRY"

    @model_validator(mode="after")
    def validate_probe_gallery(self) -> CandidateListEntry:
        if self.probe_sample_id == self.gallery_sample_id:
            raise ValueError("a probe cannot be its own gallery candidate")
        return self
