# QualSight

Local, offline research workbench that relates **face image quality** (OFIQ v1.1.2, the ISO/IEC 29794-5
reference implementation) to **face-matcher utility** (AdaFace, ElasticFace, MagFace, TopoFR,
TransFace-B, and TransFace-S under two frozen
preprocessing conditions; imported external scores marked `EXTERNALLY_COMPUTED`) over frozen protocols,
with every number recorded as an immutable, hash-addressed Run: verification (FMR/FNMR/ROC/DET/EER),
quality→utility (ERC, AU-ERC, risk-coverage, quality-conditioned error, error models, threshold-policy
analysis), cross-matcher and preprocessing contrasts, 1:N identification, controlled degradation,
provenance-bearing figures, dashboards, reports, exports, and reproduction bundles. Nothing leaves the
workstation.

The *Add-ons · Connections* control surface exposes the closed, code-owned catalog: six matcher
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

**Status:** 0.1.0-beta.2 — the 41-node controlled-workstation beta capability graph is accepted end to
end on real data (`docs/BETA_ACCEPTANCE.md`). Later local research work implemented 1:N identification,
controlled transformations, and a container execution adapter, but those extensions remain outside the 0.1.0 beta acceptance scope and grant no DHS transition or deployment authority.

## Start here

* Install and package boundary: `docs/INSTALL.md` (controlled reference workstation; not a relocatable
  cold-start package). Run `bash scripts/install.sh`, then `python3 scripts/offline_gate.py`.
* Use: `docs/USER_GUIDE.md` (console at <http://127.0.0.1:8480> after `qualsight serve`; every CLI block
  in the guide is executed verbatim by `scripts/docs_workflow_gate.py`).
* Methods and limitations: `docs/SCIENTIFIC_METHODS.md`. Threat model and local data controls: `docs/THREAT_MODEL.md`.
* Demonstration: `bash scripts/demo.sh` runs the whole workflow on the registered subset fixture and starts the console.
* OFIQ native rebuild: `scripts/ofiq_v112_build.sh --help` uses the committed Conan 2.18.1 lock/profile,
  downloads missing upstream archives and locked packages when allowed, and verifies every archive,
  dependency, conformance result, linked library, and real-image smoke output before writing a receipt.
* Governance: `ATG_PLAN.md` / `ATG_LOOP.md` (capability graph and controller), `atg/ledger.jsonl`
  (append-only evidence ledger), `PRD.md`, `REQUIREMENTS.md`, `metrics/metric-registry.json`,
  `resources/resource-registry.json`.

## Demonstration

`bash scripts/demo_dhs.sh` starts the console on the recorded evidence, verifies the demo state,
and ensures the *Stakeholder Demo* dashboard; the timed 15-minute operator flow is
`docs/DEMO_SCRIPT.md`. Evaluators can bring their own photo or directory (decision 30): inputs are
hashed, recorded as `OWNER_SUPPLIED_LOCAL_INPUT`, assessed by the registered engine, never
redistributed, and purgeable.

## Rights and data

Datasets, model checkpoints, the OFIQ runtime, and derived biometric data are registered by hash and used
under recorded owner decisions for local noncommercial research. Release artifacts contain no source images,
model checkpoints, embeddings, operational databases, runtime observations, or recorded result fixtures.
Internal agent/session records and machine-specific performance records are also excluded. The source
archive does retain public protocol identifiers, registered byte hashes, and distributable aggregate
assurance metadata. It also retains deployment registry paths needed to locate owner-held external assets,
so it is a controlled-host research package rather than a portable or generally redistributable data package.
Archive creation fails closed on prohibited biometric/model/data/native/screenshot file classes and
high-confidence credential indicators. See `resources/authorizations/` and
`resources/security/release-package-policy-v1.json`.

## Layout

`backend/` (Python 3.11 control plane, engines, matchers, metrics, analyses, reporting, security),
`frontend/` (Vite/React PWA console), `scripts/` (installation, gates, validators, verifiers),
`tests/` and `backend/tests/` (real-data tests only), `docs/`, `metrics/`, `resources/`, `atg/`.

The complete release-authority split between controlled beta requirements and DHS transition-only
controls is frozen in `REQUIREMENTS.md`. Remote API connectors remain unadmitted.
