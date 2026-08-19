# QualSight 0.1.0-beta.1 — beta acceptance record

Every row below names the evidence that closes it. Identifiers are content hashes recorded in
`atg/ledger.jsonl` (176 records) and in the persistent store on the reference workstation;
gate files live in `atg/evidence/`. Nothing here is projected: every number was produced by the
shipped code on real registered inputs.

## §57 canonical workflow (no manual Python analysis)

| Step | Where it runs | Evidence |
|---|---|---|
| Create project | console *Projects*, `qualsight project-create`, `POST /api/projects` | offline gate 02 `project-created`; docs gate 03 block 1 |
| Import identity-labeled dataset | LFW funneled release, 13,233 samples, directory identity | offline gate 02 `lfw-imported`; dataset `dataset:sha256:98a15dd7…` |
| Run OFIQ | registered OFIQ v1.1.2 (787/787 conformance), single image + batch | r-01 UQS 79 (`assessment`); full LFW batch `job:sha256:5c6b51c8…` (13,233 measured, 0 failures) |
| Inspect quality/components | console *Sample inspector*, *Quality explorer* (28 measures, distributions, drilldown) | console observation 03–09 `explorer` steps |
| Import/run matcher | frozen AdaFace / ElasticFace / TransFace runtime (GPU) under two frozen preprocessing conditions; registered external score artifact imported as `EXTERNALLY_COMPUTED` | matrix `matrix:sha256:3769cdaf…` (six Runs); external import `run:sha256:36b47713…` (decision 29) |
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
| Matchers (score import; approved local matchers) | done — three matchers, two conditions; external score import |
| Verification (FMR/FNMR/ROC/DET/EER/operating points) | done |
| FIQA (ERC, AU-ERC, quality-conditioned, quality-error modelling) | done |
| Cross-matcher (overlays, failure overlap, utility differences) | done |
| Statistics (CIs, bootstrap, correlations, regression) | done |
| Visualization (plots, failure drilldown, dashboards) | done |
| Reporting (PDF, HTML, print, CSV, Parquet, JSON, SVG/PNG/PDF) | done |
| Provenance (immutable Runs, hashes, versions, configuration, reproduction bundle) | done |

## Post-beta (Phase 6) status

1:N identification, controlled degradation, container execution accepted (cycle C25); demographic
analysis, PAD, capture clients, SSO, commercial SDKs, and non-face modalities are BLOCKED_BY_OWNER_INPUT
with the exact input each needs (ATG_PLAN.md, post-beta branch).

## Known limitations carried into the beta

* LFW results are QualSight-executed on the funneled release with SCRFD alignment; not published-figure reproductions.
* Matcher training-set identity exclusion is each matcher's published claim (decision 19).
* The external score artifact (decision 29) is an independent open-source pipeline, not a vendor file, and shares the buffalo_l detector file with the registered SCRFD-10G model.
* SCRFD detection runs on the CPU execution provider inside the frozen matcher runtime; embeddings and identification similarity run on the GPU.
* One transient `setUpClass` error in `test_matcher_api_real` was observed once (2026-08-17) and never reproduced.
