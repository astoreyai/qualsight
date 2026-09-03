# QualSight

Local, offline research workbench that relates **face image quality** (OFIQ v1.1.2, the ISO/IEC 29794-5
reference implementation) to **face-matcher utility** (the beta-required AdaFace IR101,
ElasticFace Arc R100, and TransFace-S matrix under two frozen SCRFD preprocessing conditions;
catalogued MagFace, TopoFR, TransFace-B, and prealigned-pass-through research extensions; imported
external scores marked `EXTERNALLY_COMPUTED`) over frozen protocols,
with every number recorded as an immutable, hash-addressed Run: verification (FMR/FNMR/ROC/DET/EER),
quality→utility (ERC, AU-ERC, risk-coverage, quality-conditioned error, error models, threshold-policy
analysis), cross-matcher and preprocessing contrasts, 1:N identification, controlled degradation,
provenance-bearing figures, dashboards, reports, exports, and reproduction bundles. The beta-4 release-candidate
workflow is local and performs no automatic egress. Operator-downloaded reports, exports, and
reproduction bundles remain governed files: they may contain deployment-local provenance and require
disclosure review before any external transfer. Population 1:N, dataset degradation, and container
execution remain research extensions outside beta acceptance.

The left-navigation console and its **Manage system** surface expose the closed, code-owned catalog: six matcher
drivers, the registered identity-labeled LFW driver, three downloadable pair-verification dataset
drivers, three swappable matcher-preprocessing profiles (two SCRFD source-image profiles and one
publisher-prealigned ArcFace-112 pass-through), and seven
controlled-degradation operators. It verifies matcher checkpoints and pinned architecture sources,
performs explicit source pulls, acquires the owner-admitted research checkpoints and verification
archive into private managed storage with exact byte verification and durable receipts, loads/unloads the actual shared matcher runtimes used by jobs,
inventories project embeddings, and records each lifecycle action. Matcher and preprocessing choices
are made per Run. This beta admits no remote API connections or arbitrary plug-in code. AdaFace and
TransFace checkpoint rights remain unlicensed or repository-license-only facts, so their explicit
owner authorization permits local noncommercial research acquisition but never redistribution. Direct
acquisition of the identity-labeled LFW tree remains blocked; the three author-published, pair-only
verification protocols are downloadable and carry no fabricated identity labels.

**Status:** 0.1.0-beta.3 passed its release gate but failed the installed-package browser gate. The current
tree targets 0.1.0-beta.4 and must pass the complete controlled-workstation release and installed-package
gates before that version is tagged. The target scope
and provisional evidence are recorded in `docs/BETA_ACCEPTANCE.md`; no local beta result grants DHS transition,
remote deployment, production, ATO, or mission-readiness authority.

## Start here

The installer requires Linux x86-64, Python 3.11.2, `uv` 0.9.26, Node 26.7.0, pnpm 10.28.0, and the registered external
OFIQ/resource locations described in `docs/INSTALL.md`.

This beta is not a generic clone-and-run install. On the reviewed workstation, run from the deployment
root recorded by its controlled registry after making the registered OFIQ/runtime assets available:

```bash
bash scripts/install.sh
env PYTHONPATH="$PWD/backend/src:$PWD" uv run --directory backend --frozen python -m qualsight.cli setup
python3 scripts/offline_gate.py --observation var/offline-gate.json
env PYTHONPATH="$PWD/backend/src:$PWD" uv run --directory backend --frozen --offline python -m qualsight.cli serve
```

The `setup` command is the explicit network-enabled acquisition step for the exact pinned matcher
architecture sources. It verifies and caches those sources before the offline gate; scientific
execution then consumes the verified cache with network access disabled.

An extracted release archive additionally requires `QUALSIGHT_REGISTRY_PATH` to the reviewed,
unprojected controlled-host registry. `scripts/install.sh --validate-package-only` checks the embedded
`QUALSIGHT-PACKAGE-CONTENTS.json`, the exact projected membership, hashes, sizes, safe modes, archive
boundary, and toolchain without installing dependencies or claiming that external resources are present.
The manifest must declare `0644` or `0755`; a hardened extraction umask may only remove group/world
permissions while preserving required owner access.

The offline gate owns port 8480, starts and stops its own server, and must run while no QualSight service
is active. After it exits 0, start the product with the next command and open
<http://127.0.0.1:8480>. Installation is for the controlled reference workstation; it is not a
relocatable cold-start package.

* Use: `docs/USER_GUIDE.md` (console at <http://127.0.0.1:8480> after the explicit serve command above). Start on
  **Home**, choose or create a project under **Projects**, admit data under **Inputs**, then use
  **Inspect quality**, **Compare**, **Stress test**, **Analyze utility**, **Results**,
  **Evidence & reports**, and **Manage system**. Every CLI
  block in the guide is also executed verbatim by `scripts/docs_workflow_gate.py`.
* Methods and limitations: `docs/SCIENTIFIC_METHODS.md`. Threat model and local data controls: `docs/THREAT_MODEL.md`.
* OFIQ native rebuild: `scripts/ofiq_v112_build.sh --help` uses the committed Conan 2.18.1 lock/profile,
  downloads missing upstream archives and locked packages when allowed, and verifies every archive,
  dependency, conformance result, linked library, and real-image smoke output before writing a receipt.
* Governance: `ATG_PLAN.md` / `ATG_LOOP.md` (capability graph and controller), `atg/ledger.jsonl`
  (append-only evidence ledger), `PRD.md`, `REQUIREMENTS.md`, `metrics/metric-registry.json`,
  `resources/resource-registry.json`.

## Configuration

The normal controlled-workstation start command needs no credential or remote-service environment
variables. `QUALSIGHT_DATA_DIR`, `QUALSIGHT_REGISTRY_PATH`, `QUALSIGHT_SOURCE_CACHE_ROOT`, and
`QUALSIGHT_RUNTIME_RESOURCE_ID` may override their documented local defaults for an isolated governed
installation. `QUALSIGHT_SOURCE_CACHE_ROOT` selects an existing absolute, non-symlinked directory of
manifest-verified upstream source bytes; it does not authorize a download. `QUALSIGHT_NETWORK_GUARD`
defaults to enabled. QualSight has no admitted remote provider credentials or connection configuration in
this beta.

## Rights and data

Datasets, model checkpoints, the OFIQ runtime, and derived biometric data are registered by hash and used
under recorded owner decisions for local noncommercial research. Release artifacts contain no source images,
model checkpoints, embeddings, operational databases, runtime observations, or recorded result fixtures.
Internal agent/session records and machine-specific performance records are also excluded. The source
archive does retain public protocol identifiers, registered byte hashes, and distributable aggregate
assurance metadata. The archive projects operator-home locators to the external prefix declared in its release policy, retains the
remaining controlled-host bindings and public acquisition pins, and requires a separately reviewed full
registry at installation; it is not a portable or generally redistributable data package.
Archive creation fails closed on prohibited biometric/model/data/native/screenshot file classes and
high-confidence credential indicators. Every projected regular file is also bound by path, SHA-256, byte
length, and archive-declared safe mode in `QUALSIGHT-PACKAGE-CONTENTS.json`; release staging extracts the archive
and validates it with the committed-tree validator and expected commit before publication. See `resources/authorizations/` and
`resources/security/release-package-policy-v1.json`.

Scientific work is bounded and recoverable within the local beta. Direct matcher execution has a hard
deadline in a terminable process. Quality batches, matcher matrices, external-score imports, direct-pair
comparisons, derived images, utility/cross-matcher/degradation/identification analyses, figures, reports,
exports, and bundles use operation-bound prepared publication or equivalent durable result recovery before
SQLite completion is claimed. Startup and exact-request retry reconcile only integrity-checked output.
Transformation requests are refused before execution above 20,000 derived files; owner protocols refuse
more than 20,000 admitted source samples or 20,000 output pairs before pair objects are materialized.

## Controlled-checkout layout

`backend/` (Python 3.11 control plane, engines, matchers, metrics, analyses, reporting, security),
`frontend/` (Vite/React PWA console), `scripts/` (installation, gates, validators, verifiers),
`tests/` and `backend/tests/` (real-data tests only), `docs/`, `metrics/`, `resources/`, `atg/`.

The release source archive intentionally omits both test trees under its documented package policy.
Those directories describe the clean controlled Git checkout used to build the archive, not the
extracted package layout.

Architecture and trust boundaries are in `docs/ARCHITECTURE.md`; the local browser/API contract is in
`docs/API.md`.

The complete release-authority split between controlled beta requirements and DHS transition-only
controls is frozen in `REQUIREMENTS.md`. Remote API connectors remain unadmitted.
