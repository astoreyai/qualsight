# ADR 004: Filter internal and sensitive records from release archives

## Context

QualSight's internal session records, append-only ATG record, runtime performance records, local browser
observations, and real-result fixtures support development and validation of the controlled-workstation
beta. Those records can contain operator attribution, deployment-local paths, public-dataset subject
identifiers, quality measures, and comparison scores. Keeping the records in the controlled repository
preserves the audit chain, but placing them in a distributable source archive would contradict the release
boundary and disclose internal or local derived biometric evidence.

## Options

1. Archive the complete Git tree, which is simple but redistributes sensitive runtime evidence.
2. Delete or rewrite the evidence before commit, which breaks the exact append-only audit chain.
3. Build a declared projection of the exact commit that excludes governed sensitive paths.

## Decision

On 2026-08-29, choose option 3. `resources/security/release-package-policy-v1.json` is the source-of-truth
exclusion contract. `scripts/create_release_source_archive.py` applies it to the exact release commit,
verifies the resulting tar member paths, rejects nonregular members and prohibited biometric/model/data/
native/screenshot suffixes or credential indicators, and refuses overwrite. The release manifest records
the same policy and its excluded content classes.

## Why

This preserves exact local governance and validation evidence without distributing it. The tradeoff is that the source
tarball is a reproducible policy-filtered projection of the recorded Git tree, not a complete developer
clone. That limitation is preferable to either weakening the audit chain or mislabeling sensitive evidence
as redistributable source.

## Consequences

The source archive excludes the policy-listed agent/session files, all `atg/` records,
`backend/tests/fixtures/`, and `docs/performance/`; the console archive contains only the compiled static
console. Full repository governance, machine-specific performance evidence, and real-fixture tests require
the controlled Git checkout, while install/runtime code remains present in the source archive. Adding or
removing an exclusion requires a new policy version, a matching release-manifest change, a source-archive
regression, and a new append-only ATG record. Exact deployment registry paths remain because current runtime
resolution depends on them; this decision does not make the archive portable or authorize general
redistribution.
