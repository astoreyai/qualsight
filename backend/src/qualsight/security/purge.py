"""Application-level verified purge of a project's derived artifacts (PRD §§49–50; QS-H70).

Scope (stated, not implied):

* deleted — every derived artifact file the store records for the project:
  quality-batch Parquet and job directories, matcher comparison/embedding/
  metric files and matrix directories, utility and cross-matcher analysis
  directories, figures, reports, bundles, and exports whose provenance names
  the project's Runs;
* retained — the immutable metadata records (projects, Runs, audit events,
  analysis summaries) as tombstones so the ledger of what existed stays
  auditable; the source dataset images, which QualSight never owns;
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

PURGE_VERSION = "1.0.0"
SCOPE_STATEMENT = (
    "Application-level verified purge: derived artifact files recorded for the project under the data directory are deleted and their absence re-verified; "
    "immutable metadata records remain as tombstones; source dataset images are never touched; SQLite free pages, filesystem journals, and backups are not scrubbed."
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
    for leaf in store.matcher_leaves(project_id=project_id):
        files.add(leaf["comparisons_parquet"])
        directories.add(str(Path(leaf["comparisons_parquet"]).parent))
        directories.add(str(Path(leaf["comparisons_parquet"]).parent.parent))
    for row in store.utility_analyses(project_id=project_id):
        directories.add(row["directory"])
    for row in store.cross_matcher_analyses(project_id=project_id):
        directories.add(row["directory"])
    for row in store.figures(project_id=project_id):
        directories.add(row["directory"])
    for row in store.reports(project_id=project_id):
        directories.add(row["directory"])
    for row in store.experiment_bundles(project_id=project_id):
        files.add(row["path"])
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
    if confirm != project_id:
        raise PurgeError("purge confirmation must repeat the project id")
    if store.project(project_id) is None:
        raise PurgeError("project is not recorded")
    targets = purge_targets(store, data_dir, project_id)
    deleted, absent, failed = [], [], []
    for path_text in targets["files"]:
        path = Path(path_text)
        if not path.exists():
            absent.append(path_text)
            continue
        try:
            path.unlink()
            deleted.append(path_text)
        except OSError as error:
            failed.append({"path": path_text, "error": str(error)})
    for path_text in sorted(targets["directories"], key=len, reverse=True):
        path = Path(path_text)
        if not path.exists():
            absent.append(path_text)
            continue
        try:
            shutil.rmtree(path)
            deleted.append(path_text)
        except OSError as error:
            failed.append({"path": path_text, "error": str(error)})
    remaining = [p for p in targets["files"] + targets["directories"] if Path(p).exists()]
    verified = not remaining and not failed
    record = {
        "purge_id": content_id("purge", project_id, _now()), "version": PURGE_VERSION, "project_id": project_id, "actor_id": actor, "created_at": _now(),
        "scope": SCOPE_STATEMENT, "targets": targets, "deleted": deleted, "already_absent": absent, "failed": failed, "remaining_after_purge": remaining, "verified": verified,
        "retained": ["immutable metadata records (projects, runs, audit events, analysis summaries) as tombstones", "source dataset images (never owned by QualSight)"],
        "not_performed": ["secure erasure of SQLite free pages", "filesystem journal or backup scrubbing"],
        "os_pid": os.getpid(),
    }
    store.record_purge(record)
    return record
