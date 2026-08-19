# Changelog

## Unreleased (Plan D/E — DHS demo readiness, decisions 30–31)

* Owner-supplied local inputs (decision 30): single-photo upload assessed by the registered engine
  as a recorded Run (`POST /api/quality/assess-upload`, console *Assess your own photo*,
  `assess-owner` CLI); identity-labelled directory / CSV-manifest imports with saved reusable field
  mappings; deterministic seeded owner pair protocols executing on the real matcher matrix; purge
  coverage; source kind `OWNER_SUPPLIED_LOCAL_INPUT` on every result.
* External-score import accepts an interactive field mapping (PRD §12) with saved mappings.
* Visual evidence: SCRFD detection overlays (labelled as the matcher pipeline's), OFIQ's own
  ADNet-98 landmarks through a registered auxiliary binary built from the registered v1.1.2 source,
  compared 112×112 crops recomputed under the recorded frozen condition, and a match-evidence panel
  placing each pair's score on its Run's genuine/impostor distributions with recorded thresholds,
  decisions, association-only quality attribution, and a fixed honesty statement.
* Routed console navigation (PRD §44): one view per route with honest empty states.
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
