# Recorded image-workflow beta acceptance extension

**Extension version:** v1.0

**Authority:** controlled single-user workstation beta

**Decision:** `docs/decisions/006-recorded-image-workflow.md`

This extension adds operator-workflow acceptance without changing the frozen 81-item PRD-derived
registry in `REQUIREMENTS.md`. A row is complete only when its named automated evidence passes on real,
rights-admitted input and the recorded artifact values are checked. A rendered control without a real
backend contract does not satisfy a row.

## Frozen design evidence

| Surface | Role | SHA-256 |
|---|---|---|
| Concept A | Guided Home and first-task selection | `0d7023fe53e5672c9841f8114825e9b72047008163dc3f403d3a5ee2b8860067` |
| Concept B | Single-image and pair Comparison Studio | `982b40b763e7d45746545f88c594004f2c6b94cf9096ddaa82c752eebd155aaa` |
| Concept C | Dataset and robustness study | `77879cbae10f1c2890325f3354fd26ce34a2df5b70b4d66f271be0a4807608a1` |

The renderings govern hierarchy and interaction structure. Runtime values come only from recorded
project APIs and artifacts.

## Acceptance registry

| ID | Required behavior | Scientific and safety boundary | Automated evidence |
|---|---|---|---|
| QS-IMG-001 | A persistent left task rail reaches Home, Projects, Inputs, Inspect quality, Compare, Stress test, Utility, Results/evidence, and Manage without route wrapping. | Active project and evidence context remain visible; legacy hash routes remain deterministic. | `scripts/observe_console.py` |
| QS-IMG-002 | Uploading one real admitted photo makes its recorded sample the active inspector image and displays the canonical hash-verified server bytes. | The selected project, opaque sample identifier, project-scoped image link, SHA-256, dimensions, source classification, and provenance all come from the upload response. Server-private storage paths remain outside the public contract. | `scripts/validate_upload_working_sample.py`; `backend/tests/api/test_owner_supplied_real.py` |
| QS-IMG-003 | The active uploaded sample displays all 28 recorded OFIQ 1.1.2 measures plus independently identifiable SCRFD five-point and OFIQ ADNet-98 geometry. | Upload measures are reused without a second OFIQ assessment; absent measures remain explicit failures and are not replaced with zero. | `scripts/validate_upload_working_sample.py`; `backend/tests/api/test_owner_supplied_real.py` |
| QS-IMG-004 | Two real images are admitted as one recorded pair with one asserted relationship: same person, different people, or relationship not recorded. | An operator assertion remains distinct from publisher identity; an unknown relationship cannot support FMR, FNMR, DET, false-match, or false-non-match claims. | `backend/tests/api/test_matcher_api_real.py` |
| QS-IMG-005 | The active recorded sample can enter the controlled seven-operator stress workflow and display hash-verified original and derived images with recorded measure trajectories. | Single-sample quality response remains distinct from population matcher-error response. | `scripts/observe_console.py` |
| QS-IMG-006 | Utility measure selection is derived from scalar measures recorded in the selected completed quality job and defaults to UnifiedQualityScore only when present. | A stale or absent measure is rejected at the API boundary and cannot admit an analysis. | `backend/tests/api/test_matcher_api_real.py`; `scripts/observe_console.py` |
| QS-IMG-007 | Quality, pair, stress, utility, and Run evidence are readable structured views; raw JSON is an explicit secondary view or download. | Every chart value retains metric identifier, unit, Run and active-context lineage. | `scripts/observe_console.py` |
| QS-IMG-008 | Manage presents closed-catalog matchers, datasets, preprocessing, transformations, connections policy, runtime/storage, and audit state with truthful lifecycle controls. | No arbitrary package/module/command loader or unauthorized biometric egress is admitted. | `scripts/observe_console.py`; `backend/tests/api/test_matcher_api_real.py` |
| QS-IMG-009 | The complete workflow passes keyboard, focus, zoom, narrow-layout, stale-response, error/retry, purge, and packaged-offline checks using real recorded data. | Completion applies only to the controlled single-user workstation beta and makes no production, ATO, or representative-user claim. | `scripts/observe_console.py`; `scripts/validate_upload_working_sample.py` |

## Visible-element source mapping

| Visible element family | Recorded source or honest state |
|---|---|
| Product name, beta boundary, application and OFIQ versions | Application metadata and `/api/health`; never inferred from browser build labels. |
| Active workspace header, visibility, project details | Active project response and project state. No selected project produces a named empty state. |
| Left-rail active item, stage markers, disabled reasons | Current hash route plus explicit prerequisites from active project, sample, pair, protocol, quality job, matcher Run, and evidence context. |
| Home task cards | Deterministic links to admitted workflows; unavailable work names its missing prerequisite. |
| Recent recorded work | Project-scoped Run, utility, degradation, figure, report, and bundle feeds. Empty, loading, error, and retry states remain distinct. |
| Workspace counts | Live add-on catalog, OFIQ measure catalog, transformation catalog, and active-project record feeds. |
| Image preview, filename, dimensions, SHA-256, recorded state | Hash-verified sample-image endpoint plus the selected sample's recorded metadata and provenance. |
| SCRFD badge, box, five points | Detection response and its detector/model/preprocessing provenance. |
| OFIQ face and 98 landmarks | OFIQ landmark response and its engine/model provenance. |
| UnifiedQualityScore ring | Recorded UnifiedQualityScore scalar from the active OFIQ assessment. It is not a pass/fail policy. |
| Measure groups, scalar bars, native values, failures | The active assessment's 28 measure records and code-owned display grouping; native units never share an invented common axis. |
| Image A, Image B, relationship, comparison state | Atomic pair-admission record, each selected sample, operator assertion provenance, and matcher Run state. |
| Matcher and preprocessing choices | Compatible READY records from `/api/addons`; every chosen combination creates an immutable Run. |
| Score, threshold, decision, failure | Matcher artifact and registered threshold provenance. Relationship assertion remains a separate field. |
| Stress-test operators and parameters | Backend-published controlled-transformation contracts and exact admitted parameter domains. |
| Original/derived gallery and trajectories | Hash-verified sample bytes, derived-set lineage, and recorded OFIQ job artifacts. |
| Population response chart | Frozen pair protocol, matcher Run, thresholds, quality job, and degradation-analysis artifacts; absent evidence produces an unavailable state. |
| Utility measure selector and charts | Selected completed quality-job Parquet schema, metric registry, and utility-analysis artifacts. |
| Evidence strip and structured record | Active Run/job/analysis provenance, hashes, timings, warnings, repository state, and audit lineage. |
| Add-on lifecycle and receipts | Closed `/api/addons` catalog, governed acquisition receipts, verified asset identities, runtime leases, and lifecycle errors. |
| Connections | `NOT_ADMITTED_IN_BETA` policy state until provider-specific authority exists; no actionable connection control. |
| Runtime, storage, recovery, and audit | Health, registry, database, data-directory lease, network guard, disk, operation, recovery, and append-only audit records. |

## Release use

The release gate records the exact commit and tree that passed this extension. Browser evidence is
written outside distributable archives. Owner inputs, face images, embeddings, databases, downloaded
models, derived images, browser profiles, and local observations remain excluded by the release package
policy.
