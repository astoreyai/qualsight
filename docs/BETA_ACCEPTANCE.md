# QualSight 0.1.0-beta.2 — beta acceptance record

Every row below names the evidence that closes it. Identifiers are content hashes recorded in the
append-only `atg/ledger.jsonl` and in the persistent store on the reference workstation;
gate files live in `atg/evidence/`. Nothing here is projected: every number was produced by the
shipped code on real registered inputs.

## §57 canonical workflow (no manual Python analysis)

| Step | Where it runs | Evidence |
|---|---|---|
| Create project | console *Projects*, `qualsight project-create`, `POST /api/projects` | offline gate 02 `project-created`; docs gate 03 block 1 |
| Import identity-labeled dataset | LFW funneled release, 13,233 samples, directory identity | offline gate 02 `lfw-imported`; dataset `dataset:sha256:98a15dd7…` |
| Run OFIQ | registered OFIQ v1.1.2 (787/787 conformance), single image + batch | r-01 UQS 79 (`assessment`); full LFW batch `job:sha256:5c6b51c8…` (13,233 measured, 0 failures) |
| Inspect quality/components | console *Sample inspector*, *Quality explorer* (28 measures, distributions, drilldown) | console observation 03–09 `explorer` steps |
| Import/run matcher | six frozen checkpoint runtimes (GPU) under two SCRFD profiles or publisher-prealigned ArcFace-112 pass-through; registered external score artifacts remain `EXTERNALLY_COMPUTED` | historical matrix `matrix:sha256:3769cdaf…` (three matchers × two profiles); all six real loaders and executable catalog are covered by `test_matcher_runtime_real.py`; exact checkpoint acquisition by `test_checkpoint_acquisition_real.py` |
| Generate genuine + impostor comparisons | frozen LFW View 2 protocol, 6,000 pairs × 10 folds, every pair retained with explicit failures | protocol `protocol:sha256:68125d45…` |
| Compute FMR/FNMR + DET | `biometric.verification.*@1.0.0`, Clopper–Pearson 95 %, two failure policies | six leaf Runs (EER ≈ 2.1 % shared, ≈ 2.35 % native); DET overlay figure `2295409…` |
| Compute ERC | `biometric.quality.utility.*@1.0.0` (registry v1.2.0), fixed MATCHER THRESHOLD, cluster-bootstrap intervals | six utility Runs (e.g. AS `run:sha256:6005f48a…`; AU-ERC 0.0147); verifier 213 checks × 6 |
| Compare two matchers | fixed-condition matcher contrasts + fixed-matcher preprocessing contrasts | cross-matcher Run `run:sha256:d2db18bd…` (9 contrasts; verifier 234 checks) |
| Inspect disagreement/failure samples | cross-matcher workbench cells → real pairs, images, both thresholds, annotations | observation 07 (annotation echoed); offline gate 02 `disagreements-resolve-to-pairs` |
| Save dashboard | content-addressed layouts, exact reload | dashboard `1e41ab08…`; offline gate 02 `dashboard-saved-and-reloaded` |
| Generate report | HTML (print) + PDF + JSON, classification-enforced images | report `931d69a0…` (8 permitted LFW images); offline gate 02 `report-built` |
| Export experiment bundle | `.qsexp` with checksums, no image bytes; reproduction EXACT/WITHIN_TOLERANCE/DIFFERENT/MISSING_DEPENDENCY/MISSING_DATA/BLOCKED | bundles `a86d3566…`, `9be9b4f0…`, `ff98d746…`; reproductions EXACT incl. re-embedding 7,701 images |
| Trace every result to its exact Run | every API row/figure/report cell carries run ids; `GET /api/runs/{id}` | 50 immutable Runs in the reference store (27 matcher, 13 analysis, 6 batch, 3 single-sample, 1 identification) |

Whole-chain executions: `atg/evidence/2026-08-18-offline-gate-02.json` (product started with the network
guard, 24 attributed audit events, zero refused connections, exit 0) and
`atg/evidence/2026-08-18-docs-workflow-gate-03.json` (user guide executed verbatim, 10 blocks, exit 0).

Independent database verification uses a consistent snapshot, never the API-owned live WAL store:

```sh gate
SNAPSHOT_PATH="$QUALSIGHT_DATA_DIR/cross-verifier-$(date -u +%Y%m%dT%H%M%SZ)-$$.snapshot.sqlite3"
$QS snapshot-database --output "$SNAPSHOT_PATH"
$UV run --directory backend --frozen --offline python ../scripts/verify_cross_matcher.py "$CROSS_DIR" "$SNAPSHOT_PATH"
rm -- "$SNAPSHOT_PATH"
```

## §58 scientific acceptance fields

Every `CompletedRun` binds: input dataset (`dataset_manifest`, `dataset_hash`), subject population
(`subject_protocol`, dataset subject count), sample and comparison counts (metric observations),
pairing/search protocol (`pair_search_protocol` with pairs sha), engine (`quality_engine` with artifact
hashes), matcher and preprocessing (`matcher`, `matcher_preprocessing`, `model_hashes`), thresholds
(`operating_thresholds`; MATCHER vs QUALITY POLICY kinds never conflated), metric definitions
(registry ids@versions on every observation), uncertainty method (`confidence_interval_method`,
`statistical_methods`, seeds), software version (`application_version`, `repository`,
`environment` lock identity), model version/hash (`model_hashes`), run id. Enforced by the strict
`CompletedRun` model (`backend/src/qualsight/domain/run.py`) and shown in every report's provenance section.

## §55 beta functional scope

| Area | Status |
|---|---|
| Projects (load/save, versions, manifests) | done — lifecycle NEW/OPEN/SAVED/CLOSED/ARCHIVED, clone/save-as, versions, purge |
| Users (single-user; optional local account) | single-user attribution done (QS-H71); optional local-account mode not built (documented as optional) |
| Data (import, manifests, identity mappings, pair definitions) | done — LFW import, protocol freeze, subset fixture, derived sets |
| Quality (OFIQ adapter, single image, batch, distributions) | done |
| Matchers (score import; approved local matchers) | done — beta-required AdaFace, ElasticFace, and TransFace-S under both frozen SCRFD preprocessing conditions; external score import. MagFace, TopoFR, TransFace-B, and publisher-prealigned research paths are catalogued extensions outside QS-H74. |
| Verification (FMR/FNMR/ROC/DET/EER/operating points) | done |
| FIQA (ERC, AU-ERC, quality-conditioned, quality-error modelling) | done |
| Cross-matcher (overlays, failure overlap, utility differences) | done |
| Statistics (CIs, bootstrap, correlations, regression) | done |
| Visualization (plots, failure drilldown, dashboards) | done |
| Reporting (PDF, HTML, print, CSV, Parquet, JSON, SVG/PNG/PDF) | done |
| Provenance (immutable Runs, hashes, versions, configuration, reproduction bundle) | done |

## 2026-08-29 closure hardening evidence

The closure pass adds cross-process data-directory ownership, durable operation admission and restart
reconciliation, crash-recoverable purge, streaming request ceilings, bounded scientific work admission,
separate liveness/readiness/metrics/version operations, bounded native/container execution, pre-decode
encoded-image dimension refusal, and completed-job controller release. The real twelve-leaf LFW benchmark
records a 3.92 GB post-fix peak RSS, only 30.2 MB incremental RSS on the second run, and 2.57 GB measured
process GPU VRAM (`docs/performance/2026-08-28_full-lfw-twelve-leaf-post-retention.json` and
`2026-08-28_full-lfw-twelve-leaf-vram.json`).

The closed add-on catalog contains six matcher drivers, four dataset drivers (identity-labeled LFW plus
three downloadable pair-only protocols), three representation-compatible preprocessing profiles, seven
controlled transforms, and no admitted remote connection. All checkpoint and pair-dataset transfers are
explicit setup actions with exact source, size, SHA-256, no-clobber promotion, and durable receipts; scientific
execution remains offline. The release builder emits committed-tree source/console archives, a real uv/pnpm
and governed-resource CycloneDX inventory, a complete recorded 13-step beta gate, a lock-bound vulnerability
audit, checksums, and a unified manifest. It still refuses to call
the result a portable installer because identity-labeled LFW, SCRFD, native OFIQ, and 199 deployment-specific
registry paths remain external prerequisites.

`atg/evidence/2026-08-29-console-cycle41-observation.json` records a completed 30-step Chromium 145 workflow
covering all nine views, real CUDA load/unload, real OFIQ and matcher work, project-switch isolation, reports,
identification, degradation, semantic accessibility, and a 390 px viewport. Across every view it found zero
contrast, labeling, focusability, duplicate-id, heading, table, image-alt, overflow, or ordinary-control-size
failures. `atg/evidence/2026-08-29-console-cycle41-interaction-audit.json` records 386 unique rendered API/image
links, zero non-200 responses, zero browser errors, and drilldowns for every matcher row in the selected real
matrix. These gates establish professional organization, interaction correctness, and mechanical accessibility;
they do not establish first-time intuitiveness with independent DHS biometric operators.

`atg/evidence/2026-08-29-dependency-vulnerability-audit-final-v6.json` records zero frontend advisories and
two exact local-only PyTorch acceptances (`PYSEC-2025-194`, `PYSEC-2026-139`). The waiver gate scans 73
repository Python sources plus all 9 byte-pinned setup-time upstream sources by AST, finds no direct or imported-alias references to the
affected `torch.jit.script` or `torch.compile` paths, rejects direct `eval`, `exec`, dynamic-import, and torch
reflection patterns, and constrains each current deployment-source `load_module` call to a literal key whose
manifest-pinned module is included in the scan before verifying strict hash-bound checkpoint loading. Static
Python inspection cannot prove the absence of every equivalent dynamic-execution path. This is constrained beta risk acceptance, not a vulnerability-free or
production-authorized claim.

The complete backend discovery baseline was 149/149 passing in 898.650 seconds with two explicit
large-network skips. The current controlled-beta closure ran 184/184 in 1,555.557 seconds with the same
two skips. The 35 additional tests cover bounded admission, streaming upload rejection, durable
request/attempt/result recovery, process leasing, atomic no-clobber backup publication and restore,
image-header preflight, exact blockwise identification, checkpoint and dataset integrity, scientific-claim
support boundaries, subject multi-membership resampling, corrected matcher-matrix contracts, and
independent SQLite snapshot/lease enforcement.

## 2026-08-31 beta-2 release evidence

The native OFIQ v1.1.2 rebuild path now consumes the committed Conan 2.18.1 lock, GCC 12 release
profile, and exact 15-node host/build closure. A clean offline rebuild from the governed source commit
and three hash-verified upstream archives passed 787/787 upstream conformance cases, resolved every
runtime library, executed the registered `r-01-frontal.png` input, and wrote a hashed build receipt.
The builder also supports verified HTTPS acquisition for missing archives and locked Conan packages;
it does not bundle or redistribute the model and conformance payloads.

## Research extensions outside QS-H74 beta acceptance

1:N identification, controlled degradation, and container execution were implemented and locally
validated after terminal beta acceptance in cycle C25. They remain outside QS-H74 beta acceptance and
do not change the `0.1.0-beta.2` release authority. Demographic analysis, PAD, capture clients, SSO,
commercial SDKs, and non-face modalities remain transition-only and are not admitted by this beta.

## Known limitations carried into the beta

* LFW results are QualSight-executed on the funneled release with SCRFD alignment; not published-figure reproductions.
* Matcher training-set identity exclusion is each matcher's published claim (decision 19).
* The external score artifact (decision 29) is an independent open-source pipeline, not a vendor file, and shares the buffalo_l detector file with the registered SCRFD-10G model.
* SCRFD detection runs on the CPU execution provider inside the frozen matcher runtime; embeddings and identification similarity run on the GPU.
* One transient `setUpClass` error in `test_matcher_api_real` was observed once (2026-08-17) and never reproduced.
* First-time operator intuitiveness has not been established with independent DHS biometric evaluators. The automated Chromium and expert-review gates establish interaction correctness and accessibility, not human usability.
* The source/console release is unsigned and has no publisher attestation; the release manifest says so. A clean committed tree is required before it will build.
* The exact pinned InsightFace and TransFace architecture sources call deprecated `torch.cuda.amp.autocast` and `timm.models.layers` APIs. PyTorch 2.10.0 and timm 1.0.28 execute those sources successfully in the real matcher gate; a dependency upgrade must repin or explicitly govern new source bytes and repeat the real embedding and matrix gates rather than silently rewriting the registered upstream code.
