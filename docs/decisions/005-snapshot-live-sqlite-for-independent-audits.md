# ADR 005: Snapshot the live SQLite store before independent inspection

## Context

QualSight uses SQLite WAL mode while the API owns multiple reader connections and one locked writer.
Independent verification and local-data auditing previously opened that live database from child
processes. In the complete real test order, the cross-matcher verifier removed the live `-wal` and `-shm`
sidecars while parent connections still held their old descriptors. A later reader failed with extended
SQLite code `SQLITE_IOERR_SHORT_READ`; the writer's `PRAGMA integrity_check` still returned `ok`.

## Decision

On 2026-08-30, prohibit independent child processes from opening the API-owned live database while the API
owns its data directory. The `snapshot-database` CLI holds the exclusive data-directory lease while
`RunStore` creates a consistent private snapshot through SQLite's Backup API, verifies that snapshot, and
publishes it with no-clobber semantics. The cross-matcher verifier and local-data audit require a regular,
non-symlink snapshot path and open it read-only and immutable; the latter continues to scan the real data
tree for stored files and secrets. Without a snapshot, the local-data audit must acquire the same exclusive
lease or fail before opening the live database.

On 2026-09-03, apply the same boundary to process-death recovery tests. An API worker publishes its actual
accepted HTTP result identifier through an atomic, private, no-clobber record; a CLI worker is identified by its
canonical prepared-output receipt. The parent observes that identity and the real prepared output files without
opening SQLite, kills and reaps the worker, and only then verifies database state. The parent never joins the
child-owned live WAL merely to discover progress. Result identities come from the worker or canonical receipt,
not from filename reconstruction, and a partial artifact alone is not evidence that publication completed.

## Why

SQLite's WAL and shared-memory files are part of the live database state. A child verifier needs a stable
read surface, not participation in the API's live lock and checkpoint lifecycle. The Backup API provides a
transactionally consistent copy without moving, rewriting, or exposing the original database sidecars.

## Consequences

Independent inspection requires temporary storage equal to one database snapshot. Snapshot creation
requires the `.snapshot.sqlite3` suffix, refuses an existing destination, uses private file mode, verifies
`PRAGMA integrity_check`, and records the snapshot size and SHA-256. A missing snapshot, a symlink, a
non-regular file, a multi-link alias, a wrong-schema database, or the live store fails closed. Inspectors
hold a no-follow file descriptor through validation and give SQLite the descriptor through `/proc/self/fd`,
so a path replacement cannot change the inspected inode. Callers delete the snapshot after inspection. The
ordered real API regression binds the live WAL/SHM device and inode identities before and after verification
and exercises report, bundle, reproduction, audit, and purge afterward. A beta-5 gate failure confirmed why this
also applies to tests: the process received `SIGBUS` in `sqlite3WalFindFrame`, and the fault address was within the
active database's mapped WAL-index file while a parent observer and child worker overlapped.
