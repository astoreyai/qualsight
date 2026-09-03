# Changelog

## 0.1.0-beta.5 (unreleased release candidate) — 2026-09-03

* Installed owner-purge and Stress Studio validation now resolve their immutable real-input manifests
  through the controlled deployment repository. The installed regression uses the real release projector
  and checked-in LFW authorities, and it succeeds without relying on an ambient registry override.
  Beta 4 remains immutable because release and installed offline validation passed but installed browser
  validation stopped at owner purge; beta 5 is the required no-clobber repair.
* Installed source code, the separately built package console, and controlled scientific authority now
  remain independently selectable without silent version drift. Installation rejects ambiguous package/Git
  roots, binds the package to the exact clean controlled commit and byte-identical unprojected registry,
  runs the full resource-registry validator before dependency mutation, and emits location-independent
  commands. Every environment-driven installed API or CLI start repeats the commit, cleanliness, and
  registry-byte checks; data-tree hardening protects the separately installed console root.
* Release-package policy v2 removes checkout-only release construction, complete-gate, ATG append,
  provenance-review, and registry-repin entrypoints instead of shipping commands that cannot operate inside
  the projected archive. Real generated-archive regressions execute the installed validator imports and
  installer authority checks.

## 0.1.0-beta.4 (published candidate; not accepted) — 2026-09-03

* Installed-package browser validation now reads the immutable LFW subset manifest from the controlled
  deployment repository, matching the existing controlled Git, registry, source-cache, and real-input
  boundaries. Beta 3 remains preserved because its package passed release and offline installation but
  failed this browser validator; beta 4 was a no-clobber versioned repair. Its release and installed
  offline gates passed, but installed browser validation exposed the same split authority in owner purge
  at validator 4/9, so beta 4 also remains preserved, untagged, and unaccepted.

## 0.1.0-beta.3 (published candidate; not accepted) — 2026-09-03

* Reorganized the console around the accepted persistent left-hand workflow: Home, Projects, Inputs,
  Inspect quality, Compare, Stress test, Analyze utility, Results, Evidence & reports, and Manage system. Workflow entries
  show exact prerequisites, retain project context, isolate stale asynchronous results, collapse to an
  accessible mobile drawer, and preserve deterministic hash routes.
* Owner-uploaded images now become the active recorded WorkingSample. The canonical hash-verified image,
  SCRFD five-point geometry, OFIQ ADNet-98 landmarks, UnifiedQualityScore, all 28 grouped scalar/native
  measures, observed failure reasons, chart data, and structured evidence remain in one inspector instead
  of opening JSON as the primary result.
* Comparison Studio records two independent real images, a closed unverified relationship assertion,
  matcher, preprocessing profile, fixed threshold, immutable direct-pair Run, source/crop/detection/
  embedding evidence, and multi-run comparison history. Operator assertions never authorize outcome
  correctness or population-performance claims. Matcher execution runs in a terminable child process
  under a hard deadline; prepared output receipts allow exact restart reconciliation after process death.
* Stress Studio executes all seven closed transformations on the active recorded sample, renders every
  hash-verified derivative, compares source/derived SCRFD and OFIQ geometry, and measures the derivatives
  with OFIQ. Owner-derived execution verifies its complete current-project parent chain before admission.
  Transform work is durably admitted through the bounded executor, refuses more than 20,000 output files
  before execution, and reconciles fsynced prepared outputs on retry.
* Analyze utility exposes every recorded OFIQ measure and explicit compatible multi-analysis overlays. The frozen
  protocol digest identifies the pair universe; per-Run comparison digests remain verified provenance.
  Matcher, preprocessing, measure, and composite differences are labelled separately, and incompatible or
  incomplete records are refused rather than silently combined.
* Replaced primary raw API navigation with typed in-app Evidence routes across project, sample, matcher,
  analysis, report, system, and comparison surfaces. Raw JSON remains an explicit secondary action.
* Manage system now separates Add-ons, Processing profiles, Connections, Runtime & storage, and Audit. It exposes
  the closed downloadable matcher/dataset catalog, exact publisher URL, verified receipts, load/unload,
  runtime readiness, live process lease, and zero admitted remote connections without generic plug-in or
  provider controls.
* Recorded dataset, sample, image, geometry, quality, derived-set, direct-comparison, direct-Run, and raw
  artifact reads are project-scoped. Foreign-project and retired unscoped routes return 404. Browser-facing
  records use opaque identities and links rather than biometric storage paths.
* Purge previews, results, failures, and history are path-free public receipts containing counts, verification,
  limitations, and an evidence digest. Complete target paths and operating-system failures remain in the
  private local purge attestation used for integrity and recovery.
* Startup now snapshots a database whose current version has an incomplete migration ledger or schema
  shape before repair. Readiness publishes the kernel-held process-lifetime data-directory lease without
  disclosing its lock path, re-hashes the registered OFIQ binary/configuration, treats every non-completed
  purge journal as unresolved, and returns stable path-free dependency codes.
* Local storage remediation now treats only a file or directory that disappears between secure enumeration
  and no-follow open as benign. Symlinks, type replacement, access failure, and every other error remain
  fail-closed. This prevents transient real SQLite WAL/SHM cleanup from aborting CLI crash recovery.
* Owner-upload and verification-dataset acquisition publication now use staged, fsynced, no-clobber files
  plus durable receipt authority. Same-size drift, symlinks, short writes, missing receipts, completion-event
  failure, and restart reconciliation are refused or rolled back without claiming unrecorded bytes.
* Durable process-death recovery now covers quality batches, matcher-matrix leaves, registered external-score
  imports, utility/cross-matcher/degradation/identification analyses, and figure/report/export/bundle
  publication in addition to direct comparisons and derived sets. Prepared identities bind project, request,
  result, members, sizes, hashes, and the recorded actor; malformed, foreign, partial, and symlinked stages
  fail closed and remain purgeable.
* Exact interrupted matcher-matrix retry reconstructs the canonical request, reconciles verified prepared
  leaves under their original identities, and executes only the missing leaves.
* Owner-protocol generation now preflights a 20,000-source-sample and 20,000-output-pair ceiling before
  materialization, uses deterministic ranked sampling instead of allocating all possible pairs, and shares
  the bounded-work admission contract across API and CLI execution.
* Annotations now resolve a closed project-owned subject type before persistence and commit the annotation
  with its audit event in one SQLite transaction. Cross-project/unknown subjects and split evidence are refused.
* The backend package identity is `0.1.0b3`; workspace and console identity is `0.1.0-beta.3`. Release
  publication remains no-clobber, so the accepted beta-2 package is preserved for rollback.
* The exact reviewed Node runtime is now 26.7.0 with pnpm 10.28.0. Install, release, and browser
  validation use the reviewed `node` selected on `PATH` and fail preflight before dependency mutation
  when its identity differs; they no longer force the retired Node 20 system binary.
* The release vulnerability gate caught four newly published high-severity `fast-uri` advisories in
  the PWA build chain after all 15 product gates passed. The lock now forces patched `fast-uri` 3.1.7;
  the same complete lock audit reports zero frontend advisories. No exception was added to policy.
* A failed release now reports passed, failed, and not-run gate counts plus the first failed gate, and retains
  the path-normalized gate record privately outside the disposable stage with 0700/0600 permissions. A bounded
  final output tail is retained only after controlled paths are labelled and remaining private absolute paths
  are removed; failure diagnostics never enter the versioned release package. The staged documentation gate now
  preserves the controlled deployment repository root instead of rebasing deployment authorization paths onto
  its disposable Git-archive root.
* The complete browser release chain now preserves the controlled deployment repository root while executing
  staged code. Browser validators no longer rebase registry authorizations, legacy Git evidence, or the populated
  read-only evidence database onto the disposable source archive. Structured-evidence validation continues to use
  SQLite online backup into isolated state and never opens the controlled database for mutation.
* Upstream matcher architecture acquisition is now an explicit `qualsight setup` step. The offline
  gate imports only exact manifest-verified cached sources into its fresh private data directory, sets
  the setup network scope to disabled, and requires zero authorized or refused external connections.
  Controlled LFW input resolution is registry-driven rather than bound to a workstation pathname.
  `QUALSIGHT_SOURCE_CACHE_ROOT` selects an existing absolute, non-symlinked verified cache without
  silently contacting a publisher or granting network access.
* Governed table exports now require origin-checked POST creation and download through the console as real
  attachment bytes. A JSON-accepting console request receives a verified export receipt, then lets Chromium
  retrieve that exact recorded file through a read-only project-scoped download route; this preserves API
  error handling without a client-created blob URL. Direct POST clients retain the attachment response,
  while retired GET creation routes are refused without changing stored export state. Browser mutations now
  match Origin scheme, host, and effective port rather than accepting every loopback port. The browser audit
  verifies the downloaded CSV filename, nonempty byte count, and SHA-256.
* Release publication rechecks the commit, tree, and clean tracked state immediately before the atomic
  no-clobber rename. Source projection and extracted-package validation reject raw, encoded, and
  platform-specific operator-home paths while retaining only reviewed external locator bindings.
* Release preflight validates the pinned Python, uv, Node, and pnpm toolchain before dependency mutation.
  Source creation and extracted-package validation reject disguised biometric/runtime bytes, exact
  registered restricted hashes, secrets, special nodes, and encoded host paths; the map-free console is
  scanned for raw or encoded private build paths before archiving. Playwright 1.58.0 is pinned for the
  nine-workflow release browser aggregate. The final SBOM, vulnerability, gate, manifest, commit, and
  checksum sidecars are also scanned before atomic publication. The source archive embeds exact
  path/hash/size/safe-mode membership, and the committed-tree validator checks the extracted archive and
  expected commit before publication. The full backend gate now has a 5,400-second deadline, above the
  observed 4,047.221-second healthy-suite baseline. Release applies its deterministic safe umask before
  creating any staged or published path; extracted packages may safely reduce group/world permissions but
  cannot add permissions, special bits, group/world write, or remove required owner access.
* The immutable release chain now executes the shipped user-guide command blocks as a required gate and
  records their evidence beside the offline and nine-workflow browser gates. The installer prints a complete
  offline verification command with explicit observation and data-directory destinations. The offline gate
  distinguishes a live listener from a recently closed loopback connection and writes server output to a
  seekable temporary log, so kernel `TIME_WAIT` state and log volume cannot cause false refusal or pipe
  backpressure during matcher execution.

## 0.1.0-beta.2 — 2026-08-31

* OFIQ v1.1.2 can now be rebuilt through a committed Conan 2.18.1 lock, GCC 12 profile, and exact
  15-node host/build dependency closure. The builder verifies the three upstream archive identities,
  supports governed online acquisition or fully offline cached resolution, runs all 787 upstream
  conformance cases, checks dynamic linkage, executes a real conformance image, and writes a hashed
  receipt. Restricted model and image payloads remain external and are never packaged.

* Controlled-beta closure now separates beta-required capabilities from deferred research extensions
  and DHS transition controls. The committed release gate requires exactly AdaFace IR101,
  ElasticFace Arc R100, and TransFace-S under both frozen SCRFD preprocessing conditions, records all
  nine cross-matcher contrasts, proves ownership of its offline listener, and derives required SBOM
  resource edges from that same admitted set.
* Durable operation requests now retain separate attempt and result identities across retry and restart,
  so internal retry attempts cannot appear as phantom project jobs. The local operator can create,
  verify, and restore an integrity-bound offline backup while the data-directory lease excludes live
  mutation.
* Scientific outputs now distinguish descriptive in-sample thresholds from independently supported
  claims, require observed false-match evidence before supporting an operating point, use subject-aware
  resampling for multi-membership comparisons, and propagate claim-specific `NOT_SUPPORTED` status into
  reports. Legacy report compatibility is exercised from a provenance-labelled subset of the real
  2026-08-17 SQLite record rather than a manufactured record.

* Closed add-on catalog: the console and `GET /api/addons` now enumerate six real matcher
  drivers, the registered identity-labelled LFW dataset, three author-published pair datasets,
  three swappable matcher-preprocessing profiles, and seven
  controlled-degradation operators from code/registry truth. Verification Runs accept a selected
  matcher × preprocessing Cartesian product; Advanced Analysis uses the same catalog instead of
  duplicated identifiers.
* API connections are explicitly empty for the local-only beta. Arbitrary packages, module hooks,
  commands, and remote REST drivers are not loadable. The catalog states the governance requirements
  for any future code-owned connection adapter.
* Checkpoint acquisition is rights-gated per registered matcher. All six exact checkpoints can be
  imported from verified owner-held bytes or streamed from their author-published HTTPS files into
  private managed storage with size/SHA-256 verification, no-clobber promotion, and durable receipts.
  LFW, CFP-FP, and AgeDB-30 pair protocols can be downloaded, safely extracted, atomically imported,
  and executed; their absent identity/fold labels remain explicitly not assessed rather than invented.
  Direct identity-labelled LFW and SCRFD cold-start acquisition remain package-boundary blockers.
* Release assurance now refuses dirty worktrees and existing destinations, extracts the exact commit,
  performs fresh frozen all-group Python and frontend installs, rebuilds the console, derives every
  metadata gate from that staged tree, emits a deterministic CycloneDX SBOM with real uv/pnpm runtime
  and release-toolchain edges, records artifact hashes plus the exact commit/tree, and atomically publishes
  one locked no-clobber version directory. A committed package policy excludes internal session/ATG
  records, machine-specific performance observations, and recorded real-result fixtures from the source
  archive while retaining their integrity hashes in the controlled checkout,
  rejects prohibited biometric/model/data/native/screenshot file classes and credential indicators, and
  gates current Python/npm vulnerability evidence. Vite and its build dependencies were upgraded
  to remove all 13 npm audit findings; two non-reachable local-only PyTorch advisories remain governed
  by an exact fail-closed acceptance policy pending a real six-matcher runtime-upgrade equivalence gate.
* The committed-tree beta gate now preserves the supplied virtual-environment interpreter entrypoint;
  resolving its symlink previously launched Debian system Python and failed before the staged root gate
  could load the release-pinned Pydantic version. Backend discovery now also fixes `backend/` as its
  top-level directory so the root test package cannot shadow the real backend fixture package. The live
  offline workflow preserves that same entrypoint and installs the three beta matchers' pinned architecture
  sources through the real add-on API before executing its six-leaf matrix.
* Add-on readiness is revalidated from live bytes: checkpoint cache identity includes inode/ctime as well
  as size/mtime, and the LFW card verifies its archive, manifest, exact tree membership, every image hash,
  and the registered tree digest before advertising `READY_TO_IMPORT`.
* System asset lifecycle: verified checkpoint/source status, explicit hash-pinned architecture-source
  pull, real shared matcher load/unload with active-job protection, and durable evidence-hashed events.
  Every one of the six checkpoint actions records durable request/completion/failure events, preserves
  its factual third-party rights status, and refuses loaded or leased runtimes.
* Project embedding inventory re-hashes the six recorded `.npy` artifacts, reports shape/classification,
  and links to Runs and governed exports.
* Local data storage now starts with umask `077`, opens every path component and managed entry through
  no-follow file descriptors, remediates managed directories/files to `0700`/`0600`, refuses symlinked
  ancestors, symlinks, special nodes, filesystem roots, and protected ancestors, and enables the TCP/UDP
  network guard for API and CLI unless explicitly disabled. Pinned source acquisition uses one explicit,
  thread-bounded network scope.
* Verified purge now removes and checks managed files and scoped biometric/scientific metadata while
  retaining only the project/version audit shell and purge attestation; admission is mutually exclusive
  with in-flight project mutations and scientific jobs. Content-addressed owner uploads retain one
  immutable reference per admitting project and are deleted only after the last project reference is purged.
  A process-wide owner-file mutation boundary excludes cross-project admission across the complete purge
  target-compute, unlink, and metadata interval.
* Browser mutations reject hostile origins, DNS-rebinding hosts, and the test-client-only `testserver` name.
  Report downloads use database-anchored
  hashes for HTML, PDF, JSON, and provenance; verified figures, reports, exports, bundles, and sample images
  stream from the same private snapshot that was hashed, closing pathname-replacement and in-place-write races.
  Embedding drift is returned as typed inventory state.
* Report and reproduction inventories now publish a verified/missing/drifted/unanchored download state for
  every artifact. The console links only verified bytes and explains unavailable legacy or drifted evidence
  in place, instead of exposing actions that terminate in an integrity refusal.
* Matcher lifecycle actions are globally serialized, write durable intent before runtime/source mutation,
  and restore the exact pre-action runtime, source, managed-checkpoint, and receipt state if completion-event
  persistence fails. The runtime manager remains exclusively controlled through event persistence or rollback,
  so a scientific lease cannot observe an uncommitted lifecycle state. Linked completion or failure evidence
  remains mandatory.
* Operator console adds owner-directory/CSV-manifest import controls, dataset-aware protocol freezing,
  persistent active-project selection, cross-view status messages, responsive asset cards, and explicit
  loading/empty/error states.
* The Projects Run inventory now requests and renders only the active project's immutable Runs. A real
  two-project API regression and populated-store Chromium gate prevent the formerly global Run feed from
  being presented under a project-specific caption.
* Owner-supplied local inputs (decision 30): single-photo upload assessed by the registered engine
  as a recorded Run (`POST /api/quality/assess-upload`, console *Assess your own photo*,
  `assess-owner` CLI); identity-labelled directory / CSV-manifest imports with saved reusable field
  mappings; deterministic seeded owner pair protocols executing on the real matcher matrix; purge
  coverage; source kind `OWNER_SUPPLIED_LOCAL_INPUT` on every result.
* External-score import accepts the closed registered artifact from the console. Custom field mappings
  and saved mapping names remain available through the API/CLI contract; the beta console does not
  claim an interactive external-score mapping editor.
* Visual evidence: SCRFD detection overlays (labelled as the matcher pipeline's), OFIQ's own
  ADNet-98 landmarks through a registered auxiliary binary built from the registered v1.1.2 source,
  compared 112×112 crops recomputed under the recorded frozen condition, and a match-evidence panel
  placing each pair's score on its Run's genuine/impostor distributions with recorded thresholds,
  decisions, association-only quality attribution, and a fixed honesty statement.
* Routed console navigation (PRD §44): one view per route with honest empty states.
* The repeatable Chromium gate now checks all nine populated routes for accessible names and labels, native
  semantics, disabled-control explanations, image alternatives, heading continuity, unique identifiers,
  dialog semantics, text contrast, named tables with scoped row/column headers, keyboard-operated tabs,
  mobile overflow, and 44 px targets. The Reports section hierarchy and 28 table surfaces were corrected
  after this gate reproduced skipped headings and missing table names.
* The interaction gate opens recorded evidence for all three admitted matchers, requires the complete
  six-leaf (three-matcher × two-preprocessing) nine-contrast Cross summary, and checks every rendered
  artifact link.
* DFAB/DHS demo: `scripts/demo_dhs.sh` (evidence verification + Stakeholder Demo dashboard) and the
  timed operator flow `docs/DEMO_SCRIPT.md`.
* Matched quality-job pick lists: utility/cross-matcher panels only offer quality jobs over the
  selected Run's own dataset (an uploaded photo's job can no longer be selected against an LFW matrix).

## 0.1.0-beta.1 — 2026-08-18

First beta. The 41-node beta capability graph (QS-A00 … QS-H74) is accepted on real data; see
`docs/BETA_ACCEPTANCE.md` for the evidence behind each PRD §57 step and §58 field.

* Phase 0 — resource/metric registries, OFIQ v1.1.2 build and 787/787 conformance, registered CLI
  execution, single-image assessment, immutable Runs, reproduction, project lifecycle, PWA console.
* Phase 1 — LFW import (13,233 samples), OFIQ batch (13,233 measured), quality explorer.
* Phase 2 — frozen LFW View 2 protocol, three matchers × two preprocessing conditions on the GPU,
  FMR/FNMR/ROC/DET/EER with Clopper–Pearson intervals, error drilldown, external score import
  (`EXTERNALLY_COMPUTED`; owner decisions 28/29).
* Phase 3 — pair-quality policies, ERC/AU-ERC/pAU-ERC/risk-coverage, quality-conditioned rates,
  Spearman/Kendall/logistic/ROC-AUC, threshold-policy analysis; metric registry v1.1.0.
* Phase 4 — fixed-condition matcher contrasts and fixed-matcher preprocessing contrasts, error
  overlap, paired-bootstrap AU-ERC differences, interaction models, disagreement workbench, annotations.
* Phase 5 — provenance-bearing figures (PNG/SVG/PDF/CSV + sidecar), content-addressed dashboards,
  HTML/PDF/JSON reports with data-classification enforcement, CSV/JSON/Parquet exports,
  `.qsexp` bundles and reproduction mode (EXACT/WITHIN_TOLERANCE/DIFFERENT/MISSING_DEPENDENCY/
  MISSING_DATA/BLOCKED) with real re-execution.
* Phase 7 — threat model, runtime network guard, secrets audit, verified purge, single-user
  attribution, offline installation and gate, shipped documentation executed verbatim, PRD §44 navigation.
* Post-beta (cycle C25) — 1:N identification (registry v1.2.0), controlled transformations and
  degradation responses, container execution adapter.

Known limitations are listed in `docs/BETA_ACCEPTANCE.md` and `docs/SCIENTIFIC_METHODS.md`.
