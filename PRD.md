# QualSight
## Biometric Quality, Recognition & Evaluation Workstation
**Product Requirements Document — v0.3**
**Date:** August 13, 2026
**Repository working name:** `ofiqgui`
**Product:** QualSight
**Deployment:** Clarkson University-controlled research workstation
**License objective:** Open source
**Initial modality:** Face
**Architecture:** Local-first scientific backend + React/TypeScript PWA console
**Primary stakeholders:** Clarkson University researchers, DFAB, DHS research/evaluation stakeholders

> This document is the canonical PRD. It supersedes the product-scope sections of `PLAN.md` (v0.2), which is retained as an engineering/research companion (OFIQ integration mechanics, dataset research). v0.3 integrates the replacement PRD and the execution-adapter architecture addendum of Aug 13, 2026.

---

# 1. Executive Summary

QualSight is an open-source scientific workstation for biometric sample-quality assessment, recognition-performance evaluation, matcher comparison, controlled experimentation, statistical analysis, failure investigation, visualization, and reproducible reporting.

The initial release focuses on face biometrics and integrates ISO/IEC 29794-5-compatible face-image quality engines such as OFIQ with biometric matchers and externally generated matcher scores.

QualSight is not merely an OFIQ graphical interface. **QualSight executes, compares, and evaluates heterogeneous biometric algorithms through reproducible typed adapters. OFIQ is the initial standards-oriented face-quality engine, not the architectural boundary of the system.**

The product shall support the complete experimental chain:

```text
Dataset
   ↓
Sample / quality assessment
   ↓
Biometric preprocessing
   ↓
Matcher
   ↓
Verification / identification experiment
   ↓
Statistical evaluation
   ↓
Failure analysis
   ↓
Visualization
   ↓
Report / publication artifact
   ↓
Reproducibility bundle
```

The beta shall run on a Clarkson-controlled research workstation. No cloud service, mobile application, kiosk deployment, or production DHS infrastructure is required.

The frontend shall be a Progressive Web App used as the interactive console for the local scientific backend. Computational work remains in backend worker processes.

The centerpiece first scientific workflow:

```text
                       IMAGE
                         │
        ┌────────────────┼─────────────────┐
        │                │                 │
        ▼                ▼                 ▼
      OFIQ            US-FIQA           CR-FIQA
        │                │                 │
        └────────────────┼─────────────────┘
                         │
                  Quality comparison
                         │
               ┌─────────┴─────────┐
               ▼                   ▼
            ArcFace              AdaFace
               │                   │
               └─────────┬─────────┘
                         ▼
              Cross-matcher analysis
                         │
       ┌─────────────────┼─────────────────┐
       ▼                 ▼                 ▼
      ERC               DET          Failure analysis
                         │
                         ▼
                Reproducible report
```

---

# 2. Scientific and Standards Context

QualSight shall distinguish three concepts that are often incorrectly conflated:

1. **Biometric sample quality**
2. **Biometric recognition performance**
3. **Operational policy**

ISO/IEC 29794-5:2025 evaluates properties of individual face images and explicitly does not define recognition comparison of image pairs or performance evaluation of FIQA algorithms. Recognition-utility experiments performed by QualSight therefore constitute a separate analysis layer.

ISO/IEC 29794-1:2024 provides the broader biometric-sample-quality framework, including interpretation, normalization, aggregation, exchange, and evaluation concepts for biometric quality measurements.

Biometric recognition evaluation shall align where applicable with ISO/IEC 19795-1:2021, which establishes biometric performance-testing principles, performance metrics, reporting requirements, and requirements intended to reduce evaluation bias. An Edition 3 revision is currently under development, so QualSight shall version metric definitions rather than hard-code standards semantics permanently.

QualSight does not provide or claim biometric-system certification.

---

# 3. Product Vision

QualSight shall make biometric evaluation:

* interactive;
* statistically rigorous;
* inspectable;
* reproducible;
* matcher-aware;
* quality-aware;
* standards-informed;
* accessible to researchers without requiring every analysis to be rebuilt as a Python script.

The desired experience is:

> **A scientific workbench for biometric T&E: dataset management + matcher evaluation + FIQA analysis + failure investigation + statistical visualization + reproducibility.**

QualSight shall expose enough detail for an evaluator to answer:

> What dataset, images, matcher, quality engine, configuration, threshold, protocol, statistical method, code version, and software environment produced this number?

---

# 4. Primary Product Goals

## G1 — Face image quality analysis
Evaluate individual images using OFIQ and future FIQA engines and expose:

* unified/global quality values;
* individual component measurements;
* raw measurements;
* quality-engine status;
* assessment failures;
* configuration;
* engine and model version.

## G2 — Biometric matcher evaluation
Evaluate 1:1 verification and eventually 1:N identification systems using consistent, explicitly defined protocols.

## G3 — Quality-to-utility analysis
Measure whether and how biometric quality predicts downstream recognition performance.

## G4 — Cross-matcher analysis
Determine whether quality-performance relationships transfer between matchers or are matcher-specific.

## G5 — Experiment reproducibility
Preserve every completed scientific run as an immutable, provenance-bearing artifact.

## G6 — Failure investigation
Allow users to move from aggregate error metrics directly to the samples and comparisons responsible for them.

## G7 — Stakeholder communication
Generate defensible figures, dashboards, tables, and reports suitable for scientific review and controlled DFAB/DHS demonstrations.

---

# 5. Non-Goals for Initial Beta

The beta shall not attempt to provide:

* operational biometric identity decisions;
* DHS production deployment;
* agency accreditation;
* cloud hosting;
* multi-tenant SaaS;
* mobile applications;
* tablet/kiosk applications;
* remote capture stations;
* enterprise SSO;
* commercial biometric SDK redistribution;
* autonomous scientific conclusions;
* biometric certification;
* production identity-proofing workflows.

The architecture may support these later without making them beta dependencies.

---

# 6. User Model

## 6.1 Roles

QualSight shall support the following logical roles:

| Role          | Purpose                                          |
| ------------- | ------------------------------------------------ |
| Administrator | Application, engines, resources, users           |
| Researcher    | Create projects, experiments, runs and artifacts |
| Reviewer      | Inspect runs, annotate results, create reports   |
| Viewer        | Read-only project and report access              |

## 6.2 Beta authentication

The beta shall support:

```text
Single-user mode
```

and optionally:

```text
Local-account mode
```

Single-user mode may automatically associate activity with the configured researcher.

The data model shall nevertheless contain:

```text
users
roles
projects
project_members
audit_events
```

so authentication can be enabled without restructuring scientific data later.

Enterprise SSO/OIDC is post-beta.

---

# 7. Project Model

The primary organizational unit shall be a **Project**.

```text
Project
│
├── Datasets
├── Samples
├── Subjects
├── Pair Sets
├── Quality Engines
├── Matchers
├── Protocols
├── Experiments
├── Runs
├── Visualizations
├── Dashboards
├── Reports
├── Annotations
└── Artifacts
```

Example:

```text
Cross-Matcher Face Quality Evaluation — Fall 2026
```

Projects shall support: New; Open; Save; Save As; Archive; Clone; Import; Export; metadata; notes; tags; ownership; access policy.

---

# 8. Load, Save and Scientific State

QualSight shall preserve complete scientific project state.

Saving a project shall preserve:

* dataset manifests;
* subject mappings;
* pair-generation definitions;
* quality-engine configurations;
* matcher configurations;
* experimental protocols;
* visualization configuration;
* dashboard layouts;
* annotations;
* completed run references;
* report definitions.

Draft configurations may be editable. Completed Runs shall be immutable. Changing a parameter after execution shall create a new Run rather than altering the previous result.

```text
Experiment E07
   │
   ├── Run R102 — Matcher A
   ├── Run R103 — Matcher B
   └── Run R104 — Matcher B + threshold change
```

QualSight shall provide:

> Compare Run

for any compatible runs.

---

# 9. Scientific Run Model

Each execution shall create an immutable Run containing at minimum:

```text
run_id
project_id
experiment_id
user_id
dataset manifest
dataset hash
subject protocol
pair/search protocol
quality engine
quality engine version
quality engine configuration
model hashes
matcher
matcher version
matcher model hash
matcher preprocessing
operating thresholds
statistical methods
confidence-interval method
random seeds
application version
Git commit
dirty-state information
Python/environment identity
dependency manifest
hardware
device
start time
end time
runtime
metrics
artifacts
logs
warnings
status
```

Run states shall distinguish:

```text
QUEUED
RUNNING
FAILED
CANCELLED
COMPLETED
VALIDATED
REPRODUCED
```

The product shall explicitly preserve:

```text
COMPLETED ≠ VALIDATED ≠ REPRODUCED ≠ SCIENTIFICALLY SUPPORTED
```

This principle is retained from the existing scientific architecture.

---

# 10. Dataset Workbench

The Dataset Workbench shall support:

* directory import;
* individual sample import;
* CSV manifests;
* JSON manifests;
* Parquet manifests;
* ZIP/archive ingestion;
* existing QualSight project bundles;
* identity mappings;
* genuine-pair lists;
* impostor-pair lists;
* enrollment/probe definitions;
* dataset splits;
* dataset hashing;
* integrity checks.

Each dataset shall carry a manifest containing:

```text
dataset_id
name
version
source
license
retrieval date
manifest hash
number of subjects
number of samples
modality
identity schema
metadata schema
redistribution permission
display permission
derived-artifact policy
PUBLIC / PRIVATE / RESTRICTED / SYNTHETIC / GENERATED
```

**`modality` is a required schema field from day one** (initial value: `face`). Samples are never implicitly assumed to be faces forever; the schema anticipates fingerprint, iris, voice, and multimodal futures (§51.9).

---

# 11. Dataset Statistics

Every dataset summary shall report at least:

### Population
* number of subjects;
* number of images/samples;
* samples per subject;
* median samples per subject;
* minimum/maximum samples per subject.

### Evaluation support
* number of mated comparisons;
* number of non-mated comparisons;
* number of probes;
* gallery size;
* enrollment-template count.

### Data completeness
* missing files;
* missing identity labels;
* duplicate files;
* duplicate hashes;
* unreadable files;
* images without detected faces;
* images with multiple detected faces.

### Quality coverage
* quality-assessment success rate;
* quality-assessment failure rate;
* distribution of overall quality;
* component completeness.

---

# 12. Import Center

A centralized Import Center shall support mapping external data into canonical QualSight schemas.

## Supported inputs

### Images and datasets
```text
directory · CSV · JSON · Parquet · archive · manifest
```

### Matcher results
```text
pair score files · score matrices · mated lists · non-mated lists · candidate lists · embeddings
```

### Experiments
```text
QualSight project · QualSight experiment · QualSight run/reproduction bundle
```

The importer shall provide an interactive mapping interface such as:

```text
External field       QualSight field
-----------------------------------------
person_id          → subject_id
file               → sample.path
similarity         → comparison.score
is_match           → comparison.mated
```

Mappings shall themselves be saved and reusable.

All externally computed results shall carry the provenance marker **`EXTERNALLY COMPUTED`**, permanently distinguished from **`QUALSIGHT EXECUTED`** (§51.8).

---

# 13. Quality Engine Architecture

Quality systems shall implement a common adapter contract while preserving engine-specific semantics. OFIQ is one member of a larger quality registry:

```text
Face Quality
│
├── OFIQ C++
├── python-ofiq
├── OFIQpy
├── OpenFIQA
├── US-FIQA
├── SER-FIQ
├── CR-FIQA
├── MagFace quality
├── FaceQAN
├── SDD-FIQA
├── eDifFIQA
└── Custom FIQA
```

This breadth is scientifically valuable because these methods estimate different notions of utility and are often recognition-model dependent. SER-FIQ estimates face-image quality using stochastic embedding robustness and its published implementation is based on ArcFace ("SER-FIQ (on ArcFace)"). CR-FIQA learns relative classifiability with an official implementation intended for recognition-utility quality evaluation. MagFace jointly provides recognition embeddings and a quality signal derived from feature magnitude. FaceQAN and SDD-FIQA provide further FIQA approaches integrable through the same interface.

A QualityResult shall contain:

```text
engine
engine_version
engine_build
input_hash
configuration_hash
model_hashes
overall_quality
component_measurements
raw_measurements
statuses
warnings
artifacts
runtime
provenance
```

The Quality Engine Comparison screen shall display per-image scores across engines:

```text
Image                    Score
──────────────────────────────
OFIQ                     72
SER-FIQ                  .61
CR-FIQA                  .84
MagFace quality          23.8
US-FIQA                  .77
```

**The UI shall not pretend these numbers share the same scale.** Scores from different FIQA systems shall never be silently treated as equivalent. Normalization shall be an explicit, provenance-bearing transformation.

QualSight shall additionally support **generic (non-biometric) image-quality tools** — blur, brightness, contrast, entropy, SNR, sharpness, compression artifacts, no-reference IQA models — as a labeled category. They may help answer *why* a biometric quality engine responds as it does. The UI shall distinguish:

```text
IMAGE QUALITY  vs  BIOMETRIC UTILITY QUALITY  vs  STANDARDIZED BIOMETRIC QUALITY
```

---

# 14. Single-Sample Quality Analysis

The sample inspector shall display:

```text
Original image
Face region
Landmarks where available
Metadata
Overall quality
Component measurements
Raw measurements
Engine status
Operational thresholds
Annotations
Provenance
```

Three types of interpretation shall be visually distinguished:

### Engine measurement
Example: `Sharpness = 74`

### Standards/engine assessment
Only where explicitly supported.

### User operational policy
Example: `Research policy: Warn if sharpness < 60`

QualSight shall never present user-selected thresholds as ISO requirements.

---

# 15. Verification — 1:1 Metrics

The following shall be first-class biometric metrics.

## 15.1 FMR
**False Match Rate** — proportion of non-mated comparisons incorrectly accepted at a specified threshold.

## 15.2 FNMR
**False Non-Match Rate** — proportion of mated comparisons incorrectly rejected at a specified threshold. Current NIST FRTE 1:1 evaluations report FNMR at explicitly controlled FMR operating points.

## 15.3 TMR/TAR
True Match Rate / True Accept Rate: `1 - FNMR`. QualSight shall prefer standards-consistent terminology in scientific output while permitting aliases in the UI.

## 15.4 ROC
Receiver Operating Characteristic.

## 15.5 DET
Detection Error Tradeoff. DET shall be a primary biometric visualization rather than a generic ROC-only presentation.

## 15.6 EER
Equal Error Rate — reported as a convenience summary metric but shall **not** replace performance at application-relevant operating points.

## 15.7 AUC and pAUC
ROC AUC; partial AUC over explicitly specified operating ranges.

## 15.8 Threshold operating-point tables

For each matcher:

```text
Threshold · FMR · FNMR · TMR · number of false matches · number of false non-matches · 95% CI
```

---

# 16. Statistically Supportable Error Rates

QualSight shall not imply measurement precision unsupported by the experiment.

For every FMR/FNMR/FPIR/FNIR estimate, the application shall show:

```text
event count
trial count
number of unique subjects
point estimate
confidence interval
confidence level
resampling method
```

The system shall flag operating points for which the available comparison population cannot support a credible estimate. Example warning:

> Insufficient non-mated comparisons to characterize FMR = 10⁻⁶ with meaningful statistical precision.

This is particularly important because contemporary NIST FRTE evaluates high-performing face matchers at very low FMR values using extremely large comparison populations.

Pair counts shall not automatically be treated as independent observations when samples or subjects are reused. Subject-aware/hierarchical resampling shall be supported.

---

# 17. Identification — 1:N Metrics

**1:N is not required for the earliest beta, but its schema and metric architecture shall be designed now.** Verification-only schemas would create an unnecessary architectural dead end; NIST's current evaluation program treats 1:1 verification and 1:N identification as distinct performance regimes.

## 17.1 FPIR
**False Positive Identification Rate** — for open-set searches, the proportion of non-mated searches that return at least one candidate above threshold.

## 17.2 FNIR
**False Negative Identification Rate** — proportion of mated searches that fail to return the correct identity above threshold. Current NIST FRTE 1:N reporting uses FNIR at specified FPIR operating points.

## 17.3 TPIR
True Positive Identification Rate where applicable.

## 17.4 Rank-k identification
Report Rank 1 / 5 / 10 / 20 / 100 when appropriate to the gallery size and protocol.

## 17.5 CMC
Cumulative Match Characteristic.

## 17.6 True-mate rank distribution
Histogram/CDF of correct-mate rank.

## 17.7 Gallery effects
Performance shall be analyzable against: gallery size; probe quality; gallery quality; number of enrolled samples; template composition.

Closed-set and open-set identification experiments shall be explicitly distinguished.

---

# 18. Acquisition and Enrollment Metrics

QualSight shall support metrics that precede matching:

## Failure to Acquire — FTA
Rate at which the capture/process cannot obtain a usable biometric sample.

## Failure to Enroll — FTE
Rate at which subjects cannot produce a usable biometric enrollment/reference.

## Failure to Assess — FTA-Q
Engine-specific failure to generate a quality assessment. Use a distinct internal name to avoid ambiguity with acquisition FTA.

## Failure to Template
Rate at which the matcher cannot produce a template/embedding.

## Failure to Compare
Rate at which a valid comparison score cannot be produced.

These failures shall not silently disappear from recognition metrics. QualSight shall report both:

```text
conditional recognition performance
```

and, when possible:

```text
end-to-end performance including processing failures
```

ISO/IEC 19795-10 explicitly considers failure-to-enrol and failure-to-acquire among performance measures relevant to population comparisons.

---

# 19. Quality Metrics

For each quality engine, report: global/unified quality; all component measurements; raw measurements; assessment status; assessment latency; missing/unsupported measures; measure distributions; percentile ranks; dataset coverage.

Quality metrics shall remain engine-scoped. ISO/IEC 29794-1 recognizes quality-score aggregation and quality-algorithm evaluation as distinct quality-framework concerns.

---

# 20. Quality-Utility Metrics

This shall be one of QualSight's primary differentiating modules.

## 20.1 ERC / EDC
Error-versus-Reject or Error-versus-Discard Curve. Display error performance as progressively lower-quality samples are rejected.

## 20.2 AU-ERC
Area under the ERC over an explicitly defined rejection interval. The integration domain shall always be recorded.

## 20.3 Partial AU-ERC
Support limited ranges such as 0–10% / 0–20% rejection when operationally relevant.

## 20.4 Risk-Coverage
Report error/risk as a function of retained dataset coverage.

## 20.5 Quality-conditioned FNMR
`FNMR | quality bin`

## 20.6 Quality-conditioned FMR
Where statistically supportable: `FMR | quality bin`

## 20.7 Quality-error association
Spearman correlation; Kendall rank correlation where useful; regression coefficient; odds ratio from error models; confidence intervals.

## 20.8 Quality as failure predictor
Treat matcher failure as an outcome and report: ROC-AUC; PR-AUC/AUPRC; sensitivity; specificity; precision; recall. AUPRC is particularly useful where matcher failures are rare.

## 20.9 Calibration metrics
Only when a model explicitly predicts error probability or another probabilistic target: Brier score; negative log-likelihood; expected calibration error; reliability diagram. Raw FIQA or matcher scores shall not be described as calibrated probabilities merely to make these metrics available.

---

# 21. Pair-Quality Aggregation

ISO/IEC 29794-5 evaluates single face images rather than pairs. When quality is used to characterize a biometric comparison, QualSight shall require an explicit pair aggregation policy. Supported examples:

```text
minimum(Q1, Q2)
maximum(Q1, Q2)
mean(Q1, Q2)
geometric mean
enrollment quality
probe quality
custom registered function
```

The chosen aggregation rule shall become part of the experimental protocol. Sensitivity to aggregation method shall be evaluable.

---

# 22. Cross-Matcher Metrics

Cross-matcher evaluation shall quantify whether a quality method predicts utility consistently across recognition systems. Required analyses:

### ERC overlay
One quality measure against multiple matchers.

### AU-ERC difference
With paired/bootstrap confidence intervals.

### Quality rank consistency
Compare quality rankings against matcher-specific failure patterns.

### Error overlap
For Matcher A and Matcher B: failed by A only / failed by B only / failed by both / failed by neither.

### Failure-set Jaccard similarity
Optional diagnostic of matcher failure overlap.

### Quality × Matcher interaction
Regression or mixed-effects analysis shall test whether the relationship between quality and error differs significantly by matcher.

### Threshold transferability
Evaluate consequences of policies learned with one matcher and applied to another.

### Cross-dataset transfer
Repeat quality-performance relationships across datasets.

No single derived "matcher independence score" shall be introduced without a documented and versioned scientific definition.

---

# 23. Score-Distribution Metrics

For each matcher: genuine/mated distribution; impostor/non-mated distribution; mean; median; standard deviation; quantiles; overlap visualization.

Optional research diagnostics: decidability/separation index; KS statistic; distribution overlap metrics. These shall be labeled research diagnostics rather than standards-required metrics unless a source standard explicitly defines otherwise.

---

# 24. Threshold and Policy Analysis

Users shall be able to define an operating policy such as:

```text
Quality >= 60
Matcher threshold = T
```

and inspect:

```text
retention rate
rejection rate
FMR
FNMR
number rejected
number of errors prevented
number of successful comparisons removed
per-quality-measure impact
```

Thresholds shall be identifiable as:

```text
MATCHER THRESHOLD · QUALITY POLICY THRESHOLD · STANDARDS/ENGINE ASSESSMENT
```

and shall never be visually conflated. Matcher thresholds shall not be silently re-optimized during ERC rejection unless an explicit experimental protocol requests threshold re-estimation.

---

# 25. Demographic and Population-Differential Analysis

This module shall exist in the architecture from the beginning but may remain disabled until appropriate datasets, metadata, research approvals, and stakeholder requirements exist.

ISO/IEC 19795-10:2024 specifically addresses biometric performance variation across demographic groups, including FTE, FTA, comparison-score shifts, recognition errors, throughput, uncertainty estimates, and operational thresholds.

For approved studies, report:

```text
FMR by group
FNMR by group
FTE by group
FTA by group
quality distributions by group
score distributions by group
latency by group
```

Differential summaries may include:

```text
max / min rate ratio
max / arithmetic mean
max / geometric mean
log-ratio dispersion
Gini coefficient
```

NIST FRTE currently uses group-specific FMR/FNMR and summary measures including ratios and Gini coefficients to characterize demographic variation.

Every demographic result shall include uncertainty estimates and group sample counts. A common fixed matcher threshold shall be supported for evaluating group differences rather than silently selecting different thresholds by group.

---

# 26. Presentation Attack Detection

PAD is not required for the initial quality/matcher beta, but QualSight shall define a future PAD evaluation module.

ISO/IEC 30107-3:2023 establishes principles and methods for PAD performance assessment and reporting.

Supported PAD metrics shall include where applicable:

### APCER
Attack Presentation Classification Error Rate.

### BPCER
Bona Fide Presentation Classification Error Rate.

### APCER/BPCER operating curves
Including results by presentation attack instrument/species where available.

### IAPAR
Impostor Attack Presentation Accept Rate for applicable system-level experiments.

NIST SP 800-63 Revision 4 references an IAPAR target for biometric PAD in federal digital-identity contexts and requires facial-recognition PAD in the covered authentication scenario. This shall be implemented only as an optional evaluation profile, not presented as universal DHS policy or QualSight certification.

PAD subsystem metrics and matcher recognition metrics shall remain distinct. The PAD module shall also support investigating interactions:

```text
Image quality → PAD performance → Recognition performance
```

since poor capture conditions can influence both recognition and attack detection.

---

# 27. Robustness and Controlled-Degradation Metrics

QualSight shall support controlled transformations as **executable, provenance-bearing components** (§51.6): compression (JPEG, JPEG2000); resolution; blur (Gaussian, motion, defocus); illumination; noise; crop; rotation; pose simulation where valid; occlusion; gamma/exposure/contrast; color transformations; custom transforms.

For parameterized degradations:

```text
JPEG quality · Blur sigma · Image resolution · Brightness · Occlusion fraction
```

report:

```text
quality vs degradation
matcher score vs degradation
FNMR vs degradation
FMR vs degradation
ERC vs degradation
component measure vs degradation
```

Example experimental chain:

```text
Original
   ↓
JPEG [100 → 10]
   ↓
OFIQ · SER-FIQ (quality response)
   ↓
ArcFace (score / error response)
```

Derived research summaries may include: relative performance loss; degradation-response slope; area under degradation-response curve; threshold crossing point; failure onset.

All transforms shall preserve parameter and provenance information.

---

# 28. Statistical Analysis

All primary metric estimates shall support uncertainty quantification.

Required methods: bootstrap confidence intervals; subject-level bootstrap; paired bootstrap; permutation testing; Pearson correlation; Spearman correlation; Kendall correlation; regression; logistic regression; mixed-effects models; effect sizes; multiple-comparison correction.

Optional: generalized additive models; survival-style analysis for degradation failure; Bayesian hierarchical analysis.

Statistical analyses shall themselves be executable components (§51.7), so a full experiment is executable end-to-end, e.g.:

```text
OFIQ components + matcher failures
   ↓
mixed-effects logistic regression
   ↓
quality × matcher interaction
   ↓
coefficient table
   ↓
publication figure
```

Metric results shall record:

```text
method
software implementation
version
parameters
confidence level
random seed
sampling unit
number of replicates
```

---

# 29. Metric Registry

All reported metrics shall be defined through a machine-readable **Metric Registry**. **The Metric Registry shall be implemented in Phase 0, before any individual metric screens are built** — otherwise FMR, FNMR, ERC, AU-ERC, EER, FPIR, FNIR, demographic ratios, calibration metrics, etc. gradually become hard-coded calculations scattered through the backend and frontend.

Each metric definition shall contain:

```text
metric_id
canonical_name
aliases
domain
  QUALITY | VERIFICATION | IDENTIFICATION | ACQUISITION | PAD
  ROBUSTNESS | CALIBRATION | DEMOGRAPHICS | SYSTEM
definition
formula/version
higher_or_lower_is_better
required_inputs
aggregation_unit
threshold_semantics
confidence_interval_methods
source_standard
source_version
references
implementation
implementation_version
limitations
warnings
```

Example:

```yaml
metric_id: biometric.verification.fnmr
canonical_name: False Non-Match Rate
domain: VERIFICATION
direction: lower
requires:
  - comparison_scores
  - mated_labels
  - threshold
standard:
  source: ISO/IEC 19795-1
  version: "2021"
```

This registry is critical because both ISO/IEC 19795-1 and ISO/IEC 29794 are currently undergoing further revision work; QualSight versions metric definitions rather than hard-coding standards semantics.

---

# 30. Evaluation Profiles

QualSight shall support named Evaluation Profiles. Examples:

```text
Generic ISO/IEC 19795 Verification
NIST-style 1:1 Verification
NIST-style 1:N Identification
ISO/IEC 29794-5 Quality
Quality Utility / ERC
Cross-Matcher Utility
PAD Evaluation
```

A profile specifies:

```text
required inputs
metrics
operating points
plots
statistical methods
minimum metadata
report template
```

Profiles provide repeatability but do not constitute certification.

---

# 31. NIST/Federal Reference Profile

QualSight may provide an optional reference configuration based on current NIST SP 800-63 biometric requirements.

For example, NIST SP 800-63B currently specifies a fixed-threshold biometric authentication requirement with FMR no worse than 1 in 10,000 and recommends FNMR below 5% for the covered authentication scenario.

The UI shall label this clearly:

> **NIST 800-63 Reference Analysis**

not "NIST Certified" or "DHS Compliant".

---

# 32. System and Computational Metrics

Every benchmark run shall optionally record:

## Quality engine
initialization time; per-image latency; throughput; assessment failures.

## Matcher
template creation time; comparison time; search time; templates/second; comparisons/second.

NIST biometric evaluations routinely report computational quantities such as template-creation and search time alongside recognition performance.

## Host resources
CPU utilization; GPU utilization; RAM; VRAM; peak memory; disk usage.

## Latency summaries
mean; median; p90; p95; p99.

These shall be categorized as engineering/system-performance metrics rather than recognition accuracy.

---

# 33. Failure Analysis Workbench

Users shall be able to formulate queries such as:

```text
OFIQ overall quality > 80
AND Matcher A = success
AND Matcher B = false non-match
```

or:

```text
Sharpness < 40
AND Pose > threshold
AND Matcher error
```

The failure browser shall display:

```text
samples
subjects
quality values
quality components
matcher scores
threshold
decision
ground truth
run
dataset
transform history
provenance
annotations
```

Selecting points, bins, lines or outliers from visualizations should filter directly into the failure browser where possible. This extends a capability already specified in the existing Studio design.

---

# 34. Annotation and Research Notes

Annotations shall be attachable to: samples; subjects; comparisons; datasets; experiments; runs; figures.

Supported operations:

```text
tag · flag · comment · classification · review status
```

Example:

```text
Matcher disagreement
High-quality false non-match
Suspected pose issue
Investigate illumination component
```

Annotations shall record author and timestamp.

---

# 35. Visualization System

Visualizations shall be first-class scientific artifacts rather than disposable UI components.

## Sample-level
original image; crop; landmarks; quality overlay; component panel; quality profile.

## Dataset-level
histograms; ECDFs; box plots; violin plots; thumbnail grids; outlier explorers.

## Matcher
mated/non-mated distributions; ROC; DET; threshold plot; operating-point table.

## Identification
CMC; rank distribution; FNIR vs FPIR.

## Quality utility
ERC; AU-ERC comparison; risk-coverage; quality-conditioned FNMR; quality-conditioned DET; error probability vs quality.

## Cross-matcher
ERC overlays; DET overlays; error overlap; quality-performance matrix; matcher × quality interaction.

## Statistics
correlation matrices; scatter plots; confidence intervals; regression diagnostics; calibration plots.

## Robustness
degradation-response curves; degradation heatmaps; threshold-crossing visualization.

---

# 36. Visualization Provenance

Every visualization shall expose:

```text
View source data
View run
View protocol
View metric definition
View thresholds
View statistics
View provenance
```

Every visualization menu shall support where applicable:

```text
Save · Duplicate · Add to dashboard · Add to report
Export PNG · Export SVG · Export PDF · Export CSV
```

Guiding requirement:

> **No scientific chart without data lineage.**

---

# 37. Dashboards

Users shall be able to construct and save dashboards.

```text
┌────────────────────────┬────────────────────────┐
│ Dataset quality        │ Quality distributions  │
├────────────────────────┼────────────────────────┤
│ ERC                    │ DET                    │
├────────────────────────┼────────────────────────┤
│ Cross-matcher results  │ Failure examples       │
├────────────────────────┼────────────────────────┤
│ Operating-point table  │ Component correlations │
└────────────────────────┴────────────────────────┘
```

Dashboards shall be saved as project artifacts. Templates may include:

```text
Dataset Overview
Matcher Evaluation
FIQA Evaluation
Cross-Matcher Evaluation
Stakeholder Demo
Publication Workspace
```

---

# 38. Reports and Print

QualSight shall include a Report Builder. Reports may include:

```text
title
executive summary
dataset
protocol
engine
matcher
operating points
metrics
figures
tables
failure examples
statistical methods
confidence intervals
limitations
provenance
software versions
model hashes
dataset hashes
```

Supported output: PDF; HTML; Print.

Printing shall operate primarily through the report system rather than maintaining an independent print architecture.

---

# 39. Data Export

Supported tabular/data formats: CSV; JSON; Parquet.

Export categories:

```text
quality results
matcher scores
embeddings
comparison records
identification results
metric tables
statistical results
annotations
```

Exports shall contain sufficient identifiers to trace data to the source run.

---

# 40. Figure Export

Supported formats: PNG; SVG; PDF.

SVG shall be treated as a first-class requirement for publication-quality scientific figures. Figures shall optionally include a machine-readable sidecar:

```text
figure.svg
figure.provenance.json
```

---

# 41. Reproducibility Bundle

QualSight shall export a portable experiment package such as:

```text
experiment.qsexp
```

containing:

```text
manifest.json
experiment.yaml
protocol.yaml
dataset-manifest.json
environment.json
metrics.json
results.parquet
figures/
tables/
checksums.sha256
README.md
```

Restricted biometric image data shall not be automatically included.

---

# 42. Reproduction Mode

Given a compatible experiment bundle:

```text
Import → Validate dependencies → Validate input hashes → Validate model versions → Execute → Compare outputs
```

Possible reproduction states:

```text
EXACT
WITHIN_TOLERANCE
DIFFERENT
MISSING_DEPENDENCY
MISSING_DATA
BLOCKED
```

Target results shall never be modified to match reproduction output.

---

# 43. Reportable Metric Catalog

QualSight shall ultimately support the following metric families.

| Family          | Core metrics                                      |
| --------------- | ------------------------------------------------- |
| Verification    | FMR, FNMR, TMR/TAR, EER, ROC, DET, AUC, pAUC      |
| Identification  | FPIR, FNIR, TPIR, Rank-k, CMC                     |
| Acquisition     | FTA, FTE, Failure-to-Template, Failure-to-Compare |
| Quality         | Overall quality, components, assessment coverage  |
| FIQA utility    | ERC, AU-ERC, pAU-ERC, risk-coverage               |
| Prediction      | AUROC, AUPRC, sensitivity, specificity            |
| Calibration     | Brier, NLL, ECE when probabilistically valid      |
| Cross-matcher   | ERC difference, error overlap, interactions       |
| Robustness      | degradation-response, relative degradation        |
| Demographics    | per-group errors, ratios, dispersion, Gini        |
| PAD             | APCER, BPCER, IAPAR where applicable              |
| Statistics      | CI, effect sizes, hypothesis tests                |
| Runtime         | latency, throughput, CPU/GPU/RAM/VRAM             |
| Reproducibility | hashes, environment, reproduction status          |

No single dashboard should display all metrics simultaneously. Metrics shall be selected by experimental question.

---

# 44. Application Navigation

Recommended primary navigation:

```text
QUALSIGHT
Projects
DATA
  Datasets
  Samples
  Subjects
  Imports
QUALITY
  Sample Analysis
  Quality Engines
  Quality Explorer
BIOMETRICS
  Matchers
  Verification
  Identification
  Score Analysis
EXPERIMENTS
  Protocols
  Experiments
  Runs
  Compare Runs
ANALYSIS
  Quality Utility
  Statistics
  Cross-Matcher
  Robustness
  Failure Analysis
EXPLORE
  Visualizations
  Dashboards
OUTPUT
  Reports
  Exports
  Reproduction
SYSTEM
  Engines
  Resources
  Users
  Settings
```

---

# 45. Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│                     QualSight PWA                           │
│                React + TypeScript                           │
│                                                             │
│ Projects │ Data │ Quality │ Matchers │ Experiments          │
│ Analysis │ Visualization │ Reports │ Provenance             │
└──────────────────────────────┬──────────────────────────────┘
                               │
                       REST + WebSocket
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                    FastAPI Control Plane                    │
│                                                             │
│ API │ Projects │ Jobs │ Runs │ Protocols │ Artifacts        │
│ Metric Registry │ Provenance │ Validation                   │
└────────┬────────────┬─────────────┬─────────────┬───────────┘
         │            │             │             │
         ▼            ▼             ▼             ▼
    Quality       Matcher       Evaluation     Statistics
    Workers       Workers        Engine         Engine
         │            │             │             │
         └────────────┴─────────────┴─────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                      Data Layer                             │
│                                                             │
│ SQLite  → project/run/configuration metadata                │
│ Parquet → quality, scores, comparisons, metric results      │
│ DuckDB  → interactive analytical queries                    │
│ Disk    → images, models, figures, reports, artifacts       │
│ SHA-256 → content identity/provenance                       │
└─────────────────────────────────────────────────────────────┘
```

The Experiment Runtime executes heterogeneous engine categories through the adapter architecture (§51):

```text
                         QualSight
                             │
                    Experiment Runtime
                             │
       ┌─────────────────────┼─────────────────────┐
       │                     │                     │
       ▼                     ▼                     ▼
  BIOMETRIC             SCIENTIFIC             EXTERNAL
  ENGINES                ENGINES                ENGINES
       │                     │                     │
  Quality                 Statistics             CLI
  Matcher                 ML models              REST
  Detection               Transforms             Docker
  Alignment               Calibration            Score files
  PAD                     Scripts                Existing results
  Identification          Notebooks
```

---

# 46. PWA Requirement

The PWA is the **QualSight console**, not the scientific runtime. Its responsibilities are: configuration; visualization; project management; experiment definition; execution control; progress monitoring; inspection; reporting.

Scientific processing shall occur in backend worker processes.

A future lightweight PWA/client may expose only selected functions such as single-image quality assessment, but this is not part of the beta.

---

# 47. Storage

## SQLite
Use for: projects; users; engines; configurations; protocols; runs; annotations; artifact references; audit log.

## Parquet
Use for: per-image quality measurements; embeddings; comparison scores; pair tables; candidate lists; statistical output.

## DuckDB
Use for interactive analytics over Parquet where beneficial.

## Filesystem
Use for: biometric samples; models; generated figures; reports; experiment bundles.

---

# 48. Security and Data Handling

The beta shall operate locally on a Clarkson-controlled workstation.

Default requirements:

* no cloud calls at runtime;
* no telemetry containing biometric data;
* no automatic upload;
* no external image transmission;
* no credentials in logs;
* no restricted samples in public fixtures;
* no restricted images in exported reports without explicit authorization.

Embeddings/templates shall be treated as sensitive biometric-derived data.

Projects and datasets shall support:

```text
PUBLIC · SYNTHETIC · GENERATED · PRIVATE · RESTRICTED
```

Export shall inspect project classification before including source imagery.

---

# 49. Purge

Dataset purge shall remove application-managed: samples; cached derivatives; thumbnails; quality results; embeddings; comparison records; reports containing samples; temporary files.

The application shall describe this as:

> application-level verified purge

rather than claiming guaranteed physical erasure from storage media.

---

# 50. Audit History

Audit history shall record meaningful user/scientific events including:

```text
project created
dataset imported
configuration changed
experiment created
run started
run completed
artifact exported
annotation modified
dataset purged
```

The audit system is local and shall not function as telemetry.

---

# 51. Execution-Adapter Architecture

Scientific integrations shall use typed adapters. **The key abstraction is not `QualityEngine`; it is `ExecutionAdapter`** (with specialized interfaces underneath). QualSight shall be able to run essentially any biometric research component that can expose a defined input/output contract.

## 51.1 Core plugin model

```text
ExecutionAdapter
│
├── BiometricAdapter
│   │
│   ├── QualityEngine
│   ├── Matcher
│   ├── Detector
│   ├── Landmarker
│   ├── Aligner
│   ├── TemplateExtractor
│   └── PADSystem
│
├── TransformAdapter
│
├── ModelAdapter
│   ├── ONNX
│   ├── PyTorch
│   └── TorchScript
│
├── StatisticalAdapter
│
├── ExternalAdapter
│   ├── CLI
│   ├── Python
│   ├── Container
│   └── REST
│
└── ImportAdapter
    ├── ScoreFile
    ├── EmbeddingFile
    ├── CandidateList
    └── ExternalResults
```

Every adapter declares:

```text
identity
version
modality
capabilities
license
source
model/checkpoint
input schema
output schema
preprocessing
configuration
runtime
device requirements
deterministic/stochastic
seed support
metrics exposed
artifacts
provenance
```

## 51.2 Detection, alignment and preprocessing as first-class stages

Preprocessing can materially alter downstream results. The experimental graph shall represent detector, landmark detector, aligner, cropper, and template generator independently rather than hiding them inside a matcher wrapper, so a researcher can test:

```text
Detector A → Alignment A → OFIQ → ArcFace
     vs
Detector B → Alignment B → OFIQ → ArcFace
```

and discover whether an apparent quality or matcher effect is actually a preprocessing effect. (InsightFace combines detection, alignment and recognition, illustrating why these stages must be independently representable.)

## 51.3 Generic ONNX model runner (core capability)

Instead of a custom integration for every model, the user supplies:

```text
Model · Input shape · Input datatype · Preprocessing · Output interpretation · Device (CPU / CUDA)
```

and maps output, e.g.:

```text
output[0] → quality score
output[0] → embedding
output[0] → PAD probability
```

New research models become dramatically easier to integrate.

## 51.4 PyTorch model runner

Supports PyTorch / TorchScript / checkpoint + registered architecture. The adapter records:

```text
architecture · checkpoint · checkpoint hash · precision · device · input transform · output schema
```

This lets a new FIQA model be tested without modifying the QualSight core.

## 51.5 Python function/module and CLI executable adapters

**Python module** (retained from the Studio concept):

```text
module: research.fiqa.evaluate
function: run
environment: ./venv
```

Inputs and outputs are typed; existing research code does not have to be rewritten merely to appear in QualSight. Environment, input/output hashes, exit status and provenance are captured.

**CLI adapter** — anything with `program input.jpg --output result.json` can become a QualSight engine:

```yaml
type: cli
command: ./program
inputs:
  image:
    argument: "--image"
outputs:
  result:
    format: json
```

A C++ biometric implementation therefore doesn't need Python bindings at all — for some research tools this is a better fallback than custom pybind bindings.

## 51.6 Transform (controlled degradation) adapters

Degradations are executable components with parameter sweeps and provenance (§27): JPEG, JPEG2000, blur, motion blur, defocus, noise, resolution, gamma, exposure, contrast, crop, rotation, occlusion, custom.

## 51.7 Statistical/model execution adapters

Analysis models are executable components: Pearson, Spearman, Kendall, logistic/linear regression, mixed-effects models, bootstrap, permutation, PCA, clustering, calibration, custom statistical models — so a full experiment is executable, not just the biometric inference.

## 51.8 Import adapters and execution provenance

Sometimes the matcher cannot be run (commercial matcher, NIST result, partner laboratory, historical experiment). QualSight shall support importing scores, candidate lists, quality results, embeddings, and metrics — permanently marked:

```text
EXTERNALLY COMPUTED   (never conflated with QUALSIGHT EXECUTED)
```

## 51.9 Container adapter (post-beta)

A model supplies a Docker/OCI container implementing a QualSight contract (`stdin/files → container → JSON/Parquet/artifacts`), giving immutable execution environments (e.g., a CR-FIQA container pinning PyTorch 1.7 + CUDA + checkpoint). Shall not block the beta, but the adapter architecture shall accommodate it. A REST adapter follows the same pattern.

## 51.10 User-created plugins

Eventually a researcher shall be able to register an engine entirely through the UI:

```text
Settings → Engines → Add Engine
  Engine type: Quality
  Name: MyFIQA
  Runtime: ONNX
  Model: myfiqa.onnx
  Input: Aligned Face RGB 112x112
  Output: float32[1]
  Output interpretation: Quality score
  Higher is better: Yes
```

QualSight generates/registers the adapter — a PhD student can add a model without changing application code.

## 51.11 Modality extensibility

`modality` is part of every relevant schema now (§10). The long-term architecture:

```text
BiometricModality
FACE        ├ QualityEngine ├ Matcher ├ PAD
FINGERPRINT ├ QualityEngine (e.g., NIST NFIQ 2) ├ Matcher ├ PAD
IRIS        ├ QualityEngine ├ Matcher ├ PAD
VOICE / PALM / MULTIMODAL
```

Multimodal support is not implemented in the beta, but nothing in the schema assumes face-only forever.

---

# 52. Resource and License Registry

Each external model/resource shall have:

```text
resource
version
source
license
use restrictions
redistribution status
research-use status
hash
citation
installation method
```

Open-source QualSight distribution shall not imply that every external model or dataset may be redistributed.

---

# 53. Initial Engine Integration Targets

Five engines are the initial integration target:

1. **OFIQ** — standards-oriented quality baseline (first validated adapter; see PLAN.md for python-ofiq/process-pool mechanics and CLI-adapter fallback).
2. **US-FIQA / OpenFIQA** — Aaron's own research lineage.
3. **CR-FIQA or SER-FIQ** — independent FIQA comparator (CR-FIQA has an official implementation explicitly intended for recognition-utility quality evaluation).
4. **AdaFace** — quality-aware face matcher.
5. **ArcFace / InsightFace adapter** — conventional matcher baseline and face-processing stack (detection + alignment + recognition).

The engine adapter shall preserve differences among quality implementations rather than collapsing them into one generic score. Target quality registry over time: §13.

---

# 54. Matcher Strategy

QualSight shall actually **run** matchers, not merely import their scores. Initial matcher registry:

```text
Face Recognition
│
├── ArcFace
├── AdaFace
├── MagFace          (matcher + quality signal simultaneously)
├── custom ONNX matcher
├── custom PyTorch matcher
├── external matcher
└── imported matcher scores
```

MagFace is particularly useful for experiments investigating whether matcher-derived quality generalizes to other recognition systems.

Delivery tiers:

## Tier 1
Score-file importer — immediate compatibility with externally generated matcher experiments.

## Tier 2
Approved locally executable open/research matcher (ArcFace/AdaFace via the ModelAdapter runtime).

## Tier 3
Additional research matchers (MagFace, custom ONNX/PyTorch).

## Tier 4
Commercial matcher SDK adapters when licensing becomes available.

No beta capability shall depend on a commercial SDK.

---

# 55. Beta Functional Scope

The beta shall include:

### Projects
load/save; autosave; project manifests.

### Users
single-user mode; optional local account.

### Data
dataset import; manifests; identity mappings; pair definitions.

### Quality
OFIQ adapter; single-image inspection; batch analysis; distributions.

### Matchers
score import; at least one approved local matcher where practical.

### Verification
FMR/FNMR; ROC; DET; EER; operating points.

### FIQA
ERC; AU-ERC; quality-conditioned recognition; quality-error modeling.

### Cross-matcher
comparison overlays; failure overlap; utility differences.

### Statistics
confidence intervals; bootstrap; correlations; regression.

### Visualization
interactive plots; failure drilldown; dashboards.

### Reporting
PDF; HTML; print; CSV; Parquet; JSON; SVG/PNG/PDF figures.

### Provenance
immutable Runs; hashes; versions; configuration; reproduction bundle.

---

# 56. Phase Plan

## Phase 0 — Canonicalization and De-Risking

* QualSight naming/product boundary;
* reconcile existing OpenFIQA Studio assets;
* **define the ExecutionAdapter contract (§51) and implement the Metric Registry (§29) before any metric screens**;
* verify OFIQ adapters (python-ofiq library path; CLI adapter as fallback);
* benchmark latency;
* confirm available measurements;
* define canonical schemas (including `modality` and 1:N-capable comparison/search schemas);
* establish approved test fixtures.

**Exit:** one reproducible OFIQ evaluation represented using canonical QualSight schemas.

## Phase 1 — Projects + Quality Workbench

PWA shell; FastAPI; project lifecycle; user attribution; dataset import; single-image analysis; batch OFIQ; quality visualization; provenance.

**Exit:** trustworthy quality-analysis workstation.

## Phase 2 — Matcher Evaluation

Score importer; comparison schema; mated/non-mated protocols; local matcher adapter; FMR/FNMR; ROC; DET; EER; operating-point analysis.

**Exit:** complete 1:1 biometric evaluation.

## Phase 3 — FIQA Utility

ERC; AU-ERC; risk-coverage; quality bins; error prediction; component analysis; threshold-policy analysis.

**Exit:** quality-versus-performance analysis.

## Phase 4 — Cross-Matcher T&E

Matcher overlays; cross-matcher utility; failure overlap; statistical interaction; protocol comparison; run comparison.

**Exit:** DFAB/DHS centerpiece.

## Phase 5 — Visualization and Reporting

Dashboards; report builder; print; publication figures; export system; reproduction bundles.

**Exit:** stakeholder-ready scientific reporting.

## Phase 6 — Advanced Analysis

1:N identification; controlled degradation; advanced statistics; demographic module; PAD adapters where appropriate.

## Phase 7 — Hardening

Documentation; threat model; local auth; resource/license registry; installation packaging; offline deployment; demonstration script; beta release.

---

# 57. Beta Acceptance Criteria

The beta succeeds when a researcher can:

```text
Create project
   ↓
Import identity-labeled dataset
   ↓
Run OFIQ
   ↓
Inspect quality/components
   ↓
Import/run matcher
   ↓
Generate genuine + impostor comparisons
   ↓
Compute FMR/FNMR + DET
   ↓
Compute ERC
   ↓
Compare two matchers
   ↓
Inspect disagreement/failure samples
   ↓
Save dashboard
   ↓
Generate report
   ↓
Export experiment bundle
   ↓
Trace every result to its exact Run
```

No manual Python analysis shall be required for this canonical workflow.

---

# 58. Scientific Acceptance Criteria

A result shall not be considered scientifically reportable unless QualSight can identify:

* input dataset;
* subject population;
* sample count;
* comparison count;
* pairing/search protocol;
* engine;
* matcher;
* preprocessing;
* thresholds;
* metric definition;
* uncertainty method;
* software version;
* model version/hash;
* run ID.

---

# 59. Product Success Condition

QualSight succeeds when it allows a biometric researcher to move fluidly between:

```text
sample → quality → matcher → error metric → aggregate visualization
→ statistical comparison → individual failure → experiment provenance
→ publication/stakeholder artifact
```

without losing the lineage connecting those levels.

The defining characteristic of QualSight is therefore not the presence of an OFIQ score gauge. It is the ability to answer:

> **How does biometric sample quality affect recognition performance, under which matcher, dataset, operating point, population, and experimental protocol — and can another researcher reproduce the result?**

---

# 60. Governing Design Principle

Every number in QualSight shall have:

```text
a definition
a population
a protocol
an operating point
an uncertainty estimate where applicable
and a provenance chain
```

Every plot shall have:

```text
source data
source run
metric definition
configuration
exportable underlying values
```

Every scientific comparison shall preserve:

```text
what changed
what remained fixed
```

That is the threshold that separates QualSight from a biometric dashboard and makes it a scientific biometric evaluation workstation.

---

# References

### Standards
* ISO/IEC 19795-1:2021 — Biometric performance testing and reporting — Principles and framework. https://www.iso.org/standard/73515.html
* ISO/IEC 19795-2:2007 — Technology and scenario evaluation methodology. https://www.iso.org/standard/41448.html
* ISO/IEC 19795-10:2024 — Performance variation across demographic groups. https://www.iso.org/standard/81223.html
* ISO/IEC 29794-1:2024 — Biometric sample quality framework. https://www.iso.org/standard/79519.html
* ISO/IEC 29794-5:2025 — Face image quality. https://www.iso.org/standard/81005.html
* ISO/IEC 30107-3:2023 — Presentation attack detection testing and reporting. https://www.iso.org/standard/79520.html
* ISO/IEC 19795-1 Ed.3 revision (under development). https://committee.iso.org/standard/93355.html

### NIST
* NIST FRTE 1:1 Verification. https://pages.nist.gov/frvt/html/frvt11.html
* NIST FRTE 1:N Identification. https://pages.nist.gov/frvt/html/frvt1N.html
* NIST FRTE Demographic Effects. https://pages.nist.gov/frvt/html/frvt_demographics.html
* NIST FATE PAD. https://pages.nist.gov/frvt/html/frvt_pad.html
* NIST SP 800-63 Revision 4. https://pages.nist.gov/800-63-4/ · SP 800-63B: https://pages.nist.gov/800-63-4/sp800-63b.html
* NIST IREX 10 (computational-metric reporting example). https://pages.nist.gov/IREX10/idbio_001/2026_03_06/index.html

### Engines and implementations
* OFIQ (BSI): https://github.com/BSI-OFIQ/OFIQ-Project · python-ofiq: https://github.com/unicef/python-ofiq
* SER-FIQ: https://github.com/pterhoer/FaceImageQuality
* CR-FIQA: https://github.com/fdbtrs/cr-fiqa
* MagFace: https://github.com/IrvingMeng/MagFace
* FaceQAN: https://github.com/LSIbabnikz/FaceQAN
* AdaFace: https://github.com/mk-minchul/adaface
* InsightFace (ArcFace, detection, alignment): https://github.com/deepinsight/insightface
* NFIQ 2 (fingerprint quality, future modality): https://github.com/usnistgov/nfiq2

---

# Changelog

* **v0.3 (2026-08-13):** Replacement canonical PRD. Narrowed product around biometric T&E and scientific analysis; added metric families (1:1, 1:N, acquisition, uncertainty, demographics, PAD, robustness, computational); Metric Registry (§29) prioritized to Phase 0; 1:N schemas designed now; ExecutionAdapter architecture (§51) with OFIQ as first validated adapter, not the core execution model; five initial engine targets (§53); `modality` field mandatory. Beta scope drops live capture, kiosk/tablet, and mobile apps (now explicit non-goals, §5) relative to PLAN v0.2.
* **v0.2 (2026-08-13):** PLAN.md — QualSight naming, dataset research, GPU, sensitive-data mode.
* **v0.1 (2026-08-13):** PLAN.md — initial plan.




