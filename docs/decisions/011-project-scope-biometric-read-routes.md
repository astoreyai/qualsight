# ADR 011: Project-scope recorded biometric read routes

Status: Accepted on 2026-09-01

## Context

Recorded samples, derived images, direct comparisons, and dataset manifests previously had read routes addressed by a sample identifier or absolute filesystem path. Those routes allowed a caller holding another project's identifier or path to retrieve biometric bytes or evidence. Several responses also disclosed workstation paths even though the browser needs only immutable identities and same-origin links.

## Decision

Recorded biometric reads are addressed by both project ID and opaque evidence ID. The API verifies the recorded project relationship before returning a dataset, sample, image, geometry record, quality record, derived set, direct-comparison status, direct Run, manifest, or raw artifact. A foreign project and every retired unscoped route return 404.

Public dataset and sample records omit local paths. They retain immutable IDs, SHA-256 digests, byte sizes, media types, source classification, scientific metadata, and project-scoped links. Derived chains omit parent paths. Owner-upload responses return sample IDs and project-scoped links rather than managed storage paths. Full private path evidence remains in the local store for integrity checks and verified purge.

Exact globally registered inputs use an opaque `registered_sample_id` for image, detection, OFIQ-landmark, and single-image assessment routes. The browser API accepts no host path. Recorded project samples continue to use their project-scoped sample identities.

## Consequences

- Project IDs now constrain browser-visible biometric evidence, even in the single-user local beta.
- Frontend components follow server-returned links or provide project ID plus evidence ID.
- Legacy clients using unscoped recorded-sample or direct-comparison URLs fail closed and must migrate.
- Internal integrity, recovery, and purge code continues to use private paths without publishing them.

## Validation

Real LFW and real owner-pair tests verify same-project reads, foreign-project 404 responses, retired-route 404 responses, byte hashes, geometry execution, derived-chain privacy, and direct raw-artifact access. Browser validators use the project-scoped links returned by the API.
