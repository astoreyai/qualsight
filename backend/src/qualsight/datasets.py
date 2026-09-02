"""Real dataset import from registered resources (PRD §§10–12, QS-B11).

The importer never invents a sample: it reads the exact registered image
manifest of a resource (path, SHA-256, size for every file), verifies each file
in place, derives the identity mapping from the dataset's own layout (LFW:
``<subject>/<subject>_<index>.jpg``), records the dataset with its factual
rights position, computes the PRD §11 statistics that can be computed without
a detector, and freezes a deterministic real-data subset fixture whose
selection rule and provenance are recorded.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import getpass
import hashlib
import json
from pathlib import Path
import statistics
import struct

from .api.store import RunStore
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
    ArtifactIdentity as DomainArtifactIdentity,
    CanonicalJson,
    ComponentBinding,
    ComponentState,
    CountEvidence,
    EvidenceState,
    EvidenceText,
)
from .engines.ofiq.adapter import content_id
from .execution.registry_binding import RegisteredResource, ResourceRegistry
from .provenance import sha256_bytes, sha256_file

LFW_RESOURCE_ID = "lfw-funneled-local-candidate"


class DatasetImportError(RuntimeError):
    """Raised when a registered dataset cannot be imported faithfully."""


@dataclass(frozen=True, slots=True)
class ImportedSample:
    sample: Sample
    subject_id: str


@dataclass(frozen=True, slots=True)
class DatasetImportResult:
    dataset: Dataset
    statistics: dict[str, object]
    statistics_sha256: str
    sample_count: int
    subject_count: int
    subset_fixture: dict[str, object]
    audit_event: AuditEvent


def _jpeg_header(path: Path) -> tuple[int, int] | None:
    """Return (width, height) from the JPEG frame header, or None if unreadable."""

    try:
        with path.open("rb") as handle:
            data = handle.read(65536)
    except OSError:
        return None
    if data[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xC0, 0xC1, 0xC2}:
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        index += 2 + length
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


class RegisteredDatasetImporter:
    def __init__(self, registry: ResourceRegistry, store: RunStore, actor_id: str | None = None) -> None:
        self.registry = registry
        self.store = store
        self.actor = actor_id or f"local-user:{getpass.getuser()}"

    # -- registered inputs -----------------------------------------------------

    def _lfw_artifacts(self, resource: RegisteredResource) -> tuple[Path, Path]:
        image_manifest = None
        pair_manifest = None
        for artifact in resource.artifacts:
            path = artifact.identity.path or ""
            if artifact.role == "MANIFEST" and path.endswith("lfw-funneled-images-v1.jsonl"):
                image_manifest = artifact.identity
            if artifact.role == "PROTOCOL" and path.endswith("lfw-local-pairs-v1.jsonl"):
                pair_manifest = artifact.identity
        if image_manifest is None or pair_manifest is None:
            raise DatasetImportError("the LFW resource does not register both the image and pair manifests")
        for identity in (image_manifest, pair_manifest):
            actual = sha256_file(Path(identity.path))
            if actual != identity.sha256:
                raise DatasetImportError(f"registered manifest drifted: {identity.path}")
        return Path(image_manifest.path), Path(pair_manifest.path)

    # -- import ----------------------------------------------------------------

    def import_lfw(self, project: Project, *, verify_hashes: bool = True) -> DatasetImportResult:
        resource = self.registry.resource(LFW_RESOURCE_ID)
        if resource.state not in {"APPROVED_LOCAL_RESEARCH_USE", "VERIFIED_LIMITED_USE"}:
            raise DatasetImportError(f"LFW resource state {resource.state} does not permit import")
        raw = self.registry.raw_resource(LFW_RESOURCE_ID)
        tree_checks = [
            check for check in raw.get("checks", []) if check.get("type") == "tree_manifest"
        ]
        if len(tree_checks) != 1 or not isinstance(tree_checks[0].get("root"), str):
            raise DatasetImportError("the LFW resource does not register one tree root")
        lfw_tree_root = Path(tree_checks[0]["root"])
        if not lfw_tree_root.is_absolute():
            raise DatasetImportError("the registered LFW tree root is not absolute")
        image_manifest_path, pair_manifest_path = self._lfw_artifacts(resource)
        image_manifest_identity = DomainArtifactIdentity(
            path=str(image_manifest_path),
            sha256=sha256_file(image_manifest_path),
            size_bytes=image_manifest_path.stat().st_size,
            mtime_ns=image_manifest_path.stat().st_mtime_ns,
        )
        pair_manifest_identity = DomainArtifactIdentity(
            path=str(pair_manifest_path),
            sha256=sha256_file(pair_manifest_path),
            size_bytes=pair_manifest_path.stat().st_size,
            mtime_ns=pair_manifest_path.stat().st_mtime_ns,
        )
        rows = [json.loads(line) for line in image_manifest_path.read_text(encoding="utf-8").splitlines() if line]
        if not rows:
            raise DatasetImportError("the registered image manifest is empty")

        dataset_id = content_id("dataset", project.project_id, LFW_RESOURCE_ID, raw["version"], image_manifest_identity.sha256)
        manifest_descriptor = CanonicalJson.from_value(
            {
                "dataset_resource_id": LFW_RESOURCE_ID,
                "dataset_version": raw["version"],
                "manifest_version": "1.0.0",
                "image_manifest": image_manifest_identity.model_dump(mode="json"),
                "pair_manifest": pair_manifest_identity.model_dump(mode="json"),
                "tree_root": str(lfw_tree_root),
                "identity_rule": "subject_id is the immediate parent directory name of each image (LFW layout <subject>/<subject>_<index>.jpg)",
                "sample_count": len(rows),
                "selection_rule": "Every image row of the registered image manifest; no filtering.",
            }
        )

        # Verify every file in place and build samples + statistics.
        missing_files: list[str] = []
        size_mismatch: list[str] = []
        hash_mismatch: list[str] = []
        unreadable: list[str] = []
        per_subject: Counter[str] = Counter()
        hash_counter: Counter[str] = Counter()
        path_counter: Counter[str] = Counter()
        widths: Counter[int] = Counter()
        heights: Counter[int] = Counter()
        samples: list[Sample] = []
        for row in rows:
            relative = row["path"]
            path_counter[relative] += 1
            absolute = lfw_tree_root / relative
            subject_id = Path(relative).parent.name
            if not subject_id:
                raise DatasetImportError(f"row without an identity directory: {relative}")
            per_subject[subject_id] += 1
            hash_counter[row["sha256"]] += 1
            try:
                stat = absolute.stat()
            except OSError:
                missing_files.append(relative)
                continue
            if stat.st_size != row["size_bytes"]:
                size_mismatch.append(relative)
            if verify_hashes and sha256_file(absolute) != row["sha256"]:
                hash_mismatch.append(relative)
            dims = _jpeg_header(absolute)
            if dims is None:
                unreadable.append(relative)
                metadata = {"resource_id": LFW_RESOURCE_ID, "subject_id": subject_id, "readable_header": False}
            else:
                widths[dims[0]] += 1
                heights[dims[1]] += 1
                metadata = {
                    "resource_id": LFW_RESOURCE_ID,
                    "subject_id": subject_id,
                    "width": dims[0],
                    "height": dims[1],
                    "readable_header": True,
                }
            samples.append(
                Sample(
                    sample_id=content_id("sample", dataset_id, row["sha256"], relative),
                    dataset_id=dataset_id,
                    subject_id=EvidenceText(state=EvidenceState.RECORDED, value=subject_id),
                    modality=Modality.FACE,
                    source_path=str(absolute),
                    media_type="image/jpeg",
                    sha256=row["sha256"],
                    size_bytes=row["size_bytes"],
                    mtime_ns=stat.st_mtime_ns,
                    metadata=CanonicalJson.from_value(metadata),
                )
            )
        if missing_files or size_mismatch or hash_mismatch:
            raise DatasetImportError(
                f"registered LFW bytes are not intact: missing={len(missing_files)} "
                f"size_mismatch={len(size_mismatch)} hash_mismatch={len(hash_mismatch)}"
            )

        # Pair counts from the registered pair manifest (evaluation support).
        mated = nonmated = 0
        pair_samples: set[str] = set()
        with pair_manifest_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("record_type") != "PAIR":
                    continue
                if record["label"] == "MATED":
                    mated += 1
                else:
                    nonmated += 1
                pair_samples.add(record["left_path"])
                pair_samples.add(record["right_path"])

        counts = sorted(per_subject.values())
        duplicate_hashes = {digest: count for digest, count in hash_counter.items() if count > 1}
        duplicate_paths = {path: count for path, count in path_counter.items() if count > 1}
        stats = {
            "population": {
                "subjects": len(per_subject),
                "samples": len(rows),
                "samples_per_subject_mean": round(len(rows) / len(per_subject), 4),
                "samples_per_subject_median": statistics.median(counts),
                "samples_per_subject_min": counts[0],
                "samples_per_subject_max": counts[-1],
                "subjects_with_at_least_two_samples": sum(1 for value in counts if value >= 2),
            },
            "evaluation_support": {
                "mated_comparisons": mated,
                "nonmated_comparisons": nonmated,
                "unique_protocol_samples": len(pair_samples),
                "probes": {"state": "NOT_ASSESSED", "reason": "LFW View 2 defines pairs, not a probe/gallery split."},
                "gallery_size": {"state": "NOT_ASSESSED", "reason": "LFW View 2 defines pairs, not a probe/gallery split."},
                "enrollment_template_count": {"state": "NOT_ASSESSED", "reason": "No template has been generated by a matcher Run yet."},
            },
            "data_completeness": {
                "missing_files": len(missing_files),
                "missing_identity_labels": 0,
                "duplicate_files": len(duplicate_paths),
                "duplicate_hashes": len(duplicate_hashes),
                "duplicate_hash_groups": sorted(duplicate_hashes.items())[:50],
                "unreadable_files": len(unreadable),
                "unreadable_examples": unreadable[:20],
                "size_mismatches": len(size_mismatch),
                "hash_mismatches": len(hash_mismatch),
                "hashes_verified": bool(verify_hashes),
                "images_without_detected_faces": {"state": "NOT_ASSESSED", "reason": "No detector Run has executed over this dataset."},
                "images_with_multiple_detected_faces": {"state": "NOT_ASSESSED", "reason": "No detector Run has executed over this dataset."},
            },
            "quality_coverage": {
                "state": "NOT_ASSESSED",
                "reason": "No quality Run has executed over this dataset yet (QS-B12).",
            },
            "image_dimensions": {
                "widths": sorted(widths.items()),
                "heights": sorted(heights.items()),
            },
            "computed_at": _now().isoformat(),
        }
        stats_text = json.dumps(stats, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        stats_sha = sha256_bytes(stats_text.encode("utf-8"))

        rights_evidence = tuple(
            DomainArtifactIdentity(
                path=item.identity.path,
                sha256=item.identity.sha256,
                size_bytes=item.identity.size_bytes,
                mtime_ns=item.identity.mtime_ns,
            )
            for item in resource.artifacts
            if item.role in {"AUTHORIZATION", "PROTOCOL"} and item.identity.path and item.identity.path.endswith((".json", ".pdf"))
        )
        dataset = Dataset(
            dataset_id=dataset_id,
            project_id=project.project_id,
            name=raw["name"],
            version=raw["version"],
            source_uri=raw["source"]["uri"],
            license=None,
            rights_status=raw["third_party_rights_status"],
            rights_evidence=rights_evidence,
            retrieval=EvidenceText(
                state=EvidenceState.NOT_RECORDED,
                reason="The registered archive's historical acquisition provenance is not reconstructed; its SHA-256 identity is registered.",
            ),
            manifest=manifest_descriptor,
            subject_count=CountEvidence(state=EvidenceState.RECORDED, value=len(per_subject)),
            sample_count=CountEvidence(state=EvidenceState.RECORDED, value=len(rows)),
            modality=Modality.FACE,
            identity_schema=ComponentBinding(
                state=ComponentState.CONFIGURED,
                component_id="lfw-directory-identity",
                version="1.0.0",
                configuration_sha256=sha256_bytes(b"subject_id := parent directory name of the image path"),
            ),
            metadata_schema=ComponentBinding(
                state=ComponentState.CONFIGURED,
                component_id="lfw-sample-metadata",
                version="1.0.0",
                configuration_sha256=sha256_bytes(b"resource_id, subject_id, width, height, readable_header"),
            ),
            redistribution_permission=PermissionState.PROHIBITED,
            display_permission=PermissionState.LIMITED,
            derived_artifact_policy=(
                "Quality measurements, hashes, crops, and embeddings may be recorded locally for owner-approved research; "
                "no image, crop, or embedding leaves the workstation."
            ),
            classification=DataClassification(raw["data_classification"]),
        )
        subset_fixture = self.subset_fixture(pair_manifest_path, image_manifest_identity, pair_manifest_identity, rows)
        evidence = sha256_bytes(dataset.model_dump_json().encode("utf-8"))
        occurred_at = _now()
        audit_event = AuditEvent(
            audit_event_id=content_id("audit-event", dataset_id, "DATASET_IMPORTED", evidence, occurred_at.isoformat()),
            project_id=project.project_id,
            actor_id=self.actor,
            event_type=AuditEventType.DATASET_IMPORTED,
            subject_type="Dataset",
            subject_id=dataset_id,
            occurred_at=occurred_at,
            details=CanonicalJson.from_value(
                {
                    "resource_id": LFW_RESOURCE_ID,
                    "samples": len(rows),
                    "subjects": len(per_subject),
                    "statistics_sha256": stats_sha,
                    "image_manifest_sha256": image_manifest_identity.sha256,
                    "hashes_verified": bool(verify_hashes),
                }
            ),
            current_evidence_sha256=evidence,
            local_only=True,
        )
        self.store.record_dataset_import(dataset, samples, stats_text, stats_sha, audit_event)
        return DatasetImportResult(
            dataset=dataset,
            statistics=stats,
            statistics_sha256=stats_sha,
            sample_count=len(rows),
            subject_count=len(per_subject),
            subset_fixture=subset_fixture,
            audit_event=audit_event,
        )

    # -- deterministic real-data subset fixture --------------------------------

    @staticmethod
    def subset_fixture(
        pair_manifest_path: Path,
        image_manifest_identity: DomainArtifactIdentity,
        pair_manifest_identity: DomainArtifactIdentity,
        rows: list[dict],
    ) -> dict[str, object]:
        """Fold-1 first five mated and first five non-mated View-2 pairs → their real images.

        The rule is fixed and documented; the fixture lists paths and digests
        only (no image bytes) so it can be re-derived and re-verified.
        """

        by_path = {row["path"]: row for row in rows}
        mated: list[dict] = []
        nonmated: list[dict] = []
        with pair_manifest_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("record_type") != "PAIR" or record["fold"] != 1:
                    continue
                bucket = mated if record["label"] == "MATED" else nonmated
                if len(bucket) < 5:
                    bucket.append(record)
                if len(mated) == 5 and len(nonmated) == 5:
                    break
        pairs = mated + nonmated
        images: dict[str, dict] = {}
        for record in pairs:
            for side in ("left", "right"):
                relative = record[f"{side}_path"]
                row = by_path[relative]
                images[relative] = {
                    "path": relative,
                    "sha256": row["sha256"],
                    "size_bytes": row["size_bytes"],
                    "subject_id": Path(relative).parent.name,
                }
        return {
            "fixture_id": "lfw-view2-fold1-first5-mated-first5-nonmated-v1",
            "selection_rule": (
                "From the registered LFW pair manifest take View 2 fold 1 pairs in source order: the first five MATED and the "
                "first five NON_MATED pairs; the fixture is the set of images those ten pairs reference."
            ),
            "provenance": {
                "image_manifest": image_manifest_identity.model_dump(mode="json"),
                "pair_manifest": pair_manifest_identity.model_dump(mode="json"),
                "resource_id": LFW_RESOURCE_ID,
            },
            "allowed_use": "Owner-approved local noncommercial research (QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1); never redistributed.",
            "pairs": [
                {"pair_id": r["pair_id"], "label": r["label"], "fold": r["fold"], "left_path": r["left_path"], "right_path": r["right_path"]}
                for r in pairs
            ],
            "images": sorted(images.values(), key=lambda item: item["path"]),
        }
