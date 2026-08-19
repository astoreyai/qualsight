"""Owner-supplied local inputs (decision 30; Plan D QS-Y80/Y82/Y83).

An evaluator working at this workstation may bring their own photograph,
directory, or CSV manifest. Admission is the governance boundary:

* Uploaded byte streams are decoded (refused if they are not an image),
  SHA-256 hashed, and stored content-addressed under the data directory.
* Directory / manifest files are hashed and recorded in place; every path must
  stay inside the declared root — symlinks and rows escaping the root are
  refused, never silently skipped.
* Every admitted input carries source kind ``OWNER_SUPPLIED_LOCAL_INPUT``, the
  attributed operator, and the admitting project; the batch engine admits an
  owner-supplied path only when the store records exactly that path with a
  matching current hash. Nothing here is a registered, rights-reviewed
  resource, and results must always say so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random

import cv2
import numpy as np

from ..api.store import RunStore
from ..domain.comparison import (
    ComparisonLabel,
    PairPolicy,
    ProtocolCategory,
    ProtocolPair,
    VerificationProtocolRecord,
)
from ..domain.entities import (
    AuditEvent,
    AuditEventType,
    DataClassification,
    Dataset,
    Modality,
    PermissionState,
    Project,
    Sample,
)
from ..domain.primitives import (
    ArtifactIdentity,
    CanonicalJson,
    ComponentBinding,
    ComponentState,
    CountEvidence,
    EvidenceState,
    EvidenceText,
)
from ..engines.ofiq.adapter import content_id
from ..provenance import sha256_bytes, sha256_file

SOURCE_KIND = "OWNER_SUPPLIED_LOCAL_INPUT"
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_DECODED_PIXELS = 96_000_000  # decode-bomb guard: refuse > 96 MP images
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".ppm"}


class OwnerSuppliedError(RuntimeError):
    """Raised when an owner-supplied input cannot be admitted honestly."""


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _decode_check(data: bytes, name: str) -> tuple[int, int]:
    if len(data) > MAX_UPLOAD_BYTES:
        raise OwnerSuppliedError(f"{name}: {len(data)} bytes exceeds the {MAX_UPLOAD_BYTES}-byte upload cap")
    array = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise OwnerSuppliedError(f"{name}: the bytes do not decode as an image")
    height, width = image.shape[:2]
    if height * width > MAX_DECODED_PIXELS:
        raise OwnerSuppliedError(f"{name}: decoded size {width}x{height} exceeds the decode guard")
    return width, height


@dataclass(frozen=True, slots=True)
class AdmittedInput:
    sha256: str
    path: str
    size_bytes: int
    width: int
    height: int
    original_name: str
    storage: str  # COPIED | IN_PLACE


class OwnerSuppliedAdmission:
    """Decision-30 admission of operator-picked local inputs."""

    def __init__(self, store: RunStore, data_dir: Path, actor_id: str) -> None:
        self.store = store
        self.data_dir = data_dir
        self.actor = actor_id
        self.storage_root = data_dir / "owner_supplied"

    def _record(self, admitted: AdmittedInput, project_id: str) -> AdmittedInput:
        self.store.record_owner_supplied(
            {
                "path": admitted.path,
                "sha256": admitted.sha256,
                "project_id": project_id,
                "actor_id": self.actor,
                "original_name": admitted.original_name,
                "size_bytes": admitted.size_bytes,
                "width": admitted.width,
                "height": admitted.height,
                "storage": admitted.storage,
                "source_kind": SOURCE_KIND,
                "admitted_at": _now().isoformat(),
            }
        )
        return admitted

    def admit_bytes(self, data: bytes, original_name: str, project_id: str) -> AdmittedInput:
        width, height = _decode_check(data, original_name)
        digest = hashlib.sha256(data).hexdigest()
        suffix = Path(original_name).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            suffix = ".jpg"
        self.storage_root.mkdir(parents=True, exist_ok=True)
        target = self.storage_root / f"{digest}{suffix}"
        if not target.exists():
            target.write_bytes(data)
        admitted = AdmittedInput(
            sha256=digest,
            path=str(target),
            size_bytes=len(data),
            width=width,
            height=height,
            original_name=original_name,
            storage="COPIED",
        )
        return self._record(admitted, project_id)

    def admit_path(self, path: Path, root: Path, project_id: str) -> AdmittedInput:
        resolved_root = root.resolve(strict=True)
        if path.is_symlink():
            raise OwnerSuppliedError(f"{path}: symlinks are not admitted")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root):
            raise OwnerSuppliedError(f"{path}: resolves outside the declared root {root}")
        data = resolved.read_bytes()
        width, height = _decode_check(data, str(path))
        admitted = AdmittedInput(
            sha256=hashlib.sha256(data).hexdigest(),
            path=str(resolved),
            size_bytes=len(data),
            width=width,
            height=height,
            original_name=resolved.name,
            storage="IN_PLACE",
        )
        return self._record(admitted, project_id)


@dataclass(frozen=True, slots=True)
class OwnerImportResult:
    dataset: Dataset
    sample_count: int
    subject_count: int
    statistics_sha256: str


class OwnerSuppliedImporter:
    """Identity-labelled datasets from an operator directory or CSV manifest (QS-Y82)."""

    def __init__(self, store: RunStore, data_dir: Path, actor_id: str) -> None:
        self.store = store
        self.admission = OwnerSuppliedAdmission(store, data_dir, actor_id)
        self.actor = actor_id

    # -- collection ------------------------------------------------------------

    def _collect_directory(self, root: Path) -> list[tuple[str, Path]]:
        if not root.is_dir():
            raise OwnerSuppliedError(f"{root} is not a directory")
        rows: list[tuple[str, Path]] = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise OwnerSuppliedError(f"{path}: symlinks are not admitted")
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            subject = path.parent.name if path.parent != root else ""
            if not subject:
                raise OwnerSuppliedError(
                    f"{path}: images must sit in a subject directory (subject = immediate folder name)"
                )
            rows.append((subject, path))
        if not rows:
            raise OwnerSuppliedError(f"{root} contains no images in subject directories")
        return rows

    def _collect_manifest(self, manifest_path: Path, mapping: dict[str, str], root: Path) -> list[tuple[str, Path]]:
        import csv as _csv

        for field in ("subject_id", "path"):
            if field not in mapping or not mapping[field]:
                raise OwnerSuppliedError(f"the field mapping must name a source column for {field}")
        rows: list[tuple[str, Path]] = []
        with manifest_path.open("r", newline="", encoding="utf-8") as handle:
            reader = _csv.DictReader(handle)
            if reader.fieldnames is None:
                raise OwnerSuppliedError("the manifest has no header row")
            for field, column in mapping.items():
                if column not in reader.fieldnames:
                    raise OwnerSuppliedError(f"mapped column {column} ({field}) is not in the manifest header")
            for index, row in enumerate(reader, start=2):
                subject = (row.get(mapping["subject_id"]) or "").strip()
                raw_path = (row.get(mapping["path"]) or "").strip()
                if not subject or not raw_path:
                    raise OwnerSuppliedError(f"manifest line {index}: empty subject or path")
                path = Path(raw_path)
                if not path.is_absolute():
                    path = root / path
                rows.append((subject, path))
        if not rows:
            raise OwnerSuppliedError("the manifest has no data rows")
        return rows

    # -- import ----------------------------------------------------------------

    def import_rows(
        self,
        project: Project,
        rows: list[tuple[str, Path]],
        *,
        root: Path,
        name: str,
        origin: dict[str, object],
    ) -> OwnerImportResult:
        admitted: list[tuple[str, AdmittedInput]] = []
        for subject, path in rows:
            admitted.append((subject, self.admission.admit_path(path, root, project.project_id)))
        subjects = sorted({subject for subject, _ in admitted})
        digest = hashlib.sha256()
        for subject, item in admitted:
            digest.update(f"{subject}\t{item.sha256}\t{item.path}\n".encode("utf-8"))
        manifest_sha = digest.hexdigest()
        dataset_id = content_id(
            "dataset", project.project_id, SOURCE_KIND, manifest_sha, name,
            json.dumps(origin, sort_keys=True, separators=(",", ":")),
        )
        manifest_descriptor = CanonicalJson.from_value(
            {
                "source_kind": SOURCE_KIND,
                "manifest_version": "1.0.0",
                "tree_root": str(root),
                "identity_rule": "subject_id supplied by the operator (directory name or mapped manifest column)",
                "sample_count": len(admitted),
                "content_sha256": manifest_sha,
                "origin": origin,
                "admitting_actor": self.actor,
            }
        )
        samples: list[Sample] = []
        for subject, item in admitted:
            samples.append(
                Sample(
                    sample_id=content_id("sample", dataset_id, item.sha256, item.path),
                    dataset_id=dataset_id,
                    subject_id=EvidenceText(state=EvidenceState.RECORDED, value=subject),
                    modality=Modality.FACE,
                    source_path=item.path,
                    media_type="image/jpeg" if item.path.lower().endswith((".jpg", ".jpeg")) else "image/png",
                    sha256=item.sha256,
                    size_bytes=item.size_bytes,
                    mtime_ns=Path(item.path).stat().st_mtime_ns,
                    metadata=CanonicalJson.from_value(
                        {
                            "source_kind": SOURCE_KIND,
                            "subject_id": subject,
                            "width": item.width,
                            "height": item.height,
                            "storage": item.storage,
                        }
                    ),
                )
            )
        per_subject: dict[str, int] = {}
        for subject, _ in admitted:
            per_subject[subject] = per_subject.get(subject, 0) + 1
        stats = {
            "population": {
                "subjects": len(subjects),
                "samples": len(admitted),
                "samples_per_subject_min": min(per_subject.values()),
                "samples_per_subject_max": max(per_subject.values()),
            },
            "provenance": {
                "source_kind": SOURCE_KIND,
                "rights_review": "NOT_PERFORMED — operator-supplied local input under decision 30",
                "admitting_actor": self.actor,
            },
            "computed_at": _now().isoformat(),
        }
        stats_text = json.dumps(stats, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        stats_sha = sha256_bytes(stats_text.encode("utf-8"))
        dataset = Dataset(
            dataset_id=dataset_id,
            project_id=project.project_id,
            name=name,
            version="1.0.0",
            source_uri=f"file://{root}",
            license=None,
            rights_status="OWNER_SUPPLIED_UNREVIEWED",
            rights_evidence=(),
            retrieval=EvidenceText(
                state=EvidenceState.RECORDED,
                value="Supplied interactively by the attributed operator on this workstation (decision 30); no third-party rights review performed.",
            ),
            manifest=manifest_descriptor,
            subject_count=CountEvidence(state=EvidenceState.RECORDED, value=len(subjects)),
            sample_count=CountEvidence(state=EvidenceState.RECORDED, value=len(admitted)),
            modality=Modality.FACE,
            identity_schema=ComponentBinding(
                state=ComponentState.CONFIGURED,
                component_id="owner-supplied-identity",
                version="1.0.0",
                configuration_sha256=sha256_bytes(b"subject_id := operator-declared label (directory name or mapped column)"),
            ),
            metadata_schema=ComponentBinding(
                state=ComponentState.CONFIGURED,
                component_id="owner-supplied-metadata",
                version="1.0.0",
                configuration_sha256=sha256_bytes(b"source_kind, subject_id, width, height, storage"),
            ),
            redistribution_permission=PermissionState.PROHIBITED,
            display_permission=PermissionState.LIMITED,
            derived_artifact_policy=(
                "Local analysis only: hashes, quality measures, crops, and embeddings may be recorded on this "
                "workstation; nothing is redistributed and the source kind travels with every result."
            ),
            classification=DataClassification.PRIVATE,
        )
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
                    "source_kind": SOURCE_KIND,
                    "samples": len(admitted),
                    "subjects": len(subjects),
                    "statistics_sha256": stats_sha,
                    "content_sha256": manifest_sha,
                    "origin": origin,
                }
            ),
            current_evidence_sha256=evidence,
            local_only=True,
        )
        self.store.record_dataset_import(dataset, samples, stats_text, stats_sha, audit_event)
        return OwnerImportResult(dataset=dataset, sample_count=len(admitted), subject_count=len(subjects), statistics_sha256=stats_sha)

    def import_admitted_single(self, project: Project, admitted: AdmittedInput, name: str) -> tuple[Dataset, Sample]:
        """A content-addressed one-sample dataset for an uploaded photo (QS-Y81); idempotent per byte identity."""

        dataset_id = content_id("dataset", project.project_id, SOURCE_KIND, "single", admitted.sha256)
        existing = self.store.dataset(dataset_id)
        sample_id = content_id("sample", dataset_id, admitted.sha256, admitted.path)
        if existing is not None:
            sample = self.store.sample(sample_id)
            if sample is None:
                raise OwnerSuppliedError("recorded single-sample dataset lost its sample record")
            return existing, sample
        manifest_descriptor = CanonicalJson.from_value(
            {
                "source_kind": SOURCE_KIND,
                "manifest_version": "1.0.0",
                "tree_root": str(Path(admitted.path).parent),
                "identity_rule": "no identity label — a single operator-uploaded photograph",
                "sample_count": 1,
                "content_sha256": admitted.sha256,
                "origin": {"kind": "UPLOAD", "original_name": admitted.original_name},
                "admitting_actor": self.actor,
            }
        )
        sample = Sample(
            sample_id=sample_id,
            dataset_id=dataset_id,
            subject_id=EvidenceText(state=EvidenceState.NOT_RECORDED, reason="Uploaded photo carries no operator identity label."),
            modality=Modality.FACE,
            source_path=admitted.path,
            media_type="image/jpeg" if admitted.path.lower().endswith((".jpg", ".jpeg")) else "image/png",
            sha256=admitted.sha256,
            size_bytes=admitted.size_bytes,
            mtime_ns=Path(admitted.path).stat().st_mtime_ns,
            metadata=CanonicalJson.from_value(
                {
                    "source_kind": SOURCE_KIND,
                    "original_name": admitted.original_name,
                    "width": admitted.width,
                    "height": admitted.height,
                    "storage": admitted.storage,
                }
            ),
        )
        stats = {
            "population": {"subjects": 0, "samples": 1},
            "provenance": {
                "source_kind": SOURCE_KIND,
                "rights_review": "NOT_PERFORMED — operator-supplied local input under decision 30",
                "admitting_actor": self.actor,
            },
            "computed_at": _now().isoformat(),
        }
        stats_text = json.dumps(stats, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        stats_sha = sha256_bytes(stats_text.encode("utf-8"))
        dataset = Dataset(
            dataset_id=dataset_id,
            project_id=project.project_id,
            name=name,
            version="1.0.0",
            source_uri=f"upload://{admitted.original_name}",
            license=None,
            rights_status="OWNER_SUPPLIED_UNREVIEWED",
            rights_evidence=(),
            retrieval=EvidenceText(
                state=EvidenceState.RECORDED,
                value="Uploaded interactively by the attributed operator (decision 30); stored content-addressed in the data directory.",
            ),
            manifest=manifest_descriptor,
            subject_count=CountEvidence(state=EvidenceState.NOT_RECORDED, reason="No identity label was supplied."),
            sample_count=CountEvidence(state=EvidenceState.RECORDED, value=1),
            modality=Modality.FACE,
            identity_schema=ComponentBinding(state=ComponentState.NOT_RECORDED, reason="No identity label was supplied with the upload."),
            metadata_schema=ComponentBinding(
                state=ComponentState.CONFIGURED,
                component_id="owner-supplied-metadata",
                version="1.0.0",
                configuration_sha256=sha256_bytes(b"source_kind, original_name, width, height, storage"),
            ),
            redistribution_permission=PermissionState.PROHIBITED,
            display_permission=PermissionState.LIMITED,
            derived_artifact_policy=(
                "Local analysis only: hashes, quality measures, crops, and embeddings may be recorded on this "
                "workstation; nothing is redistributed and the source kind travels with every result."
            ),
            classification=DataClassification.PRIVATE,
        )
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
                {"source_kind": SOURCE_KIND, "samples": 1, "sha256": admitted.sha256, "origin": "UPLOAD"}
            ),
            current_evidence_sha256=evidence,
            local_only=True,
        )
        self.store.record_dataset_import(dataset, [sample], stats_text, stats_sha, audit_event)
        return dataset, sample

    def import_directory(self, project: Project, root: Path, name: str) -> OwnerImportResult:
        rows = self._collect_directory(root)
        return self.import_rows(project, rows, root=root, name=name, origin={"kind": "DIRECTORY", "root": str(root)})

    def import_manifest(
        self,
        project: Project,
        manifest_path: Path,
        *,
        name: str,
        mapping: dict[str, str],
    ) -> OwnerImportResult:
        manifest_path = manifest_path.resolve(strict=True)
        root = manifest_path.parent
        rows = self._collect_manifest(manifest_path, mapping, root)
        origin = {
            "kind": "CSV_MANIFEST",
            "manifest_path": str(manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
            "mapping": dict(sorted(mapping.items())),
        }
        return self.import_rows(project, rows, root=root, name=name, origin=origin)


# -- QS-Y83: deterministic pair protocol over an owner-supplied dataset ---------


def freeze_owner_pairs(
    store: RunStore,
    project: Project,
    dataset: Dataset,
    *,
    data_dir: Path,
    actor_id: str,
    seed: int,
    max_mated_pairs_per_subject: int = 6,
    nonmated_multiple: float = 1.0,
) -> VerificationProtocolRecord:
    descriptor = json.loads(dataset.manifest.text)
    if descriptor.get("source_kind") != SOURCE_KIND:
        raise OwnerSuppliedError("owner pair protocols are defined only over owner-supplied datasets")
    samples = store.samples_for_dataset(dataset.dataset_id, limit=1_000_000)
    by_subject: dict[str, list[Sample]] = {}
    for sample in sorted(samples, key=lambda item: item.source_path):
        subject = sample.subject_id.value or ""
        if not subject:
            raise OwnerSuppliedError(f"sample {sample.sample_id} has no subject label")
        by_subject.setdefault(subject, []).append(sample)
    subjects = sorted(by_subject)
    rng = random.Random(seed)
    mated: list[tuple[Sample, Sample]] = []
    for subject in subjects:
        items = by_subject[subject]
        pairs = [(items[i], items[j]) for i in range(len(items)) for j in range(i + 1, len(items))]
        if len(pairs) > max_mated_pairs_per_subject:
            pairs = rng.sample(pairs, max_mated_pairs_per_subject)
            pairs.sort(key=lambda pair: (pair[0].source_path, pair[1].source_path))
        mated.extend(pairs)
    if not mated:
        raise OwnerSuppliedError("no subject has two or more samples; no mated pair exists")
    target_nonmated = max(1, int(round(len(mated) * nonmated_multiple)))
    nonmated: list[tuple[Sample, Sample]] = []
    seen: set[tuple[str, str]] = set()
    attempts = 0
    while len(nonmated) < target_nonmated and attempts < target_nonmated * 200:
        attempts += 1
        left_subject, right_subject = rng.sample(subjects, 2)
        left = rng.choice(by_subject[left_subject])
        right = rng.choice(by_subject[right_subject])
        key = (left.sample_id, right.sample_id)
        if key in seen:
            continue
        seen.add(key)
        nonmated.append((left, right))
    if len(nonmated) < target_nonmated:
        raise OwnerSuppliedError("could not draw the requested number of distinct non-mated pairs")
    protocol_id = content_id(
        "protocol", project.project_id, dataset.dataset_id,
        f"seed={seed};mated_cap={max_mated_pairs_per_subject};nonmated_multiple={nonmated_multiple}",
        "OWNER_PAIRS_V1",
    )
    pairs: list[ProtocolPair] = []
    digest = hashlib.sha256()
    ordered = [(ComparisonLabel.MATED, pair) for pair in mated] + [(ComparisonLabel.NON_MATED, pair) for pair in nonmated]
    for order, (label, (left, right)) in enumerate(ordered, start=1):
        pair = ProtocolPair(
            pair_id=f"owner-{order:06d}",
            protocol_id=protocol_id,
            fold=1,
            source_order=order,
            label=label,
            left_sample_id=left.sample_id,
            right_sample_id=right.sample_id,
            left_subject_id=left.subject_id.value or "",
            right_subject_id=right.subject_id.value or "",
            left_sha256=left.sha256,
            right_sha256=right.sha256,
        )
        pairs.append(pair)
        digest.update(pair.model_dump_json().encode("utf-8"))
        digest.update(b"\n")
    sample_ids = {p.left_sample_id for p in pairs} | {p.right_sample_id for p in pairs}
    # The frozen pair list is itself the pair manifest: written once, hashed, bound.
    existing = store.protocol(protocol_id)
    if existing is not None:
        return existing
    manifest_dir = data_dir / "protocols"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"{protocol_id.replace(':', '_')}.jsonl"
    header = {
        "record_type": "PROTOCOL",
        "generator": "qualsight.importers.owner_supplied.freeze_owner_pairs@1.0.0",
        "seed": seed,
        "max_mated_pairs_per_subject": max_mated_pairs_per_subject,
        "nonmated_multiple": nonmated_multiple,
        "pair_count": len(pairs),
        "mated_pairs": len(mated),
        "pairs_sha256": digest.hexdigest(),
    }
    lines = [json.dumps(header, sort_keys=True)]
    for pair in pairs:
        lines.append(json.dumps({"record_type": "PAIR", **json.loads(pair.model_dump_json())}, sort_keys=True))
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stat = manifest_path.stat()
    manifest_identity = ArtifactIdentity(
        path=str(manifest_path),
        sha256=sha256_file(manifest_path),
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )
    record = VerificationProtocolRecord(
        protocol_id=protocol_id,
        project_id=project.project_id,
        dataset_id=dataset.dataset_id,
        name=f"Owner-supplied pairs (seed {seed}) — {dataset.name}",
        pair_policy=PairPolicy.ALL_CROSS_SUBJECT_PAIRS,
        category=ProtocolCategory.NOT_APPLICABLE,
        pair_manifest=manifest_identity,
        protocol_authority=(),
        pair_count=len(pairs),
        mated_count=len(mated),
        nonmated_count=len(nonmated),
        fold_count=1,
        unique_sample_count=len(sample_ids),
        unique_subject_count=len(subjects),
        pairs_sha256=digest.hexdigest(),
        limitations=(
            "The pair list is generated by QualSight from operator-declared identity labels (decision 30), not by an official protocol authority.",
            f"Generation rule: exhaustive within-subject mated pairs capped at {max_mated_pairs_per_subject} per subject; non-mated pairs drawn without replacement by seeded PRNG (seed {seed}).",
            "Identity labels are the operator's claim; QualSight has not verified them against any authority.",
            "No comparison, score, or threshold exists until a matcher Run executes over this protocol.",
        ),
        created_at=_now(),
        metadata=CanonicalJson.from_value(
            {
                "seed": seed,
                "max_mated_pairs_per_subject": max_mated_pairs_per_subject,
                "nonmated_multiple": nonmated_multiple,
                "source_kind": SOURCE_KIND,
                "generator": "qualsight.importers.owner_supplied.freeze_owner_pairs@1.0.0",
            }
        ),
    )
    evidence = sha256_bytes(record.model_dump_json().encode("utf-8"))
    audit_event = AuditEvent(
        audit_event_id=content_id("audit-event", protocol_id, "PROTOCOL_FROZEN", evidence, record.created_at.isoformat()),
        project_id=project.project_id,
        actor_id=actor_id,
        event_type=AuditEventType.CONFIGURATION_CHANGED,
        subject_type="VerificationProtocol",
        subject_id=protocol_id,
        occurred_at=record.created_at,
        details=CanonicalJson.from_value(
            {"pairs": len(pairs), "mated": len(mated), "pairs_sha256": record.pairs_sha256, "seed": seed}
        ),
        current_evidence_sha256=evidence,
        local_only=True,
    )
    store.record_protocol(record, pairs, audit_event)
    return record
