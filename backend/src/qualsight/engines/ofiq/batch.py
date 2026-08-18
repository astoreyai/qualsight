"""Batch OFIQ quality execution over an imported dataset (PRD §§13–14, 32; QS-B12).

A batch job selects samples of a recorded dataset, re-verifies each sample's
bytes against its recorded SHA-256, writes one or more OFIQSampleApp list files
in a job directory, launches one registered engine process per chunk through
the registry-bound CLI adapter, tracks progress from the engine's own output
files, supports cancellation with process-group termination, records host
resource use, and finally persists every per-sample result — measurement or
explicit failure — to Parquet together with one immutable batch Run.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import getpass
import hashlib
import json
import math
from pathlib import Path
import resource as host_resource
import threading
import time
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from ...domain.entities import (
    Artifact,
    ArtifactKind,
    AuditEvent,
    AuditEventType,
    DataClassification,
    Dataset,
    Experiment,
    Project,
    ProtocolKind,
    Sample,
    SingleSampleQualityProtocol,
    StorageBackend,
)
from ...domain.primitives import (
    ArtifactIdentity as DomainArtifactIdentity,
    CanonicalJson,
    ComponentBinding,
    ComponentState,
    ResourceBinding,
)
from ...domain.run import CompletedRun, LogRecord, MetricObservation, RunStatus, ScientificSupportState
from ...execution.cli_runtime import RunningExecution
from ...execution.contracts import ArtifactIdentity, LifecycleState
from ...provenance import (
    application_version,
    artifact_identity,
    environment_identity,
    hardware_identity,
    repository_identity,
    sha256_bytes,
    sha256_file,
)
from .adapter import OFIQEngineError, OFIQQualityEngine, _not_applicable, _to_domain, content_id
from .output import ENGINE_FAILURE_SCALAR, VERSION_RE, INITIALIZATION_RE

MEASURE_NAMES: tuple[str, ...] = (
    "UnifiedQualityScore",
    "BackgroundUniformity",
    "IlluminationUniformity",
    "LuminanceMean",
    "LuminanceVariance",
    "UnderExposurePrevention",
    "OverExposurePrevention",
    "DynamicRange",
    "Sharpness",
    "CompressionArtifacts",
    "NaturalColour",
    "SingleFacePresent",
    "EyesOpen",
    "MouthClosed",
    "EyesVisible",
    "MouthOcclusionPrevention",
    "FaceOcclusionPrevention",
    "InterEyeDistance",
    "HeadSize",
    "LeftwardCropOfTheFaceImage",
    "RightwardCropOfTheFaceImage",
    "MarginAboveOfTheFaceImage",
    "MarginBelowOfTheFaceImage",
    "HeadPoseYaw",
    "HeadPosePitch",
    "HeadPoseRoll",
    "ExpressionNeutrality",
    "NoHeadCoverings",
)


class BatchJobError(RuntimeError):
    """Raised when a batch job cannot be constructed or its evidence is inconsistent."""


@dataclass
class ChunkState:
    index: int
    list_path: Path
    output_path: Path
    sample_count: int
    running: RunningExecution | None = None
    record: Any = None
    completed_rows: int = 0


@dataclass
class BatchProgress:
    job_id: str
    state: str
    total: int
    assessed: int
    failed: int
    integrity_failures: int
    not_assessed: int
    chunks: int
    started_at: str | None
    ended_at: str | None
    cancellation_requested: bool
    run_id: str | None
    error: str | None
    resource_use: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BatchResultRow:
    sample_id: str
    source_path: str
    sha256: str
    subject_id: str | None
    chunk: int
    outcome: str  # MEASURED | ENGINE_ROW_FAILURE | INTEGRITY_FAILURE | NOT_ASSESSED
    failure_reason: str | None
    assessment_ms: int | None
    measures: dict[str, tuple[float | None, int | None, bool]]


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _parse_csv_rows(output_path: Path) -> list[dict[str, str]]:
    if not output_path.is_file():
        return []
    text = output_path.read_text(encoding="utf-8", errors="strict")
    lines = [line for line in text.splitlines() if line]
    if not lines:
        return []
    reader = csv.reader(lines, delimiter=";", strict=True)
    header = next(reader)
    while header and header[-1] == "":
        header.pop()
    rows: list[dict[str, str]] = []
    for values in reader:
        while values and values[-1] == "":
            values.pop()
        rows.append({"__header__": json.dumps(header), "__values__": json.dumps(values)})
    return rows


def _row_from_engine(header: list[str], values: list[str], filename: str) -> tuple[str, str | None, int | None, dict[str, tuple[float | None, int | None, bool]]]:
    """Interpret one engine CSV row (same rules as output.parse_ofiq_stdout)."""

    if header[0] != "Filename" or header[-1] != "assessment_time_in_ms":
        raise BatchJobError("engine CSV header is not the OFIQSampleApp layout")
    natives = [name[: -len(".native")] for name in header[1:-1] if name.endswith(".native")]
    scalars = [name[: -len(".scalar")] for name in header[1:-1] if name.endswith(".scalar")]
    if natives != scalars:
        raise BatchJobError("engine CSV native/scalar columns do not correspond")
    assessment_ms = int(values[-1]) if values and values[-1].strip() else None
    cells = values[1:-1]
    if not natives or len(cells) != 2 * len(natives) or all(cell == "" for cell in cells):
        return "ENGINE_ROW_FAILURE", None, assessment_ms, {}
    measures: dict[str, tuple[float | None, int | None, bool]] = {}
    for name, native_text, scalar_text in zip(natives, cells[: len(natives)], cells[len(natives) :], strict=True):
        native: float | None = None
        scalar: int | None = None
        failed = False
        try:
            native_value = float(native_text)
            native = native_value if math.isfinite(native_value) else None
        except ValueError:
            native = None
        if native is None:
            failed = True
        try:
            scalar_value = float(scalar_text)
        except ValueError:
            scalar_value = float("nan")
        if math.isfinite(scalar_value) and scalar_value.is_integer() and int(scalar_value) != ENGINE_FAILURE_SCALAR and 0 <= int(scalar_value) <= 100:
            scalar = int(scalar_value)
        else:
            failed = True
        measures[name] = (native, scalar, failed)
    return "MEASURED", None, assessment_ms, measures


class OFIQBatchJob:
    """One batch execution; ``run()`` blocks until the engine processes finish."""

    def __init__(
        self,
        *,
        engine: OFIQQualityEngine,
        store: Any,
        project: Project,
        dataset: Dataset,
        samples: list[Sample],
        purpose_scope: str,
        job_root: Path,
        chunks: int = 1,
        selection_rule: str,
        actor_id: str | None = None,
        execution: str = "host",
    ) -> None:
        if not samples:
            raise BatchJobError("a batch job needs at least one sample")
        if chunks < 1 or chunks > 32:
            raise BatchJobError("chunks must be between 1 and 32")
        self.engine = engine
        self.store = store
        self.project = project
        self.dataset = dataset
        self.samples = samples
        self.purpose_scope = purpose_scope
        self.chunks_requested = min(chunks, len(samples))
        self.selection_rule = selection_rule
        self.actor = actor_id or f"local-user:{getpass.getuser()}"
        self.created_at = _now()
        selection_sha = sha256_bytes("\n".join(sorted(sample.sample_id for sample in samples)).encode("utf-8"))
        self.job_id = content_id(
            "job", project.project_id, dataset.dataset_id, selection_sha, engine.runtime.resource_id, self.created_at.isoformat()
        )
        self.job_dir = job_root / self.job_id.replace(":", "_")
        self.job_dir.mkdir(parents=True, exist_ok=False)
        self._lock = threading.Lock()
        self._cancel_requested = False
        self.state = "QUEUED"
        self.started_at: datetime | None = None
        self.ended_at: datetime | None = None
        self.error: str | None = None
        self.run_id: str | None = None
        self.chunk_states: list[ChunkState] = []
        self.integrity_failures: dict[str, str] = {}
        self.resource_use: dict[str, float] = {}
        self.results: list[BatchResultRow] = []
        self.parquet_path: Path | None = None
        self.engine_version: str | None = None
        self.initialization_ms: list[int] = []
        self._input_resource, _ = self._input_resource_for(samples[0].source_path)
        if execution not in ("host", "container"):
            raise BatchJobError("execution must be host or container")
        self.execution = execution
        self.container_adapter = None
        if execution == "container":
            from ...execution.container_runtime import ContainerCLIAdapter, ContainerRuntimeError, build_runtime_image

            try:
                image = build_runtime_image(runtime_resource=engine.runtime, work_dir=job_root / ".images")
            except ContainerRuntimeError as error:
                raise BatchJobError(f"container execution unavailable: {error}") from error
            mounts = {Path(engine.configuration.path).parent, Path(engine.runtime.artifact("BINARY").identity.path).parent}
            for resource in engine.input_resources:
                for artifact in resource.artifacts:
                    if artifact.role == "DATA" and artifact.identity.path:
                        mounts.add(Path(artifact.identity.path).parent)
                    if artifact.role == "MANIFEST" and artifact.identity.path and artifact.identity.path.endswith("lfw-funneled-images-v1.jsonl"):
                        mounts.add(Path("/mnt/projects/datasets/lfw/lfw_funneled"))
            derived_root = job_root.parent / "derived"
            if derived_root.exists():
                mounts.add(derived_root)
            self.container_adapter = ContainerCLIAdapter(inner=engine.adapters[self._input_resource.resource_id], image=image, read_only_mounts=tuple(sorted(mounts)))

    def _input_resource_for(self, path: str):
        # Derived samples (QS-X61): admitted only when the store records the exact path and hash with a
        # transformation chain rooting at a bound registered input resource.
        derived = self.store.derived_sample_by_path(path) if hasattr(self.store, "derived_sample_by_path") else None
        if derived is not None:
            root_id = derived["chain"][0].get("root_resource_id")
            for resource in self.engine.input_resources:
                if resource.resource_id == root_id:
                    if sha256_file(Path(path)) != derived["sha256"]:
                        raise BatchJobError(f"derived sample {path} drifted from its recorded hash")
                    return resource, None
            raise BatchJobError(f"derived sample {path} roots at {root_id}, which is not a bound input resource")
        for resource in self.engine.input_resources:
            for artifact in resource.artifacts:
                if artifact.role == "MANIFEST" and artifact.identity.path and artifact.identity.path.endswith("lfw-funneled-images-v1.jsonl"):
                    if path.startswith("/mnt/projects/datasets/lfw/lfw_funneled/"):
                        return resource, artifact
        for resource in self.engine.input_resources:
            for artifact in resource.artifacts:
                if artifact.role == "DATA" and artifact.identity.path == path:
                    return resource, artifact
        raise BatchJobError(f"no bound input resource governs sample path {path}")

    # -- preparation -----------------------------------------------------------

    def _prepare_lists(self) -> None:
        verified: list[Sample] = []
        for sample in self.samples:
            path = Path(sample.source_path)
            try:
                digest = sha256_file(path)
            except OSError as error:
                self.integrity_failures[sample.sample_id] = f"unreadable: {error}"
                continue
            if digest != sample.sha256:
                self.integrity_failures[sample.sample_id] = f"sha256 drift: {digest}"
                continue
            verified.append(sample)
        if not verified:
            raise BatchJobError("no sample passed integrity verification")
        chunk_count = min(self.chunks_requested, len(verified))
        size = math.ceil(len(verified) / chunk_count)
        for index in range(chunk_count):
            part = verified[index * size : (index + 1) * size]
            if not part:
                continue
            list_path = self.job_dir / f"chunk-{index:02d}-inputs.txt"
            list_path.write_text("\n".join(sample.source_path for sample in part) + "\n", encoding="utf-8")
            self.chunk_states.append(
                ChunkState(
                    index=index,
                    list_path=list_path,
                    output_path=self.job_dir / f"chunk-{index:02d}-results.csv",
                    sample_count=len(part),
                )
            )

    # -- execution -------------------------------------------------------------

    def _launch(self, chunk: ChunkState) -> None:
        adapter = self.container_adapter or self.engine.adapters[self._input_resource.resource_id]
        list_identity = ArtifactIdentity.from_file(chunk.list_path)
        request = adapter.create_request(
            arguments=("-c", self.engine.configuration.path, "-i", str(chunk.list_path), "-o", str(chunk.output_path)),
            input_artifacts=(list_identity,),
            purpose_scope=self.purpose_scope,
        )
        chunk.running = adapter.start(adapter.prepare(request))

    def progress(self) -> BatchProgress:
        with self._lock:
            assessed = failed = 0
            for chunk in self.chunk_states:
                if chunk.output_path.is_file():
                    try:
                        lines = chunk.output_path.read_text(encoding="utf-8", errors="ignore").count("\n")
                    except OSError:
                        lines = 0
                    chunk.completed_rows = max(0, lines - 1)
            if self.results:
                assessed = sum(1 for row in self.results if row.outcome == "MEASURED")
                failed = sum(1 for row in self.results if row.outcome in {"ENGINE_ROW_FAILURE"})
                not_assessed = sum(1 for row in self.results if row.outcome == "NOT_ASSESSED")
            else:
                assessed = sum(chunk.completed_rows for chunk in self.chunk_states)
                not_assessed = max(0, len(self.samples) - len(self.integrity_failures) - assessed)
            return BatchProgress(
                job_id=self.job_id,
                state=self.state,
                total=len(self.samples),
                assessed=assessed,
                failed=failed,
                integrity_failures=len(self.integrity_failures),
                not_assessed=not_assessed,
                chunks=len(self.chunk_states),
                started_at=self.started_at.isoformat() if self.started_at else None,
                ended_at=self.ended_at.isoformat() if self.ended_at else None,
                cancellation_requested=self._cancel_requested,
                run_id=self.run_id,
                error=self.error,
                resource_use=dict(self.resource_use),
            )

    def cancel(self) -> bool:
        with self._lock:
            self._cancel_requested = True
            running = [chunk.running for chunk in self.chunk_states if chunk.running is not None]
        cancelled = False
        for handle in running:
            cancelled = handle.cancel(grace_seconds=5.0) or cancelled
        return cancelled

    def run(self) -> BatchProgress:
        with self._lock:
            self.state = "RUNNING"
            self.started_at = _now()
        usage_before = host_resource.getrusage(host_resource.RUSAGE_CHILDREN)
        wall_start = time.perf_counter()
        try:
            self._prepare_lists()
            for chunk in self.chunk_states:
                if self._cancel_requested:
                    break
                self._launch(chunk)
            for chunk in self.chunk_states:
                if chunk.running is not None:
                    chunk.record = chunk.running.collect()
            usage_after = host_resource.getrusage(host_resource.RUSAGE_CHILDREN)
            self.resource_use = {
                "wall_seconds": round(time.perf_counter() - wall_start, 3),
                "children_user_cpu_seconds": round(usage_after.ru_utime - usage_before.ru_utime, 3),
                "children_system_cpu_seconds": round(usage_after.ru_stime - usage_before.ru_stime, 3),
                "children_max_rss_kib": float(usage_after.ru_maxrss),
                "processes": float(len([c for c in self.chunk_states if c.record is not None])),
            }
            self._collect_results()
            self._persist()
            with self._lock:
                self.state = "CANCELLED" if self._cancel_requested else "COMPLETED"
        except Exception as error:  # noqa: BLE001 - recorded, not hidden
            with self._lock:
                self.state = "FAILED"
                self.error = f"{type(error).__name__}: {error}"
            raise
        finally:
            with self._lock:
                self.ended_at = _now()
        return self.progress()

    # -- results ---------------------------------------------------------------

    def _collect_results(self) -> None:
        by_path = {sample.source_path: sample for sample in self.samples}
        seen: set[str] = set()
        results: list[BatchResultRow] = []
        for chunk in self.chunk_states:
            stderr_lines: list[str] = []
            if chunk.record is not None:
                stdout = chunk.record.stdout.content.decode("utf-8", errors="replace")
                stderr = chunk.record.stderr.content.decode("utf-8", errors="replace")
                stderr_lines = [line for line in stderr.splitlines() if line.startswith("[ERROR]")]
                for line in stdout.splitlines():
                    if (match := VERSION_RE.fullmatch(line)):
                        self.engine_version = match.group(1)
                    elif (match := INITIALIZATION_RE.fullmatch(line)):
                        self.initialization_ms.append(int(match.group(1)))
            for raw in _parse_csv_rows(chunk.output_path):
                header = json.loads(raw["__header__"])
                values = json.loads(raw["__values__"])
                if not values:
                    continue
                filename = values[0]
                sample = by_path.get(filename)
                if sample is None:
                    raise BatchJobError(f"engine reported a path that was not in the job: {filename}")
                outcome, _, assessment_ms, measures = _row_from_engine(header, values, filename)
                reason = None
                if outcome == "ENGINE_ROW_FAILURE":
                    reason = stderr_lines.pop(0) if stderr_lines else "engine reported empty measures for this image"
                results.append(
                    BatchResultRow(
                        sample_id=sample.sample_id,
                        source_path=sample.source_path,
                        sha256=sample.sha256,
                        subject_id=sample.subject_id.value,
                        chunk=chunk.index,
                        outcome=outcome,
                        failure_reason=reason,
                        assessment_ms=assessment_ms,
                        measures=measures,
                    )
                )
                seen.add(sample.sample_id)
        for sample in self.samples:
            if sample.sample_id in seen:
                continue
            if sample.sample_id in self.integrity_failures:
                results.append(
                    BatchResultRow(sample.sample_id, sample.source_path, sample.sha256, sample.subject_id.value, -1, "INTEGRITY_FAILURE", self.integrity_failures[sample.sample_id], None, {})
                )
            else:
                reason = "cancelled before the engine reached this image" if self._cancel_requested else "engine produced no row for this image"
                results.append(
                    BatchResultRow(sample.sample_id, sample.source_path, sample.sha256, sample.subject_id.value, -1, "NOT_ASSESSED", reason, None, {})
                )
        with self._lock:
            self.results = results
        if self.engine_version is not None and self.engine_version != self.engine.runtime.version:
            raise BatchJobError(f"engine reported version {self.engine_version}, registry records {self.engine.runtime.version}")

    def _persist(self) -> None:
        columns: dict[str, list[Any]] = {
            "sample_id": [], "source_path": [], "sha256": [], "subject_id": [], "chunk": [],
            "outcome": [], "failure_reason": [], "assessment_ms": [],
        }
        for name in MEASURE_NAMES:
            columns[f"{name}.native"] = []
            columns[f"{name}.scalar"] = []
            columns[f"{name}.failed"] = []
        for row in self.results:
            columns["sample_id"].append(row.sample_id)
            columns["source_path"].append(row.source_path)
            columns["sha256"].append(row.sha256)
            columns["subject_id"].append(row.subject_id)
            columns["chunk"].append(row.chunk)
            columns["outcome"].append(row.outcome)
            columns["failure_reason"].append(row.failure_reason)
            columns["assessment_ms"].append(row.assessment_ms)
            for name in MEASURE_NAMES:
                native, scalar, failed = row.measures.get(name, (None, None, row.outcome != "MEASURED"))
                columns[f"{name}.native"].append(native)
                columns[f"{name}.scalar"].append(scalar)
                columns[f"{name}.failed"].append(failed)
        schema_fields = [
            pa.field("sample_id", pa.string()), pa.field("source_path", pa.string()), pa.field("sha256", pa.string()),
            pa.field("subject_id", pa.string()), pa.field("chunk", pa.int32()), pa.field("outcome", pa.string()),
            pa.field("failure_reason", pa.string()), pa.field("assessment_ms", pa.int32()),
        ]
        for name in MEASURE_NAMES:
            schema_fields += [pa.field(f"{name}.native", pa.float64()), pa.field(f"{name}.scalar", pa.int16()), pa.field(f"{name}.failed", pa.bool_())]
        table = pa.table(columns, schema=pa.schema(schema_fields))
        parquet_path = self.job_dir / "quality_measurements.parquet"
        pq.write_table(table, parquet_path, compression="zstd")
        self.parquet_path = parquet_path

        measured = [row for row in self.results if row.outcome == "MEASURED"]
        failed_rows = [row for row in self.results if row.outcome == "ENGINE_ROW_FAILURE"]
        integrity = [row for row in self.results if row.outcome == "INTEGRITY_FAILURE"]
        not_assessed = [row for row in self.results if row.outcome == "NOT_ASSESSED"]
        uqs = [row.measures["UnifiedQualityScore"][1] for row in measured if row.measures.get("UnifiedQualityScore", (None, None, True))[1] is not None]
        measure_failures = sum(1 for row in measured for value in row.measures.values() if value[2])
        metrics = [
            MetricObservation(metric_id="batch.samples_selected", value=len(self.samples), unit="count"),
            MetricObservation(metric_id="batch.samples_measured", value=len(measured), unit="count"),
            MetricObservation(metric_id="batch.engine_row_failures", value=len(failed_rows), unit="count"),
            MetricObservation(metric_id="batch.integrity_failures", value=len(integrity), unit="count"),
            MetricObservation(metric_id="batch.not_assessed", value=len(not_assessed), unit="count"),
            MetricObservation(metric_id="batch.measure_level_failures", value=measure_failures, unit="count"),
            MetricObservation(metric_id="batch.quality_assessment_success_rate", value=round(len(measured) / len(self.samples), 6), unit="ratio"),
            MetricObservation(metric_id="batch.quality_assessment_failure_rate", value=round((len(failed_rows) + len(integrity) + len(not_assessed)) / len(self.samples), 6), unit="ratio"),
        ]
        if uqs:
            metrics += [
                MetricObservation(metric_id="batch.UnifiedQualityScore.scalar.mean", value=round(sum(uqs) / len(uqs), 6), unit="scalar"),
                MetricObservation(metric_id="batch.UnifiedQualityScore.scalar.min", value=min(uqs), unit="scalar"),
                MetricObservation(metric_id="batch.UnifiedQualityScore.scalar.max", value=max(uqs), unit="scalar"),
            ]
        for key, value in self.resource_use.items():
            metrics.append(MetricObservation(metric_id=f"batch.resource.{key}", value=value, unit="observation"))
        assessment_total = sum(row.assessment_ms or 0 for row in measured)

        runtime_record = self.engine.registry.raw_resource(self.engine.runtime.resource_id)
        input_record = self.engine.registry.raw_resource(self._input_resource.resource_id)
        binary = _to_domain(self.engine.executable)
        configuration = _to_domain(self.engine.configuration)
        libraries = tuple(_to_domain(item.identity) for item in self.engine.runtime.artifacts_with_role("RUNTIME_LIBRARY"))
        models = tuple(_to_domain(item.identity) for item in self.engine.runtime.artifacts_with_role("MODEL"))
        selection_manifest = CanonicalJson.from_value(
            {
                "execution_environment": self.container_adapter.environment_record() if self.container_adapter else {"kind": "HOST", "network": "not-restricted-by-adapter"},
                "dataset_id": self.dataset.dataset_id,
                "dataset_manifest_sha256": self.dataset.manifest.sha256,
                "selection_rule": self.selection_rule,
                "sample_count": len(self.samples),
                "sample_ids_sha256": sha256_bytes("\n".join(sorted(s.sample_id for s in self.samples)).encode("utf-8")),
                "chunks": [
                    {"index": c.index, "list_sha256": sha256_file(c.list_path), "sample_count": c.sample_count}
                    for c in self.chunk_states
                ],
            }
        )
        experiment_id = content_id("experiment", self.project.project_id, self.dataset.dataset_id, configuration.sha256, self.engine.runtime.resource_id, "batch", selection_manifest.sha256)
        run_id = content_id("run", experiment_id, self.started_at.isoformat(), sha256_file(parquet_path), *(model.sha256 for model in models))
        stdout_text = "\n".join(
            (chunk.record.stdout.content.decode("utf-8", errors="replace") if chunk.record else "") for chunk in self.chunk_states
        )
        stderr_text = "\n".join(
            (chunk.record.stderr.content.decode("utf-8", errors="replace") if chunk.record else "") for chunk in self.chunk_states
        )
        artifacts = [
            Artifact(
                artifact_id=content_id("artifact", run_id, sha256_file(parquet_path)),
                run_id=run_id,
                kind=ArtifactKind.METRICS,
                storage_backend=StorageBackend.PARQUET,
                uri=str(parquet_path),
                sha256=sha256_file(parquet_path),
                size_bytes=parquet_path.stat().st_size,
                media_type="application/vnd.apache.parquet",
                classification=DataClassification(input_record["data_classification"]),
                created_at=_now(),
            )
        ]
        for chunk in self.chunk_states:
            for path, kind, media in ((chunk.list_path, ArtifactKind.TABLE, "text/plain"), (chunk.output_path, ArtifactKind.TABLE, "text/csv")):
                if path.is_file():
                    digest = sha256_file(path)
                    artifacts.append(
                        Artifact(
                            artifact_id=content_id("artifact", run_id, digest, path.name),
                            run_id=run_id,
                            kind=kind,
                            storage_backend=StorageBackend.FILESYSTEM,
                            uri=str(path),
                            sha256=digest,
                            size_bytes=path.stat().st_size,
                            media_type=media,
                            classification=DataClassification(input_record["data_classification"]),
                            created_at=_now(),
                        )
                    )
        exit_codes = [chunk.record.exit_code for chunk in self.chunk_states if chunk.record is not None]
        run_status = RunStatus.COMPLETED if not self._cancel_requested and all(code == 0 for code in exit_codes) else None
        experiment = Experiment(
            experiment_id=experiment_id,
            project_id=self.project.project_id,
            dataset_id=self.dataset.dataset_id,
            name=f"OFIQ batch quality execution ({len(self.samples)} samples)",
            created_by=self.actor,
            created_at=self.started_at,
            protocol=SingleSampleQualityProtocol(kind=ProtocolKind.SINGLE_SAMPLE_QUALITY, sample_ids=tuple(s.sample_id for s in self.samples)),
            configuration=selection_manifest,
        )
        self.experiment = experiment
        if run_status is None:
            # A cancelled or nonzero-exit batch keeps its Parquet evidence and
            # audit trail but is not recorded as a COMPLETED scientific Run.
            self.completed_run = None
            self.store.record_batch_job(self, experiment, None, artifacts)
            return
        run = CompletedRun(
            run_id=run_id,
            project_id=self.project.project_id,
            experiment_id=experiment_id,
            user_id=self.actor,
            dataset_manifest=selection_manifest,
            dataset_hash=selection_manifest.sha256,
            subject_protocol=ComponentBinding(state=ComponentState.CONFIGURED, component_id="lfw-directory-identity", version="1.0.0"),
            pair_search_protocol=_not_applicable("Batch quality execution scores samples individually and performs no comparison."),
            quality_engine=ResourceBinding(
                resource_id=self.engine.runtime.resource_id,
                version=self.engine.runtime.version or runtime_record["version"],
                registry_state=runtime_record["state"],
                artifacts=(binary, *libraries, *models),
            ),
            quality_engine_configuration=configuration,
            model_hashes=models,
            matcher=_not_applicable("Batch quality execution invokes no matcher."),
            matcher_preprocessing=_not_applicable("Batch quality execution has no matcher preprocessing stage."),
            operating_thresholds=_not_applicable("Batch quality execution applies no decision threshold."),
            statistical_methods=_not_applicable("Batch quality execution reports counts and descriptive aggregates only."),
            confidence_interval_method=_not_applicable("Batch quality execution computes no confidence interval."),
            randomness=_not_applicable("The engine command and registered configuration contain no random-seed setting."),
            random_seeds=(),
            application_version=application_version(self.engine.repository_root / "backend" / "pyproject.toml"),
            repository=repository_identity(self.engine.repository_root),
            environment=environment_identity(self.engine.repository_root / "uv.lock", {}),
            hardware=hardware_identity("OFIQSampleApp output does not identify an execution device."),
            start_time=self.started_at,
            end_time=_now(),
            runtime_seconds=self.resource_use.get("wall_seconds", 0.0),
            initialization_ms=max(self.initialization_ms) if self.initialization_ms else 0,
            assessment_ms=assessment_total,
            configuration=selection_manifest,
            metrics=tuple(metrics),
            artifacts=tuple(artifacts),
            logs=(
                LogRecord(stream="stdout", sha256=sha256_bytes(stdout_text.encode("utf-8")), text=stdout_text),
                LogRecord(stream="stderr", sha256=sha256_bytes(stderr_text.encode("utf-8")), text=stderr_text),
            ),
            warnings=tuple(f"{row.source_path}: {row.failure_reason}" for row in self.results if row.outcome != "MEASURED")[:500],
            provenance_limits=tuple(runtime_record["use_restrictions"]) + tuple(input_record["use_restrictions"]),
            exit_code=0,
            status=RunStatus.COMPLETED,
            scientific_support=ScientificSupportState.NOT_ASSESSED,
        )
        self.completed_run = run
        self.run_id = run_id
        self.store.record_batch_job(self, experiment, run, artifacts)
