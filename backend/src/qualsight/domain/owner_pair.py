"""Immutable operator-asserted relationships between two owner-supplied images."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import model_validator

from .comparison import ComparisonLabel
from .primitives import AwareDateTime, NonEmptyStr, Sha256, StrictFrozenModel


class OwnerPairRelationship(StrEnum):
    SAME_PERSON = "SAME_PERSON"
    DIFFERENT_PEOPLE = "DIFFERENT_PEOPLE"
    NOT_RECORDED = "NOT_RECORDED"


class OperatorRelationshipAssertion(StrictFrozenModel):
    state: Literal["OPERATOR_ASSERTED_UNVERIFIED"]
    relationship: OwnerPairRelationship
    asserted_by: NonEmptyStr
    asserted_at: AwareDateTime


class OwnerPairAdmission(StrictFrozenModel):
    schema_version: Literal["1.0.0"]
    pair_id: NonEmptyStr
    project_id: NonEmptyStr
    left_sample_id: NonEmptyStr
    right_sample_id: NonEmptyStr
    left_sha256: Sha256
    right_sha256: Sha256
    relationship: OwnerPairRelationship
    comparison_label: ComparisonLabel | None
    relationship_assertion: OperatorRelationshipAssertion
    population_error_claims_allowed: Literal[False]
    outcome_correctness_allowed: Literal[False]
    source_kind: Literal["OWNER_SUPPLIED_LOCAL_INPUT"]
    local_only: Literal[True]

    @model_validator(mode="after")
    def validate_claim_boundary(self) -> OwnerPairAdmission:
        expected_label = {
            OwnerPairRelationship.SAME_PERSON: ComparisonLabel.MATED,
            OwnerPairRelationship.DIFFERENT_PEOPLE: ComparisonLabel.NON_MATED,
            OwnerPairRelationship.NOT_RECORDED: None,
        }[self.relationship]
        if self.comparison_label != expected_label:
            raise ValueError("relationship and comparison label disagree")
        if self.relationship_assertion.relationship is not self.relationship:
            raise ValueError("relationship assertion differs from the pair relationship")
        if self.left_sample_id == self.right_sample_id:
            raise ValueError("a direct pair cannot compare a sample with itself")
        if self.left_sha256 == self.right_sha256:
            raise ValueError("a direct pair requires two different image byte identities")
        return self


class OwnerPairAdmissionError(RuntimeError):
    """Base error for a refused direct owner-pair admission."""


class OwnerPairNotFound(OwnerPairAdmissionError):
    """The caller did not identify two owner inputs in the requested project."""


class OwnerPairIntegrityError(OwnerPairAdmissionError):
    """Recorded owner-input evidence no longer agrees with the stored bytes."""


class OwnerPairValidationError(OwnerPairAdmissionError):
    """The requested pair violates the direct-pair domain contract."""
