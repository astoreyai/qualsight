# ADR 007: Record direct pair comparisons without coercing them into dataset Runs

Date: 2026-09-01

## Context

An owner-pair admission joins two independently recorded owner-supplied images. Each upload currently
has its own dataset, sample, source hash, OFIQ assessment, and Run. The general `CompletedRun` contract
binds one experiment and one dataset manifest. A direct two-image matcher execution therefore cannot
use that contract without falsely selecting one side's dataset as the authority for both images.

The existing matcher matrix contract is also unsuitable. It requires a frozen verification protocol
with publisher- or dataset-authoritative labels and supports population statistics. A direct pair may
instead carry `NOT_RECORDED`, which permits descriptive score and matcher-decision evidence but no
outcome-correctness, FMR, FNMR, DET, false-match, or false-non-match claim.

## Options

1. Bind the comparison to the left image's dataset. This loses the right image's independent dataset
   lineage and makes the Run contract false.
2. Create a bridge dataset or duplicate both sample records. This manufactures a dataset identity that
   the operator did not admit and weakens the original sample lineage.
3. Record a domain-specific immutable direct-pair comparison Run that references the admitted pair,
   both sample and dataset identities, the matcher and preprocessing identities, threshold provenance,
   execution artifacts, decision, failures, repository state, and audit event.

## Decision

Choose option 3. `DirectPairComparisonRun` is a project-scoped scientific execution record, but it does
not claim to satisfy the single-dataset `CompletedRun` schema. It uses a content-derived identifier,
retains the two source dataset and sample identities, and records only one direct pair score or an
explicit preprocessing/template failure. The operator relationship assertion and matcher decision are
separate fields. A disagreement is descriptive and is not promoted to publisher-authoritative truth.

Every direct comparison records the selected compatible READY matcher and preprocessing leaf, exact
registered asset identities, source hashes, crops or explicit failures, threshold identity and value,
runtime timing, environment/repository identity, and an audit event. Repeating the same execution
identity is idempotent; changing matcher, preprocessing, or registered threshold creates a distinct
immutable record. The record appears in project execution inventory and exposes structured evidence
plus an explicit raw JSON action.

No direct-pair comparison record exposes population error rates. `NOT_RECORDED` additionally disables
outcome-correctness language. Direct comparisons never create a protocol, subject identity, bridge
dataset, or duplicated sample.

## Consequences

- The Comparison Studio can honestly compare two separately uploaded images.
- Existing dataset-scale matcher Runs and their population metrics remain unchanged.
- Reporting consumers must recognize the direct-pair record kind explicitly instead of assuming every
  scientific execution has exactly one dataset.
- Project purge removes direct-pair Runs before pair and sample records; shared content-addressed bytes
  remain governed by their existing project references.
