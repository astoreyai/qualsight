# Changelog

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
