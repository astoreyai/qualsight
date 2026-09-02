"""Direct comparison of one admitted owner pair without population claims."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import asdict
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import time
from typing import Any

import cv2
import numpy as np

from ..domain.direct_pair_comparison import (
    DirectPairArtifact,
    DirectPairComparisonRun,
    DirectPairDecision,
    DirectPairDecisionReason,
    DirectPairResult,
    DirectPairSideEvidence,
    DirectPairThreshold,
)
from ..domain.entities import Sample
from ..domain.owner_pair import OwnerPairAdmission, OwnerPairRelationship
from ..domain.primitives import CanonicalJson
from ..execution.prepared_output import (
    STAGING_IDENTITY_FILENAME,
    STAGING_IDENTITY_SCHEMA_VERSION,
    PreparedOutputError,
    _publish_directory_no_clobber,
    create_staging_directory,
    discard_staging_directory,
    load_staging_identity,
)
from ..provenance import application_version, sha256_bytes, sha256_file
from .conditions import CONDITION_DEFINITIONS, ConditionId, CropResult, preprocess
from .detection import Detection, SCRFDDetector
from .lifecycle import MatcherRuntimeManager
from .runtime import MatcherBinding, MatcherId, cosine_similarity


class DirectPairComparisonError(RuntimeError):
    """Raised when a direct comparison cannot produce durable evidence."""


PREPARED_RUN_FILENAME = ".qualsight-direct-pair.prepared.json"


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def direct_comparison_identity(
    *,
    pair_id: str,
    matcher_id: MatcherId,
    preprocessing_id: ConditionId,
    threshold: DirectPairThreshold,
) -> tuple[str, str, CanonicalJson]:
    request = CanonicalJson.from_value(
        {
            "matcher_id": matcher_id.value,
            "pair_id": pair_id,
            "preprocessing_id": preprocessing_id.value,
            "threshold": threshold.model_dump(mode="json"),
        }
    )
    digest = hashlib.sha256(
        b"direct-owner-pair-comparison-v1\x00" + request.text.encode("utf-8")
    ).hexdigest()
    comparison_id = f"owner-pair-comparison:sha256:{digest}"
    run_id = f"direct-pair-run:sha256:{hashlib.sha256((comparison_id + request.sha256).encode('utf-8')).hexdigest()}"
    return comparison_id, run_id, request


def _artifact(
    name: str,
    path: Path,
    media_type: str,
    *,
    recorded_path: Path | None = None,
) -> DirectPairArtifact:
    return DirectPairArtifact(
        artifact=name,
        path=str((recorded_path or path).resolve(strict=recorded_path is None)),
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
        media_type=media_type,
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
            raise DirectPairComparisonError("prepared output contains a symbolic link")
        if path.is_dir():
            directories.append(path)
            continue
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    for directory in reversed(directories):
        _sync_directory(directory)


def _write_prepared_run(directory: Path, run: DirectPairComparisonRun) -> None:
    path = directory / PREPARED_RUN_FILENAME
    payload = (run.model_dump_json() + "\n").encode("utf-8")
    with path.open("xb") as handle:
        os.chmod(path, 0o600)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _promoted_staging_marker(
    directory: Path,
    *,
    run_id: str,
    project_id: str,
    request_sha256: str,
) -> Path | None:
    """Validate the marker retained by a process death during promotion."""

    marker = directory / STAGING_IDENTITY_FILENAME
    if not marker.exists() and not marker.is_symlink():
        return None
    if marker.is_symlink() or not marker.is_file():
        raise DirectPairComparisonError("prepared Run staging identity is invalid")
    try:
        text = marker.read_text(encoding="utf-8")
        identity = json.loads(text)
        expected = {
            "schema_version": STAGING_IDENTITY_SCHEMA_VERSION,
            "target_name": directory.name,
            "operation_kind": "DIRECT_PAIR_COMPARISON",
            "result_id": run_id,
            "project_id": project_id,
            "request_sha256": request_sha256,
        }
        if identity != expected or text != CanonicalJson.from_value(expected).text + "\n":
            raise DirectPairComparisonError("prepared Run staging identity differs")
        return marker
    except (OSError, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        if isinstance(error, DirectPairComparisonError):
            raise
        raise DirectPairComparisonError(
            "prepared Run staging identity failed integrity verification"
        ) from error


def recover_prepared_direct_pair(
    directory: Path,
    *,
    comparison_id: str,
    run_id: str,
    project_id: str,
    request_sha256: str,
) -> DirectPairComparisonRun:
    """Validate a fully promoted Run receipt after abrupt process death."""

    try:
        if directory.is_symlink() or not directory.is_dir():
            raise DirectPairComparisonError("prepared output directory is unavailable")
        receipt = directory / PREPARED_RUN_FILENAME
        if receipt.is_symlink() or not receipt.is_file():
            raise DirectPairComparisonError("prepared Run receipt is unavailable")
        text = receipt.read_text(encoding="utf-8")
        parsed = json.loads(text)
        canonical = DirectPairComparisonRun.model_validate_json(text)
        if text != canonical.model_dump_json() + "\n":
            raise DirectPairComparisonError("prepared Run receipt is not canonical")
        expected_directory = directory.resolve(strict=True)
        if (
            canonical.comparison_id != comparison_id
            or canonical.run_id != run_id
            or canonical.project_id != project_id
            or canonical.request_sha256 != request_sha256
            or Path(canonical.output_directory).resolve(strict=True)
            != expected_directory
        ):
            raise DirectPairComparisonError("prepared Run identity differs")
        marker = _promoted_staging_marker(
            directory,
            run_id=run_id,
            project_id=project_id,
            request_sha256=request_sha256,
        )
        expected_members = {Path(PREPARED_RUN_FILENAME)}
        if marker is not None:
            expected_members.add(Path(STAGING_IDENTITY_FILENAME))
        for side in (canonical.left, canonical.right):
            for artifact in side.artifacts:
                path = Path(artifact.path)
                if path.is_symlink() or not path.is_file():
                    raise DirectPairComparisonError("prepared Run artifact is unavailable")
                if (
                    path.stat().st_size != artifact.size_bytes
                    or sha256_file(path) != artifact.sha256
                ):
                    raise DirectPairComparisonError("prepared Run artifact differs")
                if artifact.artifact != f"{side.side}-source" and not path.resolve(
                    strict=True
                ).is_relative_to(expected_directory):
                    raise DirectPairComparisonError("prepared Run artifact escaped its directory")
                if path.resolve(strict=True).is_relative_to(expected_directory):
                    relative = path.relative_to(directory)
                    expected_members.add(relative)
                    expected_members.update(
                        parent for parent in relative.parents if parent != Path(".")
                    )
        observed_members = {
            path.relative_to(directory) for path in directory.rglob("*")
        }
        if observed_members != expected_members:
            raise DirectPairComparisonError("prepared Run contains unbound members")
        if marker is not None:
            marker.unlink()
            _sync_directory(directory)
        return canonical
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        if isinstance(error, DirectPairComparisonError):
            raise
        raise DirectPairComparisonError("prepared Run failed integrity verification") from error


def _matcher_embedding_worker(
    sender: Any,
    *,
    matcher_id: str,
    registry_path: str,
    upstream_manifest_path: str,
    data_dir: str,
    crops_bgr: np.ndarray | None,
) -> None:
    """Load and execute one real matcher in a deadline-terminable process."""

    try:
        from ..execution.registry_binding import load_resource_registry
        from .runtime import FrozenMatcher
        from .upstream import UpstreamSources

        registry = load_resource_registry(Path(registry_path))
        upstream = UpstreamSources(Path(upstream_manifest_path), Path(data_dir))
        matcher = FrozenMatcher(MatcherId(matcher_id), registry, upstream)
        embeddings = None if crops_bgr is None else matcher.embed(crops_bgr)
        sender.send(
            {
                "binding": asdict(matcher.binding),
                "embeddings": embeddings,
                "state": "COMPLETED",
            }
        )
    except BaseException as error:
        sender.send(
            {
                "error": f"{type(error).__name__}: {error}",
                "state": "FAILED",
            }
        )
    finally:
        sender.close()


def _detection_value(detection: Detection, *, canvas: int) -> dict[str, Any]:
    return {
        "bbox_xyxy": [float(value) for value in detection.bbox.tolist()],
        "canvas": canvas,
        "landmarks_xy": [
            [float(coordinate) for coordinate in point]
            for point in detection.landmarks.tolist()
        ],
        "score": float(detection.score),
    }


class DirectPairComparisonJob:
    """Execute and persist one matcher/configuration over two real admitted images."""

    def __init__(
        self,
        *,
        store: Any,
        pair: OwnerPairAdmission,
        left: Sample,
        right: Sample,
        matcher_id: MatcherId,
        preprocessing_id: ConditionId,
        threshold: DirectPairThreshold,
        matcher_manager: MatcherRuntimeManager,
        detector: SCRFDDetector,
        output_root: Path,
        repository_root: Path,
        actor_id: str,
        execution_timeout_seconds: float,
    ) -> None:
        self.store = store
        self.pair = pair
        self.left_sample = left
        self.right_sample = right
        self.matcher_id = matcher_id
        self.preprocessing_id = preprocessing_id
        self.threshold = threshold
        self.matcher_manager = matcher_manager
        self.detector = detector
        self.repository_root = repository_root
        self.actor_id = actor_id
        if (
            not isinstance(execution_timeout_seconds, (int, float))
            or isinstance(execution_timeout_seconds, bool)
            or execution_timeout_seconds <= 0
        ):
            raise ValueError("direct comparison execution timeout must be positive")
        self.execution_timeout_seconds = float(execution_timeout_seconds)
        self.comparison_id, self.run_id, self.request = direct_comparison_identity(
            pair_id=pair.pair_id,
            matcher_id=matcher_id,
            preprocessing_id=preprocessing_id,
            threshold=threshold,
        )
        self.directory = output_root / self.comparison_id.replace(":", "_")
        self._write_directory = self.directory

    def _embed_with_deadline(
        self,
        crops_bgr: np.ndarray | None,
    ) -> tuple[np.ndarray | None, MatcherBinding]:
        registry_path = self.matcher_manager.registry.file_identity.path
        if not registry_path:
            raise DirectPairComparisonError("matcher registry path is not recorded")
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_matcher_embedding_worker,
            kwargs={
                "sender": sender,
                "matcher_id": self.matcher_id.value,
                "registry_path": registry_path,
                "upstream_manifest_path": str(
                    self.matcher_manager.upstream.manifest_path
                ),
                "data_dir": str(self.matcher_manager.upstream.root.parent),
                "crops_bgr": crops_bgr,
            },
            name=f"qualsight-direct-pair-{self.comparison_id[-12:]}",
            daemon=True,
        )
        process.start()
        sender.close()
        try:
            if not receiver.poll(self.execution_timeout_seconds):
                process.terminate()
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=5)
                raise DirectPairComparisonError(
                    "matcher execution exceeded its configured deadline"
                )
            try:
                payload = receiver.recv()
            except EOFError as error:
                raise DirectPairComparisonError(
                    "matcher execution process ended without evidence"
                ) from error
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=5)
                raise DirectPairComparisonError(
                    "matcher execution process did not terminate after producing evidence"
                )
            if process.exitcode != 0 or payload.get("state") != "COMPLETED":
                raise DirectPairComparisonError(
                    "matcher execution failed: "
                    + str(payload.get("error") or f"child exit {process.exitcode}")
                )
            binding = MatcherBinding(**payload["binding"])
            embeddings = payload["embeddings"]
            if embeddings is not None and (
                not isinstance(embeddings, np.ndarray)
                or embeddings.dtype != np.float32
                or embeddings.ndim != 2
                or embeddings.shape[1] != 512
                or not np.all(np.isfinite(embeddings))
            ):
                raise DirectPairComparisonError(
                    "matcher execution returned invalid embedding evidence"
                )
            return embeddings, binding
        finally:
            receiver.close()
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=5)

    def _quality(self, sample_id: str) -> CanonicalJson | None:
        rows = [
            row
            for row in self.store.quality_results_for_sample(sample_id)
            if row["outcome"] == "MEASURED"
        ]
        if not rows:
            return None
        row = rows[-1]
        return CanonicalJson.from_value(
            {
                "failed_measures": sorted(
                    name for name, value in row["measures"].items() if value["failed"]
                ),
                "job_id": row["job_id"],
                "run_id": row["run_id"],
                "unified_quality_score": row["uqs_scalar"],
            }
        )

    def _prepare_side(
        self, side: str, sample: Sample
    ) -> tuple[CropResult | None, DirectPairSideEvidence, np.ndarray | None]:
        source = Path(sample.source_path)
        source_artifact = DirectPairArtifact(
            artifact=f"{side}-source",
            path=str(source.resolve(strict=True)),
            sha256=sample.sha256,
            size_bytes=sample.size_bytes,
            media_type=sample.media_type,
        )
        if sha256_file(source) != sample.sha256:
            evidence = DirectPairSideEvidence(
                side=side,
                sample_id=sample.sample_id,
                dataset_id=sample.dataset_id,
                source_sha256=sample.sha256,
                face_count=None,
                detection=None,
                crop_pixel_sha256=None,
                embedding_value_sha256=None,
                embedding_shape=None,
                embedding_dtype=None,
                failure="INTEGRITY_FAILURE",
                quality=self._quality(sample.sample_id),
                artifacts=(source_artifact,),
            )
            return None, evidence, None
        image = cv2.imread(str(source), cv2.IMREAD_COLOR)
        if image is None:
            evidence = DirectPairSideEvidence(
                side=side,
                sample_id=sample.sample_id,
                dataset_id=sample.dataset_id,
                source_sha256=sample.sha256,
                face_count=None,
                detection=None,
                crop_pixel_sha256=None,
                embedding_value_sha256=None,
                embedding_shape=None,
                embedding_dtype=None,
                failure="FAILURE_TO_ACQUIRE",
                quality=self._quality(sample.sample_id),
                artifacts=(source_artifact,),
            )
            return None, evidence, None
        try:
            result = preprocess(image, self.preprocessing_id, self.detector)
        except (RuntimeError, ValueError, cv2.error):
            evidence = DirectPairSideEvidence(
                side=side,
                sample_id=sample.sample_id,
                dataset_id=sample.dataset_id,
                source_sha256=sample.sha256,
                face_count=None,
                detection=None,
                crop_pixel_sha256=None,
                embedding_value_sha256=None,
                embedding_shape=None,
                embedding_dtype=None,
                failure="FAILURE_TO_ACQUIRE",
                quality=self._quality(sample.sample_id),
                artifacts=(source_artifact,),
            )
            return None, evidence, None
        if result.failure is not None or result.crop_bgr is None:
            evidence = DirectPairSideEvidence(
                side=side,
                sample_id=sample.sample_id,
                dataset_id=sample.dataset_id,
                source_sha256=sample.sha256,
                face_count=result.face_count,
                detection=None,
                crop_pixel_sha256=None,
                embedding_value_sha256=None,
                embedding_shape=None,
                embedding_dtype=None,
                failure=result.failure or "FAILURE_TO_ACQUIRE",
                quality=self._quality(sample.sample_id),
                artifacts=(source_artifact,),
            )
            return result, evidence, None

        artifacts: list[DirectPairArtifact] = [source_artifact]
        crop_path = self._write_directory / f"{side}-crop.png"
        encoded, crop_bytes = cv2.imencode(".png", result.crop_bgr)
        if not encoded:
            raise DirectPairComparisonError("aligned crop encoding failed")
        crop_path.write_bytes(crop_bytes.tobytes())
        artifacts.append(
            _artifact(
                f"{side}-crop",
                crop_path,
                "image/png",
                recorded_path=self.directory / crop_path.name,
            )
        )
        detection_json = None
        if result.detection is not None:
            detection_json = CanonicalJson.from_value(
                _detection_value(result.detection, canvas=result.canvas)
            )
            detection_path = self._write_directory / f"{side}-detection.json"
            detection_path.write_text(detection_json.text, encoding="utf-8")
            artifacts.append(
                _artifact(
                    f"{side}-detection",
                    detection_path,
                    "application/json",
                    recorded_path=self.directory / detection_path.name,
                )
            )
        evidence = DirectPairSideEvidence(
            side=side,
            sample_id=sample.sample_id,
            dataset_id=sample.dataset_id,
            source_sha256=sample.sha256,
            face_count=result.face_count,
            detection=detection_json,
            crop_pixel_sha256=result.crop_sha256,
            embedding_value_sha256=None,
            embedding_shape=None,
            embedding_dtype=None,
            failure=None,
            quality=self._quality(sample.sample_id),
            artifacts=tuple(artifacts),
        )
        return result, evidence, result.crop_bgr

    def _with_embedding(
        self,
        evidence: DirectPairSideEvidence,
        embedding: np.ndarray,
    ) -> DirectPairSideEvidence:
        value = np.ascontiguousarray(embedding, dtype=np.float32)
        embedding_path = self._write_directory / f"{evidence.side}-embedding.npy"
        np.save(embedding_path, value, allow_pickle=False)
        return DirectPairSideEvidence.model_validate(
            {
                **evidence.model_dump(mode="python"),
                "embedding_value_sha256": sha256_bytes(value.tobytes()),
                "embedding_shape": (512,),
                "embedding_dtype": "float32",
                "artifacts": (
                    *evidence.artifacts,
                    _artifact(
                        f"{evidence.side}-embedding",
                        embedding_path,
                        "application/x-npy",
                        recorded_path=self.directory / embedding_path.name,
                    ),
                ),
            }
        )

    def run(self) -> DirectPairComparisonRun:
        started_at = _now()
        clock = time.perf_counter()
        self.store.start_operation(self.comparison_id)
        if self.directory.exists():
            raise DirectPairComparisonError("comparison output already exists")
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        _sync_directory(self.directory.parent)
        try:
            staging = create_staging_directory(
                self.directory,
                operation_kind="DIRECT_PAIR_COMPARISON",
                result_id=self.run_id,
                project_id=self.pair.project_id,
                request_sha256=self.request.sha256,
            )
        except PreparedOutputError as error:
            raise DirectPairComparisonError(
                "comparison output staging failed integrity verification"
            ) from error
        self._write_directory = staging
        promoted = False
        try:
            left_crop, left_evidence, left_pixels = self._prepare_side(
                "left", self.left_sample
            )
            right_crop, right_evidence, right_pixels = self._prepare_side(
                "right", self.right_sample
            )
            score: float | None = None
            failure: str | None = None
            failure_side: str | None = None
            successful: list[tuple[str, np.ndarray]] = []
            if left_pixels is not None:
                successful.append(("left", left_pixels))
            if right_pixels is not None:
                successful.append(("right", right_pixels))
            embedding_input = (
                np.stack([pixels for _, pixels in successful], axis=0)
                if successful
                else None
            )
            embeddings, matcher_binding = self._embed_with_deadline(embedding_input)
            if successful:
                assert embeddings is not None
                by_side = dict(zip((side for side, _ in successful), embeddings))
                if "left" in by_side:
                    left_evidence = self._with_embedding(left_evidence, by_side["left"])
                if "right" in by_side:
                    right_evidence = self._with_embedding(right_evidence, by_side["right"])
                if len(by_side) == 2:
                    candidate = float(
                        cosine_similarity(
                            by_side["left"].reshape(1, 512),
                            by_side["right"].reshape(1, 512),
                        )[0]
                    )
                    if np.isfinite(candidate):
                        score = candidate
                    else:
                        failure = "FAILURE_TO_COMPARE"
                        failure_side = "both"
            if score is None and failure is None:
                failed = [
                    side
                    for side, evidence in (
                        ("left", left_evidence),
                        ("right", right_evidence),
                    )
                    if evidence.failure is not None
                ]
                failure_side = "+".join(failed) if failed else "both"
                kinds = {
                    evidence.failure
                    for evidence in (left_evidence, right_evidence)
                    if evidence.failure is not None
                }
                failure = (
                    "INTEGRITY_FAILURE"
                    if "INTEGRITY_FAILURE" in kinds
                    else "FAILURE_TO_ACQUIRE"
                    if "FAILURE_TO_ACQUIRE" in kinds
                    else "FAILURE_TO_DETECT"
                )

            threshold_value = float(self.threshold.value)
            decision = None
            decision_reason = DirectPairDecisionReason.NO_SCORE_PROCESSING_FAILURE
            agreement = None
            if score is not None:
                decision = (
                    DirectPairDecision.MATCH
                    if score >= threshold_value
                    else DirectPairDecision.NO_MATCH
                )
                decision_reason = (
                    DirectPairDecisionReason.SCORE_GE_FIXED_THRESHOLD
                    if decision is DirectPairDecision.MATCH
                    else DirectPairDecisionReason.SCORE_LT_FIXED_THRESHOLD
                )
                if self.pair.relationship is not OwnerPairRelationship.NOT_RECORDED:
                    agreement = (
                        decision is DirectPairDecision.MATCH
                    ) == (self.pair.relationship is OwnerPairRelationship.SAME_PERSON)
            preprocessing_sha = CanonicalJson.from_value(
                CONDITION_DEFINITIONS[self.preprocessing_id]
            ).sha256
            ended_at = _now()
            run = DirectPairComparisonRun(
                schema_version="1.0.0",
                comparison_id=self.comparison_id,
                run_id=self.run_id,
                project_id=self.pair.project_id,
                pair_id=self.pair.pair_id,
                actor_id=self.actor_id,
                left_sample_id=self.left_sample.sample_id,
                right_sample_id=self.right_sample.sample_id,
                left_dataset_id=self.left_sample.dataset_id,
                right_dataset_id=self.right_sample.dataset_id,
                left_source_sha256=self.left_sample.sha256,
                right_source_sha256=self.right_sample.sha256,
                relationship=self.pair.relationship,
                relationship_assertion=self.pair.relationship_assertion,
                matcher_id=self.matcher_id.value,
                matcher_checkpoint_sha256=matcher_binding.checkpoint_sha256,
                matcher_architecture_sha256=matcher_binding.architecture_source_sha256,
                matcher_device=matcher_binding.device,
                preprocessing_id=self.preprocessing_id.value,
                preprocessing_definition_sha256=preprocessing_sha,
                detector_sha256=self.detector.sha256,
                threshold=self.threshold,
                request_sha256=self.request.sha256,
                output_directory=str(self.directory.resolve(strict=False)),
                started_at=started_at,
                ended_at=ended_at,
                runtime_seconds=float(time.perf_counter() - clock),
                left=left_evidence,
                right=right_evidence,
                result=DirectPairResult(
                    score=score,
                    failure=failure,
                    failure_side=failure_side,
                    decision=decision,
                    decision_reason=decision_reason,
                    agreement_with_operator_assertion=agreement,
                ),
                application_version=application_version(
                    self.repository_root / "backend" / "pyproject.toml"
                ),
                population_error_claims_allowed=False,
                outcome_correctness_allowed=self.pair.outcome_correctness_allowed,
                ground_truth_authority=False,
                source_kind="OWNER_SUPPLIED_LOCAL_INPUT",
                local_only=True,
            )
            _write_prepared_run(staging, run)
            _sync_tree(staging)
            load_staging_identity(
                staging,
                target_name=self.directory.name,
                operation_kind="DIRECT_PAIR_COMPARISON",
                result_id=self.run_id,
                project_id=self.pair.project_id,
                request_sha256=self.request.sha256,
            )
            _publish_directory_no_clobber(staging, self.directory)
            promoted = True
            _sync_directory(self.directory.parent)
            recovered = recover_prepared_direct_pair(
                self.directory,
                comparison_id=self.comparison_id,
                run_id=self.run_id,
                project_id=self.pair.project_id,
                request_sha256=self.request.sha256,
            )
            self.store.record_direct_pair_comparison(recovered)
            return recovered
        except BaseException:
            if not promoted:
                discard_staging_directory(staging)
            raise
        finally:
            self._write_directory = self.directory
