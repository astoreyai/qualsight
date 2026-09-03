# QualSight architecture

QualSight is a single-user, loopback-only research workbench for the controlled reference workstation. The beta is not a hosted service, multi-user application, or portable bundle of biometric data and model runtimes.

## Runtime shape

```text
React console, same origin
        |
        v
FastAPI control plane on 127.0.0.1
   |        |                 |
   |        |                 +-- closed add-on catalog and lifecycle manager
   |        +-- bounded scientific and detector executors
   +-- SQLite evidence store and managed artifact directories
                |
                +-- OFIQ 1.1.2 native process
                +-- ONNX Runtime / OpenCV detector and preprocessing
                +-- PyTorch/CUDA matcher kernels
                +-- Arrow / Parquet / DuckDB analytical artifacts
```

The built React console is served from the FastAPI origin in the installed product. Installed execution
separates three explicit roots: `QUALSIGHT_CONSOLE_ROOT` selects the package-built console,
`PYTHONPATH` and the installed interpreter select package code, and `QUALSIGHT_REPOSITORY_ROOT` selects
the unprojected scientific and provenance authority. The installer and every environment-driven installed
start require that controlled Git root to be clean and at the package's recorded source commit, and require
the selected registry bytes to equal that commit's canonical unprojected registry. This prevents
code/version/provenance drift across the split. The development validator may use a separate local Vite
preview, but it still calls the loopback
API. The API holds a kernel-enforced exclusive lease on its data directory for the process lifetime. A
second mutating process is refused; the beta does not coordinate concurrent writers across multiple
server processes.

## Control plane and compute boundary

Python owns admission, validation, project lifecycle, immutable identities, provenance composition, queues, recovery, and HTTP presentation. Compute-intensive work stays in the registered native or numerical runtime: OFIQ, ONNX Runtime, OpenCV, PyTorch/CUDA, NumPy, Arrow, DuckDB, and Parquet. A language rewrite is not an accepted substitute for measured hotspot evidence.

Scientific work is admitted through bounded executors. A durable operation record is created before work runs, transitions through recorded states, and binds the completed result. Quality batches, matcher-matrix leaves, external-score imports, direct-pair comparisons, derived sets, utility/cross-matcher/degradation/identification analyses, and reporting figures/reports/exports/bundles publish only through operation-bound recovery contracts. Files and directories are staged privately, identity-marked, fsynced, promoted without overwrite, and reconciled into SQLite before completion is claimed. Startup and exact-request retry accept only a complete receipt whose project, result, request, membership, size, and SHA-256 identities match; incomplete, foreign, symlinked, or drifted output fails closed and remains purgeable. Derived-image admission is limited to 20,000 output files. Owner-protocol admission is limited to 20,000 real source samples and 20,000 output pairs and computes sampled pair ranks without allocating the all-pairs Cartesian set. Direct matcher execution runs in a terminable child process under the configured execution deadline; expiry records a real failure and releases bounded capacity. Readiness reports database/schema state, recovery, executor state, disk capacity, registry state, network guard, and the live data-directory lease.

## Evidence and storage

SQLite is the authoritative metadata and evidence index. Projects bind datasets, samples, protocols, operations, Runs, audits, analyses, reports, exports, and purge attestations. Large numerical and image artifacts remain in managed project directories and are referenced by immutable SHA-256 and byte length. The store verifies recorded artifacts before presenting or reusing them.

Database migration is snapshot-backed. Startup treats the schema as current only when the user version, exact migration ledger, required tables, foreign keys, and indexes all match. Backup uses SQLite's online backup mechanism rather than copying a live database file. Restore refuses overwrite unless explicitly authorized and preserves the prior bytes on failure.

Recorded browser reads are project-scoped. Dataset, sample, image, geometry, quality, derived-set, and direct-comparison routes verify the project relationship before returning evidence. Those browser-facing records omit biometric storage paths while private records retain them for integrity verification, recovery, and purge. Exact globally registered inputs are addressed by opaque registered-sample identity, never by host path. The browser-facing Run projection preserves scientific identities, hashes, metrics, execution state, and safe artifact metadata while the byte-exact path-bearing Run remains private in SQLite.

## Add-ons and connections

Add-ons are closed, code-owned drivers. The catalog enumerates six matchers, three source/pair preprocessing profiles, seven controlled real-input transformations, and the admitted verification-data protocols. It does not load arbitrary modules, packages, commands, hooks, or manifests supplied at runtime.

Acquisition verifies registered origin policy, redirects, byte limits, archive structure, SHA-256, size, rights state, and durable receipts before promotion. Matcher source pull, checkpoint acquisition, load, unload, and execution are distinct states. Public add-on records expose opaque identities, integrity state, and hashes without managed or external paths; the private receipt retains exact locations. A matcher and preprocessing profile are selected per immutable Run.

Connections is a policy surface, not a generic connector. This beta has zero admitted remote biometric providers. A future provider requires an exact origin, credential boundary, egress authorization, request/response schema, retention policy, audit contract, reproducibility policy, and project-scoped data handling before code or UI can make it actionable.

## Security and scientific boundaries

- The server binds to loopback and enforces the local browser origin boundary.
- Outbound network access is deny-by-default and temporarily opened only for an admitted acquisition target.
- Owner uploads are size-bounded, decoded, hashed, classified, copied to managed storage, audited, and purgeable.
- Project purge stores complete private path/error evidence locally but exposes only counts, verification state, evidence identity, and limitations to the browser.
- Operator-declared pair relationships are `OPERATOR_ASSERTED_UNVERIFIED`. They never authorize correctness or population-performance claims.
- Single-image transformations and trajectories are descriptive. FMR, FNMR, EER, ROC, DET, and operating-point claims require an admitted labelled protocol and recorded metric policy.
- Release archives exclude biometric images, derived images, embeddings, model checkpoints, databases, runtime observations, screenshots, credentials, and workstation-generated evidence.

## Packaging boundary

The source package installs the Python control plane and built console. Reviewed native runtimes, model checkpoints, and rights-controlled datasets remain external governed dependencies and are acquired or located through the registered workstation paths. The installed package serves its own console while resolving scientific authority from the exact clean controlled commit recorded in its package manifest; an ambiguous root, mismatched commit, tracked drift, or registry-byte mismatch is refused before dependency mutation and again at environment-driven startup. The package is fail-closed when those dependencies are missing or drifted. It is not represented as a relocatable cold-start package.

## Broader deployment

Multi-process mutation, authentication, tenant authorization, remote access, provider credentials, external audit anchoring, capacity certification, independent usability evaluation, and ATO evidence are post-beta work. None is implied by the local controlled-workstation acceptance.

See [THREAT_MODEL.md](THREAT_MODEL.md), [SCIENTIFIC_METHODS.md](SCIENTIFIC_METHODS.md), [API.md](API.md), and the accepted decisions in [decisions/](decisions/).
