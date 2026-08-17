# QualSight Continual Development Loop

**Control contract:** v1.6  
**Date:** August 17, 2026  
**State:** Active controller; Phases 0, 1, and 2 are accepted under owner decision 28 (QS-A00 through QS-A10, QS-B10 through QS-B14, QS-C20, QS-C22, QS-C23, QS-C24, QS-C25); current evidence children QS-A02.R7, QS-A02.LFW.R0, QS-A02.LFW.P0, QS-A02.SCRFD.P0, QS-A03.R1, QS-A04.R1, QS-A04.R2, QS-A06.R1O, QS-A06.R2, QS-B12.LFW.FULL, QS-C22.CROP.R0, QS-C22.MATRIX.LFW, and QS-D31.R0 are accepted; refinement parent QS-A02.LFW is accepted; QS-C21 and QS-C25 remain BLOCKED on a real external score artifact; no whole capability is dependency-ready; execution of the registered installed OFIQ v1.1.2 runtime and of the frozen matcher runtime (registered SCRFD-10G, AdaFace, ElasticFace, TransFace bytes) on registered inputs is authorized by the owner use-permission receipt  
**Companion graph:** [ATG_PLAN.md](./ATG_PLAN.md)  
**Product authority:** [PRD.md](./PRD.md)

This document defines how QualSight development proceeds through finite, evidence-bearing Atomic Task Graph cycles. It does not create an unbounded agent loop. Each cycle freezes a finite batch of ready work, executes it in dependency order, records real evidence, and terminates.

## L-1 — Frozen decision record

These constraints apply to every refinement, execution, and repair:

1. PRD.md v0.3 is the product-scope authority. PLAN.md v0.2 is evidence only for the specifically retained OFIQ integration research, dataset research, environment facts, and schedule heuristics. A conflict is resolved in favor of PRD.md.
2. The implementation root is /mnt/projects/ofiqgui. It contains planning and control documents, pinned Python and Node workspace manifests and locks, the resource and metric registries and validators, canonical domain schemas, the registry-bound execution contract, the OFIQ v1.1.2 engine adapter, the FastAPI control plane with its append-only SQLite store (Runs, projects with versions, datasets, samples, jobs, quality-result index, artifacts, audit), the project lifecycle, the registered-dataset importer, the batch engine with Parquet output, the verification protocol/comparison schemas, the frozen matcher runtime and six-Run matrix, the verification metric engine, the reproduction/export CLI, and the React/TypeScript PWA console with its Dataset · Batch · Explorer and Verification workbenches, in an initialized Git worktree on branch main. The backend pins `pydantic==2.13.4`, `fastapi==0.141.1`, `uvicorn==0.52.3`, `pyarrow==25.0.1`, `duckdb==1.5.5`, `torch==2.10.0`, `timm==1.0.28`, `onnxruntime==1.28.0`, `opencv-python-headless==5.0.0.93`, and `numpy==2.4.6` (dev: `httpx==0.28.1`); the console pins React 18.3.1, Vite 5.4.11, TypeScript 5.6.3, and vite-plugin-pwa 0.21.1. HEAD remains unborn, and no commit or push has occurred.
3. No synthetic, simulated, fabricated, randomly generated, or mock data may enter source, tests, figures, notebooks, reports, or analyses. A small test fixture must be a deterministic subset of real data with recorded provenance, license, allowed use, source hashes, and selection rule.
4. Every shipped function and workflow must be complete. Unresolved work markers, unimplemented branches, hardcoded surrogate results, and alternate presentation-only paths are forbidden. A stakeholder demonstration must execute the same real pipeline used by the product.
5. The initial real OFIQ conformance input is the existing r-01-frontal.png file identified in ATG_PLAN.md. Its license limits it to reproducing OFIQ conformance tests. It may not be copied into general fixtures, modified, republished, or used as evidence for unrelated dataset workflows. Its limited input scope does not authorize the BLOCKED OFIQ runtime.
6. On 2026-08-17 the project owner granted local noncommercial research use of the registered LFW bytes, the OFIQ v1.1.2 resources, the SCRFD-10G detector, and the AdaFace, ElasticFace, and TransFace checkpoints, and instructed that QualSight never ships them but acquires them at setup time from official or mirror sources under pinned SHA-256 identities (`resources/authorizations/qualsight-owner-use-permission-granted-2026-08-17.json`). LFW, the three matchers, SCRFD, and the installed OFIQ v1.1.2 runtime are therefore `APPROVED_LOCAL_RESEARCH_USE` with the effective scope `QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1`; each keeps its factual third-party rights status, and matcher training-set identity exclusion is recorded as a published claim. LFW local archive/tree bytes, all 6,000 raw pair rows, and three exact official UMass protocol documents are reconciled; the current `buffalo_l.zip` and its `det_10g.onnx` member are byte-reconciled to the official InsightFace v0.7 release asset. Verification, FIQA-utility, and cross-matcher slices remain blocked on the frozen matcher runtime and on the missing real external matcher-score artifact.
7. OFIQ 1.1.0 observations remain historical evidence and carry owner decision `NOT_APPROVED`. The official v1.1.2 tag was built without sudo in the isolated sibling root `/mnt/projects/02_perception_biometrics/OFIQ-v1.1.2-build` by `scripts/ofiq_v112_build.sh` from the preserved byte-identical archives, installed, smoke-tested on r-01, and validated against the official conformance table (787/787); the installed executable, libraries, configuration, and 24-file model tree are registered as `ofiq-v1-1-2-linux-runtime` (`APPROVED_LOCAL_RESEARCH_USE`, `VERIFIED_LICENSE`). Only that registered installed runtime executes for QualSight; the acquisition candidate record and the 1.1.0 build tree remain immutable evidence.
8. Completed scientific Runs are immutable. A changed parameter creates a new Run.
9. No commit, push, publication, deployment, purge, migration, or transmission to another person occurs without the authority required by the workspace instructions. A cycle may prepare a reversible local change only within its authorized scope.
10. Validated nodes and prior loop iterations stay frozen during repair. Failures are repaired through the smallest affected subgraph.
11. Acceptance of a refinement slice does not accept its parent. QS-A02.R6, QS-A02.LFW.R0, QS-A02.LFW.P0, QS-A02.SCRFD.P0, QS-A03.R1, QS-A04.R1, QS-A06.R1O, and QS-C22.CROP.R0 preserve their validated outputs while QS-A02.LFW and all five capability parents remain BLOCKED on their unfulfilled predicates; superseded resource, domain, and execution events remain immutable history.
12. Project-owner authorization, third-party rights, and effective approved scope are separate fields. No cycle may infer one from another.
13. QS-C22 contains exactly six immutable real-data Runs: AdaFace, ElasticFace, and TransFace crossed with `SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1` and `SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1`. Matcher comparisons hold preprocessing fixed; preprocessing comparisons hold matcher and protocol fixed.
14. Unpinned or unverified downloads, random weights, unsafe checkpoint loads, non-strict or key-mapped state loading, silent pair filtering, unregistered bytes, network calls during scientific runtime, and pooling results across the two preprocessing conditions are prohibited. Setup-time acquisition (archives, checkpoints, and pinned upstream architecture sources) is allowed only with a pinned source and SHA-256 identity that refuses on mismatch.

## Level 0 — Cycle interface

**Input**

- the canonical PRD;
- the capability DAG and node ledger in ATG_PLAN.md;
- the current filesystem and source-control state;
- the pinned project manifests, lockfiles, and project-local package-manager configuration;
- registered real-data and external-resource evidence;
- acceptance commands attached to ready nodes;
- the frozen constraints above.

**Output**

- a finite set of accepted atomic slices;
- before-and-after gate evidence for every slice;
- an authorized real runtime observation for stateful or visual behavior, or explicit evidence that a blocked resource was refused before process launch;
- updated requirement, provenance, and node ledgers;
- an explicit terminal result: completed, body failure, bound exhaustion, stalled, or blocked by an unavailable real input.

## Level 1 — Enclosing DAG

~~~mermaid
flowchart LR
    N0[Validate authorities, workspace, resources] --> N1[Compute dependency-ready set]
    N1 --> N2[Freeze finite cycle batch]
    N2 --> L1[[L1: bounded development cycle]]
    L1 --> N3[Run integrated gate]
    N3 --> N4[Persist evidence and recompute graph state]
~~~

There is no back-edge from N4 to N0. Continual development consists of starting a new finite cycle against the newly persisted state. This keeps each execution graph acyclic and auditable.

## Level 2 — Controller nodes

N0–N4 preserve the cycle interface but are not all leaf invocations. ATG_PLAN.md retains the literal commands and results that produced historical evidence; under v1.6 (owner decision 28) Phase 2 is closed on QualSight-executed Runs, QS-C21 is parked before QS-H74, and QS-D30 is the dependency-ready whole capability. For each frozen cycle, S3 must refine every remaining parameterized controller action into literal leaves before L1 starts. Prose in this table is never sufficient authority to execute an edit or launch a process.

| Node | Input | Literal action | Output |
|---|---|---|---|
| N0 | PRD.md, PLAN.md, ATG_PLAN.md, current root | Run the exact current validation commands authorized in ATG_PLAN.md; executing gates may launch only the registered installed OFIQ v1.1.2 runtime on registered inputs | Source hashes, tool versions, resource hashes, Git state, and drift findings |
| N1 | N0 output and capability registry | Select whole capabilities or explicitly authorized child slices whose every predecessor is ACCEPTED and whose complete Definition of Ready is satisfied | Ordered ready set; in v1.5 it is empty for whole capabilities because QS-C21 lacks its real input |
| N2 | Ready set | Freeze a finite batch ordered by priority, then topological position, then node ID | Immutable batch B and integer bound K = size of B |
| L1 | Frozen batch B | Execute the bounded loop defined below | Accepted batch or explicit non-success terminal state |
| N3 | L1 accepted output | Run the batch’s recorded full-gate command and its authorized runtime-observation or pre-launch-refusal command | Integrated gate exit codes, counts, artifacts, and observations |
| N4 | N3 output | Append actual node and iteration records to atg/ledger.jsonl and update ATG_PLAN.md statuses in one reviewed patch | Durable evidence and newly computable ready set |

N1, N2, and N4 are not allowed to infer missing commands, data, paths, licenses, or acceptance thresholds. A node with a missing field is BLOCKED, not ready.

## v1.6 controller snapshot

This snapshot is routing state, not new acceptance evidence. The companion graph and append-only ledger carry the corresponding commands, results, and hashes.

| Class | Count | IDs or value | Controller effect |
|---|---:|---|---|
| Accepted whole capabilities | 21 | QS-A00, QS-A01, QS-A02, QS-A03, QS-A04, QS-A05, QS-A06, QS-A07, QS-A08, QS-A09, QS-A10, QS-B10, QS-B11, QS-B12, QS-B13, QS-B14, QS-C20, QS-C22, QS-C23, QS-C24, QS-C25 | Frozen accepted outputs; Phase 0, 1, and 2 exits satisfied (Phase 2 under decision 28); Phase 2 executed except imported scores |
| Current accepted evidence children | 13 | QS-A02.R7, QS-A02.LFW.R0, QS-A02.LFW.P0, QS-A02.SCRFD.P0, QS-A03.R1, QS-A04.R1, QS-A04.R2, QS-A06.R1O, QS-A06.R2, QS-B12.LFW.FULL, QS-C22.CROP.R0, QS-C22.MATRIX.LFW, QS-D31.R0 | Frozen child outputs |
| Superseded historical child events | 11 | QS-A02.R0, QS-A02.R1, QS-A02.R2, QS-A02.R3, QS-A02.R4, QS-A02.R5, QS-A02.R6, QS-A03.R0, QS-A04.R0, QS-A06.R0, QS-A06.R1M | Preserved in the ledger; not current-byte evidence |
| Blocked parent capabilities | 1 | QS-C21 | Parked by decision 28 as a direct predecessor of QS-H74; accepts only when a real externally computed score artifact is registered (owner input) |
| Blocked refinement parents | 0 | none | — |
| Accepted refinement parents | 1 | QS-A02.LFW | Dataset registry consumed by QS-B11 |
| Dependency-ready whole capabilities | 1 | QS-D30 | Phase 3 entry; owner-selected next work is Phases 3, 4, 5, and 7 in dependency order |
| Authorized OFIQ process executions | registered v1.1.2 runtime only | `ofiq-v1-1-2-linux-runtime` on registered inputs or generated batch lists of recorded dataset samples under a common approved scope | Refusal before `Popen` for every other runtime, path, or scope |
| Authorized matcher/detector executions | frozen runtime only | registered SCRFD-10G, AdaFace IR-101, ElasticFace-Arc R100, TransFace-S bytes through `qualsight.matchers` (strict, weights_only, pinned architecture sources) | Any other loader path is refused |
| Direct backend runtime dependencies | 10 | `pydantic==2.13.4`, `fastapi==0.141.1`, `uvicorn==0.52.3`, `pyarrow==25.0.1`, `duckdb==1.5.5`, `torch==2.10.0`, `timm==1.0.28`, `onnxruntime==1.28.0`, `opencv-python-headless==5.0.0.93`, `numpy==2.4.6` | Pinned schema-validation, control-plane, analytics, and matcher-runtime dependencies |

The historical atomic `A05` repository-initialization leaf remains accepted evidence under QS-A01; it is distinct from the accepted QS-A05 Metric Registry capability.

Current beta resource blockers are:

1. No real external matcher-score artifact is registered; the bounded score directory contains only `.gitkeep` (blocks QS-C21, which under decision 28 blocks only QS-H74). This is an owner input: supply a real externally computed score artifact over a real protocol, or amend the QS-C25 predicate.
2. The fixed 640-canvas native condition fails to detect 43 protocol pairs' faces on the funneled 250×250 images; the failures are explicit in the six Runs.
3. LFW results are QualSight-executed on the funneled release with SCRFD alignment and are not the matchers' published LFW figures.

Controlled-data requirements and IJB-C agreement authority remain unresolved but do not block the open-data beta path unless that scope is explicitly activated.

## L1 — Bounded development cycle

**Input:** frozen batch B from N2  
**Body:** process one dependency-ready slice through S1–S8 below  
**Guard:** every node ID in B has status ACCEPTED  
**Bound:** K = size of B, recorded as an integer before the first iteration  
**Progress:** accepted-node count within B must increase by exactly one per completed iteration  
**Output:** accepted batch plus per-node evidence

If an iteration cannot accept its selected slice, L1 exits immediately with the inner loop’s terminal state. It does not skip the slice and claim progress.

### L1 body

~~~mermaid
flowchart LR
    S1[Select first ready node] --> S2[Clarify and freeze slice interface]
    S2 --> S3[Refine slice to atomic invocations]
    S3 --> S4[Record baseline and prove acceptance check is red]
    S4 --> L2[[L2: until-green implementation repair]]
    L2 --> S5[Run full gate against baseline]
    S5 --> S6[Observe authorized runtime or refusal]
    S6 --> S7[Adversarially inspect diff and evidence]
    S7 --> S8[Accept slice and append ledger]
~~~

| Node | Required action and result |
|---|---|
| S1 | Choose the first node by priority, topological position, and node ID. The choice must not be re-reasoned after work begins. |
| S2 | Run the clarification gate. Freeze source requirement IDs, inputs, output, files in scope, data/resource entries, acceptance predicate, rollback, and outward-action boundary. |
| S3 | Invoke the ATG planning procedure on this slice until every leaf is one literal tool call, command, or patch with a defined output. Preserve the parent node interface. |
| S4 | Run the recorded full baseline and targeted acceptance command before changing implementation. Record exit codes, pass/fail counts, named failures, source commit or NOT_A_GIT_WORKTREE, and trusted fixture mtimes. A behavior slice must show a failing targeted check before implementation; an already-green check requires proving the requirement is already satisfied rather than editing code. |
| L2 | Apply and validate the minimal implementation through the bounded loop below. |
| S5 | Run the entire recorded gate, not a grep-filtered subset. Compare counts and names with S4. |
| S6 | Exercise the actual authorized changed path with the registered real input. If registry state prohibits launch, observe refusal before process start and do not substitute a historical run. Visual work requires a real rendered observation. |
| S7 | Inspect all changed lines against the frozen slice interface, the real-data/full-implementation constraint, old-contract consumers, data handling, and rollback. Findings are hypotheses until confirmed in source or runtime. |
| S8 | Mark the node ACCEPTED only when every predicate passes. Append evidence and update traceability. No commit or push is implied. |

## L2 — Until-green implementation repair

**Input:** confirmed red acceptance check and atomic slice subgraph  
**Body:** apply one minimal patch, then run the targeted acceptance command  
**Guard:** targeted acceptance command exits 0 and every named targeted assertion passes  
**Bound:** 5 iterations  
**Progress:** the ordered pair (failing targeted assertions, unverified acceptance predicates) must strictly decrease after each iteration  
**Output:** targeted gate evidence and changed-file set

The body mutates shared files and therefore runs sequentially. Independent read-only diagnosis commands may run in parallel before a patch.

Exit handling:

- **body failure:** localize the failed leaf, repair only its minimal subgraph, and resume the same iteration;
- **bound exhaustion with progress:** stop and report the partial evidence; increasing the bound requires an explicit new decision;
- **stall:** restore the last known-good state when the iteration introduced a regression, then stop and replace the failed approach;
- **blocked input:** stop and name the absent real dataset, resource, license, credential, or user-owned decision.

## Optional L3 — Bounded discovery

Use discovery only when a finite inventory cannot be known without searching.

**Guard:** a round returns zero new in-scope findings  
**Bound:** 3 rounds  
**Progress:** new-finding count must strictly decrease each round  
**Body:** run the same frozen search surface, classify new findings, and update the inventory  
**Output:** closed inventory or a stalled/bound-exhausted report

Discovery never authorizes implementation outside the selected slice.

## Definition of Ready

A capability may enter B only when all fields below contain real, verified values:

- stable node ID and PRD source lines;
- one output artifact or behavior;
- exact files or directories in scope;
- accepted predecessor nodes;
- real input path, identity/version, hash, mtime, classification, and immutable manifest when data is required;
- exact project-owner authorization receipt, resource-specific decision, permitted scope, and authorization time;
- independent third-party-rights status, evidence bytes, restrictions, and redistribution boundary;
- effective approved scope shared by every resource the node consumes;
- external runtime identity, version, hash, dependency closure, and no-network/no-autoresolution requirements;
- literal targeted acceptance command and expected predicate;
- literal full-gate command with recorded baseline;
- literal runtime-observation command or procedure;
- rollback procedure;
- old-contract consumers to check;
- outward-action boundary;
- estimated blast radius.

A missing field changes the node state to BLOCKED with the missing evidence named. It is never filled with a marker or invented value.

## Definition of Done

A slice is ACCEPTED only when:

1. its pre-change acceptance check was observed and recorded;
2. its targeted gate exits 0;
3. its full gate is compared with the recorded baseline and introduces no unexplained failure;
4. its authorized actual runtime path succeeds on registered real input; a refusal-only safety slice instead proves that the process is not launched and cannot claim engine execution;
5. every result is traceable to source data, configuration, code state, versions, and hashes;
6. all data are registered real inputs or provenance-preserving derivatives, every code path is fully implemented, and no unresolved marker or presentation-only branch was added;
7. the diff contains only authorized scope;
8. consumers of the old contract were checked;
9. the rollback remains valid;
10. the ledger contains the commands, exit codes, counts, artifacts, observations, and status;
11. scientific comparison slices preserve every frozen protocol row, label explicit failures, and keep matcher and preprocessing condition identities separate.

Compilation success alone is never sufficient.

## Ledger contract

The first authorized implementation cycle created atg/ledger.jsonl with its first real event; the append-only ledger must never be replaced with an empty or fabricated history. Historical runtime observations remain recorded even when the current resource state forbids repeating them. Every line is one actual node or loop-iteration record with:

- cycle ID and iteration index;
- node ID and historical ancestor ID;
- timestamp and operator;
- input hashes;
- exact action;
- output artifact paths and hashes;
- exit code;
- baseline and final pass/fail counts;
- guard evaluation;
- progress measure before and after;
- status;
- error classification;
- minimal repair subgraph when applicable;
- source commit and dirty state;
- authorized runtime observation or pre-launch refusal observation;
- unresolved external block.

Allowed node states are BLOCKED, READY, RUNNING, FAILED, ACCEPTED, and SUPERSEDED. Allowed loop exits are COMPLETED, BODY_FAILURE, BOUND_EXHAUSTED, STALLED, and BLOCKED_INPUT.

## Local repair rule

For a failure at leaf v_f:

1. identify v_f from the ledger;
2. trace to the lowest common historical ancestor a_f;
3. include only v_f, necessary predecessors, and consumers affected by its output;
4. freeze all other accepted outputs;
5. repair the minimal subgraph while preserving its external interface;
6. re-run the targeted gate, the affected group, the full gate, and the authorized runtime or pre-launch refusal observation;
7. re-ask only when the repair would contradict the frozen decision record.

## Cycle close

Every cycle closes with:

- accepted node IDs;
- exact gate delta from baseline;
- authorized real runtime evidence or explicit pre-launch refusal evidence;
- remaining blocked nodes and their specific missing inputs;
- dirty/committed/pushed state;
- one claim most likely to be wrong when runtime coverage is incomplete.

The next cycle starts from that persisted state as a new DAG execution. In v1.6, whole-capability cycles proceed from QS-D30 through Phases 3, 4, 5, and 7 in dependency order; QS-C21 stays parked until the owner registers a real external score artifact, and no substitute artifact may be generated to close it.
