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
import logging
import os
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..domain.entities import Dataset, DataClassification, Modality, PermissionState, Sample
from ..domain.primitives import CanonicalJson, ComponentBinding, ComponentState, CountEvidence, EvidenceState, EvidenceText
from ..engines.ofiq.adapter import content_id
from ..execution.prepared_output import (
    STAGING_IDENTITY_FILENAME,
    STAGING_IDENTITY_SCHEMA_VERSION,
    PreparedOutputError,
    _publish_directory_no_clobber,
    create_staging_directory,
    discard_staging_directory,
    load_staging_identity,
)
from ..provenance import sha256_bytes
from .operators import OPERATOR_VERSION, PARAMETER_NAMES, TransformError, apply, encode, validate_parameter

DERIVED_SET_VERSION = "1.0.0"
PREPARED_SET_FILENAME = ".qualsight-derived-set.prepared.json"
LOGGER = logging.getLogger(__name__)


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


def derived_set_identity(
    *,
    project_id: str,
    dataset_id: str,
    samples: list[Any],
    transforms: list[dict[str, Any]],
) -> str:
    spec_text = json.dumps(
        {"transforms": transforms, "operator_version": OPERATOR_VERSION},
        sort_keys=True,
        allow_nan=False,
    )
    source_sha = sha256_bytes(
        "\n".join(sorted(sample.sample_id for sample in samples)).encode("utf-8")
    )
    return content_id(
        "derived-set",
        project_id,
        dataset_id,
        source_sha,
        sha256_bytes(spec_text.encode("utf-8")),
    )


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_tree(root: Path) -> None:
    directories = [root]
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise DerivedSetError("prepared derived output contains a symbolic link")
        if path.is_dir():
            directories.append(path)
            continue
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    for directory in reversed(directories):
        _sync_directory(directory)


def _write_prepared_set(directory: Path, result: DerivedSetResult) -> None:
    document = CanonicalJson.from_value(
        {
            "dataset": result.dataset.model_dump(mode="json"),
            "derived_set_id": result.derived_set_id,
            "record": result.record,
            "rows": result.rows,
            "samples": [sample.model_dump(mode="json") for sample in result.samples],
            "schema_version": "1.0.0",
        }
    )
    path = directory / PREPARED_SET_FILENAME
    with path.open("xb") as handle:
        os.chmod(path, 0o600)
        handle.write((document.text + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def _promoted_staging_marker(
    directory: Path,
    *,
    derived_set_id: str,
    project_id: str,
    request_sha256: str | None,
) -> Path | None:
    """Validate the marker retained by a process death during promotion."""

    marker = directory / STAGING_IDENTITY_FILENAME
    if not marker.exists() and not marker.is_symlink():
        return None
    if request_sha256 is None:
        raise DerivedSetError("prepared derived-set request identity is unavailable")
    if marker.is_symlink() or not marker.is_file():
        raise DerivedSetError("prepared derived-set staging identity is invalid")
    try:
        text = marker.read_text(encoding="utf-8")
        identity = json.loads(text)
        expected = {
            "schema_version": STAGING_IDENTITY_SCHEMA_VERSION,
            "target_name": directory.name,
            "operation_kind": "DERIVED_SET",
            "result_id": derived_set_id,
            "project_id": project_id,
            "request_sha256": request_sha256,
        }
        if identity != expected or text != CanonicalJson.from_value(expected).text + "\n":
            raise DerivedSetError("prepared derived-set staging identity differs")
        return marker
    except (OSError, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        if isinstance(error, DerivedSetError):
            raise
        raise DerivedSetError(
            "prepared derived-set staging identity failed integrity verification"
        ) from error


def _has_symlink_component(path: Path, root: Path) -> bool:
    current = path
    while current != root:
        if current.is_symlink():
            return True
        if root not in current.parents:
            return True
        current = current.parent
    return root.is_symlink()


def _recover_prepared_set(
    directory: Path,
    *,
    derived_set_id: str,
    project: Any,
    dataset: Dataset,
    dataset_resource_id: str | None,
    samples: list[Any],
    transforms: list[dict[str, Any]],
    request_sha256: str | None = None,
) -> DerivedSetResult:
    try:
        if directory.is_symlink() or not directory.is_dir():
            raise DerivedSetError("prepared derived-set directory is unavailable")
        receipt = directory / PREPARED_SET_FILENAME
        if receipt.is_symlink() or not receipt.is_file():
            raise DerivedSetError("prepared derived-set receipt is unavailable")
        text = receipt.read_text(encoding="utf-8")
        document = json.loads(text)
        if text != CanonicalJson.from_value(document).text + "\n":
            raise DerivedSetError("prepared derived-set receipt is not canonical")
        if set(document) != {
            "dataset",
            "derived_set_id",
            "record",
            "rows",
            "samples",
            "schema_version",
        } or document["schema_version"] != "1.0.0":
            raise DerivedSetError("prepared derived-set receipt contract differs")
        recovered_dataset = Dataset.model_validate_json(
            json.dumps(document["dataset"], separators=(",", ":"), ensure_ascii=False)
        )
        recovered_samples = [
            Sample.model_validate_json(
                json.dumps(item, separators=(",", ":"), ensure_ascii=False)
            )
            for item in document["samples"]
        ]
        rows = document["rows"]
        record = document["record"]
        expected_sample_ids = {sample.sample_id for sample in samples}
        expected_sources = {sample.sample_id: sample for sample in samples}
        if (
            document["derived_set_id"] != derived_set_id
            or record["derived_set_id"] != derived_set_id
            or record["project_id"] != project.project_id
            or record["source_dataset_id"] != dataset.dataset_id
            or record["source_resource_id"] != dataset_resource_id
            or record["derived_dataset_id"] != recovered_dataset.dataset_id
            or record["directory"] != str(directory)
            or record["transforms"] != transforms
            or recovered_dataset.project_id != project.project_id
            or recovered_dataset.source_uri != str(directory)
            or recovered_dataset.sample_count.value != len(recovered_samples)
            or int(record["derived_count"]) != len(recovered_samples)
            or int(record["source_count"]) != len(samples)
            or not isinstance(rows, list)
            or len(rows) != len(recovered_samples)
        ):
            raise DerivedSetError("prepared derived-set identity differs")
        by_id = {sample.sample_id: sample for sample in recovered_samples}
        if len(by_id) != len(recovered_samples):
            raise DerivedSetError("prepared derived-set sample identities are not unique")
        expected_directory = directory.resolve(strict=True)
        observed_parents: set[str] = set()
        marker = _promoted_staging_marker(
            directory,
            derived_set_id=derived_set_id,
            project_id=project.project_id,
            request_sha256=request_sha256,
        )
        expected_members = {Path(PREPARED_SET_FILENAME)}
        if marker is not None:
            expected_members.add(Path(STAGING_IDENTITY_FILENAME))
        for row in rows:
            recovered = by_id.get(row["derived_sample_id"])
            if recovered is None:
                raise DerivedSetError("prepared derived-set row has no recorded sample")
            path = Path(row["path"])
            if (
                _has_symlink_component(path, directory)
                or not path.is_file()
                or not path.resolve(strict=True).is_relative_to(expected_directory)
                or recovered.source_path != str(path)
                or recovered.sha256 != row["sha256"]
                or recovered.size_bytes != row["size_bytes"]
                or path.stat().st_size != row["size_bytes"]
                or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]
            ):
                raise DerivedSetError("prepared derived-set artifact differs")
            chain = row["chain"]
            if not isinstance(chain, list) or len(chain) != 2:
                raise DerivedSetError("prepared derived-set chain differs")
            parent_id = row["parent_sample_id"]
            parent = expected_sources.get(parent_id)
            if (
                parent is None
                or chain[0].get("parent_sample_id") != parent.sample_id
                or chain[0].get("parent_sha256") != parent.sha256
                or chain[0].get("parent_path") != parent.source_path
                or chain[0].get("root_resource_id") != dataset_resource_id
            ):
                raise DerivedSetError("prepared derived-set source chain differs")
            observed_parents.add(parent_id)
            relative = path.relative_to(directory)
            expected_members.add(relative)
            expected_members.update(
                parent for parent in relative.parents if parent != Path(".")
            )
        if observed_parents != expected_sample_ids:
            raise DerivedSetError("prepared derived-set source selection differs")
        observed_members = {
            path.relative_to(directory) for path in directory.rglob("*")
        }
        if observed_members != expected_members:
            raise DerivedSetError("prepared derived set contains unbound members")
        if marker is not None:
            marker.unlink()
            _sync_directory(directory)
        return DerivedSetResult(
            derived_set_id=derived_set_id,
            dataset=recovered_dataset,
            samples=recovered_samples,
            rows=rows,
            record=record,
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise DerivedSetError("prepared derived set failed integrity verification") from error


def build_derived_set(store: Any, *, project: Any, dataset: Any, dataset_resource_id: str | None, samples: list[Any], transforms: list[dict[str, Any]], root: Path, actor: str, request_sha256: str) -> DerivedSetResult:
    if not samples:
        raise DerivedSetError("no source samples")
    if not transforms:
        raise DerivedSetError("no transforms")
    levels: list[tuple[str, float]] = []
    for item in transforms:
        kind = item.get("kind")
        parameters = item.get("parameters")
        if kind not in PARAMETER_NAMES:
            raise DerivedSetError(f"unknown transform {kind}")
        if not isinstance(parameters, (list, tuple)) or not parameters:
            raise DerivedSetError(f"transform {kind} needs at least one parameter")
        for parameter in parameters:
            try:
                levels.append((kind, validate_parameter(kind, parameter)))
            except TransformError as error:
                raise DerivedSetError(str(error)) from error
    spec_text = json.dumps({"transforms": transforms, "operator_version": OPERATOR_VERSION}, sort_keys=True, allow_nan=False)
    source_descriptor = json.loads(dataset.manifest.text)
    source_kind = source_descriptor.get("source_kind")
    source_sha = sha256_bytes("\n".join(sorted(s.sample_id for s in samples)).encode("utf-8"))
    derived_set_id = derived_set_identity(
        project_id=project.project_id,
        dataset_id=dataset.dataset_id,
        samples=samples,
        transforms=transforms,
    )
    directory = root / derived_set_id.replace(":", "_")
    if directory.exists():
        if store.derived_set(derived_set_id) is not None:
            raise DerivedSetError("this derived set (same sources and transforms) already exists")
        recovered = _recover_prepared_set(
            directory,
            derived_set_id=derived_set_id,
            project=project,
            dataset=dataset,
            dataset_resource_id=dataset_resource_id,
            samples=samples,
            transforms=transforms,
            request_sha256=request_sha256,
        )
        try:
            store.record_derived_set(recovered)
            return recovered
        except Exception as error:
            LOGGER.exception("prepared derived-set metadata recovery failed")
            raise DerivedSetError("prepared derived set could not be recorded atomically") from error
    root.mkdir(parents=True, exist_ok=True)
    _sync_directory(root)
    try:
        staging = create_staging_directory(
            directory,
            operation_kind="DERIVED_SET",
            result_id=derived_set_id,
            project_id=project.project_id,
            request_sha256=request_sha256,
        )
    except PreparedOutputError as error:
        raise DerivedSetError(
            "derived-set output staging failed integrity verification"
        ) from error
    promoted = False
    try:
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
            for level_index, (kind, parameter) in enumerate(levels):
                try:
                    result = apply(kind, image, parameter)
                    payload = encode(result)
                except TransformError as error:
                    raise DerivedSetError(str(error)) from error
                digest = hashlib.sha256(payload).hexdigest()
                relative = Path(kind) / f"{PARAMETER_NAMES[kind]}={parameter:g}" / f"{Path(sample.source_path).stem}{result.extension}"
                staged_path = staging / relative
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                staged_path.write_bytes(payload)
                stat = staged_path.stat()
                out_path = directory / relative
                chain = [{"root_resource_id": dataset_resource_id, "root_source_kind": source_kind, "parent_sample_id": sample.sample_id, "parent_sha256": sample.sha256, "parent_path": sample.source_path},
                         {"kind": kind, "version": OPERATOR_VERSION, "parameters": result.parameters, "encoding": {"extension": result.extension, "parameters": list(result.encode_parameters)}, "level_index": level_index}]
                derived_sample_id = content_id("sample", "derived", sample.sample_id, json.dumps(chain, sort_keys=True), digest)
                derived_samples.append(Sample(
                    sample_id=derived_sample_id, dataset_id=derived_dataset_id, subject_id=sample.subject_id, modality=Modality.FACE,
                    source_path=str(out_path), media_type="image/jpeg" if result.extension == ".jpg" else "image/png", sha256=digest, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                    metadata=CanonicalJson.from_value({"derived": True, "chain": chain, "transform": kind, "parameter": parameter, "parameter_name": PARAMETER_NAMES[kind], "width": int(result.image_bgr.shape[1]), "height": int(result.image_bgr.shape[0])}),
                ))
                rows.append({"derived_sample_id": derived_sample_id, "parent_sample_id": sample.sample_id, "transform": kind, "parameter": parameter, "level_index": level_index, "path": str(out_path), "sha256": digest, "size_bytes": stat.st_size, "chain": chain})
        manifest = CanonicalJson.from_value({"derived_set_id": derived_set_id, "source_dataset_id": dataset.dataset_id, "source_resource_id": dataset_resource_id, "dataset_resource_id": dataset_resource_id, "source_kind": source_kind, "source_sample_ids_sha256": source_sha, "levels": [{"kind": k, "parameter": p} for k, p in levels], "operator_version": OPERATOR_VERSION, "derived_samples": [{"sample_id": r["derived_sample_id"], "sha256": r["sha256"], "parent_sample_id": r["parent_sample_id"], "transform": r["transform"], "parameter": r["parameter"]} for r in rows]})
        recorded_subjects = {
            sample.subject_id.value
            for sample in samples
            if sample.subject_id.state is EvidenceState.RECORDED
            and sample.subject_id.value is not None
        }
        complete_subject_authority = all(
            sample.subject_id.state is EvidenceState.RECORDED
            and sample.subject_id.value is not None
            for sample in samples
        )
        derived_dataset = Dataset(
            dataset_id=derived_dataset_id, project_id=project.project_id, name=f"derived from {dataset.name}: {len(levels)} levels × {len(samples)} sources", version=DERIVED_SET_VERSION,
            source_uri=str(directory), license=None, rights_status="OWNER_DERIVED_FROM_REGISTERED_INPUT", rights_evidence=tuple(dataset.rights_evidence),
            retrieval=EvidenceText(state=EvidenceState.RECORDED, value=f"derived on this workstation from recorded samples of {dataset.dataset_id} by qualsight.transforms {OPERATOR_VERSION}"),
            manifest=manifest, subject_count=(CountEvidence(state=EvidenceState.RECORDED, value=len(recorded_subjects)) if complete_subject_authority else CountEvidence(state=EvidenceState.NOT_RECORDED, reason="The selected source samples do not carry complete recorded identity labels.")), sample_count=CountEvidence(state=EvidenceState.RECORDED, value=len(derived_samples)),
            modality=Modality.FACE, identity_schema=dataset.identity_schema, metadata_schema=ComponentBinding(state=ComponentState.CONFIGURED, component_id="qualsight-derived-sample-metadata", version="1.0.0", configuration_sha256=sha256_bytes(b"derived, chain, transform, parameter, width, height")),
            redistribution_permission=PermissionState.PROHIBITED, display_permission=dataset.display_permission,
            derived_artifact_policy="Derived images stay under the local data directory with their full transformation chain; they inherit the source dataset's restrictions and never leave the workstation.",
            classification=dataset.classification,
        )
        record = {"derived_set_id": derived_set_id, "version": DERIVED_SET_VERSION, "project_id": project.project_id, "source_dataset_id": dataset.dataset_id, "source_resource_id": dataset_resource_id, "source_kind": source_kind, "derived_dataset_id": derived_dataset_id, "directory": str(directory),
                  "transforms": transforms, "operator_version": OPERATOR_VERSION, "levels": len(levels), "source_count": len(samples), "derived_count": len(derived_samples), "created_by": actor, "created_at": _now().isoformat(), "manifest_sha256": manifest.sha256}
        result = DerivedSetResult(derived_set_id, derived_dataset, derived_samples, rows, record)
        _write_prepared_set(staging, result)
        _sync_tree(staging)
        load_staging_identity(
            staging,
            target_name=directory.name,
            operation_kind="DERIVED_SET",
            result_id=derived_set_id,
            project_id=project.project_id,
            request_sha256=request_sha256,
        )
        _publish_directory_no_clobber(staging, directory)
        promoted = True
        _sync_directory(root)
        recovered = _recover_prepared_set(
            directory,
            derived_set_id=derived_set_id,
            project=project,
            dataset=dataset,
            dataset_resource_id=dataset_resource_id,
            samples=samples,
            transforms=transforms,
            request_sha256=request_sha256,
        )
        store.record_derived_set(recovered)
        return recovered
    except DerivedSetError:
        if not promoted:
            discard_staging_directory(staging)
        raise
    except Exception as error:
        if not promoted:
            discard_staging_directory(staging)
        LOGGER.exception("derived-set metadata transaction failed")
        raise DerivedSetError("derived set could not be recorded atomically") from error
