"""QualSight local control plane.

Every quality result served here comes from a real execution of the registered
engine on registered bytes; every Run is persisted immutably before it is
returned, and ``GET /api/runs/{run_id}`` re-reads the persisted evidence rather
than an in-memory copy, so the API response and the stored Run agree by
construction. Source images are neither copied nor transmitted anywhere except
to the local console viewer, and only when they are registered inputs.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import threading
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..domain.entities import AuditEvent, AuditEventType, DataClassification
from ..domain.primitives import CanonicalJson
from ..domain.run import CompletedRun
from ..engines.ofiq.adapter import OFIQEngineError, OFIQQualityEngine, OFIQSingleSampleResult
from ..datasets import DatasetImportError, RegisteredDatasetImporter
from ..engines.ofiq.batch import BatchJobError, OFIQBatchJob
from ..projects import ProjectLifecycleError, ProjectService, ProjectView
from ..protocols import ProtocolError, ProtocolService
from ..matchers.matrix import MatcherMatrixJob, MatrixError
from ..analysis.utility import UtilityAnalysisError, UtilityAnalysisJob, UtilityRequest, pair_quality_policies
from ..analysis.crossmatcher import CrossMatcherError, CrossMatcherJob, CrossMatcherRequest, matcher_sets
from ..reporting import bundle as bundle_module
from ..reporting import figures as figure_module
from ..reporting.dashboards import DashboardError, load_dashboard, save_dashboard
from ..reporting.exports import ExportError, export_table
from ..reporting.reports import ReportBuilder, ReportError
from ..security.network_guard import guard_status, install_network_guard
from ..importers.external_scores import EXTERNAL_CONDITION_ID, ExternalScoreImportError, ExternalScoreImporter
from ..security.purge import PurgeError, SCOPE_STATEMENT, purge_project, purge_targets
from ..matchers.upstream import UpstreamSources
from ..execution.contracts import ContractValidationError, ExecutionOutcomeError
from ..provenance import artifact_identity
from .settings import Settings
from .store import ImmutableRecordConflict, RunStore


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MeasureOut(ApiModel):
    """One engine measurement. ``native`` is the raw engine value; ``scalar`` is
    the ISO/IEC 29794-5 quality component in [0, 100]. Neither is a policy
    decision: QualSight applies no threshold here."""

    name: str
    native: float | None
    scalar: int | None
    failed: bool


class ProvenanceLinks(ApiModel):
    run: str
    project: str
    experiment: str
    sample: str
    audit_events: str


class AssessRequest(ApiModel):
    image_path: str = Field(min_length=1)
    purpose_scope: str = Field(min_length=1)
    project_id: str | None = None


class UtilityAnalysisRequest(ApiModel):
    matcher_run_id: str = Field(min_length=1)
    quality_job_id: str = Field(min_length=1)
    quality_measure: str = Field(default="UnifiedQualityScore", min_length=1)
    pair_quality_policy: str = Field(default="pair.quality.min", min_length=1)
    target_fmr: float = Field(default=0.001, gt=0, lt=1)
    matcher_threshold: float | None = None
    quality_threshold: float | None = None
    partial_interval: float = Field(default=0.2, gt=0, le=1)
    bootstrap_replicates: int = Field(default=1000, ge=0, le=100_000)
    seed: int = Field(default=20260817, ge=0)


class CrossMatcherRequestBody(ApiModel):
    matrix_id: str = Field(min_length=1)
    quality_job_id: str = Field(min_length=1)
    quality_measure: str = Field(default="UnifiedQualityScore", min_length=1)
    pair_quality_policy: str = Field(default="pair.quality.min", min_length=1)
    target_fmr: float = Field(default=0.001, gt=0, lt=1)
    partial_interval: float = Field(default=0.2, gt=0, le=1)
    bootstrap_replicates: int = Field(default=1000, ge=0, le=100_000)
    seed: int = Field(default=20260817, ge=0)


class AnnotationCreate(ApiModel):
    project_id: str = Field(min_length=1)
    subject_type: str = Field(min_length=1, max_length=64)
    subject_id: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1, max_length=4000)
    context: dict[str, Any] = Field(default_factory=dict)


class FigureCreate(ApiModel):
    kind: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)


class DashboardCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    template: str = Field(default="Custom")
    columns: int = Field(default=2, ge=1, le=4)
    panels: list[dict[str, Any]] = Field(min_length=1)
    supersedes: str | None = None


class ReportCreate(ApiModel):
    spec: dict[str, Any] = Field(default_factory=dict)


class BundleCreate(ApiModel):
    run_id: str = Field(min_length=1)


class ReproduceRequest(ApiModel):
    bundle_path: str = Field(min_length=1)


class ExternalScoreImportRequest(ApiModel):
    resource_id: str = Field(min_length=1)
    purpose_scope: str = Field(default="QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1", min_length=1)


class ProjectCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    purpose_scope: str = Field(min_length=1)
    tags: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    classification: DataClassification = DataClassification.PRIVATE


class ProjectSave(ApiModel):
    name: str | None = None
    description: str | None = None
    tags: tuple[str, ...] | None = None
    notes: tuple[str, ...] | None = None


class ProjectDerive(ApiModel):
    name: str = Field(min_length=1, max_length=200)


class DatasetImportRequest(ApiModel):
    resource_id: str = Field(min_length=1)
    verify_hashes: bool = True


class DatasetOut(ApiModel):
    dataset_id: str
    project_id: str
    name: str
    version: str
    source_uri: str
    rights_status: str
    modality: str
    classification: str
    subject_count: int | None
    sample_count: int
    manifest_sha256: str
    manifest: dict[str, Any]
    statistics_sha256: str | None
    evidence_sha256: str
    resource_id: str | None
    approved_scopes: tuple[str, ...]
    common_scopes_with_engine: tuple[str, ...]


class SampleOut(ApiModel):
    sample_id: str
    dataset_id: str
    subject_id: str | None
    source_path: str
    media_type: str
    sha256: str
    size_bytes: int
    metadata: dict[str, Any]


class BatchSelection(ApiModel):
    kind: Literal["all", "subset_fixture", "sample_ids", "subject"]
    sample_ids: tuple[str, ...] = ()
    subject_id: str | None = None
    limit: int | None = Field(default=None, ge=1)


class BatchRequest(ApiModel):
    selection: BatchSelection
    chunks: int = Field(default=1, ge=1, le=32)
    purpose_scope: str | None = None


class JobOut(ApiModel):
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
    resource_use: dict[str, float]
    parquet_path: str | None = None
    engine_version: str | None = None


class ProjectOut(ApiModel):
    project_id: str
    name: str
    description: str
    lifecycle: str
    version: int
    owner_user_id: str
    classification: str
    tags: tuple[str, ...]
    notes: tuple[str, ...]
    created_at: str
    updated_at: str
    scientific_configuration: dict[str, Any]
    run_ids: tuple[str, ...]
    derived_from: dict[str, Any] | None
    evidence_sha256: str


class AssessResponse(ApiModel):
    kind: Literal["ENGINE_MEASUREMENT"] = "ENGINE_MEASUREMENT"
    run_id: str
    evidence_sha256: str
    engine_resource_id: str
    engine_version: str
    engine_state: str
    purpose_scope: str
    image_path: str
    image_sha256: str
    initialization_ms: int
    assessment_ms: int
    runtime_seconds: float
    measures: tuple[MeasureOut, ...]
    failed_measures: tuple[str, ...]
    warnings: tuple[str, ...]
    policy: Literal["NONE_APPLIED"] = "NONE_APPLIED"
    provenance: ProvenanceLinks


class AssessmentFailure(ApiModel):
    error: Literal["ASSESSMENT_FAILED"] = "ASSESSMENT_FAILED"
    image_path: str
    engine_version: str
    exit_code: int
    reason: str
    stderr: str


class RegisteredSample(ApiModel):
    resource_id: str
    resource_state: str
    approved_scopes: tuple[str, ...]
    path: str
    sha256: str
    size_bytes: int
    media_type: str


class ResourceOut(ApiModel):
    resource_id: str
    kind: str
    name: str
    state: str
    version: str | None
    approved_scopes: tuple[str, ...]
    third_party_rights_status: str
    research_use_status: str
    data_classification: str
    consumers: tuple[str, ...]


class HealthOut(ApiModel):
    status: Literal["ok"]
    application_version: str
    registry_sha256: str
    registry_id: str
    engine: ResourceOut
    database_path: str
    actor_id: str = ""
    network_guard: dict[str, Any] = Field(default_factory=dict)


class PurgeRequest(ApiModel):
    confirm: str = Field(min_length=1)


class RunSummaryOut(ApiModel):
    run_id: str
    project_id: str
    experiment_id: str
    sample_id: str
    status: str
    engine_resource_id: str
    engine_version: str
    start_time: str
    end_time: str
    evidence_sha256: str


def _resource_out(engine: OFIQQualityEngine, resource_id: str) -> ResourceOut:
    raw = engine.registry.raw_resource(resource_id)
    return ResourceOut(
        resource_id=raw["resource_id"],
        kind=raw["kind"],
        name=raw["name"],
        state=raw["state"],
        version=raw.get("version"),
        approved_scopes=tuple(raw["approved_scopes"]),
        third_party_rights_status=raw["third_party_rights_status"],
        research_use_status=raw["research_use_status"],
        data_classification=raw["data_classification"],
        consumers=tuple(raw["consumers"]),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_environment()
    state: dict[str, Any] = {}

    if settings.network_guard:
        install_network_guard()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        state["engine"] = OFIQQualityEngine(
            registry_path=settings.registry_path,
            repository_root=settings.repository_root,
            runtime_resource_id=settings.runtime_resource_id,
            input_resource_ids=settings.input_resource_ids,
        )
        state["store"] = RunStore(settings.database_path)
        try:
            yield
        finally:
            state["store"].close()

    app = FastAPI(title="QualSight", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings

    def engine() -> OFIQQualityEngine:
        return state["engine"]

    def store() -> RunStore:
        return state["store"]

    def projects() -> ProjectService:
        return ProjectService(store(), engine(), actor_id=settings.actor)

    def record_action(project_id: str, event_type: AuditEventType, subject_type: str, subject_id: str, details: dict[str, Any]) -> None:
        """Attribute a meaningful scientific action to the configured user with a timestamp (PRD §§6, 50, 55)."""

        occurred = datetime.now(timezone.utc).astimezone()
        payload = CanonicalJson.from_value({"event_type": event_type.value, "subject_type": subject_type, "subject_id": subject_id, "occurred_at": occurred.isoformat(), **details})
        event = AuditEvent(
            audit_event_id=f"audit-event:sha256:{hashlib.sha256((project_id + subject_id + event_type.value + occurred.isoformat() + payload.sha256).encode('utf-8')).hexdigest()}",
            project_id=project_id, actor_id=settings.actor, event_type=event_type, subject_type=subject_type, subject_id=subject_id, occurred_at=occurred,
            details=payload, current_evidence_sha256=payload.sha256, local_only=True,
        )
        store().record_audit_event(event)

    jobs: dict[str, OFIQBatchJob] = {}
    job_lock = threading.Lock()
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="qualsight-batch")

    def job_out(job: OFIQBatchJob | None, row: dict[str, Any] | None) -> JobOut:
        if job is not None:
            progress = job.progress()
            return JobOut(
                job_id=progress.job_id,
                state=progress.state,
                total=progress.total,
                assessed=progress.assessed,
                failed=progress.failed,
                integrity_failures=progress.integrity_failures,
                not_assessed=progress.not_assessed,
                chunks=progress.chunks,
                started_at=progress.started_at,
                ended_at=progress.ended_at,
                cancellation_requested=progress.cancellation_requested,
                run_id=progress.run_id,
                error=progress.error,
                resource_use=progress.resource_use,
                parquet_path=str(job.parquet_path) if job.parquet_path else None,
                engine_version=job.engine_version,
            )
        assert row is not None
        return JobOut(
            job_id=row["job_id"],
            state=row["state"],
            total=row["total"],
            assessed=row["measured"],
            failed=row["engine_row_failures"],
            integrity_failures=row["integrity_failures"],
            not_assessed=row["not_assessed"],
            chunks=0,
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            cancellation_requested=bool(row["cancellation_requested"]),
            run_id=row["run_id"],
            error=None,
            resource_use=json.loads(row["resource_use_json"]),
            parquet_path=row["parquet_path"],
            engine_version=row["engine_version"],
        )

    @app.post("/api/projects/{project_id}/datasets/{dataset_id}/quality-batch", response_model=JobOut, status_code=202)
    def start_batch(project_id: str, dataset_id: str, request: BatchRequest) -> JobOut:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        dataset = store().dataset(dataset_id)
        if dataset is None or dataset.project_id != project_id:
            raise HTTPException(status_code=404, detail="dataset is not recorded in this project")
        selection = request.selection
        if selection.kind == "all":
            samples = store().samples_for_dataset(dataset_id, limit=selection.limit or 1_000_000)
            rule = "all recorded samples of the dataset in source-path order" + (f" (first {selection.limit})" if selection.limit else "")
        elif selection.kind == "subject":
            if not selection.subject_id:
                raise HTTPException(status_code=422, detail="subject selection needs subject_id")
            samples = store().samples_for_dataset(dataset_id, limit=selection.limit or 1_000_000, subject_id=selection.subject_id)
            rule = f"all recorded samples of subject {selection.subject_id}"
        elif selection.kind == "sample_ids":
            samples = [item for item in (store().sample(sample_id) for sample_id in selection.sample_ids) if item is not None and item.dataset_id == dataset_id]
            if len(samples) != len(selection.sample_ids):
                raise HTTPException(status_code=404, detail="a requested sample is not recorded in this dataset")
            rule = f"{len(samples)} explicitly selected sample IDs"
        else:
            fixture_path = settings.repository_root / "resources" / "manifests" / "lfw-subset-fixture-v1.json"
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            tree_root = json.loads(dataset.manifest.text).get("tree_root")
            samples = []
            for image in fixture["images"]:
                sample = store().sample_by_path(dataset_id, f"{tree_root}/{image['path']}")
                if sample is None or sample.sha256 != image["sha256"]:
                    raise HTTPException(status_code=409, detail=f"fixture image is not a verified sample of this dataset: {image['path']}")
                samples.append(sample)
            rule = f"registered subset fixture {fixture['fixture_id']}: {fixture['selection_rule']}"
        if not samples:
            raise HTTPException(status_code=422, detail="the selection contains no samples")
        purpose_scope = request.purpose_scope
        if purpose_scope is None:
            _, _, common = dataset_scopes(dataset)
            if not common:
                raise HTTPException(status_code=409, detail="the dataset resource and the engine share no approved scope")
            purpose_scope = common[0]
        try:
            job = OFIQBatchJob(
                engine=engine(),
                store=store(),
                project=project,
                dataset=dataset,
                samples=samples,
                purpose_scope=purpose_scope,
                job_root=settings.data_dir / "jobs",
                chunks=request.chunks,
                selection_rule=rule,
            )
        except BatchJobError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        with job_lock:
            jobs[job.job_id] = job

        def _run() -> None:
            try:
                job.run()
            except Exception:  # noqa: BLE001 - state carries the error
                pass
            finally:
                store().finish_job(job.job_id, job.ended_at.isoformat() if job.ended_at else "", job.state)

        executor.submit(_run)
        record_action(project_id, AuditEventType.RUN_STARTED, "QualityBatchJob", job.job_id, {"dataset_id": dataset_id, "samples": len(samples), "selection_rule": rule, "purpose_scope": purpose_scope})
        return job_out(job, None)

    @app.get("/api/jobs/{job_id}", response_model=JobOut)
    def job_status(job_id: str) -> JobOut:
        with job_lock:
            job = jobs.get(job_id)
        if job is not None:
            return job_out(job, None)
        row = store().job(job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="job is not recorded")
        return job_out(None, row)

    @app.post("/api/jobs/{job_id}/cancel", response_model=JobOut)
    def job_cancel(job_id: str) -> JobOut:
        with job_lock:
            job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job is not active in this process")
        job.cancel()
        return job_out(job, None)

    @app.get("/api/projects/{project_id}/jobs", response_model=list[JobOut])
    def project_jobs(project_id: str) -> list[JobOut]:
        rows = store().jobs_for_project(project_id)
        items = []
        for row in rows:
            with job_lock:
                job = jobs.get(row["job_id"])
            items.append(job_out(job, row) if job is None else job_out(job, None))
        return items

    @app.get("/api/jobs/{job_id}/distribution")
    def job_distribution(
        job_id: str,
        measure: str = Query(default="UnifiedQualityScore"),
        component: Literal["scalar", "native"] = Query(default="scalar"),
        bins: int = Query(default=20, ge=1, le=200),
    ) -> JSONResponse:
        """Histogram of one measure over the job's Parquet rows (MEASURED rows only).

        Every bin count is derived from the same Parquet the drilldown reads, so
        the distribution reconciles to batch rows by construction; the response
        also carries the exact row totals per outcome for that reconciliation.
        """

        row = store().job(job_id)
        if row is None or not row["parquet_path"]:
            raise HTTPException(status_code=404, detail="job has no recorded results")
        import duckdb

        column = f'"{measure}.{component}"'
        connection = duckdb.connect()
        try:
            parquet = row["parquet_path"].replace("'", "''")
            columns = {item[0] for item in connection.execute(f"DESCRIBE SELECT * FROM read_parquet('{parquet}')").fetchall()}
            if f"{measure}.{component}" not in columns:
                raise HTTPException(status_code=422, detail=f"unknown measure column {measure}.{component}")
            outcomes = dict(connection.execute(f"SELECT outcome, COUNT(*) FROM read_parquet('{parquet}') GROUP BY outcome").fetchall())
            summary_row = connection.execute(
                f"SELECT COUNT({column}), MIN({column}), MAX({column}), AVG({column}), MEDIAN({column}), "
                f"SUM(CASE WHEN \"{measure}.failed\" THEN 1 ELSE 0 END) FROM read_parquet('{parquet}') WHERE outcome = 'MEASURED'"
            ).fetchone()
            count, minimum, maximum, mean, median, measure_failures = summary_row
            if component == "scalar":
                low, high = 0.0, 100.0
            else:
                low, high = (float(minimum), float(maximum)) if count else (0.0, 1.0)
                if high == low:
                    high = low + 1.0
            width = (high - low) / bins
            histogram = connection.execute(
                f"SELECT LEAST(CAST(FLOOR(({column} - {low}) / {width}) AS INTEGER), {bins - 1}) AS bin, COUNT(*) "
                f"FROM read_parquet('{parquet}') WHERE outcome = 'MEASURED' AND {column} IS NOT NULL GROUP BY bin ORDER BY bin"
            ).fetchall()
        finally:
            connection.close()
        counts = {int(bin_index): int(bin_count) for bin_index, bin_count in histogram}
        bins_out = [
            {"bin": index, "low": low + index * width, "high": low + (index + 1) * width, "count": counts.get(index, 0)}
            for index in range(bins)
        ]
        return JSONResponse(
            content={
                "job_id": job_id,
                "run_id": row["run_id"],
                "measure": measure,
                "component": component,
                "bins": bins_out,
                "measured_rows_with_value": int(count or 0),
                "measure_failures": int(measure_failures or 0),
                "outcomes": {key: int(value) for key, value in outcomes.items()},
                "summary": {"min": minimum, "max": maximum, "mean": mean, "median": median},
                "parquet_sha256": row["parquet_sha256"],
            }
        )

    @app.get("/api/jobs/{job_id}/drilldown")
    def job_drilldown(
        job_id: str,
        min_scalar: int | None = Query(default=None, ge=0, le=100),
        max_scalar: int | None = Query(default=None, ge=0, le=100),
        outcome: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=2000),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        """Real samples behind a distribution point: sample, subject, path, UQS, run link."""

        if store().job(job_id) is None:
            raise HTTPException(status_code=404, detail="job is not recorded")
        total, rows = store().quality_results_for_job(
            job_id, min_scalar=min_scalar, max_scalar=max_scalar, outcome=outcome, limit=limit, offset=offset
        )
        for item in rows:
            item["links"] = {
                "sample": f"/api/samples/{item['sample_id']}",
                "quality": f"/api/samples/{item['sample_id']}/quality",
                "run": f"/api/runs/{item['run_id']}" if item.get("run_id") else None,
                "image": f"/api/samples/image?path={item['source_path']}",
            }
        return JSONResponse(content={"job_id": job_id, "total": total, "offset": offset, "rows": rows})

    @app.get("/api/samples/{sample_id}/quality")
    def sample_quality(sample_id: str) -> JSONResponse:
        """Every recorded quality result for one sample with its Run and provenance links."""

        sample = store().sample(sample_id)
        if sample is None:
            raise HTTPException(status_code=404, detail="sample is not recorded")
        results = store().quality_results_for_sample(sample_id)
        for item in results:
            item["links"] = {
                "run": f"/api/runs/{item['run_id']}" if item.get("run_id") else None,
                "job": f"/api/jobs/{item['job_id']}",
                "audit": f"/api/runs/{item['run_id']}/audit" if item.get("run_id") else None,
            }
        return JSONResponse(
            content={
                "sample": sample.model_dump(mode="json"),
                "results": results,
                "links": {"image": f"/api/samples/image?path={sample.source_path}", "dataset": f"/api/datasets/{sample.dataset_id}"},
            }
        )

    @app.get("/api/jobs/{job_id}/results")
    def job_results(
        job_id: str,
        limit: int = Query(default=100, ge=1, le=20_000),
        offset: int = Query(default=0, ge=0),
        outcome: str | None = Query(default=None),
    ) -> JSONResponse:
        row = store().job(job_id)
        if row is None or not row["parquet_path"]:
            raise HTTPException(status_code=404, detail="job has no recorded results")
        import pyarrow.parquet as pq

        table = pq.read_table(row["parquet_path"])
        if outcome is not None:
            import pyarrow.compute as pc

            table = table.filter(pc.equal(table["outcome"], outcome))
        total = table.num_rows
        page = table.slice(offset, limit).to_pylist()
        return JSONResponse(content={"job_id": job_id, "total": total, "offset": offset, "rows": page, "parquet_sha256": row["parquet_sha256"]})

    def project_out(view: ProjectView) -> ProjectOut:
        import hashlib as _hashlib
        import json as _json

        metadata = _json.loads(view.project.metadata.text)
        return ProjectOut(
            project_id=view.project.project_id,
            name=view.project.name,
            description=view.project.description,
            lifecycle=view.project.lifecycle.value,
            version=view.version,
            owner_user_id=view.project.owner_user_id,
            classification=view.project.classification.value,
            tags=view.project.tags,
            notes=view.project.notes,
            created_at=view.project.created_at.isoformat(),
            updated_at=view.project.updated_at.isoformat(),
            scientific_configuration=metadata.get("scientific_configuration", {}),
            run_ids=view.run_ids,
            derived_from=metadata.get("derived_from"),
            evidence_sha256=_hashlib.sha256(view.project.model_dump_json().encode("utf-8")).hexdigest(),
        )

    @app.post("/api/projects", response_model=ProjectOut, status_code=201)
    def create_project(request: ProjectCreate) -> ProjectOut:
        view = projects().create(
            name=request.name,
            description=request.description,
            purpose_scope=request.purpose_scope,
            tags=request.tags,
            notes=request.notes,
            classification=request.classification,
        )
        return project_out(view)

    @app.get("/api/projects", response_model=list[ProjectOut])
    def list_projects() -> list[ProjectOut]:
        service = projects()
        return [project_out(service.head(project.project_id)) for project, _ in store().list_project_heads()]

    def _lifecycle(project_id: str, action: str, **fields: Any) -> ProjectOut:
        service = projects()
        try:
            method = getattr(service, action)
            view = method(project_id, **fields) if fields else method(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return project_out(view)

    @app.post("/api/projects/{project_id}/open", response_model=ProjectOut)
    def open_project(project_id: str) -> ProjectOut:
        return _lifecycle(project_id, "open")

    @app.post("/api/projects/{project_id}/save", response_model=ProjectOut)
    def save_project(project_id: str, request: ProjectSave) -> ProjectOut:
        return _lifecycle(project_id, "save", **request.model_dump())

    @app.post("/api/projects/{project_id}/close", response_model=ProjectOut)
    def close_project(project_id: str) -> ProjectOut:
        return _lifecycle(project_id, "close")

    @app.post("/api/projects/{project_id}/archive", response_model=ProjectOut)
    def archive_project(project_id: str) -> ProjectOut:
        return _lifecycle(project_id, "archive")

    @app.post("/api/projects/{project_id}/clone", response_model=ProjectOut, status_code=201)
    def clone_project(project_id: str, request: ProjectDerive) -> ProjectOut:
        return _lifecycle(project_id, "clone", name=request.name)

    @app.post("/api/projects/{project_id}/save-as", response_model=ProjectOut, status_code=201)
    def save_project_as(project_id: str, request: ProjectDerive) -> ProjectOut:
        return _lifecycle(project_id, "save_as", name=request.name)

    def dataset_scopes(dataset) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
        import json as _json

        resource_id = _json.loads(dataset.manifest.text).get("dataset_resource_id")
        eng = engine()
        try:
            resource = eng.registry.resource(resource_id) if resource_id else None
        except Exception:  # noqa: BLE001 - unknown resource id in an old record
            resource = None
        approved = tuple(resource.approved_scopes) if resource else ()
        common = tuple(scope for scope in approved if scope in eng.runtime.approved_scopes)
        return resource_id, approved, common

    def dataset_out(dataset, statistics_sha256: str | None) -> DatasetOut:
        import hashlib as _hashlib
        import json as _json

        resource_id, approved, common = dataset_scopes(dataset)
        return DatasetOut(
            resource_id=resource_id,
            approved_scopes=approved,
            common_scopes_with_engine=common,
            dataset_id=dataset.dataset_id,
            project_id=dataset.project_id,
            name=dataset.name,
            version=dataset.version,
            source_uri=dataset.source_uri,
            rights_status=dataset.rights_status,
            modality=dataset.modality.value,
            classification=dataset.classification.value,
            subject_count=dataset.subject_count.value,
            sample_count=store().sample_count(dataset.dataset_id),
            manifest_sha256=dataset.manifest.sha256,
            manifest=_json.loads(dataset.manifest.text),
            statistics_sha256=statistics_sha256,
            evidence_sha256=_hashlib.sha256(dataset.model_dump_json().encode("utf-8")).hexdigest(),
        )

    @app.post("/api/projects/{project_id}/datasets/import-registered", response_model=DatasetOut, status_code=201)
    def import_registered_dataset(project_id: str, request: DatasetImportRequest) -> DatasetOut:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if request.resource_id != "lfw-funneled-local-candidate":
            raise HTTPException(status_code=422, detail="only the registered LFW resource has an importer in this release")
        importer = RegisteredDatasetImporter(engine().registry, store())
        try:
            result = importer.import_lfw(project, verify_hashes=request.verify_hashes)
        except DatasetImportError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return dataset_out(result.dataset, result.statistics_sha256)

    @app.get("/api/projects/{project_id}/datasets", response_model=list[DatasetOut])
    def project_datasets(project_id: str) -> list[DatasetOut]:
        items = []
        for dataset in store().datasets_for_project(project_id):
            stats = store().dataset_statistics(dataset.dataset_id)
            items.append(dataset_out(dataset, stats[1] if stats else None))
        return items

    @app.get("/api/datasets/{dataset_id}", response_model=DatasetOut)
    def dataset(dataset_id: str) -> DatasetOut:
        found = store().dataset(dataset_id)
        if found is None:
            raise HTTPException(status_code=404, detail="dataset is not recorded")
        stats = store().dataset_statistics(dataset_id)
        return dataset_out(found, stats[1] if stats else None)

    @app.get("/api/datasets/{dataset_id}/statistics")
    def dataset_statistics(dataset_id: str) -> JSONResponse:
        stats = store().dataset_statistics(dataset_id)
        if stats is None:
            raise HTTPException(status_code=404, detail="no statistics are recorded for this dataset")
        statistics = dict(stats[0])
        # Import-time statistics are immutable evidence; quality coverage is
        # recomputed live from every recorded batch result of the dataset.
        statistics["quality_coverage"] = store().quality_coverage(dataset_id)
        return JSONResponse(content={"dataset_id": dataset_id, "evidence_sha256": stats[1], "import_statistics_sha256": stats[1], "statistics": statistics})

    @app.get("/api/datasets/{dataset_id}/samples", response_model=list[SampleOut])
    def dataset_samples(
        dataset_id: str,
        limit: int = Query(default=100, ge=1, le=20_000),
        offset: int = Query(default=0, ge=0),
        subject_id: str | None = Query(default=None),
    ) -> list[SampleOut]:
        import json as _json

        items = store().samples_for_dataset(dataset_id, limit=limit, offset=offset, subject_id=subject_id)
        return [
            SampleOut(
                sample_id=sample.sample_id,
                dataset_id=sample.dataset_id,
                subject_id=sample.subject_id.value,
                source_path=sample.source_path,
                media_type=sample.media_type,
                sha256=sample.sha256,
                size_bytes=sample.size_bytes,
                metadata=_json.loads(sample.metadata.text),
            )
            for sample in items
        ]

    @app.post("/api/projects/{project_id}/datasets/{dataset_id}/protocols/freeze-lfw-view2", status_code=201)
    def freeze_lfw_protocol(project_id: str, dataset_id: str) -> JSONResponse:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        dataset = store().dataset(dataset_id)
        if dataset is None or dataset.project_id != project_id:
            raise HTTPException(status_code=404, detail="dataset is not recorded in this project")
        service = ProtocolService(engine().registry, store(), projects().actor)
        try:
            frozen = service.freeze_lfw_view2(project, dataset)
        except ProtocolError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return JSONResponse(status_code=201, content=frozen.record.model_dump(mode="json"))

    @app.get("/api/projects/{project_id}/protocols")
    def project_protocols(project_id: str) -> JSONResponse:
        return JSONResponse(content=[record.model_dump(mode="json") for record in store().protocols_for_project(project_id)])

    @app.get("/api/protocols/{protocol_id}")
    def protocol(protocol_id: str) -> JSONResponse:
        found = store().protocol(protocol_id)
        if found is None:
            raise HTTPException(status_code=404, detail="protocol is not recorded")
        body = found.model_dump(mode="json")
        body["recorded_pair_count"] = store().protocol_pair_count(protocol_id)
        return JSONResponse(content=body)

    @app.get("/api/protocols/{protocol_id}/pairs")
    def protocol_pairs(
        protocol_id: str,
        limit: int = Query(default=100, ge=1, le=10_000),
        offset: int = Query(default=0, ge=0),
        fold: int | None = Query(default=None, ge=1),
        label: str | None = Query(default=None),
    ) -> JSONResponse:
        if store().protocol(protocol_id) is None:
            raise HTTPException(status_code=404, detail="protocol is not recorded")
        pairs = store().protocol_pairs(protocol_id, limit=limit, offset=offset, fold=fold, label=label)
        return JSONResponse(content=[pair.model_dump(mode="json") for pair in pairs])

    matrices: dict[str, MatcherMatrixJob] = {}

    def leaf_summary(row: dict[str, Any]) -> dict[str, Any]:
        metrics = json.loads(row["metrics_json"])
        excl = metrics["by_policy"]["EXCLUDE_FAILED_TRIALS"]
        gen = metrics["by_policy"]["FAILURES_AS_REJECT"]
        return {
            "provenance_marker": "EXTERNALLY_COMPUTED" if row["condition_id"] == EXTERNAL_CONDITION_ID else "QUALSIGHT_EXECUTED",
            "run_id": row["run_id"],
            "matrix_id": row["matrix_id"],
            "leaf_id": row["leaf_id"],
            "matcher_id": row["matcher_id"],
            "condition_id": row["condition_id"],
            "protocol_id": row["protocol_id"],
            "comparison_count": row["comparison_count"],
            "scored_count": row["scored_count"],
            "failed_count": row["failed_count"],
            "created_at": row["created_at"],
            "eer": {"EXCLUDE_FAILED_TRIALS": excl["eer"], "FAILURES_AS_REJECT": gen["eer"]},
            "operating_points": {"EXCLUDE_FAILED_TRIALS": excl["operating_points"], "FAILURES_AS_REJECT": gen["operating_points"]},
            "view2_tenfold": {"EXCLUDE_FAILED_TRIALS": {k: v for k, v in excl["view2_tenfold"].items() if k != "folds"}, "FAILURES_AS_REJECT": {k: v for k, v in gen["view2_tenfold"].items() if k != "folds"}},
            "counts": metrics["counts"],
            "metrics_sha256": row["metrics_sha256"],
            "comparisons_sha256": row["comparisons_sha256"],
            "links": {"run": f"/api/runs/{row['run_id']}", "metrics": f"/api/matcher-runs/{row['run_id']}/metrics", "comparisons": f"/api/matcher-runs/{row['run_id']}/comparisons", "errors": f"/api/matcher-runs/{row['run_id']}/errors"},
        }

    @app.post("/api/projects/{project_id}/protocols/{protocol_id}/matrix", status_code=202)
    def start_matrix(project_id: str, protocol_id: str, fixture_pairs_only: bool = Query(default=False)) -> JSONResponse:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        protocol = store().protocol(protocol_id)
        if protocol is None or protocol.project_id != project_id:
            raise HTTPException(status_code=404, detail="protocol is not recorded in this project")
        dataset = store().dataset(protocol.dataset_id)
        pairs = store().protocol_pairs(protocol_id, limit=1_000_000)
        rule = "every pair of the frozen protocol"
        if fixture_pairs_only:
            fixture = json.loads((settings.repository_root / "resources" / "manifests" / "lfw-subset-fixture-v1.json").read_text(encoding="utf-8"))
            wanted = {p["pair_id"] for p in fixture["pairs"]}
            pairs = [pair for pair in pairs if pair.pair_id in wanted]
            rule = "the registered subset-fixture pairs of the frozen protocol"
        upstream = UpstreamSources(settings.repository_root / "resources" / "manifests" / "matcher-upstream-sources-v1.json", settings.data_dir)
        try:
            upstream.acquire()
            job = MatcherMatrixJob(
                registry=engine().registry, registry_path=settings.registry_path, repository_root=settings.repository_root, upstream=upstream,
                store=store(), project=project, dataset=dataset, protocol=protocol, pairs=pairs, job_root=settings.data_dir / "matrix", selection_rule=rule,
            )
        except (MatrixError, Exception) as error:  # noqa: BLE001 - surfaced as a typed HTTP error
            raise HTTPException(status_code=409, detail=str(error)) from error
        with job_lock:
            matrices[job.matrix_id] = job

        def _run() -> None:
            try:
                job.run()
            except Exception as error:  # noqa: BLE001
                job.progress = {"phase": "failed", "detail": f"{type(error).__name__}: {error}"}

        executor.submit(_run)
        record_action(project_id, AuditEventType.RUN_STARTED, "MatcherMatrix", job.matrix_id, {"protocol_id": protocol_id, "pairs": len(job.pairs), "samples": len(job.sample_ids), "selection_rule": rule})
        return JSONResponse(status_code=202, content={"matrix_id": job.matrix_id, "pairs": len(job.pairs), "samples": len(job.sample_ids), "progress": job.progress})

    @app.post("/api/projects/{project_id}/protocols/{protocol_id}/import-external-scores", status_code=201)
    def import_external_scores(project_id: str, protocol_id: str, request: ExternalScoreImportRequest) -> JSONResponse:
        """QS-C21: import a registered, externally computed score artifact over a frozen protocol as an EXTERNALLY_COMPUTED Run."""

        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        protocol = store().protocol(protocol_id)
        if protocol is None or protocol.project_id != project_id:
            raise HTTPException(status_code=404, detail="protocol is not recorded in this project")
        try:
            importer = ExternalScoreImporter(store=store(), registry=engine().registry, repository_root=settings.repository_root, project=project, protocol=protocol, resource_id=request.resource_id, job_root=settings.data_dir / "external", actor=projects().actor, purpose_scope=request.purpose_scope)
            result = importer.run()
        except (ExternalScoreImportError, ContractValidationError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        record_action(project_id, AuditEventType.RUN_COMPLETED, "ExternalScoreImport", result.run.run_id, {"resource_id": request.resource_id, "protocol_id": protocol_id, "provenance_marker": "EXTERNALLY_COMPUTED", "mapping": result.mapping})
        row = store().matcher_leaf(result.run.run_id)
        return JSONResponse(status_code=201, content={**leaf_summary(row), "provenance_marker": "EXTERNALLY_COMPUTED", "mapping": result.mapping})

    @app.get("/api/matrices/{matrix_id}")
    def matrix_status(matrix_id: str) -> JSONResponse:
        with job_lock:
            job = matrices.get(matrix_id)
        rows = store().matcher_leaves(matrix_id=matrix_id)
        if job is None and not rows:
            raise HTTPException(status_code=404, detail="matrix is not recorded")
        return JSONResponse(content={"matrix_id": matrix_id, "progress": job.progress if job else {"phase": "completed" if len(rows) == 6 else "partial", "detail": f"{len(rows)} leaves recorded"}, "leaves": [leaf_summary(row) for row in rows]})

    @app.get("/api/projects/{project_id}/matcher-leaves")
    def project_matcher_leaves(project_id: str) -> JSONResponse:
        return JSONResponse(content=[leaf_summary(row) for row in store().matcher_leaves(project_id=project_id)])

    @app.get("/api/matcher-runs/{run_id}/metrics")
    def matcher_run_metrics(run_id: str) -> JSONResponse:
        row = store().matcher_leaf(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="matcher run is not recorded")
        return JSONResponse(content={"run_id": run_id, "leaf_id": row["leaf_id"], "metrics_sha256": row["metrics_sha256"], "metrics": json.loads(row["metrics_json"])})

    def _comparison_rows(row: dict[str, Any], where: str, params: list[Any], limit: int, offset: int) -> tuple[int, list[dict[str, Any]]]:
        import duckdb

        connection = duckdb.connect()
        try:
            parquet = row["comparisons_parquet"].replace("'", "''")
            total = int(connection.execute(f"SELECT COUNT(*) FROM read_parquet('{parquet}') WHERE {where}", params).fetchone()[0])
            cursor = connection.execute(f"SELECT * FROM read_parquet('{parquet}') WHERE {where} ORDER BY fold, source_order LIMIT ? OFFSET ?", [*params, int(limit), int(offset)])
            names = [item[0] for item in cursor.description]
            rows = [dict(zip(names, values)) for values in cursor.fetchall()]
        finally:
            connection.close()
        for item in rows:
            left = store().sample(item["left_sample_id"])
            right = store().sample(item["right_sample_id"])
            item["links"] = {
                "left_sample": f"/api/samples/{item['left_sample_id']}",
                "right_sample": f"/api/samples/{item['right_sample_id']}",
                "left_image": f"/api/samples/image?path={left.source_path}" if left else None,
                "right_image": f"/api/samples/image?path={right.source_path}" if right else None,
                "run": f"/api/runs/{row['run_id']}",
            }
        return total, rows

    @app.get("/api/matcher-runs/{run_id}/comparisons")
    def matcher_run_comparisons(
        run_id: str,
        label: str | None = Query(default=None),
        failed_only: bool = Query(default=False),
        min_score: float | None = Query(default=None),
        max_score: float | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=10_000),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        row = store().matcher_leaf(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="matcher run is not recorded")
        clauses, params = ["1=1"], []
        if label is not None:
            clauses.append("label = ?"); params.append(label)
        if failed_only:
            clauses.append("failure IS NOT NULL")
        if min_score is not None:
            clauses.append("score >= ?"); params.append(min_score)
        if max_score is not None:
            clauses.append("score <= ?"); params.append(max_score)
        total, rows = _comparison_rows(row, " AND ".join(clauses), params, limit, offset)
        return JSONResponse(content={"run_id": run_id, "leaf_id": row["leaf_id"], "matcher_id": row["matcher_id"], "condition_id": row["condition_id"], "total": total, "offset": offset, "rows": rows})

    @app.get("/api/matcher-runs/{run_id}/errors")
    def matcher_run_errors(
        run_id: str,
        threshold: float | None = Query(default=None),
        policy: Literal["EXCLUDE_FAILED_TRIALS", "FAILURES_AS_REJECT"] = Query(default="EXCLUDE_FAILED_TRIALS"),
        limit: int = Query(default=200, ge=1, le=10_000),
    ) -> JSONResponse:
        """False matches and false non-matches at a threshold (default: the recorded EER threshold)."""

        row = store().matcher_leaf(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="matcher run is not recorded")
        metrics = json.loads(row["metrics_json"])
        chosen = threshold if threshold is not None else metrics["by_policy"][policy]["eer"]["threshold"]
        _, false_matches = _comparison_rows(row, "label = 'NON_MATED' AND score IS NOT NULL AND score >= ?", [chosen], limit, 0)
        _, false_non_matches = _comparison_rows(row, "label = 'MATED' AND score IS NOT NULL AND score < ?", [chosen], limit, 0)
        _, failed = _comparison_rows(row, "failure IS NOT NULL", [], limit, 0)
        run = store().run(run_id)
        transform = json.loads(run[0].configuration.text)["matcher"]["transform_id"] if run else None
        return JSONResponse(content={
            "run_id": run_id, "leaf_id": row["leaf_id"], "matcher_id": row["matcher_id"], "condition_id": row["condition_id"], "transform_id": transform,
            "threshold": chosen, "policy": policy, "score_direction": metrics["score_direction"], "threshold_equality_rule": metrics["threshold_equality_rule"],
            "false_matches": false_matches, "false_non_matches": false_non_matches, "processing_failures": failed,
            "counts": {"false_matches": len(false_matches), "false_non_matches": len(false_non_matches), "processing_failures": len(failed)},
        })

    @app.get("/api/matcher-runs/{run_id}/erc")
    def matcher_run_erc(
        run_id: str,
        quality_job_id: str = Query(min_length=1),
        policy: str = Query(default="pair.quality.min"),
        target_fmr: float = Query(default=0.001, gt=0, lt=1),
        threshold: float | None = Query(default=None),
    ) -> JSONResponse:
        """ERC of one matcher Run against the UnifiedQualityScore of a recorded quality job."""

        import numpy as np
        import pyarrow.parquet as pq

        from ..metrics.utility import ErcInputs, PairQualityPolicy, compute_erc

        leaf = store().matcher_leaf(run_id)
        if leaf is None:
            raise HTTPException(status_code=404, detail="matcher run is not recorded")
        job = store().job(quality_job_id)
        if job is None or not job["parquet_path"]:
            raise HTTPException(status_code=404, detail="quality job is not recorded")
        try:
            chosen_policy = PairQualityPolicy(policy)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=f"unknown pair-quality policy {policy}") from error
        rows = pq.read_table(leaf["comparisons_parquet"]).to_pylist()
        quality = {}
        for record in pq.read_table(job["parquet_path"], columns=["sample_id", "outcome", "UnifiedQualityScore.scalar", "UnifiedQualityScore.failed"]).to_pylist():
            if record["outcome"] == "MEASURED" and not record["UnifiedQualityScore.failed"] and record["UnifiedQualityScore.scalar"] is not None:
                quality[record["sample_id"]] = float(record["UnifiedQualityScore.scalar"])
        scores = np.array([r["score"] if r["score"] is not None else np.nan for r in rows], dtype=np.float64)
        mated = np.array([r["label"] == "MATED" for r in rows])
        failed = np.array([r["failure"] is not None for r in rows])
        q_left = np.array([quality.get(r["left_sample_id"], np.nan) for r in rows], dtype=np.float64)
        q_right = np.array([quality.get(r["right_sample_id"], np.nan) for r in rows], dtype=np.float64)
        metrics = json.loads(leaf["metrics_json"])
        if threshold is None:
            point = next((p for p in metrics["by_policy"]["EXCLUDE_FAILED_TRIALS"]["operating_points"] if p["target_fmr"] == target_fmr and p.get("threshold") is not None), None)
            if point is None:
                raise HTTPException(status_code=422, detail=f"no recorded threshold reaches target FMR {target_fmr}")
            chosen = float(point["threshold"])
            provenance = {"source": "RECORDED_OPERATING_POINT", "target_fmr": target_fmr, "failure_policy": "EXCLUDE_FAILED_TRIALS", "fmr_at_threshold": point["fmr"], "fnmr_at_threshold": point["fnmr"]}
        else:
            chosen = float(threshold)
            provenance = {"source": "CALLER_SUPPLIED"}
        erc = compute_erc(ErcInputs(scores, mated, failed, q_left, q_right), policy=chosen_policy, threshold=chosen, threshold_provenance=provenance)
        return JSONResponse(content={
            "run_id": run_id, "leaf_id": leaf["leaf_id"], "matcher_id": leaf["matcher_id"], "condition_id": leaf["condition_id"],
            "quality_job_id": quality_job_id, "quality_run_id": job["run_id"], "quality_measure": "UnifiedQualityScore.scalar",
            "samples_with_quality": len(quality), "erc": erc,
        })

    # -- quality-utility analyses (QS-D30–QS-D33) ---------------------------------

    analyses: dict[str, dict[str, Any]] = {}

    @app.get("/api/pair-quality-policies")
    def list_pair_quality_policies() -> JSONResponse:
        return JSONResponse(content={"version": pair_quality_policies()[0]["version"], "policies": pair_quality_policies()})

    def analysis_summary(row: dict[str, Any]) -> dict[str, Any]:
        summary = json.loads(row["summary_json"])
        summary["links"] = {"analysis": f"/api/utility-analyses/{row['analysis_id']}", "run": f"/api/runs/{row['run_id']}", "pairs": f"/api/utility-analyses/{row['analysis_id']}/pairs", "matcher_run": f"/api/runs/{row['matcher_run_id']}", "quality_job": f"/api/jobs/{row['quality_job_id']}"}
        summary["created_at"] = row["created_at"]
        return summary

    @app.post("/api/projects/{project_id}/utility-analyses", status_code=202)
    def start_utility_analysis(project_id: str, request: UtilityAnalysisRequest) -> JSONResponse:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        try:
            job = UtilityAnalysisJob(store=store(), repository_root=settings.repository_root, project=project, actor=projects().actor, request=UtilityRequest(**request.model_dump()), job_root=settings.data_dir / "analyses")
        except UtilityAnalysisError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        existing = store().utility_analysis(job.analysis_id)
        if existing is not None:
            return JSONResponse(status_code=200, content={"analysis_id": job.analysis_id, "state": "COMPLETED", "already_recorded": True, "summary": analysis_summary(existing)})
        with job_lock:
            if job.analysis_id in analyses:
                return JSONResponse(status_code=202, content={"analysis_id": job.analysis_id, "state": analyses[job.analysis_id]["state"]})
            analyses[job.analysis_id] = {"state": "RUNNING", "error": None}

        def _run() -> None:
            try:
                job.run()
                with job_lock:
                    analyses[job.analysis_id] = {"state": "COMPLETED", "error": None}
            except Exception as error:  # noqa: BLE001 - surfaced through the status endpoint
                with job_lock:
                    analyses[job.analysis_id] = {"state": "FAILED", "error": f"{type(error).__name__}: {error}"}

        executor.submit(_run)
        record_action(project_id, AuditEventType.RUN_STARTED, "UtilityAnalysis", job.analysis_id, {"matcher_run_id": request.matcher_run_id, "quality_job_id": request.quality_job_id, "quality_measure": request.quality_measure, "pair_quality_policy": request.pair_quality_policy})
        return JSONResponse(status_code=202, content={"analysis_id": job.analysis_id, "state": "RUNNING"})

    @app.get("/api/projects/{project_id}/utility-analyses")
    def project_utility_analyses(project_id: str) -> JSONResponse:
        return JSONResponse(content=[analysis_summary(row) for row in store().utility_analyses(project_id=project_id)])

    @app.get("/api/utility-analyses/{analysis_id}")
    def utility_analysis(analysis_id: str, include: Literal["summary", "full"] = Query(default="full")) -> JSONResponse:
        row = store().utility_analysis(analysis_id)
        with job_lock:
            live = analyses.get(analysis_id)
        if row is None:
            if live is None:
                raise HTTPException(status_code=404, detail="utility analysis is not recorded")
            return JSONResponse(content={"analysis_id": analysis_id, "state": live["state"], "error": live["error"]})
        content: dict[str, Any] = {"analysis_id": analysis_id, "state": "COMPLETED", "error": None, "summary": analysis_summary(row)}
        if include == "full":
            directory = Path(row["directory"])
            content["utility"] = json.loads((directory / "utility.json").read_text(encoding="utf-8"))
            content["policy"] = json.loads((directory / "policy.json").read_text(encoding="utf-8"))
            content["artifact_sha256"] = {"utility.json": row["utility_sha256"], "policy.json": row["policy_sha256"], "pair_quality.parquet": row["pair_table_sha256"]}
        return JSONResponse(content=content)

    @app.get("/api/utility-analyses/{analysis_id}/pairs")
    def utility_analysis_pairs(
        analysis_id: str,
        label: str | None = Query(default=None),
        retained: bool | None = Query(default=None),
        decision_error: bool | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=10_000),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        import duckdb

        row = store().utility_analysis(analysis_id)
        if row is None:
            raise HTTPException(status_code=404, detail="utility analysis is not recorded")
        parquet = str(Path(row["directory"]) / "pair_quality.parquet").replace("'", "''")
        clauses, params = ["1=1"], []
        if label is not None:
            clauses.append("label = ?"); params.append(label)
        if retained is not None:
            clauses.append("retained_by_quality_policy = ?"); params.append(retained)
        if decision_error is not None:
            clauses.append("decision_error = ?"); params.append(decision_error)
        where = " AND ".join(clauses)
        connection = duckdb.connect()
        try:
            total = int(connection.execute(f"SELECT COUNT(*) FROM read_parquet('{parquet}') WHERE {where}", params).fetchone()[0])
            cursor = connection.execute(f"SELECT * FROM read_parquet('{parquet}') WHERE {where} ORDER BY pair_quality NULLS LAST, pair_id LIMIT ? OFFSET ?", [*params, int(limit), int(offset)])
            names = [item[0] for item in cursor.description]
            rows = [dict(zip(names, values)) for values in cursor.fetchall()]
        finally:
            connection.close()
        for item in rows:
            left = store().sample(item["left_sample_id"])
            right = store().sample(item["right_sample_id"])
            item["links"] = {"left_sample": f"/api/samples/{item['left_sample_id']}", "right_sample": f"/api/samples/{item['right_sample_id']}", "left_image": f"/api/samples/image?path={left.source_path}" if left else None, "right_image": f"/api/samples/image?path={right.source_path}" if right else None, "left_quality": f"/api/samples/{item['left_sample_id']}/quality", "right_quality": f"/api/samples/{item['right_sample_id']}/quality", "run": f"/api/runs/{row['run_id']}"}
        return JSONResponse(content={"analysis_id": analysis_id, "run_id": row["run_id"], "total": total, "offset": offset, "rows": rows})

    # -- cross-matcher contrasts and the failure workbench (QS-E40–QS-E42) --------

    cross_jobs: dict[str, dict[str, Any]] = {}

    @app.get("/api/matrices/{matrix_id}/sets")
    def matrix_sets(matrix_id: str) -> JSONResponse:
        rows = store().matcher_leaves(matrix_id=matrix_id)
        if not rows:
            raise HTTPException(status_code=404, detail="matrix is not recorded")
        sets = matcher_sets(rows)
        registry = engine().registry
        sets["matcher_rights"] = {row["matcher_id"]: registry.raw_resource(row["matcher_id"])["state"] for row in rows}
        return JSONResponse(content=sets)

    def cross_summary(row: dict[str, Any]) -> dict[str, Any]:
        summary = json.loads(row["summary_json"])
        summary["links"] = {"analysis": f"/api/cross-matcher/{row['analysis_id']}", "run": f"/api/runs/{row['run_id']}", "pairs": f"/api/cross-matcher/{row['analysis_id']}/pairs", "matrix": f"/api/matrices/{row['matrix_id']}", "quality_job": f"/api/jobs/{row['quality_job_id']}"}
        summary["created_at"] = row["created_at"]
        return summary

    @app.post("/api/projects/{project_id}/cross-matcher", status_code=202)
    def start_cross_matcher(project_id: str, request: CrossMatcherRequestBody) -> JSONResponse:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        try:
            job = CrossMatcherJob(store=store(), registry=engine().registry, repository_root=settings.repository_root, project=project, actor=projects().actor, request=CrossMatcherRequest(**request.model_dump()), job_root=settings.data_dir / "analyses")
        except CrossMatcherError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        existing = store().cross_matcher_analysis(job.analysis_id)
        if existing is not None:
            return JSONResponse(status_code=200, content={"analysis_id": job.analysis_id, "state": "COMPLETED", "already_recorded": True, "summary": cross_summary(existing)})
        with job_lock:
            if job.analysis_id in cross_jobs:
                return JSONResponse(status_code=202, content={"analysis_id": job.analysis_id, "state": cross_jobs[job.analysis_id]["state"]})
            cross_jobs[job.analysis_id] = {"state": "RUNNING", "error": None}

        def _run() -> None:
            try:
                job.run()
                with job_lock:
                    cross_jobs[job.analysis_id] = {"state": "COMPLETED", "error": None}
            except Exception as error:  # noqa: BLE001
                with job_lock:
                    cross_jobs[job.analysis_id] = {"state": "FAILED", "error": f"{type(error).__name__}: {error}"}

        executor.submit(_run)
        record_action(project_id, AuditEventType.RUN_STARTED, "CrossMatcherAnalysis", job.analysis_id, {"matrix_id": request.matrix_id, "quality_job_id": request.quality_job_id})
        return JSONResponse(status_code=202, content={"analysis_id": job.analysis_id, "state": "RUNNING"})

    @app.get("/api/projects/{project_id}/cross-matcher")
    def project_cross_matcher(project_id: str) -> JSONResponse:
        return JSONResponse(content=[cross_summary(row) for row in store().cross_matcher_analyses(project_id=project_id)])

    @app.get("/api/cross-matcher/{analysis_id}")
    def cross_matcher_analysis(analysis_id: str, include: Literal["summary", "full"] = Query(default="full")) -> JSONResponse:
        row = store().cross_matcher_analysis(analysis_id)
        with job_lock:
            live = cross_jobs.get(analysis_id)
        if row is None:
            if live is None:
                raise HTTPException(status_code=404, detail="cross-matcher analysis is not recorded")
            return JSONResponse(content={"analysis_id": analysis_id, "state": live["state"], "error": live["error"]})
        content: dict[str, Any] = {"analysis_id": analysis_id, "state": "COMPLETED", "error": None, "summary": cross_summary(row)}
        if include == "full":
            content["document"] = json.loads((Path(row["directory"]) / "crossmatcher.json").read_text(encoding="utf-8"))
            content["artifact_sha256"] = {"crossmatcher.json": row["document_sha256"], "contrast_pairs.parquet": row["pairs_sha256"]}
        return JSONResponse(content=content)

    @app.get("/api/cross-matcher/{analysis_id}/pairs")
    def cross_matcher_pairs(
        analysis_id: str,
        contrast_id: str = Query(min_length=1),
        category: str | None = Query(default=None),
        label: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=10_000),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        import duckdb

        row = store().cross_matcher_analysis(analysis_id)
        if row is None:
            raise HTTPException(status_code=404, detail="cross-matcher analysis is not recorded")
        document = json.loads((Path(row["directory"]) / "crossmatcher.json").read_text(encoding="utf-8"))
        contrast = next((c for c in document["contrasts"] if c["contrast_id"] == contrast_id), None)
        if contrast is None:
            raise HTTPException(status_code=404, detail="contrast is not part of this analysis")
        parquet = str(Path(row["directory"]) / "contrast_pairs.parquet").replace("'", "''")
        clauses, params = ["contrast_id = ?"], [contrast_id]
        if category is not None:
            clauses.append("category = ?"); params.append(category)
        if label is not None:
            clauses.append("label = ?"); params.append(label)
        where = " AND ".join(clauses)
        connection = duckdb.connect()
        try:
            total = int(connection.execute(f"SELECT COUNT(*) FROM read_parquet('{parquet}') WHERE {where}", params).fetchone()[0])
            cursor = connection.execute(f"SELECT * FROM read_parquet('{parquet}') WHERE {where} ORDER BY pair_quality NULLS LAST, pair_id LIMIT ? OFFSET ?", [*params, int(limit), int(offset)])
            names = [item[0] for item in cursor.description]
            rows = [dict(zip(names, values)) for values in cursor.fetchall()]
        finally:
            connection.close()
        annotations_by_pair: dict[str, list[dict[str, Any]]] = {}
        for note in store().annotations(project_id=row["project_id"]):
            annotations_by_pair.setdefault(note["subject_id"], []).append(note)
        for item in rows:
            left = store().sample(item["left_sample_id"])
            right = store().sample(item["right_sample_id"])
            item["annotations"] = annotations_by_pair.get(item["pair_id"], [])
            item["links"] = {"left_sample": f"/api/samples/{item['left_sample_id']}", "right_sample": f"/api/samples/{item['right_sample_id']}", "left_image": f"/api/samples/image?path={left.source_path}" if left else None, "right_image": f"/api/samples/image?path={right.source_path}" if right else None,
                             "left_quality": f"/api/samples/{item['left_sample_id']}/quality", "right_quality": f"/api/samples/{item['right_sample_id']}/quality", "run_a": f"/api/runs/{contrast['a']['run_id']}", "run_b": f"/api/runs/{contrast['b']['run_id']}", "analysis_run": f"/api/runs/{row['run_id']}"}
        return JSONResponse(content={"analysis_id": analysis_id, "contrast": {"contrast_id": contrast_id, "kind": contrast["kind"], "a": contrast["a"], "b": contrast["b"], "held_fixed": contrast["held_fixed"]}, "total": total, "offset": offset, "rows": rows})

    @app.post("/api/annotations", status_code=201)
    def create_annotation(request: AnnotationCreate) -> JSONResponse:
        try:
            projects().require_writable(request.project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        note = store().record_annotation(project_id=request.project_id, subject_type=request.subject_type, subject_id=request.subject_id, actor_id=projects().actor, text=request.text, context=request.context)
        record_action(request.project_id, AuditEventType.ANNOTATION_MODIFIED, request.subject_type, request.subject_id, {"annotation_id": note["annotation_id"], "evidence_sha256": note["evidence_sha256"]})
        return JSONResponse(status_code=201, content=note)

    @app.get("/api/annotations")
    def list_annotations(subject_id: str | None = Query(default=None), project_id: str | None = Query(default=None)) -> JSONResponse:
        if subject_id is None and project_id is None:
            raise HTTPException(status_code=422, detail="subject_id or project_id is required")
        return JSONResponse(content=store().annotations(subject_id=subject_id, project_id=project_id))

    # -- Phase 5: figures, dashboards, reports, exports, bundles, reproduction ------

    def figure_summary(row: dict[str, Any]) -> dict[str, Any]:
        provenance = json.loads(row["provenance_json"])
        return {"figure_id": row["figure_id"], "kind": row["kind"], "title": row["title"], "created_at": row["created_at"], "source_run_ids": json.loads(row["source_run_ids_json"]), "provenance_sha256": row["provenance_sha256"],
                "files": {name: f"/api/figures/{row['figure_id']}/file?format={name}" for name in ("png", "svg", "pdf", "csv", "provenance")}, "sha256": {name: item["sha256"] for name, item in provenance["files"].items()}, "metric_definitions": provenance["metric_definitions"], "thresholds": provenance["thresholds"]}

    @app.post("/api/projects/{project_id}/figures", status_code=201)
    def create_figure(project_id: str, request: FigureCreate) -> JSONResponse:
        try:
            projects().require_writable(project_id)
            spec = figure_module.build(store(), project_id, request.kind, request.parameters)
            record = figure_module.render(spec, settings.data_dir / "figures")
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except figure_module.FigureError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        store().record_figure(record)
        record_action(project_id, AuditEventType.ARTIFACT_EXPORTED, "Figure", record["figure_id"], {"kind": request.kind, "source_run_ids": record["source_run_ids"], "files": {name: item["sha256"] for name, item in record["files"].items()}})
        row = store().figure(record["figure_id"])
        return JSONResponse(status_code=201, content=figure_summary(row))

    @app.get("/api/projects/{project_id}/figures")
    def project_figures(project_id: str) -> JSONResponse:
        return JSONResponse(content=[figure_summary(row) for row in store().figures(project_id=project_id)])

    @app.get("/api/figures/{figure_id}")
    def figure_detail(figure_id: str) -> JSONResponse:
        row = store().figure(figure_id)
        if row is None:
            raise HTTPException(status_code=404, detail="figure is not recorded")
        return JSONResponse(content={**figure_summary(row), "provenance": json.loads(row["provenance_json"])})

    @app.get("/api/figures/{figure_id}/file")
    def figure_file(figure_id: str, format: Literal["png", "svg", "pdf", "csv", "provenance"] = Query(default="png")) -> FileResponse:
        row = store().figure(figure_id)
        if row is None:
            raise HTTPException(status_code=404, detail="figure is not recorded")
        provenance = json.loads(row["provenance_json"])
        if format == "provenance":
            path = Path(row["directory"]) / "figure.provenance.json"
            media = "application/json"
        else:
            path = Path(provenance["files"][format]["path"])
            media = {"png": "image/png", "svg": "image/svg+xml", "pdf": "application/pdf", "csv": "text/csv"}[format]
        if not path.exists():
            raise HTTPException(status_code=410, detail="figure file is missing")
        return FileResponse(str(path), media_type=media, filename=f"{figure_id.replace(':', '_')}.{'provenance.json' if format == 'provenance' else format}")

    @app.post("/api/projects/{project_id}/dashboards", status_code=201)
    def create_dashboard(project_id: str, request: DashboardCreate) -> JSONResponse:
        try:
            projects().require_writable(project_id)
            record = save_dashboard(store(), project_id=project_id, name=request.name, template=request.template, panels=request.panels, columns=request.columns, actor=projects().actor, supersedes=request.supersedes)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except DashboardError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        record_action(project_id, AuditEventType.CONFIGURATION_CHANGED, "Dashboard", record["dashboard_id"], {"name": request.name, "template": request.template, "version": record["version"], "layout_sha256": record["layout_sha256"], "panel_run_ids": record["panel_run_ids"]})
        return JSONResponse(status_code=201, content={k: v for k, v in record.items() if k != "layout_text"})

    @app.get("/api/projects/{project_id}/dashboards")
    def project_dashboards(project_id: str) -> JSONResponse:
        return JSONResponse(content=[{"dashboard_id": row["dashboard_id"], "name": row["name"], "template": row["template"], "version": row["version"], "supersedes": row["supersedes"], "created_at": row["created_at"], "layout_sha256": row["evidence_sha256"], "panels": len(json.loads(row["layout_json"])["panels"])} for row in store().dashboards(project_id=project_id)])

    @app.get("/api/dashboards/{dashboard_id}")
    def dashboard_detail(dashboard_id: str) -> JSONResponse:
        try:
            return JSONResponse(content=load_dashboard(store(), dashboard_id))
        except DashboardError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/projects/{project_id}/reports", status_code=201)
    def create_report(project_id: str, request: ReportCreate) -> JSONResponse:
        try:
            projects().require_writable(project_id)
            builder = ReportBuilder(store=store(), registry=engine().registry, repository_root=settings.repository_root, project_id=project_id, spec=request.spec, actor=projects().actor, root=settings.data_dir / "reports")
            record = builder.build()
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ReportError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        record_action(project_id, AuditEventType.ARTIFACT_EXPORTED, "Report", record["report_id"], {"title": record["title"], "image_policy": record["image_policy"], "images_embedded": record["images_embedded"], "images_withheld": record["images_withheld"], "html_sha256": record["files"]["html"]["sha256"], "pdf_sha256": record["files"]["pdf"]["sha256"]})
        return JSONResponse(status_code=201, content={**record, "links": {"html": f"/api/reports/{record['report_id']}/file?format=html", "pdf": f"/api/reports/{record['report_id']}/file?format=pdf", "json": f"/api/reports/{record['report_id']}/file?format=json"}})

    @app.get("/api/projects/{project_id}/reports")
    def project_reports(project_id: str) -> JSONResponse:
        return JSONResponse(content=[{"report_id": row["report_id"], "title": row["title"], "created_at": row["created_at"], "created_by": row["created_by"], "html_sha256": row["html_sha256"], "pdf_sha256": row["pdf_sha256"], "links": {"html": f"/api/reports/{row['report_id']}/file?format=html", "pdf": f"/api/reports/{row['report_id']}/file?format=pdf", "json": f"/api/reports/{row['report_id']}/file?format=json"}} for row in store().reports(project_id=project_id)])

    @app.get("/api/reports/{report_id}/file")
    def report_file(report_id: str, format: Literal["html", "pdf", "json", "provenance"] = Query(default="html")) -> FileResponse:
        row = store().report(report_id)
        if row is None:
            raise HTTPException(status_code=404, detail="report is not recorded")
        name = {"html": "report.html", "pdf": "report.pdf", "json": "report.json", "provenance": "report.provenance.json"}[format]
        path = Path(row["directory"]) / name
        media = {"html": "text/html", "pdf": "application/pdf", "json": "application/json", "provenance": "application/json"}[format]
        return FileResponse(str(path), media_type=media, filename=f"{report_id.replace(':', '_')}-{name}")

    def _project_of_ref(category: str, ref: str) -> str | None:
        if category == "quality_results":
            job = store().job(ref)
            return job["project_id"] if job else None
        if category in ("matcher_scores", "comparison_records", "embeddings", "metric_tables"):
            row = store().matcher_leaf(ref)
            return row["project_id"] if row else None
        if category == "statistical_results":
            row = store().utility_analysis(ref)
            return row["project_id"] if row else None
        if category == "annotations":
            return ref
        return None

    @app.get("/api/exports/{category}")
    def export_category(category: str, ref: str = Query(min_length=1), format: Literal["csv", "json", "parquet"] = Query(default="csv")) -> FileResponse:
        try:
            record = export_table(store(), category=category, ref=ref, fmt=format, root=settings.data_dir / "exports")
        except ExportError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        media = {"csv": "text/csv", "json": "application/json", "parquet": "application/vnd.apache.parquet"}[format]
        project_of_export = record["source"].get("project_id") or _project_of_ref(category, ref)
        if project_of_export:
            record_action(project_of_export, AuditEventType.ARTIFACT_EXPORTED, "Export", record["export_id"], {"category": category, "ref": ref, "format": format, "rows": record["rows"], "sha256": record["sha256"], "classification": record["classification"]})
        return FileResponse(record["path"], media_type=media, filename=f"{category}-{ref[-12:]}.{format}", headers={"X-QualSight-Export-Id": record["export_id"], "X-QualSight-Export-Sha256": record["sha256"], "X-QualSight-Export-Rows": str(record["rows"]), "X-QualSight-Classification": record["classification"]})

    @app.post("/api/projects/{project_id}/bundles", status_code=201)
    def create_bundle(project_id: str, request: BundleCreate) -> JSONResponse:
        found = store().run(request.run_id)
        if found is None or found[0].project_id != project_id:
            raise HTTPException(status_code=404, detail="run is not recorded in this project")
        try:
            record = bundle_module.export_bundle(store(), run_id=request.run_id, root=settings.data_dir / "bundles", repository_root=settings.repository_root)
        except bundle_module.BundleError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        record_action(project_id, AuditEventType.ARTIFACT_EXPORTED, "Bundle", record["bundle_id"], {"run_id": request.run_id, "sha256": record["sha256"], "size_bytes": record["size_bytes"]})
        return JSONResponse(status_code=201, content={**record, "links": {"download": f"/api/bundles/{record['bundle_id']}/file"}})

    @app.get("/api/projects/{project_id}/bundles")
    def project_bundles(project_id: str) -> JSONResponse:
        return JSONResponse(content=[{**{k: v for k, v in row.items() if k != "manifest_json"}, "manifest": json.loads(row["manifest_json"]), "links": {"download": f"/api/bundles/{row['bundle_id']}/file"}} for row in store().experiment_bundles(project_id=project_id)])

    @app.get("/api/bundles/{bundle_id}/file")
    def bundle_file(bundle_id: str) -> FileResponse:
        rows = [row for row in store().experiment_bundles() if row["bundle_id"] == bundle_id]
        if not rows:
            raise HTTPException(status_code=404, detail="bundle is not recorded")
        return FileResponse(rows[0]["path"], media_type="application/zip", filename=Path(rows[0]["path"]).name)

    @app.post("/api/reproductions", status_code=201)
    def reproduce(request: ReproduceRequest) -> JSONResponse:
        path = Path(request.bundle_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="bundle file does not exist")
        result = bundle_module.reproduce_bundle(path, store=store(), registry=engine().registry, repository_root=settings.repository_root, work_root=settings.data_dir / "reproductions", project_service=projects(), engine=engine())
        original = store().run(result.original_run_id)
        if original is not None:
            record_action(original[0].project_id, AuditEventType.RUN_COMPLETED, "Reproduction", result.reproduction_id, {"original_run_id": result.original_run_id, "state": result.state, "compared_values": result.compared_values, "bundle_sha256": result.bundle_sha256})
        return JSONResponse(status_code=201, content=asdict(result))

    @app.get("/api/reproductions")
    def list_reproductions(original_run_id: str | None = Query(default=None), project_id: str | None = Query(default=None)) -> JSONResponse:
        rows = store().reproductions(original_run_id=original_run_id, project_id=project_id)
        return JSONResponse(content=[json.loads(row["report_json"]) for row in rows])

    @app.get("/api/projects/{project_id}/versions")
    def project_versions(project_id: str) -> JSONResponse:
        versions = store().project_versions(project_id)
        if not versions:
            raise HTTPException(status_code=404, detail="project has no recorded versions")
        return JSONResponse(
            content=[{"version": version, "project": project.model_dump(mode="json")} for version, project in versions]
        )

    @app.get("/api/projects/{project_id}/audit")
    def project_audit(project_id: str) -> JSONResponse:
        events = store().audit_events_for_project(project_id)
        if not events:
            raise HTTPException(status_code=404, detail="no audit event is recorded for this project")
        return JSONResponse(content=[event.model_dump(mode="json") for event in events])

    @app.get("/api/health", response_model=HealthOut)
    def health() -> HealthOut:
        eng = engine()
        from ..provenance import application_version

        return HealthOut(
            status="ok",
            application_version=application_version(settings.repository_root / "backend" / "pyproject.toml"),
            registry_sha256=eng.registry.file_identity.sha256,
            registry_id=eng.registry.registry_id,
            engine=_resource_out(eng, eng.runtime.resource_id),
            database_path=str(settings.database_path),
            actor_id=settings.actor,
            network_guard=guard_status(),
        )

    @app.get("/api/projects/{project_id}/purge-scope")
    def project_purge_scope(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(content={"project_id": project_id, "scope": SCOPE_STATEMENT, "targets": purge_targets(store(), settings.data_dir, project_id)})

    @app.post("/api/projects/{project_id}/purge", status_code=201)
    def project_purge(project_id: str, request: PurgeRequest) -> JSONResponse:
        try:
            record = purge_project(store(), data_dir=settings.data_dir, project_id=project_id, actor=settings.actor, confirm=request.confirm)
        except PurgeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        record_action(project_id, AuditEventType.DATASET_PURGED, "Project", project_id, {"purge_id": record["purge_id"], "deleted": len(record["deleted"]), "already_absent": len(record["already_absent"]), "failed": len(record["failed"]), "verified": record["verified"], "scope": SCOPE_STATEMENT})
        return JSONResponse(status_code=201, content=record)

    @app.get("/api/projects/{project_id}/purges")
    def project_purges(project_id: str) -> JSONResponse:
        return JSONResponse(content=store().purges(project_id=project_id))

    @app.get("/api/resources", response_model=list[ResourceOut])
    def resources() -> list[ResourceOut]:
        eng = engine()
        return [_resource_out(eng, resource.resource_id) for resource in eng.registry.resources]

    @app.get("/api/samples/registered", response_model=list[RegisteredSample])
    def registered_samples() -> list[RegisteredSample]:
        eng = engine()
        items: list[RegisteredSample] = []
        for resource in eng.input_resources:
            for artifact in resource.artifacts:
                if artifact.role != "DATA" or artifact.identity.path is None:
                    continue
                media_type = mimetypes.guess_type(artifact.identity.path)[0] or "application/octet-stream"
                items.append(
                    RegisteredSample(
                        resource_id=resource.resource_id,
                        resource_state=resource.state,
                        approved_scopes=tuple(resource.approved_scopes),
                        path=artifact.identity.path,
                        sha256=artifact.identity.sha256,
                        size_bytes=artifact.identity.size_bytes,
                        media_type=media_type,
                    )
                )
        return items

    @app.get("/api/samples/image")
    def sample_image(path: str = Query(min_length=1)) -> FileResponse:
        """Serve a registered image or a recorded dataset sample to the local console.

        Only bytes that are either registered DATA artifacts or samples recorded
        by a dataset import (whose digest was verified against the registered
        manifest) are served, and only after re-hashing them.
        """

        eng = engine()
        expected_sha256: str | None = None
        try:
            _, identity = eng.registered_image(path)
            expected_sha256 = identity.sha256
        except ContractValidationError:
            recorded = store().sample_by_source_path(path)
            if recorded is None:
                raise HTTPException(status_code=404, detail="path is neither a registered image nor a recorded dataset sample")
            expected_sha256 = recorded.sha256
        media_type = mimetypes.guess_type(path)[0]
        if media_type is None or not media_type.startswith("image/"):
            raise HTTPException(status_code=415, detail="artifact is not a displayable image")
        current = artifact_identity(Path(path))
        if current.sha256 != expected_sha256:
            raise HTTPException(status_code=409, detail="image bytes drifted from their recorded identity")
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})

    @app.post(
        "/api/quality/assess",
        response_model=AssessResponse,
        responses={422: {"model": AssessmentFailure}},
    )
    def assess(request: AssessRequest) -> AssessResponse | JSONResponse:
        eng = engine()
        target_project = None
        if request.project_id is not None:
            try:
                target_project = projects().require_writable(request.project_id)
            except KeyError as error:
                raise HTTPException(status_code=404, detail="project is not recorded") from error
            except ProjectLifecycleError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
        try:
            result: OFIQSingleSampleResult = eng.assess(request.image_path, request.purpose_scope)
        except ContractValidationError as error:
            message = str(error)
            status = 403 if "approved scope" in message else 404 if "not a registered" in message else 400
            raise HTTPException(status_code=status, detail=message) from error
        except ExecutionOutcomeError as error:
            record = error.record
            failure = AssessmentFailure(
                image_path=request.image_path,
                engine_version=eng.runtime.version or "",
                exit_code=int(record.exit_code),
                reason=str(error),
                stderr=record.stderr.content.decode("utf-8", errors="replace"),
            )
            return JSONResponse(status_code=422, content=failure.model_dump())
        except OFIQEngineError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        if not result.succeeded:
            failure = AssessmentFailure(
                image_path=request.image_path,
                engine_version=result.assessment.engine_version,
                exit_code=int(result.record.exit_code),
                reason=result.row.failure_reason or "engine reported no measures",
                stderr=result.record.stderr.content.decode("utf-8", errors="replace"),
            )
            return JSONResponse(status_code=422, content=failure.model_dump())
        try:
            bundle = eng.build_run(result, purpose_scope=request.purpose_scope, project=target_project)
        except OFIQEngineError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        try:
            evidence_sha256 = store().record_bundle(bundle)
        except ImmutableRecordConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        run = bundle.run
        return AssessResponse(
            run_id=run.run_id,
            evidence_sha256=evidence_sha256,
            engine_resource_id=run.quality_engine.resource_id,
            engine_version=run.quality_engine.version,
            engine_state=run.quality_engine.registry_state,
            purpose_scope=request.purpose_scope,
            image_path=bundle.sample.source_path,
            image_sha256=bundle.sample.sha256,
            initialization_ms=run.initialization_ms,
            assessment_ms=run.assessment_ms,
            runtime_seconds=run.runtime_seconds,
            measures=tuple(
                MeasureOut(name=m.name, native=m.native, scalar=m.scalar, failed=m.failed)
                for m in result.row.measures
            ),
            failed_measures=result.row.failed_measure_names,
            warnings=run.warnings,
            provenance=ProvenanceLinks(
                run=f"/api/runs/{run.run_id}",
                project=f"/api/projects/{run.project_id}",
                experiment=f"/api/experiments/{run.experiment_id}",
                sample=f"/api/samples/{bundle.sample.sample_id}",
                audit_events=f"/api/runs/{run.run_id}/audit",
            ),
        )

    @app.get("/api/runs", response_model=list[RunSummaryOut])
    def runs(limit: int = Query(default=200, ge=1, le=10_000)) -> list[RunSummaryOut]:
        return [RunSummaryOut(**asdict(item)) for item in store().run_summaries(limit)]

    @app.get("/api/runs/{run_id}")
    def run(run_id: str) -> JSONResponse:
        found = store().run(run_id)
        if found is None:
            raise HTTPException(status_code=404, detail="run is not recorded")
        completed, evidence_sha256 = found
        return JSONResponse(
            content={
                "evidence_sha256": evidence_sha256,
                "sample_id": store().sample_id_for_run(run_id),
                "run": completed.model_dump(mode="json"),
            }
        )

    @app.get("/api/runs/{run_id}/audit")
    def run_audit(run_id: str) -> JSONResponse:
        events = store().audit_events_for(run_id)
        if not events:
            raise HTTPException(status_code=404, detail="no audit event is recorded for this run")
        return JSONResponse(content=[event.model_dump(mode="json") for event in events])

    @app.get("/api/projects/{project_id}", response_model=ProjectOut)
    def project(project_id: str) -> ProjectOut:
        try:
            return project_out(projects().head(project_id))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error

    @app.get("/api/experiments/{experiment_id}")
    def experiment(experiment_id: str) -> JSONResponse:
        found = store().experiment(experiment_id)
        if found is None:
            raise HTTPException(status_code=404, detail="experiment is not recorded")
        return JSONResponse(content=found.model_dump(mode="json"))

    @app.get("/api/samples/{sample_id}")
    def sample(sample_id: str) -> JSONResponse:
        found = store().sample(sample_id)
        if found is None:
            raise HTTPException(status_code=404, detail="sample is not recorded")
        return JSONResponse(content=found.model_dump(mode="json"))

    # Shipped console: serve the built frontend from the same local origin when it exists (offline product; API routes take precedence).
    dist = settings.repository_root / "frontend" / "dist"
    if (dist / "index.html").exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(dist), html=True), name="console")
    return app


def run_from_environment() -> None:  # pragma: no cover - process entry point
    import uvicorn

    settings = Settings.from_environment()
    uvicorn.run(create_app(settings), host="127.0.0.1", port=8480, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    run_from_environment()
