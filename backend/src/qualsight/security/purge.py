"""Application-level verified purge of a project's managed biometric data (PRD §§49–50; QS-H70).

Scope (stated, not implied):

* deleted — every derived artifact file the store records for the project:
  quality-batch Parquet and job directories, matcher comparison/embedding/
  metric files and matrix directories, utility and cross-matcher analysis
  directories, figures, reports, bundles, and exports whose provenance names
  the project's Runs;
* deleted — application-managed sample, result, embedding, comparison,
  analysis, report, and owner-input metadata from SQLite after file deletion;
* retained — the project shell, project version history, audit history, purge
  attestations, and source dataset images that QualSight never owns;
* not performed — secure erasure of the SQLite file's free pages, filesystem
  journals, or backups; that is out of the application's control and is
  described as such in the purge record.

The purge is verified: after deletion every targeted path is re-checked and
the record lists what was deleted, what was already absent, and what could
not be removed.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from typing import Any

from ..engines.ofiq.adapter import content_id
from .owner_file_lock import serialized_owner_file_mutation

PURGE_VERSION = "2.0.0"
SCOPE_STATEMENT = (
    "Application-level verified purge: application-managed sample copies, derivatives, quality results, embeddings, comparisons, analyses, reports, and their scientific metadata are deleted and re-verified; "
    "the project shell, project version history, audit history, and purge attestation remain as tombstones; source dataset images outside QualSight are never touched; SQLite free pages, filesystem journals, and backups are not scrubbed."
)


class PurgeError(RuntimeError):
    """Raised when a purge cannot be executed faithfully."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def purge_targets(store: Any, data_dir: Path, project_id: str) -> dict[str, list[str]]:
    """Enumerate every artifact path and directory the store records for the project."""

    files: set[str] = set()
    directories: set[str] = set()
    for run_id in store.run_ids_for_project(project_id):
        for artifact in store.artifacts_for_run(run_id):
            files.add(artifact.uri)
    for job in store.jobs_for_project(project_id):
        if job.get("parquet_path"):
            files.add(job["parquet_path"])
            directories.add(str(Path(job["parquet_path"]).parent))
    if hasattr(store, "operations_for_project"):
        for operation in store.operations_for_project(project_id):
            if operation.get("output_path"):
                directories.add(operation["output_path"])
    for leaf in store.matcher_leaves(project_id=project_id):
        files.add(leaf["comparisons_parquet"])
        directories.add(str(Path(leaf["comparisons_parquet"]).parent))
        directories.add(str(Path(leaf["comparisons_parquet"]).parent.parent))
    for row in store.utility_analyses(project_id=project_id):
        directories.add(row["directory"])
    for row in store.cross_matcher_analyses(project_id=project_id):
        directories.add(row["directory"])
    for row in store.derived_sets(project_id=project_id):
        directories.add(row["directory"])
    for row in store.degradation_analyses(project_id=project_id):
        directories.add(row["directory"])
    for row in store.identification_searches(project_id=project_id):
        directories.add(row["directory"])
    for row in store.figures(project_id=project_id):
        directories.add(row["directory"])
    for row in store.reports(project_id=project_id):
        directories.add(row["directory"])
    for row in store.experiment_bundles(project_id=project_id):
        files.add(row["path"])
    if hasattr(store, "owner_supplied_for_project"):
        for row in store.owner_supplied_for_project(project_id):
            shared = hasattr(store, "owner_supplied_has_other_project_reference") and store.owner_supplied_has_other_project_reference(row["path"], project_id)
            if not shared:
                files.add(row["path"])  # only unshared copies under the data directory are deletable (filter below)
    project_runs = set(store.run_ids_for_project(project_id))
    exports_root = data_dir / "exports"
    if exports_root.exists():
        for sidecar in exports_root.glob("*/*.provenance.json"):
            try:
                record = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            source = record.get("source", {})
            if source.get("run_id") in project_runs or source.get("project_id") == project_id or record.get("ref") in project_runs:
                directories.add(str(sidecar.parent))
    # Only paths under the data directory are ever deleted.
    files = {path for path in files if _within(Path(path), data_dir)}
    directories = {path for path in directories if _within(Path(path), data_dir)}
    return {"files": sorted(files), "directories": sorted(directories)}


def purge_project(store: Any, *, data_dir: Path, project_id: str, actor: str, confirm: str) -> dict[str, Any]:
    # API job/project coordination never holds its lock across this call.
    # Inside this boundary, RunStore methods acquire their writer lock second.
    with serialized_owner_file_mutation():
        return _purge_project_locked(store, data_dir=data_dir, project_id=project_id, actor=actor, confirm=confirm)


def _purge_project_locked(store: Any, *, data_dir: Path, project_id: str, actor: str, confirm: str) -> dict[str, Any]:
    if confirm != project_id:
        raise PurgeError("purge confirmation must repeat the project id")
    if store.project(project_id) is None:
        raise PurgeError("project is not recorded")
    journal = store.active_purge_journal(project_id)
    if journal is None:
        created_at = _now()
        purge_id = content_id("purge", project_id, created_at)
        journal, _ = store.admit_purge(
            purge_id=purge_id,
            project_id=project_id,
            actor_id=actor,
            targets=purge_targets(store, data_dir, project_id),
        )
    targets = json.loads(journal["targets_json"])
    progress = json.loads(journal["progress_json"])
    deleted = set(progress.get("deleted", []))
    absent = set(progress.get("already_absent", []))
    failures = {item["path"]: item["error"] for item in progress.get("failed", [])}

    def progress_record() -> dict[str, Any]:
        return {
            "deleted": sorted(deleted),
            "already_absent": sorted(absent),
            "failed": [{"path": path, "error": failures[path]} for path in sorted(failures)],
        }

    store.update_purge_journal(journal["purge_id"], state="FILES_IN_PROGRESS", progress=progress_record())
    ordered_targets = [(path, "file") for path in targets["files"]]
    ordered_targets.extend((path, "directory") for path in sorted(targets["directories"], key=len, reverse=True))
    for path_text, target_kind in ordered_targets:
        path = Path(path_text)
        if not _within(path, data_dir):
            failures[path_text] = "journal target no longer resolves within the QualSight data directory"
            store.update_purge_journal(journal["purge_id"], state="FILES_IN_PROGRESS", progress=progress_record())
            continue
        if not path.exists():
            if path_text not in deleted:
                absent.add(path_text)
            failures.pop(path_text, None)
        else:
            try:
                if target_kind == "file":
                    path.unlink()
                else:
                    shutil.rmtree(path)
                deleted.add(path_text)
                absent.discard(path_text)
                failures.pop(path_text, None)
            except OSError as error:
                failures[path_text] = str(error)
        store.update_purge_journal(journal["purge_id"], state="FILES_IN_PROGRESS", progress=progress_record())

    remaining = [p for p in targets["files"] + targets["directories"] if Path(p).exists()]
    metadata = {"removed": {}, "remaining": {}, "verified": False}
    if not remaining and not failures:
        store.update_purge_journal(journal["purge_id"], state="FILES_VERIFIED", progress=progress_record())
        try:
            metadata = store.purge_project_records(project_id)
        except Exception as error:  # noqa: BLE001 - the failed attestation must preserve the exact store reason
            failures[str(store.database_path)] = f"metadata purge failed: {error}"
    progress = progress_record()
    progress["metadata"] = metadata
    verified = not remaining and not failures and metadata["verified"]
    if verified:
        store.update_purge_journal(journal["purge_id"], state="METADATA_VERIFIED", progress=progress)
    else:
        reason = "purge verification failed: " + (
            "; ".join(f"{path}: {error}" for path, error in sorted(failures.items()))
            if failures
            else f"{len(remaining)} target(s) remain"
        )
        store.update_purge_journal(journal["purge_id"], state="FAILED", progress=progress, error=reason)
    record = {
        "purge_id": journal["purge_id"], "version": PURGE_VERSION, "project_id": project_id, "actor_id": journal["actor_id"], "created_at": journal["created_at"],
        "scope": SCOPE_STATEMENT, "targets": targets, "deleted": sorted(deleted), "already_absent": sorted(absent), "failed": progress["failed"], "remaining_after_purge": remaining, "metadata": metadata, "verified": verified,
        "retained": ["project shell and project version history", "local audit history and purge attestations", "source dataset images outside QualSight (never owned by QualSight)"],
        "not_performed": ["secure erasure of SQLite free pages", "filesystem journal or backup scrubbing"],
        "os_pid": os.getpid(),
    }
    if verified:
        store.record_purge_and_complete(record, progress)
    return record


def reconcile_incomplete_purges(store: Any, *, data_dir: Path) -> list[dict[str, Any]]:
    """Resume every previously confirmed purge before the API admits new work."""

    reconciled: list[dict[str, Any]] = []
    for journal in store.incomplete_purge_journals():
        record = purge_project(
            store,
            data_dir=data_dir,
            project_id=journal["project_id"],
            actor=journal["actor_id"],
            confirm=journal["project_id"],
        )
        if not record["verified"]:
            raise PurgeError(
                f"incomplete purge {record['purge_id']} for project {record['project_id']} did not verify during startup"
            )
        reconciled.append(record)
    return reconciled
