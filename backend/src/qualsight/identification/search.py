"""1:N identification search over a frozen protocol with the frozen matcher runtime (QS-X60).

Gallery and probe templates come from the same registered SCRFD detector,
alignment, and preprocessing condition as the 1:1 matrix; embeddings are
computed by the frozen matcher on the GPU when available and the full
probe × gallery cosine similarity is computed on the same device. Every search
keeps its ordered candidate list (rank, gallery sample, subject, score) up to
the protocol's rank depth; failed probe or gallery templates are explicit.
The Run binds the matcher, the preprocessing condition, the detector and
checkpoint bytes, and the frozen protocol; gallery-size effects re-evaluate the
same similarities on deterministic gallery subsets.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

import cv2
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import torch

from ..domain.comparison import ProcessingFailureKind
from ..domain.entities import Artifact, ArtifactKind, DataClassification, Experiment, IdentificationMode, IdentificationProtocol, ProtocolKind, StorageBackend
from ..domain.primitives import ArtifactIdentity as DomainArtifactIdentity, CanonicalJson, ComponentBinding, ComponentState
from ..domain.run import CompletedRun, LogRecord, MetricObservation, RunStatus, ScientificSupportState
from ..engines.ofiq.adapter import _not_applicable, content_id
from ..matchers.alignment import ARCFACE_TEMPLATE_SHA256
from ..matchers.conditions import CONDITION_DEFINITIONS, ConditionId, preprocess
from ..matchers.detection import SCRFDDetector
from ..matchers.matrix import SCRFD_RESOURCE_ID
from ..matchers.runtime import COLOR_ORDER, TRANSFORM_IDS, FrozenMatcher, MatcherId
from ..metrics.identification import SearchRecord, compute_identification_metrics
from ..provenance import application_version, environment_identity, hardware_identity, repository_identity, sha256_bytes, sha256_file

SEARCH_VERSION = "1.0.0"
GALLERY_EFFECT_SIZES = (100, 500, 1000, 2000)
GALLERY_EFFECT_SEED = 20260818


class IdentificationSearchError(RuntimeError):
    """Raised when a search cannot be executed faithfully."""


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


@dataclass(frozen=True, slots=True)
class SearchResult:
    search_id: str
    run: CompletedRun
    experiment: Experiment
    metrics: dict[str, Any]
    candidates_parquet: Path
    metrics_path: Path
    directory: Path
    summary: dict[str, Any]


class IdentificationSearchJob:
    def __init__(self, *, store: Any, registry: Any, upstream: Any, repository_root: Path, project: Any, protocol: dict[str, Any], entries: list[dict[str, Any]], matcher_id: str, condition_id: str, job_root: Path, actor: str, purpose_scope: str = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1", quality_job_id: str | None = None) -> None:
        self.store = store
        self.registry = registry
        self.upstream = upstream
        self.repository_root = repository_root
        self.project = project
        self.protocol = protocol
        self.entries = entries
        self.actor = actor
        self.purpose_scope = purpose_scope
        self.quality_job_id = quality_job_id
        try:
            self.matcher_id = MatcherId(matcher_id)
            self.condition = ConditionId(condition_id)
        except ValueError as error:
            raise IdentificationSearchError(f"unknown matcher or condition: {error}") from error
        dataset = store.dataset(protocol["dataset_id"])
        if dataset is None:
            raise IdentificationSearchError("protocol dataset is not recorded")
        self.dataset = dataset
        for resource_id in (SCRFD_RESOURCE_ID, self.matcher_id.value):
            resource = registry.resource(resource_id)
            if purpose_scope not in resource.approved_scopes:
                raise IdentificationSearchError(f"{resource_id} does not approve scope {purpose_scope}")
        scrfd = registry.resource(SCRFD_RESOURCE_ID)
        self.detector_identity = next(item.identity for item in scrfd.artifacts if item.role == "MODEL")
        self.detector = SCRFDDetector(Path(self.detector_identity.path), self.detector_identity.sha256)
        self.search_id = content_id("identification-search", project.project_id, protocol["protocol_id"], matcher_id, condition_id, _now().isoformat())
        self.directory = job_root / self.search_id.replace(":", "_")
        self.directory.mkdir(parents=True, exist_ok=False)
        self.progress: dict[str, Any] = {"phase": "created", "detail": ""}

    # -- templates ----------------------------------------------------------------

    def _templates(self, sample_ids: list[str]) -> tuple[np.ndarray, dict[str, int], dict[str, str], float]:
        started = time.perf_counter()
        crops: list[np.ndarray] = []
        index: dict[str, int] = {}
        failures: dict[str, str] = {}
        for position, sample_id in enumerate(sample_ids):
            sample = self.store.sample(sample_id)
            if sample is None:
                failures[sample_id] = ProcessingFailureKind.FAILURE_TO_ACQUIRE.value
                continue
            path = Path(sample.source_path)
            if sha256_file(path) != sample.sha256:
                failures[sample_id] = ProcessingFailureKind.INTEGRITY_FAILURE.value
                continue
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                failures[sample_id] = ProcessingFailureKind.FAILURE_TO_ACQUIRE.value
                continue
            result = preprocess(image, self.condition, self.detector)
            if result.failure is not None or result.crop_bgr is None:
                failures[sample_id] = ProcessingFailureKind.FAILURE_TO_DETECT.value
                continue
            index[sample_id] = len(crops)
            crops.append(result.crop_bgr)
            if position % 500 == 0:
                self.progress = {"phase": "preprocess", "detail": f"{position}/{len(sample_ids)}"}
        return (np.stack(crops) if crops else np.zeros((0, 112, 112, 3), dtype=np.uint8)), index, failures, time.perf_counter() - started

    # -- search -------------------------------------------------------------------

    @staticmethod
    def _records(similarity: torch.Tensor, probes: list[dict[str, Any]], probe_index: dict[str, int], probe_failures: dict[str, str], gallery: list[dict[str, Any]], gallery_index: dict[str, int], gallery_failures: dict[str, str], gallery_positions: list[int], rank_depth: int) -> tuple[list[SearchRecord], list[dict[str, Any]]]:
        """Candidate lists and search records over the gallery columns ``gallery_positions`` (embedding rows)."""

        records: list[SearchRecord] = []
        rows: list[dict[str, Any]] = []
        gallery_subject_by_row = {gallery_index[e["sample_id"]]: e["subject_id"] for e in gallery if e["sample_id"] in gallery_index}
        gallery_sample_by_row = {gallery_index[e["sample_id"]]: e["sample_id"] for e in gallery if e["sample_id"] in gallery_index}
        gallery_failed_subjects = {e["subject_id"] for e in gallery if e["sample_id"] in gallery_failures}
        columns = torch.tensor(gallery_positions, dtype=torch.long, device=similarity.device) if gallery_positions else None
        depth = min(rank_depth, len(gallery_positions)) if gallery_positions else 0
        subset_subjects = {gallery_subject_by_row[p] for p in gallery_positions}
        for entry in probes:
            kind = "MATED" if entry["role"] == "PROBE_MATED" else "NON_MATED"
            if kind == "MATED" and entry["subject_id"] not in subset_subjects and entry["subject_id"] not in gallery_failed_subjects:
                continue  # subject not enrolled in this gallery subset
            row_index = probe_index.get(entry["sample_id"])
            if row_index is None or depth == 0:
                records.append(SearchRecord(entry["sample_id"], entry["subject_id"], kind, True, None, None, None, entry["subject_id"] in gallery_failed_subjects))
                continue
            scores = similarity[row_index].index_select(0, columns)
            values, order = torch.sort(scores, descending=True, stable=True)
            values, order = values[:depth].cpu().numpy(), order[:depth].cpu().numpy()
            true_rank = None
            true_score = None
            for rank, (value, position) in enumerate(zip(values, order), start=1):
                gallery_row = gallery_positions[int(position)]
                is_mate = gallery_subject_by_row[gallery_row] == entry["subject_id"]
                if is_mate and true_rank is None:
                    true_rank, true_score = rank, float(value)
                rows.append({"probe_sample_id": entry["sample_id"], "probe_subject_id": entry["subject_id"], "probe_kind": kind, "rank": rank, "gallery_sample_id": gallery_sample_by_row[gallery_row], "gallery_subject_id": gallery_subject_by_row[gallery_row], "score": float(value), "is_true_mate": bool(is_mate)})
            records.append(SearchRecord(entry["sample_id"], entry["subject_id"], kind, False, float(values[0]) if len(values) else None, true_rank, true_score, entry["subject_id"] in gallery_failed_subjects))
        return records, rows

    def run(self) -> SearchResult:
        started = _now()
        clock = time.perf_counter()
        gallery = [e for e in self.entries if e["role"] == "GALLERY"]
        probes = [e for e in self.entries if e["role"] != "GALLERY"]
        rank_depth = int(self.protocol["rank_depth"])
        self.progress = {"phase": "preprocess", "detail": "gallery"}
        gallery_crops, gallery_index, gallery_failures, gallery_seconds = self._templates([e["sample_id"] for e in gallery])
        self.progress = {"phase": "preprocess", "detail": "probes"}
        probe_crops, probe_index, probe_failures, probe_seconds = self._templates([e["sample_id"] for e in probes])
        self.progress = {"phase": "embed", "detail": self.matcher_id.value}
        matcher = FrozenMatcher(self.matcher_id, self.registry, self.upstream)
        embed_clock = time.perf_counter()
        gallery_embeddings = matcher.embed(gallery_crops) if gallery_crops.shape[0] else np.zeros((0, 512), dtype=np.float32)
        probe_embeddings = matcher.embed(probe_crops) if probe_crops.shape[0] else np.zeros((0, 512), dtype=np.float32)
        embed_seconds = time.perf_counter() - embed_clock
        device = torch.device(matcher.device)
        gallery_tensor = torch.from_numpy(gallery_embeddings).to(device)
        probe_tensor = torch.from_numpy(probe_embeddings).to(device)
        self.progress = {"phase": "search", "detail": f"{probe_tensor.shape[0]}×{gallery_tensor.shape[0]} on {device}"}
        similarity = probe_tensor @ gallery_tensor.T if gallery_tensor.shape[0] and probe_tensor.shape[0] else torch.zeros((probe_tensor.shape[0], gallery_tensor.shape[0]), device=device)
        all_positions = list(range(gallery_tensor.shape[0]))
        records, rows = self._records(similarity, probes, probe_index, probe_failures, gallery, gallery_index, gallery_failures, all_positions, rank_depth)
        metrics = compute_identification_metrics(records, gallery_size=len(gallery), gallery_template_failures=len(gallery_failures), rank_depth=rank_depth)
        # Gallery-size effects: deterministic subsets of the enrolled gallery templates.
        generator = np.random.default_rng(GALLERY_EFFECT_SEED)
        effects = []
        for size in [s for s in GALLERY_EFFECT_SIZES if s < len(all_positions)] + [len(all_positions)]:
            positions = sorted(int(p) for p in generator.choice(np.array(all_positions), size=size, replace=False)) if size < len(all_positions) else all_positions
            sub_records, _ = self._records(similarity, probes, probe_index, probe_failures, gallery, gallery_index, gallery_failures, positions, rank_depth)
            sub_metrics = compute_identification_metrics(sub_records, gallery_size=size, gallery_template_failures=0, rank_depth=rank_depth)
            closed = sub_metrics["closed_set"]["EXCLUDE_FAILED_TRIALS"]
            open_set = sub_metrics["open_set"]["EXCLUDE_FAILED_TRIALS"]
            point = next((p for p in open_set["operating_points"] if p["target_fpir"] == 0.01), None)
            effects.append({"gallery_size": size, "mated_searches": closed["mated_searches_included"], "rank_1": closed["rank_k"].get("1", {}).get("rate"), "rank_5": closed["rank_k"].get("5", {}).get("rate"), "fnir_at_fpir_0_01": point.get("fnir") if point else None, "threshold_at_fpir_0_01": point.get("threshold") if point else None, "subset_rule": "deterministic uniform subset of enrolled templates (seed 20260818)" if size < len(all_positions) else "full gallery"})
        metrics["gallery_effects"] = {"gallery_sizes": effects, "seed": GALLERY_EFFECT_SEED, "note": "probes whose subject is not in the subset are excluded from mated searches; non-mated searches are kept"}
        # Probe-quality effect when a quality job is given: rank-1 by probe UnifiedQualityScore bin.
        if self.quality_job_id:
            job = self.store.job(self.quality_job_id)
            if job and job.get("parquet_path"):
                quality = {r["sample_id"]: r["UnifiedQualityScore.scalar"] for r in pq.read_table(job["parquet_path"], columns=["sample_id", "outcome", "UnifiedQualityScore.scalar", "UnifiedQualityScore.failed"]).to_pylist() if r["outcome"] == "MEASURED" and not r["UnifiedQualityScore.failed"] and r["UnifiedQualityScore.scalar"] is not None}
                bins = []
                for low in range(0, 100, 20):
                    high = low + 20
                    members = [r for r in records if r.kind == "MATED" and not r.failed and r.probe_sample_id in quality and low <= quality[r.probe_sample_id] < (high if high < 100 else 101)]
                    hits = sum(1 for r in members if r.true_mate_rank == 1)
                    bins.append({"quality_low": low, "quality_high": high, "mated_searches": len(members), "rank_1": (hits / len(members)) if members else None, "supported": len(members) >= 30})
                metrics["probe_quality_effect"] = {"quality_job_id": self.quality_job_id, "measure": "UnifiedQualityScore", "bins": bins}
        # Persist artifacts (gallery and probe manifests first: sample id, subject, template outcome).
        gallery_manifest = self.directory / "gallery_manifest.tsv"
        gallery_manifest.write_text("sample_id\tsubject_id\ttemplate\n" + "".join(f"{e['sample_id']}\t{e['subject_id']}\t{gallery_failures.get(e['sample_id'], 'OK')}\n" for e in gallery), encoding="utf-8")
        probe_manifest = self.directory / "probe_manifest.tsv"
        probe_manifest.write_text("sample_id\tsubject_id\trole\ttemplate\n" + "".join(f"{e['sample_id']}\t{e['subject_id']}\t{e['role']}\t{probe_failures.get(e['sample_id'], 'OK')}\n" for e in probes), encoding="utf-8")
        self._manifests = (gallery_manifest, probe_manifest)
        candidates_path = self.directory / "candidates.parquet"
        pq.write_table(pa.Table.from_pylist(rows) if rows else pa.table({"probe_sample_id": pa.array([], pa.string())}), candidates_path, compression="zstd")
        metrics_path = self.directory / "identification_metrics.json"
        metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        seconds = time.perf_counter() - clock
        run, experiment = self._run(started, seconds, gallery_seconds + probe_seconds, embed_seconds, matcher, candidates_path, metrics_path, metrics, len(gallery), len(probes), len(gallery_failures), len(probe_failures), str(device))
        summary = {"search_id": self.search_id, "run_id": run.run_id, "protocol_id": self.protocol["protocol_id"], "matcher_id": self.matcher_id.value, "condition_id": self.condition.value, "device": str(device),
                   "gallery_size": len(gallery), "gallery_template_failures": len(gallery_failures), "mated_searches": metrics["counts"]["mated_searches"], "nonmated_searches": metrics["counts"]["nonmated_searches"], "probe_template_failures": len(probe_failures),
                   "rank_1": metrics["closed_set"]["EXCLUDE_FAILED_TRIALS"]["rank_k"].get("1", {}).get("rate"), "fnir_at_fpir_0_01": next((p.get("fnir") for p in metrics["open_set"]["EXCLUDE_FAILED_TRIALS"]["operating_points"] if p["target_fpir"] == 0.01), None), "seconds": round(seconds, 1)}
        result = SearchResult(self.search_id, run, experiment, metrics, candidates_path, metrics_path, self.directory, summary)
        self.store.record_identification_search(result, self.protocol)
        self.progress = {"phase": "completed", "detail": ""}
        return result

    def _run(self, started: datetime, seconds: float, preprocess_seconds: float, embed_seconds: float, matcher: FrozenMatcher, candidates_path: Path, metrics_path: Path, metrics: dict[str, Any], gallery_count: int, probe_count: int, gallery_failures: int, probe_failures: int, device: str) -> tuple[CompletedRun, Experiment]:
        binding = matcher.binding
        checkpoint_identity = next(item.identity for item in self.registry.resource(self.matcher_id.value).artifacts if item.role == "MODEL")
        detector_domain = DomainArtifactIdentity(path=self.detector_identity.path, sha256=self.detector_identity.sha256, size_bytes=self.detector_identity.size_bytes, mtime_ns=self.detector_identity.mtime_ns)
        checkpoint_domain = DomainArtifactIdentity(path=checkpoint_identity.path, sha256=checkpoint_identity.sha256, size_bytes=checkpoint_identity.size_bytes, mtime_ns=checkpoint_identity.mtime_ns)
        condition_definition = CONDITION_DEFINITIONS[self.condition]
        transform_id = TRANSFORM_IDS[self.matcher_id]
        transform_sha = sha256_bytes(f"{transform_id}:{COLOR_ORDER[self.matcher_id]}:CHW float32 (x/255-0.5)/0.5 in [-1,1]; L2-normalized 512-d; cosine similarity".encode("utf-8"))
        configuration = CanonicalJson.from_value({
            "search_id": self.search_id, "version": SEARCH_VERSION, "protocol_id": self.protocol["protocol_id"], "protocol_manifest_sha256": self.protocol["manifest_sha256"], "mode": self.protocol["mode"], "rank_depth": self.protocol["rank_depth"],
            "gallery_count": gallery_count, "probe_count": probe_count, "matcher": {"resource_id": self.matcher_id.value, "transform_id": transform_id, "checkpoint_sha256": binding.checkpoint_sha256, "architecture_source_sha256": list(binding.architecture_source_sha256), "parameter_count": binding.parameter_count, "device": binding.device, "torch_version": binding.torch_version},
            "preprocessing": {"condition_id": self.condition.value, **condition_definition, "detector_sha256": self.detector_identity.sha256, "detector_threshold": 0.5, "nms_threshold": 0.4, "alignment_template_sha256": ARCFACE_TEMPLATE_SHA256},
            "score": {"definition": "cosine similarity between L2-normalized 512-d embeddings; probe × gallery on the matcher device", "direction": "HIGHER_IS_MORE_SIMILAR", "similarity_device": device},
            "failure_policy": "every search retained; failed probe or gallery templates are explicit and reported under EXCLUDE_FAILED_TRIALS and FAILURES_AS_MISS", "gallery_effects_seed": GALLERY_EFFECT_SEED, "purpose_scope": self.purpose_scope, "quality_job_id": self.quality_job_id,
        })
        experiment_id = content_id("experiment", self.project.project_id, self.dataset.dataset_id, self.protocol["protocol_id"], self.matcher_id.value, self.condition.value, "identification")
        run_id = content_id("run", experiment_id, started.isoformat(), sha256_file(candidates_path), binding.checkpoint_sha256, self.detector_identity.sha256)
        closed = metrics["closed_set"]["EXCLUDE_FAILED_TRIALS"]
        open_set = metrics["open_set"]["EXCLUDE_FAILED_TRIALS"]
        observations = [MetricObservation(metric_id="searches.mated", value=metrics["counts"]["mated_searches"], unit="count"), MetricObservation(metric_id="searches.nonmated", value=metrics["counts"]["nonmated_searches"], unit="count"),
                        MetricObservation(metric_id="gallery.templates", value=metrics["counts"]["gallery_templates"], unit="count"), MetricObservation(metric_id="templates.failed_probes", value=probe_failures, unit="count"),
                        MetricObservation(metric_id="timing.preprocess_seconds", value=round(preprocess_seconds, 3), unit="observation"), MetricObservation(metric_id="timing.embed_seconds", value=round(embed_seconds, 3), unit="observation")]
        for k, block in closed["rank_k"].items():
            if block["rate"] is not None:
                observations.append(MetricObservation(metric_id=f"biometric.identification.rank-k@1.0.0[EXCLUDE_FAILED_TRIALS]@k={k}", value=float(block["rate"]), unit="rate"))
        for point in open_set["operating_points"]:
            if point.get("threshold") is not None:
                observations.append(MetricObservation(metric_id=f"biometric.identification.fnir@1.0.0[EXCLUDE_FAILED_TRIALS]@fpir<={point['target_fpir']}", value=float(point["fnir"]), unit="rate"))
        artifacts = []
        for path, kind, media, backend in ((candidates_path, ArtifactKind.TABLE, "application/vnd.apache.parquet", StorageBackend.PARQUET), (metrics_path, ArtifactKind.METRICS, "application/json", StorageBackend.FILESYSTEM)):
            digest = sha256_file(path)
            artifacts.append(Artifact(artifact_id=content_id("artifact", run_id, digest, path.name), run_id=run_id, kind=kind, storage_backend=backend, uri=str(path), sha256=digest, size_bytes=path.stat().st_size, media_type=media, classification=DataClassification.SENSITIVE_BIOMETRIC_DERIVED, created_at=_now()))
        gallery_manifest, probe_manifest = self._manifests
        gallery_identity = DomainArtifactIdentity(path=str(gallery_manifest), sha256=sha256_file(gallery_manifest), size_bytes=gallery_manifest.stat().st_size, mtime_ns=gallery_manifest.stat().st_mtime_ns)
        probe_identity = DomainArtifactIdentity(path=str(probe_manifest), sha256=sha256_file(probe_manifest), size_bytes=probe_manifest.stat().st_size, mtime_ns=probe_manifest.stat().st_mtime_ns)
        run = CompletedRun(
            run_id=run_id, project_id=self.project.project_id, experiment_id=experiment_id, user_id=self.actor,
            dataset_manifest=self.dataset.manifest, dataset_hash=self.dataset.manifest.sha256,
            subject_protocol=ComponentBinding(state=ComponentState.CONFIGURED, component_id="lfw-directory-identity", version="1.0.0"),
            pair_search_protocol=ComponentBinding(state=ComponentState.CONFIGURED, component_id=self.protocol["protocol_id"], version="LFW_IDENTIFICATION_OPEN_SET", configuration_sha256=self.protocol["manifest_sha256"]),
            quality_engine=None, quality_engine_configuration=None, model_hashes=(checkpoint_domain, detector_domain),
            matcher=ComponentBinding(state=ComponentState.CONFIGURED, component_id=self.matcher_id.value, version=binding.checkpoint_sha256[:16], configuration_sha256=transform_sha, artifact_hashes=(binding.checkpoint_sha256, *binding.architecture_source_sha256)),
            matcher_preprocessing=ComponentBinding(state=ComponentState.CONFIGURED, component_id=self.condition.value, version="1.0.0", configuration_sha256=sha256_bytes(json.dumps(condition_definition, sort_keys=True).encode("utf-8")), artifact_hashes=(self.detector_identity.sha256, ARCFACE_TEMPLATE_SHA256)),
            operating_thresholds=ComponentBinding(state=ComponentState.CONFIGURED, component_id="registry-defined-fpir-operating-points", version="1.0.0", configuration_sha256=sha256_bytes(json.dumps({"targets": [0.1, 0.01, 0.001], "rank_values": [1, 5, 10, 20, 100]}, sort_keys=True).encode("utf-8"))),
            statistical_methods=ComponentBinding(state=ComponentState.CONFIGURED, component_id="qualsight.metrics.identification", version="1.0.0", configuration_sha256=sha256_bytes(b"clopper-pearson binomial CI; deterministic gallery subsets")),
            confidence_interval_method=ComponentBinding(state=ComponentState.CONFIGURED, component_id="CLOPPER_PEARSON_EXACT_BINOMIAL_95", version="1.0.0"),
            randomness=ComponentBinding(state=ComponentState.CONFIGURED, component_id="numpy.random.default_rng", version="PCG64", configuration_sha256=sha256_bytes(str(GALLERY_EFFECT_SEED).encode("utf-8"))), random_seeds=(GALLERY_EFFECT_SEED,),
            application_version=application_version(self.repository_root / "backend" / "pyproject.toml"), repository=repository_identity(self.repository_root), environment=environment_identity(self.repository_root / "uv.lock", {}),
            hardware=hardware_identity(f"matcher and similarity on {device}; detector on CPUExecutionProvider"),
            start_time=started, end_time=_now(), runtime_seconds=round(seconds, 3), initialization_ms=0, assessment_ms=int(embed_seconds * 1000),
            configuration=configuration, metrics=tuple(observations), artifacts=tuple(artifacts),
            logs=(LogRecord(stream="stdout", sha256=sha256_bytes(b""), text=""), LogRecord(stream="stderr", sha256=sha256_bytes(b""), text="")),
            warnings=tuple(w for w in (f"{gallery_failures} gallery templates failed" if gallery_failures else None, f"{probe_failures} probe templates failed" if probe_failures else None) if w),
            provenance_limits=tuple(self.registry.raw_resource(self.matcher_id.value)["use_restrictions"]) + tuple(self.protocol["limitations"]) + ("LFW identification results are QualSight-executed on the funneled release with SCRFD alignment; they are not published-figure reproductions.",),
            exit_code=0, status=RunStatus.COMPLETED, scientific_support=ScientificSupportState.NOT_ASSESSED,
        )
        experiment = Experiment(
            experiment_id=experiment_id, project_id=self.project.project_id, dataset_id=self.dataset.dataset_id, name=f"identification: {self.matcher_id.value} × {self.condition.value} over {self.protocol['protocol_id'][-12:]}", created_by=self.actor, created_at=started,
            protocol=IdentificationProtocol(kind=ProtocolKind.IDENTIFICATION, mode=IdentificationMode.OPEN_SET, protocol_id=self.protocol["protocol_id"], gallery_manifest=gallery_identity, probe_manifest=probe_identity, rank_depth=int(self.protocol["rank_depth"]), identity_schema=ComponentBinding(state=ComponentState.CONFIGURED, component_id="lfw-directory-identity", version="1.0.0")),
            configuration=configuration,
        )
        return run, experiment
