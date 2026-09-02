"""Closed matcher × representation-compatible preprocessing matrix.

Every leaf Run keeps every protocol pair: a comparison carries a cosine score
when both templates exist, otherwise an explicit processing failure naming the
failed side. Runs share the ordered sample set, pair manifest, and score
definition. Source images use the two registered SCRFD conditions;
publisher-prealigned images use only the pass-through condition. Matcher and
preprocessing identities are recorded separately and never pooled.
"""

from __future__ import annotations

from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import getpass
import hashlib
import json
from pathlib import Path
import time
from typing import Any

import cv2
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ..domain.comparison import (
    Comparison,
    ComparisonLabel,
    ProcessingFailureKind,
    ProtocolPair,
    ProvenanceMarker,
    ScoreDirection,
    VerificationProtocolRecord,
)
from ..domain.entities import (
    Artifact,
    ArtifactKind,
    DataClassification,
    Dataset,
    Experiment,
    Project,
    ProtocolKind,
    StorageBackend,
    VerificationProtocol,
)
from ..domain.primitives import (
    ArtifactIdentity as DomainArtifactIdentity,
    CanonicalJson,
    ComponentBinding,
    ComponentState,
)
from ..domain.run import CompletedRun, LogRecord, MetricObservation, RunStatus, ScientificSupportState
from ..engines.ofiq.adapter import _not_applicable, content_id
from ..execution.registry_binding import ResourceRegistry
from ..execution.prepared_output import (
    PreparedOutputError,
    create_staging_directory,
    load_prepared_output,
    prepare_and_publish,
)
from ..metrics.verification import ComparisonInputs, compute_verification_metrics
from ..provenance import (
    application_version,
    artifact_identity,
    environment_identity,
    hardware_identity,
    repository_identity,
    sha256_bytes,
    sha256_file,
)
from .alignment import ARCFACE_TEMPLATE_SHA256
from .bounded import embed_with_deadline, validate_execution_timeout
from .conditions import (
    CONDITION_DEFINITIONS,
    PREALIGNED_CONDITIONS,
    SCRFD_CONDITIONS,
    ConditionId,
    preprocess,
)
from .detection import SCRFDDetector
from .runtime import COLOR_ORDER, TRANSFORM_IDS, FrozenMatcher, MatcherId, cosine_similarity
from .upstream import UpstreamSources

SCRFD_RESOURCE_ID = "insightface-scrfd-10g-local-candidate"
LEAF_IDS: dict[tuple[MatcherId, ConditionId], str] = {
    (MatcherId.ADAFACE_IR101, ConditionId.ADAPTIVE_LARGEST_JPEG95): "QS-C22.AS",
    (MatcherId.ADAFACE_IR101, ConditionId.FIXED640_HIGHEST_INMEMORY): "QS-C22.AN",
    (MatcherId.ELASTICFACE_ARC_R100, ConditionId.ADAPTIVE_LARGEST_JPEG95): "QS-C22.ES",
    (MatcherId.ELASTICFACE_ARC_R100, ConditionId.FIXED640_HIGHEST_INMEMORY): "QS-C22.EN",
    (MatcherId.MAGFACE_IRESNET100, ConditionId.ADAPTIVE_LARGEST_JPEG95): "QS-C22.MS",
    (MatcherId.MAGFACE_IRESNET100, ConditionId.FIXED640_HIGHEST_INMEMORY): "QS-C22.MN",
    (MatcherId.TOPOFR_R100, ConditionId.ADAPTIVE_LARGEST_JPEG95): "QS-C22.PS",
    (MatcherId.TOPOFR_R100, ConditionId.FIXED640_HIGHEST_INMEMORY): "QS-C22.PN",
    (MatcherId.TRANSFACE_B, ConditionId.ADAPTIVE_LARGEST_JPEG95): "QS-C22.BS",
    (MatcherId.TRANSFACE_B, ConditionId.FIXED640_HIGHEST_INMEMORY): "QS-C22.BN",
    (MatcherId.TRANSFACE_S, ConditionId.ADAPTIVE_LARGEST_JPEG95): "QS-C22.TS",
    (MatcherId.TRANSFACE_S, ConditionId.FIXED640_HIGHEST_INMEMORY): "QS-C22.TN",
    (MatcherId.ADAFACE_IR101, ConditionId.PREALIGNED_ARCFACE112): "QS-C22.AU",
    (MatcherId.ELASTICFACE_ARC_R100, ConditionId.PREALIGNED_ARCFACE112): "QS-C22.EU",
    (MatcherId.MAGFACE_IRESNET100, ConditionId.PREALIGNED_ARCFACE112): "QS-C22.MU",
    (MatcherId.TOPOFR_R100, ConditionId.PREALIGNED_ARCFACE112): "QS-C22.PU",
    (MatcherId.TRANSFACE_B, ConditionId.PREALIGNED_ARCFACE112): "QS-C22.BU",
    (MatcherId.TRANSFACE_S, ConditionId.PREALIGNED_ARCFACE112): "QS-C22.TU",
}


class MatrixError(RuntimeError):
    """Raised when the matrix cannot be executed faithfully."""


@dataclass
class ConditionTemplates:
    condition: ConditionId
    sample_ids: list[str]
    crops: np.ndarray  # N×112×112×3 uint8 for successful samples (aligned with success list)
    success_index: dict[str, int]  # sample_id → row in crops
    failures: dict[str, str]  # sample_id → failure kind
    face_counts: dict[str, int | None]
    canvas_by_sample: dict[str, int]
    crop_sha256: dict[str, str]
    seconds: float
    storage_path: Path


@dataclass(frozen=True, slots=True)
class LeafResult:
    leaf_id: str
    matcher_id: MatcherId
    condition_id: ConditionId
    run: CompletedRun
    metrics: dict[str, Any]
    comparisons_parquet: Path
    embeddings_path: Path
    comparison_count: int
    scored_count: int
    failed_count: int


MATRIX_LEAF_ARTIFACT_NAMES = (
    "comparisons.parquet",
    "embeddings.npy",
    "verification_metrics.json",
)


def recover_prepared_matcher_leaf(
    directory: Path,
    *,
    matrix_id: str,
    leaf_id: str,
    project_id: str,
    dataset_id: str,
    protocol_id: str,
    request_sha256: str,
) -> LeafResult:
    """Reconstruct one exact matrix leaf from its promoted receipt."""

    try:
        receipt = load_prepared_output(
            directory,
            operation_kind="MATCHER_MATRIX_LEAF",
            result_id=content_id("matrix-leaf", matrix_id, leaf_id),
            project_id=project_id,
            request_sha256=request_sha256,
            artifact_names=MATRIX_LEAF_ARTIFACT_NAMES,
        )
        record = receipt["record"]
        if set(record) != {
            "comparison_count",
            "condition_id",
            "failed_count",
            "leaf_id",
            "matcher_id",
            "matrix_id",
            "metrics",
            "run",
            "scored_count",
        }:
            raise MatrixError("prepared matcher leaf contract differs")
        matcher_id = MatcherId(record["matcher_id"])
        condition_id = ConditionId(record["condition_id"])
        if LEAF_IDS.get((matcher_id, condition_id)) != leaf_id:
            raise MatrixError("prepared matcher leaf axis identity differs")
        run = CompletedRun.model_validate_json(
            json.dumps(record["run"], separators=(",", ":"), ensure_ascii=False)
        )
        metrics = record["metrics"]
        comparisons_path = directory / "comparisons.parquet"
        embeddings_path = directory / "embeddings.npy"
        metrics_path = directory / "verification_metrics.json"
        configuration = json.loads(run.configuration.text)
        expected_experiment_id = content_id(
            "experiment",
            project_id,
            dataset_id,
            protocol_id,
            matcher_id.value,
            condition_id.value,
        )
        if (
            record["matrix_id"] != matrix_id
            or record["leaf_id"] != leaf_id
            or run.project_id != project_id
            or run.experiment_id != expected_experiment_id
            or run.matcher.component_id != matcher_id.value
            or run.matcher_preprocessing.component_id != condition_id.value
            or configuration.get("matrix_id") != matrix_id
            or configuration.get("leaf_id") != leaf_id
            or configuration.get("protocol_id") != protocol_id
            or {artifact.uri for artifact in run.artifacts}
            != {str(comparisons_path), str(embeddings_path), str(metrics_path)}
            or any(
                sha256_file(Path(artifact.uri)) != artifact.sha256
                for artifact in run.artifacts
            )
            or json.loads(metrics_path.read_text(encoding="utf-8")) != metrics
        ):
            raise MatrixError("prepared matcher leaf identity differs")
        comparison_count = int(record["comparison_count"])
        scored_count = int(record["scored_count"])
        failed_count = int(record["failed_count"])
        if (
            comparison_count < 1
            or scored_count < 0
            or failed_count < 0
            or scored_count + failed_count != comparison_count
        ):
            raise MatrixError("prepared matcher leaf counts differ")
        return LeafResult(
            leaf_id,
            matcher_id,
            condition_id,
            run,
            metrics,
            comparisons_path,
            embeddings_path,
            comparison_count,
            scored_count,
            failed_count,
        )
    except (PreparedOutputError, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        if isinstance(error, MatrixError):
            raise
        raise MatrixError("prepared matcher leaf failed integrity verification") from error


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


class MatcherMatrixJob:
    def __init__(
        self,
        *,
        registry: ResourceRegistry,
        registry_path: Path,
        repository_root: Path,
        upstream: UpstreamSources,
        store: Any,
        project: Project,
        dataset: Dataset,
        protocol: VerificationProtocolRecord,
        pairs: list[ProtocolPair],
        job_root: Path,
        purpose_scope: str = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1",
        actor_id: str | None = None,
        matchers: tuple[MatcherId, ...] = tuple(MatcherId),
        conditions: tuple[ConditionId, ...] | None = None,
        selection_rule: str = "every pair of the frozen protocol",
        runtime_manager: Any | None = None,
        detector_workers: int = 2,
        execution_timeout_seconds: float,
    ) -> None:
        if not pairs:
            raise MatrixError("the protocol has no pairs")
        if not matchers:
            raise MatrixError("the matcher selection is empty")
        descriptor = json.loads(dataset.manifest.text)
        prealigned_dataset = descriptor.get("source_kind") == "MANAGED_PREALIGNED_PAIR_DATASET"
        allowed_conditions = PREALIGNED_CONDITIONS if prealigned_dataset else SCRFD_CONDITIONS
        selected_conditions = allowed_conditions if conditions is None else conditions
        if not selected_conditions:
            raise MatrixError("the preprocessing selection is empty")
        if len(set(matchers)) != len(matchers):
            raise MatrixError("the matcher selection contains duplicates")
        if len(set(selected_conditions)) != len(selected_conditions):
            raise MatrixError("the preprocessing selection contains duplicates")
        incompatible = set(selected_conditions) - set(allowed_conditions)
        if incompatible:
            representation = "author-prealigned pair images" if prealigned_dataset else "source images requiring SCRFD alignment"
            raise MatrixError(
                f"preprocessing conditions {sorted(item.value for item in incompatible)} are incompatible with {representation}"
            )
        if detector_workers < 1 or detector_workers > 4:
            raise MatrixError("detector worker count must be between one and four")
        self.registry = registry
        self.registry_path = registry_path
        self.repository_root = repository_root
        self.upstream = upstream
        self.store = store
        self.project = project
        self.dataset = dataset
        self.protocol = protocol
        fold_states = {pair.fold is None for pair in pairs}
        if len(fold_states) != 1:
            raise MatrixError("a protocol cannot mix recorded and unrecorded fold assignments")
        folds_absent = True in fold_states
        if folds_absent != (protocol.fold_count is None):
            raise MatrixError("protocol fold authority disagrees with its pair rows")
        self.pairs = sorted(
            pairs,
            key=lambda item: (item.fold is None, item.fold or 0, item.source_order),
        )
        self.purpose_scope = purpose_scope
        self.actor = actor_id or f"local-user:{getpass.getuser()}"
        self.matchers = matchers
        self.conditions = selected_conditions
        self.expected_leaf_count = len(matchers) * len(selected_conditions)
        self.selection_rule = selection_rule
        self.runtime_manager = runtime_manager
        self.detector_workers = detector_workers
        try:
            self.execution_timeout_seconds = validate_execution_timeout(
                execution_timeout_seconds
            )
        except ValueError as error:
            raise MatrixError(str(error)) from error
        scoped_resources = tuple(m.value for m in matchers) + (dataset_resource_of(dataset),)
        if not prealigned_dataset:
            scoped_resources = (SCRFD_RESOURCE_ID, *scoped_resources)
        for resource_id in filter(None, scoped_resources):
            resource = registry.resource(resource_id)
            if purpose_scope not in resource.approved_scopes:
                raise MatrixError(f"{resource_id} does not approve scope {purpose_scope}")
        self.created_at = _now()
        self.leaf_ids = tuple(
            LEAF_IDS[(matcher, condition)]
            for matcher in self.matchers
            for condition in self.conditions
        )
        self.matrix_id = content_id(
            "matrix",
            project.project_id,
            protocol.protocol_id,
            protocol.pairs_sha256,
            json.dumps(sorted(self.leaf_ids), separators=(",", ":")),
            selection_rule,
        )
        self.job_dir = job_root / self.matrix_id.replace(":", "_")
        # Ordered sample set shared by every selected leaf Run.
        ordered: dict[str, ProtocolPair] = {}
        self.sample_ids: list[str] = []
        for pair in self.pairs:
            for sample_id in (pair.left_sample_id, pair.right_sample_id):
                if sample_id not in ordered:
                    ordered[sample_id] = pair
                    self.sample_ids.append(sample_id)
        self.samples = {}
        for sample_id in self.sample_ids:
            sample = store.sample(sample_id)
            if sample is None:
                raise MatrixError(f"protocol pair references an unrecorded sample {sample_id}")
            self.samples[sample_id] = sample
        self.detector_identity = None
        self.detector = None
        if not prealigned_dataset:
            scrfd = registry.resource(SCRFD_RESOURCE_ID)
            detector_model = next(item.identity for item in scrfd.artifacts if item.role == "MODEL")
            self.detector_identity = detector_model
            self.detector = SCRFDDetector(Path(detector_model.path), detector_model.sha256)
        self.templates: dict[ConditionId, ConditionTemplates] = {}
        self.template_storage_kind = "NUMPY_DISK_BACKED_MEMMAP_V1"
        self.template_manifest_sha256: dict[ConditionId, str] = {}
        self.template_set_sha256: str | None = None
        self.leaves: list[LeafResult] = []
        self.progress: dict[str, Any] = {"phase": "created", "detail": ""}

    # -- stage 1: preprocessing per condition ---------------------------------

    def _preprocess_all(self) -> None:
        for condition in self.conditions:
            started = time.perf_counter()
            storage_path = self.job_dir / f".{condition.value.lower()}-crops.npy"
            crops = np.lib.format.open_memmap(
                storage_path,
                mode="w+",
                dtype=np.uint8,
                shape=(len(self.sample_ids), 112, 112, 3),
            )
            successful_crops = 0
            success_index: dict[str, int] = {}
            failures: dict[str, str] = {}
            face_counts: dict[str, int | None] = {}
            canvas_by_sample: dict[str, int] = {}
            crop_sha: dict[str, str] = {}
            def prepare_sample(sample_id: str) -> tuple[str, Any | None, str | None]:
                sample = self.samples[sample_id]
                path = Path(sample.source_path)
                if sha256_file(path) != sample.sha256:
                    return sample_id, None, ProcessingFailureKind.INTEGRITY_FAILURE.value
                image = cv2.imread(str(path), cv2.IMREAD_COLOR)
                if image is None:
                    return sample_id, None, ProcessingFailureKind.FAILURE_TO_ACQUIRE.value
                return sample_id, preprocess(image, condition, self.detector), None

            try:
                with ThreadPoolExecutor(
                    max_workers=self.detector_workers,
                    thread_name_prefix="qualsight-scrfd",
                ) as detector_pool:
                    position = 0
                    for batch_start in range(0, len(self.sample_ids), 64):
                        for sample_id, result, admission_failure in detector_pool.map(
                            prepare_sample,
                            self.sample_ids[batch_start : batch_start + 64],
                        ):
                            if admission_failure is not None:
                                failures[sample_id] = admission_failure
                            else:
                                assert result is not None
                                face_counts[sample_id] = result.face_count
                                canvas_by_sample[sample_id] = result.canvas
                                if result.failure is not None or result.crop_bgr is None:
                                    failures[sample_id] = result.failure or ProcessingFailureKind.FAILURE_TO_ACQUIRE.value
                                else:
                                    success_index[sample_id] = successful_crops
                                    crops[successful_crops] = result.crop_bgr
                                    written_sha = hashlib.sha256(crops[successful_crops].tobytes()).hexdigest()
                                    if written_sha != result.crop_sha256:
                                        raise MatrixError(f"disk-backed crop verification failed for {sample_id}")
                                    successful_crops += 1
                                    crop_sha[sample_id] = result.crop_sha256
                            if position % 500 == 0:
                                self.progress = {"phase": f"preprocess:{condition.value}", "detail": f"{position}/{len(self.sample_ids)}"}
                            position += 1
                crops.flush()
            except BaseException:
                crops._mmap.close()  # type: ignore[union-attr]
                storage_path.unlink(missing_ok=True)
                raise
            self.templates[condition] = ConditionTemplates(
                condition=condition,
                sample_ids=list(self.sample_ids),
                crops=crops[:successful_crops],
                success_index=success_index,
                failures=failures,
                face_counts=face_counts,
                canvas_by_sample=canvas_by_sample,
                crop_sha256=crop_sha,
                seconds=time.perf_counter() - started,
                storage_path=storage_path,
            )
            self.template_manifest_sha256[condition] = sha256_bytes(
                json.dumps(crop_sha, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )

    def _release_templates(self) -> None:
        for templates in self.templates.values():
            mapping = getattr(templates.crops, "_mmap", None)
            if mapping is not None:
                mapping.close()
            templates.storage_path.unlink(missing_ok=True)
        self.templates.clear()

    # -- stage 2: one leaf Run per matcher × condition ---------------------------

    def _leaf(
        self,
        matcher: FrozenMatcher,
        condition: ConditionId,
        *,
        request_sha256: str,
    ) -> LeafResult:
        templates = self.templates[condition]
        leaf_id = LEAF_IDS[(matcher.matcher_id, condition)]
        started_at = _now()
        clock = time.perf_counter()
        embeddings = (
            embed_with_deadline(
                matcher_id=matcher.matcher_id,
                registry=self.registry,
                upstream=self.upstream,
                crops_bgr=templates.crops,
                timeout_seconds=self.execution_timeout_seconds,
                process_label=leaf_id,
            )
            if templates.crops.shape[0]
            else np.zeros((0, 512), dtype=np.float32)
        )
        embed_seconds = time.perf_counter() - clock
        leaf_dir = self.job_dir / leaf_id.replace(".", "_")
        leaf_result_id = content_id("matrix-leaf", self.matrix_id, leaf_id)
        staging = create_staging_directory(
            leaf_dir,
            operation_kind="MATCHER_MATRIX_LEAF",
            result_id=leaf_result_id,
            project_id=self.project.project_id,
            request_sha256=request_sha256,
        )
        embeddings_path = leaf_dir / "embeddings.npy"
        staged_embeddings_path = staging / embeddings_path.name
        np.save(staged_embeddings_path, embeddings)
        # Comparisons: every protocol pair, in protocol order.
        rows: dict[str, list[Any]] = {k: [] for k in ("pair_id", "fold", "source_order", "label", "left_sample_id", "right_sample_id", "left_subject_id", "right_subject_id", "score", "failure", "failure_side", "left_face_count", "right_face_count")}
        comparison_inputs_scores: list[float] = []
        comparison_inputs_mated: list[bool] = []
        comparison_inputs_failed: list[bool] = []
        comparison_inputs_folds: list[int | None] = []
        scored = 0
        for pair in self.pairs:
            left_index = templates.success_index.get(pair.left_sample_id)
            right_index = templates.success_index.get(pair.right_sample_id)
            failure = None
            side = None
            score: float | None = None
            if left_index is None or right_index is None:
                failed_sides = [s for s, idx in (("left", left_index), ("right", right_index)) if idx is None]
                side = "+".join(failed_sides)
                kinds = {templates.failures.get(pair.left_sample_id if s == "left" else pair.right_sample_id, ProcessingFailureKind.FAILURE_TO_DETECT.value) for s in failed_sides}
                failure = ProcessingFailureKind.INTEGRITY_FAILURE.value if ProcessingFailureKind.INTEGRITY_FAILURE.value in kinds else (
                    ProcessingFailureKind.FAILURE_TO_ACQUIRE.value if ProcessingFailureKind.FAILURE_TO_ACQUIRE.value in kinds else ProcessingFailureKind.FAILURE_TO_DETECT.value
                )
            else:
                value = float(cosine_similarity(embeddings[[left_index]], embeddings[[right_index]])[0])
                if not np.isfinite(value):
                    failure = ProcessingFailureKind.FAILURE_TO_COMPARE.value
                    side = "both"
                else:
                    score = value
                    scored += 1
            # Validate through the domain schema (label/score/failure invariants).
            Comparison(
                comparison_id=content_id("comparison", leaf_id, pair.pair_id),
                run_id="pending",
                protocol_id=self.protocol.protocol_id,
                pair_id=pair.pair_id,
                label=pair.label,
                left_sample_id=pair.left_sample_id,
                right_sample_id=pair.right_sample_id,
                score=score,
                score_direction=ScoreDirection.HIGHER_IS_MORE_SIMILAR,
                failure=ProcessingFailureKind(failure) if failure else None,
                failure_detail=side,
                provenance=ProvenanceMarker.QUALSIGHT_EXECUTED,
                matcher_component_id=matcher.matcher_id.value,
                preprocessing_condition_id=condition.value,
            )
            rows["pair_id"].append(pair.pair_id)
            rows["fold"].append(pair.fold)
            rows["source_order"].append(pair.source_order)
            rows["label"].append(pair.label.value)
            rows["left_sample_id"].append(pair.left_sample_id)
            rows["right_sample_id"].append(pair.right_sample_id)
            rows["left_subject_id"].append(pair.left_subject_id)
            rows["right_subject_id"].append(pair.right_subject_id)
            rows["score"].append(score)
            rows["failure"].append(failure)
            rows["failure_side"].append(side)
            rows["left_face_count"].append(templates.face_counts.get(pair.left_sample_id))
            rows["right_face_count"].append(templates.face_counts.get(pair.right_sample_id))
            comparison_inputs_scores.append(score if score is not None else float("nan"))
            comparison_inputs_mated.append(pair.label is ComparisonLabel.MATED)
            comparison_inputs_failed.append(failure is not None)
            comparison_inputs_folds.append(pair.fold)
        table = pa.table(
            rows,
            schema=pa.schema(
                [
                    pa.field("pair_id", pa.string()), pa.field("fold", pa.int32()), pa.field("source_order", pa.int32()), pa.field("label", pa.string()),
                    pa.field("left_sample_id", pa.string()), pa.field("right_sample_id", pa.string()), pa.field("left_subject_id", pa.string()), pa.field("right_subject_id", pa.string()),
                    pa.field("score", pa.float64()), pa.field("failure", pa.string()), pa.field("failure_side", pa.string()), pa.field("left_face_count", pa.int32()), pa.field("right_face_count", pa.int32()),
                ]
            ),
        )
        comparisons_path = leaf_dir / "comparisons.parquet"
        staged_comparisons_path = staging / comparisons_path.name
        pq.write_table(table, staged_comparisons_path, compression="zstd")
        metrics = compute_verification_metrics(
            ComparisonInputs(
                scores=np.array(comparison_inputs_scores, dtype=np.float64),
                mated=np.array(comparison_inputs_mated, dtype=bool),
                failed=np.array(comparison_inputs_failed, dtype=bool),
                folds=np.array(comparison_inputs_folds, dtype=object),
            )
        )
        metrics_path = leaf_dir / "verification_metrics.json"
        staged_metrics_path = staging / metrics_path.name
        staged_metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        run = self._leaf_run(
            matcher,
            condition,
            leaf_id,
            started_at,
            embed_seconds,
            comparisons_path,
            embeddings_path,
            metrics_path,
            metrics,
            templates,
            scored,
            len(self.pairs) - scored,
            artifact_source_directory=staging,
        )
        leaf = LeafResult(leaf_id, matcher.matcher_id, condition, run, metrics, comparisons_path, embeddings_path, len(self.pairs), scored, len(self.pairs) - scored)
        prepare_and_publish(
            staging,
            leaf_dir,
            operation_kind="MATCHER_MATRIX_LEAF",
            result_id=leaf_result_id,
            project_id=self.project.project_id,
            request_sha256=request_sha256,
            artifact_names=MATRIX_LEAF_ARTIFACT_NAMES,
            record={
                "comparison_count": leaf.comparison_count,
                "condition_id": condition.value,
                "failed_count": leaf.failed_count,
                "leaf_id": leaf_id,
                "matcher_id": matcher.matcher_id.value,
                "matrix_id": self.matrix_id,
                "metrics": metrics,
                "run": run.model_dump(mode="json"),
                "scored_count": leaf.scored_count,
            },
        )
        return leaf

    def _leaf_run(self, matcher: FrozenMatcher, condition: ConditionId, leaf_id: str, started_at: datetime, embed_seconds: float, comparisons_path: Path, embeddings_path: Path, metrics_path: Path, metrics: dict[str, Any], templates: ConditionTemplates, scored: int, failed: int, *, artifact_source_directory: Path | None = None) -> CompletedRun:
        binding = matcher.binding
        matcher_record = self.registry.raw_resource(matcher.matcher_id.value)
        checkpoint_identity = next(item.identity for item in self.registry.resource(matcher.matcher_id.value).artifacts if item.role == "MODEL")
        detector_domain = (
            DomainArtifactIdentity(
                path=self.detector_identity.path,
                sha256=self.detector_identity.sha256,
                size_bytes=self.detector_identity.size_bytes,
                mtime_ns=self.detector_identity.mtime_ns,
            )
            if self.detector_identity is not None
            else None
        )
        checkpoint_domain = DomainArtifactIdentity(path=checkpoint_identity.path, sha256=checkpoint_identity.sha256, size_bytes=checkpoint_identity.size_bytes, mtime_ns=checkpoint_identity.mtime_ns)
        condition_definition = CONDITION_DEFINITIONS[condition]
        condition_sha = sha256_bytes(json.dumps(condition_definition, sort_keys=True).encode("utf-8"))
        transform_id = TRANSFORM_IDS[matcher.matcher_id]
        transform_sha = sha256_bytes(f"{transform_id}:{COLOR_ORDER[matcher.matcher_id]}:CHW float32 (x/255-0.5)/0.5 in [-1,1]; L2-normalized 512-d; cosine similarity".encode("utf-8"))
        configuration = CanonicalJson.from_value(
            {
                "leaf_id": leaf_id,
                "matrix_id": self.matrix_id,
                "protocol_id": self.protocol.protocol_id,
                "protocol_pairs_sha256": self.protocol.pairs_sha256,
                "pair_selection": {"rule": self.selection_rule, "pair_count": len(self.pairs), "protocol_pair_count": self.protocol.pair_count, "complete_protocol": len(self.pairs) == self.protocol.pair_count},
                "pair_manifest_sha256": self.protocol.pair_manifest.sha256,
                "ordered_sample_ids_sha256": sha256_bytes("\n".join(self.sample_ids).encode("utf-8")),
                "sample_count": len(self.sample_ids),
                "matcher": {"resource_id": matcher.matcher_id.value, "transform_id": transform_id, "checkpoint_sha256": binding.checkpoint_sha256, "architecture_source_sha256": list(binding.architecture_source_sha256), "parameter_count": binding.parameter_count, "device": binding.device, "torch_version": binding.torch_version},
                "preprocessing": {
                    "condition_id": condition.value,
                    **{k: v for k, v in condition_definition.items()},
                    "detector_sha256": self.detector_identity.sha256 if self.detector_identity is not None else None,
                    "detector_threshold": 0.5 if self.detector_identity is not None else None,
                    "nms_threshold": 0.4 if self.detector_identity is not None else None,
                    "detector_workers": self.detector_workers if self.detector_identity is not None else None,
                },
                "template_storage": {"kind": self.template_storage_kind, "allocated_rows": len(self.sample_ids), "successful_rows": templates.crops.shape[0], "write_verification": "SHA256_MATCHES_PREPROCESS_OUTPUT", "crop_manifest_sha256": self.template_manifest_sha256[condition]},
                "score": {"definition": "cosine similarity between L2-normalized 512-d embeddings", "direction": "HIGHER_IS_MORE_SIMILAR"},
                "failure_policy": "every protocol pair retained; a pair without two templates carries an explicit processing failure and no score",
                "purpose_scope": self.purpose_scope,
                "preprocessing_failures": {"count": len(templates.failures), "by_kind": {kind: sum(1 for value in templates.failures.values() if value == kind) for kind in set(templates.failures.values())}},
            }
        )
        experiment_id = content_id("experiment", self.project.project_id, self.dataset.dataset_id, self.protocol.protocol_id, matcher.matcher_id.value, condition.value)
        comparison_source_path = (
            artifact_source_directory / comparisons_path.name
            if artifact_source_directory is not None
            else comparisons_path
        )
        run_id = content_id(
            "run",
            experiment_id,
            started_at.isoformat(),
            sha256_file(comparison_source_path),
            binding.checkpoint_sha256,
            self.detector_identity.sha256 if self.detector_identity is not None else condition_sha,
        )
        policy = metrics["by_policy"]["EXCLUDE_FAILED_TRIALS"]
        generalized = metrics["by_policy"]["FAILURES_AS_REJECT"]
        observations = [
            MetricObservation(metric_id="comparisons.total", value=len(self.pairs), unit="count"),
            MetricObservation(metric_id="comparisons.scored", value=scored, unit="count"),
            MetricObservation(metric_id="comparisons.failed", value=failed, unit="count"),
            MetricObservation(metric_id="templates.failed_samples", value=len(templates.failures), unit="count"),
            MetricObservation(metric_id="biometric.verification.eer@1.0.0[EXCLUDE_FAILED_TRIALS]", value=float(policy["eer"]["eer"]), unit="rate"),
            MetricObservation(metric_id="biometric.verification.eer@1.0.0[FAILURES_AS_REJECT]", value=float(generalized["eer"]["eer"]), unit="rate"),
            MetricObservation(metric_id="timing.preprocess_seconds", value=round(templates.seconds, 3), unit="observation"),
            MetricObservation(metric_id="timing.embed_seconds", value=round(embed_seconds, 3), unit="observation"),
        ]
        for name, block in (("EXCLUDE_FAILED_TRIALS", policy), ("FAILURES_AS_REJECT", generalized)):
            mean_accuracy = block["view2_tenfold"]["mean_accuracy"]
            if mean_accuracy is not None:
                observations.append(MetricObservation(metric_id=f"view2.tenfold.mean_accuracy[{name}]", value=float(mean_accuracy), unit="rate"))
        for point in policy["operating_points"]:
            if point.get("threshold") is not None:
                observations.append(MetricObservation(metric_id=f"biometric.verification.fnmr@1.0.0[EXCLUDE_FAILED_TRIALS]@fmr<={point['target_fmr']}", value=float(point["fnmr"]), unit="rate"))
        artifacts = []
        for path, kind, media in ((comparisons_path, ArtifactKind.TABLE, "application/vnd.apache.parquet"), (metrics_path, ArtifactKind.METRICS, "application/json"), (embeddings_path, ArtifactKind.TABLE, "application/x-npy")):
            source_path = (
                artifact_source_directory / path.name
                if artifact_source_directory is not None
                else path
            )
            digest = sha256_file(source_path)
            artifacts.append(
                Artifact(
                    artifact_id=content_id("artifact", run_id, digest, path.name),
                    run_id=run_id,
                    kind=kind,
                    storage_backend=StorageBackend.PARQUET if path.suffix == ".parquet" else StorageBackend.FILESYSTEM,
                    uri=str(path),
                    sha256=digest,
                    size_bytes=source_path.stat().st_size,
                    media_type=media,
                    classification=DataClassification.SENSITIVE_BIOMETRIC_DERIVED,
                    created_at=_now(),
                )
            )
        run = CompletedRun(
            run_id=run_id,
            project_id=self.project.project_id,
            experiment_id=experiment_id,
            user_id=self.actor,
            dataset_manifest=self.dataset.manifest,
            dataset_hash=self.dataset.manifest.sha256,
            subject_protocol=self.dataset.identity_schema,
            pair_search_protocol=ComponentBinding(state=ComponentState.CONFIGURED, component_id=self.protocol.protocol_id, version="1.0.0", configuration_sha256=self.protocol.pairs_sha256),
            quality_engine=None,
            quality_engine_configuration=None,
            model_hashes=(checkpoint_domain,) + ((detector_domain,) if detector_domain is not None else ()),
            matcher=ComponentBinding(state=ComponentState.CONFIGURED, component_id=matcher.matcher_id.value, version=binding.checkpoint_sha256[:16], configuration_sha256=transform_sha, artifact_hashes=(binding.checkpoint_sha256, *binding.architecture_source_sha256)),
            matcher_preprocessing=ComponentBinding(
                state=ComponentState.CONFIGURED,
                component_id=condition.value,
                version="1.0.0",
                configuration_sha256=condition_sha,
                artifact_hashes=(
                    (self.detector_identity.sha256, ARCFACE_TEMPLATE_SHA256)
                    if self.detector_identity is not None
                    else ()
                ),
            ),
            operating_thresholds=ComponentBinding(state=ComponentState.CONFIGURED, component_id="registry-defined-operating-points", version="1.0.0", configuration_sha256=sha256_bytes(json.dumps({"targets": [0.1, 0.01, 0.001], "eer": "linear interpolation"}, sort_keys=True).encode("utf-8"))),
            statistical_methods=ComponentBinding(state=ComponentState.CONFIGURED, component_id="qualsight.metrics.verification", version="1.0.0", configuration_sha256=sha256_bytes(b"clopper-pearson binomial CI; view2 tenfold accuracy")),
            confidence_interval_method=ComponentBinding(state=ComponentState.CONFIGURED, component_id="CLOPPER_PEARSON_EXACT_BINOMIAL_95", version="1.0.0"),
            randomness=_not_applicable("Detection, alignment, embedding, and cosine scoring contain no random-seed setting; inference is deterministic on this device."),
            random_seeds=(),
            application_version=application_version(self.repository_root / "backend" / "pyproject.toml"),
            repository=repository_identity(self.repository_root),
            environment=environment_identity(self.repository_root / "uv.lock", {}),
            hardware=hardware_identity(
                f"matcher device {binding.device}; "
                + (
                    "detector on CPUExecutionProvider"
                    if self.detector_identity is not None
                    else "publisher-prealigned input; no detector execution"
                )
            ),
            start_time=started_at,
            end_time=_now(),
            runtime_seconds=round(templates.seconds + embed_seconds, 3),
            initialization_ms=0,
            assessment_ms=int(embed_seconds * 1000),
            configuration=configuration,
            metrics=tuple(observations),
            artifacts=tuple(artifacts),
            logs=(LogRecord(stream="stdout", sha256=sha256_bytes(b""), text=""), LogRecord(stream="stderr", sha256=sha256_bytes(b""), text="")),
            warnings=tuple(f"{sid}: {kind}" for sid, kind in sorted(templates.failures.items()))[:500],
            provenance_limits=tuple(matcher_record["use_restrictions"]) + tuple(self.protocol.limitations),
            exit_code=0,
            status=RunStatus.COMPLETED,
            scientific_support=ScientificSupportState.NOT_ASSESSED,
        )
        return run

    # -- run --------------------------------------------------------------------

    def run(
        self,
        *,
        request_sha256: str | None = None,
        resume_interrupted: bool = False,
    ) -> list[LeafResult]:
        effective_request_sha256 = request_sha256 or CanonicalJson.from_value(
            {
                "dataset_id": self.dataset.dataset_id,
                "expected_leaf_count": self.expected_leaf_count,
                "leaf_ids": sorted(self.leaf_ids),
                "matrix_id": self.matrix_id,
                "pair_count": len(self.pairs),
                "protocol_id": self.protocol.protocol_id,
                "selection_rule": self.selection_rule,
            }
        ).sha256
        recovered_leaves: dict[str, LeafResult] = {}
        if resume_interrupted:
            if self.job_dir.is_symlink():
                raise MatrixError("interrupted matrix output is a symbolic link")
            self.job_dir.mkdir(parents=True, exist_ok=True)
            if not self.job_dir.is_dir():
                raise MatrixError("interrupted matrix output is not a directory")
            for leaf_id in self.leaf_ids:
                leaf_directory = self.job_dir / leaf_id.replace(".", "_")
                if not leaf_directory.exists():
                    continue
                leaf = recover_prepared_matcher_leaf(
                    leaf_directory,
                    matrix_id=self.matrix_id,
                    leaf_id=leaf_id,
                    project_id=self.project.project_id,
                    dataset_id=self.dataset.dataset_id,
                    protocol_id=self.protocol.protocol_id,
                    request_sha256=effective_request_sha256,
                )
                if self.store.matcher_leaf(leaf.run.run_id) is None:
                    self.store.record_matcher_leaf(self, leaf)
                recovered_leaves[leaf_id] = leaf
        else:
            self.job_dir.mkdir(parents=True, exist_ok=False)
        self.progress = {"phase": "preprocess", "detail": ""}
        try:
            self._preprocess_all()
            self.template_set_sha256 = sha256_bytes(
                json.dumps(
                    {
                        condition.value: templates.crop_sha256
                        for condition, templates in self.templates.items()
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            for matcher_id in self.matchers:
                self.progress = {"phase": f"load:{matcher_id.value}", "detail": ""}
                lease = self.runtime_manager.lease(matcher_id) if self.runtime_manager is not None else nullcontext(FrozenMatcher(matcher_id, self.registry, self.upstream))
                with lease as matcher:
                    for condition in self.conditions:
                        leaf_id = LEAF_IDS[(matcher_id, condition)]
                        existing = recovered_leaves.get(leaf_id)
                        if existing is not None:
                            self.progress = {
                                "phase": f"leaf:{leaf_id}",
                                "detail": "recovered exact prepared leaf",
                            }
                            self.leaves.append(existing)
                            continue
                        self.progress = {"phase": f"leaf:{leaf_id}", "detail": ""}
                        leaf = self._leaf(
                            matcher,
                            condition,
                            request_sha256=effective_request_sha256,
                        )
                        self.store.record_matcher_leaf(self, leaf)
                        self.leaves.append(leaf)
        finally:
            self._release_templates()
        self.progress = {"phase": "completed", "detail": f"{len(self.leaves)} leaves"}
        return self.leaves


def dataset_resource_of(dataset: Dataset) -> str | None:
    descriptor = json.loads(dataset.manifest.text)
    if descriptor.get("source_kind") in {
        "OWNER_SUPPLIED_LOCAL_INPUT",
        "MANAGED_PREALIGNED_PAIR_DATASET",
    }:
        return None  # scope authority is the runtime grant or the managed add-on descriptor
    return descriptor["dataset_resource_id"]
