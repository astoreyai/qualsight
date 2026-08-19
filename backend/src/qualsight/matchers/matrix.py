"""Three matchers × two preprocessing conditions over one frozen protocol (QS-C22/QS-C23).

Every leaf Run keeps every protocol pair: a comparison carries a cosine score
when both templates exist, otherwise an explicit processing failure naming the
failed side. The six Runs share the ordered sample set, the pair manifest, the
detector bytes, the alignment template, and the score definition; matcher and
preprocessing identities are recorded separately and never pooled.
"""

from __future__ import annotations

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
from .conditions import CONDITION_DEFINITIONS, ConditionId, preprocess
from .detection import SCRFDDetector
from .runtime import COLOR_ORDER, TRANSFORM_IDS, FrozenMatcher, MatcherId, cosine_similarity
from .upstream import UpstreamSources

SCRFD_RESOURCE_ID = "insightface-scrfd-10g-local-candidate"
LEAF_IDS: dict[tuple[MatcherId, ConditionId], str] = {
    (MatcherId.ADAFACE_IR101, ConditionId.ADAPTIVE_LARGEST_JPEG95): "QS-C22.AS",
    (MatcherId.ADAFACE_IR101, ConditionId.FIXED640_HIGHEST_INMEMORY): "QS-C22.AN",
    (MatcherId.ELASTICFACE_ARC_R100, ConditionId.ADAPTIVE_LARGEST_JPEG95): "QS-C22.ES",
    (MatcherId.ELASTICFACE_ARC_R100, ConditionId.FIXED640_HIGHEST_INMEMORY): "QS-C22.EN",
    (MatcherId.TRANSFACE_S, ConditionId.ADAPTIVE_LARGEST_JPEG95): "QS-C22.TS",
    (MatcherId.TRANSFACE_S, ConditionId.FIXED640_HIGHEST_INMEMORY): "QS-C22.TN",
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
    face_counts: dict[str, int]
    canvas_by_sample: dict[str, int]
    crop_sha256: dict[str, str]
    seconds: float


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
        conditions: tuple[ConditionId, ...] = tuple(ConditionId),
        selection_rule: str = "every pair of the frozen protocol",
    ) -> None:
        if not pairs:
            raise MatrixError("the protocol has no pairs")
        self.registry = registry
        self.registry_path = registry_path
        self.repository_root = repository_root
        self.upstream = upstream
        self.store = store
        self.project = project
        self.dataset = dataset
        self.protocol = protocol
        self.pairs = sorted(pairs, key=lambda item: (item.fold, item.source_order))
        self.purpose_scope = purpose_scope
        self.actor = actor_id or f"local-user:{getpass.getuser()}"
        self.matchers = matchers
        self.conditions = conditions
        self.selection_rule = selection_rule
        for resource_id in filter(None, (SCRFD_RESOURCE_ID, *(m.value for m in matchers), dataset_resource_of(dataset))):
            resource = registry.resource(resource_id)
            if purpose_scope not in resource.approved_scopes:
                raise MatrixError(f"{resource_id} does not approve scope {purpose_scope}")
        self.created_at = _now()
        self.matrix_id = content_id("matrix", project.project_id, protocol.protocol_id, protocol.pairs_sha256, self.created_at.isoformat())
        self.job_dir = job_root / self.matrix_id.replace(":", "_")
        self.job_dir.mkdir(parents=True, exist_ok=False)
        # Ordered sample set shared by all six Runs.
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
        scrfd = registry.resource(SCRFD_RESOURCE_ID)
        detector_model = next(item.identity for item in scrfd.artifacts if item.role == "MODEL")
        self.detector_identity = detector_model
        self.detector = SCRFDDetector(Path(detector_model.path), detector_model.sha256)
        self.templates: dict[ConditionId, ConditionTemplates] = {}
        self.leaves: list[LeafResult] = []
        self.progress: dict[str, Any] = {"phase": "created", "detail": ""}

    # -- stage 1: preprocessing per condition ---------------------------------

    def _preprocess_all(self) -> None:
        for condition in self.conditions:
            started = time.perf_counter()
            crops: list[np.ndarray] = []
            success_index: dict[str, int] = {}
            failures: dict[str, str] = {}
            face_counts: dict[str, int] = {}
            canvas_by_sample: dict[str, int] = {}
            crop_sha: dict[str, str] = {}
            for position, sample_id in enumerate(self.sample_ids):
                sample = self.samples[sample_id]
                path = Path(sample.source_path)
                if sha256_file(path) != sample.sha256:
                    failures[sample_id] = ProcessingFailureKind.INTEGRITY_FAILURE.value
                    continue
                image = cv2.imread(str(path), cv2.IMREAD_COLOR)
                if image is None:
                    failures[sample_id] = ProcessingFailureKind.FAILURE_TO_ACQUIRE.value
                    continue
                result = preprocess(image, condition, self.detector)
                face_counts[sample_id] = result.face_count
                canvas_by_sample[sample_id] = result.canvas
                if result.failure is not None or result.crop_bgr is None:
                    failures[sample_id] = ProcessingFailureKind.FAILURE_TO_DETECT.value
                    continue
                success_index[sample_id] = len(crops)
                crops.append(result.crop_bgr)
                crop_sha[sample_id] = result.crop_sha256 or ""
                if position % 500 == 0:
                    self.progress = {"phase": f"preprocess:{condition.value}", "detail": f"{position}/{len(self.sample_ids)}"}
            self.templates[condition] = ConditionTemplates(
                condition=condition,
                sample_ids=list(self.sample_ids),
                crops=np.stack(crops) if crops else np.zeros((0, 112, 112, 3), dtype=np.uint8),
                success_index=success_index,
                failures=failures,
                face_counts=face_counts,
                canvas_by_sample=canvas_by_sample,
                crop_sha256=crop_sha,
                seconds=time.perf_counter() - started,
            )

    # -- stage 2: one leaf Run per matcher × condition ---------------------------

    def _leaf(self, matcher: FrozenMatcher, condition: ConditionId) -> LeafResult:
        templates = self.templates[condition]
        leaf_id = LEAF_IDS[(matcher.matcher_id, condition)]
        started_at = _now()
        clock = time.perf_counter()
        embeddings = matcher.embed(templates.crops) if templates.crops.shape[0] else np.zeros((0, 512), dtype=np.float32)
        embed_seconds = time.perf_counter() - clock
        leaf_dir = self.job_dir / leaf_id.replace(".", "_")
        leaf_dir.mkdir(parents=True, exist_ok=False)
        embeddings_path = leaf_dir / "embeddings.npy"
        np.save(embeddings_path, embeddings)
        # Comparisons: every protocol pair, in protocol order.
        rows: dict[str, list[Any]] = {k: [] for k in ("pair_id", "fold", "source_order", "label", "left_sample_id", "right_sample_id", "left_subject_id", "right_subject_id", "score", "failure", "failure_side", "left_face_count", "right_face_count")}
        comparison_inputs_scores: list[float] = []
        comparison_inputs_mated: list[bool] = []
        comparison_inputs_failed: list[bool] = []
        comparison_inputs_folds: list[int] = []
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
        pq.write_table(table, comparisons_path, compression="zstd")
        metrics = compute_verification_metrics(
            ComparisonInputs(
                scores=np.array(comparison_inputs_scores, dtype=np.float64),
                mated=np.array(comparison_inputs_mated, dtype=bool),
                failed=np.array(comparison_inputs_failed, dtype=bool),
                folds=np.array(comparison_inputs_folds, dtype=np.int64),
            )
        )
        metrics_path = leaf_dir / "verification_metrics.json"
        metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        run = self._leaf_run(matcher, condition, leaf_id, started_at, embed_seconds, comparisons_path, embeddings_path, metrics_path, metrics, templates, scored, len(self.pairs) - scored)
        return LeafResult(leaf_id, matcher.matcher_id, condition, run, metrics, comparisons_path, embeddings_path, len(self.pairs), scored, len(self.pairs) - scored)

    def _leaf_run(self, matcher: FrozenMatcher, condition: ConditionId, leaf_id: str, started_at: datetime, embed_seconds: float, comparisons_path: Path, embeddings_path: Path, metrics_path: Path, metrics: dict[str, Any], templates: ConditionTemplates, scored: int, failed: int) -> CompletedRun:
        binding = matcher.binding
        matcher_record = self.registry.raw_resource(matcher.matcher_id.value)
        checkpoint_identity = next(item.identity for item in self.registry.resource(matcher.matcher_id.value).artifacts if item.role == "MODEL")
        detector_domain = DomainArtifactIdentity(path=self.detector_identity.path, sha256=self.detector_identity.sha256, size_bytes=self.detector_identity.size_bytes, mtime_ns=self.detector_identity.mtime_ns)
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
                "preprocessing": {"condition_id": condition.value, **{k: v for k, v in condition_definition.items()}, "detector_sha256": self.detector_identity.sha256, "detector_threshold": 0.5, "nms_threshold": 0.4, "alignment_template_sha256": ARCFACE_TEMPLATE_SHA256},
                "score": {"definition": "cosine similarity between L2-normalized 512-d embeddings", "direction": "HIGHER_IS_MORE_SIMILAR"},
                "failure_policy": "every protocol pair retained; a pair without two templates carries an explicit processing failure and no score",
                "purpose_scope": self.purpose_scope,
                "preprocessing_failures": {"count": len(templates.failures), "by_kind": {kind: sum(1 for value in templates.failures.values() if value == kind) for kind in set(templates.failures.values())}},
            }
        )
        experiment_id = content_id("experiment", self.project.project_id, self.dataset.dataset_id, self.protocol.protocol_id, matcher.matcher_id.value, condition.value)
        run_id = content_id("run", experiment_id, started_at.isoformat(), sha256_file(comparisons_path), binding.checkpoint_sha256, self.detector_identity.sha256)
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
            digest = sha256_file(path)
            artifacts.append(
                Artifact(
                    artifact_id=content_id("artifact", run_id, digest, path.name),
                    run_id=run_id,
                    kind=kind,
                    storage_backend=StorageBackend.PARQUET if path.suffix == ".parquet" else StorageBackend.FILESYSTEM,
                    uri=str(path),
                    sha256=digest,
                    size_bytes=path.stat().st_size,
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
            subject_protocol=ComponentBinding(state=ComponentState.CONFIGURED, component_id="lfw-directory-identity", version="1.0.0"),
            pair_search_protocol=ComponentBinding(state=ComponentState.CONFIGURED, component_id=self.protocol.protocol_id, version="LFW_VIEW2", configuration_sha256=self.protocol.pairs_sha256),
            quality_engine=None,
            quality_engine_configuration=None,
            model_hashes=(checkpoint_domain, detector_domain),
            matcher=ComponentBinding(state=ComponentState.CONFIGURED, component_id=matcher.matcher_id.value, version=binding.checkpoint_sha256[:16], configuration_sha256=transform_sha, artifact_hashes=(binding.checkpoint_sha256, *binding.architecture_source_sha256)),
            matcher_preprocessing=ComponentBinding(state=ComponentState.CONFIGURED, component_id=condition.value, version="1.0.0", configuration_sha256=condition_sha, artifact_hashes=(self.detector_identity.sha256, ARCFACE_TEMPLATE_SHA256)),
            operating_thresholds=ComponentBinding(state=ComponentState.CONFIGURED, component_id="registry-defined-operating-points", version="1.0.0", configuration_sha256=sha256_bytes(json.dumps({"targets": [0.1, 0.01, 0.001], "eer": "linear interpolation"}, sort_keys=True).encode("utf-8"))),
            statistical_methods=ComponentBinding(state=ComponentState.CONFIGURED, component_id="qualsight.metrics.verification", version="1.0.0", configuration_sha256=sha256_bytes(b"clopper-pearson binomial CI; view2 tenfold accuracy")),
            confidence_interval_method=ComponentBinding(state=ComponentState.CONFIGURED, component_id="CLOPPER_PEARSON_EXACT_BINOMIAL_95", version="1.0.0"),
            randomness=_not_applicable("Detection, alignment, embedding, and cosine scoring contain no random-seed setting; inference is deterministic on this device."),
            random_seeds=(),
            application_version=application_version(self.repository_root / "backend" / "pyproject.toml"),
            repository=repository_identity(self.repository_root),
            environment=environment_identity(self.repository_root / "uv.lock", {}),
            hardware=hardware_identity(f"matcher device {binding.device}; detector on CPUExecutionProvider"),
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

    def run(self) -> list[LeafResult]:
        self.progress = {"phase": "preprocess", "detail": ""}
        self._preprocess_all()
        for matcher_id in self.matchers:
            self.progress = {"phase": f"load:{matcher_id.value}", "detail": ""}
            matcher = FrozenMatcher(matcher_id, self.registry, self.upstream)
            for condition in self.conditions:
                self.progress = {"phase": f"leaf:{LEAF_IDS[(matcher_id, condition)]}", "detail": ""}
                leaf = self._leaf(matcher, condition)
                self.store.record_matcher_leaf(self, leaf)
                self.leaves.append(leaf)
        self.progress = {"phase": "completed", "detail": f"{len(self.leaves)} leaves"}
        return self.leaves


def dataset_resource_of(dataset: Dataset) -> str | None:
    descriptor = json.loads(dataset.manifest.text)
    if descriptor.get("source_kind") == "OWNER_SUPPLIED_LOCAL_INPUT":
        return None  # decision 30: not a registered resource; scope authority is the runtime grant
    return descriptor["dataset_resource_id"]
