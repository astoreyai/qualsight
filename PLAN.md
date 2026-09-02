# QualSight — Engineering & Research Companion

*(repo: ofiqgui)*

> **SUPERSEDED for product scope by `PRD.md` v0.3 (Aug 13, 2026), the canonical PRD.** QualSight is now a biometric T&E workstation with an ExecutionAdapter architecture; OFIQ is the first validated adapter, not the core execution model. Notable scope changes vs this document: live webcam capture, tablet/kiosk mode, and mobile apps are explicit beta **non-goals** (PRD §5); deployment target is a Clarkson-controlled workstation. This file is retained for the still-valid research and integration mechanics below: OFIQ library-vs-CLI analysis and python-ofiq process-pool design (§4.2), dataset research (§8), environment facts, and timeline heuristics.

**Project plan v0.2 — August 13, 2026**
**Author:** Aaron Storey · **Stakeholders:** DFAB, DHS

---

## 1. Vision

A web-based biometric application that makes Aaron's OFIQ / ISO/IEC 29794-5 work visible, interactive, and decision-relevant for DFAB and DHS stakeholders. The beta lets a user score face images against the full set of ISO/IEC 29794-5 quality measures, process whole datasets, compare images and matchers side by side, run the statistical analyses that connect image quality to matcher performance (ERC, DET, correlations, threshold policy), and demonstrate live capture with real-time quality feedback.

It begins as a research/lab tool driven by Aaron, with a deliberate path toward stakeholder distribution, tablet/kiosk capture-station demos, and eventually a native mobile app.

## 2. Background

- **OFIQ** (Open Source Face Image Quality) is the reference implementation of **ISO/IEC 29794-5**, developed under BSI. Current release: **v1.1.2 (Feb 2026)**, C++ library + sample CLI, with mobile build support. eu-LISA has announced plans to integrate OFIQ into EU large-scale systems — useful context for the DHS audience.
- OFIQ produces a **unified quality score (0–100)** plus **25+ component measures** (e.g., sharpness, illumination uniformity, pose, eyes open, mouth closed, occlusion, background uniformity, dynamic range, inter-eye distance).
- **python-ofiq** (pybind11 bindings, PyPI, v0.3.1 May 2026) wraps OFIQlib for Linux x86_64 and macOS arm64 — this is our primary integration path.

## 3. Key decisions (settled in planning discussion)

| Decision | Choice | Rationale |
|---|---|---|
| Platform | Local web app (FastAPI backend + responsive PWA frontend) | Works on desktop now and phone/tablet browsers immediately; API-first design is the clean path to a future native app |
| OFIQ integration | Wrap the library (via python-ofiq), **not** the CLI | CLI reloads ~400 MB of models per invocation (seconds of startup); a persistent process loads once and scores in sub-second time — required for live capture and interactive use |
| Scope (beta) | Single-image analysis, batch + reports, side-by-side comparison, live capture, matcher comparison + statistical analysis | All selected |
| Matchers | Open-source embeddings **and** imported score files; commercial SDKs deferred (no licenses currently held) | Maximum analytic flexibility; score-file import covers matchers we can't run locally |
| App name | **QualSight** | Presents better than "ofiqgui"; repo name stays ofiqgui |
| Statistics | ERC curves, score distributions + DET, measure correlations, threshold/policy analysis | The full quality-predicts-performance story |
| Mobile path | PWA first → tablet kiosk mode → native app (planned, post-beta) | Kiosk demo needs are met by the PWA; native app consumes the same API |
| Dev environment | Linux x86_64 workstation **with GPU** | Matches python-ofiq wheel support; GPU (CUDA) accelerates matcher embeddings via ONNX Runtime and potentially OFIQ's ONNX inference |

## 4. Architecture

### 4.1 Overview

```
┌────────────────────────────────────────────────────────────┐
│  Frontend — React + TypeScript PWA (responsive, kiosk mode) │
│  desktop browser · phone browser · tablet kiosk             │
└──────────────┬─────────────────────────────────────────────┘
               │ REST (OpenAPI) + WebSocket (live capture, job progress)
┌──────────────┴─────────────────────────────────────────────┐
│  Backend — FastAPI (Python 3.11+)                           │
│  ┌──────────────┐ ┌───────────────┐ ┌────────────────────┐  │
│  │ OFIQ engine   │ │ Matcher layer │ │ Stats module       │  │
│  │ process pool  │ │ (plugin API)  │ │ ERC/DET/corr/policy│  │
│  │ (python-ofiq) │ │ ArcFace, ...  │ │ numpy/scipy/pandas │  │
│  └──────────────┘ └───────────────┘ └────────────────────┘  │
│  ┌──────────────┐ ┌───────────────┐ ┌────────────────────┐  │
│  │ Job queue     │ │ Score-file    │ │ Report generator   │  │
│  │ (batch runs)  │ │ importer      │ │ (CSV / PDF / JSON) │  │
│  └──────────────┘ └───────────────┘ └────────────────────┘  │
└──────────────┬─────────────────────────────────────────────┘
┌──────────────┴─────────────────────────────────────────────┐
│  Storage — SQLite (metadata, scores, embeddings, jobs)      │
│  + content-addressed image store on disk (all local, no    │
│  cloud — face images never leave the machine)              │
└────────────────────────────────────────────────────────────┘
```

### 4.2 OFIQ engine service

- A **process pool** of python-ofiq workers (bindings are not thread-safe; separate processes sidestep that and give parallel batch throughput). Models load once per worker at startup.
- Returns the unified score, all component measures (scalar + raw values), and landmark/face-detection artifacts for visual overlays.
- Fallback plan if python-ofiq proves immature (§9): write our own thin pybind11 binding against OFIQlib v1.1.2, or run OFIQ behind a small C++ gRPC sidecar. The engine sits behind an internal interface so the rest of the app doesn't care which.

### 4.3 Matcher abstraction layer

A plugin interface so every matcher looks the same to the UI and stats module:

```python
class Matcher(Protocol):
    name: str
    def embed(self, image: np.ndarray) -> np.ndarray | None   # optional
    def compare(self, a: ImageRef, b: ImageRef) -> float       # similarity score
```

- **Tier 1 (beta):** open-source embedding models via ONNX Runtime — ArcFace (InsightFace buffalo_l), AdaFace; cosine similarity scoring. Free, redistributable, runs on CPU.
- **Tier 2 (beta):** **score-file importer** — ingest externally produced similarity scores (CSV / NIST-style score matrices, mated + non-mated lists) keyed to image IDs, so analyses can include matchers we can't run locally (including commercial results from other labs/NIST evaluations).
- **Tier 3 (deferred):** direct commercial SDK adapters behind the same interface. **No commercial licenses are currently held**, so this tier is post-beta; the score-file importer is the bridge to commercial-matcher analyses in the meantime.

### 4.4 Statistics module

All computations in Python (numpy/scipy/pandas), rendered as interactive charts (Plotly) in the frontend:

- **ERC (Error-vs-Reject curves)** — the ISO/IEC 29794-5 evaluation methodology: FNMR as a function of the fraction of lowest-quality mated pairs rejected, per quality measure and per matcher. Includes pAUC summary values. This is the headline "quality predicts performance" chart for DFAB/DHS.
- **Score distributions & DET** — mated/non-mated similarity histograms per matcher; DET/ROC curves; quality-binned DETs (e.g., DET for images with unified score <40 vs ≥70).
- **Measure correlations** — Spearman/Pearson correlation matrix across quality measures; measure-vs-measure scatter; optional PCA to show measure redundancy.
- **Threshold / policy analysis** — sweep a quality threshold and show acceptance rate vs downstream FNMR/FMR impact; per-dataset slices; "what would an enrollment policy of unified-score ≥ X actually reject?" with example rejected images.

### 4.5 Data model (SQLite)

`datasets` (named collections) → `images` (path, sha256, EXIF, dataset refs) → `quality_results` (per image × OFIQ version: unified + all measures, raw values, landmarks JSON) · `embeddings` (per image × matcher) · `comparisons` (pair, matcher, score, mated flag) · `score_imports` · `jobs` (batch runs, status, errors). Everything exportable to CSV/Parquet.

### 4.6 Live capture

Browser `getUserMedia` → downsampled frames over WebSocket → OFIQ engine (single dedicated worker, ~2–4 fps target on CPU) → per-frame unified score + top failing measures rendered as live overlay with **actionable guidance** ("face too small", "look at camera", "uneven lighting"). "Capture" freezes the best recent frame and runs full analysis. The same screen in fullscreen kiosk mode is the tablet capture-station demo.

### 4.7 API-first for the future native app

- OpenAPI schema is the contract; a generated TypeScript client keeps the PWA honest, and the same schema generates the future native app's client (React Native or Flutter — decide post-beta).
- Native-app on-device scoring (OFIQ mobile build) is explicitly out of beta scope but the OFIQ v1.1.x mobile build instructions make it a credible later phase.

## 5. Tech stack summary

| Layer | Choice |
|---|---|
| Backend | Python 3.11+, FastAPI, uvicorn, pydantic |
| OFIQ | python-ofiq (pybind11 wheel) over OFIQ v1.1.2; process-pool workers |
| Matchers | ONNX Runtime **with CUDA execution provider** (ArcFace/AdaFace); score-file importer; SDK adapters later |
| Stats | numpy, scipy, pandas, scikit-learn (PCA) |
| Storage | SQLite (via SQLAlchemy), content-addressed image store on disk |
| Frontend | React + TypeScript, Vite, Plotly.js (charts), PWA (installable, kiosk-capable) |
| Reports | CSV/Parquet export; PDF via headless render of report views |
| Packaging | Docker Compose (one-command bring-up); plain `pip + npm` dev mode |

## 6. Phased delivery plan

Each phase ends with something demonstrable. Rough effort assumes part-time work alongside other duties; compress if this becomes a primary task.

### Phase 0 — Foundations & de-risking spike (~1–2 weeks)
Repo scaffold (monorepo: `backend/`, `frontend/`), CI lint/test, Docker dev environment. **The critical spike:** install python-ofiq on the Linux workstation, score a real image end-to-end, and verify (a) all 25+ measures come through, (b) landmarks are accessible for overlays, (c) per-image latency after warm-up. *This spike validates or kills the primary integration path before anything is built on it.* Exit criteria: `curl` an endpoint with an image, get full ISO/IEC 29794-5 results back in <1 s warm.

### Phase 1 — Single-image analysis (~2 weeks)
Upload/drag-drop an image → face detection box + landmark overlay → unified score gauge → all component measures in a grouped, sortable panel (capture-related vs subject-related), each with plain-language descriptions and pass/warn/fail coloring against configurable thresholds. Export result as JSON/CSV. **First demo-able build.**

### Phase 2 — Datasets, batch & reports (~2–3 weeks)
Create datasets; ingest folders; background job queue with progress (WebSocket); results table with sort/filter/search by any measure; per-measure distribution histograms; thumbnail grid sorted by any score; flag/annotate images; CSV/Parquet export and a formatted PDF summary report.

### Phase 3 — Comparison & matchers (~2–3 weeks)
Side-by-side N-image comparison (radar/parallel-coordinates of measures + delta table). Matcher layer with ArcFace + AdaFace; pairwise verification scores in the comparison view; embedding computation as a batch job; score-file importer with mated/non-mated labeling.

### Phase 4 — Statistical analysis suite (~3 weeks)
ERC curves (per measure × matcher, pAUC table); mated/non-mated distributions and DET curves per matcher; quality-binned DETs; correlation matrix + scatter matrix; threshold/policy dashboard with example-image evidence. All charts interactive and exportable (PNG/SVG/CSV-behind-the-chart). **This is the DFAB/DHS centerpiece.**

### Phase 5 — Live capture & kiosk (~2 weeks)
Webcam capture screen with real-time unified score and actionable guidance; best-frame capture into a dataset; fullscreen kiosk mode with big-type UI for tablet capture-station demos; PWA install manifest so it launches app-like on a tablet.

### Phase 6 — Beta hardening & distribution (~2 weeks)
Auth (simple local accounts / access token) for when it's hosted beyond Aaron's machine; settings UI (OFIQ config, thresholds, worker count); error handling and empty states; user guide + demo script tailored to a DFAB/DHS walkthrough; versioned Docker image; tag **v0.1-beta**.

**Post-beta (not scheduled):** commercial SDK adapters (once licenses are confirmed), native mobile app (React Native/Flutter against the same API), on-device OFIQ mobile build, multi-user server deployment for stakeholders, demographic-differential analysis module (needs careful design — see risks).

## 7. Milestones

| Milestone | Target (calendar, part-time) |
|---|---|
| M0: Spike passes — OFIQ scoring end-to-end | ~late Aug 2026 |
| M1: Single-image demo | ~mid Sep 2026 |
| M2: Batch + reports | ~early Oct 2026 |
| M3: Matchers + comparison | ~late Oct 2026 |
| M4: Full stats suite (ERC/DET/policy) | ~mid Nov 2026 |
| M5: Live capture + kiosk | ~early Dec 2026 |
| M6: v0.1-beta, docs + demo script | ~mid Dec 2026 |

## 8. Datasets (researched Aug 2026)

QualSight needs identity-labeled data (mated pairs) for ERC and DET. No commercial data is currently held, so the beta runs on open datasets, tiered by suitability. Note the beta is a research tool, so research-only licenses are acceptable; check terms again before any redistribution.

### Tier A — primary demo data (cleanest provenance, best for DHS audiences)

- **NIST MEDS-II (Special Database 32)** — U.S. government mugshot dataset, ~1,300 subjects with **multiple encounters per subject** (natural mated pairs), freely downloadable from NIST, public U.S. government work. Government provenance and unconstrained-but-realistic quality variation make it the default demo dataset for a DHS audience. ([NIST SD32](https://www.nist.gov/itl/iad/btg/special-database-32-multiple-encounter-dataset-meds))
- **Real-data-only boundary** — candidate datasets must contain real observations, documented provenance,
  and rights sufficient for the intended local research use. Presentation convenience and throughput
  testing do not justify substituting generated identities or images for the real biometric pipeline.

### Tier B — standard academic benchmarks (research licenses; what the FIQA literature uses)

- **LFW** (13k images / 5.7k identities) and its harder derivatives **CALFW** (age variation), **CPLFW** (pose), and **XQLFW** (cross-quality — built specifically for quality analysis, directly on-mission for QualSight).
- **AgeDB** (16.5k / 568) and **CFP-FP** (7k / 500, frontal-profile) — the standard ERC evaluation sets in FIQA papers (CR-FIQA, FaceQAN, etc.), so QualSight results are directly comparable to published curves.
- **IJB-B / IJB-C** — richer, template-based, wide quality variation; free but requires a signed NIST agreement. Worth requesting early; treat as a Phase-4 upgrade rather than a dependency.

### Tier C — avoid

**MS-Celeb-1M and MegaFace are withdrawn**; do not use even if mirrors exist. Large web-scraped sets (VGGFace2, CASIA-WebFace, Glint360K) are research-only with murky consent provenance — poor choices for anything shown to DHS; not needed for our purposes anyway.

**Action items:** download MEDS-II + LFW/XQLFW + AgeDB + CFP-FP in Phase 2; file the IJB-C request with NIST in parallel; build the dataset ingester to record each dataset's license/provenance as metadata so every report can state its data source.

## 9. Security & data handling

Face images are biometric PII. Beta posture: **everything local** — no cloud calls at runtime (models fetched once at install), images and scores stay on the workstation, SQLite + image store under one directory that can be encrypted/wiped, and a "purge dataset" function that provably deletes images and derived data (embeddings are biometric data too). No telemetry.

**QualSight will eventually run on a non-open (controlled) dataset, and DHS/DFAB will likely impose data-handling requirements.** The plan treats this as a design constraint now, not a retrofit:

- **Sensitive-dataset mode** (designed in the data layer from Phase 2, switchable per dataset): encrypted at-rest storage (SQLCipher / encrypted volume), no thumbnails or example images in exported reports (scores and charts only), configurable storage root so controlled data can live on approved media, and an audit log of every access to a sensitive dataset.
- **Air-gap friendly:** the app must run fully offline once installed — already true by design.
- **Requirements checklist:** before any controlled data is ingested, obtain DFAB/DHS's actual handling requirements (approved systems, encryption standards e.g. FIPS 140-3 validated crypto, retention/destruction rules, who may view images) and map each to a QualSight feature or an operational control. This is a Phase 6 gate for controlled data, but the hooks are built earlier.

When the app is later hosted for stakeholders: TLS, auth, and role-based dataset access — deferred to post-beta but the data layer is designed so nothing has to be re-architected.

## 10. Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| python-ofiq immaturity (v0.3.x, small community) | Medium | Phase-0 spike tests it first; fallback = own pybind11 binding or C++ gRPC sidecar behind the same internal interface |
| OFIQ bindings not thread-safe | Certain | Process pool (already the design) |
| CPU-only performance on large batches | Medium | Parallel workers; ONNX Runtime can use GPU if one exists; batch jobs are async anyway |
| No commercial SDK licenses held | Certain (current state) | Score-file importer decouples analysis from local SDK access; SDK adapters deferred to post-beta |
| Controlled dataset arrives with unanticipated handling rules | Medium | Sensitive-dataset mode designed in from Phase 2; requirements checklist gate before ingesting controlled data (§9) |
| Model/weight redistribution licenses (OFIQ models, InsightFace) | Medium | Install-time download rather than bundling; check terms before distributing to stakeholders |
| Demographic-slice analysis is politically/scientifically sensitive | Medium | Keep out of beta; design later with DFAB guidance and proper statistical rigor |
| Scope creep across 6 phases | High | Each phase independently demo-able; beta line is drawn at Phase 6 |

## 11. Open questions

Resolved Aug 13, 2026: commercial SDKs — none currently held (Tier 3 deferred); datasets — open-source plan in §8; GPU — yes, workstation has one; name — **QualSight**.

Still open:

1. Is there a **lab server/VM** where a hosted instance could eventually run for stakeholders?
2. **The specific DHS/DFAB data-handling requirements** for the eventual controlled dataset — needed before that data is ingested (see §9 checklist gate); not blocking for open-data phases.
3. **IJB-C**: file the NIST access request — who signs the agreement (Aaron / Clarkson)?

## 12. References

- OFIQ project (BSI): https://github.com/BSI-OFIQ/OFIQ-Project — v1.1.2, Feb 2026
- python-ofiq bindings: https://github.com/unicef/python-ofiq — v0.3.1, May 2026
- eu-LISA OFIQ program: https://www.eulisa.europa.eu/activities/research-and-innovation/ofiq
- ISO/IEC 29794-5: Biometric sample quality — Part 5: Face image data
- NIST MEDS-II (SD32): https://www.nist.gov/itl/iad/btg/special-database-32-multiple-encounter-dataset-meds
- NIST biometric special databases index: https://www.nist.gov/itl/iad/btg/resources/biometric-special-databases-and-software
- XQLFW (cross-quality LFW): https://mediatum.ub.tum.de/doc/1640000/ (paper)
- CR-FIQA (ERC evaluation methodology in practice): https://github.com/fdbtrs/cr-fiqa
