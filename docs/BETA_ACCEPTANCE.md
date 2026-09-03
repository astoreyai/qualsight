# QualSight 0.1.0-beta.3 — release-candidate acceptance record

This record combines historical accepted evidence with focused beta-3 development evidence. It is not
final package authority until the frozen committed-tree release gate, installed-package gate, and rollback
check complete. Every row below names its evidence. Identifiers are content hashes recorded in the
append-only `atg/ledger.jsonl` and in the persistent store on the reference workstation;
gate files live in `atg/evidence/`. Every recorded number was produced by the identified code on real
registered inputs; focused `/tmp` artifacts are development evidence, not durable package authority.

## §57 canonical workflow (no manual Python analysis)

| Step | Where it runs | Evidence |
|---|---|---|
| Create project | console *Projects*, `qualsight project-create`, `POST /api/projects` | offline gate 02 `project-created`; docs gate 03 block 1 |
| Import identity-labeled dataset | console **Inputs → Dataset → Import real dataset**; LFW funneled release, 13,233 samples, directory identity | offline gate 02 `lfw-imported`; dataset `dataset:sha256:98a15dd7…` |
| Run OFIQ | registered OFIQ v1.1.2 (787/787 conformance), single image + batch | r-01 UQS 79 (`assessment`); full LFW batch `job:sha256:5c6b51c8…` (13,233 measured, 0 failures) |
| Inspect quality/components | console **Inspect quality** and **Inputs → Quality explorer** (28 measures, distributions, drilldown) | console observation 03–09 `explorer` steps |
| Import/run matcher | beta-required AdaFace IR101, ElasticFace Arc R100, and TransFace-S under two SCRFD profiles; MagFace, TopoFR, TransFace-B, and publisher-prealigned paths are research extensions; registered external score artifacts remain `EXTERNALLY_COMPUTED` | historical matrix `matrix:sha256:3769cdaf…` (three matchers × two profiles); all six real loaders and executable catalog are covered by `test_matcher_runtime_real.py`; exact checkpoint acquisition by `test_checkpoint_acquisition_real.py` |
| Generate genuine + impostor comparisons | frozen LFW View 2 protocol, 6,000 pairs × 10 folds, every pair retained with explicit failures | protocol `protocol:sha256:68125d45…` |
| Compute FMR/FNMR + DET | `biometric.verification.*@1.0.0`, Clopper–Pearson 95 %, two failure policies | six leaf Runs (EER ≈ 2.1 % shared, ≈ 2.35 % native); DET overlay figure `2295409…` |
| Compute ERC | `biometric.quality.utility.*@1.0.0` (registry v1.2.0), fixed MATCHER THRESHOLD, cluster-bootstrap intervals | six utility Runs (e.g. AS `run:sha256:6005f48a…`; AU-ERC 0.0147); verifier 213 checks × 6 |
| Compare two matchers | fixed-condition matcher contrasts + fixed-matcher preprocessing contrasts | cross-matcher Run `run:sha256:d2db18bd…` (9 contrasts; verifier 234 checks) |
| Inspect disagreement/failure samples | cross-matcher workbench cells → real pairs, images, both thresholds, annotations | observation 07 (annotation echoed); offline gate 02 `disagreements-resolve-to-pairs` |
| Save dashboard | content-addressed layouts, exact reload | dashboard `1e41ab08…`; offline gate 02 `dashboard-saved-and-reloaded` |
| Generate report | HTML (print) + PDF + JSON, classification-enforced images | report `931d69a0…` (8 permitted LFW images); offline gate 02 `report-built` |
| Export experiment bundle | `.qsexp` with checksums, no image bytes; reproduction EXACT/WITHIN_TOLERANCE/DIFFERENT/MISSING_DEPENDENCY/MISSING_DATA/BLOCKED | bundles `a86d3566…`, `9be9b4f0…`, `ff98d746…`; reproductions EXACT incl. re-embedding 7,701 images |
| Trace every result to its exact Run | every API row/figure/report cell carries run ids; `GET /api/projects/{project_id}/runs/{id}` requires the owning project and the retired global route returns 404 | 50 immutable Runs in the reference store (27 matcher, 13 analysis, 6 batch, 3 single-sample, 1 identification) |

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
| Projects (load/save, versions, manifests) | lifecycle NEW/OPEN/SAVED/CLOSED/ARCHIVED, clone, save-as, versions, and purge are implemented in the backend/API. The console currently exposes create/open/save/close/clone/archive/purge; it does not claim Save-as or version-history controls. |
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
and governed-resource CycloneDX inventory, a complete recorded 15-gate chain (including the executable
user guide and nine-workflow browser aggregate), a lock-bound vulnerability
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

## 2026-09-01 beta-3 image-workflow evidence

Beta 3 adds the operator-facing image workflow while preserving the controlled single-user workstation
boundary. A recorded owner upload becomes the active project-scoped WorkingSample; the inspector renders
the canonical hash-verified image, SCRFD five-point geometry, OFIQ ADNet-98 geometry, UnifiedQualityScore,
all 28 recorded scalar/native measures, observed failures, and structured evidence. Direct comparison
records two independently admitted real images, the operator's explicitly unverified relationship assertion,
one admitted matcher and preprocessing profile, thresholded decision, crop/detection/embedding evidence,
and immutable same-pair history. Stress Studio applies all seven bounded deterministic transforms to the
recorded real input and reconciles every derived image, geometry result, OFIQ trajectory, and artifact hash.
Quality, matrix-leaf, external-score, direct-pair, derived-set, utility, cross-matcher, degradation,
identification, figure, report, export, and bundle publication now survives process death without guessing:
each path durably admits the request and either records an integrity-bound prepared result or an equivalent
operation-bound completion before SQLite completion is claimed. Restart/retry reconciles only the exact
project/request/result identity and purges malformed, foreign, and incomplete stages. Transformation work
is refused above 20,000 output files. Owner protocol creation refuses more than 20,000 source samples or
20,000 output pairs before materialization and uses ranked sampling rather than constructing all possible
pairs. Direct matcher inference executes in a terminable child process under the configured hard deadline.

The pre-latest-repair backend baseline ran 225 tests in 1,811.356 seconds: 223 passed and two explicitly
opt-in large-network tests skipped. Those numbers are historical and are not the final beta-3 tree count.
The corresponding frontend TypeScript and production build passed under the then-pinned Node 20.20.2/pnpm
10.28.0 toolchain with 48 modules transformed. An earlier post-repair backend discovery then ran
303 tests in 4,047.221 seconds: 298 passed, five explicitly opt-in checkpoint/dataset network acquisition
tests skipped, exit 0, and no SIGBUS recurrence. Focused development-tree Chromium evidence includes:

* pair comparison ephemeral observation (not packaged), SHA-256
  `b8093103eb1353cb75c4baa5785aa77d54e0217a39f82a532cb67d28021af7ca`, four immutable comparisons
  across all three relationship states, one request during a held POST, locked in-flight configuration,
  collapsed secondary artifacts, exact project-scoped geometry, and evidence;
* single-image stress ephemeral observation (not packaged), SHA-256
  `7fa000cdb9ee9eb1fb3bbd743a5d5ae315c6564ed612b7a2423057f340874fcd`, a real seven-image job followed
  by a one-image replacement set, no old-result resurrection, dynamic counts, geometry, OFIQ values,
  purge, and responsive layouts;
* Analyze utility overlays ephemeral observation (not packaged), SHA-256
  `0a772799dd1b89df6eaca90bd5cfb625ec0bf159f985f80ade0a929bb3af5770`, protocol-bound pair universes,
  all result-determining threshold/bootstrap identities, explicit compatible overlays, and real held-response
  sequencing for detail and pair filters;
* final Analyze utility/context sequencing ephemeral observation (not packaged), SHA-256
  `2c27f070c8dc4f1a5e612331bd917bd278d9f3fa65a65496ad274686ffb61e27`, preserving a newer opened
  analysis against a late already-recorded start response and binding protocol, matcher leaf, and Run to one
  active Analyze utility analysis;
* Manage system ephemeral observation (not packaged), SHA-256 `04e2b594…`, six matchers, three preprocessing
  profiles, seven transforms, thirteen registered resources, zero providers, held process lease, exact
  publisher source, real source pull/load/unload, and refusal;
* upload and purge ephemeral observations (not packaged): SHA-256
  `784f0826d9fd098a453259ca96a597e3e4378188f9a140f836c741e0025f5a00`; and SHA-256
  `fcb8bd818aeeec4a1375c258c035cbf502fb553330f6dad996a104fe56477ca4`;
* final scoped integrated console: `atg/evidence/2026-09-01-console-beta3-scoped-final.json`, SHA-256
  `e6921165f3db43a4ba8e4a6bfffe37d20a9977a35fd38e0567faa32689062ca4`, 32 completed steps and exit 0.

The integrated observation exercised ten unique left-navigation destinations, real upload and OFIQ work,
the populated dataset/matcher/utility/report/degradation chain, structured evidence, loading/empty/error
contracts, keyboard focus and drawer trapping, semantic tables and dialogs, contrast, reduced-motion
handling, 390 px reflow, and 44 px scientific controls. It found no unexplained disabled control, unlabeled
or unfocusable interaction, invalid table/dialog, duplicate identifier, heading jump, missing image
alternative, contrast failure, horizontal overflow, or undersized tested target. These automated and expert
gates establish interaction correctness; independent DHS operator usability remains external evidence.

The hostile merged-diff review was not waived by the earlier green gates. Repeated review reproduced and repaired public
derived-lineage paths, unscoped quality-job reads, mutable Parquet chart reads, incomplete Utility overlay
identities, swallowed comparison-worker diagnostics, artifact read/verify races, non-durable failed-purge
evidence, duplicate direct-comparison requests, silent comparison-history errors, cross-project purge state,
old Stress and Utility response resurrection, hard-coded transform counts, misbound structured evidence,
primary filesystem-path presentation, active-context precedence, and a vacuous project-switch assertion. A final
hostile pass then reproduced and repaired public dataset source/manifest paths, post-purge comparison admission,
late already-recorded Utility detail replacement, mixed workflow context, and a browser assertion that could skip
the first disabled catalog control. The independently rerun durable 32-step observation is
`atg/evidence/2026-09-01-console-beta3-final-adversarial.json` (SHA-256
`d70c39fa6f81dbd2d906243fcf4ef0d1fe31ac82c0495550b64ec3f985eb14b5`).
Every quality-job read or mutation now requires both project and job identity; retired global routes return 404.

This remains a release-candidate evidence record, not a package acceptance claim. The later hostile pass identified
additional unscoped Run/dataset-study reads, durable failed-purge mutation admission, job-result path projection,
and path-bearing warning presentation. Those findings are now RED-tested and repaired: all 34 recorded-evidence
route families require project identity; durable incomplete purges refuse new mutations; job results are path-free;
new Run warnings are structured and path-free; and legacy path-bearing warnings are sanitized in primary Evidence.
Packaging remains stopped until the complete post-repair gates and final hostile review pass.

## 2026-09-02 beta-3 release-blocker closure evidence

The third clean-commit package attempt at `27711c13ad91404fa4dc9b82dbe5376052dbadcf` passed release
gates 1–14 and failed only `browser-beta3-workflows`. Eight validators exited 0; `observe_console.py`
timed out waiting for the governed quality-CSV download even though the backend had already published
the real 16,622-byte export. A production build that retained the client blob URL for 60 seconds failed
the same way, disproving early URL revocation as the cause.

A native same-origin POST form first restored the browser event: its 32-step observation had SHA-256
`c93b61d332c38cb7bb1179ef4c42cc36720fda3e1a59b19a1bc7fa100ed688f8`, and its complete browser
aggregate passed 9/9 at SHA-256 `f8ae646d6e322d9f895bc832ccf3f6bb6fbebedcee1acde51a65d1c60afe6dc9`.
Adversarial review did not accept that interim mechanism because a form response could not return a
creation refusal to the React workflow or keep its busy state bound to server completion.

The accepted development-tree mechanism keeps export creation behind the project purge fence and exact
scheme/host/port Origin check. A JSON-accepting POST returns a path-free hash/size/classification receipt;
the receipt links to a read-only project-scoped GET that can only recover and stream the already-created,
integrity-checked prepared output. The source contract was observed red before implementation. The real
LFW API regression was observed failing on the old attachment representation, then passed in 162.546
seconds with receipt shape, downloaded SHA-256/size equality, and wrong-port Origin refusal. The rebuilt
console passed all 32 Chromium steps with observation SHA-256
`63c454bb206df2a24615601979c48951f836b7be3e5cd9992ac09e8366a3b701`; the complete nine-validator
aggregate passed 9/9 with SHA-256
`30491cb1d2c9a4f2e577d6106283b93225144b0ded24e26434df1c83d564e2f9` and no nonzero validator.
These are development-tree closure results. Final committed-package, installed-package, rollback, tag,
and remote identities remain pending and are not implied by this section.

The next clean-commit attempt at `0540066c5618612ab587f8b7a72d0cd2005d3a70` again passed gates
1–14 and failed the browser gate before package publication. The private gate record has SHA-256
`4bb05e4c6cca7216978a9d6c3bb5f31d9b43f4a7a594590edb2e3fbbc5fcc21e`. Its 33-byte stdout identity
proved that validator 1 failed. A retained exact clean stage reproduced the cause: the staged browser chain
overwrote `QUALSIGHT_REPOSITORY_ROOT`, so the API compared the controlled deployment registry with a
disposable-stage authorization path and refused startup. The first clean-stage observation failed with
SHA-256 `8bace4fbb213bfd15cf725e82e8726d454cf3444af4caac32d3bd9c80c6cfb46`.

The browser aggregate and all nine validators now preserve an already-governed deployment root. Two additional
staged assumptions were then exposed and repaired: the warning/privacy validator runs its pinned historical
`git archive` against the controlled Git repository, and structured-evidence navigation snapshots the real
controlled database rather than looking for excluded `var/` state inside the source archive. Three focused
root-contract tests were observed failing before repair and pass 3/3 afterward. The formerly failing clean-stage
Comparison Studio workflow passes with four ready matchers, three real LFW uploads, and four comparisons
(SHA-256 `948e5f6255a46fdb261dc0baf898bf6c557011fa42d832e1bee4fbb91c783dce`). The complete separated-root
browser aggregate passes 9/9 at SHA-256
`a4e0eabde9b97347be077ce92c95e1c056734c384cb3e3473cd107c516f3627e`; its final 32-step console
observation has SHA-256 `86fa59ead978725b5da23178a769fe3cd2e46e6509ee0948860a5207664fbe55`.
These are staged repair results, not final package acceptance. A new clean committed-tree release remains required.

The clean release from repair commit `7abfd880e7dc46724b61e738a472536a26263e39` then passed all
15 product gates, including the complete nine-workflow browser aggregate. Publication still failed closed:
the post-gate lock audit found four high-severity advisories published on 2026-09-02 against transitive
`fast-uri` 3.1.5 in the PWA build chain. The audit report recorded frontend advisory ids 1158521,
1158524, 1158527, and 1158530; no beta-3 directory was published. The dependency path is
`vite-plugin-pwa` → `workbox-build` → `ajv` → `fast-uri`.

The root pnpm policy now forces `fast-uri` 3.1.7, whose package-registry integrity is locked. The exact
same vulnerability command changed from exit 1 with four frontend advisories to exit 0 with zero,
without adding a policy exception; the two existing local-only PyTorch findings remain the exact accepted
set. Final committed-package, installed-package, rollback, tag, and remote identities remain pending.

## Research extensions outside QS-H74 beta acceptance

1:N identification, controlled degradation, and container execution were implemented and locally
validated after terminal beta acceptance in cycle C25. They remain outside QS-H74 beta acceptance and
do not change the original QS-H74 acceptance boundary; beta 3 exposes the already validated controlled
degradation capability as a project-scoped single-image workflow. Demographic analysis, PAD, capture clients, SSO,
commercial SDKs, and non-face modalities remain transition-only and are not admitted by this beta.

## Known limitations carried into the beta

* LFW results are QualSight-executed on the funneled release with SCRFD alignment; not published-figure reproductions.
* Matcher training-set identity exclusion is each matcher's published claim (decision 19).
* The external score artifact (decision 29) is an independent open-source pipeline, not a vendor file, and shares the buffalo_l detector file with the registered SCRFD-10G model.
* SCRFD detection runs on the CPU execution provider. Embeddings and identification use the Run-recorded matcher device: CUDA when selected and available with sufficient headroom, otherwise CPU.
* One transient `setUpClass` error in `test_matcher_api_real` was observed once (2026-08-17) and never reproduced.
* First-time operator intuitiveness has not been established with independent DHS biometric evaluators. The automated Chromium and expert-review gates establish interaction correctness and accessibility, not human usability.
* The source/console release is unsigned and has no publisher attestation; the release manifest says so. A clean committed tree is required before it will build.
* The exact pinned InsightFace and TransFace architecture sources call deprecated `torch.cuda.amp.autocast` and `timm.models.layers` APIs. PyTorch 2.10.0 and timm 1.0.28 execute those sources successfully in the real matcher gate; a dependency upgrade must repin or explicitly govern new source bytes and repeat the real embedding and matrix gates rather than silently rewriting the registered upstream code.
