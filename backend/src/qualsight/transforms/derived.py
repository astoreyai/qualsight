"""Derived sets: transformed copies of registered real samples with complete provenance chains (QS-X61).

A derived set applies a recorded list of (operator, parameter) levels to a
recorded selection of real samples. Every derived image is written under the
data directory, hashed, and recorded as a Sample of a derived Dataset whose
chain names the root registered resource, the parent sample (id and hash),
and the operator kind, version, and parameters. Derived sets never contain
invented identities: subject ids are inherited from the parent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..domain.entities import Dataset, DataClassification, Modality, PermissionState, Sample
from ..domain.primitives import CanonicalJson, ComponentBinding, ComponentState, CountEvidence, EvidenceState, EvidenceText
from ..engines.ofiq.adapter import content_id
from ..provenance import sha256_bytes
from .operators import OPERATOR_VERSION, PARAMETER_NAMES, TransformError, apply, encode

DERIVED_SET_VERSION = "1.0.0"


class DerivedSetError(RuntimeError):
    """Raised when a derived set cannot be produced faithfully."""


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


@dataclass(frozen=True, slots=True)
class DerivedSetResult:
    derived_set_id: str
    dataset: Dataset
    samples: list[Sample]
    rows: list[dict[str, Any]]
    record: dict[str, Any]


def build_derived_set(store: Any, *, project: Any, dataset: Any, dataset_resource_id: str, samples: list[Any], transforms: list[dict[str, Any]], root: Path, actor: str) -> DerivedSetResult:
    if not samples:
        raise DerivedSetError("no source samples")
    if not transforms:
        raise DerivedSetError("no transforms")
    levels: list[tuple[str, float]] = []
    for item in transforms:
        kind = item.get("kind")
        parameters = item.get("parameters") or []
        if kind not in PARAMETER_NAMES:
            raise DerivedSetError(f"unknown transform {kind}")
        if not parameters:
            raise DerivedSetError(f"transform {kind} needs at least one parameter")
        for parameter in parameters:
            levels.append((kind, float(parameter)))
    spec_text = json.dumps({"transforms": transforms, "operator_version": OPERATOR_VERSION}, sort_keys=True)
    source_sha = sha256_bytes("\n".join(sorted(s.sample_id for s in samples)).encode("utf-8"))
    derived_set_id = content_id("derived-set", project.project_id, dataset.dataset_id, source_sha, sha256_bytes(spec_text.encode("utf-8")))
    directory = root / derived_set_id.replace(":", "_")
    if directory.exists():
        raise DerivedSetError("this derived set (same sources and transforms) already exists")
    directory.mkdir(parents=True)
    derived_dataset_id = content_id("dataset", derived_set_id)
    rows: list[dict[str, Any]] = []
    derived_samples: list[Sample] = []
    for sample in samples:
        path = Path(sample.source_path)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sample.sha256:
            raise DerivedSetError(f"source sample {sample.sample_id} drifted from its recorded hash")
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise DerivedSetError(f"source sample {sample.sample_id} is not decodable")
        subject = sample.subject_id.value if hasattr(sample.subject_id, "value") else str(sample.subject_id)
        for level_index, (kind, parameter) in enumerate(levels):
            try:
                result = apply(kind, image, parameter)
            except TransformError as error:
                raise DerivedSetError(str(error)) from error
            payload = encode(result)
            digest = hashlib.sha256(payload).hexdigest()
            level_dir = directory / kind / f"{PARAMETER_NAMES[kind]}={parameter:g}"
            level_dir.mkdir(parents=True, exist_ok=True)
            out_path = level_dir / f"{Path(sample.source_path).stem}{result.extension}"
            out_path.write_bytes(payload)
            stat = out_path.stat()
            chain = [{"root_resource_id": dataset_resource_id, "parent_sample_id": sample.sample_id, "parent_sha256": sample.sha256, "parent_path": sample.source_path},
                     {"kind": kind, "version": OPERATOR_VERSION, "parameters": result.parameters, "encoding": {"extension": result.extension, "parameters": list(result.encode_parameters)}, "level_index": level_index}]
            derived_sample_id = content_id("sample", "derived", sample.sample_id, json.dumps(chain, sort_keys=True), digest)
            derived_samples.append(Sample(
                sample_id=derived_sample_id, dataset_id=derived_dataset_id, subject_id=EvidenceText(state=EvidenceState.RECORDED, value=subject), modality=Modality.FACE,
                source_path=str(out_path), media_type="image/jpeg" if result.extension == ".jpg" else "image/png", sha256=digest, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                metadata=CanonicalJson.from_value({"derived": True, "chain": chain, "transform": kind, "parameter": parameter, "parameter_name": PARAMETER_NAMES[kind], "width": int(result.image_bgr.shape[1]), "height": int(result.image_bgr.shape[0])}),
            ))
            rows.append({"derived_sample_id": derived_sample_id, "parent_sample_id": sample.sample_id, "transform": kind, "parameter": parameter, "level_index": level_index, "path": str(out_path), "sha256": digest, "size_bytes": stat.st_size, "chain": chain})
    manifest = CanonicalJson.from_value({"derived_set_id": derived_set_id, "source_dataset_id": dataset.dataset_id, "source_resource_id": dataset_resource_id, "dataset_resource_id": dataset_resource_id, "source_sample_ids_sha256": source_sha, "levels": [{"kind": k, "parameter": p} for k, p in levels], "operator_version": OPERATOR_VERSION, "derived_samples": [{"sample_id": r["derived_sample_id"], "sha256": r["sha256"], "parent_sample_id": r["parent_sample_id"], "transform": r["transform"], "parameter": r["parameter"]} for r in rows]})
    derived_dataset = Dataset(
        dataset_id=derived_dataset_id, project_id=project.project_id, name=f"derived from {dataset.name}: {len(levels)} levels × {len(samples)} sources", version=DERIVED_SET_VERSION,
        source_uri=str(directory), license=None, rights_status="OWNER_DERIVED_FROM_REGISTERED_INPUT", rights_evidence=tuple(dataset.rights_evidence),
        retrieval=EvidenceText(state=EvidenceState.RECORDED, value=f"derived on this workstation from recorded samples of {dataset.dataset_id} by qualsight.transforms {OPERATOR_VERSION}"),
        manifest=manifest, subject_count=CountEvidence(state=EvidenceState.RECORDED, value=len({(s.subject_id.value if hasattr(s.subject_id, 'value') else str(s.subject_id)) for s in samples})), sample_count=CountEvidence(state=EvidenceState.RECORDED, value=len(derived_samples)),
        modality=Modality.FACE, identity_schema=dataset.identity_schema, metadata_schema=ComponentBinding(state=ComponentState.CONFIGURED, component_id="qualsight-derived-sample-metadata", version="1.0.0", configuration_sha256=sha256_bytes(b"derived, chain, transform, parameter, width, height")),
        redistribution_permission=PermissionState.PROHIBITED, display_permission=dataset.display_permission,
        derived_artifact_policy="Derived images stay under the local data directory with their full transformation chain; they inherit the source dataset's restrictions and never leave the workstation.",
        classification=dataset.classification,
    )
    record = {"derived_set_id": derived_set_id, "version": DERIVED_SET_VERSION, "project_id": project.project_id, "source_dataset_id": dataset.dataset_id, "source_resource_id": dataset_resource_id, "derived_dataset_id": derived_dataset_id, "directory": str(directory),
              "transforms": transforms, "operator_version": OPERATOR_VERSION, "levels": len(levels), "source_count": len(samples), "derived_count": len(derived_samples), "created_by": actor, "created_at": _now().isoformat(), "manifest_sha256": manifest.sha256}
    result = DerivedSetResult(derived_set_id, derived_dataset, derived_samples, rows, record)
    store.record_derived_set(result)
    return result
