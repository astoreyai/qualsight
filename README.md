# QualSight

Local, offline research workbench that relates **face image quality** (OFIQ v1.1.2, the ISO/IEC 29794-5
reference implementation) to **face-matcher utility** (AdaFace, ElasticFace, TransFace under two frozen
preprocessing conditions; imported external scores marked `EXTERNALLY_COMPUTED`) over frozen protocols,
with every number recorded as an immutable, hash-addressed Run: verification (FMR/FNMR/ROC/DET/EER),
quality→utility (ERC, AU-ERC, risk-coverage, quality-conditioned error, error models, threshold-policy
analysis), cross-matcher and preprocessing contrasts, 1:N identification, controlled degradation,
provenance-bearing figures, dashboards, reports, exports, and reproduction bundles. Nothing leaves the
workstation.

**Status:** 0.1.0-beta.1 — the 41-node beta capability graph is accepted end to end on real data
(`docs/BETA_ACCEPTANCE.md`); post-beta cycle C25 added 1:N identification, controlled transformations,
and a container execution adapter.

## Start here

* Install: `docs/INSTALL.md` (`bash scripts/install.sh`; then `python3 scripts/offline_gate.py`).
* Use: `docs/USER_GUIDE.md` (console at <http://127.0.0.1:8480> after `qualsight serve`; every CLI block
  in the guide is executed verbatim by `scripts/docs_workflow_gate.py`).
* Methods and limitations: `docs/SCIENTIFIC_METHODS.md`. Threat model and local data controls: `docs/THREAT_MODEL.md`.
* Demonstration: `bash scripts/demo.sh` runs the whole workflow on the registered subset fixture and starts the console.
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
under recorded owner decisions for local noncommercial research; **none of those bytes are bundled with,
committed to, or redistributed from this repository.** See `resources/authorizations/`.

## Layout

`backend/` (Python 3.11 control plane, engines, matchers, metrics, analyses, reporting, security),
`frontend/` (Vite/React PWA console), `scripts/` (installation, gates, validators, verifiers),
`tests/` and `backend/tests/` (real-data tests only), `docs/`, `metrics/`, `resources/`, `atg/`.
