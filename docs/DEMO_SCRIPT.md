# QualSight — DFAB/DHS demonstration script (15 minutes)

**Prep (before the room):** `bash scripts/demo_dhs.sh` — starts the console on the recorded
evidence (persistent store), verifies every precomputed artifact, and ensures the *Stakeholder
Demo* dashboard. Everything below is either **recorded evidence** (immutable Runs from the beta
acceptance) or **executed live** on the registered engine in the room. Nothing is mocked; if the
live steps fail, they fail visibly — that honesty is part of the product.

**Framing sentence (say it early):** *"Every number you'll see traces to an immutable Run — data
hashes, model hashes, versioned metric definitions, thresholds with their kinds. QualSight is a
T&E workbench, not a certification: it never says 'ISO compliant', and quality–error relationships
are shown as associations, never causes."*

| Minute | Route | What to do | What it shows |
|---|---|---|---|
| 0–1 | `#/system` | Engine badge: OFIQ v1.1.2, registry state, 10 registered resources, attributed operator, network guard. | Controlled environment: registered runtime, offline, single attributed user. |
| 1–3 | `#/assess` | **LIVE** — ask the evaluator for a photo (USB/phone transfer to the workstation), *Assess your own photo*. UQS + 28 ISO/IEC 29794-5 measures appear in ~4 s with a Run link. | Bring-your-own-image works; decision-30 provenance (OWNER_SUPPLIED, hashed, purgeable) is stated on screen. |
| 3–5 | `#/assess` | On the registered sample: *Show face detection (matcher pipeline)* then *Show OFIQ landmarks (ADNet-98)*. Point at the two labels. | The two geometry sources are distinct and labelled — SCRFD five points feed the matchers; ADNet-98 feeds the quality measures. |
| 5–7 | `#/data` | **LIVE** — import a small evaluator directory (subject = folder name), run the quality batch on it. Show the distribution and drill into one sample. | Bring-your-own-dataset: identity-labelled import, real engine batch, per-sample drilldown. |
| 7–9 | `#/matchers` | Recorded six-Run matrix (3 matchers × 2 crops): DET overlay, operating points with exact 95 % CIs, error drilldown at FMR ≤ 10⁻³. Open **match evidence** on one false non-match: originals, the exact compared 112×112 crops, the pair's score on the genuine/impostor distributions, ACCEPT/REJECT per recorded threshold. | Verification is inspectable to the individual comparison — the "why" is recorded values placed in context. |
| 9–11 | `#/utility` | Recorded ERC overlay (AU-ERC with bootstrap CIs), threshold-policy table: "reject the worst N % by quality → FNMR before → after, errors prevented, good comparisons removed", rejected-pairs drilldown with images. | The FIQA centerpiece: quality → utility with uncertainty, at a fixed matcher threshold. |
| 11–12 | `#/cross` | Recorded cross-matcher contrasts. Say the finding: matchers agree on failures within a crop (Jaccard 0.93–0.97) while the two crops fail on nearly disjoint pairs (0.06–0.07) — face *selection*, not the matcher, drives most false non-matches here. | A real, non-obvious T&E finding the tool surfaced. |
| 12–13 | `#/reports` | *Stakeholder Demo* dashboard (recorded figures); build or open the HTML/PDF report; show a `.qsexp` bundle and its EXACT reproduction record. | Defensible reporting and one-click reproducibility. |
| 13–14 | `#/advanced` | Recorded 1:N (rank-1 97.2 %, FNIR 2.32 % @ FPIR 1 %, CMC, gallery-size effect) and the degradation response (UQS 52.8 → 5.8 at 15 % occlusion). | Beyond the beta bar: identification and controlled robustness. |
| 14–15 | `#/system` + ATG_PLAN.md | Close honestly: demographic analysis and PAD are **gated modules** — blocked until approved data exists, with the block reasons recorded in the plan. Q&A. | The tool refuses to fake what it cannot support — the strongest trust signal in the room. |

**Rules for the room**

* Never present LFW numbers as the matchers' published figures (they are QualSight-executed under
  frozen crops — the caveat is printed on every report).
* Never call anything "certified" or "compliant"; the words on screen are the words to use.
* The evaluator's photo and directory are hashed, recorded as OWNER_SUPPLIED_LOCAL_INPUT, never
  redistributed, and can be purged in front of them (`#/projects` → purge) — offer it.
