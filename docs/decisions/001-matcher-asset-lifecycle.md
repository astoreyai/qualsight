# Matcher asset lifecycle is explicit and operational

Date: 2026-08-26 · current implementation updated 2026-08-29

## Context

QualSight registers six frozen matcher checkpoints and their architecture sources. Every checkpoint has a registered size and SHA-256 identity. The closed acquisition manifest admits an explicit operator-triggered download for each exact checkpoint under the recorded owner-approved local-noncommercial scope; factual third-party rights states remain visible and redistribution is prohibited. The architecture sources have pinned URLs, commits, sizes, and hashes. Before this decision, scientific API jobs could acquire sources as an implicit side effect and each job constructed its own model, while the System panel exposed no lifecycle controls.

## Options

1. Bundle matcher bytes with the application. Rejected because registered redistribution conditions do not authorize that packaging.
2. Expose UI state without changing scientific execution. Rejected because a display-only load flag would not control the models jobs use.
3. Use one process-local runtime manager for verified dependencies, explicit source pulls, real load/unload, and job leases.

## Decision

Use option 3. Scientific endpoints verify local dependencies and refuse actionably when they are absent or drifted. Only the setup command and explicit System action may pull hash-pinned architecture sources, within the process network guard's explicit setup scope. A shared runtime manager owns the exact model instances used by matrix, degradation, and identification work. Unload and checkpoint replacement are refused while a model has active job leases. Lifecycle actions are globally serialized, and the runtime manager's exclusive-control lock remains held from mutation through linked completion-event persistence or exact rollback. Each lifecycle request persists an intent event before mutation and a linked completion or failure event afterward. Checkpoint acquisition is available only when its closed manifest records the expected bytes, source, redirect policy, receipt contract, and rights decision.

## Why

This makes the operator-visible state truthful, keeps scientific execution offline by default, prevents duplicate model residency, and preserves checkpoint provenance and rights constraints.

## Consequences

- Matrix, degradation, and identification API work no longer acquires sources implicitly.
- The System panel exposes checkpoint integrity, source integrity, runtime state, pull, load, unload, and durable write-ahead lifecycle events.
- CLI scientific work remains process-local but verifies dependencies before it starts.
- AdaFace, ElasticFace, MagFace, TopoFR, TransFace-B, and TransFace-S support managed acquisition from their exact admitted sources with durable receipts; publisher availability failures and byte mismatches fail closed.
- A scientific lease cannot observe a lifecycle mutation until its completion event is durable. If completion persistence fails, the exact pre-action runtime, sources, managed checkpoint, and receipt state is restored before the exclusive-control boundary opens.
