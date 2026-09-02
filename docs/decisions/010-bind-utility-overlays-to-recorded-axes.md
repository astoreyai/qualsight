# ADR 010: Bind utility overlays to recorded comparison axes

Status: Accepted on 2026-09-01

## Context

Utility curves are meaningful only when the compared analyses use the same frozen pair universe and differ on an axis the operator explicitly selected. The comparison Parquet digest is execution provenance, not the pair-universe identity: it can legitimately change when the matcher or preprocessing condition changes.

## Decision

Every utility summary records a versioned overlay identity. The frozen protocol `pairs_sha256` identifies the pair universe. The matcher Run comparison digest, matcher, preprocessing condition, quality measure, quality policy, target FMR, threshold rule, and metric-registry digest remain independently bound provenance.

The console classifies a candidate as matcher, preprocessing, measure, or composite only from exact recorded differences. An operator must explicitly include an eligible overlay. Candidates with a different pair universe, an unselected causal axis, incomplete legacy identity, or drifted artifacts remain visible with a refusal reason and are not fetched as curve detail.

## Consequences

- A changed comparison digest does not falsely imply a changed pair universe.
- Composite overlays are descriptive and are not presented as single-axis causal evidence.
- Old records without the complete identity remain readable but are not silently overlaid.
- Actual quality and comparison artifacts are rehashed before an overlay summary is accepted.

## Validation

The real LFW backend gate covers matcher, preprocessing, composite, measure, legacy, and artifact-drift cases. The Chromium gate confirms explicit inclusion, refusal explanations, bounded desktop/mobile layout, and that incompatible detail is never requested.
