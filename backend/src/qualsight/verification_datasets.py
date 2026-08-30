"""Import acquired author-published pair protocols without inventing identities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import stat
from typing import Any

from .api.store import RunStore
from .dataset_acquisition import DatasetAcquisitionError, VerificationDatasetStore
from .domain.comparison import (
    ComparisonLabel,
    PairPolicy,
    ProtocolCategory,
    ProtocolPair,
    VerificationProtocolRecord,
)
from .domain.entities import (
    AuditEvent,
    AuditEventType,
    DataClassification,
    Dataset,
    Modality,
    PermissionState,
    Project,
    Sample,
)
from .domain.primitives import (
    ArtifactIdentity,
    CanonicalJson,
    ComponentBinding,
    ComponentState,
    CountEvidence,
    EvidenceState,
    EvidenceText,
)
from .engines.ofiq.adapter import content_id
from .provenance import sha256_bytes, sha256_file


SOURCE_KIND = "MANAGED_PREALIGNED_PAIR_DATASET"
APPROVED_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class VerificationDatasetImportError(RuntimeError):
    """Raised when acquired pair-protocol bytes cannot be recorded faithfully."""


@dataclass(frozen=True, slots=True)
class VerificationDatasetImportResult:
    dataset: Dataset
    protocol: VerificationProtocolRecord
    samples: int
    pairs: int
    statistics_sha256: str


def _recorded_at(identity: ArtifactIdentity) -> datetime:
    return datetime.fromtimestamp(identity.mtime_ns / 1_000_000_000, tz=timezone.utc).astimezone()


def _identity(path: Path) -> ArtifactIdentity:
    observed = path.lstat()
    if not stat.S_ISREG(observed.st_mode):
        raise VerificationDatasetImportError(f"required evidence is not a regular file: {path}")
    return ArtifactIdentity(
        path=str(path),
        sha256=sha256_file(path),
        size_bytes=observed.st_size,
        mtime_ns=observed.st_mtime_ns,
    )


class AcquiredVerificationDatasetImporter:
    """Record one verified pair-only add-on as a project Dataset and protocol."""

    def __init__(
        self,
        acquired: VerificationDatasetStore,
        store: RunStore,
        actor_id: str,
    ) -> None:
        self.acquired = acquired
        self.store = store
        self.actor = actor_id

    def import_addon(self, project: Project, addon_id: str) -> VerificationDatasetImportResult:
        try:
            status = self.acquired.status(addon_id, verify_images=True)
            definition = self.acquired.protocols[addon_id]
        except DatasetAcquisitionError as error:
            raise VerificationDatasetImportError(str(error)) from error
        if status["runtime_state"] != "READY":
            reason = status.get("extraction", {}).get("reason") or "archive bytes are not verified"
            raise VerificationDatasetImportError(
                f"verification dataset {addon_id} is not ready: {reason}; download and verify it in System first"
            )

        root = self.acquired.protocol_root(addon_id)
        image_list = root / "img.list"
        pair_list = root / "pair.list"
        image_list_identity = _identity(image_list)
        pair_list_identity = _identity(pair_list)
        archive_identity = _identity(self.acquired.archive_path)
        manifest_identity = _identity(self.acquired.manifest_path)
        extraction_manifest_identity = _identity(self.acquired.extraction_manifest_path)
        evidence_path = self.acquired.repository_root / self.acquired.archive["evidence_path"]
        authority = [manifest_identity, extraction_manifest_identity, _identity(evidence_path)]
        owner_authorization = (
            self.acquired.repository_root
            / "resources"
            / "authorizations"
            / "qualsight-owner-addon-acquisition-2026-08-28.json"
        )
        if owner_authorization.is_file():
            authority.append(_identity(owner_authorization))

        image_lines = image_list.read_text(encoding="utf-8").splitlines()
        pair_lines = pair_list.read_text(encoding="utf-8").splitlines()
        if len(image_lines) != definition.sample_count or len(pair_lines) != definition.pair_count:
            raise VerificationDatasetImportError("verified pair-dataset list counts changed before import")

        observed_rows: list[dict[str, Any]] = []
        sample_identities: list[tuple[str, int, int]] = []
        for index, relative in enumerate(image_lines):
            expected = f"data/{definition.archive_directory}/imgs/{index}.jpg"
            if relative != expected:
                raise VerificationDatasetImportError(f"image list order changed at row {index}")
            path = root / "imgs" / f"{index}.jpg"
            observed = path.lstat()
            if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
                raise VerificationDatasetImportError(f"pair-dataset image is not a regular file: {path}")
            digest = sha256_file(path)
            observed_rows.append(
                {
                    "index": index,
                    "path": str(path),
                    "sha256": digest,
                    "size_bytes": observed.st_size,
                    "mtime_ns": observed.st_mtime_ns,
                }
            )
            sample_identities.append((digest, observed.st_size, observed.st_mtime_ns))

        ordered_sample_digest = hashlib.sha256()
        for row in observed_rows:
            ordered_sample_digest.update(
                json.dumps(
                    {key: row[key] for key in ("index", "path", "sha256", "size_bytes")},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            ordered_sample_digest.update(b"\n")
        ordered_samples_sha256 = ordered_sample_digest.hexdigest()
        dataset_id = content_id(
            "dataset",
            project.project_id,
            addon_id,
            archive_identity.sha256,
            image_list_identity.sha256,
            ordered_samples_sha256,
        )

        samples: list[Sample] = []
        for row, (digest, size_bytes, mtime_ns) in zip(observed_rows, sample_identities, strict=True):
            sample_id = content_id("sample", dataset_id, str(row["index"]), digest)
            samples.append(
                Sample(
                    sample_id=sample_id,
                    dataset_id=dataset_id,
                    subject_id=EvidenceText(
                        state=EvidenceState.NOT_RECORDED,
                        reason=(
                            "The author-published pair archive supplies pair labels and image indices, "
                            "not subject identities."
                        ),
                    ),
                    modality=Modality.FACE,
                    source_path=str(row["path"]),
                    media_type="image/jpeg",
                    sha256=digest,
                    size_bytes=size_bytes,
                    mtime_ns=mtime_ns,
                    metadata=CanonicalJson.from_value(
                        {
                            "dataset_addon_id": addon_id,
                            "archive_image_index": row["index"],
                            "prealignment": "AUTHOR_PUBLISHED_112X112_IMAGE",
                            "subject_identity_state": "NOT_RECORDED",
                        }
                    ),
                )
            )

        dataset_manifest = CanonicalJson.from_value(
            {
                "source_kind": SOURCE_KIND,
                "dataset_addon_id": addon_id,
                "approved_scopes": [APPROVED_SCOPE],
                "archive": archive_identity.model_dump(mode="json"),
                "image_list": image_list_identity.model_dump(mode="json"),
                "pair_list": pair_list_identity.model_dump(mode="json"),
                "extracted_files": extraction_manifest_identity.model_dump(mode="json"),
                "ordered_samples_sha256": ordered_samples_sha256,
                "sample_count": len(samples),
                "pair_count": len(pair_lines),
                "identity_state": "NOT_RECORDED",
                "selection_rule": "Every image and pair row in the verified author-published protocol lists, in source order.",
            }
        )
        dataset = Dataset(
            dataset_id=dataset_id,
            project_id=project.project_id,
            name=definition.display_name,
            version="1.0.0",
            source_uri=self.acquired.archive["published_url"],
            license=None,
            rights_status="OWNER_APPROVED_LOCAL_NONCOMMERCIAL_RESEARCH_PUBLISHER_DOWNLOAD_NO_REDISTRIBUTION_CLAIM",
            rights_evidence=tuple(authority),
            retrieval=EvidenceText(
                state=EvidenceState.RECORDED,
                value=(
                    f"Verified archive {archive_identity.sha256} acquired from the registered author-published HTTPS endpoint."
                ),
            ),
            manifest=dataset_manifest,
            subject_count=CountEvidence(
                state=EvidenceState.NOT_RECORDED,
                reason="The pair archive does not publish subject identities or a subject count.",
            ),
            sample_count=CountEvidence(state=EvidenceState.RECORDED, value=len(samples)),
            modality=Modality.FACE,
            identity_schema=ComponentBinding(
                state=ComponentState.NOT_RECORDED,
                reason="Only protocol pair labels are published; no subject identity schema is available.",
            ),
            metadata_schema=ComponentBinding(
                state=ComponentState.CONFIGURED,
                component_id="qualsight.author-published-pair-image-index",
                version="1.0.0",
                configuration_sha256=sha256_bytes(
                    b"dataset_addon_id, archive_image_index, prealignment, subject_identity_state"
                ),
            ),
            redistribution_permission=PermissionState.PROHIBITED,
            display_permission=PermissionState.PROHIBITED,
            derived_artifact_policy=(
                "Quality measurements, hashes, and embeddings may be recorded locally for owner-approved research; "
                "source images, crops, and embeddings may not be redistributed."
            ),
            classification=DataClassification.RESTRICTED,
        )

        labels = [line.split() for line in pair_lines]
        if any(len(fields) != 3 or not all(field.isdigit() for field in fields) for fields in labels):
            raise VerificationDatasetImportError("verified pair list changed before import")
        mated_count = sum(1 for fields in labels if fields[2] == "1")
        imported_at = _recorded_at(extraction_manifest_identity)
        statistics = {
            "population": {
                "subjects": {"state": "NOT_RECORDED", "reason": "The archive supplies no subject identities."},
                "samples": len(samples),
            },
            "evaluation_support": {
                "mated_comparisons": mated_count,
                "nonmated_comparisons": len(labels) - mated_count,
                "unique_protocol_samples": len({int(fields[0]) for fields in labels} | {int(fields[1]) for fields in labels}),
                "folds": {"state": "NOT_RECORDED", "reason": "The published pair.list has no fold field."},
            },
            "data_completeness": {
                "missing_files": 0,
                "unreadable_files": 0,
                "hash_mismatches": 0,
                "duplicate_hashes": len(samples) - len({sample.sha256 for sample in samples}),
                "size_mismatches": 0,
                "hashes_recorded_at_import": len(samples),
                "ordered_samples_sha256": ordered_samples_sha256,
            },
            "quality_coverage": {
                "state": "NOT_ASSESSED",
                "reason": "No quality Run has executed over this imported dataset.",
            },
            "computed_at": imported_at.isoformat(),
        }
        statistics_text = json.dumps(
            statistics, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        statistics_sha256 = sha256_bytes(statistics_text.encode("utf-8"))
        dataset_evidence = sha256_bytes(dataset.model_dump_json().encode("utf-8"))
        dataset_event = AuditEvent(
            audit_event_id=content_id(
                "audit-event",
                dataset_id,
                "DATASET_IMPORTED",
                dataset_evidence,
                imported_at.isoformat(),
            ),
            project_id=project.project_id,
            actor_id=self.actor,
            event_type=AuditEventType.DATASET_IMPORTED,
            subject_type="Dataset",
            subject_id=dataset_id,
            occurred_at=imported_at,
            details=CanonicalJson.from_value(
                {
                    "dataset_addon_id": addon_id,
                    "samples": len(samples),
                    "subjects": "NOT_RECORDED",
                    "archive_sha256": archive_identity.sha256,
                    "statistics_sha256": statistics_sha256,
                }
            ),
            current_evidence_sha256=dataset_evidence,
            local_only=True,
        )
        by_index = {int(row["index"]): sample for row, sample in zip(observed_rows, samples, strict=True)}
        protocol_id = content_id(
            "protocol", project.project_id, dataset_id, pair_list_identity.sha256, addon_id
        )
        pairs: list[ProtocolPair] = []
        pair_digest = hashlib.sha256()
        for row_number, fields in enumerate(labels, start=1):
            if len(fields) != 3 or not all(field.isdigit() for field in fields):
                raise VerificationDatasetImportError(f"invalid pair row {row_number}")
            left_index, right_index, published_label = map(int, fields)
            if left_index == right_index:
                raise VerificationDatasetImportError(f"pair row {row_number} compares an image with itself")
            try:
                left = by_index[left_index]
                right = by_index[right_index]
            except KeyError as error:
                raise VerificationDatasetImportError(f"pair row {row_number} references an unknown image") from error
            label = ComparisonLabel.MATED if published_label == 1 else ComparisonLabel.NON_MATED
            pair = ProtocolPair(
                pair_id=content_id(
                    "pair", protocol_id, str(row_number), str(left_index), str(right_index), label.value
                ),
                protocol_id=protocol_id,
                fold=None,
                source_order=row_number,
                label=label,
                left_sample_id=left.sample_id,
                right_sample_id=right.sample_id,
                left_subject_id=None,
                right_subject_id=None,
                left_sha256=left.sha256,
                right_sha256=right.sha256,
            )
            pairs.append(pair)
            pair_digest.update(pair.model_dump_json().encode("utf-8"))
            pair_digest.update(b"\n")

        pair_samples = {pair.left_sample_id for pair in pairs} | {
            pair.right_sample_id for pair in pairs
        }
        protocol = VerificationProtocolRecord(
            protocol_id=protocol_id,
            project_id=project.project_id,
            dataset_id=dataset_id,
            name=f"{definition.display_name} — author-published pre-aligned pair protocol",
            pair_policy=PairPolicy.OFFICIAL_PROTOCOL_LIST,
            category=ProtocolCategory.NOT_APPLICABLE,
            pair_manifest=pair_list_identity,
            protocol_authority=tuple(authority),
            pair_count=len(pairs),
            mated_count=mated_count,
            nonmated_count=len(pairs) - mated_count,
            fold_count=None,
            unique_sample_count=len(pair_samples),
            unique_subject_count=None,
            pairs_sha256=pair_digest.hexdigest(),
            limitations=(
                "Pair labels and image order come from the author-published pair.list without relabelling.",
                "The archive publishes no subject identities; subject-level statistics and subject-cluster uncertainty are not available.",
                "The pair list publishes no fold assignments; fold-based accuracy is not assessed.",
                "The images are author-published pre-aligned 112x112 inputs and execute only through the hash-verified pass-through condition without SCRFD detection or realignment.",
            ),
            created_at=imported_at,
            metadata=CanonicalJson.from_value(
                {
                    "dataset_addon_id": addon_id,
                    "source_pair_list_sha256": pair_list_identity.sha256,
                    "source_order_preserved": True,
                    "subject_identity_state": "NOT_RECORDED",
                    "fold_assignment": "NOT_RECORDED",
                }
            ),
        )
        protocol_evidence = sha256_bytes(protocol.model_dump_json().encode("utf-8"))
        protocol_event = AuditEvent(
            audit_event_id=content_id(
                "audit-event",
                protocol_id,
                "PROTOCOL_FROZEN",
                protocol_evidence,
                protocol.created_at.isoformat(),
            ),
            project_id=project.project_id,
            actor_id=self.actor,
            event_type=AuditEventType.CONFIGURATION_CHANGED,
            subject_type="VerificationProtocol",
            subject_id=protocol_id,
            occurred_at=protocol.created_at,
            details=CanonicalJson.from_value(
                {
                    "dataset_addon_id": addon_id,
                    "pairs": len(pairs),
                    "mated": mated_count,
                    "subjects": "NOT_RECORDED",
                    "pairs_sha256": protocol.pairs_sha256,
                }
            ),
            current_evidence_sha256=protocol_evidence,
            local_only=True,
        )
        self.store.record_verification_dataset_import(
            dataset=dataset,
            samples=samples,
            statistics_json=statistics_text,
            statistics_sha256=statistics_sha256,
            dataset_audit_event=dataset_event,
            protocol=protocol,
            pairs=pairs,
            protocol_audit_event=protocol_event,
        )
        return VerificationDatasetImportResult(
            dataset=dataset,
            protocol=protocol,
            samples=len(samples),
            pairs=len(pairs),
            statistics_sha256=statistics_sha256,
        )
