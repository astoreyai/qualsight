"""Freeze the registered LFW View 2 verification protocol over a recorded dataset (QS-C20).

Every pair comes from the registered pair manifest (byte-verified against the
registry); each side must resolve to a recorded sample of the target dataset
with the manifest's SHA-256; the label is the protocol's, and the record keeps
the official-protocol authority documents, the conservative category, and its
limitations. Nothing is scored here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .api.store import RunStore
from .domain.comparison import (
    ComparisonLabel,
    PairPolicy,
    ProtocolCategory,
    ProtocolPair,
    VerificationProtocolRecord,
)
from .domain.entities import AuditEvent, AuditEventType, Dataset, Project
from .domain.primitives import ArtifactIdentity, CanonicalJson
from .engines.ofiq.adapter import content_id
from .execution.registry_binding import ResourceRegistry
from .provenance import sha256_bytes, sha256_file

LFW_RESOURCE_ID = "lfw-funneled-local-candidate"


class ProtocolError(RuntimeError):
    """Raised when the registered protocol cannot be frozen faithfully."""


@dataclass(frozen=True, slots=True)
class FrozenProtocol:
    record: VerificationProtocolRecord
    pairs: list[ProtocolPair]
    audit_event: AuditEvent


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _identity(path: Path) -> ArtifactIdentity:
    stat = path.stat()
    return ArtifactIdentity(path=str(path), sha256=sha256_file(path), size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)


class ProtocolService:
    def __init__(self, registry: ResourceRegistry, store: RunStore, actor_id: str) -> None:
        self.registry = registry
        self.store = store
        self.actor = actor_id

    def freeze_lfw_view2(self, project: Project, dataset: Dataset) -> FrozenProtocol:
        descriptor = json.loads(dataset.manifest.text)
        if descriptor.get("dataset_resource_id") != LFW_RESOURCE_ID:
            raise ProtocolError("the dataset is not the registered LFW resource")
        resource = self.registry.resource(LFW_RESOURCE_ID)
        pair_manifest = None
        authority: list[ArtifactIdentity] = []
        for artifact in resource.artifacts:
            path = artifact.identity.path or ""
            if artifact.role == "PROTOCOL" and path.endswith("lfw-local-pairs-v1.jsonl"):
                pair_manifest = Path(path)
            elif artifact.role == "PROTOCOL" and path.endswith(".pdf"):
                authority.append(_identity(Path(path)))
        if pair_manifest is None:
            raise ProtocolError("the LFW resource does not register the pair manifest")
        manifest_identity = _identity(pair_manifest)
        registered = next(a.identity for a in resource.artifacts if (a.identity.path or "") == str(pair_manifest))
        if registered.sha256 != manifest_identity.sha256:
            raise ProtocolError("registered pair manifest drifted")
        if descriptor.get("pair_manifest", {}).get("sha256") != manifest_identity.sha256:
            raise ProtocolError("the dataset descriptor names a different pair manifest")
        tree_root = descriptor["tree_root"]

        header: dict | None = None
        pairs: list[ProtocolPair] = []
        protocol_id = content_id("protocol", project.project_id, dataset.dataset_id, manifest_identity.sha256, "LFW_VIEW2")
        digest = hashlib.sha256()
        with pair_manifest.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("record_type") == "PROTOCOL":
                    header = record
                    continue
                if record.get("record_type") != "PAIR":
                    raise ProtocolError(f"unexpected record type {record.get('record_type')}")
                left = self.store.sample_by_path(dataset.dataset_id, f"{tree_root}/{record['left_path']}")
                right = self.store.sample_by_path(dataset.dataset_id, f"{tree_root}/{record['right_path']}")
                if left is None or right is None:
                    raise ProtocolError(f"pair {record['pair_id']} references a sample not recorded in the dataset")
                if left.sha256 != record["left_sha256"] or right.sha256 != record["right_sha256"]:
                    raise ProtocolError(f"pair {record['pair_id']} sample digest disagrees with the recorded sample")
                pair = ProtocolPair(
                    pair_id=record["pair_id"],
                    protocol_id=protocol_id,
                    fold=int(record["fold"]),
                    source_order=int(record["source_order"]),
                    label=ComparisonLabel(record["label"]),
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
        if header is None or not pairs:
            raise ProtocolError("the pair manifest has no header or no pairs")
        mated = sum(1 for pair in pairs if pair.label is ComparisonLabel.MATED)
        folds = {pair.fold for pair in pairs}
        samples = {pair.left_sample_id for pair in pairs} | {pair.right_sample_id for pair in pairs}
        subjects = {pair.left_subject_id for pair in pairs} | {pair.right_subject_id for pair in pairs}
        if header.get("pair_count") != len(pairs) or header.get("mated_pairs") != mated:
            raise ProtocolError("pair counts disagree with the registered manifest header")
        record = VerificationProtocolRecord(
            protocol_id=protocol_id,
            project_id=project.project_id,
            dataset_id=dataset.dataset_id,
            name="LFW View 2 (10-fold, 6,000 pairs) — registered local pair manifest",
            pair_policy=PairPolicy.OFFICIAL_PROTOCOL_LIST,
            category=ProtocolCategory.UNRESTRICTED_WITH_LABELED_OUTSIDE_DATA,
            pair_manifest=manifest_identity,
            protocol_authority=tuple(sorted(authority, key=lambda item: item.path)),
            pair_count=len(pairs),
            mated_count=mated,
            nonmated_count=len(pairs) - mated,
            fold_count=len(folds),
            unique_sample_count=len(samples),
            unique_subject_count=len(subjects),
            pairs_sha256=digest.hexdigest(),
            limitations=(
                "Every pair label comes from the official View 2 list; no pair is invented, filtered, or re-labelled.",
                "The category UNRESTRICTED_WITH_LABELED_OUTSIDE_DATA is the conservative classification for pretrained matchers; training-set exclusion of LFW identities is each matcher's published claim.",
                "This record freezes the protocol only; no comparison, score, or threshold exists until a matcher Run executes.",
            ),
            created_at=_now(),
            metadata=CanonicalJson.from_value(
                {
                    "source_header": {k: header[k] for k in sorted(header) if k not in {"record_type"}},
                    "folds": sorted(folds),
                }
            ),
        )
        evidence = sha256_bytes(record.model_dump_json().encode("utf-8"))
        audit_event = AuditEvent(
            audit_event_id=content_id("audit-event", protocol_id, "PROTOCOL_FROZEN", evidence, record.created_at.isoformat()),
            project_id=project.project_id,
            actor_id=self.actor,
            event_type=AuditEventType.CONFIGURATION_CHANGED,
            subject_type="VerificationProtocol",
            subject_id=protocol_id,
            occurred_at=record.created_at,
            details=CanonicalJson.from_value({"pairs": len(pairs), "mated": mated, "pairs_sha256": record.pairs_sha256}),
            current_evidence_sha256=evidence,
            local_only=True,
        )
        self.store.record_protocol(record, pairs, audit_event)
        return FrozenProtocol(record=record, pairs=pairs, audit_event=audit_event)
