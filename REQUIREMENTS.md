# QualSight Requirement Registry

**Registry version:** v0.2  
**Date:** August 14, 2026  
**Canonical source:** [PRD.md](./PRD.md) v0.3  
**Execution graph:** [ATG_PLAN.md](./ATG_PLAN.md) v0.5  
**Controller:** [ATG_LOOP.md](./ATG_LOOP.md) v0.5

This registry gives every PRD section a stable requirement ID, release classification, single acceptance owner, and required evidence. It is the traceability input for QS-A00 and for all later ATG refinement.

## Frozen source snapshot

| Artifact | SHA-256 | Mtime |
|---|---|---|
| PRD.md | cbdd00e9a43c67ae1e803b08cb1729fae23f842a79e3be5e7f18246c3bbf3025 | 2026-08-13 17:06:54.266266242 -0500 |
| PLAN.md | c60914f1815c3c241daf536c4427d4778ae69bd2c716807481a067bf3ea6cff4 | 2026-09-02 13:23:06.081840330 -0500 |
| Owner authorization receipt | 48b11e76ac1d7288c40f0052fa000ae72f7edd4c930db3057751c7c7f841a3fa | 2026-08-14 10:18:34.006247146 -0500 |
If any recorded identity changes, this registry is stale until the changed clauses are reviewed and the affected rows are updated explicitly.

## Classification

| Class | Meaning |
|---|---|
| REQUIRED_BETA | Must pass before QS-H74 can accept the beta |
| CONDITIONAL | Must pass if the named option, resource, or workflow is activated; otherwise it does not block QS-H74 |
| DEFERRED | Preserved in architecture but excluded from the first beta unless explicitly promoted |
| POST_BETA | May begin only after QS-H74 and a new authorization |

Every normative statement within a listed PRD section inherits the row’s class and acceptance owner unless a more specific row for that section overrides it. When a section has mixed release status, suffix rows split the requirements explicitly.

## Requirement register

| ID | Section | Source | Class | Requirement | Acceptance owner | Required evidence |
|---|---:|---|---|---|---|---|
| QS-REQ-001 | 1 | [PRD.md:17–83](./PRD.md) | REQUIRED_BETA | Deliver the local biometric quality, recognition, evaluation, failure-analysis, visualization, reporting, and reproducibility chain without cloud, mobile, kiosk, or DHS production dependencies. | QS-H74 | Real end-to-end run and immutable provenance |
| QS-REQ-002 | 2 | [PRD.md:85–101](./PRD.md) | REQUIRED_BETA | Keep sample quality, recognition performance, and operational policy distinct; version standards semantics; make no certification claim. | QS-H74 | Terminology audit, metric registry, and rendered-output inspection |
| QS-REQ-003 | 3 | [PRD.md:103–124](./PRD.md) | REQUIRED_BETA | Make every reported result traceable to dataset, samples, matcher, quality engine, configuration, threshold, protocol, statistical method, code, and environment. | QS-H74 | Complete trace from each terminal artifact to its Run |
| QS-REQ-004 | 4 | [PRD.md:126–157](./PRD.md) | REQUIRED_BETA | Satisfy the beta portions of goals G1–G7: quality, 1:1 matcher evaluation, utility, cross-matcher analysis, reproducibility, failure investigation, and stakeholder communication. | QS-H74 | Canonical workflow gate |
| QS-REQ-005 | 5 | [PRD.md:159–179](./PRD.md) | REQUIRED_BETA | Exclude operational identity decisions, production deployment, certification, cloud/SaaS, mobile/kiosk/remote capture, enterprise identity, commercial redistribution, autonomous conclusions, and production identity proofing. | QS-H74 | Scope scan and runtime/network inspection |
| QS-REQ-006A | 6 | [PRD.md:181–222](./PRD.md) | REQUIRED_BETA | Support single-user mode with researcher attribution and schemas for users, roles, projects, membership, and audit events. | QS-H71 | Attribution and audit integration tests |
| QS-REQ-006B | 6 | [PRD.md:194–220](./PRD.md) | CONDITIONAL | Support local-account mode only if activated without restructuring scientific data. | QS-H71 | Activated-mode authentication and ownership tests |
| QS-REQ-006C | 6 | [PRD.md:183–193](./PRD.md) | DEFERRED | Preserve Administrator, Researcher, Reviewer, and Viewer role semantics without requiring multi-user enforcement in the first beta. | QS-H71 | Schema and policy validation |
| QS-REQ-006D | 6 | [PRD.md:222](./PRD.md) | POST_BETA | Add enterprise SSO/OIDC only after beta authorization. | QS-X66 | External identity contract and security gate |
| QS-REQ-007 | 7 | [PRD.md:226–257](./PRD.md) | REQUIRED_BETA | Implement Project as the organizational root with lifecycle, metadata, notes, tags, ownership, and access policy. | QS-B14 | Project lifecycle round-trip |
| QS-REQ-008 | 8 | [PRD.md:259–293](./PRD.md) | REQUIRED_BETA | Save complete scientific state, keep completed Runs immutable, and create new Runs for changed execution parameters. | QS-F54 | Save/reopen comparison and immutable-Run test |
| QS-REQ-009 | 9 | [PRD.md:295–357](./PRD.md) | REQUIRED_BETA | Record the complete Run manifest and distinguish completed, validated, reproduced, and scientifically supported states. | QS-A10 | Schema tests and one real reproducible Run |
| QS-REQ-010 | 10 | [PRD.md:359–401](./PRD.md) | REQUIRED_BETA | Import real datasets with identities, pair/search definitions, splits, hashes, integrity checks, permissions, and mandatory modality. | QS-B14 | Approved real-data import gate |
| QS-REQ-011 | 11 | [PRD.md:403–436](./PRD.md) | REQUIRED_BETA | Report population, evaluation support, completeness, and quality-coverage statistics from real dataset rows. | QS-B14 | Counts reconciled to source manifest |
| QS-REQ-012A | 12 | [PRD.md:438–474](./PRD.md) | REQUIRED_BETA | Map image, dataset, and manifest inputs into canonical schemas with saved reusable mappings. | QS-B14 | Real import and mapping round-trip |
| QS-REQ-012B | 12 | [PRD.md:449–472](./PRD.md) | REQUIRED_BETA | Import real matcher scores, score matrices, pair labels, candidate lists, or embeddings and permanently mark external computation. | QS-C25 | Real score import reconciliation |
| QS-REQ-012C | 12 | [PRD.md:454–472](./PRD.md) | REQUIRED_BETA | Import QualSight project, experiment, Run, and reproduction bundles through canonical schemas. | QS-F54 | Bundle import validation |
| QS-REQ-013 | 13 | [PRD.md:476–538](./PRD.md) | REQUIRED_BETA | Implement typed quality-engine results, preserve engine-specific scales and semantics, and distinguish image quality, biometric utility quality, and standardized biometric quality. | QS-A10 | Adapter contract and real OFIQ result |
| QS-REQ-014 | 14 | [PRD.md:540–571](./PRD.md) | REQUIRED_BETA | Render real sample imagery, face artifacts, measurements, statuses, policy, annotations, and provenance while separating engine measurements from user policy. | QS-B14 | Real browser observation |
| QS-REQ-015 | 15 | [PRD.md:573–606](./PRD.md) | REQUIRED_BETA | Compute FMR, FNMR, TMR/TAR, ROC, DET, EER, AUC/pAUC, and threshold operating-point tables with explicit definitions. | QS-C25 | Independent calculation cross-check |
| QS-REQ-016 | 16 | [PRD.md:608–632](./PRD.md) | REQUIRED_BETA | Report event/trial/subject counts, uncertainty method and level, warn on unsupported precision, and use subject-aware resampling when observations are dependent. | QS-C25 | Real-comparison uncertainty gate |
| QS-REQ-017A | 17 | [PRD.md:634–661](./PRD.md) | REQUIRED_BETA | Preserve 1:N-capable schemas and the distinction between open-set and closed-set identification without implementing 1:N execution. | QS-A10 | Schema validation |
| QS-REQ-017B | 17 | [PRD.md:634–661](./PRD.md) | POST_BETA | Implement FPIR, FNIR, TPIR, rank-k, CMC, true-mate rank, and gallery-effect evaluation. | QS-X60 | Approved real 1:N experiment |
| QS-REQ-018 | 18 | [PRD.md:663–696](./PRD.md) | REQUIRED_BETA | Preserve acquisition, enrollment, quality-assessment, template, and comparison failures and distinguish conditional from end-to-end performance. | QS-C25 | Failure-path tests on real inputs |
| QS-REQ-019 | 19 | [PRD.md:698–704](./PRD.md) | REQUIRED_BETA | Keep quality metrics engine-scoped and report values, raw measurements, status, latency, missing measures, distributions, percentiles, and coverage. | QS-B14 | Batch-to-summary reconciliation |
| QS-REQ-020 | 20 | [PRD.md:706–737](./PRD.md) | REQUIRED_BETA | Compute ERC/EDC, AU-ERC, partial AU-ERC, risk-coverage, quality-conditioned errors, associations, failure prediction, and calibration only when probabilistically valid. | QS-D34 | Real FIQA-utility gate |
| QS-REQ-021 | 21 | [PRD.md:739–755](./PRD.md) | REQUIRED_BETA | Require an explicit, versioned pair-quality aggregation policy and support sensitivity analysis. | QS-D34 | Policy registry and real-pair comparison |
| QS-REQ-022 | 22 | [PRD.md:757–787](./PRD.md) | REQUIRED_BETA | Compare quality utility across two compatible real matcher sources using overlays, differences, overlap, interactions, transfer, and uncertainty without inventing an undefined independence score. | QS-E43 | Cross-matcher real-run gate |
| QS-REQ-023 | 23 | [PRD.md:789–795](./PRD.md) | REQUIRED_BETA | Report real mated/non-mated score distributions and label optional separation diagnostics correctly. | QS-C25 | Source-row reconciliation |
| QS-REQ-024 | 24 | [PRD.md:797–827](./PRD.md) | REQUIRED_BETA | Separate matcher, quality-policy, and standards/engine thresholds and preserve the chosen threshold policy during utility analysis. | QS-D34 | Threshold sweep reconciliation |
| QS-REQ-025 | 25 | [PRD.md:829–861](./PRD.md) | POST_BETA | Enable demographic analysis only with approved data, metadata, research authorization, group counts, uncertainty, and fixed-threshold support. | QS-X62 | Authorized population study |
| QS-REQ-026 | 26 | [PRD.md:863–893](./PRD.md) | POST_BETA | Implement PAD metrics and interactions only as a distinct optional evaluation module with no certification claim. | QS-X63 | Approved real PAD evaluation |
| QS-REQ-027 | 27 | [PRD.md:895–932](./PRD.md) | POST_BETA | Apply controlled transforms only to registered real inputs and preserve every parameter and transformation edge. | QS-X61 | Provenance-complete derived-data experiment |
| QS-REQ-028A | 28 | [PRD.md:934–969](./PRD.md) | REQUIRED_BETA | Provide bootstrap, subject/paired bootstrap, permutation, correlation, regression, mixed-effects, effect-size, and multiple-comparison methods with recorded implementations and sampling details. | QS-D34 | Statistical cross-check on real observations |
| QS-REQ-028B | 28 | [PRD.md:940](./PRD.md) | CONDITIONAL | Run generalized additive, survival-style, or Bayesian hierarchical methods only when an activated real study justifies them. | QS-D34 | Method-specific validation and diagnostics |
| QS-REQ-029 | 29 | [PRD.md:971–1018](./PRD.md) | REQUIRED_BETA | Implement the machine-readable, versioned Metric Registry before metric screens. | QS-A10 | Registry schema and core metric definitions |
| QS-REQ-030 | 30 | [PRD.md:1020–1048](./PRD.md) | REQUIRED_BETA | Support named evaluation profiles with inputs, metrics, operating points, plots, methods, metadata, and report template without certification claims. | QS-F54 | Profile-driven real experiment |
| QS-REQ-031 | 31 | [PRD.md:1050–1062](./PRD.md) | CONDITIONAL | Provide a clearly labeled NIST 800-63 reference analysis only if activated and never label it certification or universal compliance. | QS-F54 | Label and threshold-source inspection |
| QS-REQ-032 | 32 | [PRD.md:1064–1084](./PRD.md) | CONDITIONAL | Record initialization, latency, throughput, processing failures, host resources, and latency summaries for benchmark Runs. | QS-H74 | Benchmark provenance and system-metric reconciliation |
| QS-REQ-033 | 33 | [PRD.md:1086–1124](./PRD.md) | REQUIRED_BETA | Query and inspect the real samples, subjects, scores, thresholds, decisions, ground truth, transforms, provenance, and annotations responsible for errors. | QS-E43 | Visualization-to-failure drilldown |
| QS-REQ-034 | 34 | [PRD.md:1126–1147](./PRD.md) | REQUIRED_BETA | Attach authored and timestamped tags, flags, comments, classifications, and review status to scientific entities. | QS-E43 | Annotation persistence test |
| QS-REQ-035 | 35 | [PRD.md:1149–1177](./PRD.md) | REQUIRED_BETA | Provide the beta visualization families at sample, dataset, matcher, utility, cross-matcher, and statistical levels. | QS-F54 | Rendered real-data inspection |
| QS-REQ-036 | 36 | [PRD.md:1179–1204](./PRD.md) | REQUIRED_BETA | Make every visualization a traceable artifact with source data, Run, protocol, metric, thresholds, statistics, provenance, and exports. | QS-F54 | Figure provenance reconciliation |
| QS-REQ-037 | 37 | [PRD.md:1206–1233](./PRD.md) | REQUIRED_BETA | Build, save, and reload dashboards whose panels retain scientific lineage. | QS-F54 | Dashboard round-trip |
| QS-REQ-038 | 38 | [PRD.md:1235–1264](./PRD.md) | REQUIRED_BETA | Build PDF, HTML, and print reports with methods, limitations, versions, hashes, figures, and tables. | QS-F54 | Report-to-source reconciliation |
| QS-REQ-039 | 39 | [PRD.md:1266–1285](./PRD.md) | REQUIRED_BETA | Export CSV, JSON, and Parquet data with identifiers sufficient to resolve the source Run. | QS-F54 | Export round-trip |
| QS-REQ-040 | 40 | [PRD.md:1287–1298](./PRD.md) | REQUIRED_BETA | Export PNG, SVG, and PDF figures with an optional machine-readable provenance sidecar. | QS-F54 | Render and sidecar validation |
| QS-REQ-041 | 41 | [PRD.md:1300–1326](./PRD.md) | REQUIRED_BETA | Export a checksummed reproduction bundle containing the specified experiment, protocol, environment, metric, result, figure, and table artifacts without automatically including restricted images. | QS-F54 | Bundle schema and checksum gate |
| QS-REQ-042 | 42 | [PRD.md:1328–1349](./PRD.md) | REQUIRED_BETA | Validate dependencies, inputs, models, execution, and outputs and report only observed reproduction states without changing targets. | QS-F54 | Reproduction rerun |
| QS-REQ-043 | 43 | [PRD.md:1351–1374](./PRD.md) | REQUIRED_BETA | Support the beta metric families through the Metric Registry and select metrics by experimental question rather than showing every metric together. | QS-F54 | Profile and rendered-output inspection |
| QS-REQ-044 | 44 | [PRD.md:1376–1422](./PRD.md) | REQUIRED_BETA | Provide coherent navigation across projects, data, quality, biometrics, experiments, analysis, exploration, output, and system functions included in beta. | QS-H74 | Real browser workflow |
| QS-REQ-045 | 45 | [PRD.md:1424–1483](./PRD.md) | REQUIRED_BETA | Implement the PWA, FastAPI control plane, worker/runtime boundaries, SQLite metadata, Parquet analytical data, optional DuckDB queries, filesystem artifacts, and SHA-256 identity. | QS-A10 | Phase-0 vertical-slice architecture gate |
| QS-REQ-046 | 46 | [PRD.md:1485–1493](./PRD.md) | REQUIRED_BETA | Keep the PWA as the console and all scientific processing in backend workers. | QS-B14 | Process-boundary integration test |
| QS-REQ-047 | 47 | [PRD.md:1495–1509](./PRD.md) | REQUIRED_BETA | Use SQLite for control metadata, Parquet for analytical records, DuckDB where beneficial, and the filesystem for samples, models, and artifacts. | QS-A10 | Persistence contract tests |
| QS-REQ-048 | 48 | [PRD.md:1511–1535](./PRD.md) | REQUIRED_BETA | Operate locally without runtime cloud calls, telemetry, automatic upload, external image transmission, leaked credentials, or unauthorized restricted-image export; treat embeddings as sensitive. | QS-H74 | Network, log, and export security gates |
| QS-REQ-049 | 49 | [PRD.md:1537–1547](./PRD.md) | REQUIRED_BETA | Remove all application-managed dataset derivatives and describe the result only as application-level verified purge. | QS-H74 | Purge inventory test |
| QS-REQ-050 | 50 | [PRD.md:1549–1567](./PRD.md) | REQUIRED_BETA | Record meaningful local user and scientific events without telemetry behavior. | QS-H74 | Audit completeness test |
| QS-REQ-051A | 51 | [PRD.md:1569–1631](./PRD.md) | REQUIRED_BETA | Define the typed ExecutionAdapter base and specialized contracts with identity, schemas, versions, resources, device needs, determinism, outputs, artifacts, and provenance. | QS-A10 | Contract-test suite |
| QS-REQ-051B | 51 | [PRD.md:1633–1643](./PRD.md) | REQUIRED_BETA | Represent detection, landmarks, alignment, cropping, preprocessing, and templates explicitly; preserve and display distinct provenance for `SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1` and `SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1` without conflating their results. | QS-C25 | Six-Run preprocessing provenance comparison |
| QS-REQ-051C | 51 | [PRD.md:1645–1698](./PRD.md) | REQUIRED_BETA | Support the actual OFIQ CLI bridge plus AdaFace IR-101, ElasticFace-Arc R100, and TransFace-S Glint360K through typed verified-resource execution paths; prohibit automatic downloads, random weights, and unregistered runtime bytes. | QS-C25 | Real adapter executions and network/resource refusal gates |
| QS-REQ-051D | 51 | [PRD.md:1700–1702](./PRD.md) | POST_BETA | Implement controlled-transform adapters only under QS-REQ-027. | QS-X61 | Derived-data provenance gate |
| QS-REQ-051E | 51 | [PRD.md:1704–1706](./PRD.md) | REQUIRED_BETA | Execute beta statistical methods as registered, provenance-bearing components. | QS-D34 | Statistical adapter contract tests |
| QS-REQ-051F | 51 | [PRD.md:1708–1714](./PRD.md) | REQUIRED_BETA | Preserve external-result origin permanently and never conflate imported output with QualSight execution. | QS-C25 | Import provenance test |
| QS-REQ-051G | 51 | [PRD.md:1716–1718](./PRD.md) | POST_BETA | Add container and REST adapters after beta. | QS-X64 | Isolated execution contract |
| QS-REQ-051H | 51 | [PRD.md:1720–1736](./PRD.md) | POST_BETA | Add UI-created adapters only after a complete validated registration and security design. | QS-X64 | Registration, validation, and sandbox gate |
| QS-REQ-051I | 51 | [PRD.md:1738–1750](./PRD.md) | REQUIRED_BETA | Include modality in every relevant schema while limiting beta execution to face. | QS-A10 | Schema tests |
| QS-REQ-052 | 52 | [PRD.md:1754–1773](./PRD.md) | REQUIRED_BETA | Register every external model and resource with version, source, license, restrictions, redistribution/research status, hash, citation, and installation method; record project-owner authorization separately from third-party-rights status and evidence, because owner approval cannot satisfy missing third-party rights. | QS-A02 | Resource-registry validation |
| QS-REQ-053A | 53 | [PRD.md:1775–1785](./PRD.md) | REQUIRED_BETA | Validate the official OFIQ v1.1.2 tag at commit `d589ca27896a472446abc9eca8b62947ee47db19` with the model and ONNX resources selected by its tagged CMake configuration; retain v1.1.0 only as historical evidence and never relabel it. | QS-A10 | Real OFIQ Phase-0 gate |
| QS-REQ-053B | 53 | [PRD.md:1777–1785](./PRD.md) | REQUIRED_BETA | Integrate AdaFace IR-101, ElasticFace-Arc R100, and TransFace-S Glint360K as three provenance-distinct local matcher paths and execute each under both registered preprocessing conditions, producing six immutable Runs. | QS-C25 | Six real matcher-condition Runs |
| QS-REQ-053C | 53 | [PRD.md:1777–1785](./PRD.md) | DEFERRED | Preserve US-FIQA/OpenFIQA and CR-FIQA or SER-FIQ as additional comparator targets without blocking the first beta when their resources are not approved. | QS-E43 | Promoted-scope cross-quality comparison |
| QS-REQ-054A | 54 | [PRD.md:1789–1823](./PRD.md) | REQUIRED_BETA | Import real matcher scores through Tier 1 with external-computation provenance. | QS-C25 | Real score-import gate |
| QS-REQ-054B | 54 | [PRD.md:1807–1821](./PRD.md) | REQUIRED_BETA | Execute the three selected local research matchers through Tier 2 under both registered preprocessing conditions, with one immutable Run per matcher-condition pair and no silent pair filtering. | QS-C25 | Six real local-matcher gates |
| QS-REQ-054C | 54 | [PRD.md:1815–1816](./PRD.md) | DEFERRED | Add MagFace or any research matcher other than the promoted AdaFace, ElasticFace, and TransFace paths only after its resources are registered and its value is accepted into a cycle. | QS-E43 | Promoted-scope matcher comparison |
| QS-REQ-054D | 54 | [PRD.md:1818–1821](./PRD.md) | POST_BETA | Add commercial matcher SDK adapters only after licensing and separate authorization. | QS-X67 | Licensed SDK contract and security gate |
| QS-REQ-055 | 55 | [PRD.md:1825–1865](./PRD.md) | REQUIRED_BETA | Deliver the listed project, user, data, quality, matcher, verification, FIQA, cross-matcher, statistics, visualization, reporting, and provenance scope. | QS-H74 | Capability-owner acceptance set |
| QS-REQ-056A | 56 | [PRD.md:1867–1920](./PRD.md) | REQUIRED_BETA | Complete Phase 0, 1, 2, 3, 4, 5, and 7 gates in dependency order. | QS-H74 | Accepted phase-gate nodes |
| QS-REQ-056B | 56 | [PRD.md:1912–1914](./PRD.md) | POST_BETA | Execute the advanced Phase-6 program only after beta and separate authorization. | QS-X59 | Advanced-analysis release gate |
| QS-REQ-057 | 57 | [PRD.md:1922–1958](./PRD.md) | REQUIRED_BETA | Complete the canonical workflow from project creation through traceable experiment-bundle export without manual Python analysis. | QS-H74 | Real end-to-end runtime |
| QS-REQ-058 | 58 | [PRD.md:1960–1979](./PRD.md) | REQUIRED_BETA | Refuse scientific-reportable status unless all listed population, protocol, engine, matcher, preprocessing, threshold, metric, uncertainty, software, model, and Run fields are known. | QS-H74 | Reportability validator |
| QS-REQ-059 | 59 | [PRD.md:1981–1997](./PRD.md) | REQUIRED_BETA | Preserve lineage while moving between samples, quality, matchers, errors, aggregate statistics, failures, and publication/stakeholder artifacts. | QS-H74 | Bidirectional lineage traversal |
| QS-REQ-060 | 60 | [PRD.md:1999–2031](./PRD.md) | REQUIRED_BETA | Give every number, plot, and scientific comparison its required definition, population, protocol, operating point, uncertainty, source, configuration, and changed/fixed record. | QS-H74 | Terminal provenance audit |

## Decision authority and PLAN.md overrides

| ID | Source | Binding decision | Owner |
|---|---|---|---|
| QS-SRC-001 | [PLAN.md:5](./PLAN.md) | PRD.md controls product scope; PLAN.md cannot introduce beta capabilities. | QS-A00 |
| QS-SRC-002 | [PLAN.md:66–70](./PLAN.md) | OFIQ process/library research is admissible evidence, but the working first adapter is selected from locally verified runtime facts. | QS-A06 |
| QS-SRC-003 | [PLAN.md:161–180](./PLAN.md) | Dataset research may nominate real datasets only; the synthetic-identity recommendation is excluded and cannot enter the resource registry. | QS-A02 |
| QS-SRC-004 | [PLAN.md:100–107](./PLAN.md) | Live capture, kiosk, tablet, and mobile behavior is superseded by PRD beta non-goals. | QS-A00 |
| QS-SRC-005 | [PLAN.md:96–99](./PLAN.md) | PRD storage assignments control: SQLite holds control metadata and Parquet holds analytical results. | QS-A03 |
| QS-SRC-006 | [PLAN.md:20](./PLAN.md) | The 1.1.2 release statement does not relabel the installed OFIQ 1.1.0 binary; version reconciliation is QS-A06. | QS-A06 |
| QS-SRC-007 | [PLAN.md:35](./PLAN.md) | The GPU workstation statement is accepted only as the observed RTX 3090 environment recorded in ATG_PLAN.md. | QS-A01 |
| QS-SRC-008 | [PLAN.md:149–159](./PLAN.md) | Calendar estimates are nonbinding heuristics and never acceptance predicates. | QS-A00 |
| QS-SRC-009 | [Owner authorization receipt:8](./resources/authorizations/qualsight-local-noncommercial-research-2026-08-14.json) | Acquire the official OFIQ v1.1.2 tagged-CMake-selected resources for evidence review; acquisition does not authorize build or execution. | QS-A06 |
| QS-SRC-010 | [Owner authorization receipt:9](./resources/authorizations/qualsight-local-noncommercial-research-2026-08-14.json) | Project-owner local noncommercial approval is recorded independently and never substitutes for third-party-rights evidence. | QS-A02 |
| QS-SRC-011 | [Owner authorization receipt:10](./resources/authorizations/qualsight-local-noncommercial-research-2026-08-14.json) | Promote AdaFace IR-101, ElasticFace-Arc R100, and TransFace-S Glint360K to the beta matcher set, subject to each resource becoming executable and rights-cleared. | QS-C22 |
| QS-SRC-012 | [Owner authorization receipt:11](./resources/authorizations/qualsight-local-noncommercial-research-2026-08-14.json) | Compute and display both registered preprocessing conditions as separate scientific results. | QS-C25 |

## Beta acceptance owner set

The beta terminal node QS-H74 may accept only after these phase gates are ACCEPTED:

| Phase gate | Requirement range |
|---|---|
| QS-A10 | Phase-0 architecture, schemas, Metric Registry, resources, OFIQ adapter, API/PWA vertical slice, and reproducible Run |
| QS-B14 | Project lifecycle, approved real dataset import, batch quality, sample/dataset exploration, and provenance |
| QS-C25 | Real score import, approved local matcher, 1:1 protocol, verification metrics, uncertainty, and failure drilldown |
| QS-D34 | Pair-quality policy, ERC/risk-coverage, uncertainty/error models, and threshold-policy analysis |
| QS-E43 | Compatible real matcher pair, cross-matcher metrics, disagreement/failure analysis, and annotations |
| QS-F54 | Provenance-bearing figures, dashboards, reports, exports, and reproduction bundle/mode |
| QS-H74 | Security, attribution/audit, offline packaging, operating documentation, and canonical end-to-end workflow |

Conditional requirements are added to the applicable phase gate only when activated. Deferred and post-beta requirements cannot silently enter QS-H74.

## Controlled beta release boundary

The `0.1.0-beta.5` release authority is limited to one trusted researcher operating one
Clarkson-controlled workstation. The beta-required controls are the accepted QS-A10 through QS-H74
gates, the exact committed dependency locks and resource registry, loopback-only service binding,
single-process data-directory ownership, private managed storage, explicit and hash-verified add-on
acquisition, immutable scientific provenance, application-level verified purge,
verified local offline backup and no-clobber restore, and the complete staged release gate recorded by
`scripts/run_release_beta_gate.py`. The OFIQ native rebuild path is governed by an exact Conan 2.18.1
lock/profile/closure and verifies downloaded archives, the resolved package graph, 787 upstream
conformance cases, dynamic linkage, and a real-image smoke result. Model, conformance-image, and native
runtime bytes remain external and are not release payloads.

The release is unsigned, nonportable, not authorized for production or shared multi-user operation, and
does not include biometric data, model checkpoints, native runtimes, or operational databases. Its
Python socket guard detects and refuses non-loopback socket use by the Python process; it is not an
operating-system egress control for native executables or child processes. Remote API connectors remain unadmitted. Beta 3 admits the recorded single-image transformation and descriptive trajectory workflow;
dataset-scale 1:N execution, population degradation claims, and the container research adapter remain
outside QS-H74 beta acceptance even though their code and local validation are present in the repository.

## DHS transition-only controls

The following controls are outside the controlled beta and cannot be inferred from QS-H74 acceptance:

1. a DHS mission sponsor, approved mission need, CONOPS, operational users, and measurable mission exit criteria;
2. named organizational decision authorities, lifecycle ownership, and separation of duties;
3. privacy, civil-rights, records-management, data-retention, and approved biometric-data-flow decisions;
4. a selected security baseline, independent control assessment, authorization decision, and continuous monitoring;
5. authenticated multi-user access, host egress enforcement, managed encryption, operational continuity with defined RTO/RPO, incident response, and decommissioning;
6. resolved acquisition and redistribution rights for every dataset, checkpoint, native dependency, and model;
7. a portable, signed distribution with an authenticated trust channel and independently operated installation; and
8. independent DHS operator usability and mission-population performance evaluation.

These are transition kill conditions, not incomplete beta implementations. No document, test, SBOM,
checksum, project-owner receipt, or local scientific result in this repository satisfies them.

## Change control

Changing a requirement requires one patch that:

1. cites the changed PRD or a new frozen decision;
2. updates the affected registry row without reusing its ID for a different meaning;
3. updates ATG_PLAN.md nodes or edges if dependency shape changes;
4. identifies accepted downstream evidence invalidated by the change;
5. records the change in atg/ledger.jsonl after validation.

Removed requirements remain in history as superseded records after Git history exists; IDs are never reassigned.
