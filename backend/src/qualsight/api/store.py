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
from pathlib import Path
import sqlite3
import threading

from typing import Any

from ..domain.entities import Artifact, AuditEvent, AuditEventType, Dataset, Experiment, Project, ProtocolKind, Sample, VerificationProtocol
from ..domain.primitives import CanonicalJson, ComponentBinding, ComponentState
from ..domain.run import CompletedRun
from ..engines.ofiq.adapter import SingleSampleRunBundle

SCHEMA = """
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
    fold INTEGER NOT NULL,
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


class RunStore:
    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(str(database_path), check_same_thread=False, isolation_level=None)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(SCHEMA)

    def close(self) -> None:
        self._connection.close()

    # -- writes ----------------------------------------------------------------

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
                audit_json = bundle.audit_event.model_dump_json()
                self._insert_once(
                    "audit_events",
                    "audit_event_id",
                    bundle.audit_event.audit_event_id,
                    byte_exact=True,
                    columns={
                        "audit_event_id": bundle.audit_event.audit_event_id,
                        "project_id": bundle.audit_event.project_id,
                        "subject_type": bundle.audit_event.subject_type,
                        "subject_id": bundle.audit_event.subject_id,
                        "occurred_at": bundle.audit_event.occurred_at.isoformat(),
                        "evidence_sha256": _evidence(audit_json),
                        "record_json": audit_json,
                    },
                )
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
        record_json = event.model_dump_json()
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                self._insert_once(
                    "audit_events",
                    "audit_event_id",
                    event.audit_event_id,
                    byte_exact=True,
                    columns={
                        "audit_event_id": event.audit_event_id,
                        "project_id": event.project_id,
                        "subject_type": event.subject_type,
                        "subject_id": event.subject_id,
                        "occurred_at": event.occurred_at.isoformat(),
                        "evidence_sha256": _evidence(record_json),
                        "record_json": record_json,
                    },
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

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
                audit_json = audit_event.model_dump_json()
                self._insert_once(
                    "audit_events",
                    "audit_event_id",
                    audit_event.audit_event_id,
                    byte_exact=True,
                    columns={
                        "audit_event_id": audit_event.audit_event_id,
                        "project_id": audit_event.project_id,
                        "subject_type": audit_event.subject_type,
                        "subject_id": audit_event.subject_id,
                        "occurred_at": audit_event.occurred_at.isoformat(),
                        "evidence_sha256": _evidence(audit_json),
                        "record_json": audit_json,
                    },
                )
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
                    event_json = event.model_dump_json()
                    self._insert_once(
                        "audit_events",
                        "audit_event_id",
                        event.audit_event_id,
                        byte_exact=True,
                        columns={
                            "audit_event_id": event.audit_event_id,
                            "project_id": event.project_id,
                            "subject_type": event.subject_type,
                            "subject_id": event.subject_id,
                            "occurred_at": event.occurred_at.isoformat(),
                            "evidence_sha256": _evidence(event_json),
                            "record_json": event_json,
                        },
                    )
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
                    "INSERT INTO matcher_leaves (run_id, matrix_id, leaf_id, project_id, dataset_id, protocol_id, matcher_id, condition_id, comparisons_parquet, comparisons_sha256, metrics_json, metrics_sha256, comparison_count, scored_count, failed_count, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run.run_id, job.matrix_id, leaf.leaf_id, run.project_id, job.dataset.dataset_id, job.protocol.protocol_id, leaf.matcher_id.value, leaf.condition_id.value,
                     str(leaf.comparisons_parquet), hashlib.sha256(leaf.comparisons_parquet.read_bytes()).hexdigest(), metrics_text, _evidence(metrics_text),
                     leaf.comparison_count, leaf.scored_count, leaf.failed_count, run.end_time.isoformat()),
                )
                event = AuditEvent(
                    audit_event_id=f"audit-event:sha256:{_evidence(run.run_id + run_sha)}", project_id=run.project_id, actor_id=run.user_id,
                    event_type=AuditEventType.RUN_COMPLETED, subject_type="Run", subject_id=run.run_id, occurred_at=run.end_time,
                    details=CanonicalJson.from_value({"kind": "MATCHER", "leaf_id": leaf.leaf_id, "matrix_id": job.matrix_id, "scored": leaf.scored_count, "failed": leaf.failed_count}),
                    current_evidence_sha256=run_sha, local_only=True)
                event_json = event.model_dump_json()
                self._insert_once("audit_events", "audit_event_id", event.audit_event_id, byte_exact=True, columns={
                    "audit_event_id": event.audit_event_id, "project_id": event.project_id, "subject_type": event.subject_type, "subject_id": event.subject_id,
                    "occurred_at": event.occurred_at.isoformat(), "evidence_sha256": _evidence(event_json), "record_json": event_json})
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
                event_json = event.model_dump_json()
                self._insert_once("audit_events", "audit_event_id", event.audit_event_id, byte_exact=True, columns={
                    "audit_event_id": event.audit_event_id, "project_id": event.project_id, "subject_type": event.subject_type, "subject_id": event.subject_id,
                    "occurred_at": event.occurred_at.isoformat(), "evidence_sha256": _evidence(event_json), "record_json": event_json})
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
        event_json = event.model_dump_json()
        self._insert_once("audit_events", "audit_event_id", event.audit_event_id, byte_exact=True, columns={
            "audit_event_id": event.audit_event_id, "project_id": event.project_id, "subject_type": event.subject_type, "subject_id": event.subject_id,
            "occurred_at": event.occurred_at.isoformat(), "evidence_sha256": _evidence(event_json), "record_json": event_json})
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
        cursor = self._connection.execute(f"SELECT * FROM cross_matcher_analyses{where} ORDER BY created_at", params)
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def cross_matcher_analysis(self, analysis_id: str) -> dict[str, Any] | None:
        cursor = self._connection.execute("SELECT * FROM cross_matcher_analyses WHERE analysis_id = ? OR run_id = ?", (analysis_id, analysis_id))
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
        cursor = self._connection.execute(f"SELECT * FROM annotations{where} ORDER BY created_at LIMIT ?", [*params, int(limit)])
        rows = [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]
        for row in rows:
            row["context"] = json.loads(row.pop("context_json"))
        return rows

    # -- Phase 5 artifacts: figures, dashboards, reports, bundles, reproductions ------

    def record_figure(self, record: dict[str, Any]) -> None:
        provenance_text = json.dumps({k: v for k, v in record.items() if k != "provenance_sha256"}, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._lock:
            existing = self._connection.execute("SELECT figure_id FROM figures WHERE figure_id = ?", (record["figure_id"],)).fetchone()
            if existing is not None:
                return
            self._connection.execute(
                "INSERT INTO figures (figure_id, project_id, kind, title, directory, source_run_ids_json, provenance_json, provenance_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record["figure_id"], record["project_id"], record["kind"], record["title"], record["directory"], json.dumps(record["source_run_ids"]), provenance_text, _evidence(provenance_text), record["created_at"]),
            )
            self._connection.commit()

    def figure(self, figure_id: str) -> dict[str, Any] | None:
        cursor = self._connection.execute("SELECT * FROM figures WHERE figure_id = ?", (figure_id,))
        row = cursor.fetchone()
        return {d[0]: v for d, v in zip(cursor.description, row)} if row else None

    def figures(self, *, project_id: str) -> list[dict[str, Any]]:
        cursor = self._connection.execute("SELECT * FROM figures WHERE project_id = ? ORDER BY created_at", (project_id,))
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
        cursor = self._connection.execute("SELECT * FROM dashboards WHERE dashboard_id = ?", (dashboard_id,))
        row = cursor.fetchone()
        return {d[0]: v for d, v in zip(cursor.description, row)} if row else None

    def dashboards(self, *, project_id: str) -> list[dict[str, Any]]:
        cursor = self._connection.execute("SELECT * FROM dashboards WHERE project_id = ? ORDER BY created_at", (project_id,))
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def record_report(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO reports (report_id, project_id, title, directory, spec_json, provenance_json, html_sha256, pdf_sha256, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record["report_id"], record["project_id"], record["title"], record["directory"], json.dumps(record["spec"], sort_keys=True), json.dumps(record["provenance"], sort_keys=True), record["files"]["html"]["sha256"], record["files"]["pdf"]["sha256"], record["created_by"], record["created_at"]),
            )
            self._connection.commit()

    def report(self, report_id: str) -> dict[str, Any] | None:
        cursor = self._connection.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,))
        row = cursor.fetchone()
        return {d[0]: v for d, v in zip(cursor.description, row)} if row else None

    def reports(self, *, project_id: str) -> list[dict[str, Any]]:
        cursor = self._connection.execute("SELECT * FROM reports WHERE project_id = ? ORDER BY created_at", (project_id,))
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
        cursor = self._connection.execute(f"SELECT * FROM bundles{where} ORDER BY created_at", params)
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def record_reproduction(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute("INSERT INTO reproductions (reproduction_id, project_id, original_run_id, reproduction_run_id, bundle_sha256, state, report_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                     (record["reproduction_id"], record.get("project_id"), record["original_run_id"], record.get("reproduction_run_id"), record.get("bundle_sha256"), record["state"], json.dumps(record, sort_keys=True, default=str), record["created_at"]))
            self._connection.commit()

    def reproductions(self, *, original_run_id: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if original_run_id is not None:
            clauses.append("original_run_id = ?"); params.append(original_run_id)
        if project_id is not None:
            clauses.append("project_id = ?"); params.append(project_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cursor = self._connection.execute(f"SELECT * FROM reproductions{where} ORDER BY created_at", params)
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def utility_analyses(self, *, project_id: str | None = None, matcher_run_id: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if project_id is not None:
            clauses.append("project_id = ?"); params.append(project_id)
        if matcher_run_id is not None:
            clauses.append("matcher_run_id = ?"); params.append(matcher_run_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cursor = self._connection.execute(f"SELECT * FROM utility_analyses{where} ORDER BY created_at", params)
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def utility_analysis(self, analysis_id: str) -> dict[str, Any] | None:
        cursor = self._connection.execute("SELECT * FROM utility_analyses WHERE analysis_id = ? OR run_id = ?", (analysis_id, analysis_id))
        row = cursor.fetchone()
        return {d[0]: v for d, v in zip(cursor.description, row)} if row else None

    def matcher_leaves(self, *, project_id: str | None = None, matrix_id: str | None = None) -> list[dict[str, Any]]:
        clauses, params = [], []
        if project_id is not None:
            clauses.append("project_id = ?"); params.append(project_id)
        if matrix_id is not None:
            clauses.append("matrix_id = ?"); params.append(matrix_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cursor = self._connection.execute(f"SELECT * FROM matcher_leaves{where} ORDER BY created_at, leaf_id", params)
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def matcher_leaf(self, run_id: str) -> dict[str, Any] | None:
        cursor = self._connection.execute("SELECT * FROM matcher_leaves WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        return {d[0]: v for d, v in zip(cursor.description, row)} if row else None

    def finish_job(self, job_id: str, ended_at: str, state: str) -> None:
        with self._lock:
            self._connection.execute("UPDATE jobs SET ended_at = ?, state = ? WHERE job_id = ? AND ended_at IS NULL", (ended_at, state, job_id))

    def job(self, job_id: str) -> dict[str, Any] | None:
        cursor = self._connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return {description[0]: value for description, value in zip(cursor.description, row)}

    def jobs_for_project(self, project_id: str) -> list[dict[str, Any]]:
        cursor = self._connection.execute("SELECT * FROM jobs WHERE project_id = ? ORDER BY created_at DESC", (project_id,))
        return [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def quality_results_for_sample(self, sample_id: str) -> list[dict[str, Any]]:
        cursor = self._connection.execute(
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
        total = int(self._connection.execute(f"SELECT COUNT(*) FROM quality_results WHERE {where}", params).fetchone()[0])
        cursor = self._connection.execute(
            f"SELECT q.job_id, q.sample_id, q.dataset_id, q.run_id, q.outcome, q.failure_reason, q.uqs_scalar, q.uqs_native, s.source_path, s.sha256 "
            f"FROM quality_results q JOIN samples s ON s.sample_id = q.sample_id WHERE {where} ORDER BY q.uqs_scalar, s.source_path LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        )
        return total, [{d[0]: v for d, v in zip(cursor.description, row)} for row in cursor.fetchall()]

    def quality_coverage(self, dataset_id: str) -> dict[str, Any]:
        """PRD §11 quality coverage from every recorded batch result of a dataset."""

        totals = self._connection.execute(
            "SELECT outcome, COUNT(*) FROM quality_results WHERE dataset_id = ? GROUP BY outcome", (dataset_id,)
        ).fetchall()
        by_outcome = {outcome: int(count) for outcome, count in totals}
        assessed_samples = int(
            self._connection.execute(
                "SELECT COUNT(DISTINCT sample_id) FROM quality_results WHERE dataset_id = ? AND outcome = 'MEASURED'", (dataset_id,)
            ).fetchone()[0]
        )
        distinct_samples = int(
            self._connection.execute("SELECT COUNT(DISTINCT sample_id) FROM quality_results WHERE dataset_id = ?", (dataset_id,)).fetchone()[0]
        )
        jobs = [row[0] for row in self._connection.execute("SELECT DISTINCT job_id FROM quality_results WHERE dataset_id = ?", (dataset_id,)).fetchall()]
        if not distinct_samples:
            return {"state": "NOT_ASSESSED", "reason": "No quality Run has executed over this dataset yet (QS-B12)."}
        uqs = self._connection.execute(
            "SELECT MIN(uqs_scalar), MAX(uqs_scalar), AVG(uqs_scalar), COUNT(uqs_scalar) FROM quality_results WHERE dataset_id = ? AND outcome = 'MEASURED'",
            (dataset_id,),
        ).fetchone()
        histogram = self._connection.execute(
            "SELECT uqs_scalar / 10 AS bin, COUNT(*) FROM quality_results WHERE dataset_id = ? AND outcome = 'MEASURED' AND uqs_scalar IS NOT NULL GROUP BY bin ORDER BY bin",
            (dataset_id,),
        ).fetchall()
        component_failures: dict[str, int] = {}
        for (measures_json,) in self._connection.execute(
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
                audit_json = audit_event.model_dump_json()
                self._insert_once(
                    "audit_events",
                    "audit_event_id",
                    audit_event.audit_event_id,
                    byte_exact=True,
                    columns={
                        "audit_event_id": audit_event.audit_event_id,
                        "project_id": audit_event.project_id,
                        "subject_type": audit_event.subject_type,
                        "subject_id": audit_event.subject_id,
                        "occurred_at": audit_event.occurred_at.isoformat(),
                        "evidence_sha256": _evidence(audit_json),
                        "record_json": audit_json,
                    },
                )
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    def protocol(self, protocol_id: str) -> Any | None:
        from ..domain.comparison import VerificationProtocolRecord

        row = self._connection.execute("SELECT record_json FROM protocols WHERE protocol_id = ?", (protocol_id,)).fetchone()
        return VerificationProtocolRecord.model_validate_json(row[0]) if row else None

    def protocols_for_project(self, project_id: str) -> list[Any]:
        from ..domain.comparison import VerificationProtocolRecord

        rows = self._connection.execute("SELECT record_json FROM protocols WHERE project_id = ?", (project_id,)).fetchall()
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
        rows = self._connection.execute(
            f"SELECT record_json FROM protocol_pairs WHERE {' AND '.join(clauses)} ORDER BY fold, source_order LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        ).fetchall()
        return [ProtocolPair.model_validate_json(row[0]) for row in rows]

    def protocol_pair_count(self, protocol_id: str) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM protocol_pairs WHERE protocol_id = ?", (protocol_id,)).fetchone()[0])

    def artifacts_for_run(self, run_id: str) -> list[Artifact]:
        rows = self._connection.execute("SELECT record_json FROM artifacts WHERE run_id = ?", (run_id,)).fetchall()
        return [Artifact.model_validate_json(row[0]) for row in rows]

    # -- reads -----------------------------------------------------------------

    def datasets_for_project(self, project_id: str) -> list[Dataset]:
        rows = self._connection.execute(
            "SELECT record_json FROM datasets WHERE project_id = ?", (project_id,)
        ).fetchall()
        return [Dataset.model_validate_json(row[0]) for row in rows]

    def dataset_statistics(self, dataset_id: str) -> tuple[dict, str] | None:
        row = self._connection.execute(
            "SELECT record_json, evidence_sha256 FROM dataset_statistics WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
        if row is None:
            return None
        if _evidence(row[0]) != row[1]:
            raise ImmutableRecordConflict(f"dataset statistics {dataset_id} do not match their digest")
        return json.loads(row[0]), row[1]

    def sample_count(self, dataset_id: str) -> int:
        row = self._connection.execute("SELECT COUNT(*) FROM samples WHERE dataset_id = ?", (dataset_id,)).fetchone()
        return int(row[0])

    def samples_for_dataset(self, dataset_id: str, *, limit: int, offset: int = 0, subject_id: str | None = None) -> list[Sample]:
        if subject_id is None:
            rows = self._connection.execute(
                "SELECT record_json FROM samples WHERE dataset_id = ? ORDER BY source_path LIMIT ? OFFSET ?",
                (dataset_id, int(limit), int(offset)),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT record_json FROM samples WHERE dataset_id = ? AND source_path LIKE ? ORDER BY source_path LIMIT ? OFFSET ?",
                (dataset_id, f"%/{subject_id}/%", int(limit), int(offset)),
            ).fetchall()
        return [Sample.model_validate_json(row[0]) for row in rows]

    def sample_by_source_path(self, source_path: str) -> Sample | None:
        row = self._connection.execute(
            "SELECT record_json FROM samples WHERE source_path = ? LIMIT 1", (source_path,)
        ).fetchone()
        return Sample.model_validate_json(row[0]) if row else None

    def sample_by_path(self, dataset_id: str, source_path: str) -> Sample | None:
        row = self._connection.execute(
            "SELECT record_json FROM samples WHERE dataset_id = ? AND source_path = ?", (dataset_id, source_path)
        ).fetchone()
        return Sample.model_validate_json(row[0]) if row else None

    def project_head(self, project_id: str) -> tuple[Project, int] | None:
        row = self._connection.execute(
            "SELECT record_json, version, evidence_sha256 FROM project_versions WHERE project_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if row is None:
            legacy = self._connection.execute(
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
        rows = self._connection.execute(
            "SELECT version, record_json FROM project_versions WHERE project_id = ? ORDER BY version",
            (project_id,),
        ).fetchall()
        return [(int(version), Project.model_validate_json(record_json)) for version, record_json in rows]

    def list_project_heads(self) -> list[tuple[Project, int]]:
        rows = self._connection.execute(
            "SELECT p.project_id FROM projects p ORDER BY p.created_at DESC"
        ).fetchall()
        heads: list[tuple[Project, int]] = []
        for (project_id,) in rows:
            head = self.project_head(project_id)
            if head is not None:
                heads.append(head)
        return heads

    def run_ids_for_project(self, project_id: str) -> list[str]:
        rows = self._connection.execute(
            "SELECT run_id FROM runs WHERE project_id = ? ORDER BY start_time", (project_id,)
        ).fetchall()
        return [row[0] for row in rows]

    def audit_events_for_project(self, project_id: str) -> list[AuditEvent]:
        rows = self._connection.execute(
            "SELECT record_json FROM audit_events WHERE project_id = ? ORDER BY occurred_at", (project_id,)
        ).fetchall()
        return [AuditEvent.model_validate_json(row[0]) for row in rows]

    def run(self, run_id: str) -> tuple[CompletedRun, str] | None:
        row = self._connection.execute(
            "SELECT record_json, evidence_sha256 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        record_json, evidence_sha256 = row
        if _evidence(record_json) != evidence_sha256:
            raise ImmutableRecordConflict(f"stored Run {run_id} does not match its evidence digest")
        return CompletedRun.model_validate_json(record_json), evidence_sha256

    def run_summaries(self, limit: int = 200) -> list[RunSummary]:
        rows = self._connection.execute(
            "SELECT run_id, project_id, experiment_id, sample_id, status, engine_resource_id, "
            "engine_version, start_time, end_time, evidence_sha256 FROM runs "
            "ORDER BY start_time DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [RunSummary(*row) for row in rows]

    def project(self, project_id: str) -> Project | None:
        row = self._connection.execute("SELECT record_json FROM projects WHERE project_id = ?", (project_id,)).fetchone()
        return Project.model_validate_json(row[0]) if row else None

    def dataset(self, dataset_id: str) -> Dataset | None:
        row = self._connection.execute("SELECT record_json FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
        return Dataset.model_validate_json(row[0]) if row else None

    def sample(self, sample_id: str) -> Sample | None:
        row = self._connection.execute("SELECT record_json FROM samples WHERE sample_id = ?", (sample_id,)).fetchone()
        return Sample.model_validate_json(row[0]) if row else None

    def experiment(self, experiment_id: str) -> Experiment | None:
        row = self._connection.execute(
            "SELECT record_json FROM experiments WHERE experiment_id = ?", (experiment_id,)
        ).fetchone()
        return Experiment.model_validate_json(row[0]) if row else None

    def audit_events_for(self, subject_id: str) -> list[AuditEvent]:
        rows = self._connection.execute(
            "SELECT record_json FROM audit_events WHERE subject_id = ? ORDER BY occurred_at", (subject_id,)
        ).fetchall()
        return [AuditEvent.model_validate_json(row[0]) for row in rows]

    def sample_id_for_run(self, run_id: str) -> str | None:
        row = self._connection.execute("SELECT sample_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return row[0] if row else None
