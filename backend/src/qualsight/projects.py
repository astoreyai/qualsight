"""Project lifecycle (PRD §§7–9): New, Open, Save, Save As, Close, Clone, Archive.

Projects are append-only versioned records. Every lifecycle transition writes a
new full :class:`~qualsight.domain.entities.Project` version (never rewriting a
previous one) and one audit event. The scientific configuration captured at
creation (registry identity, engine identity and configuration digest, purpose
scope) travels unchanged through every version, Save As, and Clone, and the
immutable Runs recorded under a project stay referenced by their run_ids.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import getpass
import hashlib
import json

from .api.store import ImmutableRecordConflict, RunStore
from .domain.entities import (
    AccessPolicy,
    AuditEvent,
    AuditEventType,
    DataClassification,
    PermissionState,
    Project,
    ProjectLifecycle,
)
from .domain.primitives import CanonicalJson
from .engines.ofiq.adapter import OFIQQualityEngine, content_id

ALLOWED_TRANSITIONS: dict[str, dict[ProjectLifecycle, ProjectLifecycle]] = {
    "open": {
        ProjectLifecycle.NEW: ProjectLifecycle.OPEN,
        ProjectLifecycle.SAVED: ProjectLifecycle.OPEN,
        ProjectLifecycle.ARCHIVED: ProjectLifecycle.OPEN,
        ProjectLifecycle.OPEN: ProjectLifecycle.OPEN,
    },
    "save": {
        ProjectLifecycle.NEW: ProjectLifecycle.SAVED,
        ProjectLifecycle.OPEN: ProjectLifecycle.SAVED,
        ProjectLifecycle.SAVED: ProjectLifecycle.SAVED,
    },
    "close": {
        ProjectLifecycle.OPEN: ProjectLifecycle.SAVED,
        ProjectLifecycle.NEW: ProjectLifecycle.SAVED,
        ProjectLifecycle.SAVED: ProjectLifecycle.SAVED,
    },
    "archive": {
        ProjectLifecycle.NEW: ProjectLifecycle.ARCHIVED,
        ProjectLifecycle.OPEN: ProjectLifecycle.ARCHIVED,
        ProjectLifecycle.SAVED: ProjectLifecycle.ARCHIVED,
    },
}


class ProjectLifecycleError(ValueError):
    """Raised when a transition is not allowed from the current lifecycle state."""


@dataclass(frozen=True, slots=True)
class ProjectView:
    project: Project
    version: int
    run_ids: tuple[str, ...]


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _evidence(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ProjectService:
    def __init__(self, store: RunStore, engine: OFIQQualityEngine, actor_id: str | None = None) -> None:
        self.store = store
        self.engine = engine
        self.actor = actor_id or f"local-user:{getpass.getuser()}"

    # -- scientific configuration captured at creation -------------------------

    def scientific_configuration(self, purpose_scope: str) -> dict[str, object]:
        runtime = self.engine.runtime
        return {
            "registry_id": self.engine.registry.registry_id,
            "registry_sha256": self.engine.registry.file_identity.sha256,
            "quality_engine": {
                "resource_id": runtime.resource_id,
                "version": runtime.version,
                "registry_state": runtime.state,
                "configuration_sha256": self.engine.configuration.sha256,
                "model_hashes": [item.identity.sha256 for item in runtime.artifacts_with_role("MODEL")],
            },
            "purpose_scope": purpose_scope,
            "input_resource_ids": [resource.resource_id for resource in self.engine.input_resources],
        }

    # -- lifecycle -------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        description: str,
        purpose_scope: str,
        tags: tuple[str, ...] = (),
        notes: tuple[str, ...] = (),
        classification: DataClassification = DataClassification.PRIVATE,
        extra_metadata: dict[str, object] | None = None,
    ) -> ProjectView:
        created_at = _now()
        project_id = content_id("project", self.actor, name, created_at.isoformat())
        metadata = {
            "scientific_configuration": self.scientific_configuration(purpose_scope),
            "run_references": [],
            **(extra_metadata or {}),
        }
        project = Project(
            project_id=project_id,
            name=name,
            description=description,
            lifecycle=ProjectLifecycle.NEW,
            owner_user_id=self.actor,
            access_policy=AccessPolicy.LOCAL_SINGLE_USER,
            classification=classification,
            redistribution_permission=PermissionState.PROHIBITED,
            display_permission=PermissionState.LIMITED,
            use_restrictions=tuple(sorted(set(self.engine.runtime.use_restrictions))),
            tags=tuple(tags),
            notes=tuple(notes),
            metadata=CanonicalJson.from_value(metadata),
            created_at=created_at,
            updated_at=created_at,
        )
        version = self.store.record_project_version(project, first=True)
        self._audit(project, AuditEventType.PROJECT_CREATED, {"version": version, "lifecycle": project.lifecycle.value})
        return ProjectView(project=project, version=version, run_ids=())

    def head(self, project_id: str) -> ProjectView:
        found = self.store.project_head(project_id)
        if found is None:
            raise KeyError(project_id)
        project, version = found
        return ProjectView(project=project, version=version, run_ids=tuple(self.store.run_ids_for_project(project_id)))

    def _transition(
        self,
        project_id: str,
        action: str,
        *,
        name: str | None = None,
        description: str | None = None,
        tags: tuple[str, ...] | None = None,
        notes: tuple[str, ...] | None = None,
    ) -> ProjectView:
        view = self.head(project_id)
        current = view.project
        try:
            target = ALLOWED_TRANSITIONS[action][current.lifecycle]
        except KeyError as error:
            raise ProjectLifecycleError(
                f"cannot {action} a project in lifecycle {current.lifecycle.value}"
            ) from error
        metadata = json.loads(current.metadata.text)
        metadata["run_references"] = list(view.run_ids)
        updated = current.model_copy(
            update={
                "lifecycle": target,
                "name": name if name is not None else current.name,
                "description": description if description is not None else current.description,
                "tags": tuple(tags) if tags is not None else current.tags,
                "notes": tuple(notes) if notes is not None else current.notes,
                "metadata": CanonicalJson.from_value(metadata),
                "updated_at": _now(),
            }
        )
        # model_copy skips validation on frozen strict models; re-validate.
        updated = Project.model_validate_json(updated.model_dump_json())
        version = self.store.record_project_version(updated, first=False)
        self._audit(
            updated,
            AuditEventType.CONFIGURATION_CHANGED,
            {"action": action, "version": version, "from": current.lifecycle.value, "to": target.value},
        )
        return ProjectView(project=updated, version=version, run_ids=view.run_ids)

    def open(self, project_id: str) -> ProjectView:
        return self._transition(project_id, "open")

    def save(self, project_id: str, **fields) -> ProjectView:
        return self._transition(project_id, "save", **fields)

    def close(self, project_id: str) -> ProjectView:
        return self._transition(project_id, "close")

    def archive(self, project_id: str) -> ProjectView:
        return self._transition(project_id, "archive")

    def _derive(self, source_id: str, name: str, mode: str) -> ProjectView:
        source = self.head(source_id)
        source_metadata = json.loads(source.project.metadata.text)
        created_at = _now()
        project_id = content_id("project", self.actor, name, created_at.isoformat(), source_id, mode)
        metadata = {
            "scientific_configuration": source_metadata["scientific_configuration"],
            "run_references": [],
            "derived_from": {
                "project_id": source_id,
                "version": source.version,
                "mode": mode,
                "run_references": list(source.run_ids),
            },
        }
        project = Project(
            project_id=project_id,
            name=name,
            description=source.project.description,
            lifecycle=ProjectLifecycle.NEW,
            owner_user_id=self.actor,
            access_policy=source.project.access_policy,
            classification=source.project.classification,
            redistribution_permission=source.project.redistribution_permission,
            display_permission=source.project.display_permission,
            use_restrictions=source.project.use_restrictions,
            tags=source.project.tags,
            notes=source.project.notes,
            metadata=CanonicalJson.from_value(metadata),
            created_at=created_at,
            updated_at=created_at,
        )
        version = self.store.record_project_version(project, first=True)
        self._audit(project, AuditEventType.PROJECT_CREATED, {"version": version, "mode": mode, "source_project_id": source_id})
        return ProjectView(project=project, version=version, run_ids=())

    def clone(self, project_id: str, name: str) -> ProjectView:
        return self._derive(project_id, name, "CLONE")

    def save_as(self, project_id: str, name: str) -> ProjectView:
        return self._derive(project_id, name, "SAVE_AS")

    def require_writable(self, project_id: str) -> Project:
        view = self.head(project_id)
        if view.project.lifecycle is ProjectLifecycle.ARCHIVED:
            raise ProjectLifecycleError("archived projects do not accept new runs; reopen or clone first")
        return view.project

    def _audit(self, project: Project, event_type: AuditEventType, details: dict[str, object]) -> None:
        evidence = _evidence(project.model_dump_json())
        event = AuditEvent(
            audit_event_id=content_id("audit-event", project.project_id, event_type.value, evidence, project.updated_at.isoformat()),
            project_id=project.project_id,
            actor_id=self.actor,
            event_type=event_type,
            subject_type="Project",
            subject_id=project.project_id,
            occurred_at=project.updated_at,
            details=CanonicalJson.from_value(details),
            current_evidence_sha256=evidence,
            local_only=True,
        )
        self.store.record_audit_event(event)


__all__ = ["ProjectService", "ProjectLifecycleError", "ProjectView", "ImmutableRecordConflict"]
