# QualSight hardening and optimization closure contract

## Goal

Close every locally implementable correctness, containment, recovery, and measured-performance issue from the 2026-08-28 Python/hardening audit without changing biometric scientific identities or expanding the product beyond its local single-operator beta boundary.

## Done when

- A real subprocess regression proves that two supported QualSight processes cannot mutate one `QUALSIGHT_DATA_DIR` concurrently, including server-versus-CLI operation.
- Every accepted background operation is durably visible before work starts and is reconciled truthfully after process restart; a killed operation cannot disappear as 404 or collide silently with orphan output.
- Project purge has a durable intent/progress/terminal record and restart reconciliation across file deletion, metadata deletion, and attestation.
- HTTP uploads are rejected by a streaming byte ceiling before full buffering or decode; queued scientific work has a bounded admission contract with deterministic overload responses.
- Liveness and readiness are separate; readiness verifies the database, private managed storage, resource integrity needed for admitted work, executor admission, and unreconciled operations.
- Native execution has an explicit deadline and bounded output; container execution also has CPU, memory, and PID limits recorded in provenance.
- The full twelve-leaf real LFW path has reproducible stage timing and peak RSS/VRAM evidence. Memory-bound crop retention and full-gallery ranking are replaced only when real-data equivalence gates pass.
- Dependency/security evidence clearly separates pin/license inventory from vulnerability/SBOM/penetration/OS-encryption controls; no production or DHS authorization claim is made without those external gates.
- After every slice, its focused real test passes and the relevant complete gate shows the exact baseline delta. Final gates: backend full discovery, root full discovery, frontend typecheck/build, local-data audit, registry/ATG validation, `git diff --check`, and adversarial diff review.

## Never touch

- User-owned unrelated changes: `claude-skills`, `.claude/`, `.ignore`, and `.mcp.json`.
- Registered biometric bytes, model checkpoints, dataset images, historical ATG ledger lines, or accepted external rights statements.
- Scientific matcher identities, preprocessing definitions, score direction, thresholds, protocols, metrics, or numerical tolerances unless a real-data equivalence test first proves the change.
- Remote deployment, biometric egress, authentication-provider selection, controlled DHS data, or redistribution rights; those require external authority and remain explicit blocks.

## Stop if

- A previously green complete gate gains a new failure and the responsible slice cannot be restored immediately.
- Three evidence-based repair hypotheses fail for the same defect.
- A change would require inventing data, rights, credentials, provider behavior, or scientific outputs.
- More than 30 repository files outside the named hardening/performance implementation, tests, documentation, and append-only evidence surfaces would change.
