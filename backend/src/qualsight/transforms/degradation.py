"""Degradation-response analysis over a derived set (PRD §27; QS-X61).

For every transform and level of a derived set: the OFIQ quality response
(every scalar measure's mean and the UnifiedQualityScore distribution against
the untransformed baseline job), the matcher response (frozen matcher on the
GPU: transformed left image versus the original right image of every recorded
protocol pair whose left sample was transformed — mated score, FNMR at the
fixed MATCHER THRESHOLD, FMR for non-mated pairs), and derived research
summaries (relative loss, response slope over the level index, area under the
response, threshold crossing, failure onset). Everything is written into an
immutable analysis Run that binds the derived set, both quality jobs, the
matcher, the preprocessing condition, and the threshold.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

import cv2
import numpy as np
import pyarrow.parquet as pq

from ..domain.entities import Artifact, ArtifactKind, DataClassification, Experiment, ProtocolKind, StorageBackend, VerificationProtocol
from ..domain.primitives import ArtifactIdentity as DomainArtifactIdentity, CanonicalJson, ComponentBinding, ComponentState
from ..domain.run import CompletedRun, LogRecord, MetricObservation, RunStatus, ScientificSupportState
from ..engines.ofiq.adapter import content_id
from ..execution.prepared_output import (
    PreparedOutputError,
    create_staging_directory,
    discard_staging_directory,
    load_prepared_output,
    prepare_and_publish,
)
from ..matchers.alignment import ARCFACE_TEMPLATE_SHA256
from ..matchers.bounded import embed_with_deadline, validate_execution_timeout
from ..matchers.conditions import CONDITION_DEFINITIONS, ConditionId, preprocess
from ..matchers.detection import SCRFDDetector
from ..matchers.matrix import SCRFD_RESOURCE_ID
from ..matchers.runtime import COLOR_ORDER, TRANSFORM_IDS, FrozenMatcher, MatcherId, cosine_similarity
from ..metrics.statistics import clopper_pearson
from ..provenance import application_version, environment_identity, hardware_identity, repository_identity, sha256_bytes, sha256_file
from .operators import PARAMETER_NAMES

ANALYSIS_KIND = "DEGRADATION_RESPONSE"
ANALYSIS_VERSION = "1.0.0"


class DegradationAnalysisError(RuntimeError):
    """Raised when the analysis cannot be executed faithfully on the recorded inputs."""


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


@dataclass(frozen=True, slots=True)
class DegradationRequest:
    derived_set_id: str
    quality_job_id: str
    baseline_quality_job_id: str | None
    matcher_id: str
    condition_id: str
    matcher_threshold: float
    protocol_id: str | None = None


@dataclass(frozen=True, slots=True)
class DegradationResult:
    analysis_id: str
    run: CompletedRun
    experiment: Experiment
    document: dict[str, Any]
    directory: Path
    document_path: Path
    summary: dict[str, Any]


DEGRADATION_ARTIFACT_NAMES = ("degradation.json",)


def recover_prepared_degradation_analysis(
    directory: Path,
    *,
    analysis_id: str,
    project_id: str,
    request_sha256: str,
) -> DegradationResult:
    """Reconstruct a typed degradation result from its final receipt."""

    try:
        receipt = load_prepared_output(
            directory,
            operation_kind="DEGRADATION_ANALYSIS",
            result_id=analysis_id,
            project_id=project_id,
            request_sha256=request_sha256,
            artifact_names=DEGRADATION_ARTIFACT_NAMES,
        )
        record = receipt["record"]
        if set(record) != {"experiment", "run", "summary"}:
            raise DegradationAnalysisError(
                "prepared degradation result contract differs"
            )
        run = CompletedRun.model_validate_json(
            json.dumps(record["run"], separators=(",", ":"), ensure_ascii=False)
        )
        experiment = Experiment.model_validate_json(
            json.dumps(record["experiment"], separators=(",", ":"), ensure_ascii=False)
        )
        summary = record["summary"]
        document_path = directory / "degradation.json"
        document = json.loads(document_path.read_text(encoding="utf-8"))
        if (
            run.project_id != project_id
            or experiment.project_id != project_id
            or summary.get("analysis_id") != analysis_id
            or summary.get("run_id") != run.run_id
            or {artifact.uri for artifact in run.artifacts} != {str(document_path)}
            or any(
                sha256_file(Path(artifact.uri)) != artifact.sha256
                for artifact in run.artifacts
            )
        ):
            raise DegradationAnalysisError(
                "prepared degradation result identity differs"
            )
        return DegradationResult(
            analysis_id,
            run,
            experiment,
            document,
            directory,
            document_path,
            summary,
        )
    except (PreparedOutputError, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        if isinstance(error, DegradationAnalysisError):
            raise
        raise DegradationAnalysisError(
            "prepared degradation result failed integrity verification"
        ) from error


def _quality_by_sample(parquet_path: str) -> tuple[dict[str, dict[str, float | None]], list[str]]:
    table = pq.read_table(parquet_path)
    measures = sorted({name[: -len(".scalar")] for name in table.schema.names if name.endswith(".scalar")})
    out: dict[str, dict[str, float | None]] = {}
    for record in table.to_pylist():
        if record["outcome"] != "MEASURED":
            continue
        out[record["sample_id"]] = {m: (None if record[f"{m}.failed"] or record[f"{m}.scalar"] is None else float(record[f"{m}.scalar"])) for m in measures}
    return out, measures


def _mean(values: list[float | None]) -> float | None:
    finite = [v for v in values if v is not None]
    return float(np.mean(finite)) if finite else None


class DegradationAnalysisJob:
    def __init__(self, *, store: Any, registry: Any, upstream: Any, repository_root: Path, project: Any, request: DegradationRequest, job_root: Path, actor: str, execution_timeout_seconds: float, purpose_scope: str = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1", runtime_manager: Any | None = None) -> None:
        self.store = store
        self.registry = registry
        self.upstream = upstream
        self.repository_root = repository_root
        self.project = project
        self.request = request
        self.actor = actor
        self.purpose_scope = purpose_scope
        self.runtime_manager = runtime_manager
        try:
            self.execution_timeout_seconds = validate_execution_timeout(
                execution_timeout_seconds
            )
        except ValueError as error:
            raise DegradationAnalysisError(str(error)) from error
        derived = store.derived_set(request.derived_set_id)
        if derived is None or derived["project_id"] != project.project_id:
            raise DegradationAnalysisError("derived set is not recorded in this project")
        self.derived = derived
        self.rows = store.derived_samples(request.derived_set_id)
        job = store.job(request.quality_job_id)
        if job is None or job.get("state") != "COMPLETED" or job["dataset_id"] != derived["derived_dataset_id"]:
            raise DegradationAnalysisError("quality job is not a completed job over this derived dataset")
        self.quality_job = job
        self.baseline_job = None
        if request.baseline_quality_job_id:
            baseline = store.job(request.baseline_quality_job_id)
            if baseline is None or baseline.get("state") != "COMPLETED" or baseline["dataset_id"] != derived["source_dataset_id"]:
                raise DegradationAnalysisError("baseline quality job is not a completed job over the source dataset")
            self.baseline_job = baseline
        try:
            self.matcher_id = MatcherId(request.matcher_id)
            self.condition = ConditionId(request.condition_id)
        except ValueError as error:
            raise DegradationAnalysisError(f"unknown matcher or condition: {error}") from error
        for resource_id in (SCRFD_RESOURCE_ID, self.matcher_id.value):
            if purpose_scope not in registry.resource(resource_id).approved_scopes:
                raise DegradationAnalysisError(f"{resource_id} does not approve scope {purpose_scope}")
        protocols = [p for p in store.protocols_for_project(project.project_id) if p.dataset_id == derived["source_dataset_id"]]
        if request.protocol_id:
            protocols = [p for p in protocols if p.protocol_id == request.protocol_id]
        if not protocols:
            raise DegradationAnalysisError("no frozen verification protocol over the source dataset; freeze one first")
        self.protocol = protocols[-1]
        scrfd = registry.resource(SCRFD_RESOURCE_ID)
        self.detector_identity = next(item.identity for item in scrfd.artifacts if item.role == "MODEL")
        self.detector = SCRFDDetector(Path(self.detector_identity.path), self.detector_identity.sha256)
        self.analysis_id = content_id("analysis", ANALYSIS_KIND, ANALYSIS_VERSION, request.derived_set_id, request.quality_job_id, request.baseline_quality_job_id or "", request.matcher_id, request.condition_id, f"{request.matcher_threshold:.10f}", self.protocol.protocol_id)
        self.directory = job_root / self.analysis_id.replace(":", "_")
        self.progress: dict[str, Any] = {"phase": "created", "detail": ""}

    def _template(self, path: str, expected_sha: str) -> np.ndarray | None:
        file = Path(path)
        if sha256_file(file) != expected_sha:
            return None
        image = cv2.imread(str(file), cv2.IMREAD_COLOR)
        if image is None:
            return None
        result = preprocess(image, self.condition, self.detector)
        return None if result.failure is not None or result.crop_bgr is None else result.crop_bgr

    def run(self, *, request_sha256: str | None = None) -> DegradationResult:
        lease = self.runtime_manager.lease(self.matcher_id) if self.runtime_manager is not None else nullcontext(FrozenMatcher(self.matcher_id, self.registry, self.upstream))
        with lease as matcher:
            return self._execute(matcher, request_sha256=request_sha256)

    def _execute(self, matcher: FrozenMatcher, *, request_sha256: str | None = None) -> DegradationResult:
        started = _now()
        clock = time.perf_counter()
        request = self.request
        derived_quality, measures = _quality_by_sample(self.quality_job["parquet_path"])
        baseline_quality, _ = _quality_by_sample(self.baseline_job["parquet_path"]) if self.baseline_job else ({}, [])
        source_ids = sorted({r["parent_sample_id"] for r in self.rows})
        source_set = set(source_ids)
        pairs = [p for p in self.store.protocol_pairs(self.protocol.protocol_id, limit=1_000_000) if p.left_sample_id in source_set]
        # Templates: originals (left and right of the selected pairs) and every derived left image.
        self.progress = {"phase": "templates", "detail": "originals"}
        original_ids = sorted({p.left_sample_id for p in pairs} | {p.right_sample_id for p in pairs})
        crops: list[np.ndarray] = []
        index: dict[str, int] = {}
        failures: dict[str, str] = {}
        for sample_id in original_ids:
            sample = self.store.sample(sample_id)
            crop = self._template(sample.source_path, sample.sha256) if sample else None
            if crop is None:
                failures[sample_id] = "FAILURE_TO_DETECT"
                continue
            index[sample_id] = len(crops)
            crops.append(crop)
        derived_by_level: dict[tuple[str, float], list[dict[str, Any]]] = {}
        for row in self.rows:
            derived_by_level.setdefault((row["transform"], row["parameter"]), []).append(row)
        for position, row in enumerate(self.rows):
            if row["parent_sample_id"] not in {p.left_sample_id for p in pairs}:
                continue
            crop = self._template(row["path"], row["sha256"])
            if crop is None:
                failures[row["derived_sample_id"]] = "FAILURE_TO_DETECT"
                continue
            index[row["derived_sample_id"]] = len(crops)
            crops.append(crop)
            if position % 100 == 0:
                self.progress = {"phase": "templates", "detail": f"derived {position}/{len(self.rows)}"}
        self.progress = {"phase": "embed", "detail": f"{len(crops)} templates on {matcher.device}"}
        embeddings = (
            embed_with_deadline(
                matcher_id=matcher.matcher_id,
                registry=self.registry,
                upstream=self.upstream,
                crops_bgr=np.stack(crops),
                timeout_seconds=self.execution_timeout_seconds,
                process_label=self.analysis_id,
            )
            if crops
            else np.zeros((0, 512), dtype=np.float32)
        )
        threshold = float(request.matcher_threshold)

        def score(left_id: str, right_id: str) -> float | None:
            a, b = index.get(left_id), index.get(right_id)
            return None if a is None or b is None else float(cosine_similarity(embeddings[[a]], embeddings[[b]])[0])

        mated_pairs = [p for p in pairs if p.label.value == "MATED"]
        nonmated_pairs = [p for p in pairs if p.label.value == "NON_MATED"]
        baseline_scores = {p.pair_id: score(p.left_sample_id, p.right_sample_id) for p in pairs}
        baseline_block = {
            "mated_pairs": len(mated_pairs), "nonmated_pairs": len(nonmated_pairs),
            "mated_score_mean": _mean([baseline_scores[p.pair_id] for p in mated_pairs]),
            "fnmr": clopper_pearson(sum(1 for p in mated_pairs if baseline_scores[p.pair_id] is not None and baseline_scores[p.pair_id] < threshold), sum(1 for p in mated_pairs if baseline_scores[p.pair_id] is not None)),
            "fmr": clopper_pearson(sum(1 for p in nonmated_pairs if baseline_scores[p.pair_id] is not None and baseline_scores[p.pair_id] >= threshold), sum(1 for p in nonmated_pairs if baseline_scores[p.pair_id] is not None)),
            "quality": {m: _mean([baseline_quality.get(s, {}).get(m) for s in source_ids]) for m in measures} if baseline_quality else None,
        }
        responses = []
        transforms_in_order: list[str] = []
        for (kind, parameter) in derived_by_level:
            if kind not in transforms_in_order:
                transforms_in_order.append(kind)
        for kind in transforms_in_order:
            levels = []
            level_keys = sorted((k for k in derived_by_level if k[0] == kind), key=lambda k: next(r["level_index"] for r in derived_by_level[k]))
            for key in level_keys:
                rows = derived_by_level[key]
                by_parent = {r["parent_sample_id"]: r for r in rows}
                mated_scores = []
                nonmated_scores = []
                for p in mated_pairs:
                    r = by_parent.get(p.left_sample_id)
                    mated_scores.append(score(r["derived_sample_id"], p.right_sample_id) if r else None)
                for p in nonmated_pairs:
                    r = by_parent.get(p.left_sample_id)
                    nonmated_scores.append(score(r["derived_sample_id"], p.right_sample_id) if r else None)
                m_valid = [s for s in mated_scores if s is not None]
                n_valid = [s for s in nonmated_scores if s is not None]
                levels.append({
                    "parameter": key[1], "level_index": rows[0]["level_index"], "derived_samples": len(rows),
                    "quality": {m: _mean([derived_quality.get(r["derived_sample_id"], {}).get(m) for r in rows]) for m in measures},
                    "quality_measured": sum(1 for r in rows if r["derived_sample_id"] in derived_quality),
                    "mated_pairs": len(mated_pairs), "mated_scored": len(m_valid), "mated_score_mean": _mean(m_valid), "mated_score_min": min(m_valid) if m_valid else None,
                    "fnmr": clopper_pearson(sum(1 for s in m_valid if s < threshold), len(m_valid)),
                    "nonmated_pairs": len(nonmated_pairs), "nonmated_scored": len(n_valid), "nonmated_score_mean": _mean(n_valid),
                    "fmr": clopper_pearson(sum(1 for s in n_valid if s >= threshold), len(n_valid)),
                    "template_failures": sum(1 for r in rows if r["derived_sample_id"] in failures),
                    "pair_scores": [{"pair_id": p.pair_id, "label": p.label.value, "derived_sample_id": by_parent[p.left_sample_id]["derived_sample_id"] if p.left_sample_id in by_parent else None, "score": s, "baseline_score": baseline_scores[p.pair_id]} for p, s in list(zip(mated_pairs, mated_scores)) + list(zip(nonmated_pairs, nonmated_scores))],
                })
            fnmr_curve = [(lvl["level_index"], lvl["fnmr"]["rate"]) for lvl in levels]
            score_curve = [(lvl["level_index"], lvl["mated_score_mean"]) for lvl in levels]
            uqs_curve = [(lvl["level_index"], lvl["quality"].get("UnifiedQualityScore")) for lvl in levels]

            def summaries(curve: list[tuple[int, float | None]], baseline_value: float | None, crossing_rule: str, crossing_test) -> dict[str, Any]:
                xs = [float(i) for i, (_, v) in enumerate(curve) if v is not None]
                ys = [v for _, v in curve if v is not None]
                out: dict[str, Any] = {"levels": len(curve), "defined": len(ys), "x_axis": "level position 0..n-1 in recorded order (parameter units differ per transform; the level parameters are recorded)"}
                if len(ys) >= 2:
                    slope = float(np.polyfit(xs, ys, 1)[0])
                    out["slope_per_level"] = slope
                    out["area_under_response"] = float(np.trapezoid(ys, xs)) if hasattr(np, "trapezoid") else float(np.trapz(ys, xs))
                else:
                    out["slope_per_level"] = None
                    out["area_under_response"] = None
                out["baseline"] = baseline_value
                out["relative_change_last_level"] = ((ys[-1] - baseline_value) / baseline_value) if ys and baseline_value not in (None, 0) else None
                crossing = next((curve[i][0] for i in range(len(curve)) if curve[i][1] is not None and crossing_test(curve[i][1])), None)
                out["threshold_crossing"] = {"rule": crossing_rule, "first_level_index": crossing, "parameter": next((lvl["parameter"] for lvl in levels if lvl["level_index"] == crossing), None)}
                return out

            responses.append({
                "transform": kind, "parameter_name": PARAMETER_NAMES[kind],
                "levels": levels, "baseline": baseline_block,
                "derived_summaries": {
                    "fnmr": summaries(fnmr_curve, baseline_block["fnmr"]["rate"], "first level whose FNMR exceeds the baseline FNMR", lambda v: baseline_block["fnmr"]["rate"] is not None and v > baseline_block["fnmr"]["rate"]),
                    "mated_score": summaries(score_curve, baseline_block["mated_score_mean"], f"first level whose mean mated score falls below the matcher threshold {threshold}", lambda v: v < threshold),
                    "unified_quality_score": summaries(uqs_curve, (baseline_block["quality"] or {}).get("UnifiedQualityScore"), "first level whose mean UnifiedQualityScore falls below 90 % of the baseline", lambda v: (baseline_block["quality"] or {}).get("UnifiedQualityScore") is not None and v < 0.9 * (baseline_block["quality"] or {}).get("UnifiedQualityScore")),
                    "failure_onset": {"rule": "first level with any false non-match among the mated pairs", "first_level_index": next((lvl["level_index"] for lvl in levels if lvl["fnmr"]["events"] > 0), None)},
                },
            })
        effective_request_sha256 = request_sha256 or CanonicalJson.from_value(
            asdict(self.request)
        ).sha256
        stage = create_staging_directory(
            self.directory,
            operation_kind="DEGRADATION_ANALYSIS",
            result_id=self.analysis_id,
            project_id=self.project.project_id,
            request_sha256=effective_request_sha256,
        )
        document = {
            "analysis_id": self.analysis_id, "kind": ANALYSIS_KIND, "version": ANALYSIS_VERSION,
            "sources": {"derived_set_id": request.derived_set_id, "derived_dataset_id": self.derived["derived_dataset_id"], "source_dataset_id": self.derived["source_dataset_id"], "derived_manifest_sha256": self.derived["manifest_sha256"], "quality_job_id": request.quality_job_id, "quality_run_id": self.quality_job["run_id"], "quality_parquet_sha256": self.quality_job["parquet_sha256"],
                        "baseline_quality_job_id": request.baseline_quality_job_id, "baseline_quality_run_id": self.baseline_job["run_id"] if self.baseline_job else None, "protocol_id": self.protocol.protocol_id, "protocol_pairs_sha256": self.protocol.pairs_sha256, "matcher_id": self.matcher_id.value, "condition_id": self.condition.value},
            "matcher_threshold": {"kind": "MATCHER_THRESHOLD", "value": threshold, "source": "CALLER_SUPPLIED", "rule": "ACCEPT_WHEN_SCORE_GE_THRESHOLD"},
            "pair_selection": {"rule": "protocol pairs whose left sample belongs to the derived set's sources; the transformed left image is compared with the original right image", "mated_pairs": len(mated_pairs), "nonmated_pairs": len(nonmated_pairs)},
            "template_failures": failures, "measures": measures, "responses": responses,
        }
        document_path = stage / "degradation.json"
        document_path.write_text(json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        seconds = time.perf_counter() - clock
        run, experiment = self._run(started, seconds, document, document_path, matcher, threshold, str(matcher.device), artifact_directory=self.directory)
        summary = {"analysis_id": self.analysis_id, "run_id": run.run_id, "derived_set_id": request.derived_set_id, "matcher_id": self.matcher_id.value, "condition_id": self.condition.value, "matcher_threshold": threshold, "transforms": [r["transform"] for r in responses],
                   "levels": sum(len(r["levels"]) for r in responses), "mated_pairs": len(mated_pairs), "nonmated_pairs": len(nonmated_pairs), "seconds": round(seconds, 1),
                   "fnmr_by_transform": {r["transform"]: [lvl["fnmr"]["rate"] for lvl in r["levels"]] for r in responses}, "uqs_by_transform": {r["transform"]: [lvl["quality"].get("UnifiedQualityScore") for lvl in r["levels"]] for r in responses}}
        result = DegradationResult(
            self.analysis_id,
            run,
            experiment,
            document,
            self.directory,
            self.directory / document_path.name,
            summary,
        )
        try:
            prepare_and_publish(
                stage,
                self.directory,
                operation_kind="DEGRADATION_ANALYSIS",
                result_id=self.analysis_id,
                project_id=self.project.project_id,
                request_sha256=effective_request_sha256,
                artifact_names=DEGRADATION_ARTIFACT_NAMES,
                record={
                    "run": json.loads(run.model_dump_json()),
                    "experiment": json.loads(experiment.model_dump_json()),
                    "summary": summary,
                },
            )
        except BaseException:
            discard_staging_directory(stage)
            raise
        self.store.record_degradation_analysis(result)
        self.progress = {"phase": "completed", "detail": ""}
        return result

    def _run(self, started: datetime, seconds: float, document: dict[str, Any], document_path: Path, matcher: FrozenMatcher, threshold: float, device: str, *, artifact_directory: Path | None = None) -> tuple[CompletedRun, Experiment]:
        binding = matcher.binding
        checkpoint_identity = next(item.identity for item in self.registry.resource(self.matcher_id.value).artifacts if item.role == "MODEL")
        quality_run = self.store.run(self.quality_job["run_id"])
        if quality_run is None:
            raise DegradationAnalysisError("quality Run is not recorded")
        quality_run = quality_run[0]
        dataset = self.store.dataset(self.derived["derived_dataset_id"])
        configuration = CanonicalJson.from_value({"analysis_id": self.analysis_id, "kind": ANALYSIS_KIND, "version": ANALYSIS_VERSION, "sources": document["sources"], "matcher_threshold": document["matcher_threshold"], "pair_selection": document["pair_selection"], "transforms": self.derived["transforms"], "operator_version": self.derived["operator_version"], "purpose_scope": self.purpose_scope})
        experiment_id = content_id("experiment", self.project.project_id, self.derived["derived_dataset_id"], ANALYSIS_KIND, self.request.derived_set_id, self.request.quality_job_id, self.matcher_id.value, self.condition.value)
        run_id = content_id("run", experiment_id, started.isoformat(), sha256_file(document_path))
        observations = [MetricObservation(metric_id="derived.levels", value=sum(len(r["levels"]) for r in document["responses"]), unit="count"), MetricObservation(metric_id="pairs.mated", value=document["pair_selection"]["mated_pairs"], unit="count"), MetricObservation(metric_id="timing.analysis_seconds", value=round(seconds, 3), unit="observation")]
        for response in document["responses"]:
            for level in response["levels"]:
                if level["fnmr"]["rate"] is not None:
                    observations.append(MetricObservation(metric_id=f"degradation.fnmr[{response['transform']}={level['parameter']:g}]", value=float(level["fnmr"]["rate"]), unit="rate"))
                uqs = level["quality"].get("UnifiedQualityScore")
                if uqs is not None:
                    observations.append(MetricObservation(metric_id=f"degradation.uqs_mean[{response['transform']}={level['parameter']:g}]", value=float(uqs), unit="observation"))
        digest = sha256_file(document_path)
        artifact_path = (artifact_directory / document_path.name) if artifact_directory is not None else document_path
        artifacts = (Artifact(artifact_id=content_id("artifact", run_id, digest, document_path.name), run_id=run_id, kind=ArtifactKind.METRICS, storage_backend=StorageBackend.FILESYSTEM, uri=str(artifact_path), sha256=digest, size_bytes=document_path.stat().st_size, media_type="application/json", classification=DataClassification.SENSITIVE_BIOMETRIC_DERIVED, created_at=_now()),)
        detector_domain = DomainArtifactIdentity(path=self.detector_identity.path, sha256=self.detector_identity.sha256, size_bytes=self.detector_identity.size_bytes, mtime_ns=self.detector_identity.mtime_ns)
        checkpoint_domain = DomainArtifactIdentity(path=checkpoint_identity.path, sha256=checkpoint_identity.sha256, size_bytes=checkpoint_identity.size_bytes, mtime_ns=checkpoint_identity.mtime_ns)
        transform_sha = sha256_bytes(f"{TRANSFORM_IDS[self.matcher_id]}:{COLOR_ORDER[self.matcher_id]}".encode("utf-8"))
        run = CompletedRun(
            run_id=run_id, project_id=self.project.project_id, experiment_id=experiment_id, user_id=self.actor, dataset_manifest=dataset.manifest, dataset_hash=dataset.manifest.sha256,
            subject_protocol=ComponentBinding(state=ComponentState.CONFIGURED, component_id="lfw-directory-identity", version="1.0.0"),
            pair_search_protocol=ComponentBinding(state=ComponentState.CONFIGURED, component_id=self.protocol.protocol_id, version="LFW_VIEW2", configuration_sha256=self.protocol.pairs_sha256),
            quality_engine=quality_run.quality_engine, quality_engine_configuration=quality_run.quality_engine_configuration, model_hashes=(checkpoint_domain, detector_domain),
            matcher=ComponentBinding(state=ComponentState.CONFIGURED, component_id=self.matcher_id.value, version=binding.checkpoint_sha256[:16], configuration_sha256=transform_sha, artifact_hashes=(binding.checkpoint_sha256, *binding.architecture_source_sha256)),
            matcher_preprocessing=ComponentBinding(state=ComponentState.CONFIGURED, component_id=self.condition.value, version="1.0.0", configuration_sha256=sha256_bytes(json.dumps(CONDITION_DEFINITIONS[self.condition], sort_keys=True).encode("utf-8")), artifact_hashes=(self.detector_identity.sha256, ARCFACE_TEMPLATE_SHA256)),
            operating_thresholds=ComponentBinding(state=ComponentState.CONFIGURED, component_id="caller-supplied-matcher-threshold", version="1.0.0", configuration_sha256=sha256_bytes(f"{threshold:.10f}".encode("utf-8"))),
            statistical_methods=ComponentBinding(state=ComponentState.CONFIGURED, component_id="qualsight.transforms.degradation", version="1.0.0", configuration_sha256=sha256_bytes(b"clopper-pearson; least-squares slope over level index; trapezoid area")),
            confidence_interval_method=ComponentBinding(state=ComponentState.CONFIGURED, component_id="CLOPPER_PEARSON_EXACT_BINOMIAL_95", version="1.0.0"),
            randomness=ComponentBinding(state=ComponentState.NOT_APPLICABLE, reason="Deterministic transforms, detection, embedding, and rate computation contain no random-seed setting."), random_seeds=(),
            application_version=application_version(self.repository_root / "backend" / "pyproject.toml"), repository=repository_identity(self.repository_root), environment=environment_identity(self.repository_root / "uv.lock", {}), hardware=hardware_identity(f"matcher on {device}; detector on CPUExecutionProvider"),
            start_time=started, end_time=_now(), runtime_seconds=round(seconds, 3), initialization_ms=0, assessment_ms=int(seconds * 1000), configuration=configuration, metrics=tuple(observations), artifacts=artifacts,
            logs=(LogRecord(stream="stdout", sha256=sha256_bytes(b""), text=""), LogRecord(stream="stderr", sha256=sha256_bytes(b""), text="")),
            warnings=tuple(w for w in (f"{len(document['template_failures'])} templates failed" if document["template_failures"] else None,) if w),
            provenance_limits=tuple(self.registry.raw_resource(self.matcher_id.value)["use_restrictions"]) + ("Derived images are controlled transformations of registered real inputs; every level records its operator, parameters, and parent sample; no identity or sample is invented.", "Responses are QualSight-executed on funneled LFW with SCRFD alignment; they are not published-figure reproductions."),
            exit_code=0, status=RunStatus.COMPLETED, scientific_support=ScientificSupportState.NOT_ASSESSED,
        )
        experiment = Experiment(experiment_id=experiment_id, project_id=self.project.project_id, dataset_id=self.derived["derived_dataset_id"], name=f"degradation: {self.request.derived_set_id[-12:]} × {self.matcher_id.value}", created_by=self.actor, created_at=started,
                                protocol=VerificationProtocol(kind=ProtocolKind.VERIFICATION, protocol_id=self.protocol.protocol_id, pair_manifest=self.protocol.pair_manifest, identity_schema=ComponentBinding(state=ComponentState.CONFIGURED, component_id="lfw-directory-identity", version="1.0.0")), configuration=configuration)
        return run, experiment
