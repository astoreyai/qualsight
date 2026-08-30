# Project purge removes managed biometric data and retains an audit shell

Date: 2026-08-26

## Context

The previous purge enumerated many file artifacts but omitted derived sets, degradation analyses, identification searches, and most corresponding database rows. It could therefore attest success while sensitive scientific metadata and managed derivatives remained.

## Options

1. Keep metadata and delete only files. Rejected because the retained rows contain biometric workflow structure and can point to data that no longer exists.
2. Delete the entire project record and audit history. Rejected because it removes the evidence needed to establish who requested the purge and what the application verified.
3. Delete all application-managed samples, results, embeddings, comparisons, analyses, reports, owner-input copies, and scientific metadata, while retaining only the project/version shell, audit events, and purge attestation.

## Decision

Use option 3. Use one admission gate to make purge mutually exclusive with new project mutations, including quality assessment, upload, and reproduction routes whose URLs do not carry a project id. Refuse purge while the current process has active work for the project. Content-addressed owner inputs carry one reference per admitting project. A process-wide owner-file mutation lock covers every owner-file admission and the complete purge target-compute, unlink, and metadata interval, so a second project cannot acquire a reference to bytes being deleted. Delete recorded files first, verify their absence, delete the scoped metadata, verify zero residual rows in every scoped table, and record a failed attestation when either phase is incomplete.

## Why

The result matches the product requirement to purge managed biometric data without falsely claiming secure erasure outside the application's control. The retained shell supplies a minimal, attributable tombstone.

## Consequences

- Completed job, run, dataset, matcher, transformation, identification, report, and export records are removed with their managed files.
- Shared owner-input bytes survive until the final project reference is purged. The lock is process-wide; deployments with multiple API worker processes require an inter-process coordinator before they can preserve this guarantee.
- The purge explicitly does not scrub SQLite free pages, filesystem journals, backups, or source images outside QualSight.
- A verified purge returns success only after file and metadata checks pass; incomplete purge returns a conflict response and leaves the failure details in the attestation.
