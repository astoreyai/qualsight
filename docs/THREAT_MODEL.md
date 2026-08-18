# QualSight threat model and local data controls

Scope: a single researcher's controlled workstation holding face images and derived biometric data
(quality measures, embeddings, comparison scores). QualSight is a local tool; it has no accounts, no
server, and no cloud component.

## Assets

1. Source images (LFW funneled release; the OFIQ conformance image) — owned by the researcher under
   the recorded use decisions; QualSight reads them at their registry paths and never copies them
   into its records, reports (unless the image policy explicitly permits and the classification is
   PUBLIC/PRIVATE), bundles, or exports.
2. Derived biometric data — embeddings, comparison scores, quality measures (classification
   `SENSITIVE_BIOMETRIC_DERIVED`), analysis documents, figures, reports.
3. Provenance records — the append-only SQLite store (projects, Runs, audit events, artifacts).
4. Registered runtimes and models — OFIQ v1.1.2, matcher checkpoints, SCRFD; identified by hash.

## Adversaries and controls

| Threat | Control | Evidence |
|---|---|---|
| Runtime exfiltration of images or derived data over the network | The control plane binds `127.0.0.1` only. The backend package contains one outbound-network code path — installation-time acquisition of pinned upstream matcher sources (`qualsight setup`) — and it runs before operation. `QUALSIGHT_NETWORK_GUARD=1` installs a process-wide guard that refuses every non-loopback socket connection and counts attempts; `/api/health` reports it. | `scripts/audit_local_data_controls.py` (static posture check), `scripts/offline_gate.py` (full workflow under the guard with zero refused attempts), backend test `test_network_guard_refuses_non_loopback_and_admits_loopback_in_a_fresh_process` |
| Secrets (tokens, credentials) leaking into records or logs | QualSight stores no credentials. The audit script scans every stored record, the ledger, and every text file under the data directory for the values of credential-like environment variables and common token shapes. | `scripts/audit_local_data_controls.py` → `LOCAL_DATA_CONTROLS_OK` |
| Unattributed scientific actions | Every meaningful action (project lifecycle, dataset import, engine and matcher Runs, analyses, figures, dashboards, reports, exports, bundles, reproductions, annotations, purges) writes an audit event with the configured user (`QUALSIGHT_ACTOR`, default `local-user:<login>`) and a timezone-aware timestamp; `GET /api/projects/{id}/audit`. | backend test `test_zz_audit_attribution_and_verified_purge`; offline gate step `audit-attributed` |
| Tampered inputs, models, or bundles | Registered artifacts are hash-pinned; adapters refuse unregistered runtimes and drifted bytes; bundles carry `checksums.sha256` and reproduction refuses (BLOCKED) on mismatch. | registry validators; reproduction test with a tampered bundle |
| Residual data after a study ends | Application-level verified purge: `POST /api/projects/{id}/purge` (confirmation must repeat the project id) deletes every derived artifact file the store records for the project under the data directory — quality Parquet and job directories, comparison/embedding files and matrix directories, analysis directories, figures, reports, bundles, exports naming the project's Runs — re-verifies their absence, records what was deleted / already absent / failed, and writes a `DATASET_PURGED` audit event. Immutable metadata rows remain as tombstones; source images are never touched. **Not performed:** secure erasure of SQLite free pages, filesystem journals, or backups — those are outside the application and must be handled by the workstation's own controls. | `qualsight/security/purge.py` (`SCOPE_STATEMENT`), backend test, `GET /api/projects/{id}/purge-scope` |
| Restricted images reaching outputs | Report images are embedded only under `image_policy=INCLUDE_PERMITTED` and only for PUBLIC/PRIVATE datasets; RESTRICTED sources are never embedded and RESTRICTED records are refused by the exporters; bundles carry paths and hashes only. | report/export code paths and tests |

## Out of scope

Multi-user access control, disk encryption, physical access, and OS hardening are the workstation's
responsibility. QualSight's optional local-account mode (PRD §50) is not required for the beta gate.
