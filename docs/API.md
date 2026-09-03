# QualSight local API

The API is a loopback, same-origin interface for the QualSight console. It is not a public or remote service contract. The running server publishes the exhaustive machine-readable schema at `/openapi.json`; this document records the operator-relevant boundaries that clients must preserve. The beta-3 console uses API contract `2.0.0-beta.1`, reported by `/api/version` and the `X-QualSight-API-Version` response header.

## Addressing and errors

Recorded biometric evidence is always addressed under its project:

```text
/api/projects/{project_id}/datasets/{dataset_id}
/api/projects/{project_id}/datasets/{dataset_id}/samples
/api/projects/{project_id}/samples/{sample_id}
/api/projects/{project_id}/samples/{sample_id}/image
/api/projects/{project_id}/samples/{sample_id}/detection
/api/projects/{project_id}/samples/{sample_id}/ofiq-landmarks
/api/projects/{project_id}/samples/{sample_id}/quality
/api/projects/{project_id}/derived-sets/{derived_set_id}
/api/projects/{project_id}/owner-pair-comparisons/{comparison_id}
/api/projects/{project_id}/direct-pair-runs/{run_id}
/api/projects/{project_id}/runs/{run_id}
/api/projects/{project_id}/matcher-runs/{run_id}/metrics
/api/projects/{project_id}/utility-analyses/{analysis_id}
/api/projects/{project_id}/cross-matcher/{analysis_id}
/api/projects/{project_id}/reports/{report_id}/file
```

The API returns 404 when the evidence is absent from the named project, including when the identifier exists in another project. Retired unscoped recorded-sample, dataset, Run, audit, protocol, analysis, matrix, matcher-result, figure, report, export, bundle, reproduction, and direct-comparison routes also return 404. Recorded-sample, dataset, operation, Run, add-on, acquisition-receipt, and system-event public projections contain immutable identities, hashes, sizes, state, and same-origin links without host filesystem locations. Complete path-bearing Run, receipt, and failure records remain private in the local evidence store.

`GET /api/samples/registered` returns an opaque `registered_sample_id` for each exact globally registered input artifact. `GET /api/samples/image`, `GET /api/detection`, and `GET /api/ofiq-landmarks` accept that identity as the `registered_sample_id` query parameter. Ordinary sample, image, geometry, and assessment routes never accept a workstation path. The explicit project-scoped owner-directory and owner-manifest import requests are the narrow exception: the local operator supplies `root` or `manifest_path` to admit an existing controlled dataset. Those inputs are not echoed as public storage locations. Project-recorded owner and derived images use their project-scoped sample routes instead.

Validation failures use 422, immutable or lifecycle conflicts use 409, unavailable execution dependencies use 502 or readiness 503, and missing/project-mismatched evidence uses 404. Errors returned to the browser omit private paths and operating-system exception text. A readiness dependency failure uses only the stable machine code `DATABASE_UNAVAILABLE` or `STORAGE_UNAVAILABLE`; every purge journal whose state is not `COMPLETED` makes readiness return 503. Readiness re-hashes the registered OFIQ binary and configuration, so a same-size byte change also returns 503.

## Project and input workflow

- `POST /api/projects`, then `POST /api/projects/{id}/open`, creates the active research boundary.
- `POST /api/projects/{id}/datasets/import-registered` imports the registered LFW record without copying its source images.
- `POST /api/quality/assess-upload` admits and measures one bounded owner image and returns the recorded sample ID, dimensions, hash, 28 OFIQ measures, and project-scoped sample/image links.
- `POST /api/projects/{id}/datasets/import-owner-directory` and `import-owner-manifest` admit real operator-controlled datasets under explicit identity mapping.
- Owner pair-protocol creation preflights the recorded identity counts and refuses more than 20,000 source samples or 20,000 generated pairs before pair records or a protocol receipt are materialized.
- `POST /api/projects/{id}/purge` requires the exact project ID as confirmation and returns a path-free verified receipt.

## Quality, geometry, and stress

- `POST /api/quality/assess` accepts `registered_sample_id`, `purpose_scope`, and an optional `project_id`; it never accepts an image path. A native failure returns a bounded reason code and safe message plus the immutable private-event identity. Native stderr and exact local paths remain only in the private event record.
- `POST /api/projects/{id}/datasets/{dataset_id}/quality-batch` records a bounded OFIQ batch.
- Job status, cancel, distribution, drill-down, results, and measure-catalog routes use
  `/api/projects/{project_id}/jobs/{job_id}...`, verify project ownership, and bind their rows to the
  recorded hash-verified Parquet snapshot. Retired global `/api/jobs/{job_id}...` routes return 404.
- Job projections include `quality_result_available`. Clients request result rows, charts, or the quality-measure catalog only when this field is true; recovered operation-only jobs remain visible with the field false.
- Project sample image, SCRFD detection, OFIQ landmarks, and quality routes drive the human-readable inspector.
- `POST /api/projects/{id}/datasets/{dataset_id}/transformations` executes the seven closed real-input operators through durable bounded admission. The beta refuses requests whose selected samples × requested transform levels exceed 20,000 output files; `selection=all` is count-checked before sample rows are materialized. A completed promoted output is recovered only from its integrity-checked prepared receipt. Derived-set detail returns parent/transform/hash chains and project-scoped derivative links without local paths.

## Pair comparison

- `POST /api/projects/{id}/owner-pair-admissions` binds two recorded project samples and one closed relationship value.
- `POST /api/projects/{id}/owner-pair-comparisons` starts a matcher/preprocessing/threshold Run. Matcher execution has a configured hard deadline in a terminable child process. A process death after output promotion is reconciled on startup from the fsynced prepared receipt before the operation is marked complete.
- Project-scoped status, evidence, direct-Run, raw-manifest, and raw-artifact routes expose immutable recorded results.
- Operator relationship assertions remain unverified and `outcome_correctness_allowed` is always false. Agreement is descriptive only.

## Dataset studies

Project-scoped routes freeze verification and identification protocols, execute matcher matrices and searches, import registered external-score artifacts, record controlled degradation studies, compute cross-matcher contrasts, and create quality-utility analyses. Every Run binds the exact matcher, preprocessing, protocol pair universe, threshold semantics, metric registry, and artifacts used. Each asynchronous mutation is admitted as a durable operation; a same-request retry resumes missing matrix leaves or reconciles exact prepared output rather than silently recomputing completed work.

Recorded detail routes retain the same project boundary:

```text
/api/projects/{project_id}/protocols/{protocol_id}[/pairs]
/api/projects/{project_id}/identification-protocols/{protocol_id}
/api/projects/{project_id}/identification-searches/{search_id}[/candidates]
/api/projects/{project_id}/degradation-analyses/{analysis_id}
/api/projects/{project_id}/matrices/{matrix_id}[/sets]
/api/projects/{project_id}/matcher-runs/{run_id}/{metrics|comparisons|errors|erc}
/api/projects/{project_id}/matcher-runs/{run_id}/pairs/{pair_id}/{evidence|crops/{side}}
/api/projects/{project_id}/utility-analyses/{analysis_id}[/pairs]
/api/projects/{project_id}/cross-matcher/{analysis_id}[/pairs]
```

Project-scoped annotations, figures/files, dashboards, report files, exports, bundle files, reproductions, operations, Runs/audit, and experiments follow the same rule. Tabular export creation is available only as an origin-checked `POST /api/projects/{project_id}/exports/{category}` request. The default representation remains the attachment for direct clients; `Accept: application/json` returns a path-free receipt with the recorded hash, size, classification, and a project-scoped `GET .../exports/{category}/file` link. That GET verifies and serves only the already-created prepared output and never creates export state. Retired GET creation routes return 404 without producing files. Annotation writes accept only the closed `Project` and `PROTOCOL_PAIR` subject types, verify that the subject belongs to the named project, and commit the annotation with its audit event in one SQLite transaction. Reproduction accepts a recorded `bundle_id`; it never accepts an arbitrary filesystem path. The server authorizes the project and recorded bundle before reading bytes or starting work. Public reproduction POST/list projections omit dependency locations and native error text, publish a `missing_or_drifted_count`, and point the operator to private local evidence when diagnostics exist. Binary denials return 404 before opening bytes and do not emit content, digest, or classification headers.

Report and `.qsexp` bundle file routes are explicit governed downloads for the local operator. Their bytes may contain deployment-local provenance paths required for forensic reproduction. They are not path-free API projections and must not be forwarded or exposed as a remote service artifact without a separate disclosure review.

Figure, report, export, and bundle publication commits the artifact row and its audit event atomically after no-clobber prepared output promotion. Restart and retry reuse the actor and request identity recorded with that publication; malformed, foreign-project, partial, or unbound files are refused and remain discoverable by project purge.

Utility overlay identity uses the frozen protocol `pairs_sha256` as pair-universe authority. Comparison Parquet SHA-256 remains per-Run provenance. Incompatible or incomplete overlay candidates are refused rather than merged.

## Add-ons, runtime, and connections

- `/api/addons` returns the closed matchers, verification datasets, preprocessing profiles, transformations, analysis parameter contracts, and zero admitted connections.
- Matcher asset routes expose explicit source pull, checkpoint acquisition, load, unload, and status operations with durable system events. Public status distinguishes `MANAGED` from `REGISTERED_EXTERNAL` checkpoint location state without returning either location.
- `/api/health`, `/api/ready`, and `/api/metrics` expose operational state without the data-directory lease path.
- `/api/system-events` is the durable receipt/failure feed used by Manage.

There is no generic plug-in loader and no endpoint for adding an arbitrary remote biometric provider.

## Beta-2 migration boundary

API contract `2.0.0-beta.1` is intentionally incompatible with the beta-2 `1.0.0` browser contract. Clients must replace registered-image path parameters and `image_path` assessment bodies with `registered_sample_id`; use project-scoped evidence routes; resolve job results through `/api/projects/{project_id}/jobs/{job_id}/results`; and stop reading database, Parquet, bundle, checkpoint, dataset-root, or receipt paths from public responses. Installed beta-2 clients are not compatible with a beta-3 server and must be upgraded with the console and backend together.
