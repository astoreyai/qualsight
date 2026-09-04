# ADR 012: Compare migrated schemas semantically before snapshotting

Date: 2026-09-03

## Context

ADR 009 requires a current-version database to match the exact ordered migration ledger and every required
table, foreign key, and index before startup may skip a recovery snapshot. Its first implementation compared
raw SQLite `PRAGMA` rows with a fresh in-memory schema. Live beta-5 activation showed that this was stricter
than the decision intended: the complete schema-8 workstation database produced a new 269,500,416-byte
version-8-to-version-8 snapshot on every reopen.

The real migrated database and fresh schema have the same required named columns, foreign-key relationships,
and index terms. They differ in physical column IDs and declaration order created by `ALTER TABLE`, foreign-key
row numbering, and index column IDs. Four migrated columns also retain known declaration differences:
`matcher_leaves.expected_leaf_count` has default `6`, `operations.attempt` has default `1`, and
`operations.request_id` and `operations.result_id` are declared nullable even though every stored row has a
value. None of those differences indicates an interrupted transition.

## Options

1. Keep raw `PRAGMA` tuple equality and accept a full database snapshot on every startup.
2. Trust only `user_version` and the migration ledger, dropping structural validation.
3. Preserve exact ledger and required-object validation, but compare schema structures by semantic names and
   constraints, ignoring only nonsemantic SQLite row and column identifiers and explicitly accepting the four
   migration-produced column shapes with stored-value checks for nullable declarations.

## Decision

Choose option 3. Required tables must contain exactly the declared named columns. Column type, nullability,
default, and primary-key properties must match except for the four enumerated migration-produced shapes;
the two nullable declarations are accepted only when no stored row contains `NULL`. Required foreign keys
must match their referenced table, source and target columns, actions, and match policy. Required indexes must
match term order, names, direction, collation, and key status. SQLite-assigned physical column IDs and
foreign-key row numbering are not semantic differences.

This refines ADR 009's structural-agreement definition; it does not weaken its exact migration-ledger check,
its required-object boundary, or its snapshot-and-restore behavior for incomplete current-version databases.

## Consequences

- A fully migrated schema-8 database reopens without producing a no-op recovery snapshot.
- Missing or extra required columns, changed constraints, foreign keys, index terms, or ledger entries still
  trigger the existing private, integrity-checked snapshot and repair path.
- Future migrations that intentionally produce another declaration difference must name and test that shape;
  the comparison does not accept arbitrary structural drift.
- Real registered-LFW recovery coverage exercises both the no-op reopen and incomplete-schema restoration paths.
