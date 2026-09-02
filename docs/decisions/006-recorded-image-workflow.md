# ADR 006: Use one recorded working-sample contract behind the left-task workflow

Date: 2026-09-01

## Context

QualSight's controlled-workstation beta already records owner-supplied images, all 28 OFIQ 1.1.2
measures, SCRFD detection geometry, OFIQ internal geometry, matcher executions, and derived-image
lineage. The existing browser separates these capabilities by backend subsystem. In particular, a
successful owner upload records its sample and assessment but does not make that recorded sample the
active inspector image. The resulting top route strip also wraps at desktop widths and turns into a
horizontal strip on narrow screens. Both behaviors contradict the operator workflow represented by
the three accepted interface renderings.

The accepted renderings and their SHA-256 identities are:

- Concept A, guided Home: `0d7023fe53e5672c9841f8114825e9b72047008163dc3f403d3a5ee2b8860067`.
- Concept B, single-image and pair Comparison Studio: `982b40b763e7d45746545f88c594004f2c6b94cf9096ddaa82c752eebd155aaa`.
- Concept C, dataset and robustness study: `77879cbae10f1c2890325f3354fd26ce34a2df5b70b4d66f271be0a4807608a1`.

## Options

1. Keep subsystem-first pages and add links between them. This retains the upload defect and makes the
   operator reconstruct image, project, matcher, and evidence context across screens.
2. Add browser-only previews and transient comparison state. This would show pixels and results that
   are not bound to the server's recorded hashes and provenance.
3. Use a persistent left-task shell and one recorded working-sample contract for registered and
   owner-supplied images, with task surfaces composed over existing recorded APIs.

## Decision

Choose option 3. Concept A governs Home, Concept B governs the single-image and pair workspace, and
Concept C governs dataset-scale robustness studies. Their typography, hierarchy, dark left rail,
compact active-context header, restrained teal/blue/amber accents, and evidence strip form the visual
contract. Values visible in the renderings are illustrative design content and are never shipped as
application data.

The active image is a recorded working sample. A successful upload binds the returned dataset,
sample, opaque sample identity, project-scoped hash-verified image link, SHA-256, dimensions, source classification, Run, job, measures, and
provenance into the same inspector state used by a registered sample. The browser displays only the
hash-verified server image and reuses the upload's recorded measures. Detection and landmark requests
identify that same recorded sample. A later response may update the interface only if its project and
sample context still match.

Navigation is task-first and persistent: Home, Projects, Inputs, Inspect quality, Compare, Stress test,
Utility, Results and evidence, and Manage. Existing hash routes remain valid or redirect
deterministically. The narrow layout remains vertical and keyboard operable; it does not become a
hidden horizontal route strip.

All add-ons remain in the closed code-owned catalog established by ADR 003. Remote biometric API
connections remain unavailable until a provider-specific egress, credential, retention, audit, and
reproducibility authority exists. Relationship-not-recorded pairs may produce descriptive matcher
evidence but not mated/nonmated population-error claims. Generative identity and synthetic-subject
operations are outside the repository's admitted scope.

## Consequences

- Upload, registered-sample selection, overlays, measures, stress tests, and pair work share one
  recorded context instead of parallel browser-only states.
- UnifiedQualityScore remains visible but does not replace the other 27 recorded measures.
- JSON remains downloadable evidence and is not the primary operator result.
- The left rail improves task discovery while preserving old bookmarked routes.
- The implementation must pass the real-data acceptance extension in
  `docs/IMAGE_WORKFLOW_ACCEPTANCE.md`; visual similarity alone cannot close a requirement.
- The reference renderings are design evidence, not sources for runtime counts, scores, identities, or
  statuses.
