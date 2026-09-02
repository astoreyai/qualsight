"""QualSight local control plane.

Every quality result served here comes from a real execution of the registered
engine on registered bytes; every Run is persisted immutably before it is
returned, and ``GET /api/projects/{project_id}/runs/{run_id}`` re-reads the persisted evidence rather
than an in-memory copy, so the API response and the stored Run agree by
construction. Source images are neither copied nor transmitted anywhere except
to the local console viewer, and only when they are registered inputs.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import cv2
import asyncio
import hashlib
import json
import logging
import math
import mimetypes
import os
import stat
import threading
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.background import BackgroundTask

from ..domain.entities import AuditEvent, AuditEventType, DataClassification
from ..domain.direct_pair_comparison import DirectPairThreshold
from ..domain.owner_pair import (
    OwnerPairIntegrityError,
    OwnerPairNotFound,
    OwnerPairRelationship,
    OwnerPairValidationError,
)
from ..domain.primitives import CanonicalJson
from ..domain.run import CompletedRun
from ..addons import ANALYSIS_PARAMETER_CONTRACTS, catalog as addon_catalog, dataset_addons, preprocessing_profiles
from ..dataset_acquisition import (
    MANIFEST_NAME as DATASET_ACQUISITION_MANIFEST_NAME,
    DatasetAcquisitionError,
    VerificationDatasetStore,
)
from ..verification_datasets import (
    AcquiredVerificationDatasetImporter,
    SOURCE_KIND as VERIFICATION_DATASET_SOURCE_KIND,
    VerificationDatasetImportError,
)
from ..engines.ofiq.adapter import OFIQEngineError, OFIQQualityEngine, OFIQSingleSampleResult
from ..engines.ofiq.measure_metadata import (
    native_measure_metadata,
    observed_measure_failure_reason,
)
from ..datasets import DatasetImportError, RegisteredDatasetImporter
from ..engines.ofiq.batch import BatchJobError, MEASURE_NAMES, OFIQBatchJob
from ..projects import ProjectLifecycleError, ProjectService, ProjectView
from ..protocols import ProtocolError, ProtocolService
from ..matchers.matrix import MatcherMatrixJob, MatrixError
from ..analysis.utility import UtilityAnalysisError, UtilityAnalysisJob, UtilityRequest, pair_quality_policies, quality_columns
from ..analysis.crossmatcher import CrossMatcherError, CrossMatcherJob, CrossMatcherRequest, matcher_sets
from ..reporting import bundle as bundle_module
from ..reporting import figures as figure_module
from ..reporting.dashboards import DashboardError, load_dashboard, save_dashboard
from ..reporting.exports import ExportError, export_table
from ..reporting.recovery import (
    reconcile_prepared_reporting_publications,
    reporting_publication_audit_event,
)
from ..reporting.reports import ReportBuilder, ReportError
from ..security.network_guard import guard_status, install_network_guard, permit_explicit_setup_network
from ..security.local_storage import activate_private_umask, secure_data_tree
from ..security.process_lease import DataDirectoryLease
from ..importers.external_scores import (
    EXTERNAL_CONDITION_ID,
    ExternalScoreImportError,
    ExternalScoreImporter,
    recover_prepared_external_score_import,
)
from ..importers.owner_supplied import (
    MAX_UPLOAD_BYTES,
    OWNER_PAIR_MAX_OUTPUT_PAIRS,
    SOURCE_KIND as OWNER_SOURCE_KIND,
    OwnerSuppliedAdmission,
    OwnerSuppliedError,
    OwnerSuppliedImporter,
    freeze_owner_pairs,
)
from ..identification.protocol import IdentificationProtocolError, freeze_lfw_identification
from ..identification.search import IdentificationSearchError, IdentificationSearchJob
from ..transforms.derived import (
    DerivedSetError,
    build_derived_set,
    derived_set_identity,
)
from ..transforms.degradation import DegradationAnalysisError, DegradationAnalysisJob, DegradationRequest
from ..transforms.operators import (
    OPERATOR_VERSION,
    PARAMETER_CONTRACTS,
    PARAMETER_NAMES,
    validate_parameter,
)
from ..matchers.matrix import dataset_resource_of
from ..security.purge import PurgeError, SCOPE_STATEMENT, purge_project, purge_targets, reconcile_incomplete_purges
from ..security.owner_file_lock import serialized_owner_file_mutation
from ..security.privacy import public_diagnostic, redact_private_paths
from ..matchers.upstream import UpstreamSources
from ..matchers.lifecycle import MatcherLifecycleError, MatcherRuntimeManager
from ..matchers.runtime import MatcherId
from ..matchers.conditions import SCRFD_CONDITIONS, ConditionId
from ..matchers.direct_pair import (
    DirectPairComparisonError,
    DirectPairComparisonJob,
    direct_comparison_identity,
)
from .evidence import EvidenceError, HONESTY_STATEMENT, detection_geometry, ofiq_landmarks, pair_evidence, recompute_crop
from ..execution.contracts import ContractValidationError, ExecutionOutcomeError
from ..execution.bounded_executor import BoundedExecutor, ExecutorOverloaded, ExecutorReservation
from ..provenance import application_version, artifact_identity, sha256_file
from .settings import Settings
from .store import ImmutableRecordConflict, RunStore

API_CONTRACT_VERSION = "2.0.0-beta.1"
MAX_DERIVED_OUTPUT_FILES = 20_000


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


async def read_bounded_request_body(request: Request, *, maximum_bytes: int) -> bytes:
    """Read an ASGI body incrementally and stop before it can exceed the admission cap."""

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Content-Length must be an integer") from error
        if declared_bytes < 0:
            raise HTTPException(status_code=400, detail="Content-Length cannot be negative")
        if declared_bytes > maximum_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"request body exceeds the {maximum_bytes}-byte upload cap",
                headers={"X-QualSight-Bytes-Read": "0"},
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"request body exceeds the {maximum_bytes}-byte upload cap",
                headers={"X-QualSight-Bytes-Read": str(len(body))},
            )
        body.extend(chunk)
    return bytes(body)


class MeasureOut(ApiModel):
    """One engine measurement. ``native`` is the raw engine value; ``scalar`` is
    the ISO/IEC 29794-5 quality component in [0, 100]. Neither is a policy
    decision: QualSight applies no threshold here."""

    name: str
    native: float | None
    native_unit: str
    native_semantics: str
    scalar: int | None
    failed: bool
    failure_reason: str | None


class ProvenanceLinks(ApiModel):
    run: str
    project: str
    experiment: str
    sample: str
    audit_events: str


class AssessRequest(ApiModel):
    registered_sample_id: str = Field(
        min_length=len("registered-sample:sha256:") + 64,
        max_length=len("registered-sample:sha256:") + 64,
        pattern=r"^registered-sample:sha256:[0-9a-f]{64}$",
    )
    purpose_scope: str = Field(min_length=1)
    project_id: str | None = None


class OwnerDirectoryImportRequest(ApiModel):
    root: str = Field(min_length=1)
    name: str = Field(min_length=1)


class OwnerManifestImportRequest(ApiModel):
    manifest_path: str = Field(min_length=1)
    name: str = Field(min_length=1)
    mapping: dict[str, str] | None = None
    mapping_name: str | None = None


class OwnerPairsRequest(ApiModel):
    seed: int = Field(ge=ANALYSIS_PARAMETER_CONTRACTS["owner_pair_seed"]["minimum"])
    max_mated_pairs_per_subject: int = Field(default=6, ge=1, le=1000)
    nonmated_multiple: float = Field(default=1.0, gt=0, le=100)


class OwnerPairAdmissionRequest(ApiModel):
    left_sample_id: str = Field(min_length=1, max_length=256)
    right_sample_id: str = Field(min_length=1, max_length=256)
    relationship: OwnerPairRelationship


class DirectPairThresholdRequest(ApiModel):
    contract_id: Literal["matcher_threshold"]
    kind: Literal["FIXED_VALUE"]
    value: float = Field(strict=True, allow_inf_nan=False)


class DirectPairComparisonRequest(ApiModel):
    pair_id: str = Field(min_length=1, max_length=256)
    matcher_id: str = Field(min_length=1, max_length=128)
    preprocessing_id: str = Field(min_length=1, max_length=128)
    threshold: DirectPairThresholdRequest


class UtilityAnalysisRequest(ApiModel):
    matcher_run_id: str = Field(min_length=1)
    quality_job_id: str = Field(min_length=1)
    quality_measure: str = Field(default="UnifiedQualityScore", min_length=1)
    pair_quality_policy: str = Field(default="pair.quality.min", min_length=1)
    target_fmr: float = Field(default=0.001, gt=ANALYSIS_PARAMETER_CONTRACTS["target_fmr"]["minimum"], lt=ANALYSIS_PARAMETER_CONTRACTS["target_fmr"]["maximum"], allow_inf_nan=ANALYSIS_PARAMETER_CONTRACTS["target_fmr"]["allow_inf_nan"])
    matcher_threshold: float | None = Field(
        default=None,
        allow_inf_nan=ANALYSIS_PARAMETER_CONTRACTS["matcher_threshold"]["allow_inf_nan"],
    )
    quality_threshold: float | None = Field(
        default=None,
        allow_inf_nan=ANALYSIS_PARAMETER_CONTRACTS["quality_threshold"]["allow_inf_nan"],
    )
    partial_interval: float = Field(default=0.2, gt=0, le=1)
    bootstrap_replicates: int = Field(default=1000, ge=ANALYSIS_PARAMETER_CONTRACTS["bootstrap_replicates"]["minimum"], le=ANALYSIS_PARAMETER_CONTRACTS["bootstrap_replicates"]["maximum"])
    seed: int = Field(default=20260817, ge=0)


class CrossMatcherRequestBody(ApiModel):
    matrix_id: str = Field(min_length=1)
    quality_job_id: str = Field(min_length=1)
    quality_measure: str = Field(default="UnifiedQualityScore", min_length=1)
    pair_quality_policy: str = Field(default="pair.quality.min", min_length=1)
    target_fmr: float = Field(default=0.001, gt=ANALYSIS_PARAMETER_CONTRACTS["target_fmr"]["minimum"], lt=ANALYSIS_PARAMETER_CONTRACTS["target_fmr"]["maximum"], allow_inf_nan=ANALYSIS_PARAMETER_CONTRACTS["target_fmr"]["allow_inf_nan"])
    partial_interval: float = Field(default=0.2, gt=0, le=1)
    bootstrap_replicates: int = Field(default=1000, ge=ANALYSIS_PARAMETER_CONTRACTS["bootstrap_replicates"]["minimum"], le=ANALYSIS_PARAMETER_CONTRACTS["bootstrap_replicates"]["maximum"])
    seed: int = Field(default=20260817, ge=0)


class AnnotationCreate(ApiModel):
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
    bundle_id: str = Field(min_length=1)


class ExternalScoreImportRequest(ApiModel):
    resource_id: str = Field(min_length=1)
    purpose_scope: str = Field(default="QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1", min_length=1)
    field_mapping: dict[str, str] | None = None
    mapping_name: str | None = None


class IdentificationFreezeRequest(ApiModel):
    selection: Literal["all", "subset_fixture"] = "all"
    rank_depth: int = Field(default=20, ge=1, le=1000)


class IdentificationSearchRequest(ApiModel):
    matcher_id: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    quality_job_id: str | None = None
    purpose_scope: str = Field(default="QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1", min_length=1)


class TransformationSpec(ApiModel):
    kind: Literal[
        "jpeg",
        "gaussian_blur",
        "downscale",
        "gamma",
        "contrast",
        "rotation",
        "occlusion",
    ]
    parameters: tuple[int | float, ...] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def validate_published_contract(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        kind = value.get("kind")
        parameters = value.get("parameters")
        if not isinstance(parameters, (list, tuple)):
            raise ValueError("transform parameters must be a JSON array")
        if not isinstance(kind, str):
            return value
        normalized = dict(value)
        normalized["parameters"] = tuple(validate_parameter(kind, parameter) for parameter in parameters)
        return normalized


class TransformationRequest(ApiModel):
    selection: Literal["all", "subset_fixture", "sample_ids"] = "subset_fixture"
    sample_ids: tuple[str, ...] = Field(default=(), max_length=20_000)
    transforms: tuple[TransformationSpec, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_selection(self) -> "TransformationRequest":
        if self.selection == "sample_ids":
            if not self.sample_ids:
                raise ValueError("sample_ids selection requires at least one sample ID")
            if any(not sample_id or len(sample_id) > 256 for sample_id in self.sample_ids):
                raise ValueError("sample_ids must contain nonempty IDs of at most 256 characters")
            if len(set(self.sample_ids)) != len(self.sample_ids):
                raise ValueError("sample_ids selection must not contain duplicates")
        elif self.sample_ids:
            raise ValueError("sample_ids are accepted only when selection is sample_ids")
        return self


class DegradationAnalysisRequestBody(ApiModel):
    derived_set_id: str = Field(min_length=1)
    quality_job_id: str = Field(min_length=1)
    baseline_quality_job_id: str | None = None
    matcher_id: str = Field(min_length=1)
    condition_id: str = Field(min_length=1)
    matcher_threshold: float = Field(
        allow_inf_nan=ANALYSIS_PARAMETER_CONTRACTS["matcher_threshold"]["allow_inf_nan"]
    )
    protocol_id: str | None = None


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
    source_uri: str | None
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
    execution: Literal["host", "container"] = "host"


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
    engine_version: str | None = None
    dataset_id: str | None = None
    quality_result_available: bool


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
    registered_sample_id: str
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
    registered_sample_id: str
    engine_version: str
    exit_code: int
    reason_code: Literal[
        "ENGINE_EXECUTION_FAILED", "ENGINE_RETURNED_NO_MEASURES"
    ]
    message: str
    failure_event_id: str
    failure_evidence_sha256: str


class RegisteredSample(ApiModel):
    registered_sample_id: str
    resource_id: str
    resource_state: str
    approved_scopes: tuple[str, ...]
    display_name: str
    role: Literal["DATA"] = "DATA"
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
    actor_id: str = ""
    network_guard: dict[str, Any] = Field(default_factory=dict)


class PurgeRequest(ApiModel):
    confirm: str = Field(min_length=1)


class RunSummaryOut(ApiModel):
    run_id: str
    project_id: str
    experiment_id: str
    # Batch, matcher, and analysis Runs cover many samples and record no single sample id.
    sample_id: str | None
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


def _validation_error_json(value: Any) -> Any:
    """Preserve validation detail while making rejected non-finite inputs JSON-safe."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        return {str(key): _validation_error_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_validation_error_json(item) for item in value]
    return str(value)


def _public_diagnostic(value: str | None) -> str | None:
    return public_diagnostic(value) if value is not None else None


_OMIT_PUBLIC_VALUE = object()


def _public_projection(
    value: Any,
    *,
    omit_location_keys: bool = False,
) -> Any:
    """Project a private record for the browser without host locations."""

    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        canonical_or_log_text = "sha256" in value and "text" in value
        for key, item in value.items():
            normalized = str(key).lower()
            location_key = (
                normalized == "path"
                or normalized.endswith("_path")
                or normalized in {"local_root", "managed_root"}
                or "directory" in normalized
            )
            if omit_location_keys and normalized in {"root", "uri", "hostname"}:
                location_key = True
            if location_key or (canonical_or_log_text and normalized == "text"):
                continue
            public_item = _public_projection(
                item,
                omit_location_keys=omit_location_keys,
            )
            if public_item is not _OMIT_PUBLIC_VALUE:
                projected[str(key)] = public_item
        return projected
    if isinstance(value, (list, tuple)):
        return [
            public_item
            for item in value
            if (
                public_item := _public_projection(
                    item,
                    omit_location_keys=omit_location_keys,
                )
            )
            is not _OMIT_PUBLIC_VALUE
        ]
    if isinstance(value, str):
        return redact_private_paths(value)
    return value


def _public_run(completed: CompletedRun) -> dict[str, Any]:
    return _public_projection(
        completed.model_dump(mode="json"),
        omit_location_keys=True,
    )


def _public_system_event(record: dict[str, Any]) -> dict[str, Any]:
    projected = _public_projection(record, omit_location_keys=True)
    details = projected.get("details")
    if isinstance(details, dict):
        private_diagnostic_keys = {
            "error",
            "exception",
            "message",
            "reason",
            "stderr",
            "traceback",
        }
        omitted = private_diagnostic_keys.intersection(details)
        for key in omitted:
            details.pop(key, None)
        if omitted:
            details["diagnostic_state"] = "PRIVATE_LOCAL_EVIDENCE"
    return projected


def _public_reproduction_result(record: dict[str, Any]) -> dict[str, Any]:
    """Expose reproduction state without host locations or native diagnostics."""

    projected = _public_projection(record, omit_location_keys=True)
    private_diagnostic_keys = {
        "error",
        "exception",
        "message",
        "reason",
        "stderr",
        "traceback",
    }

    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            public: dict[str, Any] = {}
            diagnostics_omitted = False
            for key, item in value.items():
                if key in private_diagnostic_keys:
                    diagnostics_omitted = True
                    continue
                if key == "missing_or_drifted":
                    public["missing_or_drifted_count"] = (
                        len(item) if isinstance(item, list) else 0
                    )
                    continue
                public[key] = sanitize(item)
            if diagnostics_omitted:
                public["diagnostic_state"] = "PRIVATE_LOCAL_EVIDENCE"
            return public
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    return sanitize(projected)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_environment()
    state: dict[str, Any] = {}
    process_started = time.monotonic()
    request_metrics_lock = threading.Lock()
    request_metrics: dict[str, Any] = {
        "requests": 0,
        "responses_by_status": {},
        "total_duration_seconds": 0.0,
    }
    operations_logger = logging.getLogger("qualsight.operations")

    activate_private_umask()
    secure_data_tree(settings.data_dir, protected_roots=(settings.repository_root, Path.home()))

    if settings.network_guard:
        install_network_guard()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        lease = DataDirectoryLease.acquire(settings.data_dir, owner="api-server")
        state["data_directory_lease"] = lease
        cleanup_lock = threading.Lock()
        cleaned = {"value": False}

        def close_runtime_resources() -> None:
            with cleanup_lock:
                if cleaned["value"]:
                    return
                cleaned["value"] = True
            if "matcher_manager" in state:
                state["matcher_manager"].unload_all()
            if "store" in state:
                state["store"].close()
            lease.close()

        try:
            state["engine"] = OFIQQualityEngine(
                registry_path=settings.registry_path,
                repository_root=settings.repository_root,
                runtime_resource_id=settings.runtime_resource_id,
                input_resource_ids=settings.input_resource_ids,
                execution_timeout_seconds=settings.native_execution_timeout_seconds,
                output_limit_bytes=settings.native_output_limit_bytes,
            )
            state["store"] = RunStore(settings.database_path)
            state["reconciled_owner_publications"] = state[
                "store"
            ].reconcile_prepared_owner_publications(settings.data_dir)
            state["reconciled_operations"] = state["store"].reconcile_interrupted_operations()
            state["reconciled_purges"] = reconcile_incomplete_purges(
                state["store"], data_dir=settings.data_dir
            )
            state["reconciled_reporting_publications"] = (
                reconcile_prepared_reporting_publications(
                    state["store"],
                    data_dir=settings.data_dir,
                    actor_id=settings.actor,
                )
            )
            state["upstream"] = UpstreamSources(
                settings.repository_root / "resources" / "manifests" / "matcher-upstream-sources-v1.json",
                settings.data_dir,
                cache_root=settings.upstream_cache_root,
            )
            state["matcher_manager"] = MatcherRuntimeManager(state["engine"].registry, state["upstream"])
            yield
        finally:
            state["shutting_down"] = True
            for quality_job in tuple(jobs.values()):
                quality_job.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            if executor.wait_for_idle(timeout_seconds=settings.shutdown_drain_seconds):
                executor.shutdown(wait=True, cancel_futures=False)
                close_runtime_resources()
            else:
                state["shutdown_drain_timed_out"] = True

                def reap_after_scientific_work() -> None:
                    while not executor.wait_for_idle(timeout_seconds=60.0):
                        continue
                    executor.shutdown(wait=True, cancel_futures=False)
                    close_runtime_resources()

                threading.Thread(
                    target=reap_after_scientific_work,
                    name="qualsight-shutdown-reaper",
                    daemon=True,
                ).start()

    app = FastAPI(
        title="QualSight",
        version=application_version(settings.repository_root / "backend" / "pyproject.toml"),
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_response(_: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": _validation_error_json(error.errors())})

    @app.middleware("http")
    async def record_structured_request(request: Request, call_next):
        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-QualSight-Request-ID"] = request_id
            response.headers["X-QualSight-API-Version"] = API_CONTRACT_VERSION
            return response
        finally:
            duration = time.perf_counter() - started
            with request_metrics_lock:
                request_metrics["requests"] += 1
                key = str(status_code)
                request_metrics["responses_by_status"][key] = request_metrics["responses_by_status"].get(key, 0) + 1
                request_metrics["total_duration_seconds"] += duration
            operations_logger.info(
                json.dumps(
                    {
                        "event": "http_request",
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path,
                        "status": status_code,
                        "duration_ms": round(duration * 1000, 3),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

    @app.middleware("http")
    async def enforce_local_browser_boundary(request: Request, call_next):
        """Reject DNS-rebinding hosts and cross-site browser mutations of the loopback API."""

        allowed_hosts = {"127.0.0.1", "localhost"}
        try:
            request_host = urlsplit(f"//{request.headers.get('host', '')}").hostname
        except ValueError:
            request_host = None
        if request_host not in allowed_hosts:
            return JSONResponse(status_code=400, content={"detail": "request Host is not an admitted loopback control-plane name"})
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin:
                try:
                    origin_host = urlsplit(origin).hostname
                except ValueError:
                    origin_host = None
                if origin_host not in allowed_hosts:
                    return JSONResponse(status_code=403, content={"detail": "cross-site mutation of the local control plane is refused"})
        parts = request.url.path.strip("/").split("/")
        mutation_project = (
            parts[2]
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and len(parts) >= 3
            and parts[:2] == ["api", "projects"]
            and not request.url.path.endswith("/purge")
            else None
        )
        if mutation_project:
            with job_lock:
                if (
                    mutation_project in purging_projects
                    or store().active_purge_journal(mutation_project) is not None
                ):
                    return JSONResponse(status_code=409, content={"detail": "project purge is in progress; new work is refused"})
                active_project_mutations[mutation_project] = active_project_mutations.get(mutation_project, 0) + 1
        try:
            return await call_next(request)
        finally:
            if mutation_project:
                with job_lock:
                    remaining = active_project_mutations[mutation_project] - 1
                    if remaining:
                        active_project_mutations[mutation_project] = remaining
                    else:
                        del active_project_mutations[mutation_project]

    def engine() -> OFIQQualityEngine:
        return state["engine"]

    def store() -> RunStore:
        return state["store"]

    def matcher_manager() -> MatcherRuntimeManager:
        return state["matcher_manager"]

    def matcher_upstream() -> UpstreamSources:
        return state["upstream"]

    def verification_dataset_store() -> VerificationDatasetStore:
        return VerificationDatasetStore(
            settings.repository_root / "resources" / "manifests" / DATASET_ACQUISITION_MANIFEST_NAME,
            settings.data_dir,
        )

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

    def record_system_action(event_type: str, subject_id: str, details: dict[str, Any]) -> dict[str, Any]:
        occurred = datetime.now(timezone.utc).astimezone().isoformat()
        payload = {"actor_id": settings.actor, "event_type": event_type, "subject_id": subject_id, "occurred_at": occurred, "details": details}
        event_id = f"system-event:sha256:{hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()}"
        record = {"system_event_id": event_id, **payload}
        evidence = store().record_system_event(record)
        return {**record, "evidence_sha256": evidence}

    jobs: dict[str, OFIQBatchJob] = {}
    direct_pair_jobs: dict[str, DirectPairComparisonJob] = {}
    job_lock = threading.Lock()
    purging_projects: set[str] = set()
    active_project_mutations: dict[str, int] = {}
    active_global_mutations = {"count": 0}
    matcher_control_lock = threading.RLock()
    executor = BoundedExecutor(
        max_workers=settings.scientific_workers,
        max_pending=settings.scientific_pending,
        thread_name_prefix="qualsight-scientific",
    )

    def reserve_scientific_work(operation_id: str) -> ExecutorReservation:
        try:
            return executor.reserve()
        except ExecutorOverloaded as error:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "SCIENTIFIC_WORK_CAPACITY_EXHAUSTED",
                    "operation_id": operation_id,
                    "message": str(error),
                    "capacity": executor.status(),
                },
            ) from error

    def admit_scientific_operation(
        *,
        operation_id: str,
        operation_kind: str,
        project_id: str,
        request: dict[str, Any],
        output_path: Path,
        retry_interrupted: bool = False,
        result_id: str | None = None,
    ) -> tuple[dict[str, Any], ExecutorReservation | None]:
        """Reserve bounded capacity before making a new operation durable."""

        existing = store().operation(operation_id)
        if existing is not None and not retry_interrupted:
            recovered = store().reconcile_prepared_operation(operation_id)
            return recovered or existing, None
        reservation = reserve_scientific_work(operation_id)
        try:
            operation, created = store().admit_operation(
                operation_id=operation_id,
                operation_kind=operation_kind,
                project_id=project_id,
                request=request,
                output_path=output_path,
                retry_interrupted=retry_interrupted,
                result_id=result_id,
            )
        except BaseException:
            reservation.release()
            raise
        if not created:
            reservation.release()
            return operation, None
        return operation, reservation

    def require_project_not_purging_locked(project_id: str) -> None:
        if project_id in purging_projects or store().active_purge_journal(project_id) is not None:
            raise HTTPException(status_code=409, detail="project purge is in progress; new work is refused")

    @contextmanager
    def admitted_mutation(project_id: str | None):
        """Keep purge mutually exclusive with one state-changing request."""

        with job_lock:
            if project_id is None:
                if purging_projects:
                    raise HTTPException(status_code=409, detail="a project purge is in progress; unscoped new work is refused")
                active_global_mutations["count"] += 1
            else:
                require_project_not_purging_locked(project_id)
                active_project_mutations[project_id] = active_project_mutations.get(project_id, 0) + 1
        try:
            yield
        finally:
            with job_lock:
                if project_id is None:
                    active_global_mutations["count"] -= 1
                else:
                    remaining = active_project_mutations[project_id] - 1
                    if remaining:
                        active_project_mutations[project_id] = remaining
                    else:
                        del active_project_mutations[project_id]

    def job_out(job: OFIQBatchJob | None, row: dict[str, Any] | None) -> JobOut:
        quality_result_available = bool(
            row is not None
            and row["state"] == "COMPLETED"
            and row.get("parquet_path")
            and row.get("parquet_sha256")
            and row.get("run_id")
        )
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
                error=_public_diagnostic(progress.error),
                resource_use=progress.resource_use,
                engine_version=job.engine_version,
                dataset_id=job.dataset.dataset_id,
                quality_result_available=quality_result_available,
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
            engine_version=row["engine_version"],
            dataset_id=row["dataset_id"],
            quality_result_available=quality_result_available,
        )

    def operation_job_out(row: dict[str, Any]) -> JobOut:
        request = json.loads(row["request_json"])
        return JobOut(
            job_id=row["result_id"],
            state=row["state"],
            total=int(request["sample_count"]),
            assessed=0,
            failed=0,
            integrity_failures=0,
            not_assessed=0,
            chunks=int(request["chunks"]),
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            cancellation_requested=False,
            run_id=None,
            error=_public_diagnostic(row["error"]),
            resource_use={},
            engine_version=None,
            dataset_id=request["dataset_id"],
            quality_result_available=False,
        )

    def _start_batch(
        project_id: str,
        dataset_id: str,
        request: BatchRequest,
        *,
        retry_operation_id: str | None = None,
    ) -> tuple[JobOut, dict[str, Any]]:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
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
            with job_lock:
                require_project_not_purging_locked(project_id)
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
                    execution=request.execution,
                )
                operation_request = {
                    **request.model_dump(mode="json"),
                    "dataset_id": dataset_id,
                    "purpose_scope": purpose_scope,
                    "sample_count": len(samples),
                    "selection_rule": rule,
                }
                operation, reservation = admit_scientific_operation(
                    operation_id=retry_operation_id or job.job_id,
                    operation_kind="QUALITY_BATCH",
                    project_id=project_id,
                    request=operation_request,
                    output_path=job.job_dir,
                    retry_interrupted=retry_operation_id is not None,
                    result_id=job.job_id,
                )
                if reservation is None:
                    return operation_job_out(operation), operation
                job.bind_operation_request_sha256(
                    CanonicalJson.from_value(operation_request).sha256
                )
                jobs[job.job_id] = job
        except BatchJobError as error:
            raise HTTPException(status_code=422, detail=redact_private_paths(str(error))) from error

        def _run() -> None:
            try:
                store().start_operation(operation["operation_id"])
                job.run()
            except Exception:  # noqa: BLE001 - state carries the error
                pass
            finally:
                terminal_state = job.state if job.state in {"COMPLETED", "FAILED", "CANCELLED"} else "FAILED"
                terminal_error = job.error if terminal_state == "FAILED" else None
                if terminal_state == "FAILED" and not terminal_error:
                    terminal_error = f"quality batch ended from nonterminal state {job.state}"
                store().finish_operation(
                    operation["operation_id"],
                    state=terminal_state,
                    error=terminal_error,
                )
                store().finish_job(job.job_id, job.ended_at.isoformat() if job.ended_at else "", job.state)

        reservation.submit(_run)
        record_action(project_id, AuditEventType.RUN_STARTED, "QualityBatchJob", job.job_id, {"dataset_id": dataset_id, "samples": len(samples), "selection_rule": rule, "purpose_scope": purpose_scope})
        return job_out(job, None), operation

    @app.post("/api/projects/{project_id}/datasets/{dataset_id}/quality-batch", response_model=JobOut, status_code=202)
    def start_batch(project_id: str, dataset_id: str, request: BatchRequest) -> JobOut:
        response, _ = _start_batch(project_id, dataset_id, request)
        return response

    @app.post("/api/projects/{project_id}/operations/{operation_id}/retry", status_code=202)
    def retry_interrupted_operation(project_id: str, operation_id: str) -> JSONResponse:
        requested = store().operation(operation_id)
        if requested is None or requested["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="operation is not recorded in this project")
        request_id = str(requested["request_id"])
        latest = store().latest_operation_attempt(request_id)
        if latest is None or latest["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="operation request is not recorded in this project")
        if latest["state"] != "INTERRUPTED":
            raise HTTPException(
                status_code=409,
                detail=(
                    "only the latest INTERRUPTED attempt can be retried explicitly; "
                    f"latest state is {latest['state']}"
                ),
            )
        payload = json.loads(latest["request_json"])
        if latest["operation_kind"] == "MATCHER_MATRIX":
            fixture_pairs_only = payload.get("fixture_pairs_only")
            leaves = payload.get("leaves")
            protocol_id = payload.get("protocol_id")
            if (
                not isinstance(fixture_pairs_only, bool)
                or not isinstance(protocol_id, str)
                or (leaves is not None and not isinstance(leaves, str))
            ):
                raise HTTPException(
                    status_code=409,
                    detail="the interrupted matcher matrix request contract is invalid",
                )
            execution, admitted = _start_matrix(
                str(latest["project_id"]),
                protocol_id,
                fixture_pairs_only,
                leaves,
                retry_operation_id=request_id,
            )
            return JSONResponse(
                status_code=202,
                content={
                    "operation": operation_out(admitted),
                    "result_id": json.loads(execution.body)["matrix_id"],
                    "execution": json.loads(execution.body),
                },
            )
        if latest["operation_kind"] != "QUALITY_BATCH":
            raise HTTPException(
                status_code=409,
                detail=(
                    "explicit beta retry currently supports QUALITY_BATCH operations only; "
                    f"{latest['operation_kind']} requires a new operator request"
                ),
            )
        batch_request = BatchRequest.model_validate(
            {
                key: payload[key]
                for key in ("selection", "chunks", "purpose_scope", "execution")
            }
        )
        execution, admitted = _start_batch(
            str(latest["project_id"]),
            str(payload["dataset_id"]),
            batch_request,
            retry_operation_id=request_id,
        )
        return JSONResponse(
            status_code=202,
            content={
                "operation": operation_out(admitted),
                "result_id": execution.job_id,
                "execution": execution.model_dump(mode="json"),
            },
        )

    @app.get("/api/projects/{project_id}/jobs/{job_id}", response_model=JobOut)
    def job_status(project_id: str, job_id: str) -> JobOut:
        with job_lock:
            job = jobs.get(job_id)
        if job is not None:
            if job.project.project_id != project_id:
                raise HTTPException(
                    status_code=404, detail="job is not recorded in this project"
                )
            return job_out(job, store().job(job_id))
        row = store().job(job_id)
        if row is None:
            operation = store().latest_operation_for_result(job_id)
            if (
                operation is None
                or operation["operation_kind"] != "QUALITY_BATCH"
                or operation["project_id"] != project_id
            ):
                raise HTTPException(
                    status_code=404, detail="job is not recorded in this project"
                )
            return operation_job_out(operation)
        if row["project_id"] != project_id:
            raise HTTPException(
                status_code=404, detail="job is not recorded in this project"
            )
        return job_out(None, row)

    @app.post("/api/projects/{project_id}/jobs/{job_id}/cancel", response_model=JobOut)
    def job_cancel(project_id: str, job_id: str) -> JobOut:
        with job_lock:
            job = jobs.get(job_id)
        if job is not None and job.project.project_id != project_id:
            raise HTTPException(
                status_code=404, detail="job is not recorded in this project"
            )
        if job is None:
            row = store().job(job_id)
            if row is None or row["project_id"] != project_id:
                raise HTTPException(
                    status_code=404, detail="job is not recorded in this project"
                )
            raise HTTPException(status_code=404, detail="job is not active in this process")
        job.cancel()
        return job_out(job, store().job(job_id))

    @app.post("/api/jobs/{job_id}/cancel", include_in_schema=False)
    def retired_global_job_cancel(job_id: str) -> None:
        raise HTTPException(status_code=404, detail="job route requires project identity")

    @app.get("/api/projects/{project_id}/jobs", response_model=list[JobOut])
    def project_jobs(project_id: str) -> list[JobOut]:
        rows = store().jobs_for_project(project_id)
        items = []
        for row in rows:
            with job_lock:
                job = jobs.get(row["job_id"])
            items.append(job_out(job, row))
        projected_ids = {row["job_id"] for row in rows}
        for row in store().operations_for_project(project_id):
            result_id = row["result_id"]
            if row["operation_kind"] != "QUALITY_BATCH" or result_id in projected_ids:
                continue
            items.append(operation_job_out(row))
            projected_ids.add(result_id)
        return items

    def _project_job_record(project_id: str, job_id: str) -> dict[str, Any]:
        row = store().job(job_id)
        if row is None or row["project_id"] != project_id:
            raise HTTPException(
                status_code=404, detail="job is not recorded in this project"
            )
        return row

    def _verified_job_parquet(row: dict[str, Any]) -> Any:
        recorded_sha256 = row.get("parquet_sha256")
        if not row.get("parquet_path") or not isinstance(recorded_sha256, str):
            raise HTTPException(
                status_code=409,
                detail="quality job result artifact is not immutably recorded",
            )
        try:
            snapshot, _ = open_recorded_snapshot(
                Path(row["parquet_path"]),
                recorded_sha256,
                "quality job result artifact",
            )
        except HTTPException as error:
            raise HTTPException(
                status_code=409,
                detail="quality job result artifact hash does not match its recorded SHA-256",
            ) from error
        return snapshot

    @app.get("/api/projects/{project_id}/jobs/{job_id}/quality-measures")
    def job_quality_measures(project_id: str, job_id: str) -> JSONResponse:
        """List scalar measures recorded by one completed quality job."""

        row = _project_job_record(project_id, job_id)
        if (
            row["state"] != "COMPLETED"
            or not row["parquet_path"]
            or not row["parquet_sha256"]
            or not row["run_id"]
        ):
            raise HTTPException(
                status_code=409,
                detail="quality job is not a completed recorded job",
            )

        import pyarrow as pa
        import pyarrow.parquet as pq

        snapshot = _verified_job_parquet(row)
        try:
            recorded = set(quality_columns(pq.read_schema(snapshot)))
        except (OSError, pa.ArrowException) as error:
            raise HTTPException(
                status_code=409,
                detail="quality job result schema is unreadable",
            ) from error
        finally:
            snapshot.close()
        registered = [name for name in MEASURE_NAMES if name in recorded]
        registered.extend(sorted(recorded - set(MEASURE_NAMES)))
        return JSONResponse(
            content={
                "job_id": job_id,
                "run_id": row["run_id"],
                "parquet_sha256": row["parquet_sha256"],
                "measures": registered,
                "default_measure": (
                    "UnifiedQualityScore"
                    if "UnifiedQualityScore" in recorded
                    else (registered[0] if registered else None)
                ),
            }
        )

    @app.get("/api/projects/{project_id}/jobs/{job_id}/distribution")
    def job_distribution(
        project_id: str,
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

        row = _project_job_record(project_id, job_id)
        import duckdb

        column = f'"{measure}.{component}"'
        snapshot = _verified_job_parquet(row)
        connection = duckdb.connect()
        try:
            parquet = f"/proc/self/fd/{snapshot.fileno()}"
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
            snapshot.close()
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

    @app.get("/api/projects/{project_id}/jobs/{job_id}/drilldown")
    def job_drilldown(
        project_id: str,
        job_id: str,
        min_scalar: int | None = Query(default=None, ge=0, le=100),
        max_scalar: int | None = Query(default=None, ge=0, le=100),
        outcome: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=2000),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        """Real samples behind a distribution point without exposing filesystem paths."""

        _project_job_record(project_id, job_id)
        total, rows = store().quality_results_for_job(
            job_id, min_scalar=min_scalar, max_scalar=max_scalar, outcome=outcome, limit=limit, offset=offset
        )
        for item in rows:
            item.pop("source_path", None)
            if isinstance(item.get("failure_reason"), str):
                item["failure_reason"] = redact_private_paths(item["failure_reason"])
            item["links"] = {
                "sample": f"/api/projects/{project_id}/samples/{item['sample_id']}",
                "quality": f"/api/projects/{project_id}/samples/{item['sample_id']}/quality",
                "run": f"/api/projects/{project_id}/runs/{item['run_id']}" if item.get("run_id") else None,
                "image": f"/api/projects/{project_id}/samples/{item['sample_id']}/image",
            }
        return JSONResponse(content={"job_id": job_id, "total": total, "offset": offset, "rows": rows})

    @app.get("/api/projects/{project_id}/samples/{sample_id}/quality")
    def sample_quality(project_id: str, sample_id: str) -> JSONResponse:
        """Every recorded quality result for one sample with its Run and provenance links."""

        sample = _project_sample(project_id, sample_id)
        results = store().quality_results_for_sample(sample_id)
        for item in results:
            if isinstance(item.get("failure_reason"), str):
                item["failure_reason"] = redact_private_paths(item["failure_reason"])
            item["links"] = {
                "run": f"/api/projects/{project_id}/runs/{item['run_id']}" if item.get("run_id") else None,
                "job": f"/api/projects/{project_id}/jobs/{item['job_id']}",
                "audit": f"/api/projects/{project_id}/runs/{item['run_id']}/audit" if item.get("run_id") else None,
            }
        return JSONResponse(
            content={
                "sample": _public_sample(project_id, sample),
                "results": results,
                "links": {
                    "image": f"/api/projects/{project_id}/samples/{sample.sample_id}/image",
                    "dataset": f"/api/projects/{project_id}/datasets/{sample.dataset_id}",
                },
            }
        )

    @app.get("/api/projects/{project_id}/jobs/{job_id}/results")
    def job_results(
        project_id: str,
        job_id: str,
        limit: int = Query(default=100, ge=1, le=20_000),
        offset: int = Query(default=0, ge=0),
        outcome: str | None = Query(default=None),
    ) -> JSONResponse:
        row = _project_job_record(project_id, job_id)
        import pyarrow.parquet as pq

        snapshot = _verified_job_parquet(row)
        try:
            table = pq.read_table(snapshot)
        finally:
            snapshot.close()
        if outcome is not None:
            import pyarrow.compute as pc

            table = table.filter(pc.equal(table["outcome"], outcome))
        total = table.num_rows
        page = table.slice(offset, limit).to_pylist()
        for item in page:
            item.pop("source_path", None)
            if isinstance(item.get("failure_reason"), str):
                item["failure_reason"] = redact_private_paths(item["failure_reason"])
            sample_id = item["sample_id"]
            item["links"] = {
                "sample": f"/api/projects/{project_id}/samples/{sample_id}",
                "quality": f"/api/projects/{project_id}/samples/{sample_id}/quality",
                "image": f"/api/projects/{project_id}/samples/{sample_id}/image",
                "job": f"/api/projects/{project_id}/jobs/{job_id}",
                "run": f"/api/projects/{project_id}/runs/{row['run_id']}" if row.get("run_id") else None,
            }
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

    def operation_out(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "operation_id": row["operation_id"],
            "request_id": row["request_id"],
            "attempt": row["attempt"],
            "supersedes_operation_id": row["supersedes_operation_id"],
            "result_id": row["result_id"],
            "operation_kind": row["operation_kind"],
            "project_id": row["project_id"],
            "state": row["state"],
            "accepted_at": row["accepted_at"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "request": json.loads(row["request_json"]),
            "error": _public_diagnostic(row["error"]),
            "links": {
                "operation": f"/api/projects/{row['project_id']}/operations/{row['operation_id']}",
                "project": f"/api/projects/{row['project_id']}",
            },
        }

    @app.get("/api/projects/{project_id}/operations/{operation_id}")
    def operation_status(project_id: str, operation_id: str) -> JSONResponse:
        row = store().operation(operation_id)
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="operation is not recorded in this project")
        return JSONResponse(content=operation_out(row))

    @app.get("/api/projects/{project_id}/operations")
    def project_operations(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(
            content=[operation_out(row) for row in store().operations_for_project(project_id)]
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
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
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

        descriptor = _json.loads(dataset.manifest.text)
        resource_id = descriptor.get("dataset_resource_id")
        eng = engine()
        if descriptor.get("source_kind") == "OWNER_SUPPLIED_LOCAL_INPUT":
            # Decision 30: the runtime grant is the scope authority for owner-supplied data.
            return None, tuple(eng.runtime.approved_scopes), tuple(eng.runtime.approved_scopes)
        if descriptor.get("source_kind") == VERIFICATION_DATASET_SOURCE_KIND:
            approved = tuple(descriptor.get("approved_scopes", ()))
            common = tuple(scope for scope in approved if scope in eng.runtime.approved_scopes)
            return None, approved, common
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
            source_uri=_public_dataset_source_uri(dataset.source_uri),
            rights_status=dataset.rights_status,
            modality=dataset.modality.value,
            classification=dataset.classification.value,
            subject_count=dataset.subject_count.value,
            sample_count=store().sample_count(dataset.dataset_id),
            manifest_sha256=dataset.manifest.sha256,
            manifest=_public_dataset_manifest(_json.loads(dataset.manifest.text)),
            statistics_sha256=statistics_sha256,
            evidence_sha256=_hashlib.sha256(dataset.model_dump_json().encode("utf-8")).hexdigest(),
        )

    @app.post("/api/projects/{project_id}/datasets/import-registered", status_code=201)
    def import_registered_dataset(project_id: str, request: DatasetImportRequest) -> JSONResponse:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        if request.resource_id != "lfw-funneled-local-candidate":
            raise HTTPException(status_code=422, detail="only the registered LFW resource has an importer in this release")
        importer = RegisteredDatasetImporter(engine().registry, store())
        try:
            result = importer.import_lfw(project, verify_hashes=request.verify_hashes)
        except DatasetImportError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        return JSONResponse(
            status_code=201,
            content=_public_dataset(result.dataset, result.statistics_sha256),
        )

    @app.get("/api/projects/{project_id}/datasets")
    def project_datasets(project_id: str) -> JSONResponse:
        items = []
        for dataset in store().datasets_for_project(project_id):
            stats = store().dataset_statistics(dataset.dataset_id)
            items.append(_public_dataset(dataset, stats[1] if stats else None))
        return JSONResponse(content=items)

    def _public_dataset_source_uri(value: str) -> str | None:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return value
        return None

    private_dataset_manifest_value = object()

    def _public_dataset_manifest(value: Any) -> Any:
        if isinstance(value, dict):
            public: dict[str, Any] = {}
            for key, item in value.items():
                if key in {
                    "path",
                    "tree_root",
                    "directory",
                    "source_path",
                    "original_name",
                }:
                    continue
                projected = _public_dataset_manifest(item)
                if projected is not private_dataset_manifest_value:
                    public[key] = projected
            return public
        if isinstance(value, list):
            projected = [_public_dataset_manifest(item) for item in value]
            return [
                item for item in projected if item is not private_dataset_manifest_value
            ]
        if isinstance(value, str):
            parsed = urlsplit(value)
            if (
                parsed.scheme == "file"
                or Path(value).is_absolute()
                or PureWindowsPath(value).is_absolute()
            ):
                return private_dataset_manifest_value
        return value

    def _public_dataset(dataset: Dataset, statistics_sha256: str | None) -> dict[str, Any]:
        payload = dataset_out(dataset, statistics_sha256).model_dump(mode="json")
        payload["manifest"] = _public_dataset_manifest(payload["manifest"])
        return payload

    def _project_dataset(project_id: str, dataset_id: str) -> Dataset:
        found = store().dataset(dataset_id)
        if found is None or found.project_id != project_id:
            raise HTTPException(
                status_code=404, detail="dataset is not recorded in this project"
            )
        return found

    @app.get("/api/projects/{project_id}/datasets/{dataset_id}")
    def dataset(project_id: str, dataset_id: str) -> JSONResponse:
        found = _project_dataset(project_id, dataset_id)
        stats = store().dataset_statistics(dataset_id)
        return JSONResponse(
            content=_public_dataset(found, stats[1] if stats else None)
        )

    @app.get("/api/projects/{project_id}/datasets/{dataset_id}/statistics")
    def dataset_statistics(project_id: str, dataset_id: str) -> JSONResponse:
        _project_dataset(project_id, dataset_id)
        stats = store().dataset_statistics(dataset_id)
        if stats is None:
            raise HTTPException(status_code=404, detail="no statistics are recorded for this dataset")
        statistics = dict(stats[0])
        # Import-time statistics are immutable evidence; quality coverage is
        # recomputed live from every recorded batch result of the dataset.
        statistics["quality_coverage"] = store().quality_coverage(dataset_id)
        return JSONResponse(content={"dataset_id": dataset_id, "evidence_sha256": stats[1], "import_statistics_sha256": stats[1], "statistics": statistics})

    @app.get("/api/projects/{project_id}/datasets/{dataset_id}/samples")
    def dataset_samples(
        project_id: str,
        dataset_id: str,
        limit: int = Query(default=100, ge=1, le=20_000),
        offset: int = Query(default=0, ge=0),
        subject_id: str | None = Query(default=None),
    ) -> JSONResponse:
        _project_dataset(project_id, dataset_id)
        items = store().samples_for_dataset(dataset_id, limit=limit, offset=offset, subject_id=subject_id)
        return JSONResponse(
            content=[_public_sample(project_id, sample) for sample in items]
        )

    @app.post("/api/projects/{project_id}/datasets/{dataset_id}/protocols/freeze-lfw-view2", status_code=201)
    def freeze_lfw_protocol(project_id: str, dataset_id: str) -> JSONResponse:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        dataset = store().dataset(dataset_id)
        if dataset is None or dataset.project_id != project_id:
            raise HTTPException(status_code=404, detail="dataset is not recorded in this project")
        service = ProtocolService(engine().registry, store(), projects().actor)
        try:
            frozen = service.freeze_lfw_view2(project, dataset)
        except ProtocolError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        return JSONResponse(
            status_code=201,
            content=_public_projection(
                frozen.record.model_dump(mode="json"), omit_location_keys=True
            ),
        )

    @app.get("/api/projects/{project_id}/protocols")
    def project_protocols(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(
            content=[
                _public_projection(
                    record.model_dump(mode="json"), omit_location_keys=True
                )
                for record in store().protocols_for_project(project_id)
            ]
        )

    @app.get("/api/projects/{project_id}/protocols/{protocol_id}")
    def protocol(project_id: str, protocol_id: str) -> JSONResponse:
        found = store().protocol(protocol_id)
        if found is None or found.project_id != project_id:
            raise HTTPException(status_code=404, detail="protocol is not recorded in this project")
        body = _public_projection(
            found.model_dump(mode="json"), omit_location_keys=True
        )
        body["recorded_pair_count"] = store().protocol_pair_count(protocol_id)
        return JSONResponse(content=body)

    @app.get("/api/projects/{project_id}/protocols/{protocol_id}/pairs")
    def protocol_pairs(
        project_id: str,
        protocol_id: str,
        limit: int = Query(default=100, ge=1, le=10_000),
        offset: int = Query(default=0, ge=0),
        fold: int | None = Query(default=None, ge=1),
        label: str | None = Query(default=None),
    ) -> JSONResponse:
        found = store().protocol(protocol_id)
        if found is None or found.project_id != project_id:
            raise HTTPException(status_code=404, detail="protocol is not recorded in this project")
        pairs = store().protocol_pairs(protocol_id, limit=limit, offset=offset, fold=fold, label=label)
        return JSONResponse(
            content=[
                _public_projection(
                    pair.model_dump(mode="json"), omit_location_keys=True
                )
                for pair in pairs
            ]
        )

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
            "dataset_id": row["dataset_id"],
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
            "links": {"run": f"/api/projects/{row['project_id']}/runs/{row['run_id']}", "metrics": f"/api/projects/{row['project_id']}/matcher-runs/{row['run_id']}/metrics", "comparisons": f"/api/projects/{row['project_id']}/matcher-runs/{row['run_id']}/comparisons", "errors": f"/api/projects/{row['project_id']}/matcher-runs/{row['run_id']}/errors"},
        }

    def _start_matrix(
        project_id: str,
        protocol_id: str,
        fixture_pairs_only: bool,
        leaves: str | None,
        *,
        retry_operation_id: str | None = None,
    ) -> tuple[JSONResponse, dict[str, Any]]:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
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
        matcher_filter = condition_filter = None
        if leaves is not None:
            from ..matchers.matrix import LEAF_IDS

            wanted_leaves = {item.strip() for item in leaves.split(",") if item.strip()}
            if not wanted_leaves:
                raise HTTPException(status_code=422, detail="matrix leaf selection must name at least one leaf")
            unknown = wanted_leaves - set(LEAF_IDS.values())
            if unknown:
                raise HTTPException(status_code=422, detail=f"unknown matrix leaves: {sorted(unknown)}")
            matcher_filter = tuple(sorted({m for (m, c), leaf in LEAF_IDS.items() if leaf in wanted_leaves}, key=lambda m: m.value))
            condition_filter = tuple(sorted({c for (m, c), leaf in LEAF_IDS.items() if leaf in wanted_leaves}, key=lambda c: c.value))
            expanded_leaves = {LEAF_IDS[(matcher, condition)] for matcher in matcher_filter for condition in condition_filter}
            if expanded_leaves != wanted_leaves:
                raise HTTPException(
                    status_code=422,
                    detail="matrix leaf selection must name a complete matcher x preprocessing Cartesian product",
                )
            rule += f" (leaf selection {sorted(wanted_leaves)}: the matcher x condition cross product of the named leaves)"
        try:
            extra = {}
            if matcher_filter is not None:
                extra = {"matchers": matcher_filter, "conditions": condition_filter}
            selected_matchers = matcher_filter if matcher_filter is not None else tuple(MatcherId)
            matcher_manager().require_ready(selected_matchers)
            with job_lock:
                require_project_not_purging_locked(project_id)
                job = MatcherMatrixJob(
                    registry=engine().registry, registry_path=settings.registry_path, repository_root=settings.repository_root, upstream=matcher_upstream(),
                    store=store(), project=project, dataset=dataset, protocol=protocol, pairs=pairs, job_root=settings.data_dir / "matrix", selection_rule=rule, **extra,
                    runtime_manager=matcher_manager(),
                    detector_workers=settings.detector_workers,
                    execution_timeout_seconds=settings.native_execution_timeout_seconds,
                )
                operation_request = {
                    "dataset_id": dataset.dataset_id,
                    "protocol_id": protocol_id,
                    "fixture_pairs_only": fixture_pairs_only,
                    "leaves": leaves,
                    "leaf_ids": sorted(job.leaf_ids),
                    "pair_count": len(job.pairs),
                    "sample_count": len(job.sample_ids),
                    "expected_leaf_count": job.expected_leaf_count,
                    "selection_rule": rule,
                }
                operation, reservation = admit_scientific_operation(
                    operation_id=retry_operation_id or job.matrix_id,
                    operation_kind="MATCHER_MATRIX",
                    project_id=project_id,
                    request=operation_request,
                    output_path=job.job_dir,
                    retry_interrupted=retry_operation_id is not None,
                    result_id=job.matrix_id,
                )
                if reservation is None:
                    return (
                        JSONResponse(
                            status_code=200,
                            content={
                                "matrix_id": job.matrix_id,
                                "state": operation["state"],
                                "error": _public_diagnostic(operation["error"]),
                                "already_recorded": True,
                            },
                        ),
                        operation,
                    )
                matrices[job.matrix_id] = job
        except HTTPException:
            raise
        except (MatrixError, Exception) as error:  # noqa: BLE001 - surfaced as a typed HTTP error
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error

        def _run() -> None:
            active_operation_id = str(operation["operation_id"])
            try:
                store().start_operation(active_operation_id)
                job.run(
                    request_sha256=CanonicalJson.from_value(
                        operation_request
                    ).sha256,
                    resume_interrupted=retry_operation_id is not None,
                )
                store().finish_operation(active_operation_id, state="COMPLETED")
            except Exception as error:  # noqa: BLE001
                detail = f"{type(error).__name__}: {error}"
                job.progress = {"phase": "failed", "detail": detail}
                store().finish_operation(
                    active_operation_id, state="FAILED", error=detail
                )
            finally:
                with job_lock:
                    matrices.pop(job.matrix_id, None)

        reservation.submit(_run)
        record_action(project_id, AuditEventType.RUN_STARTED, "MatcherMatrix", job.matrix_id, {"protocol_id": protocol_id, "pairs": len(job.pairs), "samples": len(job.sample_ids), "selection_rule": rule})
        return (
            JSONResponse(status_code=202, content={"matrix_id": job.matrix_id, "pairs": len(job.pairs), "samples": len(job.sample_ids), "expected_leaves": job.expected_leaf_count, "progress": job.progress}),
            operation,
        )

    @app.post("/api/projects/{project_id}/protocols/{protocol_id}/matrix", status_code=202)
    def start_matrix(
        project_id: str,
        protocol_id: str,
        fixture_pairs_only: bool = Query(default=False),
        leaves: str | None = Query(default=None),
    ) -> JSONResponse:
        response, _operation = _start_matrix(
            project_id,
            protocol_id,
            fixture_pairs_only,
            leaves,
        )
        return response

    @app.post("/api/projects/{project_id}/protocols/{protocol_id}/import-external-scores", status_code=201)
    def import_external_scores(project_id: str, protocol_id: str, request: ExternalScoreImportRequest) -> JSONResponse:
        """QS-C21: import a registered, externally computed score artifact over a frozen protocol as an EXTERNALLY_COMPUTED Run."""

        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        protocol = store().protocol(protocol_id)
        if protocol is None or protocol.project_id != project_id:
            raise HTTPException(status_code=404, detail="protocol is not recorded in this project")
        try:
            with admitted_mutation(project_id):
                field_mapping = request.field_mapping
                if field_mapping is None and request.mapping_name:
                    field_mapping = store().import_mapping(request.mapping_name)
                    if field_mapping is None:
                        raise HTTPException(status_code=404, detail=f"import mapping {request.mapping_name} is not recorded")
                importer = ExternalScoreImporter(store=store(), registry=engine().registry, repository_root=settings.repository_root, project=project, protocol=protocol, resource_id=request.resource_id, job_root=settings.data_dir / "external", actor=projects().actor, purpose_scope=request.purpose_scope, field_mapping=field_mapping)
                if request.mapping_name and request.field_mapping is not None:
                    store().record_import_mapping(request.mapping_name, request.field_mapping, projects().actor, datetime.now(timezone.utc).astimezone().isoformat())
                existing_operation = store().operation(importer.matrix_id)
                if existing_operation is not None:
                    recovered_operation = store().reconcile_prepared_operation(
                        importer.matrix_id
                    )
                    if (
                        recovered_operation is not None
                        and recovered_operation["state"] == "COMPLETED"
                    ):
                        result = recover_prepared_external_score_import(
                            importer.leaf_dir,
                            matrix_id=importer.matrix_id,
                            project_id=project_id,
                            dataset_id=importer.dataset.dataset_id,
                            protocol_id=protocol_id,
                            resource_id=request.resource_id,
                            request_sha256=importer.request_sha256,
                        )
                        record_action(project_id, AuditEventType.RUN_COMPLETED, "ExternalScoreImport", result.run.run_id, {"resource_id": request.resource_id, "protocol_id": protocol_id, "provenance_marker": "EXTERNALLY_COMPUTED", "mapping": result.mapping})
                        row = store().matcher_leaf(result.run.run_id)
                        return JSONResponse(
                            status_code=200,
                            content={
                                **leaf_summary(row),
                                "provenance_marker": "EXTERNALLY_COMPUTED",
                                "mapping": result.mapping,
                            },
                        )
                operation, created = store().admit_operation(
                    operation_id=importer.matrix_id,
                    operation_kind="EXTERNAL_SCORE_IMPORT",
                    project_id=project_id,
                    request=importer.operation_request,
                    output_path=importer.job_dir,
                    result_id=importer.matrix_id,
                )
                if not created:
                    raise ExternalScoreImportError(
                        f"this external-score request is already recorded in state {operation['state']}"
                    )
                try:
                    store().start_operation(operation["operation_id"])
                    result = importer.run(
                        request_sha256=importer.request_sha256,
                        operation_id=operation["operation_id"],
                    )
                except BaseException as error:
                    current = store().operation(operation["operation_id"])
                    if current is not None and current["state"] in {"QUEUED", "RUNNING"}:
                        store().finish_operation(
                            operation["operation_id"],
                            state="FAILED",
                            error=f"{type(error).__name__}: {error}",
                        )
                    raise
                record_action(project_id, AuditEventType.RUN_COMPLETED, "ExternalScoreImport", result.run.run_id, {"resource_id": request.resource_id, "protocol_id": protocol_id, "provenance_marker": "EXTERNALLY_COMPUTED", "mapping": result.mapping})
                row = store().matcher_leaf(result.run.run_id)
        except (ExternalScoreImportError, ContractValidationError) as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        return JSONResponse(status_code=201, content={**leaf_summary(row), "provenance_marker": "EXTERNALLY_COMPUTED", "mapping": result.mapping})

    # -- controlled transformations and degradation responses (QS-X61) ---------------

    degradations: dict[str, DegradationAnalysisJob] = {}

    @app.get("/api/transforms")
    def list_transforms() -> JSONResponse:
        return JSONResponse(
            content={
                "operators": [
                    {
                        "kind": kind,
                        "parameter": name,
                        "version": OPERATOR_VERSION,
                        "parameter_contract": dict(PARAMETER_CONTRACTS[kind]),
                    }
                    for kind, name in PARAMETER_NAMES.items()
                ]
            }
        )

    def _public_derived_sample(project_id: str, row: dict[str, Any]) -> dict[str, Any]:
        chain = [
            {key: value for key, value in item.items() if key != "parent_path"}
            for item in row.get("chain", [])
            if isinstance(item, dict)
        ]
        sample_id = str(row["derived_sample_id"])
        sample = store().sample(sample_id)
        if sample is None:
            raise RuntimeError(
                f"derived sample {sample_id} is missing its immutable sample record"
            )
        return {
            "derived_sample_id": sample_id,
            "parent_sample_id": row["parent_sample_id"],
            "transform": row["transform"],
            "parameter": row["parameter"],
            "level_index": row["level_index"],
            "sha256": row["sha256"],
            "size_bytes": sample.size_bytes,
            "chain": chain,
            "links": {
                "image": f"/api/projects/{project_id}/samples/{sample_id}/image",
                "detection": f"/api/projects/{project_id}/samples/{sample_id}/detection",
                "ofiq_landmarks": f"/api/projects/{project_id}/samples/{sample_id}/ofiq-landmarks",
            },
        }

    def _public_derived_set(
        record: dict[str, Any], *, include_samples: bool = False
    ) -> dict[str, Any]:
        project_id = str(record["project_id"])
        public = {
            key: record[key]
            for key in (
                "derived_set_id",
                "project_id",
                "source_dataset_id",
                "source_resource_id",
                "source_kind",
                "derived_dataset_id",
                "transforms",
                "levels",
                "source_count",
                "derived_count",
                "created_at",
            )
            if key in record
        }
        public["links"] = {
            "detail": f"/api/projects/{project_id}/derived-sets/{record['derived_set_id']}"
        }
        if include_samples:
            public["samples"] = [
                _public_derived_sample(project_id, row)
                for row in store().derived_samples(str(record["derived_set_id"]))
            ]
        return public

    @app.post("/api/projects/{project_id}/datasets/{dataset_id}/transformations", status_code=201)
    def create_transformations(project_id: str, dataset_id: str, request: TransformationRequest) -> JSONResponse:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        dataset = store().dataset(dataset_id)
        if dataset is None or dataset.project_id != project_id:
            raise HTTPException(status_code=404, detail="dataset is not recorded in this project")
        if store().derived_set(dataset_id) is not None:
            raise HTTPException(status_code=409, detail="transformations start from a registered source dataset, not from a derived one")
        transforms = [item.model_dump(mode="json") for item in request.transforms]
        level_count = sum(len(item["parameters"]) for item in transforms)
        if request.selection == "subset_fixture":
            fixture = json.loads((settings.repository_root / "resources" / "manifests" / "lfw-subset-fixture-v1.json").read_text(encoding="utf-8"))
            wanted = {image["path"] for image in fixture["images"]}
            samples = [s for s in store().samples_for_dataset(dataset_id, limit=1_000_000) if any(s.source_path.endswith("/" + path) for path in wanted)]
        elif request.selection == "sample_ids":
            samples = []
            for sample_id in request.sample_ids:
                selected = store().sample(sample_id)
                if selected is None or selected.dataset_id != dataset_id:
                    raise HTTPException(
                        status_code=422,
                        detail=[
                            {
                                "type": "value_error",
                                "loc": ["body", "sample_ids"],
                                "msg": "sample_ids must be an exact set recorded in the route dataset",
                            }
                        ],
                    )
                samples.append(selected)
        else:
            available = store().sample_count(dataset_id)
            if available * level_count > MAX_DERIVED_OUTPUT_FILES:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "derived output cardinality exceeds the controlled beta "
                        f"limit of {MAX_DERIVED_OUTPUT_FILES} files"
                    ),
                )
            samples = store().samples_for_dataset(dataset_id, limit=available)
        if not samples:
            raise HTTPException(status_code=422, detail="the selection contains no samples")
        output_count = len(samples) * level_count
        if output_count > MAX_DERIVED_OUTPUT_FILES:
            raise HTTPException(
                status_code=422,
                detail=(
                    "derived output cardinality exceeds the controlled beta "
                    f"limit of {MAX_DERIVED_OUTPUT_FILES} files"
                ),
            )
        derived_set_id = derived_set_identity(
            project_id=project_id,
            dataset_id=dataset_id,
            samples=samples,
            transforms=transforms,
        )
        operation_id = "operation:sha256:" + hashlib.sha256(
            derived_set_id.encode("utf-8")
        ).hexdigest()
        output_path = settings.data_dir / "derived" / derived_set_id.replace(":", "_")
        operation_request = {
            "dataset_id": dataset_id,
            "derived_set_id": derived_set_id,
            "output_count": output_count,
            "sample_ids": sorted(sample.sample_id for sample in samples),
            "sample_count": len(samples),
            "selection": request.selection,
            "source_sample_ids_sha256": hashlib.sha256(
                "\n".join(sorted(sample.sample_id for sample in samples)).encode("utf-8")
            ).hexdigest(),
            "transforms": transforms,
        }
        existing = store().operation(operation_id)
        if existing is not None:
            existing = store().reconcile_prepared_operation(operation_id) or existing
        retry_interrupted = bool(
            existing is not None and existing["state"] == "INTERRUPTED"
        )
        with admitted_mutation(project_id):
            operation, reservation = admit_scientific_operation(
                operation_id=operation_id,
                operation_kind="DERIVED_SET",
                project_id=project_id,
                request=operation_request,
                output_path=output_path,
                retry_interrupted=retry_interrupted,
                result_id=derived_set_id,
            )
            if reservation is None:
                if operation["state"] == "COMPLETED":
                    recorded = store().derived_set(derived_set_id)
                    if recorded is None or not Path(recorded["directory"]).is_dir():
                        raise HTTPException(
                            status_code=409,
                            detail="derived set could not be recorded atomically",
                        )
                    return JSONResponse(
                        status_code=200,
                        content=_public_derived_set(recorded),
                    )
                if operation["state"] in {"QUEUED", "RUNNING"}:
                    return JSONResponse(
                        status_code=202,
                        content=operation_out(operation),
                    )
                raise HTTPException(
                    status_code=409,
                    detail="the prior derived-set operation did not complete",
                )

            def _run_transformation():
                active_operation_id = str(operation["operation_id"])
                store().start_operation(active_operation_id)
                try:
                    result = build_derived_set(
                        store(),
                        project=project,
                        dataset=dataset,
                        dataset_resource_id=dataset_resource_of(dataset),
                        samples=samples,
                        transforms=transforms,
                        root=settings.data_dir / "derived",
                        actor=projects().actor,
                        request_sha256=CanonicalJson.from_value(
                            operation_request
                        ).sha256,
                    )
                    record_action(
                        project_id,
                        AuditEventType.DATASET_IMPORTED,
                        "DerivedSet",
                        result.derived_set_id,
                        {
                            "source_dataset_id": dataset_id,
                            "derived_dataset_id": result.record["derived_dataset_id"],
                            "transforms": transforms,
                            "source_count": len(samples),
                            "derived_count": len(result.samples),
                        },
                    )
                    store().finish_operation(active_operation_id, state="COMPLETED")
                    return result
                except BaseException:
                    current = store().operation(active_operation_id)
                    if current is not None and current["state"] in {"QUEUED", "RUNNING"}:
                        store().finish_operation(
                            active_operation_id,
                            state="FAILED",
                            error=(
                                "derived-set execution failed; inspect registered "
                                "input integrity and private local diagnostics"
                            ),
                        )
                    raise

            try:
                result = reservation.submit(_run_transformation).result()
            except DerivedSetError as error:
                raise HTTPException(
                    status_code=409,
                    detail=redact_private_paths(str(error)),
                ) from error
            except Exception as error:
                operations_logger.exception(
                    "derived-set execution failed",
                    extra={"derived_set_id": derived_set_id, "project_id": project_id},
                )
                raise HTTPException(
                    status_code=409,
                    detail="derived set could not be recorded atomically",
                ) from error
        return JSONResponse(status_code=201, content=_public_derived_set(result.record))

    @app.get("/api/projects/{project_id}/derived-sets/{derived_set_id}")
    def derived_set_detail(project_id: str, derived_set_id: str) -> JSONResponse:
        record = store().derived_set(derived_set_id)
        if record is None or record["project_id"] != project_id:
            raise HTTPException(
                status_code=404,
                detail="derived set is not recorded in this project",
            )
        return JSONResponse(content=_public_derived_set(record, include_samples=True))

    @app.get("/api/projects/{project_id}/derived-sets")
    def project_derived_sets(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(
            content=[
                _public_derived_set(record)
                for record in store().derived_sets(project_id=project_id)
            ]
        )

    @app.post("/api/projects/{project_id}/degradation-analyses", status_code=202)
    def start_degradation_analysis(project_id: str, request: DegradationAnalysisRequestBody) -> JSONResponse:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        try:
            matcher_manager().require_ready((request.matcher_id,))
            with job_lock:
                require_project_not_purging_locked(project_id)
                job = DegradationAnalysisJob(store=store(), registry=engine().registry, upstream=matcher_upstream(), repository_root=settings.repository_root, project=project, request=DegradationRequest(**request.model_dump()), job_root=settings.data_dir / "analyses", actor=projects().actor, execution_timeout_seconds=settings.native_execution_timeout_seconds, runtime_manager=matcher_manager())
                request_sha256 = CanonicalJson.from_value(
                    request.model_dump(mode="json")
                ).sha256
                operation, reservation = admit_scientific_operation(
                    operation_id=job.analysis_id,
                    operation_kind="DEGRADATION_ANALYSIS",
                    project_id=project_id,
                    request=request.model_dump(mode="json"),
                    output_path=job.directory,
                )
                if reservation is None:
                    return JSONResponse(status_code=200, content={"analysis_id": job.analysis_id, "state": operation["state"], "error": _public_diagnostic(operation["error"]), "already_recorded": True})
                degradations[job.analysis_id] = job
        except (DegradationAnalysisError, ContractValidationError, MatcherLifecycleError) as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error

        def _run() -> None:
            try:
                store().start_operation(job.analysis_id)
                job.run(request_sha256=request_sha256)
                store().finish_operation(job.analysis_id, state="COMPLETED")
            except Exception as error:  # noqa: BLE001
                detail = f"{type(error).__name__}: {error}"
                job.progress = {"phase": "failed", "detail": detail}
                store().finish_operation(job.analysis_id, state="FAILED", error=detail)

        reservation.submit(_run)
        record_action(project_id, AuditEventType.RUN_STARTED, "DegradationAnalysis", job.analysis_id, {"derived_set_id": request.derived_set_id, "quality_job_id": request.quality_job_id, "matcher_id": request.matcher_id, "matcher_threshold": request.matcher_threshold})
        return JSONResponse(status_code=202, content={"analysis_id": job.analysis_id, "state": "RUNNING"})

    @app.get("/api/projects/{project_id}/degradation-analyses/{analysis_id}")
    def degradation_analysis_status(project_id: str, analysis_id: str) -> JSONResponse:
        row = store().degradation_analysis(analysis_id)
        operation = store().operation(analysis_id)
        if row is not None and row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="degradation analysis is not recorded in this project")
        if row is None and (operation is None or operation["project_id"] != project_id or operation["operation_kind"] != "DEGRADATION_ANALYSIS"):
            raise HTTPException(status_code=404, detail="degradation analysis is not recorded in this project")
        with job_lock:
            live = degradations.get(analysis_id)
        if row is None:
            if live is None:
                return JSONResponse(content={"analysis_id": analysis_id, "state": operation["state"], "progress": {"phase": operation["state"].lower(), "detail": _public_diagnostic(operation["error"]) or ""}, "error": _public_diagnostic(operation["error"])})
            return JSONResponse(content={"analysis_id": analysis_id, "state": "FAILED" if live.progress["phase"] == "failed" else "RUNNING", "progress": live.progress, "error": _public_diagnostic(live.progress["detail"]) if live.progress["phase"] == "failed" else None})
        return JSONResponse(content={"analysis_id": analysis_id, "state": "COMPLETED", "run_id": row["run_id"], "summary": json.loads(row["summary_json"]), "document": json.loads((Path(row["directory"]) / "degradation.json").read_text(encoding="utf-8")), "document_sha256": row["document_sha256"]})

    @app.get("/api/projects/{project_id}/degradation-analyses")
    def project_degradation_analyses(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(content=[{**json.loads(row["summary_json"]), "created_at": row["created_at"], "links": {"analysis": f"/api/projects/{project_id}/degradation-analyses/{row['analysis_id']}", "run": f"/api/projects/{project_id}/runs/{row['run_id']}"}} for row in store().degradation_analyses(project_id=project_id)])

    # -- 1:N identification (QS-X60) ------------------------------------------------

    searches: dict[str, IdentificationSearchJob] = {}

    def search_summary(row: dict[str, Any]) -> dict[str, Any]:
        summary = json.loads(row["summary_json"])
        project_id = row["project_id"]
        summary["links"] = {"search": f"/api/projects/{project_id}/identification-searches/{row['search_id']}", "run": f"/api/projects/{project_id}/runs/{row['run_id']}", "candidates": f"/api/projects/{project_id}/identification-searches/{row['search_id']}/candidates", "protocol": f"/api/projects/{project_id}/identification-protocols/{row['protocol_id']}"}
        summary["created_at"] = row["created_at"]
        return summary

    @app.post("/api/projects/{project_id}/datasets/{dataset_id}/protocols/freeze-lfw-identification", status_code=201)
    def freeze_identification_protocol(project_id: str, dataset_id: str, request: IdentificationFreezeRequest) -> JSONResponse:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        dataset = store().dataset(dataset_id)
        if dataset is None or dataset.project_id != project_id:
            raise HTTPException(status_code=404, detail="dataset is not recorded in this project")
        try:
            record = freeze_lfw_identification(store(), project=project, dataset=dataset, selection=request.selection, rank_depth=request.rank_depth, repository_root=settings.repository_root, actor=projects().actor)
        except IdentificationProtocolError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        record_action(project_id, AuditEventType.CONFIGURATION_CHANGED, "IdentificationProtocol", record["protocol_id"], {"selection": request.selection, "rank_depth": request.rank_depth, "gallery_count": record["gallery_count"], "mated_probe_count": record["mated_probe_count"], "nonmated_probe_count": record["nonmated_probe_count"], "manifest_sha256": record["manifest_sha256"]})
        return JSONResponse(status_code=201, content=record)

    @app.get("/api/projects/{project_id}/identification-protocols/{protocol_id}")
    def identification_protocol_detail(project_id: str, protocol_id: str) -> JSONResponse:
        record = store().identification_protocol(protocol_id)
        if record is None or record["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="identification protocol is not recorded in this project")
        return JSONResponse(content={**record, "entries": store().identification_entries(protocol_id)})

    @app.get("/api/projects/{project_id}/identification-protocols")
    def project_identification_protocols(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(content=store().identification_protocols(project_id=project_id))

    @app.post("/api/projects/{project_id}/identification-protocols/{protocol_id}/search", status_code=202)
    def start_identification_search(project_id: str, protocol_id: str, request: IdentificationSearchRequest) -> JSONResponse:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        protocol = store().identification_protocol(protocol_id)
        if protocol is None or protocol["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="identification protocol is not recorded in this project")
        try:
            matcher_manager().require_ready((request.matcher_id,))
            with job_lock:
                require_project_not_purging_locked(project_id)
                job = IdentificationSearchJob(store=store(), registry=engine().registry, upstream=matcher_upstream(), repository_root=settings.repository_root, project=project, protocol=protocol, entries=store().identification_entries(protocol_id), matcher_id=request.matcher_id, condition_id=request.condition_id, job_root=settings.data_dir / "identification", actor=projects().actor, execution_timeout_seconds=settings.native_execution_timeout_seconds, purpose_scope=request.purpose_scope, quality_job_id=request.quality_job_id, runtime_manager=matcher_manager())
                search_request = {
                    "protocol_id": protocol_id,
                    **request.model_dump(mode="json"),
                }
                request_sha256 = CanonicalJson.from_value(search_request).sha256
                operation, reservation = admit_scientific_operation(
                    operation_id=job.search_id,
                    operation_kind="IDENTIFICATION_SEARCH",
                    project_id=project_id,
                    request=search_request,
                    output_path=job.directory,
                )
                if reservation is None:
                    return JSONResponse(status_code=200, content={"search_id": job.search_id, "state": operation["state"], "error": _public_diagnostic(operation["error"]), "already_recorded": True})
                searches[job.search_id] = job
        except (IdentificationSearchError, ContractValidationError, MatcherLifecycleError) as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error

        def _run() -> None:
            try:
                store().start_operation(job.search_id)
                job.run(request_sha256=request_sha256)
                store().finish_operation(job.search_id, state="COMPLETED")
            except Exception as error:  # noqa: BLE001
                detail = f"{type(error).__name__}: {error}"
                job.progress = {"phase": "failed", "detail": detail}
                store().finish_operation(job.search_id, state="FAILED", error=detail)

        reservation.submit(_run)
        record_action(project_id, AuditEventType.RUN_STARTED, "IdentificationSearch", job.search_id, {"protocol_id": protocol_id, "matcher_id": request.matcher_id, "condition_id": request.condition_id})
        return JSONResponse(status_code=202, content={"search_id": job.search_id, "state": "RUNNING", "progress": job.progress})

    @app.get("/api/projects/{project_id}/identification-searches/{search_id}")
    def identification_search_status(project_id: str, search_id: str) -> JSONResponse:
        row = store().identification_search(search_id)
        operation = store().operation(search_id)
        if row is not None and row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="identification search is not recorded in this project")
        if row is None and (operation is None or operation["project_id"] != project_id or operation["operation_kind"] != "IDENTIFICATION_SEARCH"):
            raise HTTPException(status_code=404, detail="identification search is not recorded in this project")
        with job_lock:
            live = searches.get(search_id)
        if row is None:
            if live is None:
                return JSONResponse(content={"search_id": search_id, "state": operation["state"], "progress": {"phase": operation["state"].lower(), "detail": _public_diagnostic(operation["error"]) or ""}, "error": _public_diagnostic(operation["error"])})
            return JSONResponse(content={"search_id": search_id, "state": "FAILED" if live.progress["phase"] == "failed" else "RUNNING", "progress": live.progress, "error": _public_diagnostic(live.progress["detail"]) if live.progress["phase"] == "failed" else None})
        return JSONResponse(content={"search_id": search_id, "state": "COMPLETED", "run_id": row["run_id"], "summary": search_summary(row), "metrics": json.loads(row["metrics_json"]), "metrics_sha256": row["metrics_sha256"], "candidates_sha256": row["candidates_sha256"]})

    @app.get("/api/projects/{project_id}/identification-searches")
    def project_identification_searches(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(content=[search_summary(row) for row in store().identification_searches(project_id=project_id)])

    @app.get("/api/projects/{project_id}/identification-searches/{search_id}/candidates")
    def identification_candidates(project_id: str, search_id: str, probe_kind: str | None = Query(default=None), probe_sample_id: str | None = Query(default=None), rank_le: int | None = Query(default=None, ge=1), limit: int = Query(default=200, ge=1, le=100_000), offset: int = Query(default=0, ge=0)) -> JSONResponse:
        import duckdb

        row = store().identification_search(search_id)
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="identification search is not recorded in this project")
        parquet = row["candidates_parquet"].replace("'", "''")
        clauses, params = ["1=1"], []
        if probe_kind is not None:
            clauses.append("probe_kind = ?"); params.append(probe_kind)
        if probe_sample_id is not None:
            clauses.append("probe_sample_id = ?"); params.append(probe_sample_id)
        if rank_le is not None:
            clauses.append("rank <= ?"); params.append(rank_le)
        where = " AND ".join(clauses)
        connection = duckdb.connect()
        try:
            total = int(connection.execute(f"SELECT COUNT(*) FROM read_parquet('{parquet}') WHERE {where}", params).fetchone()[0])
            cursor = connection.execute(f"SELECT * FROM read_parquet('{parquet}') WHERE {where} ORDER BY probe_sample_id, rank LIMIT ? OFFSET ?", [*params, int(limit), int(offset)])
            names = [item[0] for item in cursor.description]
            rows = [dict(zip(names, values)) for values in cursor.fetchall()]
        finally:
            connection.close()
        for item in rows:
            item["links"] = {
                "probe_sample": f"/api/projects/{project_id}/samples/{item['probe_sample_id']}",
                "gallery_sample": f"/api/projects/{project_id}/samples/{item['gallery_sample_id']}",
                "run": f"/api/projects/{project_id}/runs/{row['run_id']}",
            }
        return JSONResponse(content={"search_id": search_id, "run_id": row["run_id"], "total": total, "offset": offset, "rows": rows})

    @app.get("/api/projects/{project_id}/matrices/{matrix_id}")
    def matrix_status(project_id: str, matrix_id: str) -> JSONResponse:
        rows = store().matcher_leaves(matrix_id=matrix_id)
        operation = store().operation(matrix_id)
        if operation is not None:
            operation = (
                store().latest_operation_attempt(str(operation["request_id"]))
                or operation
            )
        if rows and any(row["project_id"] != project_id for row in rows):
            raise HTTPException(status_code=404, detail="matrix is not recorded in this project")
        if operation is not None and operation["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="matrix is not recorded in this project")
        with job_lock:
            job = matrices.get(matrix_id)
        if job is None and not rows and operation is None:
            raise HTTPException(status_code=404, detail="matrix is not recorded in this project")
        if job is not None:
            expected_leaves = job.expected_leaf_count
        elif rows:
            expected_leaves = int(rows[0]["expected_leaf_count"])
        else:
            expected_leaves = int(json.loads(operation["request_json"])["expected_leaf_count"])
        persisted_state = operation["state"] if operation is not None else ("COMPLETED" if len(rows) == expected_leaves else "FAILED")
        persisted_progress = {
            "phase": persisted_state.lower(),
            "detail": _public_diagnostic(operation["error"])
            if operation is not None and operation["error"]
            else f"{len(rows)}/{expected_leaves} leaves recorded",
        }
        live_state = "RUNNING" if job is not None and job.progress["phase"] not in {"completed", "failed"} else persisted_state
        return JSONResponse(content={"matrix_id": matrix_id, "state": live_state, "error": _public_diagnostic(operation["error"]) if operation is not None else None, "expected_leaves": expected_leaves, "progress": job.progress if job else persisted_progress, "leaves": [leaf_summary(row) for row in rows]})

    @app.get("/api/projects/{project_id}/matcher-leaves")
    def project_matcher_leaves(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(content=[leaf_summary(row) for row in store().matcher_leaves(project_id=project_id)])

    @app.get("/api/projects/{project_id}/matcher-runs/{run_id}/metrics")
    def matcher_run_metrics(project_id: str, run_id: str) -> JSONResponse:
        row = store().matcher_leaf(run_id)
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="matcher run is not recorded in this project")
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
            project_id = row["project_id"]
            item["links"] = {
                "left_sample": f"/api/projects/{project_id}/samples/{item['left_sample_id']}",
                "right_sample": f"/api/projects/{project_id}/samples/{item['right_sample_id']}",
                "left_image": f"/api/projects/{project_id}/samples/{item['left_sample_id']}/image",
                "right_image": f"/api/projects/{project_id}/samples/{item['right_sample_id']}/image",
                "run": f"/api/projects/{project_id}/runs/{row['run_id']}",
            }
        return total, rows

    @app.get("/api/projects/{project_id}/matcher-runs/{run_id}/comparisons")
    def matcher_run_comparisons(
        project_id: str,
        run_id: str,
        label: str | None = Query(default=None),
        failed_only: bool = Query(default=False),
        min_score: float | None = Query(default=None, allow_inf_nan=ANALYSIS_PARAMETER_CONTRACTS["matcher_threshold"]["allow_inf_nan"]),
        max_score: float | None = Query(default=None, allow_inf_nan=ANALYSIS_PARAMETER_CONTRACTS["matcher_threshold"]["allow_inf_nan"]),
        limit: int = Query(default=100, ge=1, le=10_000),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        row = store().matcher_leaf(run_id)
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="matcher run is not recorded in this project")
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

    @app.get("/api/projects/{project_id}/matcher-runs/{run_id}/errors")
    def matcher_run_errors(
        project_id: str,
        run_id: str,
        threshold: float | None = Query(default=None, allow_inf_nan=ANALYSIS_PARAMETER_CONTRACTS["matcher_threshold"]["allow_inf_nan"]),
        policy: Literal["EXCLUDE_FAILED_TRIALS", "FAILURES_AS_REJECT"] = Query(default="EXCLUDE_FAILED_TRIALS"),
        limit: int = Query(default=200, ge=1, le=10_000),
    ) -> JSONResponse:
        """False matches and false non-matches at a threshold (default: the recorded EER threshold)."""

        row = store().matcher_leaf(run_id)
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="matcher run is not recorded in this project")
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

    @app.get("/api/projects/{project_id}/matcher-runs/{run_id}/erc")
    def matcher_run_erc(
        project_id: str,
        run_id: str,
        quality_job_id: str = Query(min_length=1),
        policy: str = Query(default="pair.quality.min"),
        target_fmr: float = Query(
            default=0.001,
            gt=ANALYSIS_PARAMETER_CONTRACTS["target_fmr"]["minimum"],
            lt=ANALYSIS_PARAMETER_CONTRACTS["target_fmr"]["maximum"],
            allow_inf_nan=ANALYSIS_PARAMETER_CONTRACTS["target_fmr"]["allow_inf_nan"],
        ),
        threshold: float | None = Query(default=None, allow_inf_nan=ANALYSIS_PARAMETER_CONTRACTS["matcher_threshold"]["allow_inf_nan"]),
    ) -> JSONResponse:
        """ERC of one matcher Run against the UnifiedQualityScore of a recorded quality job."""

        import numpy as np
        import pyarrow.parquet as pq

        from ..metrics.utility import ErcInputs, PairQualityPolicy, compute_erc

        leaf = store().matcher_leaf(run_id)
        if leaf is None or leaf["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="matcher run is not recorded in this project")
        job = store().job(quality_job_id)
        if job is None or job["project_id"] != project_id or not job["parquet_path"]:
            raise HTTPException(status_code=404, detail="quality job is not recorded in this project")
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
        project_id = row["project_id"]
        summary["links"] = {"analysis": f"/api/projects/{project_id}/utility-analyses/{row['analysis_id']}", "run": f"/api/projects/{project_id}/runs/{row['run_id']}", "pairs": f"/api/projects/{project_id}/utility-analyses/{row['analysis_id']}/pairs", "matcher_run": f"/api/projects/{project_id}/runs/{row['matcher_run_id']}", "quality_job": f"/api/projects/{project_id}/jobs/{row['quality_job_id']}"}
        summary["created_at"] = row["created_at"]
        return summary

    @app.post("/api/projects/{project_id}/utility-analyses", status_code=202)
    def start_utility_analysis(project_id: str, request: UtilityAnalysisRequest) -> JSONResponse:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        try:
            job = UtilityAnalysisJob(store=store(), repository_root=settings.repository_root, project=project, actor=projects().actor, request=UtilityRequest(**request.model_dump()), job_root=settings.data_dir / "analyses")
            request_sha256 = CanonicalJson.from_value(
                request.model_dump(mode="json")
            ).sha256
        except UtilityAnalysisError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        existing = store().utility_analysis(job.analysis_id)
        if existing is not None:
            return JSONResponse(status_code=200, content={"analysis_id": job.analysis_id, "state": "COMPLETED", "already_recorded": True, "summary": analysis_summary(existing)})
        recovered_operation = store().reconcile_prepared_operation(job.analysis_id)
        if recovered_operation is not None and recovered_operation["state"] == "COMPLETED":
            existing = store().utility_analysis(job.analysis_id)
            if existing is None:
                raise HTTPException(status_code=409, detail="prepared utility analysis could not be recorded atomically")
            with job_lock:
                analyses.pop(job.analysis_id, None)
            return JSONResponse(status_code=200, content={"analysis_id": job.analysis_id, "state": "COMPLETED", "already_recorded": True, "summary": analysis_summary(existing)})
        with job_lock:
            require_project_not_purging_locked(project_id)
            if job.analysis_id in analyses:
                return JSONResponse(status_code=202, content={"analysis_id": job.analysis_id, "state": analyses[job.analysis_id]["state"]})
            operation, reservation = admit_scientific_operation(
                operation_id=job.analysis_id,
                operation_kind="UTILITY_ANALYSIS",
                project_id=project_id,
                request=request.model_dump(mode="json"),
                output_path=job.directory,
            )
            if reservation is None:
                return JSONResponse(status_code=200, content={"analysis_id": job.analysis_id, "state": operation["state"], "error": _public_diagnostic(operation["error"]), "already_recorded": True})
            analyses[job.analysis_id] = {"state": "QUEUED", "error": None, "project_id": project_id}

        def _run() -> None:
            try:
                store().start_operation(job.analysis_id)
                with job_lock:
                    analyses[job.analysis_id] = {"state": "RUNNING", "error": None, "project_id": project_id}
                job.run(request_sha256=request_sha256)
                store().finish_operation(job.analysis_id, state="COMPLETED")
                with job_lock:
                    analyses[job.analysis_id] = {"state": "COMPLETED", "error": None, "project_id": project_id}
            except Exception as error:  # noqa: BLE001 - surfaced through the status endpoint
                detail = f"{type(error).__name__}: {error}"
                store().finish_operation(job.analysis_id, state="FAILED", error=detail)
                with job_lock:
                    analyses[job.analysis_id] = {"state": "FAILED", "error": detail, "project_id": project_id}

        reservation.submit(_run)
        record_action(project_id, AuditEventType.RUN_STARTED, "UtilityAnalysis", job.analysis_id, {"matcher_run_id": request.matcher_run_id, "quality_job_id": request.quality_job_id, "quality_measure": request.quality_measure, "pair_quality_policy": request.pair_quality_policy})
        return JSONResponse(status_code=202, content={"analysis_id": job.analysis_id, "state": "RUNNING"})

    @app.get("/api/projects/{project_id}/utility-analyses")
    def project_utility_analyses(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(content=[analysis_summary(row) for row in store().utility_analyses(project_id=project_id)])

    @app.get("/api/projects/{project_id}/utility-analyses/{analysis_id}")
    def utility_analysis(project_id: str, analysis_id: str, include: Literal["summary", "full"] = Query(default="full")) -> JSONResponse:
        row = store().utility_analysis(analysis_id)
        operation = store().operation(analysis_id)
        if row is not None and row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="utility analysis is not recorded in this project")
        if row is None and (operation is None or operation["project_id"] != project_id or operation["operation_kind"] != "UTILITY_ANALYSIS"):
            raise HTTPException(status_code=404, detail="utility analysis is not recorded in this project")
        with job_lock:
            live = analyses.get(analysis_id)
        if row is None:
            if live is None:
                return JSONResponse(content={"analysis_id": analysis_id, "state": operation["state"], "error": _public_diagnostic(operation["error"])})
            if live["project_id"] != project_id:
                raise HTTPException(status_code=404, detail="utility analysis is not recorded in this project")
            return JSONResponse(content={"analysis_id": analysis_id, "state": live["state"], "error": _public_diagnostic(live["error"])})
        content: dict[str, Any] = {"analysis_id": analysis_id, "state": "COMPLETED", "error": None, "summary": analysis_summary(row)}
        if include == "full":
            directory = Path(row["directory"])
            content["utility"] = json.loads((directory / "utility.json").read_text(encoding="utf-8"))
            content["policy"] = json.loads((directory / "policy.json").read_text(encoding="utf-8"))
            content["artifact_sha256"] = {"utility.json": row["utility_sha256"], "policy.json": row["policy_sha256"], "pair_quality.parquet": row["pair_table_sha256"]}
        return JSONResponse(content=content)

    @app.get("/api/projects/{project_id}/utility-analyses/{analysis_id}/pairs")
    def utility_analysis_pairs(
        project_id: str,
        analysis_id: str,
        label: str | None = Query(default=None),
        retained: bool | None = Query(default=None),
        decision_error: bool | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=10_000),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        import duckdb

        row = store().utility_analysis(analysis_id)
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="utility analysis is not recorded in this project")
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
            item["links"] = {
                "left_sample": f"/api/projects/{project_id}/samples/{item['left_sample_id']}",
                "right_sample": f"/api/projects/{project_id}/samples/{item['right_sample_id']}",
                "left_image": f"/api/projects/{project_id}/samples/{item['left_sample_id']}/image",
                "right_image": f"/api/projects/{project_id}/samples/{item['right_sample_id']}/image",
                "left_quality": f"/api/projects/{project_id}/samples/{item['left_sample_id']}/quality",
                "right_quality": f"/api/projects/{project_id}/samples/{item['right_sample_id']}/quality",
                "run": f"/api/projects/{project_id}/runs/{row['run_id']}",
            }
        return JSONResponse(content={"analysis_id": analysis_id, "run_id": row["run_id"], "total": total, "offset": offset, "rows": rows})

    # -- cross-matcher contrasts and the failure workbench (QS-E40–QS-E42) --------

    cross_jobs: dict[str, dict[str, Any]] = {}

    @app.get("/api/projects/{project_id}/matrices/{matrix_id}/sets")
    def matrix_sets(project_id: str, matrix_id: str) -> JSONResponse:
        rows = store().matcher_leaves(matrix_id=matrix_id)
        if not rows or any(row["project_id"] != project_id for row in rows):
            raise HTTPException(status_code=404, detail="matrix is not recorded in this project")
        sets = matcher_sets(rows)
        registry = engine().registry
        sets["matcher_rights"] = {row["matcher_id"]: registry.raw_resource(row["matcher_id"])["state"] for row in rows}
        return JSONResponse(content=sets)

    def cross_summary(row: dict[str, Any]) -> dict[str, Any]:
        summary = json.loads(row["summary_json"])
        project_id = row["project_id"]
        summary["links"] = {"analysis": f"/api/projects/{project_id}/cross-matcher/{row['analysis_id']}", "run": f"/api/projects/{project_id}/runs/{row['run_id']}", "pairs": f"/api/projects/{project_id}/cross-matcher/{row['analysis_id']}/pairs", "matrix": f"/api/projects/{project_id}/matrices/{row['matrix_id']}", "quality_job": f"/api/projects/{project_id}/jobs/{row['quality_job_id']}"}
        summary["created_at"] = row["created_at"]
        return summary

    @app.post("/api/projects/{project_id}/cross-matcher", status_code=202)
    def start_cross_matcher(project_id: str, request: CrossMatcherRequestBody) -> JSONResponse:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        try:
            job = CrossMatcherJob(store=store(), registry=engine().registry, repository_root=settings.repository_root, project=project, actor=projects().actor, request=CrossMatcherRequest(**request.model_dump()), job_root=settings.data_dir / "analyses")
            request_sha256 = CanonicalJson.from_value(
                request.model_dump(mode="json")
            ).sha256
        except CrossMatcherError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        existing = store().cross_matcher_analysis(job.analysis_id)
        if existing is not None:
            return JSONResponse(status_code=200, content={"analysis_id": job.analysis_id, "state": "COMPLETED", "already_recorded": True, "summary": cross_summary(existing)})
        recovered_operation = store().reconcile_prepared_operation(job.analysis_id)
        if recovered_operation is not None and recovered_operation["state"] == "COMPLETED":
            existing = store().cross_matcher_analysis(job.analysis_id)
            if existing is None:
                raise HTTPException(status_code=409, detail="prepared cross-matcher analysis could not be recorded atomically")
            with job_lock:
                cross_jobs.pop(job.analysis_id, None)
            return JSONResponse(status_code=200, content={"analysis_id": job.analysis_id, "state": "COMPLETED", "already_recorded": True, "summary": cross_summary(existing)})
        with job_lock:
            require_project_not_purging_locked(project_id)
            if job.analysis_id in cross_jobs:
                return JSONResponse(status_code=202, content={"analysis_id": job.analysis_id, "state": cross_jobs[job.analysis_id]["state"]})
            operation, reservation = admit_scientific_operation(
                operation_id=job.analysis_id,
                operation_kind="CROSS_MATCHER_ANALYSIS",
                project_id=project_id,
                request=request.model_dump(mode="json"),
                output_path=job.directory,
            )
            if reservation is None:
                return JSONResponse(status_code=200, content={"analysis_id": job.analysis_id, "state": operation["state"], "error": _public_diagnostic(operation["error"]), "already_recorded": True})
            cross_jobs[job.analysis_id] = {"state": "QUEUED", "error": None, "project_id": project_id}

        def _run() -> None:
            try:
                store().start_operation(job.analysis_id)
                with job_lock:
                    cross_jobs[job.analysis_id] = {"state": "RUNNING", "error": None, "project_id": project_id}
                job.run(request_sha256=request_sha256)
                store().finish_operation(job.analysis_id, state="COMPLETED")
                with job_lock:
                    cross_jobs[job.analysis_id] = {"state": "COMPLETED", "error": None, "project_id": project_id}
            except Exception as error:  # noqa: BLE001
                detail = f"{type(error).__name__}: {error}"
                store().finish_operation(job.analysis_id, state="FAILED", error=detail)
                with job_lock:
                    cross_jobs[job.analysis_id] = {"state": "FAILED", "error": detail, "project_id": project_id}

        reservation.submit(_run)
        record_action(project_id, AuditEventType.RUN_STARTED, "CrossMatcherAnalysis", job.analysis_id, {"matrix_id": request.matrix_id, "quality_job_id": request.quality_job_id})
        return JSONResponse(status_code=202, content={"analysis_id": job.analysis_id, "state": "RUNNING"})

    @app.get("/api/projects/{project_id}/cross-matcher")
    def project_cross_matcher(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(content=[cross_summary(row) for row in store().cross_matcher_analyses(project_id=project_id)])

    @app.get("/api/projects/{project_id}/cross-matcher/{analysis_id}")
    def cross_matcher_analysis(project_id: str, analysis_id: str, include: Literal["summary", "full"] = Query(default="full")) -> JSONResponse:
        row = store().cross_matcher_analysis(analysis_id)
        operation = store().operation(analysis_id)
        if row is not None and row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="cross-matcher analysis is not recorded in this project")
        if row is None and (operation is None or operation["project_id"] != project_id or operation["operation_kind"] != "CROSS_MATCHER_ANALYSIS"):
            raise HTTPException(status_code=404, detail="cross-matcher analysis is not recorded in this project")
        with job_lock:
            live = cross_jobs.get(analysis_id)
        if row is None:
            if live is None:
                return JSONResponse(content={"analysis_id": analysis_id, "state": operation["state"], "error": _public_diagnostic(operation["error"])})
            if live["project_id"] != project_id:
                raise HTTPException(status_code=404, detail="cross-matcher analysis is not recorded in this project")
            return JSONResponse(content={"analysis_id": analysis_id, "state": live["state"], "error": _public_diagnostic(live["error"])})
        content: dict[str, Any] = {"analysis_id": analysis_id, "state": "COMPLETED", "error": None, "summary": cross_summary(row)}
        if include == "full":
            content["document"] = json.loads((Path(row["directory"]) / "crossmatcher.json").read_text(encoding="utf-8"))
            content["artifact_sha256"] = {"crossmatcher.json": row["document_sha256"], "contrast_pairs.parquet": row["pairs_sha256"]}
        return JSONResponse(content=content)

    @app.get("/api/projects/{project_id}/cross-matcher/{analysis_id}/pairs")
    def cross_matcher_pairs(
        project_id: str,
        analysis_id: str,
        contrast_id: str = Query(min_length=1),
        category: str | None = Query(default=None),
        label: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=10_000),
        offset: int = Query(default=0, ge=0),
    ) -> JSONResponse:
        import duckdb

        row = store().cross_matcher_analysis(analysis_id)
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="cross-matcher analysis is not recorded in this project")
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
            item["annotations"] = annotations_by_pair.get(item["pair_id"], [])
            item["links"] = {
                "left_sample": f"/api/projects/{project_id}/samples/{item['left_sample_id']}",
                "right_sample": f"/api/projects/{project_id}/samples/{item['right_sample_id']}",
                "left_image": f"/api/projects/{project_id}/samples/{item['left_sample_id']}/image",
                "right_image": f"/api/projects/{project_id}/samples/{item['right_sample_id']}/image",
                "left_quality": f"/api/projects/{project_id}/samples/{item['left_sample_id']}/quality",
                "right_quality": f"/api/projects/{project_id}/samples/{item['right_sample_id']}/quality",
                "run_a": f"/api/projects/{project_id}/runs/{contrast['a']['run_id']}",
                "run_b": f"/api/projects/{project_id}/runs/{contrast['b']['run_id']}",
                "analysis_run": f"/api/projects/{project_id}/runs/{row['run_id']}",
            }
        return JSONResponse(content={"analysis_id": analysis_id, "contrast": {"contrast_id": contrast_id, "kind": contrast["kind"], "a": contrast["a"], "b": contrast["b"], "held_fixed": contrast["held_fixed"]}, "total": total, "offset": offset, "rows": rows})

    @app.post("/api/projects/{project_id}/annotations", status_code=201)
    def create_annotation(project_id: str, request: AnnotationCreate) -> JSONResponse:
        with job_lock:
            require_project_not_purging_locked(project_id)
            active_project_mutations[project_id] = active_project_mutations.get(project_id, 0) + 1
        try:
            try:
                projects().require_writable(project_id)
            except KeyError as error:
                raise HTTPException(status_code=404, detail="project is not recorded") from error
            except ProjectLifecycleError as error:
                raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
            try:
                subject_owned = store().annotation_subject_owned_by_project(
                    project_id=project_id,
                    subject_type=request.subject_type,
                    subject_id=request.subject_id,
                )
            except ValueError as error:
                raise HTTPException(
                    status_code=422, detail="annotation subject type is not supported"
                ) from error
            if not subject_owned:
                raise HTTPException(
                    status_code=404,
                    detail="annotation subject is not recorded in this project",
                )
            note = store().record_annotation(project_id=project_id, subject_type=request.subject_type, subject_id=request.subject_id, actor_id=projects().actor, text=request.text, context=request.context)
            return JSONResponse(status_code=201, content=note)
        finally:
            with job_lock:
                remaining = active_project_mutations[project_id] - 1
                if remaining:
                    active_project_mutations[project_id] = remaining
                else:
                    del active_project_mutations[project_id]

    @app.get("/api/projects/{project_id}/annotations")
    def list_annotations(project_id: str, subject_id: str | None = Query(default=None)) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(content=store().annotations(subject_id=subject_id, project_id=project_id))

    # -- Phase 5: figures, dashboards, reports, exports, bundles, reproduction ------

    def figure_provenance_file_sha256(row: dict[str, Any], provenance: dict[str, Any]) -> str:
        recorded_sha256 = row["provenance_sha256"]
        legacy_database_sha256 = hashlib.sha256(row["provenance_json"].encode("utf-8")).hexdigest()
        if recorded_sha256 != legacy_database_sha256:
            return recorded_sha256
        original_bytes = (json.dumps(provenance, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        return hashlib.sha256(original_bytes).hexdigest()

    def figure_summary(row: dict[str, Any]) -> dict[str, Any]:
        provenance = json.loads(row["provenance_json"])
        project_id = row["project_id"]
        return {"figure_id": row["figure_id"], "project_id": project_id, "kind": row["kind"], "title": row["title"], "created_at": row["created_at"], "source_run_ids": json.loads(row["source_run_ids_json"]), "provenance_sha256": figure_provenance_file_sha256(row, provenance),
                "files": {name: f"/api/projects/{project_id}/figures/{row['figure_id']}/file?format={name}" for name in ("png", "svg", "pdf", "csv", "provenance")}, "sha256": {name: item["sha256"] for name, item in provenance["files"].items()}, "metric_definitions": provenance["metric_definitions"], "thresholds": provenance["thresholds"]}

    def require_recorded_file(path: Path, expected_sha256: str, label: str) -> str:
        if not path.is_file():
            raise HTTPException(status_code=410, detail=f"{label} file is missing")
        observed = artifact_identity(path).sha256
        if observed != expected_sha256:
            raise HTTPException(status_code=409, detail=f"{label} bytes drifted from their recorded SHA-256")
        return observed

    def open_recorded_snapshot(path: Path, expected_sha256: str, label: str) -> tuple[Any, str]:
        """Copy and verify one open inode, then serve only that immutable snapshot."""

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as error:
            raise HTTPException(status_code=410, detail=f"{label} file is missing") from error
        except OSError as error:
            raise HTTPException(status_code=409, detail=f"{label} file cannot be opened safely") from error
        source = os.fdopen(descriptor, "rb")
        snapshot = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        try:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise HTTPException(status_code=409, detail=f"{label} is not a regular file")
            digest = hashlib.sha256()
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                snapshot.write(chunk)
            after = os.fstat(source.fileno())
            if (
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise HTTPException(status_code=409, detail=f"{label} changed while it was being verified")
            observed = digest.hexdigest()
            if observed != expected_sha256:
                raise HTTPException(status_code=409, detail=f"{label} bytes drifted from their recorded SHA-256")
            snapshot.seek(0)
            return snapshot, observed
        except Exception:
            snapshot.close()
            raise
        finally:
            source.close()

    def recorded_snapshot_response(
        snapshot: Any,
        *,
        media_type: str,
        filename: str | None,
        headers: dict[str, str],
    ) -> StreamingResponse:
        snapshot.seek(0, os.SEEK_END)
        content_length = snapshot.tell()
        snapshot.seek(0)

        def body() -> Any:
            try:
                while chunk := snapshot.read(1024 * 1024):
                    yield chunk
            finally:
                snapshot.close()

        response_headers = dict(headers)
        response_headers["Content-Length"] = str(content_length)
        if filename is not None:
            response_headers["Content-Disposition"] = f"attachment; filename*=utf-8''{quote(filename)}"
        return StreamingResponse(body(), media_type=media_type, headers=response_headers)

    def recorded_download(path: Path, expected_sha256: str | None, label: str, href: str) -> dict[str, Any]:
        if not expected_sha256:
            return {
                "state": "UNANCHORED",
                "reason": f"legacy {label} has no database-anchored SHA-256; regenerate it before download",
                "href": None,
            }
        try:
            require_recorded_file(path, expected_sha256, label)
        except HTTPException as error:
            return {
                "state": "MISSING" if error.status_code == 410 else "DRIFTED",
                "reason": str(error.detail),
                "href": None,
            }
        return {"state": "VERIFIED", "reason": None, "href": href}

    def report_summary(row: dict[str, Any]) -> dict[str, Any]:
        project_id = row["project_id"]
        downloads = {
            format_name: recorded_download(
                Path(row["directory"]) / f"report.{format_name}",
                row[f"{format_name}_sha256"],
                f"report {format_name}",
                f"/api/projects/{project_id}/reports/{row['report_id']}/file?format={format_name}",
            )
            for format_name in ("html", "pdf", "json")
        }
        return {
            "report_id": row["report_id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "html_sha256": row["html_sha256"],
            "pdf_sha256": row["pdf_sha256"],
            "dataset_id": json.loads(row["spec_json"]).get("dataset_id"),
            "downloads": downloads,
            "links": {
                format_name: download["href"]
                for format_name, download in downloads.items()
                if download["href"] is not None
            },
        }

    def bundle_summary(row: dict[str, Any]) -> dict[str, Any]:
        href = f"/api/projects/{row['project_id']}/bundles/{row['bundle_id']}/file"
        download = recorded_download(Path(row["path"]), row["sha256"], "bundle", href)
        return {
            "bundle_id": row["bundle_id"],
            "project_id": row["project_id"],
            "run_id": row["run_id"],
            "created_at": row["created_at"],
            "sha256": row["sha256"],
            "download": download,
            "links": {"download": href} if download["href"] is not None else {},
        }

    @app.post("/api/projects/{project_id}/figures", status_code=201)
    def create_figure(project_id: str, request: FigureCreate) -> JSONResponse:
        try:
            projects().require_writable(project_id)
            spec = figure_module.build(store(), project_id, request.kind, request.parameters)
            record = figure_module.render(spec, settings.data_dir / "figures")
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        except figure_module.FigureError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        store().record_reporting_publication(
            "FIGURE",
            record,
            reporting_publication_audit_event(
                "FIGURE", record, actor_id=settings.actor
            ),
        )
        row = store().figure(record["figure_id"])
        return JSONResponse(status_code=201, content=figure_summary(row))

    @app.get("/api/projects/{project_id}/figures")
    def project_figures(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(content=[figure_summary(row) for row in store().figures(project_id=project_id)])

    @app.get("/api/projects/{project_id}/figures/{figure_id}")
    def figure_detail(project_id: str, figure_id: str) -> JSONResponse:
        row = store().figure(figure_id)
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="figure is not recorded in this project")
        return JSONResponse(content=figure_summary(row))

    @app.get("/api/projects/{project_id}/figures/{figure_id}/file")
    def figure_file(project_id: str, figure_id: str, format: Literal["png", "svg", "pdf", "csv", "provenance"] = Query(default="png")) -> StreamingResponse:
        row = store().figure(figure_id)
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="figure is not recorded in this project")
        provenance = json.loads(row["provenance_json"])
        if format == "provenance":
            path = Path(row["directory"]) / "figure.provenance.json"
            media = "application/json"
        else:
            path = Path(provenance["files"][format]["path"])
            media = {"png": "image/png", "svg": "image/svg+xml", "pdf": "application/pdf", "csv": "text/csv"}[format]
        if format == "provenance":
            if not path.is_file():
                raise HTTPException(status_code=410, detail="figure provenance file is missing")
            expected_sha256 = figure_provenance_file_sha256(row, provenance)
            snapshot, observed_sha256 = open_recorded_snapshot(path, expected_sha256, "figure provenance")
            try:
                observed_record = json.loads(snapshot.read().decode("utf-8"))
                snapshot.seek(0)
            except (UnicodeDecodeError, ValueError) as error:
                snapshot.close()
                raise HTTPException(status_code=409, detail=redact_private_paths(f"figure provenance is unreadable: {error}")) from error
            if observed_record != provenance:
                snapshot.close()
                raise HTTPException(status_code=409, detail="figure provenance content drifted from its recorded evidence")
        else:
            snapshot, observed_sha256 = open_recorded_snapshot(path, provenance["files"][format]["sha256"], "figure")
        return recorded_snapshot_response(snapshot, media_type=media, filename=f"{figure_id.replace(':', '_')}.{'provenance.json' if format == 'provenance' else format}", headers={"X-QualSight-Sha256": observed_sha256})

    @app.post("/api/projects/{project_id}/dashboards", status_code=201)
    def create_dashboard(project_id: str, request: DashboardCreate) -> JSONResponse:
        try:
            projects().require_writable(project_id)
            record = save_dashboard(store(), project_id=project_id, name=request.name, template=request.template, panels=request.panels, columns=request.columns, actor=projects().actor, supersedes=request.supersedes)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        except DashboardError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        record_action(project_id, AuditEventType.CONFIGURATION_CHANGED, "Dashboard", record["dashboard_id"], {"name": request.name, "template": request.template, "version": record["version"], "layout_sha256": record["layout_sha256"], "panel_run_ids": record["panel_run_ids"]})
        return JSONResponse(status_code=201, content={k: v for k, v in record.items() if k != "layout_text"})

    @app.get("/api/projects/{project_id}/dashboards")
    def project_dashboards(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(content=[{"dashboard_id": row["dashboard_id"], "name": row["name"], "template": row["template"], "version": row["version"], "supersedes": row["supersedes"], "created_at": row["created_at"], "layout_sha256": row["evidence_sha256"], "panels": len(json.loads(row["layout_json"])["panels"]), "panel_run_ids": json.loads(row["panel_run_ids_json"])} for row in store().dashboards(project_id=project_id)])

    @app.get("/api/projects/{project_id}/dashboards/{dashboard_id}")
    def dashboard_detail(project_id: str, dashboard_id: str) -> JSONResponse:
        try:
            return JSONResponse(content=load_dashboard(store(), project_id, dashboard_id))
        except DashboardError as error:
            raise HTTPException(status_code=404, detail=redact_private_paths(str(error))) from error

    @app.post("/api/projects/{project_id}/reports", status_code=201)
    def create_report(project_id: str, request: ReportCreate) -> JSONResponse:
        try:
            projects().require_writable(project_id)
            builder = ReportBuilder(store=store(), registry=engine().registry, repository_root=settings.repository_root, project_id=project_id, spec=request.spec, actor=projects().actor, root=settings.data_dir / "reports")
            record = builder.build(record_publication=False)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        except ReportError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        store().record_reporting_publication(
            "REPORT",
            record,
            reporting_publication_audit_event(
                "REPORT", record, actor_id=settings.actor
            ),
        )
        summary = report_summary(store().report(record["report_id"]))
        return JSONResponse(status_code=201, content=summary)

    @app.get("/api/projects/{project_id}/reports")
    def project_reports(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(content=[report_summary(row) for row in store().reports(project_id=project_id)])

    @app.get("/api/projects/{project_id}/reports/{report_id}/file")
    def report_file(project_id: str, report_id: str, format: Literal["html", "pdf", "json", "provenance"] = Query(default="html")) -> StreamingResponse:
        row = store().report(report_id)
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="report is not recorded in this project")
        name = {"html": "report.html", "pdf": "report.pdf", "json": "report.json", "provenance": "report.provenance.json"}[format]
        path = Path(row["directory"]) / name
        media = {"html": "text/html", "pdf": "application/pdf", "json": "application/json", "provenance": "application/json"}[format]
        expected_sha256 = {
            "html": row["html_sha256"],
            "pdf": row["pdf_sha256"],
            "json": row["json_sha256"],
            "provenance": row["provenance_sha256"],
        }[format]
        if not expected_sha256:
            raise HTTPException(status_code=409, detail=f"legacy report has no database-anchored {format} SHA-256; regenerate it before download")
        snapshot, observed_sha256 = open_recorded_snapshot(path, expected_sha256, "report provenance" if format == "provenance" else "report")
        if format == "provenance":
            try:
                provenance_record = json.loads(snapshot.read().decode("utf-8"))
                snapshot.seek(0)
            except (UnicodeDecodeError, ValueError) as error:
                snapshot.close()
                raise HTTPException(status_code=409, detail=redact_private_paths(f"report provenance is unreadable: {error}")) from error
            if provenance_record.get("report_id") != row["report_id"] or provenance_record.get("project_id") != row["project_id"]:
                snapshot.close()
                raise HTTPException(status_code=409, detail="report provenance identity drifted from its database record")
            if provenance_record.get("provenance") != json.loads(row["provenance_json"]):
                snapshot.close()
                raise HTTPException(status_code=409, detail="report provenance content drifted from its database record")
            database_file_hashes = {"html": row["html_sha256"], "pdf": row["pdf_sha256"], "json": row["json_sha256"]}
            if {key: provenance_record.get("files", {}).get(key, {}).get("sha256") for key in database_file_hashes} != database_file_hashes:
                snapshot.close()
                raise HTTPException(status_code=409, detail="report provenance file hashes drifted from their database records")
        return recorded_snapshot_response(snapshot, media_type=media, filename=f"{report_id.replace(':', '_')}-{name}", headers={"X-QualSight-Sha256": observed_sha256})

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

    @app.post("/api/projects/{project_id}/exports/{category}")
    def export_category(project_id: str, category: str, ref: str = Query(min_length=1), format: Literal["csv", "json", "parquet"] = Query(default="csv")) -> StreamingResponse:
        with admitted_mutation(project_id):
            if store().project(project_id) is None or _project_of_ref(category, ref) != project_id:
                raise HTTPException(status_code=404, detail="export source is not recorded in this project")
            try:
                record = export_table(store(), category=category, ref=ref, fmt=format, root=settings.data_dir / "exports")
            except ExportError as error:
                raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
            media = {"csv": "text/csv", "json": "application/json", "parquet": "application/vnd.apache.parquet"}[format]
            project_of_export = record["source"].get("project_id") or _project_of_ref(category, ref)
            if project_of_export != project_id:
                raise HTTPException(status_code=404, detail="export source is not recorded in this project")
            store().record_reporting_publication(
                "TABULAR_EXPORT",
                record,
                reporting_publication_audit_event(
                    "TABULAR_EXPORT", record, actor_id=settings.actor
                ),
            )
            snapshot, observed_sha256 = open_recorded_snapshot(Path(record["path"]), record["sha256"], "export")
            return recorded_snapshot_response(snapshot, media_type=media, filename=f"{category}-{ref[-12:]}.{format}", headers={"X-QualSight-Export-Id": record["export_id"], "X-QualSight-Export-Sha256": observed_sha256, "X-QualSight-Export-Rows": str(record["rows"]), "X-QualSight-Classification": record["classification"]})

    @app.post("/api/projects/{project_id}/bundles", status_code=201)
    def create_bundle(project_id: str, request: BundleCreate) -> JSONResponse:
        found = store().run(request.run_id)
        if found is None or found[0].project_id != project_id:
            raise HTTPException(status_code=404, detail="run is not recorded in this project")
        try:
            record = bundle_module.export_bundle(store(), run_id=request.run_id, root=settings.data_dir / "bundles", repository_root=settings.repository_root, record_publication=False)
        except bundle_module.BundleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        store().record_reporting_publication(
            "EXPERIMENT_BUNDLE",
            record,
            reporting_publication_audit_event(
                "EXPERIMENT_BUNDLE", record, actor_id=settings.actor
            ),
        )
        summary = bundle_summary(next(row for row in store().experiment_bundles(project_id=project_id) if row["bundle_id"] == record["bundle_id"]))
        return JSONResponse(status_code=201, content=summary)

    @app.get("/api/projects/{project_id}/bundles")
    def project_bundles(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(content=[bundle_summary(row) for row in store().experiment_bundles(project_id=project_id)])

    @app.get("/api/projects/{project_id}/bundles/{bundle_id}/file")
    def bundle_file(project_id: str, bundle_id: str) -> StreamingResponse:
        rows = [row for row in store().experiment_bundles(project_id=project_id) if row["bundle_id"] == bundle_id]
        if not rows:
            raise HTTPException(status_code=404, detail="bundle is not recorded")
        path = Path(rows[0]["path"])
        snapshot, observed_sha256 = open_recorded_snapshot(path, rows[0]["sha256"], "bundle")
        return recorded_snapshot_response(snapshot, media_type="application/zip", filename=path.name, headers={"X-QualSight-Sha256": observed_sha256})

    @app.post("/api/projects/{project_id}/reproductions", status_code=201)
    def reproduce(project_id: str, request: ReproduceRequest) -> JSONResponse:
        rows = [row for row in store().experiment_bundles(project_id=project_id) if row["bundle_id"] == request.bundle_id]
        if not rows:
            raise HTTPException(status_code=404, detail="bundle is not recorded in this project")
        bundle = rows[0]
        snapshot, _ = open_recorded_snapshot(Path(bundle["path"]), bundle["sha256"], "bundle")
        try:
            try:
                with zipfile.ZipFile(snapshot) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    experiment_record = json.loads(archive.read("experiment.yaml"))
            except (KeyError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as error:
                raise HTTPException(status_code=409, detail="recorded bundle structure is invalid") from error
            recorded_run_id = str(bundle["run_id"])
            recorded_run = store().run(recorded_run_id)
            if (
                manifest.get("run_id") != recorded_run_id
                or experiment_record.get("run", {}).get("project_id") != project_id
                or recorded_run is None
                or recorded_run[0].project_id != project_id
            ):
                raise HTTPException(status_code=409, detail="recorded bundle identity is not bound to this project")
            snapshot.seek(0)
            with admitted_mutation(project_id), tempfile.NamedTemporaryFile(
                dir=settings.data_dir,
                prefix="reproduction-snapshot-",
                suffix=".qsexp",
            ) as immutable_bundle:
                while chunk := snapshot.read(1024 * 1024):
                    immutable_bundle.write(chunk)
                immutable_bundle.flush()
                os.fsync(immutable_bundle.fileno())
                result = bundle_module.reproduce_bundle(
                    Path(immutable_bundle.name),
                    store=store(),
                    registry=engine().registry,
                    repository_root=settings.repository_root,
                    work_root=settings.data_dir / "reproductions",
                    project_service=projects(),
                    engine=engine(),
                    execution_timeout_seconds=settings.native_execution_timeout_seconds,
                )
        finally:
            snapshot.close()
        original = store().run(result.original_run_id)
        if original is None or original[0].project_id != project_id:
            raise HTTPException(status_code=409, detail="reproduction result is not bound to the bundle project")
        record_action(project_id, AuditEventType.RUN_COMPLETED, "Reproduction", result.reproduction_id, {"original_run_id": result.original_run_id, "state": result.state, "compared_values": result.compared_values, "bundle_sha256": result.bundle_sha256})
        return JSONResponse(
            status_code=201,
            content=_public_reproduction_result(asdict(result)),
        )

    @app.get("/api/projects/{project_id}/reproductions")
    def list_reproductions(project_id: str, original_run_id: str | None = Query(default=None)) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        rows = store().reproductions(original_run_id=original_run_id, project_id=project_id)
        return JSONResponse(
            content=[
                _public_reproduction_result(json.loads(row["report_json"]))
                for row in rows
            ]
        )

    @app.get("/api/projects/{project_id}/versions")
    def project_versions(project_id: str) -> JSONResponse:
        versions = store().project_versions(project_id)
        if not versions:
            raise HTTPException(status_code=404, detail="project has no recorded versions")
        return JSONResponse(
            content=[
                {
                    "version": version,
                    "project": _public_projection(
                        project.model_dump(mode="json"),
                        omit_location_keys=True,
                    ),
                }
                for version, project in versions
            ]
        )

    @app.get("/api/projects/{project_id}/audit")
    def project_audit(project_id: str) -> JSONResponse:
        events = store().audit_events_for_project(project_id)
        if not events:
            raise HTTPException(status_code=404, detail="no audit event is recorded for this project")
        return JSONResponse(
            content=[
                _public_projection(
                    event.model_dump(mode="json"), omit_location_keys=True
                )
                for event in events
            ]
        )

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
            actor_id=settings.actor,
            network_guard=guard_status(),
        )

    @app.get("/api/live")
    def live() -> JSONResponse:
        """Process liveness only; dependency checks belong to readiness."""

        return JSONResponse(content={"status": "alive"})

    @app.get("/api/version")
    def api_version() -> JSONResponse:
        return JSONResponse(
            content={
                "contract_version": API_CONTRACT_VERSION,
                "compatibility": (
                    "all /api routes in this beta implement contract version "
                    f"{API_CONTRACT_VERSION}"
                ),
            }
        )

    @app.get("/api/metrics")
    def operational_metrics() -> JSONResponse:
        with request_metrics_lock:
            requests = int(request_metrics["requests"])
            responses = dict(request_metrics["responses_by_status"])
            total_duration = float(request_metrics["total_duration_seconds"])
        return JSONResponse(
            content={
                "kind": "QUALSIGHT_LOCAL_OPERATIONAL_METRICS_V1",
                "uptime_seconds": round(time.monotonic() - process_started, 3),
                "http": {
                    "requests": requests,
                    "responses_by_status": responses,
                    "mean_duration_ms": round(total_duration * 1000 / requests, 3) if requests else None,
                },
                "scientific_work": executor.status(),
                "database": store().operational_status(),
                "reconciled_at_startup": {
                    "operations": len(state.get("reconciled_operations", [])),
                    "purges": len(state.get("reconciled_purges", [])),
                },
                "shutting_down": bool(state.get("shutting_down", False)),
            }
        )

    @app.get("/api/ready")
    def ready() -> JSONResponse:
        """Report whether durable state, runtime bytes, and work admission are usable."""

        checks: dict[str, Any] = {}
        ready_state = not bool(state.get("shutting_down", False))
        checks["shutdown"] = {"ok": ready_state}
        lease = state.get("data_directory_lease")
        checks["data_directory_lease"] = (
            lease.public_status()
            if isinstance(lease, DataDirectoryLease)
            else {
                "state": "UNAVAILABLE",
                "owner": None,
                "pid": None,
                "acquired_at": None,
                "expires_at": None,
                "release_condition": "PROCESS_SHUTDOWN",
            }
        )
        if checks["data_directory_lease"]["state"] != "HELD":
            ready_state = False
        try:
            database = store().operational_status()
            checks["database"] = {"ok": True, **database}
            if database["unresolved_purges"]:
                ready_state = False
        except Exception:  # noqa: BLE001 - dependency failure belongs in readiness
            checks["database"] = {
                "ok": False,
                "error_code": "DATABASE_UNAVAILABLE",
            }
            ready_state = False

        try:
            data_status = settings.data_dir.stat()
            file_system = os.statvfs(settings.data_dir)
            storage_ok = settings.data_dir.is_dir() and stat.S_IMODE(data_status.st_mode) == 0o700
            checks["storage"] = {
                "ok": storage_ok,
                "mode": oct(stat.S_IMODE(data_status.st_mode)),
                "available_bytes": file_system.f_bavail * file_system.f_frsize,
            }
            ready_state = ready_state and storage_ok
        except OSError:
            checks["storage"] = {
                "ok": False,
                "error_code": "STORAGE_UNAVAILABLE",
            }
            ready_state = False

        runtime_artifacts: list[dict[str, Any]] = []
        runtime_ok = True
        for role in ("BINARY", "CONFIGURATION"):
            identity = engine().runtime.artifact(role).identity
            artifact_path = Path(identity.path or "")
            try:
                artifact_status = artifact_path.stat()
                artifact_ok = (
                    artifact_path.is_file()
                    and artifact_status.st_size == identity.size_bytes
                    and sha256_file(artifact_path) == identity.sha256
                )
            except OSError:
                artifact_ok = False
            runtime_ok = runtime_ok and artifact_ok
            runtime_artifacts.append(
                {
                    "role": role,
                    "resource_id": engine().runtime.resource_id,
                    "sha256": identity.sha256,
                    "ok": artifact_ok,
                }
            )
        checks["core_runtime"] = {"ok": runtime_ok, "artifacts": runtime_artifacts}
        ready_state = ready_state and runtime_ok

        capacity = executor.status()
        checks["scientific_capacity"] = capacity
        if capacity["available"] == 0:
            ready_state = False

        guard = guard_status()
        guard_ok = not settings.network_guard or bool(guard["installed"])
        checks["network_guard"] = {"ok": guard_ok, **guard}
        ready_state = ready_state and guard_ok
        return JSONResponse(
            status_code=200 if ready_state else 503,
            content={"status": "ready" if ready_state else "not_ready", "checks": checks},
        )

    @app.get("/api/matcher-assets")
    def matcher_assets() -> JSONResponse:
        return JSONResponse(
            content=_public_projection(
                matcher_manager().catalog(), omit_location_keys=True
            )
        )

    @app.get("/api/addons")
    def addons() -> JSONResponse:
        """Closed allowlist of executable and importable beta capabilities."""

        return JSONResponse(
            content=_public_projection(
                addon_catalog(
                    engine().registry,
                    matcher_manager().catalog(),
                    settings.data_dir,
                ),
                omit_location_keys=True,
            )
        )

    @app.post("/api/dataset-assets/{addon_id}/pull")
    def pull_verification_dataset(addon_id: str) -> JSONResponse:
        with admitted_mutation(None):
            request_event = record_system_action(
                "VERIFICATION_DATASET_ACQUISITION_REQUESTED",
                addon_id,
                {},
            )
            dataset_store = verification_dataset_store()
            try:
                dataset_store.status(addon_id)
                with permit_explicit_setup_network():
                    receipt = dataset_store.acquire()
            except DatasetAcquisitionError as error:
                record_system_action(
                    "VERIFICATION_DATASET_ACQUISITION_FAILED",
                    addon_id,
                    {
                        "request_event_id": request_event["system_event_id"],
                        "reason": str(error),
                    },
                )
                raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
            try:
                status = dataset_store.status(addon_id, verify_images=True)
                event = record_system_action(
                    "VERIFICATION_DATASET_ACQUISITION_COMPLETED",
                    addon_id,
                    {
                        "request_event_id": request_event["system_event_id"],
                        "receipt_id": receipt["receipt_id"],
                        "archive_sha256": receipt["archive"]["sha256"],
                        "archive_size_bytes": receipt["archive"]["size_bytes"],
                        "protocol_state": status["runtime_state"],
                    },
                )
            except Exception as completion_error:
                try:
                    dataset_store.rollback_acquisition(receipt)
                except DatasetAcquisitionError as rollback_error:
                    raise RuntimeError(
                        "verification dataset completion evidence failed and "
                        f"acquisition rollback failed: {rollback_error}"
                    ) from completion_error
                raise
            dataset = next(
                item
                for item in dataset_addons(engine().registry, settings.data_dir)
                if item["addon_id"] == addon_id
            )
            return JSONResponse(
                content=_public_projection(
                    {"dataset": dataset, "receipt": receipt, "event": event},
                    omit_location_keys=True,
                )
            )

    @app.post(
        "/api/projects/{project_id}/dataset-assets/{addon_id}/import",
        status_code=201,
    )
    def import_verification_dataset(project_id: str, addon_id: str) -> JSONResponse:
        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        with admitted_mutation(project_id):
            importer = AcquiredVerificationDatasetImporter(
                verification_dataset_store(),
                store(),
                projects().actor,
            )
            try:
                result = importer.import_addon(project, addon_id)
            except VerificationDatasetImportError as error:
                raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        return JSONResponse(
            status_code=201,
            content={
                "dataset": dataset_out(result.dataset, result.statistics_sha256).model_dump(
                    mode="json"
                ),
                "protocol": _public_projection(
                    result.protocol.model_dump(mode="json"),
                    omit_location_keys=True,
                ),
                "samples": result.samples,
                "pairs": result.pairs,
            },
        )

    @app.get("/api/preprocessing-profiles")
    def matcher_preprocessing_profiles() -> JSONResponse:
        return JSONResponse(
            content=_public_projection(
                preprocessing_profiles(engine().registry),
                omit_location_keys=True,
            )
        )

    def capture_architecture_sources(matcher_id: str) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        for item in matcher_manager().status(matcher_id)["architecture_sources"]:
            path = Path(item["path"])
            if not os.path.lexists(path):
                snapshots.append({"path": path, "bytes": None, "mode": None})
                continue
            current = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(current.st_mode):
                raise HTTPException(
                    status_code=409,
                    detail="architecture source target is not a regular file",
                )
            snapshots.append({"path": path, "bytes": path.read_bytes(), "mode": stat.S_IMODE(current.st_mode)})
        return snapshots

    def restore_architecture_sources(snapshots: list[dict[str, Any]]) -> None:
        for snapshot in snapshots:
            path = snapshot["path"]
            before = snapshot["bytes"]
            if before is None:
                if os.path.lexists(path):
                    current = path.lstat()
                    if path.is_symlink() or not stat.S_ISREG(current.st_mode):
                        raise RuntimeError(f"refusing to remove a non-regular architecture source during rollback: {path}")
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(mode="w+b", prefix=f".{path.name}.", suffix=".rollback", dir=path.parent, delete=False) as handle:
                        temporary = Path(handle.name)
                        os.fchmod(handle.fileno(), snapshot["mode"])
                        handle.write(before)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, path)
                    temporary = None
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            if path.parent.is_dir():
                directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)

    def rollback_checkpoint_acquisition(
        receipt: dict[str, Any],
        *,
        managed_existed: bool,
        receipt_paths_before: set[Path],
    ) -> None:
        target = Path(receipt["target_path"])
        receipt_path = Path(receipt["path"])
        if not managed_existed and receipt["action"] != "REUSED_VERIFIED_MANAGED":
            if target.is_symlink() or not target.is_file():
                raise RuntimeError(f"new managed checkpoint cannot be safely rolled back: {target}")
            identity = artifact_identity(target)
            if identity.sha256 != receipt["artifact"]["sha256"] or identity.size_bytes != receipt["artifact"]["size_bytes"]:
                raise RuntimeError("new managed checkpoint changed before rollback")
            target.unlink()
        if receipt_path not in receipt_paths_before:
            if receipt_path.is_symlink() or not receipt_path.is_file():
                raise RuntimeError(f"new acquisition receipt cannot be safely rolled back: {receipt_path}")
            persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
            if persisted != {key: value for key, value in receipt.items() if key != "path"}:
                raise RuntimeError("new acquisition receipt changed before rollback")
            receipt_path.unlink()
        for directory_path in {target.parent, receipt_path.parent}:
            if directory_path.is_dir():
                directory = os.open(directory_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)

    @app.post("/api/matcher-assets/{matcher_id}/pull-sources")
    def pull_matcher_sources(matcher_id: str) -> JSONResponse:
        with matcher_control_lock, matcher_manager().exclusive_control():
            request_event = record_system_action("MATCHER_ARCHITECTURE_SOURCES_PULL_REQUESTED", matcher_id, {})
            snapshots = capture_architecture_sources(matcher_id)
            try:
                with permit_explicit_setup_network():
                    status = matcher_manager().pull_architecture_sources(matcher_id)
            except MatcherLifecycleError as error:
                record_system_action("MATCHER_ARCHITECTURE_SOURCES_PULL_FAILED", matcher_id, {"request_event_id": request_event["system_event_id"], "reason": str(error)})
                raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
            try:
                event = record_system_action(
                    "MATCHER_ARCHITECTURE_SOURCES_PULLED",
                    matcher_id,
                    {"request_event_id": request_event["system_event_id"], "source_states": {item["key"]: item["state"] for item in status["architecture_sources"]}},
                )
            except Exception as event_error:
                try:
                    restore_architecture_sources(snapshots)
                except Exception as rollback_error:
                    raise RuntimeError(
                        f"source-pull completion event failed and source rollback failed: {rollback_error}"
                    ) from event_error
                raise
            return JSONResponse(
                content=_public_projection(
                    {"asset": status, "event": event},
                    omit_location_keys=True,
                )
            )

    @app.post("/api/matcher-assets/{matcher_id}/pull-checkpoint")
    def pull_matcher_checkpoint(matcher_id: str) -> JSONResponse:
        with matcher_control_lock, matcher_manager().exclusive_control():
            request_event = record_system_action(
                "MATCHER_CHECKPOINT_ACQUISITION_REQUESTED",
                matcher_id,
                {},
            )
            acquisition_before = matcher_manager().status(matcher_id)["checkpoint_acquisition"]
            managed_path = Path(acquisition_before["managed_path"])
            managed_existed = os.path.lexists(managed_path)
            receipt_root = settings.data_dir / "acquisition-receipts" / matcher_id
            receipt_paths_before = set(receipt_root.glob("*.json")) if receipt_root.is_dir() else set()
            try:
                with permit_explicit_setup_network():
                    result = matcher_manager().acquire_checkpoint(matcher_id)
            except MatcherLifecycleError as error:
                record_system_action(
                    "MATCHER_CHECKPOINT_ACQUISITION_FAILED",
                    matcher_id,
                    {
                        "request_event_id": request_event["system_event_id"],
                        "reason": str(error),
                    },
                )
                raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
            receipt = result["receipt"]
            try:
                event = record_system_action(
                    "MATCHER_CHECKPOINT_ACQUISITION_COMPLETED",
                    matcher_id,
                    {
                        "request_event_id": request_event["system_event_id"],
                        "action": receipt["action"],
                        "checkpoint_sha256": receipt["artifact"]["sha256"],
                        "checkpoint_size_bytes": receipt["artifact"]["size_bytes"],
                        "receipt_id": receipt["receipt_id"],
                        "receipt_evidence_sha256": receipt["evidence_sha256"],
                    },
                )
            except Exception as event_error:
                try:
                    rollback_checkpoint_acquisition(
                        receipt,
                        managed_existed=managed_existed,
                        receipt_paths_before=receipt_paths_before,
                    )
                except Exception as rollback_error:
                    raise RuntimeError(
                        f"checkpoint completion event failed and acquisition rollback failed: {rollback_error}"
                    ) from event_error
                raise
            return JSONResponse(
                content=_public_projection(
                    {"asset": result["asset"], "receipt": receipt, "event": event},
                    omit_location_keys=True,
                )
            )

    @app.post("/api/matcher-assets/{matcher_id}/load")
    def load_matcher_asset(matcher_id: str) -> JSONResponse:
        with matcher_control_lock, matcher_manager().exclusive_control():
            request_event = record_system_action("MATCHER_LOAD_REQUESTED", matcher_id, {})
            before = matcher_manager().status(matcher_id)
            try:
                status = matcher_manager().load(matcher_id)
            except MatcherLifecycleError as error:
                record_system_action("MATCHER_LOAD_FAILED", matcher_id, {"request_event_id": request_event["system_event_id"], "reason": str(error)})
                raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
            try:
                event = record_system_action(
                    "MATCHER_LOADED",
                    matcher_id,
                    {"request_event_id": request_event["system_event_id"], "device": status["runtime"]["device"], "parameter_count": status["runtime"]["parameter_count"], "checkpoint_sha256": status["checkpoint"]["registered_sha256"]},
                )
            except Exception as event_error:
                if before["runtime"]["state"] == "UNLOADED":
                    try:
                        matcher_manager().unload(matcher_id)
                    except Exception as rollback_error:
                        raise RuntimeError(
                            f"matcher load completion event failed and runtime rollback failed: {rollback_error}"
                        ) from event_error
                raise
            return JSONResponse(
                content=_public_projection(
                    {"asset": status, "event": event},
                    omit_location_keys=True,
                )
            )

    @app.post("/api/matcher-assets/{matcher_id}/unload")
    def unload_matcher_asset(matcher_id: str) -> JSONResponse:
        with matcher_control_lock, matcher_manager().exclusive_control():
            request_event = record_system_action("MATCHER_UNLOAD_REQUESTED", matcher_id, {})
            before = matcher_manager().status(matcher_id)
            try:
                status = matcher_manager().unload(matcher_id)
            except MatcherLifecycleError as error:
                record_system_action("MATCHER_UNLOAD_FAILED", matcher_id, {"request_event_id": request_event["system_event_id"], "reason": str(error)})
                raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
            try:
                event = record_system_action("MATCHER_UNLOADED", matcher_id, {"request_event_id": request_event["system_event_id"], "runtime_state": status["runtime"]["state"]})
            except Exception as event_error:
                if before["runtime"]["state"] == "LOADED":
                    try:
                        matcher_manager().load(matcher_id)
                    except Exception as rollback_error:
                        raise RuntimeError(
                            f"matcher unload completion event failed and runtime rollback failed: {rollback_error}"
                        ) from event_error
                raise
            return JSONResponse(
                content=_public_projection(
                    {"asset": status, "event": event},
                    omit_location_keys=True,
                )
            )

    @app.get("/api/system-events")
    def system_events(limit: int = Query(default=100, ge=1, le=1000)) -> JSONResponse:
        return JSONResponse(
            content=[
                _public_system_event(record)
                for record in store().system_events(limit=limit)
            ]
        )

    @app.get("/api/projects/{project_id}/embeddings")
    def project_embeddings(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        import numpy as np

        items: list[dict[str, Any]] = []
        for leaf in store().matcher_leaves(project_id=project_id):
            found = store().run(leaf["run_id"])
            if found is None:
                continue
            artifact = next((item for item in found[0].artifacts if item.uri.endswith("embeddings.npy")), None)
            if artifact is None:
                continue
            path = Path(artifact.uri)
            if not path.is_file():
                integrity = "MISSING"
                integrity_reason = "recorded embedding file is missing"
                observed_sha256 = None
                rows = dimension = None
                size_bytes = None
            else:
                observed = artifact_identity(path)
                observed_sha256 = observed.sha256
                integrity = "VERIFIED" if observed.sha256 == artifact.sha256 and observed.size_bytes == artifact.size_bytes else "DRIFTED"
                integrity_reason = None if integrity == "VERIFIED" else "embedding bytes differ from the immutable Run artifact record"
                size_bytes = observed.size_bytes
                rows = dimension = None
                if integrity == "VERIFIED":
                    try:
                        array = np.load(path, mmap_mode="r", allow_pickle=False)
                    except (OSError, ValueError) as error:
                        integrity = "INVALID_FORMAT"
                        integrity_reason = f"recorded embedding is not a readable NumPy array: {type(error).__name__}"
                    else:
                        rows = int(array.shape[0]) if array.ndim == 2 else None
                        dimension = int(array.shape[1]) if array.ndim == 2 else None
                        if array.ndim != 2:
                            integrity = "INVALID_FORMAT"
                            integrity_reason = f"recorded embedding has {array.ndim} dimensions; expected 2"
            items.append(
                {
                    "run_id": leaf["run_id"],
                    "matrix_id": leaf["matrix_id"],
                    "leaf_id": leaf["leaf_id"],
                    "matcher_id": leaf["matcher_id"],
                    "condition_id": leaf["condition_id"],
                    "dataset_id": leaf["dataset_id"],
                    "classification": artifact.classification.value,
                    "integrity": integrity,
                    "integrity_reason": integrity_reason,
                    "registered_sha256": artifact.sha256,
                    "observed_sha256": observed_sha256,
                    "size_bytes": size_bytes,
                    "rows": rows,
                    "dimension": dimension,
                    "created_at": artifact.created_at.isoformat(),
                    "links": {
                        "run": f"/api/projects/{project_id}/runs/{leaf['run_id']}",
                    },
                }
            )
        return JSONResponse(content=items)

    @app.get("/api/projects/{project_id}/purge-scope")
    def project_purge_scope(project_id: str) -> JSONResponse:
        from ..security.purge import public_purge_scope

        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        targets = purge_targets(store(), settings.data_dir, project_id)
        return JSONResponse(content=public_purge_scope(project_id, targets))

    def active_work_for_project_locked(project_id: str) -> list[str]:
        active: list[str] = []
        if active_global_mutations["count"]:
            active.append(f"{active_global_mutations['count']} unscoped state-changing request(s)")
        if active_project_mutations.get(project_id, 0):
            active.append(f"{active_project_mutations[project_id]} state-changing request(s)")
        active.extend(f"quality job {job.job_id}" for job in jobs.values() if job.project.project_id == project_id and not job.ended_at)
        active.extend(f"matcher matrix {job.matrix_id}" for job in matrices.values() if job.project.project_id == project_id and job.progress["phase"] not in {"completed", "failed"})
        active.extend(f"degradation analysis {job.analysis_id}" for job in degradations.values() if job.project.project_id == project_id and job.progress["phase"] not in {"completed", "failed"})
        active.extend(f"identification search {job.search_id}" for job in searches.values() if job.project.project_id == project_id and job.progress["phase"] not in {"completed", "failed"})
        active.extend(f"utility analysis {analysis_id}" for analysis_id, item in analyses.items() if item.get("project_id") == project_id and item["state"] == "RUNNING")
        active.extend(f"cross-matcher analysis {analysis_id}" for analysis_id, item in cross_jobs.items() if item.get("project_id") == project_id and item["state"] == "RUNNING")
        active.extend(
            f"direct pair comparison {comparison_id}"
            for comparison_id, job in direct_pair_jobs.items()
            if job.pair.project_id == project_id
        )
        return active

    @app.post("/api/projects/{project_id}/purge", status_code=201)
    def project_purge(project_id: str, request: PurgeRequest) -> JSONResponse:
        from ..security.purge import public_purge_record

        def release_purge_fence() -> None:
            with job_lock:
                purging_projects.discard(project_id)

        with job_lock:
            if project_id in purging_projects:
                raise HTTPException(status_code=409, detail="project purge is already in progress")
            active = active_work_for_project_locked(project_id)
            if active:
                raise HTTPException(status_code=409, detail="purge refused while project work is active: " + "; ".join(active))
            purging_projects.add(project_id)
        release_after_response = False
        try:
            try:
                record = purge_project(store(), data_dir=settings.data_dir, project_id=project_id, actor=settings.actor, confirm=request.confirm)
            except PurgeError as error:
                safe_detail = str(error)
                if safe_detail not in {
                    "purge confirmation must repeat the project id",
                    "project is not recorded",
                }:
                    safe_detail = "purge request could not be completed"
                raise HTTPException(status_code=409, detail=safe_detail) from error
            public_record = public_purge_record(record)
            if not record["verified"]:
                raise HTTPException(
                    status_code=409,
                    detail={
                        **public_record,
                        "message": "purge did not verify; inspect the private local failure evidence",
                    },
                )
            record_action(project_id, AuditEventType.DATASET_PURGED, "Project", project_id, {"purge_id": record["purge_id"], "deleted": len(record["deleted"]), "already_absent": len(record["already_absent"]), "reconciled_after_deletion_intent": len(record.get("reconciled_after_deletion_intent", [])), "failed": len(record["failed"]), "verified": record["verified"], "scope": SCOPE_STATEMENT})
            response = JSONResponse(
                status_code=201,
                content=public_record,
                background=BackgroundTask(release_purge_fence),
            )
            release_after_response = True
            return response
        finally:
            if not release_after_response:
                release_purge_fence()

    @app.get("/api/projects/{project_id}/purges")
    def project_purges(project_id: str) -> JSONResponse:
        from ..security.purge import public_purge_record

        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return JSONResponse(
            content=[
                public_purge_record(record)
                for record in store().purges(project_id=project_id)
            ]
        )

    @app.get("/api/resources", response_model=list[ResourceOut])
    def resources() -> list[ResourceOut]:
        eng = engine()
        return [_resource_out(eng, resource.resource_id) for resource in eng.registry.resources]

    detector_holder: dict[str, Any] = {}

    def detector():
        if "detector" not in detector_holder:
            from ..matchers.detection import SCRFDDetector
            from ..matchers.matrix import SCRFD_RESOURCE_ID

            scrfd = engine().registry.resource(SCRFD_RESOURCE_ID)
            model = next(item.identity for item in scrfd.artifacts if item.role == "MODEL")
            detector_holder["detector"] = SCRFDDetector(Path(model.path), model.sha256)
        return detector_holder["detector"]

    def _registered_sample_records() -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for resource in engine().input_resources:
            for artifact in resource.artifacts:
                if artifact.role != "DATA" or artifact.identity.path is None:
                    continue
                identity = artifact.identity
                digest = hashlib.sha256(
                    "\0".join(
                        (
                            "registered-sample",
                            resource.resource_id,
                            artifact.role,
                            identity.sha256,
                        )
                    ).encode("utf-8")
                ).hexdigest()
                records.append(
                    {
                        "registered_sample_id": f"registered-sample:sha256:{digest}",
                        "resource_id": resource.resource_id,
                        "resource_state": resource.state,
                        "approved_scopes": tuple(resource.approved_scopes),
                        "display_name": Path(identity.path).name,
                        "role": "DATA",
                        "sha256": identity.sha256,
                        "size_bytes": identity.size_bytes,
                        "media_type": mimetypes.guess_type(identity.path)[0]
                        or "application/octet-stream",
                        "private_path": identity.path,
                    }
                )
        return records

    def _registered_evidence_image(
        registered_sample_id: str,
    ) -> tuple[str, str, dict[str, Any]]:
        record = next(
            (
                item
                for item in _registered_sample_records()
                if item["registered_sample_id"] == registered_sample_id
            ),
            None,
        )
        if record is None:
            raise HTTPException(
                status_code=404, detail="registered image is not recorded"
            )
        return record["private_path"], record["sha256"], record

    def _project_sample(project_id: str, sample_id: str) -> Sample:
        sample = store().sample(sample_id)
        dataset = store().dataset(sample.dataset_id) if sample is not None else None
        if sample is None or dataset is None or dataset.project_id != project_id:
            raise HTTPException(
                status_code=404, detail="sample is not recorded in this project"
            )
        return sample

    _PRIVATE_SAMPLE_METADATA_KEYS = {"original_name"}
    _OMIT_PUBLIC_METADATA = object()

    def _public_sample_metadata(value: Any) -> Any:
        if isinstance(value, dict):
            public: dict[str, Any] = {}
            for key, item in value.items():
                normalized = str(key).lower()
                if (
                    normalized in _PRIVATE_SAMPLE_METADATA_KEYS
                    or "path" in normalized
                    or "directory" in normalized
                ):
                    continue
                projected = _public_sample_metadata(item)
                if projected is not _OMIT_PUBLIC_METADATA:
                    public[str(key)] = projected
            return public
        if isinstance(value, list):
            return [
                projected
                for item in value
                if (projected := _public_sample_metadata(item))
                is not _OMIT_PUBLIC_METADATA
            ]
        if isinstance(value, str) and Path(value).is_absolute():
            return _OMIT_PUBLIC_METADATA
        return value

    def _public_sample(project_id: str, sample: Sample) -> dict[str, Any]:
        return {
            "sample_id": sample.sample_id,
            "dataset_id": sample.dataset_id,
            "subject_id": sample.subject_id.value,
            "media_type": sample.media_type,
            "sha256": sample.sha256,
            "size_bytes": sample.size_bytes,
            "metadata": _public_sample_metadata(json.loads(sample.metadata.text)),
            "links": {
                "image": f"/api/projects/{project_id}/samples/{sample.sample_id}/image",
                "detection": f"/api/projects/{project_id}/samples/{sample.sample_id}/detection",
                "ofiq_landmarks": f"/api/projects/{project_id}/samples/{sample.sample_id}/ofiq-landmarks",
                "quality": f"/api/projects/{project_id}/samples/{sample.sample_id}/quality",
            },
        }

    def _detection_response(
        source_path: str, sha256: str, condition_id: str | None
    ) -> JSONResponse:
        try:
            payload = detection_geometry(detector(), source_path, sha256, condition_id)
        except EvidenceError as error:
            raise HTTPException(status_code=422, detail=redact_private_paths(str(error))) from error
        return JSONResponse(content=payload)

    def _ofiq_landmark_response(source_path: str, sha256: str) -> JSONResponse:
        try:
            payload = ofiq_landmarks(engine().registry, source_path, sha256)
        except EvidenceError as error:
            raise HTTPException(status_code=422, detail=redact_private_paths(str(error))) from error
        return JSONResponse(content=payload)

    @app.get("/api/detection")
    def sample_detection_endpoint(
        registered_sample_id: str = Query(
            min_length=len("registered-sample:sha256:") + 64,
            max_length=len("registered-sample:sha256:") + 64,
            pattern=r"^registered-sample:sha256:[0-9a-f]{64}$",
        ),
        condition_id: str | None = Query(default=None),
    ) -> JSONResponse:
        """QS-Y85: SCRFD geometry for an exact globally registered input image."""

        source_path, sha, _ = _registered_evidence_image(registered_sample_id)
        return _detection_response(source_path, sha, condition_id)

    @app.get("/api/projects/{project_id}/samples/{sample_id}/detection")
    def project_sample_detection(
        project_id: str,
        sample_id: str,
        condition_id: str | None = Query(default=None),
    ) -> JSONResponse:
        sample = _project_sample(project_id, sample_id)
        return _detection_response(sample.source_path, sample.sha256, condition_id)

    @app.get("/api/ofiq-landmarks")
    def ofiq_landmarks_endpoint(
        registered_sample_id: str = Query(
            min_length=len("registered-sample:sha256:") + 64,
            max_length=len("registered-sample:sha256:") + 64,
            pattern=r"^registered-sample:sha256:[0-9a-f]{64}$",
        ),
    ) -> JSONResponse:
        """QS-Z90: OFIQ ADNet-98 landmarks for a globally registered input."""

        source_path, sha, _ = _registered_evidence_image(registered_sample_id)
        return _ofiq_landmark_response(source_path, sha)

    @app.get("/api/projects/{project_id}/samples/{sample_id}/ofiq-landmarks")
    def project_sample_ofiq_landmarks(
        project_id: str, sample_id: str
    ) -> JSONResponse:
        sample = _project_sample(project_id, sample_id)
        return _ofiq_landmark_response(sample.source_path, sample.sha256)

    @app.get("/api/projects/{project_id}/matcher-runs/{run_id}/pairs/{pair_id}/evidence")
    def matcher_pair_evidence(project_id: str, run_id: str, pair_id: str) -> JSONResponse:
        """QS-Y87: recorded match evidence for one compared pair, with population context."""

        row = store().matcher_leaf(run_id)
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="matcher Run is not recorded in this project")
        try:
            payload = pair_evidence(store(), row, pair_id)
        except EvidenceError as error:
            raise HTTPException(status_code=404, detail=redact_private_paths(str(error))) from error
        for side in ("left", "right"):
            info = payload["sides"][side]
            info.pop("path", None)
            if info.get("sample_id"):
                info["links"] = {
                    "image": f"/api/projects/{row['project_id']}/samples/{info['sample_id']}/image",
                    "crop": f"/api/projects/{project_id}/matcher-runs/{run_id}/pairs/{pair_id}/crops/{side}",
                    "detection": f"/api/projects/{row['project_id']}/samples/{info['sample_id']}/detection?condition_id={row['condition_id']}",
                }
        return JSONResponse(content=payload)

    @app.get("/api/projects/{project_id}/matcher-runs/{run_id}/pairs/{pair_id}/crops/{side}")
    def matcher_pair_crop(project_id: str, run_id: str, pair_id: str, side: Literal["left", "right"]) -> Response:
        """QS-Y86: the aligned 112x112 crop actually compared, recomputed under the recorded frozen condition."""

        row = store().matcher_leaf(run_id)
        if row is None or row["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="matcher Run is not recorded in this project")
        import pyarrow.parquet as pq

        rows = pq.read_table(row["comparisons_parquet"]).to_pylist()
        target = next((item for item in rows if item["pair_id"] == pair_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="pair is not part of this Run")
        sample = _project_sample(project_id, target[f"{side}_sample_id"])
        try:
            crop, crop_sha = recompute_crop(detector(), sample.source_path, sample.sha256, row["condition_id"])
        except EvidenceError as error:
            raise HTTPException(status_code=422, detail=redact_private_paths(str(error))) from error
        ok, encoded = cv2.imencode(".png", crop)
        if not ok:
            raise HTTPException(status_code=500, detail="crop encoding failed")
        return Response(content=encoded.tobytes(), media_type="image/png", headers={"X-Crop-Sha256": crop_sha, "Cache-Control": "no-store"})

    @app.get("/api/samples/registered", response_model=list[RegisteredSample])
    def registered_samples() -> list[RegisteredSample]:
        return [
            RegisteredSample(**{key: value for key, value in record.items() if key != "private_path"})
            for record in _registered_sample_records()
        ]

    @app.get("/api/samples/image")
    def sample_image(
        registered_sample_id: str = Query(
            min_length=len("registered-sample:sha256:") + 64,
            max_length=len("registered-sample:sha256:") + 64,
            pattern=r"^registered-sample:sha256:[0-9a-f]{64}$",
        ),
    ) -> StreamingResponse:
        """Serve only a globally registered DATA artifact by opaque identity."""

        path, expected_sha256, record = _registered_evidence_image(registered_sample_id)
        media_type = record["media_type"]
        if media_type is None or not media_type.startswith("image/"):
            raise HTTPException(status_code=415, detail="artifact is not a displayable image")
        snapshot, observed_sha256 = open_recorded_snapshot(Path(path), expected_sha256, "image")
        return recorded_snapshot_response(snapshot, media_type=media_type, filename=None, headers={"Cache-Control": "no-store", "X-QualSight-Sha256": observed_sha256})

    @app.get("/api/projects/{project_id}/samples/{sample_id}/image")
    def project_sample_image(project_id: str, sample_id: str) -> StreamingResponse:
        """Serve one recorded sample only through its owning project identity."""

        sample = _project_sample(project_id, sample_id)
        if not sample.media_type.startswith("image/"):
            raise HTTPException(status_code=415, detail="sample is not a displayable image")
        snapshot, observed_sha256 = open_recorded_snapshot(
            Path(sample.source_path), sample.sha256, "sample image"
        )
        return recorded_snapshot_response(
            snapshot,
            media_type=sample.media_type,
            filename=None,
            headers={
                "Cache-Control": "no-store",
                "X-QualSight-Sha256": observed_sha256,
            },
        )

    def _assess(request: AssessRequest) -> AssessResponse | JSONResponse:
        eng = engine()
        image_path, _, _ = _registered_evidence_image(
            request.registered_sample_id
        )

        def recorded_failure(
            *,
            engine_version: str,
            exit_code: int,
            reason_code: Literal[
                "ENGINE_EXECUTION_FAILED", "ENGINE_RETURNED_NO_MEASURES"
            ],
            message: str,
            reason: str,
            stderr: str,
        ) -> AssessmentFailure:
            event = record_system_action(
                "QUALITY_ASSESSMENT_FAILED",
                request.registered_sample_id,
                {
                    "registered_sample_id": request.registered_sample_id,
                    "image_path": image_path,
                    "engine_version": engine_version,
                    "exit_code": exit_code,
                    "reason_code": reason_code,
                    "reason": reason,
                    "stderr": stderr,
                },
            )
            return AssessmentFailure(
                registered_sample_id=request.registered_sample_id,
                engine_version=engine_version,
                exit_code=exit_code,
                reason_code=reason_code,
                message=message,
                failure_event_id=event["system_event_id"],
                failure_evidence_sha256=event["evidence_sha256"],
            )

        target_project = None
        if request.project_id is not None:
            try:
                target_project = projects().require_writable(request.project_id)
            except KeyError as error:
                raise HTTPException(status_code=404, detail="project is not recorded") from error
            except ProjectLifecycleError as error:
                raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        try:
            result: OFIQSingleSampleResult = eng.assess(image_path, request.purpose_scope)
        except ContractValidationError as error:
            message = str(error)
            status = 403 if "approved scope" in message else 404 if "not a registered" in message else 400
            raise HTTPException(status_code=status, detail=redact_private_paths(message)) from error
        except ExecutionOutcomeError as error:
            record = error.record
            failure = recorded_failure(
                engine_version=eng.runtime.version or "",
                exit_code=int(record.exit_code),
                reason_code="ENGINE_EXECUTION_FAILED",
                message="The registered engine failed before usable quality measures were recorded.",
                reason=str(error),
                stderr=record.stderr.content.decode("utf-8", errors="replace"),
            )
            return JSONResponse(status_code=422, content=failure.model_dump())
        except OFIQEngineError as error:
            raise HTTPException(status_code=502, detail=redact_private_paths(str(error))) from error
        if not result.succeeded:
            failure = recorded_failure(
                engine_version=result.assessment.engine_version,
                exit_code=int(result.record.exit_code),
                reason_code="ENGINE_RETURNED_NO_MEASURES",
                message="The registered engine completed without usable quality measures.",
                reason=result.row.failure_reason or "engine reported no measures",
                stderr=result.record.stderr.content.decode("utf-8", errors="replace"),
            )
            return JSONResponse(status_code=422, content=failure.model_dump())
        try:
            bundle = eng.build_run(result, purpose_scope=request.purpose_scope, project=target_project)
        except OFIQEngineError as error:
            raise HTTPException(status_code=502, detail=redact_private_paths(str(error))) from error
        try:
            evidence_sha256 = store().record_bundle(bundle)
        except ImmutableRecordConflict as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        run = bundle.run
        return AssessResponse(
            run_id=run.run_id,
            evidence_sha256=evidence_sha256,
            engine_resource_id=run.quality_engine.resource_id,
            engine_version=run.quality_engine.version,
            engine_state=run.quality_engine.registry_state,
            purpose_scope=request.purpose_scope,
            registered_sample_id=request.registered_sample_id,
            image_sha256=bundle.sample.sha256,
            initialization_ms=run.initialization_ms,
            assessment_ms=run.assessment_ms,
            runtime_seconds=run.runtime_seconds,
            measures=tuple(
                MeasureOut(
                    name=m.name,
                    native=m.native,
                    native_unit=native_measure_metadata(m.name).unit,
                    native_semantics=native_measure_metadata(m.name).semantics,
                    scalar=m.scalar,
                    failed=m.failed,
                    failure_reason=observed_measure_failure_reason(
                        name=m.name,
                        native=m.native,
                        scalar=m.scalar,
                        failed=m.failed,
                    ),
                )
                for m in result.row.measures
            ),
            failed_measures=result.row.failed_measure_names,
            warnings=run.warnings,
            provenance=ProvenanceLinks(
                run=f"/api/projects/{run.project_id}/runs/{run.run_id}",
                project=f"/api/projects/{run.project_id}",
                experiment=f"/api/projects/{run.project_id}/experiments/{run.experiment_id}",
                sample=f"/api/projects/{run.project_id}/samples/{bundle.sample.sample_id}",
                audit_events=f"/api/projects/{run.project_id}/runs/{run.run_id}/audit",
            ),
        )

    @app.post(
        "/api/quality/assess",
        response_model=AssessResponse,
        responses={422: {"model": AssessmentFailure}},
    )
    def assess(request: AssessRequest) -> AssessResponse | JSONResponse:
        with admitted_mutation(request.project_id):
            return _assess(request)

    @app.post("/api/quality/assess-upload", status_code=201)
    async def assess_upload(
        request: Request,
        project_id: str = Query(min_length=1),
        purpose_scope: str = Query(min_length=1),
        filename: str = Query(default="uploaded-image"),
    ) -> JSONResponse:
        """QS-Y81 (decision 30): assess one operator-uploaded photo with the registered engine, recorded as a Run."""

        data = await read_bounded_request_body(request, maximum_bytes=MAX_UPLOAD_BYTES)
        if not data:
            raise HTTPException(status_code=422, detail="the request body is empty")

        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error

        def _process() -> dict[str, Any]:
            importer = OwnerSuppliedImporter(store(), settings.data_dir, projects().actor)
            operation: dict[str, Any] | None = None
            try:
                admitted = importer.admission.admit_bytes(data, filename, project_id)
                dataset, sample = importer.import_admitted_single(project, admitted, f"Uploaded photo {filename}")
            except OwnerSuppliedError as error:
                raise HTTPException(status_code=422, detail=redact_private_paths(str(error))) from error
            try:
                selection_rule = (
                    "the single operator-uploaded owner-supplied image (decision 30)"
                )
                job = OFIQBatchJob(
                    engine=engine(),
                    store=store(),
                    project=project,
                    dataset=dataset,
                    samples=[sample],
                    purpose_scope=purpose_scope,
                    job_root=settings.data_dir / "jobs",
                    chunks=1,
                    selection_rule=selection_rule,
                )
                operation_request = {
                    "dataset_id": dataset.dataset_id,
                    "sample_id": sample.sample_id,
                    "purpose_scope": purpose_scope,
                    "chunks": 1,
                    "execution": "host",
                    "sample_count": 1,
                    "selection_rule": selection_rule,
                    "source_sha256": admitted.sha256,
                }
                operation, created = store().admit_operation(
                    operation_id=job.job_id,
                    operation_kind="QUALITY_BATCH",
                    project_id=project_id,
                    request=operation_request,
                    output_path=job.job_dir,
                    result_id=job.job_id,
                )
                if not created:
                    raise BatchJobError(
                        "the synchronous upload quality operation identity already exists"
                    )
                job.bind_operation_request_sha256(
                    CanonicalJson.from_value(operation_request).sha256
                )
                with job_lock:
                    jobs[job.job_id] = job
                store().start_operation(str(operation["operation_id"]))
                job.run()
            except BatchJobError as error:
                raise HTTPException(status_code=422, detail=redact_private_paths(str(error))) from error
            finally:
                if "job" in locals():
                    if operation is not None:
                        terminal_state = (
                            job.state
                            if job.state in {"COMPLETED", "FAILED", "CANCELLED"}
                            else "FAILED"
                        )
                        terminal_error = job.error if terminal_state == "FAILED" else None
                        if terminal_state == "FAILED" and not terminal_error:
                            terminal_error = (
                                "synchronous upload quality batch ended from "
                                f"nonterminal state {job.state}"
                            )
                        store().finish_operation(
                            str(operation["operation_id"]),
                            state=terminal_state,
                            error=terminal_error,
                        )
                    store().finish_job(job.job_id, job.ended_at.isoformat() if job.ended_at else "", job.state)
            if job.state != "COMPLETED" or not job.run_id:
                raise HTTPException(status_code=422, detail=f"the engine did not complete on the uploaded image (state {job.state})")
            import pyarrow.parquet as pq

            from ..engines.ofiq.batch import MEASURE_NAMES

            table = pq.read_table(job.parquet_path).to_pylist()
            row = next(item for item in table if item["sample_id"] == sample.sample_id)
            if row["outcome"] != "MEASURED":
                raise HTTPException(status_code=422, detail=f"the engine reported {row['outcome']} for the uploaded image")
            measures = [
                {
                    "name": name,
                    "native": row[f"{name}.native"],
                    "native_unit": native_measure_metadata(name).unit,
                    "native_semantics": native_measure_metadata(name).semantics,
                    "scalar": row[f"{name}.scalar"],
                    "failed": bool(row[f"{name}.failed"]),
                    "failure_reason": observed_measure_failure_reason(
                        name=name,
                        native=row[f"{name}.native"],
                        scalar=row[f"{name}.scalar"],
                        failed=bool(row[f"{name}.failed"]),
                    ),
                }
                for name in MEASURE_NAMES
            ]
            record_action(
                project_id,
                AuditEventType.RUN_COMPLETED,
                "OwnerSuppliedAssessment",
                job.run_id,
                {"source_kind": OWNER_SOURCE_KIND, "sha256": admitted.sha256, "original_name": filename},
            )
            return {
                "run_id": job.run_id,
                "job_id": job.job_id,
                "dataset_id": dataset.dataset_id,
                "sample_id": sample.sample_id,
                "source_kind": OWNER_SOURCE_KIND,
                "image_sha256": admitted.sha256,
                "original_name": filename,
                "width": admitted.width,
                "height": admitted.height,
                "measures": measures,
                "failed_measures": [m["name"] for m in measures if m["failed"]],
                "provenance": {
                    "run": f"/api/projects/{project_id}/runs/{job.run_id}",
                    "sample": f"/api/projects/{project_id}/samples/{sample.sample_id}",
                    "sample_image": f"/api/projects/{project_id}/samples/{sample.sample_id}/image",
                    "statement": "OWNER_SUPPLIED_LOCAL_INPUT: operator-supplied bytes admitted by hash under decision 30; not a registered, rights-reviewed dataset.",
                },
            }

        with admitted_mutation(project_id):
            reservation = reserve_scientific_work(
                f"owner-upload:sha256:{hashlib.sha256(data).hexdigest()}"
            )
            payload = await asyncio.wrap_future(reservation.submit(_process))
            return JSONResponse(status_code=201, content=payload)

    @app.post("/api/projects/{project_id}/datasets/import-owner-directory", status_code=201)
    def import_owner_directory(project_id: str, request: OwnerDirectoryImportRequest) -> JSONResponse:
        """QS-Y82: identity-labelled dataset from an operator directory (subject = immediate folder name)."""

        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        importer = OwnerSuppliedImporter(store(), settings.data_dir, projects().actor)
        try:
            result = importer.import_directory(project, Path(request.root), request.name)
        except (OwnerSuppliedError, OSError) as error:
            logging.getLogger(__name__).warning(
                "owner directory import refused for project %s: %s",
                project_id,
                error,
            )
            raise HTTPException(
                status_code=400,
                detail="owner directory import was refused; review the private local diagnostic log",
            ) from error
        record_action(project_id, AuditEventType.DATASET_IMPORTED, "Dataset", result.dataset.dataset_id, {"source_kind": OWNER_SOURCE_KIND, "samples": result.sample_count, "subjects": result.subject_count})
        return JSONResponse(status_code=201, content={
            "dataset_id": result.dataset.dataset_id,
            "sample_count": result.sample_count,
            "subject_count": result.subject_count,
            "source_kind": OWNER_SOURCE_KIND,
            "statistics_sha256": result.statistics_sha256,
        })

    @app.post("/api/projects/{project_id}/owner-pair-admissions", status_code=201)
    def admit_owner_pair(
        project_id: str,
        request: OwnerPairAdmissionRequest,
    ) -> JSONResponse:
        try:
            projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        with admitted_mutation(project_id):
            try:
                record, created = store().record_owner_pair_admission(
                    project_id=project_id,
                    left_sample_id=request.left_sample_id,
                    right_sample_id=request.right_sample_id,
                    relationship=request.relationship,
                    actor_id=projects().actor,
                )
            except OwnerPairNotFound as error:
                raise HTTPException(
                    status_code=404,
                    detail="owner-supplied sample is not recorded for this project",
                ) from error
            except OwnerPairIntegrityError as error:
                raise HTTPException(
                    status_code=409,
                    detail="owner-supplied pair evidence failed integrity verification",
                ) from error
            except OwnerPairValidationError as error:
                raise HTTPException(status_code=422, detail=redact_private_paths(str(error))) from error
        return JSONResponse(
            status_code=201 if created else 200,
            content=_public_projection(
                record.model_dump(mode="json"), omit_location_keys=True
            ),
        )

    def _direct_comparison_links(
        project_id: str, comparison_id: str, run_id: str
    ) -> dict[str, str]:
        root = (
            f"/api/projects/{project_id}/owner-pair-comparisons/{comparison_id}"
        )
        return {
            "self": root,
            "evidence": f"{root}/evidence",
            "run": f"/api/projects/{project_id}/direct-pair-runs/{run_id}",
            "raw_manifest": f"{root}/raw",
        }

    def _direct_comparison_status(operation: dict[str, Any]) -> dict[str, Any]:
        state_value = (
            "INTERRUPTED"
            if operation["state"] in {"CANCELLED", "INTERRUPTED"}
            else operation["state"]
        )
        request_value = json.loads(operation["request_json"])
        comparison_id = str(operation["request_id"])
        terminal = state_value in {"COMPLETED", "FAILED", "INTERRUPTED"}
        return {
            "schema_version": "1.0",
            "comparison_id": comparison_id,
            "run_id": operation["result_id"],
            "project_id": operation["project_id"],
            "pair_id": request_value["pair_id"],
            "state": state_value,
            "links": _direct_comparison_links(
                operation["project_id"],
                comparison_id,
                operation["result_id"],
            ),
            "poll_after_ms": None if terminal else 500,
        }

    def _verified_direct_comparison(project_id: str, comparison_id: str):
        operation = store().operation(comparison_id)
        if (
            operation is None
            or operation["operation_kind"] != "DIRECT_PAIR_COMPARISON"
            or operation["project_id"] != project_id
        ):
            raise HTTPException(
                status_code=404,
                detail="direct pair comparison is not recorded in this project",
            )
        try:
            run_record = store().direct_pair_comparison(comparison_id)
        except ImmutableRecordConflict as error:
            raise HTTPException(
                status_code=409,
                detail="recorded direct comparison evidence failed integrity verification",
            ) from error
        if run_record is None or run_record.project_id != project_id:
            raise HTTPException(
                status_code=404,
                detail="direct pair comparison is not recorded in this project",
            )
        expected_directory = (
            settings.data_dir
            / "direct-pair-comparisons"
            / comparison_id.replace(":", "_")
        )
        try:
            if (
                Path(run_record.output_directory).is_symlink()
                or Path(run_record.output_directory).resolve(strict=True)
                != expected_directory.resolve(strict=True)
            ):
                raise ValueError("output directory identity differs")
            pair_record = store().owner_pair(run_record.project_id, run_record.pair_id)
            if (
                pair_record.left_sample_id != run_record.left_sample_id
                or pair_record.right_sample_id != run_record.right_sample_id
                or pair_record.relationship is not run_record.relationship
            ):
                raise ValueError("pair identity differs")
            samples = {
                "left": store().sample(run_record.left_sample_id),
                "right": store().sample(run_record.right_sample_id),
            }
            if samples["left"] is None or samples["right"] is None:
                raise ValueError("sample identity is absent")
            for side_name, side in (
                ("left", run_record.left),
                ("right", run_record.right),
            ):
                sample_record = samples[side_name]
                assert sample_record is not None
                for artifact in side.artifacts:
                    path = Path(artifact.path)
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("artifact is absent")
                    if (
                        path.stat().st_size != artifact.size_bytes
                        or sha256_file(path) != artifact.sha256
                    ):
                        raise ValueError("artifact hash differs")
                    if artifact.artifact == f"{side_name}-source":
                        if path.resolve(strict=True) != Path(sample_record.source_path).resolve(
                            strict=True
                        ):
                            raise ValueError("source identity differs")
                    elif not path.resolve(strict=True).is_relative_to(
                        expected_directory.resolve(strict=True)
                    ):
                        raise ValueError("derived artifact escaped its Run directory")
        except (OSError, ValueError, OwnerPairIntegrityError, OwnerPairNotFound) as error:
            raise HTTPException(
                status_code=409,
                detail="recorded direct comparison artifacts failed integrity verification",
            ) from error
        return run_record

    def _direct_side_evidence(
        project_id: str, comparison_id: str, side
    ) -> dict[str, Any]:
        artifacts = {item.artifact: item for item in side.artifacts}
        prefix = side.side
        raw_root = (
            f"/api/projects/{project_id}/owner-pair-comparisons/"
            f"{comparison_id}/raw"
        )
        crop = artifacts.get(f"{prefix}-crop")
        detection_artifact = artifacts.get(f"{prefix}-detection")
        embedding = artifacts.get(f"{prefix}-embedding")
        return {
            "sample_id": side.sample_id,
            "dataset_id": side.dataset_id,
            "sha256": side.source_sha256,
            "face_count": side.face_count,
            "detection": (
                None
                if side.detection is None
                else {
                    "state": "RECORDED",
                    **json.loads(side.detection.text),
                    "sha256": detection_artifact.sha256
                    if detection_artifact is not None
                    else None,
                }
            ),
            "crop": (
                None
                if crop is None
                else {
                    "state": "RECORDED",
                    "sha256": crop.sha256,
                    "pixel_sha256": side.crop_pixel_sha256,
                    "width": 112,
                    "height": 112,
                }
            ),
            "embedding": (
                None
                if embedding is None
                else {
                    "state": "RECORDED",
                    "sha256": embedding.sha256,
                    "value_sha256": side.embedding_value_sha256,
                    "shape": list(side.embedding_shape or ()),
                    "dtype": side.embedding_dtype,
                }
            ),
            "failure": (
                None
                if side.failure is None
                else {
                    "kind": side.failure,
                    "detail": "this side did not produce a comparable template",
                }
            ),
            "quality": None if side.quality is None else json.loads(side.quality.text),
            "links": {
                "source": f"{raw_root}/{prefix}-source",
                "crop": None if crop is None else f"{raw_root}/{prefix}-crop",
                "detection": (
                    None
                    if detection_artifact is None
                    else f"{raw_root}/{prefix}-detection"
                ),
                "embedding": (
                    None if embedding is None else f"{raw_root}/{prefix}-embedding"
                ),
                "quality": (
                    None
                    if side.quality is None
                    else f"/api/projects/{project_id}/samples/{side.sample_id}/quality"
                ),
            },
        }

    def _direct_evidence(run_record) -> dict[str, Any]:
        comparison_label = {
            OwnerPairRelationship.SAME_PERSON: "MATED",
            OwnerPairRelationship.DIFFERENT_PEOPLE: "NON_MATED",
            OwnerPairRelationship.NOT_RECORDED: None,
        }[run_record.relationship]
        links = _direct_comparison_links(
            run_record.project_id, run_record.comparison_id, run_record.run_id
        )
        return {
            "schema_version": "1.0",
            "comparison_id": run_record.comparison_id,
            "run_id": run_record.run_id,
            "state": "COMPLETED",
            "project_id": run_record.project_id,
            "pair": {
                "pair_id": run_record.pair_id,
                "relationship": run_record.relationship.value,
                "comparison_label": comparison_label,
                "relationship_assertion": run_record.relationship_assertion.model_dump(
                    mode="json"
                ),
            },
            "execution": {
                "matcher_id": run_record.matcher_id,
                "preprocessing_id": run_record.preprocessing_id,
                "threshold": run_record.threshold.model_dump(mode="json"),
                "matcher_checkpoint_sha256": run_record.matcher_checkpoint_sha256,
                "matcher_architecture_sha256": list(
                    run_record.matcher_architecture_sha256
                ),
                "matcher_device": run_record.matcher_device,
                "preprocessing_definition_sha256": run_record.preprocessing_definition_sha256,
                "detector_sha256": run_record.detector_sha256,
                "application_version": run_record.application_version,
                "started_at": run_record.started_at.isoformat(),
                "ended_at": run_record.ended_at.isoformat(),
                "runtime_seconds": run_record.runtime_seconds,
            },
            "result": run_record.result.model_dump(mode="json"),
            "claim_boundary": {
                "population_error_claims_allowed": False,
                "outcome_correctness_allowed": run_record.outcome_correctness_allowed,
                "ground_truth_authority": False,
                "prohibited": list(run_record.prohibited_claims),
            },
            "sides": {
                "left": _direct_side_evidence(
                    run_record.project_id,
                    run_record.comparison_id,
                    run_record.left,
                ),
                "right": _direct_side_evidence(
                    run_record.project_id,
                    run_record.comparison_id,
                    run_record.right,
                ),
            },
            "links": {
                "self": links["evidence"],
                "status": links["self"],
                "run": links["run"],
                "raw_manifest": links["raw_manifest"],
            },
        }

    @app.post(
        "/api/projects/{project_id}/owner-pair-comparisons", status_code=202
    )
    def start_direct_pair_comparison(
        project_id: str, request: DirectPairComparisonRequest
    ) -> JSONResponse:
        try:
            projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        try:
            matcher_id = MatcherId(request.matcher_id)
            preprocessing_id = ConditionId(request.preprocessing_id)
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail="matcher_id and preprocessing_id must name registered add-ons",
            ) from error
        if preprocessing_id not in SCRFD_CONDITIONS:
            raise HTTPException(
                status_code=409,
                detail="preprocessing add-on is incompatible with owner-supplied source images",
            )
        threshold = DirectPairThreshold.model_validate(
            {
                **request.threshold.model_dump(),
                "rule": "MATCH_WHEN_SCORE_GE_THRESHOLD",
            }
        )
        comparison_id, run_id, request_identity = direct_comparison_identity(
            pair_id=request.pair_id,
            matcher_id=matcher_id,
            preprocessing_id=preprocessing_id,
            threshold=threshold,
        )
        existing = store().operation(comparison_id)
        if existing is not None:
            if existing["project_id"] != project_id:
                raise HTTPException(
                    status_code=404,
                    detail="owner pair is not recorded for this project",
                )
            existing = store().reconcile_prepared_operation(comparison_id) or existing
            if existing["state"] == "COMPLETED":
                _verified_direct_comparison(project_id, comparison_id)
            return JSONResponse(
                status_code=(
                    200
                    if existing["state"]
                    in {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}
                    else 202
                ),
                content=_direct_comparison_status(existing),
            )
        try:
            pair_record = store().owner_pair(project_id, request.pair_id)
        except OwnerPairNotFound as error:
            raise HTTPException(
                status_code=404,
                detail="owner pair is not recorded for this project",
            ) from error
        except OwnerPairIntegrityError as error:
            raise HTTPException(
                status_code=409,
                detail="owner-supplied pair evidence failed integrity verification",
            ) from error
        left = store().sample(pair_record.left_sample_id)
        right = store().sample(pair_record.right_sample_id)
        if left is None or right is None:
            raise HTTPException(
                status_code=409,
                detail="owner-supplied pair evidence failed integrity verification",
            )
        profiles = {
            item["addon_id"]: item for item in preprocessing_profiles(engine().registry)
        }
        profile = profiles.get(preprocessing_id.value)
        if (
            profile is None
            or profile["runtime_state"] != "READY"
            or profile["input_representation"]
            != "SOURCE_IMAGE_REQUIRING_DETECTION"
            or matcher_id.value not in profile["compatible_matcher_ids"]
        ):
            raise HTTPException(
                status_code=409,
                detail="preprocessing add-on is unavailable or incompatible",
            )
        try:
            matcher_manager().require_ready((matcher_id,))
            scrfd = detector()
        except (MatcherLifecycleError, OSError, ValueError) as error:
            raise HTTPException(
                status_code=409,
                detail="matcher or preprocessing dependencies are not ready",
            ) from error
        with job_lock:
            require_project_not_purging_locked(project_id)
        # Lock order: short API purge check, owner-file serialization, final API
        # purge recheck, then the store writer used by durable admission.
        with serialized_owner_file_mutation():
            try:
                pair_record = store().owner_pair(project_id, request.pair_id)
                left = store().sample(pair_record.left_sample_id)
                right = store().sample(pair_record.right_sample_id)
                if left is None or right is None:
                    raise OwnerPairNotFound(
                        "owner pair samples are no longer recorded"
                    )
            except (OwnerPairNotFound, OwnerPairIntegrityError) as error:
                operations_logger.warning(
                    "direct pair comparison admission refused after source evidence was removed",
                    extra={
                        "comparison_id": comparison_id,
                        "pair_id": request.pair_id,
                        "project_id": project_id,
                    },
                )
                raise HTTPException(
                    status_code=409,
                    detail="owner-supplied pair was removed before comparison admission",
                ) from error
            output_root = settings.data_dir / "direct-pair-comparisons"
            job = DirectPairComparisonJob(
                store=store(),
                pair=pair_record,
                left=left,
                right=right,
                matcher_id=matcher_id,
                preprocessing_id=preprocessing_id,
                threshold=threshold,
                matcher_manager=matcher_manager(),
                detector=scrfd,
                output_root=output_root,
                repository_root=settings.repository_root,
                actor_id=projects().actor,
                execution_timeout_seconds=settings.native_execution_timeout_seconds,
            )
            if (
                job.comparison_id != comparison_id
                or job.run_id != run_id
                or job.request != request_identity
            ):
                raise HTTPException(
                    status_code=409, detail="direct comparison request identity changed"
                )
            with job_lock:
                require_project_not_purging_locked(project_id)
                try:
                    operation, reservation = admit_scientific_operation(
                        operation_id=comparison_id,
                        operation_kind="DIRECT_PAIR_COMPARISON",
                        project_id=project_id,
                        request=json.loads(request_identity.text),
                        output_path=job.directory,
                        result_id=run_id,
                    )
                except HTTPException as error:
                    if error.status_code == 503:
                        raise HTTPException(status_code=429, detail=error.detail) from error
                    raise
                if reservation is None:
                    return JSONResponse(
                        status_code=200
                        if operation["state"]
                        in {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}
                        else 202,
                        content=_direct_comparison_status(operation),
                    )
                direct_pair_jobs[comparison_id] = job

        def _run_direct_comparison() -> None:
            try:
                job.run()
            except Exception:
                operations_logger.exception(
                    "direct pair comparison execution failed",
                    extra={
                        "comparison_id": comparison_id,
                        "project_id": project_id,
                    },
                )
                current = store().operation(comparison_id)
                if current is not None and current["state"] in {"QUEUED", "RUNNING"}:
                    store().finish_operation(
                        comparison_id,
                        state="FAILED",
                        error="direct pair comparison execution failed; inspect registered dependency and input integrity",
                    )
            finally:
                with job_lock:
                    direct_pair_jobs.pop(comparison_id, None)

        reservation.submit(_run_direct_comparison)
        return JSONResponse(
            status_code=202, content=_direct_comparison_status(operation)
        )

    @app.get("/api/projects/{project_id}/owner-pair-comparisons/{comparison_id}")
    def direct_pair_comparison_status(
        project_id: str, comparison_id: str
    ) -> JSONResponse:
        operation = store().operation(comparison_id)
        if (
            operation is None
            or operation["operation_kind"] != "DIRECT_PAIR_COMPARISON"
            or operation["project_id"] != project_id
        ):
            raise HTTPException(
                status_code=404,
                detail="direct pair comparison is not recorded in this project",
            )
        latest = store().latest_operation_attempt(str(operation["request_id"]))
        if latest is not None:
            operation = latest
        if operation["state"] == "COMPLETED":
            _verified_direct_comparison(project_id, comparison_id)
        return JSONResponse(content=_direct_comparison_status(operation))

    @app.get("/api/projects/{project_id}/owner-pair-comparisons")
    def project_direct_pair_comparisons(project_id: str) -> JSONResponse:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        rows = [
            row
            for row in store().operations_for_project(project_id)
            if row["operation_kind"] == "DIRECT_PAIR_COMPARISON"
        ]
        latest_by_request = {
            str(row["request_id"]): row
            for row in sorted(rows, key=lambda item: int(item["attempt"]))
        }
        return JSONResponse(
            content=[
                _direct_comparison_status(row)
                for row in latest_by_request.values()
            ]
        )

    @app.get(
        "/api/projects/{project_id}/owner-pair-comparisons/{comparison_id}/evidence"
    )
    def direct_pair_comparison_evidence(
        project_id: str, comparison_id: str
    ) -> JSONResponse:
        run_record = _verified_direct_comparison(project_id, comparison_id)
        return JSONResponse(content=_direct_evidence(run_record))

    @app.get("/api/projects/{project_id}/direct-pair-runs/{run_id}")
    def direct_pair_run(project_id: str, run_id: str) -> JSONResponse:
        try:
            run_record = store().direct_pair_comparison_by_run(run_id)
        except ImmutableRecordConflict as error:
            raise HTTPException(
                status_code=409,
                detail="recorded direct comparison evidence failed integrity verification",
            ) from error
        if run_record is None or run_record.project_id != project_id:
            raise HTTPException(
                status_code=404,
                detail="direct pair Run is not recorded in this project",
            )
        run_record = _verified_direct_comparison(
            project_id, run_record.comparison_id
        )
        return JSONResponse(content=_direct_evidence(run_record))

    @app.get(
        "/api/projects/{project_id}/owner-pair-comparisons/{comparison_id}/raw"
    )
    def direct_pair_comparison_raw_manifest(
        project_id: str, comparison_id: str
    ) -> JSONResponse:
        run_record = _verified_direct_comparison(project_id, comparison_id)
        artifacts = []
        for side in (run_record.left, run_record.right):
            for artifact in side.artifacts:
                artifacts.append(
                    {
                        "artifact": artifact.artifact,
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                        "media_type": artifact.media_type,
                        "link": f"/api/projects/{project_id}/owner-pair-comparisons/{comparison_id}/raw/{artifact.artifact}",
                    }
                )
        execution = json.dumps(
            _direct_evidence(run_record),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        artifacts.append(
            {
                "artifact": "execution-record",
                "sha256": hashlib.sha256(execution).hexdigest(),
                "size_bytes": len(execution),
                "media_type": "application/json",
                "link": f"/api/projects/{project_id}/owner-pair-comparisons/{comparison_id}/raw/execution-record",
            }
        )
        return JSONResponse(
            content={"comparison_id": comparison_id, "artifacts": artifacts}
        )

    @app.get(
        "/api/projects/{project_id}/owner-pair-comparisons/{comparison_id}/raw/{artifact_name}"
    )
    def direct_pair_comparison_raw_artifact(
        project_id: str,
        comparison_id: str,
        artifact_name: Literal[
            "left-source",
            "right-source",
            "left-crop",
            "right-crop",
            "left-detection",
            "right-detection",
            "left-embedding",
            "right-embedding",
            "execution-record",
        ],
    ) -> Response:
        run_record = _verified_direct_comparison(project_id, comparison_id)
        if artifact_name == "execution-record":
            body = json.dumps(
                _direct_evidence(run_record),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            digest = hashlib.sha256(body).hexdigest()
            media_type = "application/json"
        else:
            available = {
                artifact.artifact: artifact
                for side in (run_record.left, run_record.right)
                for artifact in side.artifacts
            }
            artifact = available.get(artifact_name)
            if artifact is None:
                raise HTTPException(
                    status_code=409,
                    detail="artifact is unavailable because the recorded side did not produce it",
                )
            snapshot, digest = open_recorded_snapshot(
                Path(artifact.path),
                artifact.sha256,
                "direct comparison artifact",
            )
            return recorded_snapshot_response(
                snapshot,
                media_type=artifact.media_type,
                filename=None,
                headers={
                    "Cache-Control": "no-store",
                    "X-QualSight-SHA256": digest,
                },
            )
        return Response(
            content=body,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store",
                "X-QualSight-SHA256": digest,
            },
        )

    @app.post("/api/projects/{project_id}/datasets/import-owner-manifest", status_code=201)
    def import_owner_manifest(project_id: str, request: OwnerManifestImportRequest) -> JSONResponse:
        """QS-Y82: identity-labelled dataset from an operator CSV manifest with a saved, reusable field mapping."""

        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        mapping = request.mapping
        if mapping is None:
            if not request.mapping_name:
                raise HTTPException(status_code=422, detail="either mapping or mapping_name is required")
            mapping = store().import_mapping(request.mapping_name)
            if mapping is None:
                raise HTTPException(status_code=404, detail=f"import mapping {request.mapping_name} is not recorded")
        importer = OwnerSuppliedImporter(store(), settings.data_dir, projects().actor)
        try:
            result = importer.import_manifest(project, Path(request.manifest_path), name=request.name, mapping=mapping)
        except (OwnerSuppliedError, OSError) as error:
            logging.getLogger(__name__).warning(
                "owner manifest import refused for project %s: %s",
                project_id,
                error,
            )
            raise HTTPException(
                status_code=400,
                detail="owner manifest import was refused; review the private local diagnostic log",
            ) from error
        if request.mapping_name and request.mapping is not None:
            store().record_import_mapping(request.mapping_name, mapping, projects().actor, datetime.now(timezone.utc).astimezone().isoformat())
        record_action(project_id, AuditEventType.DATASET_IMPORTED, "Dataset", result.dataset.dataset_id, {"source_kind": OWNER_SOURCE_KIND, "samples": result.sample_count, "subjects": result.subject_count, "mapping": mapping})
        return JSONResponse(status_code=201, content={
            "dataset_id": result.dataset.dataset_id,
            "sample_count": result.sample_count,
            "subject_count": result.subject_count,
            "source_kind": OWNER_SOURCE_KIND,
            "statistics_sha256": result.statistics_sha256,
            "mapping": mapping,
        })

    @app.get("/api/import-mappings")
    def list_import_mappings() -> JSONResponse:
        return JSONResponse(content=store().import_mappings())

    @app.post("/api/projects/{project_id}/datasets/{dataset_id}/protocols/freeze-owner-pairs", status_code=201)
    def freeze_owner_pairs_endpoint(project_id: str, dataset_id: str, request: OwnerPairsRequest) -> JSONResponse:
        """QS-Y83: deterministic seeded pair protocol over an owner-supplied identity-labelled dataset."""

        try:
            project = projects().require_writable(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error
        except ProjectLifecycleError as error:
            raise HTTPException(status_code=409, detail=redact_private_paths(str(error))) from error
        dataset = store().dataset(dataset_id)
        if dataset is None or dataset.project_id != project_id:
            raise HTTPException(status_code=404, detail="dataset is not recorded in this project")
        capacity_request = {
            "dataset_id": dataset_id,
            "max_mated_pairs_per_subject": request.max_mated_pairs_per_subject,
            "max_pairs": OWNER_PAIR_MAX_OUTPUT_PAIRS,
            "nonmated_multiple": request.nonmated_multiple,
            "project_id": project_id,
            "seed": request.seed,
        }
        try:
            with admitted_mutation(project_id):
                reservation = reserve_scientific_work(
                    f"owner-pairs:sha256:{CanonicalJson.from_value(capacity_request).sha256}"
                )
                record = reservation.submit(
                    freeze_owner_pairs,
                    store(),
                    project,
                    dataset,
                    data_dir=settings.data_dir,
                    actor_id=projects().actor,
                    seed=request.seed,
                    max_pairs=OWNER_PAIR_MAX_OUTPUT_PAIRS,
                    max_mated_pairs_per_subject=request.max_mated_pairs_per_subject,
                    nonmated_multiple=request.nonmated_multiple,
                ).result()
                record_action(project_id, AuditEventType.CONFIGURATION_CHANGED, "VerificationProtocol", record.protocol_id, {"pairs": record.pair_count, "seed": request.seed, "pairs_sha256": record.pairs_sha256})
        except OwnerSuppliedError as error:
            raise HTTPException(status_code=400, detail=redact_private_paths(str(error))) from error
        return JSONResponse(status_code=201, content={
            "protocol_id": record.protocol_id,
            "pair_count": record.pair_count,
            "mated_count": record.mated_count,
            "nonmated_count": record.nonmated_count,
            "pairs_sha256": record.pairs_sha256,
            "unique_subject_count": record.unique_subject_count,
            "limitations": list(record.limitations),
        })

    @app.get("/api/projects/{project_id}/runs", response_model=list[RunSummaryOut])
    def runs(
        project_id: str,
        limit: int = Query(default=200, ge=1, le=10_000),
    ) -> list[RunSummaryOut]:
        if store().project(project_id) is None:
            raise HTTPException(status_code=404, detail="project is not recorded")
        return [
            RunSummaryOut(**asdict(item))
            for item in store().run_summaries(limit, project_id=project_id)
        ]

    @app.get("/api/projects/{project_id}/runs/{run_id}")
    def run(project_id: str, run_id: str) -> JSONResponse:
        found = store().run(run_id)
        if found is None or found[0].project_id != project_id:
            raise HTTPException(status_code=404, detail="run is not recorded in this project")
        completed, evidence_sha256 = found
        return JSONResponse(
            content={
                "evidence_sha256": evidence_sha256,
                "sample_id": store().sample_id_for_run(run_id),
                "run": _public_run(completed),
            }
        )

    @app.get("/api/projects/{project_id}/runs/{run_id}/audit")
    def run_audit(project_id: str, run_id: str) -> JSONResponse:
        found = store().run(run_id)
        if found is None or found[0].project_id != project_id:
            raise HTTPException(status_code=404, detail="run is not recorded in this project")
        events = store().audit_events_for(run_id)
        if not events:
            raise HTTPException(status_code=404, detail="no audit event is recorded for this run")
        return JSONResponse(
            content=[
                _public_projection(
                    event.model_dump(mode="json"), omit_location_keys=True
                )
                for event in events
            ]
        )

    @app.get("/api/projects/{project_id}", response_model=ProjectOut)
    def project(project_id: str) -> ProjectOut:
        try:
            return project_out(projects().head(project_id))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project is not recorded") from error

    @app.get("/api/projects/{project_id}/experiments/{experiment_id}")
    def experiment(project_id: str, experiment_id: str) -> JSONResponse:
        found = store().experiment(experiment_id)
        if found is None or found.project_id != project_id:
            raise HTTPException(status_code=404, detail="experiment is not recorded in this project")
        return JSONResponse(
            content=_public_projection(
                found.model_dump(mode="json"), omit_location_keys=True
            )
        )

    @app.get("/api/projects/{project_id}/samples/{sample_id}")
    def sample(project_id: str, sample_id: str) -> JSONResponse:
        found = _project_sample(project_id, sample_id)
        return JSONResponse(content=_public_sample(project_id, found))

    @app.api_route(
        "/api/{retired_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    def retired_or_unknown_api_route(retired_path: str) -> None:
        """Fail closed instead of allowing retired API paths to reach the UI mount."""

        raise HTTPException(status_code=404, detail="API route is not available")

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
