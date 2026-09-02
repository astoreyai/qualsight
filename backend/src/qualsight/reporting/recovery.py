"""Validation and reconciliation for atomically promoted reporting publications."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterator

from ..domain.entities import AuditEvent, AuditEventType
from ..domain.primitives import CanonicalJson
from ..execution.prepared_output import (
    PREPARED_OUTPUT_FILENAME,
    PreparedOutputError,
    discover_partial_output_identity,
)
from .bundle import recover_prepared_bundle
from .exports import recover_prepared_export
from .figures import recover_prepared_figure
from .reports import recover_prepared_report


class ReportingRecoveryError(RuntimeError):
    """Raised when a promoted reporting receipt cannot be trusted."""


_ROOT_KINDS = {
    "figures": "FIGURE",
    "reports": "REPORT",
    "bundles": "EXPERIMENT_BUNDLE",
    "exports": "TABULAR_EXPORT",
}


def _read_receipt_header(directory: Path) -> dict[str, Any]:
    receipt_path = directory / PREPARED_OUTPUT_FILENAME
    try:
        descriptor = os.open(
            receipt_path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            observed = os.fstat(descriptor)
            if not stat.S_ISREG(observed.st_mode):
                raise ReportingRecoveryError(
                    "prepared reporting receipt is not a regular file"
                )
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
        finally:
            os.close(descriptor)
        receipt = json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        if isinstance(error, ReportingRecoveryError):
            raise
        raise ReportingRecoveryError(
            "prepared reporting receipt is unreadable"
        ) from error
    if not isinstance(receipt, dict) or not isinstance(receipt.get("record"), dict):
        raise ReportingRecoveryError("prepared reporting receipt contract differs")
    return receipt


def _recover_directory(
    directory: Path, expected_kind: str
) -> tuple[str, dict[str, Any]]:
    receipt = _read_receipt_header(directory)
    try:
        operation_kind = str(receipt["operation_kind"])
        result_id = str(receipt["result_id"])
        project_id = str(receipt["project_id"])
        request_sha256 = str(receipt["request_sha256"])
    except KeyError as error:
        raise ReportingRecoveryError(
            "prepared reporting receipt identity is incomplete"
        ) from error
    if operation_kind != expected_kind:
        raise ReportingRecoveryError(
            "prepared reporting receipt is stored under the wrong root"
        )
    try:
        if operation_kind == "FIGURE":
            record = recover_prepared_figure(
                directory,
                figure_id=result_id,
                project_id=project_id,
                request_sha256=request_sha256,
            )
        elif operation_kind == "REPORT":
            record = recover_prepared_report(
                directory,
                report_id=result_id,
                project_id=project_id,
                request_sha256=request_sha256,
            )
        elif operation_kind == "EXPERIMENT_BUNDLE":
            record = recover_prepared_bundle(
                directory,
                bundle_id=result_id,
                project_id=project_id,
                request_sha256=request_sha256,
            )
        elif operation_kind == "TABULAR_EXPORT":
            untrusted_record = receipt["record"]
            record = recover_prepared_export(
                directory,
                export_id=result_id,
                project_id=project_id,
                category=str(untrusted_record["category"]),
                ref=str(untrusted_record["ref"]),
                fmt=str(untrusted_record["format"]),
                request_sha256=request_sha256,
            )
        else:
            raise ReportingRecoveryError(
                "prepared reporting receipt kind is not supported"
            )
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        if isinstance(error, ReportingRecoveryError):
            raise
        raise ReportingRecoveryError(
            "prepared reporting publication failed integrity verification"
        ) from error
    return operation_kind, record


def iter_prepared_reporting_publications(
    data_dir: Path,
) -> Iterator[tuple[str, dict[str, Any], Path]]:
    """Yield every strictly validated, fully promoted reporting publication."""

    for root_name, operation_kind in _ROOT_KINDS.items():
        root = data_dir / root_name
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise ReportingRecoveryError("reporting publication root is invalid")
        for directory in sorted(root.iterdir()):
            if directory.name.startswith(".") and directory.name.endswith(".partial"):
                continue
            if directory.is_symlink():
                raise ReportingRecoveryError(
                    "prepared reporting publication is a symbolic link"
                )
            if not directory.is_dir():
                continue
            if not (directory / PREPARED_OUTPUT_FILENAME).exists():
                continue
            recovered_kind, record = _recover_directory(directory, operation_kind)
            yield recovered_kind, record, directory


def iter_interrupted_reporting_stages(
    data_dir: Path,
) -> Iterator[tuple[str, dict[str, str], Path]]:
    """Yield only strictly identified private reporting stages."""

    for root_name, operation_kind in _ROOT_KINDS.items():
        root = data_dir / root_name
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise ReportingRecoveryError("reporting publication root is invalid")
        for directory in sorted(root.iterdir()):
            if not (
                directory.name.startswith(".")
                and directory.name.endswith(".partial")
            ):
                continue
            try:
                identity = discover_partial_output_identity(directory)
            except PreparedOutputError as error:
                raise ReportingRecoveryError(
                    "interrupted reporting stage failed identity verification"
                ) from error
            if identity["operation_kind"] != operation_kind:
                raise ReportingRecoveryError(
                    "interrupted reporting stage is stored under the wrong root"
                )
            yield operation_kind, identity, directory


def reporting_publication_audit_event(
    publication_kind: str,
    record: dict[str, Any],
    *,
    actor_id: str,
) -> AuditEvent:
    """Build the stable audit identity bound to one immutable publication."""

    if publication_kind == "FIGURE":
        subject_type = "Figure"
        subject_id = record["figure_id"]
        details = {
            "kind": record["kind"],
            "source_run_ids": record["source_run_ids"],
            "files": {
                name: item["sha256"] for name, item in record["files"].items()
            },
        }
    elif publication_kind == "REPORT":
        subject_type = "Report"
        subject_id = record["report_id"]
        details = {
            "title": record["title"],
            "image_policy": record["image_policy"],
            "images_embedded": record["images_embedded"],
            "images_withheld": record["images_withheld"],
            "html_sha256": record["files"]["html"]["sha256"],
            "pdf_sha256": record["files"]["pdf"]["sha256"],
        }
    elif publication_kind == "EXPERIMENT_BUNDLE":
        subject_type = "Bundle"
        subject_id = record["bundle_id"]
        details = {
            "run_id": record["run_id"],
            "sha256": record["sha256"],
            "size_bytes": record["size_bytes"],
        }
    elif publication_kind == "TABULAR_EXPORT":
        subject_type = "Export"
        subject_id = record["export_id"]
        details = {
            "category": record["category"],
            "ref": record["ref"],
            "format": record["format"],
            "rows": record["rows"],
            "sha256": record["sha256"],
            "classification": record["classification"],
        }
    else:
        raise ReportingRecoveryError("reporting publication kind is not supported")
    try:
        occurred_at = datetime.fromisoformat(str(record["created_at"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ReportingRecoveryError(
            "reporting publication creation time is invalid"
        ) from error
    if occurred_at.tzinfo is None:
        raise ReportingRecoveryError(
            "reporting publication creation time lacks a UTC offset"
        )
    event_type = AuditEventType.ARTIFACT_EXPORTED
    payload = CanonicalJson.from_value(
        {
            "event_type": event_type.value,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "occurred_at": occurred_at.isoformat(),
            **details,
        }
    )
    project_id = str(record["project_id"])
    event_id = hashlib.sha256(
        (
            project_id
            + subject_id
            + event_type.value
            + occurred_at.isoformat()
            + payload.sha256
        ).encode("utf-8")
    ).hexdigest()
    return AuditEvent(
        audit_event_id=f"audit-event:sha256:{event_id}",
        project_id=project_id,
        actor_id=actor_id,
        event_type=event_type,
        subject_type=subject_type,
        subject_id=subject_id,
        occurred_at=occurred_at,
        details=payload,
        current_evidence_sha256=payload.sha256,
        local_only=True,
    )


def reconcile_prepared_reporting_publications(
    store: Any,
    *,
    data_dir: Path,
    actor_id: str,
) -> list[dict[str, str]]:
    """Atomically record every valid promoted reporting receipt and audit event."""

    reconciled: list[dict[str, str]] = []
    for publication_kind, record, _directory in iter_prepared_reporting_publications(
        data_dir
    ):
        event = reporting_publication_audit_event(
            publication_kind,
            record,
            actor_id=actor_id,
        )
        recorded_events = [
            recorded
            for recorded in store.audit_events_for(event.subject_id)
            if recorded.audit_event_id == event.audit_event_id
        ]
        if len(recorded_events) > 1:
            raise ReportingRecoveryError(
                "reporting publication has multiple persisted audit identities"
            )
        if recorded_events:
            recorded = recorded_events[0]
            if (
                recorded.project_id != event.project_id
                or recorded.event_type != event.event_type
                or recorded.subject_type != event.subject_type
                or recorded.subject_id != event.subject_id
                or recorded.occurred_at != event.occurred_at
                or recorded.details != event.details
                or recorded.current_evidence_sha256
                != event.current_evidence_sha256
                or recorded.local_only != event.local_only
            ):
                raise ReportingRecoveryError(
                    "persisted reporting audit evidence differs from the prepared receipt"
                )
            event = recorded
        store.record_reporting_publication(publication_kind, record, event)
        reconciled.append(
            {
                "publication_kind": publication_kind,
                "project_id": record["project_id"],
                "subject_id": event.subject_id,
            }
        )
    return reconciled


def prepared_reporting_directories(data_dir: Path, project_id: str) -> list[Path]:
    """Return validated promoted reporting directories owned by one project."""

    promoted = [
        directory
        for _kind, record, directory in iter_prepared_reporting_publications(data_dir)
        if record["project_id"] == project_id
    ]
    interrupted = [
        directory
        for _kind, identity, directory in iter_interrupted_reporting_stages(data_dir)
        if identity["project_id"] == project_id
    ]
    return [*promoted, *interrupted]
