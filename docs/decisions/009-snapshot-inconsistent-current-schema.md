# ADR 009: Snapshot an inconsistent current-version database before repair

Date: 2026-09-01

## Context

QualSight uses SQLite `PRAGMA user_version`, an ordered `schema_migrations` ledger, and declared
table/index definitions to identify the local evidence-store schema. Startup previously skipped its
pre-migration snapshot whenever `user_version` equalled the current target. A reproduced database had
target version 8 but only ledger entries 1–7 and lacked `direct_pair_comparisons`. Startup created the
missing table through the general schema script, then an injected version-8 ledger failure occurred
outside the later migration transaction. The database had changed without a pre-repair rollback image.

## Options

1. Trust `user_version` alone and treat missing objects or ledger entries as startup corruption. This
   avoids repair but does not preserve a preflight image for diagnosis or an operator-directed retry.
2. Run the general schema script first and snapshot only databases with an older version. This is the
   behavior that allowed DDL to precede rollback evidence.
3. Treat the target state as complete only when version, exact ordered ledger, and all required
   table/foreign-key/index structures agree; snapshot every other supported state before reconciliation.

## Decision

Choose option 3. A current-version database skips the snapshot only if its migration ledger equals the
declared ledger exactly and its required structural schema equals the schema built in a clean in-memory
database. Any mismatch uses the existing integrity-checked, private, no-clobber snapshot path, including
a version-8-to-version-8 recovery snapshot. A failed reconciliation restores that exact snapshot.

## Why

The version integer is a hint, not evidence that DDL and ledger publication completed together. Exact
ledger and structural agreement is cheap at single-process startup and gives repair the same rollback
contract regardless of which part of the prior transition was interrupted. The comparison deliberately
allows unrelated extra application objects while requiring every declared table, foreign key, and index.

## Consequences

- Interrupted current-version transitions retain an exact pre-repair image and can be retried after the
  cause is removed.
- Startup performs a small in-memory schema construction and SQLite metadata comparison.
- A database newer than the supported target remains refused; this decision does not implement downgrade.
- Trigger bodies are not part of the persistent declared schema and are not treated as required objects.
- Real recovery coverage injects a ledger failure, verifies exact byte restoration, removes the injected
  trigger, retries reconciliation, and checks the preserved real project and dataset records.
