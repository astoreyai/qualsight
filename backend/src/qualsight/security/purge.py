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
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any

from ..domain.primitives import CanonicalJson
from ..engines.ofiq.adapter import content_id
from ..execution.prepared_output import (
    PREPARED_OUTPUT_FILENAME,
    STAGING_IDENTITY_FILENAME,
    PreparedOutputError,
    discover_partial_output_identity,
)
from ..importers.owner_supplied import (
    OwnerSuppliedError,
    inspect_prepared_owner_input_admission,
    inspect_prepared_owner_protocol,
    prepared_owner_input_admissions,
    prepared_owner_protocols,
)
from ..reporting.recovery import prepared_reporting_directories
from .owner_file_lock import serialized_owner_file_mutation

PURGE_VERSION = "2.0.0"
SCOPE_STATEMENT = (
    "Application-level verified purge: application-managed sample copies, derivatives, quality results, embeddings, comparisons, analyses, reports, and their scientific metadata are deleted and re-verified; "
    "the project shell, project version history, audit history, and purge attestation remain as tombstones; source dataset images outside QualSight are never touched; SQLite free pages, filesystem journals, and backups are not scrubbed."
)


class PurgeError(RuntimeError):
    """Raised when a purge cannot be executed faithfully."""


PARTIAL_OUTPUT_ROOT_KINDS = {
    "direct-pair-comparisons": {"DIRECT_PAIR_COMPARISON"},
    "derived": {"DERIVED_SET"},
    "analyses": {
        "UTILITY_ANALYSIS",
        "CROSS_MATCHER_ANALYSIS",
        "DEGRADATION_ANALYSIS",
    },
    "identification": {"IDENTIFICATION_SEARCH"},
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _project_partial_output_directories(
    data_dir: Path,
    project_id: str,
) -> set[Path]:
    """Discover only strict, project-bound stages under managed result roots."""

    directories: set[Path] = set()
    for root_name, operation_kinds in PARTIAL_OUTPUT_ROOT_KINDS.items():
        root = data_dir / root_name
        if not root.is_dir() or root.is_symlink():
            continue
        for stage in root.glob(".*.partial"):
            try:
                identity = discover_partial_output_identity(stage)
            except PreparedOutputError:
                continue
            if (
                identity["project_id"] == project_id
                and identity["operation_kind"] in operation_kinds
            ):
                directories.add(stage)
    return directories


def _unmarked_operation_partial_directories(
    data_dir: Path,
    operation: dict[str, Any],
) -> set[Path]:
    """Cover only the pre-marker creation window for an exact admitted target."""

    output_text = operation.get("output_path")
    expected_root = next(
        (
            data_dir / root_name
            for root_name, kinds in PARTIAL_OUTPUT_ROOT_KINDS.items()
            if operation.get("operation_kind") in kinds
        ),
        None,
    )
    if not output_text or expected_root is None:
        return set()
    output = Path(str(output_text))
    if (
        output.parent.resolve() != expected_root.resolve()
        or not _within(output, data_dir)
    ):
        return set()
    partials: set[Path] = set()
    for stage in output.parent.glob(f".{output.name}.*.partial"):
        try:
            observed = stage.lstat()
        except OSError:
            continue
        if stage.is_symlink() or not stage.is_dir():
            continue
        marker = stage / STAGING_IDENTITY_FILENAME
        receipt = stage / PREPARED_OUTPUT_FILENAME
        has_identity_bytes = (
            marker.is_symlink()
            or marker.exists()
            or receipt.is_symlink()
            or receipt.exists()
        )
        if not has_identity_bytes:
            partials.add(stage)
            continue
        try:
            identity = discover_partial_output_identity(stage)
            request_sha256 = CanonicalJson.from_value(
                json.loads(str(operation["request_json"]))
            ).sha256
        except (PreparedOutputError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            partials.add(stage)
            continue
        if identity == {
            "schema_version": identity["schema_version"],
            "target_name": output.name,
            "operation_kind": str(operation["operation_kind"]),
            "result_id": str(operation["result_id"]),
            "project_id": str(operation["project_id"]),
            "request_sha256": request_sha256,
        }:
            partials.add(stage)
    return partials


def _prepared_owner_publication_files(
    store: Any,
    data_dir: Path,
    project_id: str,
) -> set[str]:
    """Discover only canonical owner-publication intents bound to this project."""

    files: set[str] = set()
    for receipt_path in prepared_owner_input_admissions(data_dir):
        try:
            prepared = inspect_prepared_owner_input_admission(
                receipt_path, data_dir=data_dir
            )
        except OwnerSuppliedError:
            continue
        if prepared.project_id != project_id:
            continue
        files.update((str(prepared.receipt_path), str(prepared.stage_path)))
        shared = (
            hasattr(store, "owner_supplied_has_other_project_reference")
            and store.owner_supplied_has_other_project_reference(
                str(prepared.target_path), project_id
            )
        )
        if not shared:
            files.add(str(prepared.target_path))
    for receipt_path in prepared_owner_protocols(data_dir):
        try:
            prepared = inspect_prepared_owner_protocol(
                receipt_path, data_dir=data_dir
            )
        except OwnerSuppliedError:
            continue
        if prepared.record.project_id != project_id:
            continue
        files.update(
            (
                str(prepared.receipt_path),
                str(prepared.stage_path),
                str(prepared.manifest_path),
            )
        )
    return files


def purge_targets(store: Any, data_dir: Path, project_id: str) -> dict[str, list[str]]:
    """Enumerate every artifact path and directory the store records for the project."""

    files: set[str] = set()
    directories: set[str] = set()
    files.update(_prepared_owner_publication_files(store, data_dir, project_id))
    directories.update(
        str(path)
        for path in prepared_reporting_directories(data_dir, project_id)
    )
    directories.update(
        str(path)
        for path in _project_partial_output_directories(data_dir, project_id)
    )
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
            directories.update(
                str(path)
                for path in _unmarked_operation_partial_directories(
                    data_dir, operation
                )
            )
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
    if hasattr(store, "direct_pair_comparisons_for_project"):
        for run in store.direct_pair_comparisons_for_project(project_id):
            directories.add(run.output_directory)
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
    if hasattr(store, "protocols_for_project"):
        for protocol in store.protocols_for_project(project_id):
            try:
                metadata = json.loads(protocol.metadata.text)
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if metadata.get("source_kind") == "OWNER_SUPPLIED_LOCAL_INPUT":
                files.add(protocol.pair_manifest.path)
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


def public_purge_scope(
    project_id: str, targets: dict[str, list[str]]
) -> dict[str, Any]:
    """Return a path-free purge preview for the operator API."""

    return {
        "project_id": project_id,
        "scope": SCOPE_STATEMENT,
        "target_counts": {
            "files": len(targets["files"]),
            "directories": len(targets["directories"]),
        },
    }


def purge_evidence_sha256(record: dict[str, Any]) -> str:
    """Hash the complete private purge attestation without publishing it."""

    canonical = json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def public_purge_record(record: dict[str, Any]) -> dict[str, Any]:
    """Project a private purge attestation into a path-free public receipt."""

    targets = record["targets"]
    metadata = record["metadata"]
    return {
        "purge_id": record["purge_id"],
        "version": record["version"],
        "project_id": record["project_id"],
        "actor_id": record["actor_id"],
        "created_at": record["created_at"],
        "scope": record["scope"],
        "target_counts": {
            "files": len(targets["files"]),
            "directories": len(targets["directories"]),
        },
        "outcome_counts": {
            "deleted": len(record["deleted"]),
            "already_absent": len(record["already_absent"]),
            "reconciled_after_deletion_intent": len(
                record.get("reconciled_after_deletion_intent", [])
            ),
            "failed": len(record["failed"]),
            "remaining_after_purge": len(record["remaining_after_purge"]),
        },
        "metadata": {
            "removed": {
                str(name): int(count)
                for name, count in metadata["removed"].items()
            },
            "remaining": {
                str(name): int(count)
                for name, count in metadata["remaining"].items()
            },
            "verified": bool(metadata["verified"]),
        },
        "verified": bool(record["verified"]),
        "retained": list(record["retained"]),
        "not_performed": list(record["not_performed"]),
        "evidence_sha256": purge_evidence_sha256(record),
    }


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
    deletion_intents = set(progress.get("deletion_intents", []))
    reconciled_after_deletion_intent = set(
        progress.get("reconciled_after_deletion_intent", [])
    )
    failures = {item["path"]: item["error"] for item in progress.get("failed", [])}
    metadata_failure_path = str(store.database_path)
    failures.pop(metadata_failure_path, None)
    recorded_metadata = progress.get("metadata")
    metadata_recorded = isinstance(recorded_metadata, dict)
    metadata = (
        recorded_metadata
        if metadata_recorded
        else {"removed": {}, "remaining": {}, "verified": False}
    )
    failure_evidence_history = [
        item
        for item in progress.get("failure_evidence_history", [])
        if isinstance(item, dict)
    ]

    def progress_record() -> dict[str, Any]:
        current = {
            "deleted": sorted(deleted),
            "already_absent": sorted(absent),
            "deletion_intents": sorted(deletion_intents),
            "reconciled_after_deletion_intent": sorted(
                reconciled_after_deletion_intent
            ),
            "failed": [{"path": path, "error": failures[path]} for path in sorted(failures)],
        }
        if failure_evidence_history:
            current["failure_evidence_history"] = failure_evidence_history
        if metadata_recorded:
            current["metadata"] = metadata
        return current

    file_progress_state = (
        "METADATA_VERIFIED" if metadata.get("verified") is True else "FILES_IN_PROGRESS"
    )
    store.update_purge_journal(
        journal["purge_id"], state=file_progress_state, progress=progress_record()
    )
    ordered_targets = [(path, "file") for path in targets["files"]]
    ordered_targets.extend((path, "directory") for path in sorted(targets["directories"], key=len, reverse=True))
    for path_text, target_kind in ordered_targets:
        path = Path(path_text)
        if not _within(path, data_dir):
            failures[path_text] = "journal target no longer resolves within the QualSight data directory"
            store.update_purge_journal(
                journal["purge_id"], state=file_progress_state, progress=progress_record()
            )
            continue
        if not path.exists():
            if path_text in deletion_intents:
                deletion_intents.discard(path_text)
                reconciled_after_deletion_intent.add(path_text)
                absent.discard(path_text)
            elif (
                path_text not in deleted
                and path_text not in reconciled_after_deletion_intent
            ):
                absent.add(path_text)
            failures.pop(path_text, None)
        else:
            if path_text not in deletion_intents:
                deletion_intents.add(path_text)
                store.update_purge_journal(
                    journal["purge_id"],
                    state=file_progress_state,
                    progress=progress_record(),
                )
            try:
                if target_kind == "file":
                    path.unlink()
                else:
                    shutil.rmtree(path)
                deleted.add(path_text)
                absent.discard(path_text)
                reconciled_after_deletion_intent.discard(path_text)
                deletion_intents.discard(path_text)
                failures.pop(path_text, None)
            except OSError as error:
                failures[path_text] = str(error)
        store.update_purge_journal(
            journal["purge_id"], state=file_progress_state, progress=progress_record()
        )

    remaining = [p for p in targets["files"] + targets["directories"] if Path(p).exists()]
    if not remaining and not failures:
        if metadata.get("verified") is not True:
            store.update_purge_journal(
                journal["purge_id"], state="FILES_VERIFIED", progress=progress_record()
            )
            try:
                metadata = store.purge_project_records(
                    project_id, purge_id=journal["purge_id"]
                )
                metadata_recorded = True
            except Exception as error:  # noqa: BLE001 - the failed attestation must preserve the exact store reason
                failures[metadata_failure_path] = f"metadata purge failed: {error}"
    progress = progress_record()
    progress["metadata"] = metadata
    verified = not remaining and not failures and metadata["verified"]
    record = {
        "purge_id": journal["purge_id"], "version": PURGE_VERSION, "project_id": project_id, "actor_id": journal["actor_id"], "created_at": journal["created_at"],
        "scope": SCOPE_STATEMENT, "targets": targets, "deleted": sorted(deleted), "already_absent": sorted(absent), "reconciled_after_deletion_intent": sorted(reconciled_after_deletion_intent), "failed": progress["failed"], "remaining_after_purge": remaining, "metadata": metadata, "verified": verified,
        "retained": ["project shell and project version history", "local audit history and purge attestations", "source dataset images outside QualSight (never owned by QualSight)"],
        "not_performed": ["secure erasure of SQLite free pages", "filesystem journal or backup scrubbing"],
        "os_pid": os.getpid(),
    }
    if verified:
        store.record_purge_and_complete(record, progress)
    else:
        reason = "purge verification failed: " + (
            "; ".join(f"{path}: {error}" for path, error in sorted(failures.items()))
            if failures
            else f"{len(remaining)} target(s) remain"
        )
        evidence_sha256 = purge_evidence_sha256(record)
        if all(
            purge_evidence_sha256(item) != evidence_sha256
            for item in failure_evidence_history
        ):
            failure_evidence_history.append(record)
        progress["failure_evidence_history"] = failure_evidence_history
        store.update_purge_journal(journal["purge_id"], state="FAILED", progress=progress, error=reason)
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
