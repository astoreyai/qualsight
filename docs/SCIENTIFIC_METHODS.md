# QualSight scientific methods

Every reportable number is produced by a registered, versioned definition and recorded in an
immutable Run. This document names the definitions, the fixed choices, and the limitations a reader
needs to interpret QualSight outputs. Definitions live in `metrics/metric-registry.json`
(`qualsight-core-metrics` v1.2.0, 19 definitions, validated by `scripts/validate_metric_registry.py`).

## Quality (OFIQ v1.1.2)

The registered engine is the BSI OFIQ v1.1.2 build (ISO/IEC 29794-5 reference implementation),
executed as a subprocess in list-file mode. Every sample record carries 28 measures × native value,
scalar (0–100) value, and a failed flag; the engine's `-1`/`nan` sentinels are recorded as failures,
never imputed. UnifiedQualityScore is the default quality measure for utility analysis; any of the 28
scalar measures can be selected.

## Verification (1:1)

* Protocol: LFW View 2, 6,000 pairs in 10 folds, frozen from the registered official pair lists into
  an immutable protocol record (pairs SHA-256 recorded). Subject identity is the LFW directory name.
* Matchers: AdaFace IR-101, ElasticFace-Arc R100, TransFace-S (registered checkpoints, strict
  state-dict loads, pinned architecture sources), cosine similarity of L2-normalized 512-d embeddings,
  score direction HIGHER_IS_MORE_SIMILAR, decision rule ACCEPT_WHEN_SCORE_GE_THRESHOLD.
* Preprocessing conditions (both always reported, never pooled): SCRFD-10G detection + ArcFace
  5-point similarity alignment to 112×112 under `SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1`
  (adaptive canvas, largest face, JPEG-95 crop) and `SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1`
  (fixed 640 canvas, highest-confidence face, in-memory crop). A pair without two templates carries
  an explicit processing failure (FAILURE_TO_DETECT / FAILURE_TO_ACQUIRE / INTEGRITY_FAILURE) and no score.
* Metrics (`biometric.verification.*@1.0.0`): FMR, FNMR, ROC, DET, EER (linear interpolation of the
  crossing), operating points at target FMR 10⁻¹, 10⁻², 10⁻³, View-2 ten-fold accuracy; two failure
  policies reported separately — EXCLUDE_FAILED_TRIALS and FAILURES_AS_REJECT.
* Uncertainty: exact Clopper–Pearson 95 % binomial intervals for FMR/FNMR at operating points; event,
  trial, and subject counts are always shown; operating points that the comparison population cannot
  support are flagged rather than reported.

## Quality → utility (`biometric.quality.utility.*@1.0.0`)

* Pair-quality policy (PRD §21), versioned 1.0.0: min, max, mean, geometric mean, enrollment (left
  sample), probe (right sample). The policy is part of the analysis identity.
* ERC: retained-set error rate as worst-pair-quality units are rejected first, ties leaving together,
  at a **fixed matcher threshold** taken from the recorded operating point (default FMR ≤ 10⁻³,
  EXCLUDE_FAILED_TRIALS) or supplied by the caller — never re-estimated along the curve. FNMR-ERC
  (mated units) and FMR-ERC (non-mated units) are separate.
* AU-ERC over [0, 1] and partial AU-ERC over [0, 0.2] (interval recorded) by the trapezoid rule over
  the recorded step points; risk-coverage is the same curve over retained coverage = 1 − rejected fraction.
* Quality-conditioned FNMR/FMR: exact binomial rates per recorded bin (default ten equal-width bins
  over the measure's 0–100 range); bins with fewer than 30 units are flagged as unsupported precision.
* Association: Spearman ρ (Fisher-z interval), Kendall τ-b, and a Newton–Raphson logistic regression
  of the error indicator on pair quality (Wald intervals; odds ratio per 10 quality units) that runs
  only when ≥ 10 events and ≥ 10 non-events per parameter exist. Quality as failure predictor: ROC-AUC
  (Mann–Whitney), PR-AUC (average precision), sensitivity/specificity/precision/recall at the quality
  policy threshold; both outcome classes are required.
* Uncertainty for areas: subject-cluster bootstrap (clusters = left subject id, 1,000 replicates by
  default, seed recorded, percentile 95 % intervals). Comparison units that share subjects are not
  independent observations; Wald intervals are labelled approximate.
* Threshold-policy analysis (`biometric.policy.threshold-analysis@1.0.0`): for pair quality ≥ Q at
  fixed matcher T — retention and rejection rates, FMR/FNMR before and after with exact intervals,
  errors prevented, successful comparisons removed, and the per-quality-measure impact at
  retention-matched cutoffs (a diagnostic, not a recommendation). Threshold kinds MATCHER THRESHOLD,
  QUALITY POLICY THRESHOLD, and STANDARDS/ENGINE ASSESSMENT are labelled and never conflated.

## Cross-matcher (PRD §22)

Matcher contrasts hold the preprocessing condition fixed; preprocessing contrasts hold the matcher
and protocol fixed; nothing is pooled across conditions. Per contrast: error overlap at each Run's own
recorded threshold over units scored by both (A only / B only / both / neither; mated and non-mated
separately), processing-failure overlap, failure-set Jaccard, ERC overlays on the shared units,
AU-ERC differences with paired subject-cluster bootstrap intervals, a quality × matcher logistic
interaction (approximate: pooled rows share subjects), threshold transferability (A's threshold on
B and B's on A next to each Run's own rates), score agreement, and quality rank consistency. No
single "matcher independence score" is defined.

## Reproducibility

Every Run records the dataset manifest hash, model hashes, engine/matcher/preprocessing bindings,
environment (uv.lock identity), hardware, configuration, metrics, artifacts, and warnings. Analyses
are content-addressed by their inputs; `scripts/verify_utility_analysis.py` and
`scripts/verify_cross_matcher.py` re-derive results with independent code; `.qsexp` bundles re-execute
under `reproduce-bundle` and report EXACT / WITHIN_TOLERANCE / DIFFERENT / MISSING_DEPENDENCY /
MISSING_DATA / BLOCKED (score tolerance 1e-5 for re-embedded matcher Runs on the same device, 1e-3 when the original and the reproduction ran on different devices — cuda vs cpu kernels — with both devices and the tolerance basis recorded; documents 1e-9). The matcher device is chosen per Run (CUDA when it has headroom on the shared workstation, else CPU) and recorded in the matcher binding.

## Limitations that travel with every result

* LFW results are QualSight-executed on the funneled release with SCRFD alignment under two frozen
  crops; they are not reproductions of the matchers' published LFW figures.
* Matcher training-set identity exclusion is each matcher's published claim (owner decision 19).
* The fixed-640-canvas condition misses 43 protocol pairs' faces on the 250×250 funneled images while
  the adaptive canvas finds all; the failures are explicit in the Runs.
* On the real six-Run matrix the two crops fail on nearly disjoint mated pairs (Jaccard 0.06–0.07)
  while the three matchers agree within a crop (Jaccard 0.93–0.97): face selection on multi-face
  images, not the matcher, drives most false non-matches at FMR ≤ 10⁻³. Interpret matcher contrasts
  with that in mind.
