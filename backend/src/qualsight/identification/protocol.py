"""Frozen 1:N identification protocols over a recorded dataset (PRD §17; QS-X60).

LFW directory identity: every subject with at least two recorded images
enrols its lexicographically first image in the gallery and searches the rest
as mated probes; every single-image subject searches its image as a non-mated
(open-set) probe. The rule is deterministic, recorded, and content-addressed;
no identity, label, or sample is invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from ..engines.ofiq.adapter import content_id

PROTOCOL_VERSION = "1.0.0"
SELECTIONS = ("all", "subset_fixture")


class IdentificationProtocolError(RuntimeError):
    """Raised when a protocol cannot be frozen from recorded samples."""


@dataclass(frozen=True, slots=True)
class Entry:
    role: str  # GALLERY | PROBE_MATED | PROBE_NONMATED
    sample_id: str
    subject_id: str
    source_order: int


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def freeze_lfw_identification(store: Any, *, project: Any, dataset: Any, selection: str, rank_depth: int, repository_root: Path, actor: str) -> dict[str, Any]:
    if selection not in SELECTIONS:
        raise IdentificationProtocolError(f"unknown selection {selection}")
    if rank_depth < 1 or rank_depth > 1000:
        raise IdentificationProtocolError("rank depth must be 1–1000")
    samples = store.samples_for_dataset(dataset.dataset_id, limit=1_000_000)
    if selection == "subset_fixture":
        fixture = json.loads((repository_root / "resources" / "manifests" / "lfw-subset-fixture-v1.json").read_text(encoding="utf-8"))
        wanted = {image["path"] for image in fixture["images"]}
        samples = [s for s in samples if any(s.source_path.endswith("/" + path) for path in wanted)]
        rule = f"registered subset fixture {fixture['fixture_id']}: subjects with ≥2 fixture images enrol their first image and probe the rest; single-image subjects are non-mated probes"
    else:
        rule = "every recorded LFW subject with ≥2 images enrols its lexicographically first image and probes the rest; every single-image subject is a non-mated (open-set) probe"
    if not samples:
        raise IdentificationProtocolError("no recorded samples match the selection")
    by_subject: dict[str, list[Any]] = {}
    for sample in samples:
        subject = sample.subject_id.value if hasattr(sample.subject_id, "value") else str(sample.subject_id)
        if not subject:
            raise IdentificationProtocolError(f"sample {sample.sample_id} has no subject identity")
        by_subject.setdefault(subject, []).append(sample)
    entries: list[Entry] = []
    order = 0
    for subject in sorted(by_subject):
        items = sorted(by_subject[subject], key=lambda s: s.source_path)
        if len(items) >= 2:
            entries.append(Entry("GALLERY", items[0].sample_id, subject, order)); order += 1
            for probe in items[1:]:
                entries.append(Entry("PROBE_MATED", probe.sample_id, subject, order)); order += 1
        else:
            entries.append(Entry("PROBE_NONMATED", items[0].sample_id, subject, order)); order += 1
    gallery = [e for e in entries if e.role == "GALLERY"]
    mated = [e for e in entries if e.role == "PROBE_MATED"]
    non_mated = [e for e in entries if e.role == "PROBE_NONMATED"]
    if not gallery or not mated:
        raise IdentificationProtocolError("the selection has no subject with two or more images; no identification protocol can be frozen")
    manifest_text = "\n".join(f"{e.role}\t{e.sample_id}\t{e.subject_id}" for e in entries) + "\n"
    manifest_sha = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
    protocol_id = content_id("identification-protocol", project.project_id, dataset.dataset_id, selection, str(rank_depth), manifest_sha)
    record = {
        "protocol_id": protocol_id, "project_id": project.project_id, "dataset_id": dataset.dataset_id, "version": PROTOCOL_VERSION, "kind": "IDENTIFICATION", "mode": "OPEN_SET",
        "name": f"LFW identification ({selection}) · gallery {len(gallery)} · mated probes {len(mated)} · non-mated probes {len(non_mated)}", "selection": selection, "selection_rule": rule, "rank_depth": rank_depth,
        "identity_schema": "lfw-directory-identity", "gallery_count": len(gallery), "mated_probe_count": len(mated), "nonmated_probe_count": len(non_mated), "subjects_enrolled": len(gallery), "subjects_total": len(by_subject),
        "manifest_sha256": manifest_sha, "created_by": actor, "created_at": _now(),
        "limitations": ["Closed-set quantities use the mated probes only; open-set quantities add the single-image subjects as non-mated searches.", "One enrolled sample per subject; template composition effects are not exercised by this protocol."],
    }
    store.record_identification_protocol(record, entries)
    return record
