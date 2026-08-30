# Add-ons are a closed allowlist with per-Run composition

Date: 2026-08-27 · current implementation updated 2026-08-29

## Context

Operators need one discoverable inventory for matchers, datasets, API connections, preprocessing, and
controlled transformations. The beta has real lifecycle operations for six matchers, one registered
identity-labeled LFW input plus three downloadable pair-only verification datasets, three
matcher-preprocessing profiles, and seven derived-dataset transformations, but
those capabilities were split across hardcoded frontend lists and registry/runtime endpoints.

Generic package or module loading would allow unreviewed Python or native code to run in the local API
process. A remote REST connector would create a biometric-data egress and secrets boundary that the
local-only beta does not implement. Separately versioned acquisition manifests admit the six exact
author-published matcher checkpoints and the exact LFW, CFP-FP, and AgeDB-30 pair archive for the
registered local noncommercial scope. Identity-labeled LFW-tree and SCRFD acquisition remain outside
the cold-start package boundary.

## Options

1. Load arbitrary package, module, command, or remote API definitions from manifests. Rejected because
   this bypasses the beta's code-review, offline, provenance, rights, and process-isolation boundaries.
2. Display a marketplace with download/connect actions that are not executable. Rejected because it
   would misrepresent missing acquisition and connector implementations.
3. Publish a typed, closed catalog over existing real drivers and make composition explicit per Run.

## Decision

Use option 3. `GET /api/addons` derives the admitted inventory from the validated resource registry,
matcher runtime manager, frozen preprocessing definitions, and controlled-transformation registry. It
contains code-owned driver identifiers but no executable hooks. Verification accepts a selected complete
matcher × preprocessing Cartesian product and records each leaf as an immutable Run. Advanced 1:N and
degradation analysis select from the same catalog. Controlled degradation remains a separate derived-set
operation rather than an implicit matcher-preprocessing swap.

The API rejects an explicitly empty selection, and the matrix domain rejects empty or duplicate axes.
Repeatable CLI flags preserve first occurrence and remove duplicates before construction. Each matrix
persists its expected leaf count so a reduced Cartesian Run remains `completed` after process restart;
status is not inferred from the former fixed six-leaf default. Migration retains six for historical native
matrices and backfills historical externally computed matrices to one expected leaf. The browser and CLI
expose the same exact matcher/profile identifiers. Derived-set operators are chosen only from catalog
records, with explicit finite parameter levels supplied per set.

API connections are empty and explicitly marked `NOT_ADMITTED_IN_BETA`. A future connector requires an
approved data-flow/classification, secrets outside scientific provenance, endpoint identity pinning,
explicit egress policy, and a code-owned local adapter with the immutable Run contract.

All six checkpoint acquisitions use one code-owned implementation bound per asset to its published
file identifier, exact HTTPS origin, observed redirect policy, expected size/SHA-256, factual rights
status, registered owner scope, private no-clobber storage, and durable receipt. The runtime resolves
the managed verified copy before the registered external path. The pair-dataset driver applies the
same download controls plus a closed regular-file extraction manifest and atomic import. Direct
identity-labeled LFW-tree acquisition and SCRFD detector acquisition remain unavailable because their
current evidence does not satisfy the cold-start contract.

## Consequences

- Operators see one truthful inventory and can load/unload admitted matcher runtimes.
- Matcher and preprocessing choices are swappable per Run without mutable global state.
- Reduced selections retain truthful completion state across restart, and API, CLI, and browser clients
  speak the same selection contract.
- External score artifacts stay inert local imports marked `EXTERNALLY_COMPUTED`; they are not API
  connections.
- Adding a new driver is a reviewed code and registry change, not a runtime package install.
- Admitted acquisition and blocked rights/source states are visible separately; no control simulates an
  unavailable download or remote connection.
