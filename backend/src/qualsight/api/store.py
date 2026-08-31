"""Append-only SQLite store for scientific Runs and their context entities.

SQLite holds project, dataset, sample, experiment, Run and audit metadata
(PRD §47); the Run itself is stored as its validated JSON so that a re-read is
byte-for-byte the recorded evidence. Rows are never updated or deleted here:
a completed scientific Run is immutable and a changed parameter creates a new
Run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading

from typing import Any

from ..domain.entities import Artifact, AuditEvent, AuditEventType, Dataset, Experiment, Project, ProtocolKind, Sample, VerificationProtocol
from ..domain.primitives import CanonicalJson, ComponentBinding, ComponentState
from ..domain.run import CompletedRun
from ..engines.ofiq.adapter import SingleSampleRunBundle
from ..security.owner_file_lock import serialized_owner_file_mutation

SCHEMA_MIGRATIONS = (
    (1, "BASE_APPEND_ONLY_RUN_STORE"),
    (2, "REPORT_AND_MATCHER_LEAF_COMPATIBILITY_COLUMNS"),
    (3, "DURABLE_OPERATIONS_AND_PURGE_JOURNAL"),
    (4, "PAIR_PROTOCOLS_WITH_OPTIONAL_FOLD_AUTHORITY"),
    (5, "DURABLE_OPERATION_REQUEST_AND_ATTEMPT_IDENTITIES"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    evidence_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    sample_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    source_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    evidence_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    sample_id TEXT REFERENCES samples(sample_id),
    run_kind TEXT NOT NULL DEFAULT 'SINGLE_SAMPLE',
    status TEXT NOT NULL,
    engine_resource_id TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    audit_event_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_events (
    system_event_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS system_events_by_time ON system_events(occurred_at);
CREATE INDEX IF NOT EXISTS runs_by_sample ON runs(sample_id, start_time);
CREATE TABLE IF NOT EXISTS project_versions (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    version INTEGER NOT NULL,
    lifecycle TEXT NOT NULL,
    name TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (project_id, version)
);
CREATE INDEX IF NOT EXISTS runs_by_project ON runs(project_id, start_time);
CREATE TABLE IF NOT EXISTS dataset_statistics (
    dataset_id TEXT PRIMARY KEY REFERENCES datasets(dataset_id),
    computed_at TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_by_dataset ON samples(dataset_id, source_path);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    uri TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quality_results (
    job_id TEXT NOT NULL,
    sample_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    run_id TEXT,
    outcome TEXT NOT NULL,
    failure_reason TEXT,
    uqs_scalar INTEGER,
    uqs_native REAL,
    measures_json TEXT NOT NULL,
    PRIMARY KEY (job_id, sample_id)
);
CREATE INDEX IF NOT EXISTS quality_results_by_sample ON quality_results(sample_id);
CREATE INDEX IF NOT EXISTS quality_results_by_job_uqs ON quality_results(job_id, uqs_scalar);
CREATE TABLE IF NOT EXISTS protocols (
    protocol_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    evidence_sha256 TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS protocol_pairs (
    protocol_id TEXT NOT NULL REFERENCES protocols(protocol_id),
    pair_id TEXT NOT NULL,
    fold INTEGER,
    source_order INTEGER NOT NULL,
    label TEXT NOT NULL,
    left_sample_id TEXT NOT NULL,
    right_sample_id TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (protocol_id, pair_id)
);
CREATE INDEX IF NOT EXISTS protocol_pairs_by_fold ON protocol_pairs(protocol_id, fold, source_order);
CREATE TABLE IF NOT EXISTS matcher_leaves (
    run_id TEXT PRIMARY KEY,
    matrix_id TEXT NOT NULL,
    leaf_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    dataset_id TEXT NOT NULL,
    protocol_id TEXT NOT NULL,
    matcher_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    comparisons_parquet TEXT NOT NULL,
    comparisons_sha256 TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    metrics_sha256 TEXT NOT NULL,
    comparison_count INTEGER NOT NULL,
    scored_count INTEGER NOT NULL,
    failed_count INTEGER NOT NULL,
    expected_leaf_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS matcher_leaves_by_matrix ON matcher_leaves(matrix_id, leaf_id);
CREATE TABLE IF NOT EXISTS utility_analyses (
    analysis_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    matcher_run_id TEXT NOT NULL,
    quality_job_id TEXT NOT NULL,
    quality_measure TEXT NOT NULL,
    pair_quality_policy TEXT NOT NULL,
    directory TEXT NOT NULL,
    utility_sha256 TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    pair_table_sha256 TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS utility_analyses_by_project ON utility_analyses(project_id, created_at);
CREATE TABLE IF NOT EXISTS cross_matcher_analyses (
    analysis_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    matrix_id TEXT NOT NULL,
    quality_job_id TEXT NOT NULL,
    quality_measure TEXT NOT NULL,
    pair_quality_policy TEXT NOT NULL,
    directory TEXT NOT NULL,
    document_sha256 TEXT NOT NULL,
    pairs_sha256 TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cross_matcher_by_project ON cross_matcher_analyses(project_id, created_at);
CREATE TABLE IF NOT EXISTS annotations (
    annotation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL,
    context_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS annotations_by_subject ON annotations(subject_type, subject_id, created_at);
CREATE TABLE IF NOT EXISTS figures (
    figure_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    directory TEXT NOT NULL,
    source_run_ids_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    provenance_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS figures_by_project ON figures(project_id, created_at);
CREATE TABLE IF NOT EXISTS dashboards (
    dashboard_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    name TEXT NOT NULL,
    template TEXT NOT NULL,
    version INTEGER NOT NULL,
    supersedes TEXT,
    layout_json TEXT NOT NULL,
    panel_run_ids_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dashboards_by_project ON dashboards(project_id, created_at);
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    title TEXT NOT NULL,
    directory TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    html_sha256 TEXT NOT NULL,
    pdf_sha256 TEXT NOT NULL,
    json_sha256 TEXT,
    provenance_sha256 TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reports_by_project ON reports(project_id, created_at);
CREATE TABLE IF NOT EXISTS bundles (
    bundle_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identification_protocols (
    protocol_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    dataset_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    rank_depth INTEGER NOT NULL,
    gallery_count INTEGER NOT NULL,
    mated_probe_count INTEGER NOT NULL,
    nonmated_probe_count INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identification_entries (
    protocol_id TEXT NOT NULL REFERENCES identification_protocols(protocol_id),
    source_order INTEGER NOT NULL,
    role TEXT NOT NULL,
    sample_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    PRIMARY KEY (protocol_id, source_order)
);
CREATE TABLE IF NOT EXISTS identification_searches (
    search_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    protocol_id TEXT NOT NULL,
    matcher_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    directory TEXT NOT NULL,
    candidates_parquet TEXT NOT NULL,
    candidates_sha256 TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    metrics_sha256 TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS derived_sets (
    derived_set_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    source_dataset_id TEXT NOT NULL,
    source_resource_id TEXT NOT NULL,
    derived_dataset_id TEXT NOT NULL,
    directory TEXT NOT NULL,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS derived_samples (
    derived_sample_id TEXT PRIMARY KEY,
    derived_set_id TEXT NOT NULL REFERENCES derived_sets(derived_set_id),
    parent_sample_id TEXT NOT NULL,
    transform TEXT NOT NULL,
    parameter REAL NOT NULL,
    level_index INTEGER NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    chain_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS derived_samples_by_set ON derived_samples(derived_set_id, level_index);
CREATE TABLE IF NOT EXISTS degradation_analyses (
    analysis_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    derived_set_id TEXT NOT NULL,
    quality_job_id TEXT NOT NULL,
    matcher_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    directory TEXT NOT NULL,
    document_sha256 TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purges (
    purge_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    verified INTEGER NOT NULL,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purge_journal (
    purge_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('INTENT_RECORDED', 'FILES_IN_PROGRESS', 'FILES_VERIFIED', 'METADATA_VERIFIED', 'FAILED', 'COMPLETED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    targets_json TEXT NOT NULL,
    progress_json TEXT NOT NULL,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS purge_journal_one_unresolved_per_project
ON purge_journal(project_id) WHERE state != 'COMPLETED';
CREATE TABLE IF NOT EXISTS reproductions (
    reproduction_id TEXT PRIMARY KEY,
    project_id TEXT,
    original_run_id TEXT NOT NULL,
    reproduction_run_id TEXT,
    bundle_sha256 TEXT,
    state TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    supersedes_operation_id TEXT REFERENCES operations(operation_id),
    result_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    state TEXT NOT NULL CHECK (state IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'INTERRUPTED')),
    accepted_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    request_json TEXT NOT NULL,
    output_path TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS operations_by_project ON operations(project_id, accepted_at);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    experiment_id TEXT,
    run_id TEXT,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    total INTEGER NOT NULL,
    measured INTEGER NOT NULL,
    engine_row_failures INTEGER NOT NULL,
    integrity_failures INTEGER NOT NULL,
    not_assessed INTEGER NOT NULL,
    cancellation_requested INTEGER NOT NULL,
    parquet_path TEXT,
    parquet_sha256 TEXT,
    resource_use_json TEXT NOT NULL,
    selection_rule TEXT NOT NULL,
    engine_version TEXT
);
CREATE TABLE IF NOT EXISTS owner_supplied_inputs (
    path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    project_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    original_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    storage TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    admitted_at TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS owner_supplied_project_refs (
    path TEXT NOT NULL REFERENCES owner_supplied_inputs(path) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (path, project_id)
);
CREATE TABLE IF NOT EXISTS import_mappings (
    mapping_name TEXT PRIMARY KEY,
    mapping_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class ImmutableRecordConflict(RuntimeError):
    """Raised when a write would alter an already recorded scientific record."""


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    project_id: str
    experiment_id: str
    sample_id: str
    status: str
    engine_resource_id: str
    engine_version: str
    start_time: str
    end_time: str
    evidence_sha256: str


def _evidence(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _migration_snapshot(database_path: Path) -> Path | None:
    if not database_path.exists() or database_path.stat().st_size == 0:
        return None
    source = sqlite3.connect(f"file:{database_path.absolute()}?mode=ro", uri=True)
    try:
        integrity = source.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise sqlite3.DatabaseError(
                f"database integrity check failed before schema migration: {integrity!r}"
            )
        current = int(source.execute("PRAGMA user_version").fetchone()[0])
        target = SCHEMA_MIGRATIONS[-1][0]
        if current > target:
            raise RuntimeError(
                f"database schema version {current} is newer than supported version {target}"
            )
        if current == target:
            return None
        destination = database_path.with_name(
            f"{database_path.name}.pre-migration-v{current}-to-v{target}.snapshot.sqlite3"
        )
        source_identity = hashlib.sha256(source.serialize()).hexdigest()
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_file():
                raise FileExistsError(
                    f"pre-migration snapshot path is not a reusable regular file: {destination}"
                )
            recorded = sqlite3.connect(f"file:{destination.absolute()}?mode=ro", uri=True)
            try:
                if recorded.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise sqlite3.DatabaseError(
                        "existing pre-migration snapshot integrity check failed"
                    )
                recorded_version = int(
                    recorded.execute("PRAGMA user_version").fetchone()[0]
                )
                recorded_identity = hashlib.sha256(recorded.serialize()).hexdigest()
            finally:
                recorded.close()
            if recorded_version != current:
                raise RuntimeError(
                    "existing pre-migration snapshot schema version does not match the live database"
                )
            if recorded_identity == source_identity:
                return destination
            destination = database_path.with_name(
                f"{database_path.name}.pre-migration-v{current}-to-v{target}."
                f"source-{source_identity}.snapshot.sqlite3"
            )
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_file():
                    raise FileExistsError(
                        f"content-addressed pre-migration snapshot path is unsafe: {destination}"
                    )
                recorded = sqlite3.connect(
                    f"file:{destination.absolute()}?mode=ro", uri=True
                )
                try:
                    if recorded.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                        raise sqlite3.DatabaseError(
                            "content-addressed pre-migration snapshot integrity check failed"
                        )
                    recorded_identity = hashlib.sha256(recorded.serialize()).hexdigest()
                finally:
                    recorded.close()
                if recorded_identity == source_identity:
                    return destination
                raise FileExistsError(
                    f"content-addressed pre-migration snapshot differs from its identity: {destination}"
                )
        descriptor, partial_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
        )
        os.close(descriptor)
        partial = Path(partial_name)
        snapshot: sqlite3.Connection | None = None
        try:
            snapshot = sqlite3.connect(str(partial), isolation_level=None)
            source.backup(snapshot)
            if snapshot.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise sqlite3.DatabaseError("pre-migration snapshot integrity check failed")
            snapshot.close()
            snapshot = None
            os.chmod(partial, 0o600)
            os.link(partial, destination)
        finally:
            if snapshot is not None:
                snapshot.close()
            partial.unlink(missing_ok=True)
        return destination
    finally:
        source.close()


def _restore_migration_snapshot(database_path: Path, snapshot: Path) -> None:
    descriptor, partial_name = tempfile.mkstemp(
        prefix=f".{database_path.name}.", suffix=".rollback", dir=database_path.parent
    )
    os.close(descriptor)
    partial = Path(partial_name)
    try:
        shutil.copyfile(snapshot, partial)
        os.chmod(partial, 0o600)
        check = sqlite3.connect(f"file:{partial}?mode=ro", uri=True)
        try:
            if check.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise sqlite3.DatabaseError("migration rollback snapshot integrity check failed")
        finally:
            check.close()
        Path(f"{database_path}-wal").unlink(missing_ok=True)
        Path(f"{database_path}-shm").unlink(missing_ok=True)
        os.replace(partial, database_path)
    finally:
        partial.unlink(missing_ok=True)


class RunStore:
    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path
        migration_snapshot = _migration_snapshot(database_path)
        self._lock = threading.Lock()
        # One locked writer connection; every reader thread gets its own
        # connection (QS-S97). Reads on the writer's connection would observe
        # a writer's uncommitted BEGIN..COMMIT window (same-connection reads
        # bypass isolation); WAL gives per-connection snapshot isolation instead.
        self._connection = sqlite3.connect(str(database_path), check_same_thread=False, isolation_level=None)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=30000")
        try:
            self._connection.executescript(SCHEMA)
        except BaseException:
            self._connection.close()
            if migration_snapshot is not None:
                _restore_migration_snapshot(database_path, migration_snapshot)
            raise
        recorded_versions = {
            int(row[0]): str(row[1])
            for row in self._connection.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            ).fetchall()
        }
        supported_versions = {version: name for version, name in SCHEMA_MIGRATIONS}
        unknown = set(recorded_versions) - set(supported_versions)
        mismatched = {
            version
            for version, name in recorded_versions.items()
            if supported_versions.get(version) != name
        }
        if unknown or mismatched:
            self._connection.close()
            if migration_snapshot is not None:
                _restore_migration_snapshot(database_path, migration_snapshot)
            raise RuntimeError(
                f"database schema migration history is unsupported: unknown={sorted(unknown)}, mismatched={sorted(mismatched)}"
            )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "INSERT OR IGNORE INTO owner_supplied_project_refs (path, project_id, record_json) "
                "SELECT path, project_id, record_json FROM owner_supplied_inputs"
            )
            report_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(reports)").fetchall()}
            if "json_sha256" not in report_columns:
                self._connection.execute("ALTER TABLE reports ADD COLUMN json_sha256 TEXT")
            if "provenance_sha256" not in report_columns:
                self._connection.execute("ALTER TABLE reports ADD COLUMN provenance_sha256 TEXT")
            matcher_leaf_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(matcher_leaves)").fetchall()}
            if "expected_leaf_count" not in matcher_leaf_columns:
                self._connection.execute("ALTER TABLE matcher_leaves ADD COLUMN expected_leaf_count INTEGER NOT NULL DEFAULT 6")
                self._connection.execute(
                    "UPDATE matcher_leaves SET expected_leaf_count = 1 "
                    "WHERE leaf_id = 'QS-C21.EXTERNAL' OR condition_id = 'EXTERNALLY_COMPUTED'"
                )
            protocol_pair_columns = {
                str(row[1]): int(row[3])
                for row in self._connection.execute("PRAGMA table_info(protocol_pairs)").fetchall()
            }
            if protocol_pair_columns.get("fold") == 1:
                self._connection.execute(
                    "CREATE TABLE protocol_pairs_v4 ("
                    "protocol_id TEXT NOT NULL REFERENCES protocols(protocol_id), "
                    "pair_id TEXT NOT NULL, fold INTEGER, source_order INTEGER NOT NULL, "
                    "label TEXT NOT NULL, left_sample_id TEXT NOT NULL, right_sample_id TEXT NOT NULL, "
                    "record_json TEXT NOT NULL, PRIMARY KEY (protocol_id, pair_id))"
                )
                self._connection.execute(
                    "INSERT INTO protocol_pairs_v4 "
                    "(protocol_id, pair_id, fold, source_order, label, left_sample_id, right_sample_id, record_json) "
                    "SELECT protocol_id, pair_id, fold, source_order, label, left_sample_id, right_sample_id, record_json "
                    "FROM protocol_pairs"
                )
                self._connection.execute("DROP TABLE protocol_pairs")
                self._connection.execute("ALTER TABLE protocol_pairs_v4 RENAME TO protocol_pairs")
                self._connection.execute(
                    "CREATE INDEX protocol_pairs_by_fold ON protocol_pairs(protocol_id, fold, source_order)"
                )
            operation_columns = {
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(operations)").fetchall()
            }
            if "request_id" not in operation_columns:
                self._connection.execute("ALTER TABLE operations ADD COLUMN request_id TEXT")
            if "attempt" not in operation_columns:
                self._connection.execute(
                    "ALTER TABLE operations ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1"
                )
            if "supersedes_operation_id" not in operation_columns:
                self._connection.execute(
                    "ALTER TABLE operations ADD COLUMN supersedes_operation_id TEXT REFERENCES operations(operation_id)"
                )
            if "result_id" not in operation_columns:
                self._connection.execute("ALTER TABLE operations ADD COLUMN result_id TEXT")
            self._connection.execute(
                "UPDATE operations SET request_id = operation_id WHERE request_id IS NULL"
            )
            self._connection.execute(
                "UPDATE operations SET result_id = operation_id WHERE result_id IS NULL"
            )
            if self._connection.execute(
                "SELECT 1 FROM operations "
                "WHERE request_id IS NULL OR result_id IS NULL OR attempt < 1 LIMIT 1"
            ).fetchone() is not None:
                raise RuntimeError(
                    "operation request, attempt, and result identity migration is incomplete"
                )
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS operations_by_request_attempt "
                "ON operations(request_id, attempt)"
            )
            applied_at = datetime.now(timezone.utc).astimezone().isoformat()
            for version, name in SCHEMA_MIGRATIONS:
                self._connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, applied_at),
                )
            self._connection.execute(f"PRAGMA user_version={SCHEMA_MIGRATIONS[-1][0]}")
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            self._connection.close()
            if migration_snapshot is not None:
                _restore_migration_snapshot(database_path, migration_snapshot)
            raise
        self._readers = threading.local()

    def _read(self) -> sqlite3.Connection:
        connection = getattr(self._readers, "connection", None)
        if connection is None:
            connection = sqlite3.connect(str(self.database_path), check_same_thread=False, isolation_level=None)
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA query_only=ON")
            self._readers.connection = connection
        return connection

    def close(self) -> None:
        # Reader connections are per-thread and close with their threads.
        self._connection.close()

    def snapshot_database(self, destination: Path) -> dict[str, object]:
        """Write one consistent, private, no-clobber snapshot for out-of-process inspection."""

        destination = destination.absolute()
        if not destination.name.endswith(".snapshot.sqlite3"):
            raise ValueError("database snapshot destination must end with .snapshot.sqlite3")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, partial_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
        )
        os.close(descriptor)
        partial = Path(partial_name)
        snapshot: sqlite3.Connection | None = None
        try:
            snapshot = sqlite3.connect(str(partial), isolation_level=None)
            with self._lock:
                self._connection.backup(snapshot)
            integrity = snapshot.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)]:
                raise sqlite3.DatabaseError(f"snapshot integrity check failed: {integrity!r}")
            snapshot.close()
            snapshot = None
            os.chmod(partial, 0o600)
            os.link(partial, destination)
        finally:
            if snapshot is not None:
                snapshot.close()
            partial.unlink(missing_ok=True)
        return {
            "path": str(destination),
            "size_bytes": destination.stat().st_size,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        }

    def operational_status(self) -> dict[str, Any]:
        """Return facts required by the API readiness gate from a real database read."""

        connection = self._read()
        query_result = connection.execute("SELECT 1").fetchone()
        if query_result is None or int(query_result[0]) != 1:
            raise sqlite3.DatabaseError("SQLite readiness query returned an invalid result")
        operation_states = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT state, COUNT(*) FROM operations GROUP BY state ORDER BY state"
            ).fetchall()
        }
        unresolved_purges = int(
            connection.execute(
                "SELECT COUNT(*) FROM purge_journal WHERE state NOT IN ('COMPLETED', 'FAILED')"
            ).fetchone()[0]
        )
        return {
            "query": "ok",
            "schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
            "operation_states": operation_states,
            "unresolved_purges": unresolved_purges,
        }

    @staticmethod
    def _row(cursor: sqlite3.Cursor, row: tuple[Any, ...] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {description[0]: value for description, value in zip(cursor.description, row)}

    # -- writes ----------------------------------------------------------------

    def admit_operation(
        self,
        *,
        operation_id: str,
        operation_kind: str,
        project_id: str,
        request: dict[str, Any],
        output_path: Path | None,
        retry_interrupted: bool = False,
        result_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Durably admit one immutable request, with explicit interrupted retries."""

        if not operation_id or not operation_kind or not project_id:
            raise ValueError("operation id, kind, and project id are required")
        request_json = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        output_value = str(output_path) if output_path is not None else None
        result_value = result_id or operation_id
        accepted_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cursor = self._connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            )
            existing = self._row(cursor, cursor.fetchone())
            if existing is not None:
                immutable = {
                    "operation_kind": operation_kind,
                    "project_id": project_id,
                    "request_json": request_json,
                }
                if not retry_interrupted:
                    immutable["output_path"] = output_value
                    immutable["result_id"] = result_value
                changed = [key for key, value in immutable.items() if existing[key] != value]
                if changed:
                    raise ImmutableRecordConflict(
                        f"operation {operation_id} immutable identity differs in {', '.join(changed)}"
                    )
                request_id = str(existing["request_id"])
                cursor = self._connection.execute(
                    "SELECT * FROM operations WHERE request_id = ? ORDER BY attempt DESC LIMIT 1",
                    (request_id,),
                )
                latest = self._row(cursor, cursor.fetchone())
                assert latest is not None
                if not retry_interrupted or latest["state"] != "INTERRUPTED":
                    return latest, False
                attempt = int(latest["attempt"]) + 1
                attempt_id = f"operation-attempt:sha256:{_evidence(f'{request_id}:{attempt}')}"
                self._connection.execute(
                    "INSERT INTO operations "
                    "(operation_id, request_id, attempt, supersedes_operation_id, result_id, operation_kind, "
                    "project_id, state, accepted_at, request_json, output_path) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?)",
                    (
                        attempt_id,
                        request_id,
                        attempt,
                        latest["operation_id"],
                        result_value,
                        operation_kind,
                        project_id,
                        accepted_at,
                        request_json,
                        output_value,
                    ),
                )
                cursor = self._connection.execute(
                    "SELECT * FROM operations WHERE operation_id = ?",
                    (attempt_id,),
                )
                admitted = self._row(cursor, cursor.fetchone())
                assert admitted is not None
                return admitted, True
            self._connection.execute(
                "INSERT INTO operations "
                "(operation_id, request_id, attempt, result_id, operation_kind, project_id, state, accepted_at, request_json, output_path) "
                "VALUES (?, ?, 1, ?, ?, ?, 'QUEUED', ?, ?, ?)",
                (
                    operation_id,
                    operation_id,
                    result_value,
                    operation_kind,
                    project_id,
                    accepted_at,
                    request_json,
                    output_value,
                ),
            )
            cursor = self._connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            )
            admitted = self._row(cursor, cursor.fetchone())
            assert admitted is not None
            return admitted, True

    def start_operation(self, operation_id: str) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE operations SET state = 'RUNNING', started_at = ? "
                "WHERE operation_id = ? AND state = 'QUEUED'",
                (started_at, operation_id),
            )
            if cursor.rowcount != 1:
                current = self._connection.execute(
                    "SELECT state FROM operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if current is None:
                    raise KeyError(f"operation is not recorded: {operation_id}")
                raise ImmutableRecordConflict(
                    f"operation {operation_id} cannot start from state {current[0]}"
                )
            cursor = self._connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            )
            started = self._row(cursor, cursor.fetchone())
            assert started is not None
            return started

    def finish_operation(self, operation_id: str, *, state: str, error: str | None = None) -> dict[str, Any]:
        if state not in {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            raise ValueError(f"invalid terminal operation state: {state}")
        if state == "COMPLETED" and error is not None:
            raise ValueError("a completed operation cannot carry an error")
        if state in {"FAILED", "INTERRUPTED"} and not error:
            raise ValueError(f"{state} operation requires a factual error")
        ended_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE operations SET state = ?, ended_at = ?, error = ? "
                "WHERE operation_id = ? AND state IN ('QUEUED', 'RUNNING')",
                (state, ended_at, error, operation_id),
            )
            if cursor.rowcount != 1:
                current = self._connection.execute(
                    "SELECT state FROM operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if current is None:
                    raise KeyError(f"operation is not recorded: {operation_id}")
                raise ImmutableRecordConflict(
                    f"operation {operation_id} cannot finish from state {current[0]}"
                )
            cursor = self._connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            )
            finished = self._row(cursor, cursor.fetchone())
            assert finished is not None
            return finished

    def reconcile_interrupted_operations(self) -> list[dict[str, Any]]:
        """Mark work abandoned by the previous process without attempting scientific replay."""

        ended_at = datetime.now(timezone.utc).isoformat()
        interrupted_error = "process ended before a terminal state was recorded; execution was not resumed automatically"
        with self._lock:
            cursor = self._connection.execute(
                "SELECT * FROM operations WHERE state IN ('QUEUED', 'RUNNING') ORDER BY accepted_at, operation_id"
            )
            pending = [self._row(cursor, row) for row in cursor.fetchall()]
            if not pending:
                return []
            for operation in pending:
                assert operation is not None
                operation_id = operation["operation_id"]
                result_id = operation["result_id"]
                operation_kind = operation["operation_kind"]
                terminal_state: str | None = None
                terminal_error: str | None = None
                if operation_kind == "QUALITY_BATCH":
                    recorded = self._connection.execute(
                        "SELECT state FROM jobs WHERE job_id = ?",
                        (result_id,),
                    ).fetchone()
                    if recorded is not None and recorded[0] in {"COMPLETED", "FAILED", "CANCELLED"}:
                        terminal_state = recorded[0]
                        if terminal_state == "FAILED":
                            terminal_error = "quality batch persisted a FAILED job before process termination"
                elif operation_kind == "MATCHER_MATRIX":
                    expected = int(json.loads(operation["request_json"])["expected_leaf_count"])
                    count = self._connection.execute(
                        "SELECT COUNT(*) FROM matcher_leaves WHERE matrix_id = ?",
                        (result_id,),
                    ).fetchone()[0]
                    if int(count) == expected:
                        terminal_state = "COMPLETED"
                else:
                    result_table_and_key = {
                        "UTILITY_ANALYSIS": ("utility_analyses", "analysis_id"),
                        "CROSS_MATCHER_ANALYSIS": ("cross_matcher_analyses", "analysis_id"),
                        "DEGRADATION_ANALYSIS": ("degradation_analyses", "analysis_id"),
                        "IDENTIFICATION_SEARCH": ("identification_searches", "search_id"),
                    }.get(operation_kind)
                    if result_table_and_key is not None:
                        table, key = result_table_and_key
                        recorded = self._connection.execute(
                            f"SELECT 1 FROM {table} WHERE {key} = ?",
                            (result_id,),
                        ).fetchone()
                        if recorded is not None:
                            terminal_state = "COMPLETED"
                if terminal_state is None:
                    terminal_state = "INTERRUPTED"
                    terminal_error = interrupted_error
                self._connection.execute(
                    "UPDATE operations SET state = ?, ended_at = ?, error = ? WHERE operation_id = ? AND state IN ('QUEUED', 'RUNNING')",
                    (terminal_state, ended_at, terminal_error, operation_id),
                )
            identifiers = [row["operation_id"] for row in pending if row is not None]
            placeholders = ",".join("?" for _ in identifiers)
            cursor = self._connection.execute(
                f"SELECT * FROM operations WHERE operation_id IN ({placeholders}) ORDER BY accepted_at, operation_id",
                identifiers,
            )
            reconciled = [self._row(cursor, row) for row in cursor.fetchall()]
            return [row for row in reconciled if row is not None]

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        cursor = self._read().execute(
            "SELECT * FROM operations WHERE operation_id = ?",
            (operation_id,),
        )
        return self._row(cursor, cursor.fetchone())

    def latest_operation_attempt(self, request_id: str) -> dict[str, Any] | None:
        cursor = self._read().execute(
            "SELECT * FROM operations WHERE request_id = ? ORDER BY attempt DESC LIMIT 1",
            (request_id,),
        )
        return self._row(cursor, cursor.fetchone())

    def latest_operation_for_result(self, result_id: str) -> dict[str, Any] | None:
        cursor = self._read().execute(
            "SELECT * FROM operations WHERE result_id = ? "
            "ORDER BY accepted_at DESC, attempt DESC LIMIT 1",
            (result_id,),
        )
        return self._row(cursor, cursor.fetchone())

    def operations_for_project(self, project_id: str) -> list[dict[str, Any]]:
        cursor = self._read().execute(
            "SELECT * FROM operations WHERE project_id = ? ORDER BY accepted_at DESC, operation_id",
            (project_id,),
        )
        operations = [self._row(cursor, row) for row in cursor.fetchall()]
        return [row for row in operations if row is not None]

    def _insert_once(
        self,
        table: str,
        key_column: str,
        key: str,
        columns: dict[str, str],
        *,
        byte_exact: bool,
    ) -> None:
        """Insert a record keyed by a content-addressed ID.

        Context entities (project, dataset, sample, experiment) are created the
        first time their ID appears and are never rewritten afterwards: their
        IDs are derived from the registry, resource, configuration and input
        identities, so a later bundle naming the same ID describes the same
        entity and only its creation timestamps differ. Scientific Runs and
        audit events are byte-exact: the same ID with different bytes is a
        conflict.
        """

        existing = self._connection.execute(
            f"SELECT record_json FROM {table} WHERE {key_column} = ?", (key,)
        ).fetchone()
        if existing is not None:
            if byte_exact and existing[0] != columns["record_json"]:
                raise ImmutableRecordConflict(f"{table}.{key} is already recorded with different bytes")
            return
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        self._connection.execute(
            f"INSERT INTO {table} ({names}) VALUES ({placeholders})", tuple(columns.values())
        )

    def _insert_audit_event(self, event: AuditEvent) -> None:
        existing = self._connection.execute(
            "SELECT record_json, evidence_sha256 FROM audit_events WHERE audit_event_id = ?",
            (event.audit_event_id,),
        ).fetchone()
        if existing is not None:
            record_json, evidence_sha256 = existing
            if _evidence(record_json) != evidence_sha256:
                raise ImmutableRecordConflict(
                    f"stored audit event {event.audit_event_id} does not match its evidence digest"
                )
            recorded = AuditEvent.model_validate_json(record_json)
            expected = event.model_copy(
                update={"previous_evidence_sha256": recorded.previous_evidence_sha256}
            )
            if recorded != expected:
                raise ImmutableRecordConflict(
                    f"audit_events.{event.audit_event_id} is already recorded with different bytes"
                )
            return

        prior = self._connection.execute(
            "SELECT evidence_sha256 FROM audit_events WHERE project_id = ? ORDER BY rowid DESC LIMIT 1",
            (event.project_id,),
        ).fetchone()
        previous_evidence = str(prior[0]) if prior is not None else None
        if (
            event.previous_evidence_sha256 is not None
            and event.previous_evidence_sha256 != previous_evidence
        ):
            raise ImmutableRecordConflict(
                f"audit event {event.audit_event_id} does not link to the current project chain head"
            )
        chained = AuditEvent.model_validate(
            {
                **event.model_dump(mode="python"),
                "previous_evidence_sha256": previous_evidence,
            }
        )
        record_json = chained.model_dump_json()
        self._insert_once(
            "audit_events",
            "audit_event_id",
            chained.audit_event_id,
            byte_exact=True,
            columns={
                "audit_event_id": chained.audit_event_id,
                "project_id": chained.project_id,
                "subject_type": chained.subject_type,
                "subject_id": chained.subject_id,
                "occurred_at": chained.occurred_at.isoformat(),
                "evidence_sha256": _evidence(record_json),
                "record_json": record_json,
            },
        )

    def record_bundle(self, bundle: SingleSampleRunBundle) -> str:
        """Persist a Run with its context entities; returns the Run evidence SHA-256."""

        run_json = bundle.run.model_dump_json()
        run_sha = _evidence(run_json)
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                project_json = bundle.project.model_dump_json()
                self._insert_once(
                    "projects",
                    "project_id",
                    bundle.project.project_id,
                    byte_exact=False,
                    columns={
                        "project_id": bundle.project.project_id,
                        "created_at": bundle.project.created_at.isoformat(),
                        "evidence_sha256": _evidence(project_json),
                        "record_json": project_json,
                    },
                )
                dataset_json = bundle.dataset.model_dump_json()
                self._insert_once(
                    "datasets",
                    "dataset_id",
                    bundle.dataset.dataset_id,
                    byte_exact=False,
                    columns={
                        "dataset_id": bundle.dataset.dataset_id,
                        "project_id": bundle.dataset.project_id,
                        "evidence_sha256": _evidence(dataset_json),
                        "record_json": dataset_json,
                    },
                )
                sample_json = bundle.sample.model_dump_json()
                self._insert_once(
                    "samples",
                    "sample_id",
                    bundle.sample.sample_id,
                    byte_exact=False,
                    columns={
                        "sample_id": bundle.sample.sample_id,
                        "dataset_id": bundle.sample.dataset_id,
                        "source_path": bundle.sample.source_path,
                        "sha256": bundle.sample.sha256,
                        "evidence_sha256": _evidence(sample_json),
                        "record_json": sample_json,
                    },
                )
                experiment_json = bundle.experiment.model_dump_json()
                self._insert_once(
                    "experiments",
                    "experiment_id",
                    bundle.experiment.experiment_id,
                    byte_exact=False,
                    columns={
                        "experiment_id": bundle.experiment.experiment_id,
                        "project_id": bundle.experiment.project_id,
                        "dataset_id": bundle.experiment.dataset_id,
                        "evidence_sha256": _evidence(experiment_json),
                        "record_json": experiment_json,
                    },
                )
                self._insert_once(
                    "runs",
                    "run_id",
                    bundle.run.run_id,
                    byte_exact=True,
                    columns={
                        "run_id": bundle.run.run_id,
                        "project_id": bundle.run.project_id,
                        "experiment_id": bundle.run.experiment_id,
                        "sample_id": bundle.sample.sample_id,
                        "status": bundle.run.status.value,
                        "engine_resource_id": bundle.run.quality_engine.resource_id,
                        "engine_version": bundle.run.quality_engine.version,
                        "start_time": bundle.run.start_time.isoformat(),
                        "end_time": bundle.run.end_time.isoformat(),
                        "evidence_sha256": run_sha,
                        "record_json": run_json,
                    },
                )
                self._insert_audit_event(bundle.audit_event)
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")
        return run_sha

    def record_project_version(self, project: Project, *, first: bool) -> int:
        """Append a project version; version 1 also creates the projects row."""

        record_json = project.model_dump_json()
        evidence = _evidence(record_json)
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                if first:
                    self._insert_once(
                        "projects",
                        "project_id",
                        project.project_id,
                        byte_exact=True,
                        columns={
                            "project_id": project.project_id,
                            "created_at": project.created_at.isoformat(),
                            "evidence_sha256": evidence,
                            "record_json": record_json,
                        },
                    )
                    version = 1
                else:
                    row = self._connection.execute(
                        "SELECT MAX(version) FROM project_versions WHERE project_id = ?", (project.project_id,)
                    ).fetchone()
                    if row is None or row[0] is None:
                        raise ImmutableRecordConflict(f"project {project.project_id} has no version to extend")
                    version = int(row[0]) + 1
                self._connection.execute(
                    "INSERT INTO project_versions (project_id, version, lifecycle, name, updated_at, evidence_sha256, record_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        project.project_id,
                        version,
                        project.lifecycle.value,
                        project.name,
                        project.updated_at.isoformat(),
                        evidence,
                        record_json,
                    ),
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")
        return version

    def record_audit_event(self, event: AuditEvent) -> None:
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                self._insert_audit_event(event)
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def record_system_event(self, record: dict[str, Any]) -> str:
        record_json = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
        evidence = _evidence(record_json)
        with self._lock:
            self._insert_once(
                "system_events",
                "system_event_id",
                record["system_event_id"],
                byte_exact=True,
                columns={
                    "system_event_id": record["system_event_id"],
                    "actor_id": record["actor_id"],
                    "event_type": record["event_type"],
                    "subject_id": record["subject_id"],
                    "occurred_at": record["occurred_at"],
                    "evidence_sha256": evidence,
                    "record_json": record_json,
                },
            )
            self._connection.commit()
        return evidence

    def system_events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._read().execute(
            "SELECT record_json, evidence_sha256 FROM system_events ORDER BY occurred_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        records: list[dict[str, Any]] = []
        for record_json, evidence in rows:
            if _evidence(record_json) != evidence:
                raise ImmutableRecordConflict("stored system event does not match its evidence digest")
            record = json.loads(record_json)
            record["evidence_sha256"] = evidence
            records.append(record)
        return records

    def purge_project_records(self, project_id: str) -> dict[str, Any]:
        """Remove application-managed project data while retaining the project/audit tombstone."""

        with self._lock:
            connection = self._connection
            dataset_ids = [row[0] for row in connection.execute("SELECT dataset_id FROM datasets WHERE project_id = ?", (project_id,)).fetchall()]
            run_ids = [row[0] for row in connection.execute("SELECT run_id FROM runs WHERE project_id = ?", (project_id,)).fetchall()]
            job_ids = [row[0] for row in connection.execute("SELECT job_id FROM jobs WHERE project_id = ?", (project_id,)).fetchall()]
            removed: dict[str, int] = {}

            def delete(name: str, statement: str, parameters: tuple[Any, ...] = ()) -> None:
                before = connection.total_changes
                connection.execute(statement, parameters)
                removed[name] = connection.total_changes - before

            connection.execute("BEGIN")
            try:
                delete("reproductions", "DELETE FROM reproductions WHERE project_id = ?", (project_id,))
                delete("bundles", "DELETE FROM bundles WHERE project_id = ?", (project_id,))
                delete("degradation_analyses", "DELETE FROM degradation_analyses WHERE project_id = ?", (project_id,))
                delete("derived_samples", "DELETE FROM derived_samples WHERE derived_set_id IN (SELECT derived_set_id FROM derived_sets WHERE project_id = ?)", (project_id,))
                delete("derived_sets", "DELETE FROM derived_sets WHERE project_id = ?", (project_id,))
                delete("identification_searches", "DELETE FROM identification_searches WHERE project_id = ?", (project_id,))
                delete("identification_entries", "DELETE FROM identification_entries WHERE protocol_id IN (SELECT protocol_id FROM identification_protocols WHERE project_id = ?)", (project_id,))
                delete("identification_protocols", "DELETE FROM identification_protocols WHERE project_id = ?", (project_id,))
                for table in ("reports", "dashboards", "figures", "annotations", "cross_matcher_analyses", "utility_analyses", "matcher_leaves"):
                    delete(table, f"DELETE FROM {table} WHERE project_id = ?", (project_id,))
                delete("protocol_pairs", "DELETE FROM protocol_pairs WHERE protocol_id IN (SELECT protocol_id FROM protocols WHERE project_id = ?)", (project_id,))
                delete("protocols", "DELETE FROM protocols WHERE project_id = ?", (project_id,))
                delete("quality_results", "DELETE FROM quality_results WHERE job_id IN (SELECT job_id FROM jobs WHERE project_id = ?)", (project_id,))
                delete("jobs", "DELETE FROM jobs WHERE project_id = ?", (project_id,))
                delete("operations", "DELETE FROM operations WHERE project_id = ?", (project_id,))
                delete("artifacts", "DELETE FROM artifacts WHERE run_id IN (SELECT run_id FROM runs WHERE project_id = ?)", (project_id,))
                delete("runs", "DELETE FROM runs WHERE project_id = ?", (project_id,))
                delete("experiments", "DELETE FROM experiments WHERE project_id = ?", (project_id,))
                delete("dataset_statistics", "DELETE FROM dataset_statistics WHERE dataset_id IN (SELECT dataset_id FROM datasets WHERE project_id = ?)", (project_id,))
                delete("samples", "DELETE FROM samples WHERE dataset_id IN (SELECT dataset_id FROM datasets WHERE project_id = ?)", (project_id,))
                delete("datasets", "DELETE FROM datasets WHERE project_id = ?", (project_id,))
                delete("owner_supplied_project_refs", "DELETE FROM owner_supplied_project_refs WHERE project_id = ?", (project_id,))
                delete(
                    "owner_supplied_inputs",
                    "DELETE FROM owner_supplied_inputs WHERE NOT EXISTS "
                    "(SELECT 1 FROM owner_supplied_project_refs AS refs WHERE refs.path = owner_supplied_inputs.path)",
                )
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

            remaining = {
                "datasets": connection.execute("SELECT COUNT(*) FROM datasets WHERE project_id = ?", (project_id,)).fetchone()[0],
                "experiments": connection.execute("SELECT COUNT(*) FROM experiments WHERE project_id = ?", (project_id,)).fetchone()[0],
                "runs": connection.execute("SELECT COUNT(*) FROM runs WHERE project_id = ?", (project_id,)).fetchone()[0],
                "jobs": connection.execute("SELECT COUNT(*) FROM jobs WHERE project_id = ?", (project_id,)).fetchone()[0],
                "operations": connection.execute("SELECT COUNT(*) FROM operations WHERE project_id = ?", (project_id,)).fetchone()[0],
                "protocols": connection.execute("SELECT COUNT(*) FROM protocols WHERE project_id = ?", (project_id,)).fetchone()[0],
                "matcher_leaves": connection.execute("SELECT COUNT(*) FROM matcher_leaves WHERE project_id = ?", (project_id,)).fetchone()[0],
                "utility_analyses": connection.execute("SELECT COUNT(*) FROM utility_analyses WHERE project_id = ?", (project_id,)).fetchone()[0],
                "cross_matcher_analyses": connection.execute("SELECT COUNT(*) FROM cross_matcher_analyses WHERE project_id = ?", (project_id,)).fetchone()[0],
                "annotations": connection.execute("SELECT COUNT(*) FROM annotations WHERE project_id = ?", (project_id,)).fetchone()[0],
                "figures": connection.execute("SELECT COUNT(*) FROM figures WHERE project_id = ?", (project_id,)).fetchone()[0],
                "dashboards": connection.execute("SELECT COUNT(*) FROM dashboards WHERE project_id = ?", (project_id,)).fetchone()[0],
                "reports": connection.execute("SELECT COUNT(*) FROM reports WHERE project_id = ?", (project_id,)).fetchone()[0],
                "identification_protocols": connection.execute("SELECT COUNT(*) FROM identification_protocols WHERE project_id = ?", (project_id,)).fetchone()[0],
                "identification_searches": connection.execute("SELECT COUNT(*) FROM identification_searches WHERE project_id = ?", (project_id,)).fetchone()[0],
                "derived_sets": connection.execute("SELECT COUNT(*) FROM derived_sets WHERE project_id = ?", (project_id,)).fetchone()[0],
                "degradation_analyses": connection.execute("SELECT COUNT(*) FROM degradation_analyses WHERE project_id = ?", (project_id,)).fetchone()[0],
                "owner_supplied_inputs": connection.execute("SELECT COUNT(*) FROM owner_supplied_project_refs WHERE project_id = ?", (project_id,)).fetchone()[0],
                "bundles": connection.execute("SELECT COUNT(*) FROM bundles WHERE project_id = ?", (project_id,)).fetchone()[0],
                "reproductions": connection.execute("SELECT COUNT(*) FROM reproductions WHERE project_id = ?", (project_id,)).fetchone()[0],
            }
            if dataset_ids:
                markers = ",".join("?" for _ in dataset_ids)
                remaining["samples"] = connection.execute(f"SELECT COUNT(*) FROM samples WHERE dataset_id IN ({markers})", dataset_ids).fetchone()[0]
                remaining["quality_results_by_dataset"] = connection.execute(f"SELECT COUNT(*) FROM quality_results WHERE dataset_id IN ({markers})", dataset_ids).fetchone()[0]
            if run_ids:
                markers = ",".join("?" for _ in run_ids)
                remaining["artifacts"] = connection.execute(f"SELECT COUNT(*) FROM artifacts WHERE run_id IN ({markers})", run_ids).fetchone()[0]
            if job_ids:
                markers = ",".join("?" for _ in job_ids)
                remaining["quality_results_by_job"] = connection.execute(f"SELECT COUNT(*) FROM quality_results WHERE job_id IN ({markers})", job_ids).fetchone()[0]
            return {"removed": removed, "remaining": remaining, "verified": not any(remaining.values())}

    def record_dataset_import(
        self,
        dataset: Dataset,
        samples: list[Sample],
        statistics_json: str,
        statistics_sha256: str,
        audit_event: AuditEvent,
    ) -> None:
        """Persist a dataset with all its samples, statistics, and audit event atomically."""

        dataset_json = dataset.model_dump_json()
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                self._insert_once(
                    "datasets",
                    "dataset_id",
                    dataset.dataset_id,
                    byte_exact=True,
                    columns={
                        "dataset_id": dataset.dataset_id,
                        "project_id": dataset.project_id,
                        "evidence_sha256": _evidence(dataset_json),
                        "record_json": dataset_json,
                    },
                )
                existing = {
                    row[0]
                    for row in self._connection.execute(
                        "SELECT sample_id FROM samples WHERE dataset_id = ?", (dataset.dataset_id,)
                    ).fetchall()
                }
                self._connection.executemany(
                    "INSERT INTO samples (sample_id, dataset_id, source_path, sha256, evidence_sha256, record_json) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            sample.sample_id,
                            sample.dataset_id,
                            sample.source_path,
                            sample.sha256,
                            _evidence(record_json),
                            record_json,
                        )
                        for sample in samples
                        if sample.sample_id not in existing
                        for record_json in (sample.model_dump_json(),)
                    ],
                )
                self._connection.execute(
                    "INSERT OR IGNORE INTO dataset_statistics (dataset_id, computed_at, evidence_sha256, record_json) VALUES (?, ?, ?, ?)",
                    (dataset.dataset_id, audit_event.occurred_at.isoformat(), statistics_sha256, statistics_json),
                )
                self._insert_audit_event(audit_event)
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def record_verification_dataset_import(
        self,
        *,
        dataset: Dataset,
        samples: list[Sample],
        statistics_json: str,
        statistics_sha256: str,
        dataset_audit_event: AuditEvent,
        protocol: Any,
        pairs: list[Any],
        protocol_audit_event: AuditEvent,
    ) -> None:
        """Atomically admit an acquired pair dataset, samples, protocol, pairs, and evidence."""

        dataset_json = dataset.model_dump_json()
        protocol_json = protocol.model_dump_json()
        if not samples or not pairs:
            raise ValueError("verification dataset imports require real samples and protocol pairs")
        if protocol.project_id != dataset.project_id or protocol.dataset_id != dataset.dataset_id:
            raise ValueError("verification protocol ownership differs from its dataset")
        if dataset.sample_count.value != len(samples) or protocol.pair_count != len(pairs):
            raise ValueError("verification dataset or protocol counts differ from the admitted records")
        if len({sample.sample_id for sample in samples}) != len(samples):
            raise ValueError("verification dataset sample IDs are not unique")
        if len({sample.source_path for sample in samples}) != len(samples):
            raise ValueError("verification dataset sample paths are not unique")
        if any(sample.dataset_id != dataset.dataset_id for sample in samples):
            raise ValueError("verification dataset contains a sample owned by another dataset")
        sample_by_id = {sample.sample_id: sample for sample in samples}
        if len({pair.pair_id for pair in pairs}) != len(pairs):
            raise ValueError("verification protocol pair IDs are not unique")
        if sorted(pair.source_order for pair in pairs) != list(range(1, len(pairs) + 1)):
            raise ValueError("verification protocol source order is not contiguous")
        if any(pair.protocol_id != protocol.protocol_id for pair in pairs):
            raise ValueError("verification protocol contains a pair owned by another protocol")
        for pair in pairs:
            left = sample_by_id.get(pair.left_sample_id)
            right = sample_by_id.get(pair.right_sample_id)
            if left is None or right is None:
                raise ValueError("verification protocol references a sample outside the admitted dataset")
            if pair.left_sha256 != left.sha256 or pair.right_sha256 != right.sha256:
                raise ValueError("verification protocol pair hashes differ from admitted samples")
        unique_pair_samples = {pair.left_sample_id for pair in pairs} | {
            pair.right_sample_id for pair in pairs
        }
        if protocol.unique_sample_count != len(unique_pair_samples):
            raise ValueError("verification protocol unique-sample count differs")
        fold_presence = {pair.fold is None for pair in pairs}
        if len(fold_presence) != 1 or ((True in fold_presence) != (protocol.fold_count is None)):
            raise ValueError("verification protocol fold authority differs from its pair records")
        if protocol.fold_count is not None:
            recorded_folds = {pair.fold for pair in pairs}
            if recorded_folds != set(range(1, protocol.fold_count + 1)):
                raise ValueError("verification protocol fold set is not contiguous")
        if statistics_sha256 != _evidence(statistics_json):
            raise ValueError("verification dataset statistics hash differs")
        if (
            dataset_audit_event.project_id != dataset.project_id
            or dataset_audit_event.subject_id != dataset.dataset_id
            or dataset_audit_event.current_evidence_sha256 != _evidence(dataset_json)
            or protocol_audit_event.project_id != dataset.project_id
            or protocol_audit_event.subject_id != protocol.protocol_id
            or protocol_audit_event.current_evidence_sha256 != _evidence(protocol_json)
        ):
            raise ValueError("verification dataset audit evidence or ownership differs")

        with self._lock:
            self._connection.execute("BEGIN")
            try:
                self._insert_once(
                    "datasets",
                    "dataset_id",
                    dataset.dataset_id,
                    byte_exact=True,
                    columns={
                        "dataset_id": dataset.dataset_id,
                        "project_id": dataset.project_id,
                        "evidence_sha256": _evidence(dataset_json),
                        "record_json": dataset_json,
                    },
                )
                for sample in samples:
                    sample_json = sample.model_dump_json()
                    self._insert_once(
                        "samples",
                        "sample_id",
                        sample.sample_id,
                        byte_exact=True,
                        columns={
                            "sample_id": sample.sample_id,
                            "dataset_id": sample.dataset_id,
                            "source_path": sample.source_path,
                            "sha256": sample.sha256,
                            "evidence_sha256": _evidence(sample_json),
                            "record_json": sample_json,
                        },
                    )
                self._insert_once(
                    "dataset_statistics",
                    "dataset_id",
                    dataset.dataset_id,
                    byte_exact=True,
                    columns={
                        "dataset_id": dataset.dataset_id,
                        "computed_at": dataset_audit_event.occurred_at.isoformat(),
                        "evidence_sha256": statistics_sha256,
                        "record_json": statistics_json,
                    },
                )
                self._insert_once(
                    "protocols",
                    "protocol_id",
                    protocol.protocol_id,
                    byte_exact=True,
                    columns={
                        "protocol_id": protocol.protocol_id,
                        "project_id": protocol.project_id,
                        "dataset_id": protocol.dataset_id,
                        "evidence_sha256": _evidence(protocol_json),
                        "record_json": protocol_json,
                    },
                )
                for pair in pairs:
                    pair_json = pair.model_dump_json()
                    existing_pair = self._connection.execute(
                        "SELECT record_json FROM protocol_pairs WHERE protocol_id = ? AND pair_id = ?",
                        (pair.protocol_id, pair.pair_id),
                    ).fetchone()
                    if existing_pair is not None:
                        if existing_pair[0] != pair_json:
                            raise ImmutableRecordConflict(
                                f"protocol_pairs.{pair.protocol_id}/{pair.pair_id} is already recorded with different bytes"
                            )
                        continue
                    self._connection.execute(
                        "INSERT INTO protocol_pairs "
                        "(protocol_id, pair_id, fold, source_order, label, left_sample_id, right_sample_id, record_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            pair.protocol_id,
                            pair.pair_id,
                            pair.fold,
                            pair.source_order,
                            pair.label.value,
                            pair.left_sample_id,
                            pair.right_sample_id,
                            pair_json,
                        ),
                    )
                for audit_event in (dataset_audit_event, protocol_audit_event):
                    self._insert_audit_event(audit_event)
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def record_batch_job(self, job: Any, experiment: Experiment, run: CompletedRun | None, artifacts: list[Artifact]) -> None:
        """Persist a batch job: experiment, optional COMPLETED Run, artifacts, job row, audit."""

        with self._lock:
            self._connection.execute("BEGIN")
            try:
                experiment_json = experiment.model_dump_json()
                self._insert_once(
                    "experiments",
                    "experiment_id",
                    experiment.experiment_id,
                    byte_exact=False,
                    columns={
                        "experiment_id": experiment.experiment_id,
                        "project_id": experiment.project_id,
                        "dataset_id": experiment.dataset_id,
                        "evidence_sha256": _evidence(experiment_json),
                        "record_json": experiment_json,
                    },
                )
                run_sha = None
                if run is not None:
                    run_json = run.model_dump_json()
                    run_sha = _evidence(run_json)
                    self._insert_once(
                        "runs",
                        "run_id",
                        run.run_id,
                        byte_exact=True,
                        columns={
                            "run_id": run.run_id,
                            "project_id": run.project_id,
                            "experiment_id": run.experiment_id,
                            "sample_id": None,
                            "run_kind": "BATCH",
                            "status": run.status.value,
                            "engine_resource_id": run.quality_engine.resource_id,
                            "engine_version": run.quality_engine.version,
                            "start_time": run.start_time.isoformat(),
                            "end_time": run.end_time.isoformat(),
                            "evidence_sha256": run_sha,
                            "record_json": run_json,
                        },
                    )
                for artifact in artifacts:
                    artifact_json = artifact.model_dump_json()
                    self._insert_once(
                        "artifacts",
                        "artifact_id",
                        artifact.artifact_id,
                        byte_exact=True,
                        columns={
                            "artifact_id": artifact.artifact_id,
                            "run_id": artifact.run_id,
                            "kind": artifact.kind.value,
                            "uri": artifact.uri,
                            "sha256": artifact.sha256,
                            "evidence_sha256": _evidence(artifact_json),
                            "record_json": artifact_json,
                        },
                    )
                self._connection.executemany(
                    "INSERT OR IGNORE INTO quality_results (job_id, sample_id, dataset_id, run_id, outcome, failure_reason, uqs_scalar, uqs_native, measures_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            job.job_id,
                            row.sample_id,
                            job.dataset.dataset_id,
                            run.run_id if run is not None else None,
                            row.outcome,
                            row.failure_reason,
                            row.measures.get("UnifiedQualityScore", (None, None, True))[1],
                            row.measures.get("UnifiedQualityScore", (None, None, True))[0],
                            json.dumps({name: {"native": v[0], "scalar": v[1], "failed": v[2]} for name, v in row.measures.items()}, sort_keys=True),
                        )
                        for row in job.results
                    ],
                )
                measured = sum(1 for row in job.results if row.outcome == "MEASURED")
                row_failures = sum(1 for row in job.results if row.outcome == "ENGINE_ROW_FAILURE")
                integrity = sum(1 for row in job.results if row.outcome == "INTEGRITY_FAILURE")
                not_assessed = sum(1 for row in job.results if row.outcome == "NOT_ASSESSED")
                state = "CANCELLED" if job._cancel_requested else ("COMPLETED" if run is not None else "FAILED")
                self._connection.execute(
                    "INSERT INTO jobs (job_id, project_id, dataset_id, experiment_id, run_id, state, created_at, started_at, ended_at, total, measured, engine_row_failures, integrity_failures, not_assessed, cancellation_requested, parquet_path, parquet_sha256, resource_use_json, selection_rule, engine_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job.job_id,
                        job.project.project_id,
                        job.dataset.dataset_id,
                        experiment.experiment_id,
                        run.run_id if run is not None else None,
                        state,
                        job.created_at.isoformat(),
                        job.started_at.isoformat() if job.started_at else None,
                        None,
                        len(job.samples),
                        measured,
                        row_failures,
                        integrity,
                        not_assessed,
                        1 if job._cancel_requested else 0,
                        str(job.parquet_path) if job.parquet_path else None,
                        artifacts[0].sha256 if artifacts else None,
                        json.dumps(job.resource_use, sort_keys=True),
                        job.selection_rule,
                        job.engine_version,
                    ),
                )
                if run is not None:
                    event = AuditEvent(
                        audit_event_id=f"audit-event:sha256:{_evidence(run.run_id + run_sha)}",
                        project_id=run.project_id,
                        actor_id=run.user_id,
                        event_type=AuditEventType.RUN_COMPLETED,
                        subject_type="Run",
                        subject_id=run.run_id,
                        occurred_at=run.end_time,
                        details=CanonicalJson.from_value({"kind": "BATCH", "job_id": job.job_id, "measured": measured, "engine_row_failures": row_failures, "integrity_failures": integrity, "not_assessed": not_assessed}),
                        current_evidence_sha256=run_sha,
                        local_only=True,
                    )
                    self._insert_audit_event(event)
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def record_matcher_leaf(self, job: Any, leaf: Any) -> None:
        run = leaf.run
        run_json = run.model_dump_json()
        run_sha = _evidence(run_json)
        metrics_text = json.dumps(leaf.metrics, sort_keys=True, separators=(",", ":"), allow_nan=False)
        experiment = Experiment(
            experiment_id=run.experiment_id,
            project_id=run.project_id,
            dataset_id=job.dataset.dataset_id,
            name=f"{leaf.leaf_id}: {leaf.matcher_id.value} × {leaf.condition_id.value}",
            created_by=run.user_id,
            created_at=run.start_time,
            protocol=VerificationProtocol(
                kind=ProtocolKind.VERIFICATION,
                protocol_id=job.protocol.protocol_id,
                pair_manifest=job.protocol.pair_manifest,
                identity_schema=ComponentBinding(state=ComponentState.CONFIGURED, component_id="lfw-directory-identity", version="1.0.0"),
            ),
            configuration=run.configuration,
        )
        experiment_json = experiment.model_dump_json()
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                self._insert_once("experiments", "experiment_id", experiment.experiment_id, byte_exact=False, columns={
                    "experiment_id": experiment.experiment_id, "project_id": experiment.project_id, "dataset_id": experiment.dataset_id,
                    "evidence_sha256": _evidence(experiment_json), "record_json": experiment_json})
                self._insert_once("runs", "run_id", run.run_id, byte_exact=True, columns={
                    "run_id": run.run_id, "project_id": run.project_id, "experiment_id": run.experiment_id, "sample_id": None,
                    "run_kind": "MATCHER", "status": run.status.value, "engine_resource_id": leaf.matcher_id.value,
                    "engine_version": run.matcher.version or "", "start_time": run.start_time.isoformat(), "end_time": run.end_time.isoformat(),
                    "evidence_sha256": run_sha, "record_json": run_json})
                for artifact in run.artifacts:
                    artifact_json = artifact.model_dump_json()
                    self._insert_once("artifacts", "artifact_id", artifact.artifact_id, byte_exact=True, columns={
                        "artifact_id": artifact.artifact_id, "run_id": artifact.run_id, "kind": artifact.kind.value, "uri": artifact.uri,
                        "sha256": artifact.sha256, "evidence_sha256": _evidence(artifact_json), "record_json": artifact_json})
                self._connection.execute(
                    "INSERT INTO matcher_leaves (run_id, matrix_id, leaf_id, project_id, dataset_id, protocol_id, matcher_id, condition_id, comparisons_parquet, comparisons_sha256, metrics_json, metrics_sha256, comparison_count, scored_count, failed_count, expected_leaf_count, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run.run_id, job.matrix_id, leaf.leaf_id, run.project_id, job.dataset.dataset_id, job.protocol.protocol_id, leaf.matcher_id.value, leaf.condition_id.value,
                     str(leaf.comparisons_parquet), hashlib.sha256(leaf.comparisons_parquet.read_bytes()).hexdigest(), metrics_text, _evidence(metrics_text),
                     leaf.comparison_count, leaf.scored_count, leaf.failed_count, int(getattr(job, "expected_leaf_count", 1)), run.end_time.isoformat()),
                )
                event = AuditEvent(
                    audit_event_id=f"audit-event:sha256:{_evidence(run.run_id + run_sha)}", project_id=run.project_id, actor_id=run.user_id,
                    event_type=AuditEventType.RUN_COMPLETED, subject_type="Run", subject_id=run.run_id, occurred_at=run.end_time,
                    details=CanonicalJson.from_value({"kind": "MATCHER", "leaf_id": leaf.leaf_id, "matrix_id": job.matrix_id, "scored": leaf.scored_count, "failed": leaf.failed_count}),
                    current_evidence_sha256=run_sha, local_only=True)
                self._insert_audit_event(event)
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def record_utility_analysis(self, result: Any) -> None:
        run = result.run
        run_json = run.model_dump_json()
        run_sha = _evidence(run_json)
        experiment_json = result.experiment.model_dump_json()
        summary_text = json.dumps(result.summary, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                self._insert_once("experiments", "experiment_id", result.experiment.experiment_id, byte_exact=False, columns={
                    "experiment_id": result.experiment.experiment_id, "project_id": result.experiment.project_id, "dataset_id": result.experiment.dataset_id,
                    "evidence_sha256": _evidence(experiment_json), "record_json": experiment_json})
                self._insert_once("runs", "run_id", run.run_id, byte_exact=True, columns={
                    "run_id": run.run_id, "project_id": run.project_id, "experiment_id": run.experiment_id, "sample_id": None,
                    "run_kind": "ANALYSIS", "status": run.status.value, "engine_resource_id": run.matcher.component_id or "",
                    "engine_version": run.matcher.version or "", "start_time": run.start_time.isoformat(), "end_time": run.end_time.isoformat(),
                    "evidence_sha256": run_sha, "record_json": run_json})
                for artifact in run.artifacts:
                    artifact_json = artifact.model_dump_json()
                    self._insert_once("artifacts", "artifact_id", artifact.artifact_id, byte_exact=True, columns={
                        "artifact_id": artifact.artifact_id, "run_id": artifact.run_id, "kind": artifact.kind.value, "uri": artifact.uri,
                        "sha256": artifact.sha256, "evidence_sha256": _evidence(artifact_json), "record_json": artifact_json})
                self._connection.execute(
                    "INSERT INTO utility_analyses (analysis_id, run_id, project_id, matcher_run_id, quality_job_id, quality_measure, pair_quality_policy, directory, utility_sha256, policy_sha256, pair_table_sha256, summary_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (result.analysis_id, run.run_id, run.project_id, result.summary["matcher_run_id"], result.summary["quality_job_id"], result.summary["quality_measure"], result.summary["pair_quality_policy"],
                     str(result.directory), hashlib.sha256(result.utility_path.read_bytes()).hexdigest(), hashlib.sha256(result.policy_path.read_bytes()).hexdigest(), hashlib.sha256(result.pair_table.read_bytes()).hexdigest(),
                     summary_text, run.end_time.isoformat()),
                )
                event = AuditEvent(
                    audit_event_id=f"audit-event:sha256:{_evidence(run.run_id + run_sha)}", project_id=run.project_id, actor_id=run.user_id,
                    event_type=AuditEventType.RUN_COMPLETED, subject_type="Run", subject_id=run.run_id, occurred_at=run.end_time,
                    details=CanonicalJson.from_value({"kind": "ANALYSIS", "analysis_id": result.analysis_id, "matcher_run_id": result.summary["matcher_run_id"], "quality_job_id": result.summary["quality_job_id"], "quality_measure": result.summary["quality_measure"]}),
                    current_evidence_sha256=run_sha, local_only=True)
                self._insert_audit_event(event)
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def _record_analysis_run(self, run: CompletedRun, experiment: Experiment, details: dict[str, Any]) -> str:
        """Insert the immutable Run, its experiment, artifacts, and RUN_COMPLETED audit event inside the caller's transaction."""

        run_json = run.model_dump_json()
        run_sha = _evidence(run_json)
        experiment_json = experiment.model_dump_json()
        self._insert_once("experiments", "experiment_id", experiment.experiment_id, byte_exact=False, columns={
            "experiment_id": experiment.experiment_id, "project_id": experiment.project_id, "dataset_id": experiment.dataset_id,
            "evidence_sha256": _evidence(experiment_json), "record_json": experiment_json})
        self._insert_once("runs", "run_id", run.run_id, byte_exact=True, columns={
            "run_id": run.run_id, "project_id": run.project_id, "experiment_id": run.experiment_id, "sample_id": None,
            "run_kind": "ANALYSIS", "status": run.status.value, "engine_resource_id": run.matcher.component_id or "",
            "engine_version": run.matcher.version or "", "start_time": run.start_time.isoformat(), "end_time": run.end_time.isoformat(),
            "evidence_sha256": run_sha, "record_json": run_json})
        for artifact in run.artifacts:
            artifact_json = artifact.model_dump_json()
            self._insert_once("artifacts", "artifact_id", artifact.artifact_id, byte_exact=True, columns={
                "artifact_id": artifact.artifact_id, "run_id": artifact.run_id, "kind": artifact.kind.value, "uri": artifact.uri,
                "sha256": artifact.sha256, "evidence_sha256": _evidence(artifact_json), "record_json": artifact_json})
        event = AuditEvent(
            audit_event_id=f"audit-event:sha256:{_evidence(run.run_id + run_sha)}", project_id=run.project_id, actor_id=run.user_id,
            event_type=AuditEventType.RUN_COMPLETED, subject_type="Run", subject_id=run.run_id, occurred_at=run.end_time,
            details=CanonicalJson.from_value({"kind": "ANALYSIS", **details}), current_evidence_sha256=run_sha, local_only=True)
        self._insert_audit_event(event)
        return run_sha

    def record_cross_matcher_analysis(self, result: Any) -> None:
        run = result.run
        summary_text = json.dumps(result.summary, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                self._record_analysis_run(run, result.experiment, {"analysis_kind": "CROSS_MATCHER", "analysis_id": result.analysis_id, "matrix_id": result.summary["matrix_id"], "quality_job_id": result.summary["quality_job_id"]})
                self._connection.execute(
                    "INSERT INTO cross_matcher_analyses (analysis_id, run_id, project_id, matrix_id, quality_job_id, quality_measure, pair_quality_policy, directory, document_sha256, pairs_sha256, summary_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (result.analysis_id, run.run_id, run.project_id, result.summary["matrix_id"], result.summary["quality_job_id"], result.summary["quality_measure"], result.summary["pair_quality_policy"],
                     str(result.directory), hashlib.sha256(result.document_path.read_bytes()).hexdigest(), hashlib.sha256(result.pairs_path.read_bytes()).hexdigest(), summary_text, run.end_time.isoformat()),
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def cross_matcher_analyses(self, *, project_id: str | None = None, matrix_id: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if project_id is not None:
            clauses.append("project_id = ?"); params.append(project_id)
        if matrix_id is not None:
            clauses.append("matrix_id = ?"); params.append(matrix_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cursor = self._read().execute(f"SELECT * FROM cross_matcher_analyses{where} ORDER BY created_at", params)
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def cross_matcher_analysis(self, analysis_id: str) -> dict[str, Any] | None:
        cursor = self._read().execute("SELECT * FROM cross_matcher_analyses WHERE analysis_id = ? OR run_id = ?", (analysis_id, analysis_id))
        row = cursor.fetchone()
        return {d[0]: v for d, v in zip(cursor.description, row)} if row else None

    def record_annotation(self, *, project_id: str, subject_type: str, subject_id: str, actor_id: str, text: str, context: dict[str, Any]) -> dict[str, Any]:
        created_at = datetime.now(timezone.utc).astimezone().isoformat()
        context_text = json.dumps(context, sort_keys=True, separators=(",", ":"), allow_nan=False)
        evidence = _evidence(json.dumps({"project_id": project_id, "subject_type": subject_type, "subject_id": subject_id, "actor_id": actor_id, "created_at": created_at, "text": text, "context": context}, sort_keys=True))
        annotation_id = f"annotation:sha256:{evidence}"
        with self._lock:
            self._connection.execute(
                "INSERT INTO annotations (annotation_id, project_id, subject_type, subject_id, actor_id, created_at, text, context_json, evidence_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (annotation_id, project_id, subject_type, subject_id, actor_id, created_at, text, context_text, evidence),
            )
            self._connection.commit()
        return {"annotation_id": annotation_id, "project_id": project_id, "subject_type": subject_type, "subject_id": subject_id, "actor_id": actor_id, "created_at": created_at, "text": text, "context": context, "evidence_sha256": evidence}

    def annotations(self, *, subject_id: str | None = None, project_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        clauses, params = [], []
        if subject_id is not None:
            clauses.append("subject_id = ?"); params.append(subject_id)
        if project_id is not None:
            clauses.append("project_id = ?"); params.append(project_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cursor = self._read().execute(f"SELECT * FROM annotations{where} ORDER BY created_at LIMIT ?", [*params, int(limit)])
        rows = [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]
        for row in rows:
            row["context"] = json.loads(row.pop("context_json"))
        return rows

    # -- Phase 5 artifacts: figures, dashboards, reports, bundles, reproductions ------

    def record_figure(self, record: dict[str, Any]) -> None:
        provenance_text = json.dumps({k: v for k, v in record.items() if k != "provenance_sha256"}, sort_keys=True, separators=(",", ":"), allow_nan=False)
        provenance_path = Path(record["directory"]) / "figure.provenance.json"
        provenance_sha256 = hashlib.sha256(provenance_path.read_bytes()).hexdigest()
        renderer_sha256 = record.get("provenance_sha256")
        if renderer_sha256 is not None and renderer_sha256 != provenance_sha256:
            raise ValueError("figure provenance bytes differ from the renderer's recorded SHA-256")
        with self._lock:
            existing = self._connection.execute("SELECT figure_id FROM figures WHERE figure_id = ?", (record["figure_id"],)).fetchone()
            if existing is not None:
                return
            self._connection.execute(
                "INSERT INTO figures (figure_id, project_id, kind, title, directory, source_run_ids_json, provenance_json, provenance_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record["figure_id"], record["project_id"], record["kind"], record["title"], record["directory"], json.dumps(record["source_run_ids"]), provenance_text, provenance_sha256, record["created_at"]),
            )
            self._connection.commit()

    def figure(self, figure_id: str) -> dict[str, Any] | None:
        cursor = self._read().execute("SELECT * FROM figures WHERE figure_id = ?", (figure_id,))
        row = cursor.fetchone()
        return {d[0]: v for d, v in zip(cursor.description, row)} if row else None

    def figures(self, *, project_id: str) -> list[dict[str, Any]]:
        cursor = self._read().execute("SELECT * FROM figures WHERE project_id = ? ORDER BY created_at", (project_id,))
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def record_dashboard(self, record: dict[str, Any]) -> None:
        with self._lock:
            existing = self._connection.execute("SELECT layout_json FROM dashboards WHERE dashboard_id = ?", (record["dashboard_id"],)).fetchone()
            if existing is not None:
                if existing[0] != record["layout_text"]:
                    raise ImmutableRecordConflict("dashboard identity collides with a different layout")
                return
            self._connection.execute(
                "INSERT INTO dashboards (dashboard_id, project_id, name, template, version, supersedes, layout_json, panel_run_ids_json, created_by, created_at, evidence_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record["dashboard_id"], record["project_id"], record["name"], record["template"], record["version"], record["supersedes"], record["layout_text"], json.dumps(record["panel_run_ids"]), record["created_by"], record["created_at"], _evidence(record["layout_text"])),
            )
            self._connection.commit()

    def dashboard(self, dashboard_id: str) -> dict[str, Any] | None:
        cursor = self._read().execute("SELECT * FROM dashboards WHERE dashboard_id = ?", (dashboard_id,))
        row = cursor.fetchone()
        return {d[0]: v for d, v in zip(cursor.description, row)} if row else None

    def dashboards(self, *, project_id: str) -> list[dict[str, Any]]:
        cursor = self._read().execute("SELECT * FROM dashboards WHERE project_id = ? ORDER BY created_at", (project_id,))
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def record_report(self, record: dict[str, Any]) -> None:
        provenance_path = Path(record["directory"]) / "report.provenance.json"
        provenance_sha256 = hashlib.sha256(provenance_path.read_bytes()).hexdigest()
        with self._lock:
            self._connection.execute(
                "INSERT INTO reports (report_id, project_id, title, directory, spec_json, provenance_json, html_sha256, pdf_sha256, json_sha256, provenance_sha256, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record["report_id"], record["project_id"], record["title"], record["directory"], json.dumps(record["spec"], sort_keys=True), json.dumps(record["provenance"], sort_keys=True), record["files"]["html"]["sha256"], record["files"]["pdf"]["sha256"], record["files"]["json"]["sha256"], provenance_sha256, record["created_by"], record["created_at"]),
            )
            self._connection.commit()

    def report(self, report_id: str) -> dict[str, Any] | None:
        cursor = self._read().execute("SELECT * FROM reports WHERE report_id = ?", (report_id,))
        row = cursor.fetchone()
        return {d[0]: v for d, v in zip(cursor.description, row)} if row else None

    def reports(self, *, project_id: str) -> list[dict[str, Any]]:
        cursor = self._read().execute("SELECT * FROM reports WHERE project_id = ? ORDER BY created_at", (project_id,))
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def record_experiment_bundle(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute("INSERT OR IGNORE INTO bundles (bundle_id, project_id, run_id, path, sha256, manifest_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                     (record["bundle_id"], record["project_id"], record["run_id"], record["path"], record["sha256"], json.dumps(record["manifest"], sort_keys=True), record["created_at"]))
            self._connection.commit()

    def experiment_bundles(self, *, project_id: str | None = None, run_id: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if project_id is not None:
            clauses.append("project_id = ?"); params.append(project_id)
        if run_id is not None:
            clauses.append("run_id = ?"); params.append(run_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cursor = self._read().execute(f"SELECT * FROM bundles{where} ORDER BY created_at", params)
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def record_reproduction(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute("INSERT INTO reproductions (reproduction_id, project_id, original_run_id, reproduction_run_id, bundle_sha256, state, report_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                     (record["reproduction_id"], record.get("project_id"), record["original_run_id"], record.get("reproduction_run_id"), record.get("bundle_sha256"), record["state"], json.dumps(record, sort_keys=True, default=str), record["created_at"]))
            self._connection.commit()

    def record_identification_protocol(self, record: dict[str, Any], entries: list[Any]) -> None:
        text = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            existing = self._connection.execute("SELECT record_json FROM identification_protocols WHERE protocol_id = ?", (record["protocol_id"],)).fetchone()
            if existing is not None:
                return  # content-addressed: same protocol id ⇒ same manifest
            self._connection.execute("BEGIN")
            try:
                self._connection.execute("INSERT INTO identification_protocols (protocol_id, project_id, dataset_id, mode, rank_depth, gallery_count, mated_probe_count, nonmated_probe_count, record_json, evidence_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                         (record["protocol_id"], record["project_id"], record["dataset_id"], record["mode"], record["rank_depth"], record["gallery_count"], record["mated_probe_count"], record["nonmated_probe_count"], text, _evidence(text), record["created_at"]))
                self._connection.executemany("INSERT INTO identification_entries (protocol_id, source_order, role, sample_id, subject_id) VALUES (?, ?, ?, ?, ?)", [(record["protocol_id"], e.source_order, e.role, e.sample_id, e.subject_id) for e in entries])
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def identification_protocol(self, protocol_id: str) -> dict[str, Any] | None:
        row = self._read().execute("SELECT record_json FROM identification_protocols WHERE protocol_id = ?", (protocol_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def identification_protocols(self, *, project_id: str) -> list[dict[str, Any]]:
        return [json.loads(row[0]) for row in self._read().execute("SELECT record_json FROM identification_protocols WHERE project_id = ? ORDER BY created_at", (project_id,)).fetchall()]

    def identification_entries(self, protocol_id: str) -> list[dict[str, Any]]:
        cursor = self._read().execute("SELECT source_order, role, sample_id, subject_id FROM identification_entries WHERE protocol_id = ? ORDER BY source_order", (protocol_id,))
        return [{"source_order": r[0], "role": r[1], "sample_id": r[2], "subject_id": r[3]} for r in cursor.fetchall()]

    def record_identification_search(self, result: Any, protocol: dict[str, Any]) -> None:
        run = result.run
        metrics_text = json.dumps(result.metrics, sort_keys=True, separators=(",", ":"), allow_nan=False)
        summary_text = json.dumps(result.summary, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                self._record_analysis_run(run, result.experiment, {"analysis_kind": "IDENTIFICATION_SEARCH", "search_id": result.search_id, "protocol_id": protocol["protocol_id"], "matcher_id": result.summary["matcher_id"], "condition_id": result.summary["condition_id"]})
                self._connection.execute("UPDATE runs SET run_kind = 'IDENTIFICATION' WHERE run_id = ?", (run.run_id,))
                self._connection.execute("INSERT INTO identification_searches (search_id, run_id, project_id, protocol_id, matcher_id, condition_id, directory, candidates_parquet, candidates_sha256, metrics_json, metrics_sha256, summary_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                         (result.search_id, run.run_id, run.project_id, protocol["protocol_id"], result.summary["matcher_id"], result.summary["condition_id"], str(result.directory), str(result.candidates_parquet), hashlib.sha256(result.candidates_parquet.read_bytes()).hexdigest(), metrics_text, _evidence(metrics_text), summary_text, run.end_time.isoformat()))
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def identification_search(self, search_id: str) -> dict[str, Any] | None:
        cursor = self._read().execute("SELECT * FROM identification_searches WHERE search_id = ? OR run_id = ?", (search_id, search_id))
        row = cursor.fetchone()
        return {d[0]: v for d, v in zip(cursor.description, row)} if row else None

    def identification_searches(self, *, project_id: str) -> list[dict[str, Any]]:
        cursor = self._read().execute("SELECT * FROM identification_searches WHERE project_id = ? ORDER BY created_at", (project_id,))
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def record_derived_set(self, result: Any) -> None:
        dataset_json = result.dataset.model_dump_json()
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                self._insert_once("datasets", "dataset_id", result.dataset.dataset_id, byte_exact=True, columns={
                    "dataset_id": result.dataset.dataset_id, "project_id": result.dataset.project_id, "evidence_sha256": _evidence(dataset_json), "record_json": dataset_json})
                for sample in result.samples:
                    sample_json = sample.model_dump_json()
                    self._connection.execute("INSERT INTO samples (sample_id, dataset_id, source_path, sha256, evidence_sha256, record_json) VALUES (?, ?, ?, ?, ?, ?)",
                                             (sample.sample_id, sample.dataset_id, sample.source_path, sample.sha256, _evidence(sample_json), sample_json))
                self._connection.execute("INSERT INTO derived_sets (derived_set_id, project_id, source_dataset_id, source_resource_id, derived_dataset_id, directory, record_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                         (result.derived_set_id, result.record["project_id"], result.record["source_dataset_id"], result.record["source_resource_id"], result.record["derived_dataset_id"], result.record["directory"], json.dumps(result.record, sort_keys=True), result.record["created_at"]))
                self._connection.executemany("INSERT INTO derived_samples (derived_sample_id, derived_set_id, parent_sample_id, transform, parameter, level_index, path, sha256, chain_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                             [(r["derived_sample_id"], result.derived_set_id, r["parent_sample_id"], r["transform"], r["parameter"], r["level_index"], r["path"], r["sha256"], json.dumps(r["chain"], sort_keys=True)) for r in result.rows])
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def derived_set(self, derived_set_id: str) -> dict[str, Any] | None:
        row = self._read().execute("SELECT record_json FROM derived_sets WHERE derived_set_id = ? OR derived_dataset_id = ?", (derived_set_id, derived_set_id)).fetchone()
        return json.loads(row[0]) if row else None

    def derived_sets(self, *, project_id: str) -> list[dict[str, Any]]:
        return [json.loads(row[0]) for row in self._read().execute("SELECT record_json FROM derived_sets WHERE project_id = ? ORDER BY created_at", (project_id,)).fetchall()]

    def derived_samples(self, derived_set_id: str) -> list[dict[str, Any]]:
        cursor = self._read().execute("SELECT derived_sample_id, parent_sample_id, transform, parameter, level_index, path, sha256, chain_json FROM derived_samples WHERE derived_set_id = ? ORDER BY level_index, parent_sample_id", (derived_set_id,))
        return [{"derived_sample_id": r[0], "parent_sample_id": r[1], "transform": r[2], "parameter": r[3], "level_index": r[4], "path": r[5], "sha256": r[6], "chain": json.loads(r[7])} for r in cursor.fetchall()]

    def derived_sample_by_path(self, path: str) -> dict[str, Any] | None:
        row = self._read().execute("SELECT derived_sample_id, derived_set_id, parent_sample_id, sha256, chain_json FROM derived_samples WHERE path = ?", (path,)).fetchone()
        return {"derived_sample_id": row[0], "derived_set_id": row[1], "parent_sample_id": row[2], "sha256": row[3], "chain": json.loads(row[4])} if row else None

    def record_owner_supplied(self, record: dict[str, Any]) -> None:
        """Admit immutable bytes and retain every project that references their managed path."""

        # Lock order: owner-file boundary, then the RunStore writer lock.
        with serialized_owner_file_mutation():
            with self._lock:
                row = self._connection.execute(
                    "SELECT sha256 FROM owner_supplied_inputs WHERE path = ?", (record["path"],)
                ).fetchone()
                if row is not None:
                    if row[0] != record["sha256"]:
                        raise ImmutableRecordConflict(
                            f"owner-supplied path {record['path']} was already admitted with different bytes"
                        )
                else:
                    self._connection.execute(
                        "INSERT INTO owner_supplied_inputs (path, sha256, project_id, actor_id, original_name, size_bytes, storage, source_kind, admitted_at, record_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record["path"], record["sha256"], record["project_id"], record["actor_id"],
                            record["original_name"], record["size_bytes"], record["storage"],
                            record["source_kind"], record["admitted_at"],
                            json.dumps(record, sort_keys=True, separators=(",", ":")),
                        ),
                    )
                self._connection.execute(
                    "INSERT OR IGNORE INTO owner_supplied_project_refs (path, project_id, record_json) VALUES (?, ?, ?)",
                    (
                        record["path"],
                        record["project_id"],
                        json.dumps(record, sort_keys=True, separators=(",", ":")),
                    ),
                )

    def owner_supplied_by_path(self, path: str) -> dict[str, Any] | None:
        row = self._read().execute(
            "SELECT record_json FROM owner_supplied_inputs WHERE path = ?", (path,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def owner_supplied_for_project(self, project_id: str) -> list[dict[str, Any]]:
        cursor = self._read().execute(
            "SELECT refs.record_json FROM owner_supplied_project_refs AS refs "
            "JOIN owner_supplied_inputs AS inputs ON inputs.path = refs.path "
            "WHERE refs.project_id = ? ORDER BY inputs.admitted_at",
            (project_id,),
        )
        return [json.loads(row[0]) for row in cursor.fetchall()]

    def owner_supplied_has_other_project_reference(self, path: str, project_id: str) -> bool:
        row = self._read().execute(
            "SELECT 1 FROM owner_supplied_project_refs WHERE path = ? AND project_id <> ? LIMIT 1",
            (path, project_id),
        ).fetchone()
        return row is not None

    def record_import_mapping(self, mapping_name: str, mapping: dict[str, str], actor_id: str, created_at: str) -> None:
        with self._lock:
            row = self._connection.execute(
                "SELECT mapping_json FROM import_mappings WHERE mapping_name = ?", (mapping_name,)
            ).fetchone()
            text = json.dumps(mapping, sort_keys=True, separators=(",", ":"))
            if row is not None:
                if row[0] != text:
                    raise ImmutableRecordConflict(f"import mapping {mapping_name} already exists with a different mapping")
                return
            self._connection.execute(
                "INSERT INTO import_mappings (mapping_name, mapping_json, actor_id, created_at) VALUES (?, ?, ?, ?)",
                (mapping_name, text, actor_id, created_at),
            )

    def import_mapping(self, mapping_name: str) -> dict[str, str] | None:
        row = self._read().execute(
            "SELECT mapping_json FROM import_mappings WHERE mapping_name = ?", (mapping_name,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def import_mappings(self) -> list[dict[str, Any]]:
        cursor = self._read().execute(
            "SELECT mapping_name, mapping_json, actor_id, created_at FROM import_mappings ORDER BY mapping_name"
        )
        return [
            {"mapping_name": row[0], "mapping": json.loads(row[1]), "actor_id": row[2], "created_at": row[3]}
            for row in cursor.fetchall()
        ]

    def record_degradation_analysis(self, result: Any) -> None:
        run = result.run
        summary_text = json.dumps(result.summary, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                self._record_analysis_run(run, result.experiment, {"analysis_kind": "DEGRADATION_RESPONSE", "analysis_id": result.analysis_id, "derived_set_id": result.summary["derived_set_id"], "matcher_id": result.summary["matcher_id"]})
                self._connection.execute("INSERT INTO degradation_analyses (analysis_id, run_id, project_id, derived_set_id, quality_job_id, matcher_id, condition_id, directory, document_sha256, summary_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                         (result.analysis_id, run.run_id, run.project_id, result.summary["derived_set_id"], result.document["sources"]["quality_job_id"], result.summary["matcher_id"], result.summary["condition_id"], str(result.directory), hashlib.sha256(result.document_path.read_bytes()).hexdigest(), summary_text, run.end_time.isoformat()))
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def degradation_analysis(self, analysis_id: str) -> dict[str, Any] | None:
        cursor = self._read().execute("SELECT * FROM degradation_analyses WHERE analysis_id = ? OR run_id = ?", (analysis_id, analysis_id))
        row = cursor.fetchone()
        return {d[0]: v for d, v in zip(cursor.description, row)} if row else None

    def degradation_analyses(self, *, project_id: str) -> list[dict[str, Any]]:
        cursor = self._read().execute("SELECT * FROM degradation_analyses WHERE project_id = ? ORDER BY created_at", (project_id,))
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def admit_purge(
        self,
        *,
        purge_id: str,
        project_id: str,
        actor_id: str,
        targets: dict[str, list[str]],
    ) -> tuple[dict[str, Any], bool]:
        targets_json = json.dumps(targets, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        now = datetime.now(timezone.utc).isoformat()
        progress_json = json.dumps(
            {"deleted": [], "already_absent": [], "failed": []},
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            cursor = self._connection.execute(
                "SELECT * FROM purge_journal WHERE project_id = ? AND state != 'COMPLETED' ORDER BY created_at LIMIT 1",
                (project_id,),
            )
            existing = self._row(cursor, cursor.fetchone())
            if existing is not None:
                return existing, False
            self._connection.execute(
                "INSERT INTO purge_journal (purge_id, project_id, actor_id, state, created_at, updated_at, targets_json, progress_json) "
                "VALUES (?, ?, ?, 'INTENT_RECORDED', ?, ?, ?, ?)",
                (purge_id, project_id, actor_id, now, now, targets_json, progress_json),
            )
            cursor = self._connection.execute(
                "SELECT * FROM purge_journal WHERE purge_id = ?",
                (purge_id,),
            )
            admitted = self._row(cursor, cursor.fetchone())
            assert admitted is not None
            return admitted, True

    def update_purge_journal(
        self,
        purge_id: str,
        *,
        state: str,
        progress: dict[str, Any],
        error: str | None = None,
    ) -> dict[str, Any]:
        if state not in {"FILES_IN_PROGRESS", "FILES_VERIFIED", "METADATA_VERIFIED", "FAILED"}:
            raise ValueError(f"invalid nonterminal purge journal state: {state}")
        progress_json = json.dumps(progress, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE purge_journal SET state = ?, updated_at = ?, progress_json = ?, error = ? "
                "WHERE purge_id = ? AND state != 'COMPLETED'",
                (state, updated_at, progress_json, error, purge_id),
            )
            if cursor.rowcount != 1:
                raise ImmutableRecordConflict(f"purge journal is not unresolved: {purge_id}")
            cursor = self._connection.execute(
                "SELECT * FROM purge_journal WHERE purge_id = ?",
                (purge_id,),
            )
            updated = self._row(cursor, cursor.fetchone())
            assert updated is not None
            return updated

    def active_purge_journal(self, project_id: str) -> dict[str, Any] | None:
        cursor = self._read().execute(
            "SELECT * FROM purge_journal WHERE project_id = ? AND state != 'COMPLETED' ORDER BY created_at LIMIT 1",
            (project_id,),
        )
        return self._row(cursor, cursor.fetchone())

    def incomplete_purge_journals(self) -> list[dict[str, Any]]:
        cursor = self._read().execute(
            "SELECT * FROM purge_journal WHERE state != 'COMPLETED' ORDER BY created_at, purge_id"
        )
        rows = [self._row(cursor, row) for row in cursor.fetchall()]
        return [row for row in rows if row is not None]

    def record_purge_and_complete(self, record: dict[str, Any], progress: dict[str, Any]) -> None:
        record_json = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        progress_json = json.dumps(progress, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                existing = self._connection.execute(
                    "SELECT record_json FROM purges WHERE purge_id = ?",
                    (record["purge_id"],),
                ).fetchone()
                if existing is None:
                    self._connection.execute(
                        "INSERT INTO purges (purge_id, project_id, actor_id, created_at, verified, record_json) VALUES (?, ?, ?, ?, ?, ?)",
                        (record["purge_id"], record["project_id"], record["actor_id"], record["created_at"], 1 if record["verified"] else 0, record_json),
                    )
                elif existing[0] != record_json:
                    raise ImmutableRecordConflict(f"purge attestation differs for {record['purge_id']}")
                cursor = self._connection.execute(
                    "UPDATE purge_journal SET state = 'COMPLETED', updated_at = ?, progress_json = ?, error = NULL "
                    "WHERE purge_id = ? AND state != 'COMPLETED'",
                    (updated_at, progress_json, record["purge_id"]),
                )
                if cursor.rowcount != 1:
                    current = self._connection.execute(
                        "SELECT state FROM purge_journal WHERE purge_id = ?",
                        (record["purge_id"],),
                    ).fetchone()
                    if current is None or current[0] != "COMPLETED":
                        raise ImmutableRecordConflict(f"purge journal cannot complete: {record['purge_id']}")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def purges(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        where = " WHERE project_id = ?" if project_id is not None else ""
        cursor = self._read().execute(f"SELECT record_json FROM purges{where} ORDER BY created_at", (project_id,) if project_id is not None else ())
        return [json.loads(row[0]) for row in cursor.fetchall()]

    def reproductions(self, *, original_run_id: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if original_run_id is not None:
            clauses.append("original_run_id = ?"); params.append(original_run_id)
        if project_id is not None:
            clauses.append("project_id = ?"); params.append(project_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cursor = self._read().execute(f"SELECT * FROM reproductions{where} ORDER BY created_at", params)
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def utility_analyses(self, *, project_id: str | None = None, matcher_run_id: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if project_id is not None:
            clauses.append("project_id = ?"); params.append(project_id)
        if matcher_run_id is not None:
            clauses.append("matcher_run_id = ?"); params.append(matcher_run_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cursor = self._read().execute(f"SELECT * FROM utility_analyses{where} ORDER BY created_at", params)
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def utility_analysis(self, analysis_id: str) -> dict[str, Any] | None:
        cursor = self._read().execute("SELECT * FROM utility_analyses WHERE analysis_id = ? OR run_id = ?", (analysis_id, analysis_id))
        row = cursor.fetchone()
        return {d[0]: v for d, v in zip(cursor.description, row)} if row else None

    def matcher_leaves(self, *, project_id: str | None = None, matrix_id: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if project_id is not None:
            clauses.append("project_id = ?"); params.append(project_id)
        if matrix_id is not None:
            clauses.append("matrix_id = ?"); params.append(matrix_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cursor = self._read().execute(f"SELECT * FROM matcher_leaves{where} ORDER BY created_at, leaf_id", params)
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def matcher_leaf(self, run_id: str) -> dict[str, Any] | None:
        cursor = self._read().execute("SELECT * FROM matcher_leaves WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        return {d[0]: v for d, v in zip(cursor.description, row)} if row else None

    def finish_job(self, job_id: str, ended_at: str, state: str) -> None:
        with self._lock:
            self._connection.execute("UPDATE jobs SET ended_at = ?, state = ? WHERE job_id = ? AND ended_at IS NULL", (ended_at, state, job_id))

    def job(self, job_id: str) -> dict[str, Any] | None:
        cursor = self._read().execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return {description[0]: value for description, value in zip(cursor.description, row)}

    def jobs_for_project(self, project_id: str) -> list[dict[str, Any]]:
        cursor = self._read().execute("SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at DESC", (project_id,))
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def quality_measure_medians(self, job_id: str) -> dict[str, float]:
        """Median scalar per measure over the job's MEASURED rows (recorded values only)."""

        cursor = self._read().execute(
            "SELECT measures_json FROM quality_results WHERE job_id = ? AND outcome = 'MEASURED'", (job_id,)
        )
        values: dict[str, list[float]] = {}
        for (text,) in cursor.fetchall():
            for name, value in json.loads(text).items():
                if not value["failed"] and value["scalar"] is not None:
                    values.setdefault(name, []).append(float(value["scalar"]))
        medians: dict[str, float] = {}
        for name, items in values.items():
            items.sort()
            mid = len(items) // 2
            medians[name] = items[mid] if len(items) % 2 else (items[mid - 1] + items[mid]) / 2
        return medians

    def quality_results_for_sample(self, sample_id: str) -> list[dict[str, Any]]:
        cursor = self._read().execute(
            "SELECT job_id, sample_id, dataset_id, run_id, outcome, failure_reason, uqs_scalar, uqs_native, measures_json FROM quality_results WHERE sample_id = ? ORDER BY job_id",
            (sample_id,),
        )
        items = []
        for row in cursor.fetchall():
            record = {d[0]: v for d, v in zip(cursor.description, row)}
            record["measures"] = json.loads(record.pop("measures_json"))
            items.append(record)
        return items

    def quality_results_for_job(self, job_id: str, *, min_scalar: int | None, max_scalar: int | None, outcome: str | None, limit: int, offset: int) -> tuple[int, list[dict[str, Any]]]:
        clauses = ["job_id = ?"]
        params: list[Any] = [job_id]
        if min_scalar is not None:
            clauses.append("uqs_scalar >= ?")
            params.append(min_scalar)
        if max_scalar is not None:
            clauses.append("uqs_scalar <= ?")
            params.append(max_scalar)
        if outcome is not None:
            clauses.append("outcome = ?")
            params.append(outcome)
        where = " AND ".join(clauses)
        total = int(self._read().execute(f"SELECT COUNT(*) FROM quality_results WHERE {where}", params).fetchone()[0])
        cursor = self._read().execute(
            f"SELECT q.job_id, q.sample_id, q.dataset_id, q.run_id, q.outcome, q.failure_reason, q.uqs_scalar, q.uqs_native, s.source_path, s.sha256 "
            f"FROM quality_results q JOIN samples s ON s.sample_id = q.sample_id WHERE {where} ORDER BY q.uqs_scalar, s.source_path LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        )
        return total, [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def quality_coverage(self, dataset_id: str) -> dict[str, Any]:
        """PRD §11 quality coverage from every recorded batch result of a dataset."""

        totals = self._read().execute(
            "SELECT outcome, COUNT(*) FROM quality_results WHERE dataset_id = ? GROUP BY outcome", (dataset_id,)
        ).fetchall()
        by_outcome = {outcome: int(count) for outcome, count in totals}
        assessed_samples = int(
            self._read().execute(
                "SELECT COUNT(DISTINCT sample_id) FROM quality_results WHERE dataset_id = ? AND outcome = 'MEASURED'", (dataset_id,)
            ).fetchone()[0]
        )
        distinct_samples = int(
            self._read().execute("SELECT COUNT(DISTINCT sample_id) FROM quality_results WHERE dataset_id = ?", (dataset_id,)).fetchone()[0]
        )
        jobs = [row[0] for row in self._read().execute("SELECT DISTINCT job_id FROM quality_results WHERE dataset_id = ?", (dataset_id,)).fetchall()]
        if not distinct_samples:
            return {"state": "NOT_ASSESSED", "reason": "No quality Run has executed over this dataset yet (QS-B12)."}
        uqs = self._read().execute(
            "SELECT MIN(uqs_scalar), MAX(uqs_scalar), AVG(uqs_scalar), COUNT(uqs_scalar) FROM quality_results WHERE dataset_id = ? AND outcome = 'MEASURED'",
            (dataset_id,),
        ).fetchone()
        histogram = self._read().execute(
            "SELECT uqs_scalar / 10 AS bin, COUNT(*) FROM quality_results WHERE dataset_id = ? AND outcome = 'MEASURED' AND uqs_scalar IS NOT NULL GROUP BY bin ORDER BY bin",
            (dataset_id,),
        ).fetchall()
        component_failures: dict[str, int] = {}
        for (measures_json,) in self._read().execute(
            "SELECT measures_json FROM quality_results WHERE dataset_id = ? AND outcome = 'MEASURED'", (dataset_id,)
        ).fetchall():
            for name, value in json.loads(measures_json).items():
                if value.get("failed"):
                    component_failures[name] = component_failures.get(name, 0) + 1
        total_rows = sum(by_outcome.values())
        return {
            "state": "COMPUTED",
            "jobs": jobs,
            "distinct_samples_with_results": distinct_samples,
            "distinct_samples_measured": assessed_samples,
            "rows_by_outcome": by_outcome,
            "quality_assessment_success_rate": round(by_outcome.get("MEASURED", 0) / total_rows, 6),
            "quality_assessment_failure_rate": round(1 - by_outcome.get("MEASURED", 0) / total_rows, 6),
            "unified_quality_score_scalar": {"min": uqs[0], "max": uqs[1], "mean": uqs[2], "count": uqs[3]},
            "unified_quality_score_decile_histogram": [{"bin": int(b) if b is not None else None, "count": int(c)} for b, c in histogram],
            "component_failures": dict(sorted(component_failures.items())),
            "component_completeness": {name: 1 - count / max(1, by_outcome.get("MEASURED", 0)) for name, count in sorted(component_failures.items())},
        }

    def record_protocol(self, record: Any, pairs: list[Any], audit_event: AuditEvent) -> None:
        record_json = record.model_dump_json()
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                self._insert_once(
                    "protocols",
                    "protocol_id",
                    record.protocol_id,
                    byte_exact=False,
                    columns={
                        "protocol_id": record.protocol_id,
                        "project_id": record.project_id,
                        "dataset_id": record.dataset_id,
                        "evidence_sha256": _evidence(record_json),
                        "record_json": record_json,
                    },
                )
                self._connection.executemany(
                    "INSERT OR IGNORE INTO protocol_pairs (protocol_id, pair_id, fold, source_order, label, left_sample_id, right_sample_id, record_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (pair.protocol_id, pair.pair_id, pair.fold, pair.source_order, pair.label.value, pair.left_sample_id, pair.right_sample_id, pair.model_dump_json())
                        for pair in pairs
                    ],
                )
                self._insert_audit_event(audit_event)
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def protocol(self, protocol_id: str) -> Any | None:
        from ..domain.comparison import VerificationProtocolRecord

        row = self._read().execute("SELECT record_json FROM protocols WHERE protocol_id = ?", (protocol_id,)).fetchone()
        return VerificationProtocolRecord.model_validate_json(row[0]) if row else None

    def protocols_for_project(self, project_id: str) -> list[Any]:
        from ..domain.comparison import VerificationProtocolRecord

        rows = self._read().execute("SELECT record_json FROM protocols WHERE project_id = ?", (project_id,)).fetchall()
        return [VerificationProtocolRecord.model_validate_json(row[0]) for row in rows]

    def protocol_pairs(self, protocol_id: str, *, limit: int, offset: int = 0, fold: int | None = None, label: str | None = None) -> list[Any]:
        from ..domain.comparison import ProtocolPair

        clauses = ["protocol_id = ?"]
        params: list[Any] = [protocol_id]
        if fold is not None:
            clauses.append("fold = ?")
            params.append(fold)
        if label is not None:
            clauses.append("label = ?")
            params.append(label)
        rows = self._read().execute(
            f"SELECT record_json FROM protocol_pairs WHERE {' AND '.join(clauses)} ORDER BY fold, source_order LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        ).fetchall()
        return [ProtocolPair.model_validate_json(row[0]) for row in rows]

    def protocol_pair_count(self, protocol_id: str) -> int:
        return int(self._read().execute("SELECT COUNT(*) FROM protocol_pairs WHERE protocol_id = ?", (protocol_id,)).fetchone()[0])

    def artifacts_for_run(self, run_id: str) -> list[Artifact]:
        rows = self._read().execute("SELECT record_json FROM artifacts WHERE run_id = ?", (run_id,)).fetchall()
        return [Artifact.model_validate_json(row[0]) for row in rows]

    # -- reads -----------------------------------------------------------------

    def datasets_for_project(self, project_id: str) -> list[Dataset]:
        rows = self._read().execute(
            "SELECT record_json FROM datasets WHERE project_id = ?", (project_id,)
        ).fetchall()
        return [Dataset.model_validate_json(row[0]) for row in rows]

    def dataset_statistics(self, dataset_id: str) -> tuple[dict, str] | None:
        row = self._read().execute(
            "SELECT record_json, evidence_sha256 FROM dataset_statistics WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
        if row is None:
            return None
        if _evidence(row[0]) != row[1]:
            raise ImmutableRecordConflict(f"dataset statistics {dataset_id} do not match their digest")
        return json.loads(row[0]), row[1]

    def sample_count(self, dataset_id: str) -> int:
        row = self._read().execute("SELECT COUNT(*) FROM samples WHERE dataset_id = ?", (dataset_id,)).fetchone()
        return int(row[0])

    def samples_for_dataset(self, dataset_id: str, *, limit: int, offset: int = 0, subject_id: str | None = None) -> list[Sample]:
        if subject_id is None:
            rows = self._read().execute(
                "SELECT record_json FROM samples WHERE dataset_id = ? ORDER BY source_path LIMIT ? OFFSET ?",
                (dataset_id, int(limit), int(offset)),
            ).fetchall()
        else:
            rows = self._read().execute(
                "SELECT record_json FROM samples WHERE dataset_id = ? AND source_path LIKE ? ORDER BY source_path LIMIT ? OFFSET ?",
                (dataset_id, f"%/{subject_id}/%", int(limit), int(offset)),
            ).fetchall()
        return [Sample.model_validate_json(row[0]) for row in rows]

    def sample_by_source_path(self, source_path: str) -> Sample | None:
        row = self._read().execute(
            "SELECT record_json FROM samples WHERE source_path = ? LIMIT 1", (source_path,)
        ).fetchone()
        return Sample.model_validate_json(row[0]) if row else None

    def sample_by_path(self, dataset_id: str, source_path: str) -> Sample | None:
        row = self._read().execute(
            "SELECT record_json FROM samples WHERE dataset_id = ? AND source_path = ?", (dataset_id, source_path)
        ).fetchone()
        return Sample.model_validate_json(row[0]) if row else None

    def project_head(self, project_id: str) -> tuple[Project, int] | None:
        row = self._read().execute(
            "SELECT record_json, version, evidence_sha256 FROM project_versions WHERE project_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if row is None:
            legacy = self._read().execute(
                "SELECT record_json FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if legacy is None:
                return None
            return Project.model_validate_json(legacy[0]), 1
        record_json, version, evidence = row
        if _evidence(record_json) != evidence:
            raise ImmutableRecordConflict(f"project version {project_id}@{version} does not match its digest")
        return Project.model_validate_json(record_json), int(version)

    def project_versions(self, project_id: str) -> list[tuple[int, Project]]:
        rows = self._read().execute(
            "SELECT version, record_json FROM project_versions WHERE project_id = ? ORDER BY version",
            (project_id,),
        ).fetchall()
        return [(int(version), Project.model_validate_json(record_json)) for version, record_json in rows]

    def list_project_heads(self) -> list[tuple[Project, int]]:
        rows = self._read().execute(
            "SELECT p.project_id FROM projects p ORDER BY p.created_at DESC"
        ).fetchall()
        heads: list[tuple[Project, int]] = []
        for (project_id,) in rows:
            head = self.project_head(project_id)
            if head is not None:
                heads.append(head)
        return heads

    def run_ids_for_project(self, project_id: str) -> list[str]:
        rows = self._read().execute(
            "SELECT run_id FROM runs WHERE project_id = ? ORDER BY start_time", (project_id,)
        ).fetchall()
        return [row[0] for row in rows]

    def _validated_audit_chain(self, project_id: str) -> list[AuditEvent]:
        rows = self._read().execute(
            "SELECT record_json, evidence_sha256 FROM audit_events "
            "WHERE project_id = ? ORDER BY rowid",
            (project_id,),
        ).fetchall()
        events: list[AuditEvent] = []
        previous_evidence: str | None = None
        linked_suffix_started = False
        for record_json, evidence_sha256 in rows:
            if _evidence(record_json) != evidence_sha256:
                raise ImmutableRecordConflict(
                    f"stored audit event in project {project_id} does not match its evidence digest"
                )
            event = AuditEvent.model_validate_json(record_json)
            if previous_evidence is None:
                if event.previous_evidence_sha256 is not None:
                    raise ImmutableRecordConflict(
                        f"audit chain for project {project_id} has a non-empty first link"
                    )
            elif event.previous_evidence_sha256 is None:
                if linked_suffix_started:
                    raise ImmutableRecordConflict(
                        f"audit chain for project {project_id} lost its prior evidence link"
                    )
            else:
                linked_suffix_started = True
                if event.previous_evidence_sha256 != previous_evidence:
                    raise ImmutableRecordConflict(
                        f"audit chain for project {project_id} does not match its prior evidence link"
                    )
            events.append(event)
            previous_evidence = str(evidence_sha256)
        return events

    def audit_events_for_project(self, project_id: str) -> list[AuditEvent]:
        return self._validated_audit_chain(project_id)

    def run(self, run_id: str) -> tuple[CompletedRun, str] | None:
        row = self._read().execute(
            "SELECT record_json, evidence_sha256 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        record_json, evidence_sha256 = row
        if _evidence(record_json) != evidence_sha256:
            raise ImmutableRecordConflict(f"stored Run {run_id} does not match its evidence digest")
        return CompletedRun.model_validate_json(record_json), evidence_sha256

    def run_summaries(self, limit: int = 200, *, project_id: str | None = None) -> list[RunSummary]:
        query = (
            "SELECT run_id, project_id, experiment_id, sample_id, status, engine_resource_id, "
            "engine_version, start_time, end_time, evidence_sha256 FROM runs "
        )
        parameters: tuple[Any, ...]
        if project_id is None:
            query += "ORDER BY start_time DESC LIMIT ?"
            parameters = (int(limit),)
        else:
            query += "WHERE project_id = ? ORDER BY start_time DESC LIMIT ?"
            parameters = (project_id, int(limit))
        rows = self._read().execute(query, parameters).fetchall()
        return [RunSummary(*row) for row in rows]

    def project(self, project_id: str) -> Project | None:
        row = self._read().execute("SELECT record_json FROM projects WHERE project_id = ?", (project_id,)).fetchone()
        return Project.model_validate_json(row[0]) if row else None

    def dataset(self, dataset_id: str) -> Dataset | None:
        row = self._read().execute("SELECT record_json FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
        return Dataset.model_validate_json(row[0]) if row else None

    def sample(self, sample_id: str) -> Sample | None:
        row = self._read().execute("SELECT record_json FROM samples WHERE sample_id = ?", (sample_id,)).fetchone()
        return Sample.model_validate_json(row[0]) if row else None

    def experiment(self, experiment_id: str) -> Experiment | None:
        row = self._read().execute(
            "SELECT record_json FROM experiments WHERE experiment_id = ?", (experiment_id,)
        ).fetchone()
        return Experiment.model_validate_json(row[0]) if row else None

    def audit_events_for(self, subject_id: str) -> list[AuditEvent]:
        projects = self._read().execute(
            "SELECT DISTINCT project_id FROM audit_events WHERE subject_id = ? ORDER BY project_id",
            (subject_id,),
        ).fetchall()
        events = [
            event
            for (project_id,) in projects
            for event in self._validated_audit_chain(str(project_id))
            if event.subject_id == subject_id
        ]
        return sorted(events, key=lambda event: (event.occurred_at, event.audit_event_id))

    def sample_id_for_run(self, run_id: str) -> str | None:
        row = self._read().execute("SELECT sample_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return row[0] if row else None
