"""Registry-bound OFIQ quality engine producing typed results and immutable Runs.

The engine never interprets an image itself: it launches the registered
OFIQSampleApp through :class:`RegisteredCLIAdapter` (which refuses BLOCKED
resources and unregistered paths before ``Popen``), parses the engine's own
output with :func:`parse_ofiq_stdout`, and assembles a
:class:`~qualsight.domain.run.CompletedRun` whose every provenance field is
observed at execution time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import getpass
import hashlib
from pathlib import Path
import struct
import time

from ...domain.entities import (
    AccessPolicy,
    Artifact,
    ArtifactKind,
    AuditEvent,
    AuditEventType,
    DataClassification,
    Dataset,
    Experiment,
    Modality,
    PermissionState,
    Project,
    ProjectLifecycle,
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
    CountEvidence,
    EvidenceState,
    EvidenceText,
    ResourceBinding,
)
from ...domain.run import (
    CompletedRun,
    LogRecord,
    MetricObservation,
    RunStatus,
    ScientificSupportState,
)
from ...execution.cli_runtime import RegisteredCLIAdapter
from ...execution.contracts import (
    ArtifactIdentity,
    ContractValidationError,
    ExecutionRecord,
    LifecycleState,
)
from ...execution.registry_binding import (
    OFIQ_V112_RUNTIME_RESOURCE_ID,
    RegisteredResource,
    ResourceRegistry,
    build_ofiq_transport_descriptor,
    load_resource_registry,
)
from ...provenance import (
    application_version,
    artifact_identity,
    environment_identity,
    hardware_identity,
    repository_identity,
    sha256_bytes,
)
from .output import OFIQAssessment, OFIQOutputError, OFIQRow, parse_ofiq_stdout


class OFIQEngineError(RuntimeError):
    """Raised when the engine ran but its result cannot be trusted as a Run."""


def content_id(kind: str, *parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return f"{kind}:sha256:{digest.hexdigest()}"


def read_png_metadata(path: Path) -> dict[str, int]:
    with path.open("rb") as handle:
        header = handle.read(29)
    if (
        len(header) != 29
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or struct.unpack(">I", header[8:12])[0] != 13
        or header[12:16] != b"IHDR"
    ):
        raise OFIQEngineError(f"registered input is not a PNG with a valid IHDR: {path}")
    width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", header[16:29]
    )
    return {
        "width": width,
        "height": height,
        "bit_depth": bit_depth,
        "color_type": color_type,
        "compression_method": compression,
        "filter_method": filtering,
        "interlace_method": interlace,
    }


def _jpeg_dimensions(path: Path) -> dict[str, int]:
    with path.open("rb") as handle:
        data = handle.read()
    if data[:2] != b"\xff\xd8":
        raise OFIQEngineError(f"registered input is not a JPEG: {path}")
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xC0, 0xC1, 0xC2}:
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return {"width": width, "height": height}
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        index += 2 + length
    raise OFIQEngineError(f"JPEG has no frame header: {path}")


def sample_metadata(path: Path) -> tuple[str, dict[str, int]]:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png", read_png_metadata(path)
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg", _jpeg_dimensions(path)
    raise OFIQEngineError(f"unsupported registered image type: {path}")


def _to_domain(identity: ArtifactIdentity) -> DomainArtifactIdentity:
    if identity.path is None:
        raise OFIQEngineError("registered artifact has no path")
    return DomainArtifactIdentity(
        path=identity.path,
        sha256=identity.sha256,
        size_bytes=identity.size_bytes,
        mtime_ns=identity.mtime_ns,
    )


def _not_applicable(reason: str) -> ComponentBinding:
    return ComponentBinding(state=ComponentState.NOT_APPLICABLE, reason=reason)


@dataclass(frozen=True, slots=True)
class OFIQSingleSampleResult:
    """One registered image assessed by the registered engine."""

    record: ExecutionRecord
    assessment: OFIQAssessment
    row: OFIQRow
    started_at: datetime
    ended_at: datetime
    runtime_seconds: float

    @property
    def succeeded(self) -> bool:
        return self.record.state is LifecycleState.SUCCEEDED and not self.row.row_failed


@dataclass(frozen=True, slots=True)
class SingleSampleRunBundle:
    """The immutable Run with the entities that give it scientific context."""

    project: Project
    dataset: Dataset
    sample: Sample
    experiment: Experiment
    run: CompletedRun
    audit_event: AuditEvent


class OFIQQualityEngine:
    """Execute the registered OFIQ runtime on registered images."""

    def __init__(
        self,
        *,
        registry_path: Path,
        repository_root: Path,
        runtime_resource_id: str = OFIQ_V112_RUNTIME_RESOURCE_ID,
        input_resource_ids: tuple[str, ...],
    ) -> None:
        self.registry_path = registry_path.resolve(strict=True)
        self.repository_root = repository_root.resolve(strict=True)
        self.registry: ResourceRegistry = load_resource_registry(self.registry_path)
        self.runtime: RegisteredResource = self.registry.resource(runtime_resource_id)
        self.input_resources: tuple[RegisteredResource, ...] = tuple(
            self.registry.resource(resource_id) for resource_id in input_resource_ids
        )
        # One CLI binding per input resource: a binding requires one common
        # approved scope across every resource it consumes, and each input
        # resource may carry a different grant (conformance-only vs. research).
        descriptor = build_ofiq_transport_descriptor(self.registry, runtime_resource_id)
        self.adapters: dict[str, RegisteredCLIAdapter] = {
            resource.resource_id: RegisteredCLIAdapter.from_registry(
                descriptor=descriptor,
                registry=self.registry,
                runtime_resource_id=runtime_resource_id,
                input_resource_ids=(resource.resource_id,),
                repository_root=self.repository_root,
            )
            for resource in self.input_resources
        }
        # Owner-supplied batches (decision 30) bind the runtime alone; scope
        # authority comes from the runtime resource's own approved scopes.
        self.owner_adapter: RegisteredCLIAdapter = RegisteredCLIAdapter.from_registry(
            descriptor=descriptor,
            registry=self.registry,
            runtime_resource_id=runtime_resource_id,
            input_resource_ids=(),
            repository_root=self.repository_root,
        )
        self.configuration = self.runtime.artifact("CONFIGURATION").identity
        self.executable = self.runtime.artifact("BINARY").identity

    # -- execution ------------------------------------------------------------

    def registered_image(self, path: str) -> tuple[RegisteredResource, ArtifactIdentity]:
        for resource in self.input_resources:
            for artifact in resource.artifacts:
                if artifact.role == "DATA" and artifact.identity.path == path:
                    return resource, artifact.identity
        raise ContractValidationError(f"path is not a registered DATA artifact: {path}")

    def adapter_for(self, path: str) -> RegisteredCLIAdapter:
        resource, _ = self.registered_image(path)
        return self.adapters[resource.resource_id]

    def assess(self, image_path: str, purpose_scope: str) -> OFIQSingleSampleResult:
        resource, image = self.registered_image(image_path)
        adapter = self.adapters[resource.resource_id]
        request = adapter.create_request(
            arguments=("-c", self.configuration.path, "-i", image.path),
            input_artifacts=(image,),
            purpose_scope=purpose_scope,
        )
        started_at = datetime.now(timezone.utc).astimezone()
        clock = time.perf_counter()
        record = adapter.start(adapter.prepare(request)).collect()
        runtime_seconds = float(time.perf_counter() - clock)
        ended_at = datetime.now(timezone.utc).astimezone()
        record.raise_for_status()
        stdout = record.stdout.content.decode("utf-8", errors="strict")
        stderr = record.stderr.content.decode("utf-8", errors="strict")
        try:
            assessment = parse_ofiq_stdout(stdout, stderr)
        except OFIQOutputError as error:
            raise OFIQEngineError(f"engine output could not be parsed: {error}") from error
        if assessment.engine_version != self.runtime.version:
            raise OFIQEngineError(
                f"engine reported version {assessment.engine_version}, "
                f"registry records {self.runtime.version}"
            )
        try:
            row = assessment.row_for(image.path)
        except KeyError as error:
            raise OFIQEngineError(f"engine reported no row for {image.path}") from error
        return OFIQSingleSampleResult(
            record=record,
            assessment=assessment,
            row=row,
            started_at=started_at,
            ended_at=ended_at,
            runtime_seconds=runtime_seconds,
        )

    # -- immutable Run ---------------------------------------------------------

    def build_run(
        self,
        result: OFIQSingleSampleResult,
        *,
        purpose_scope: str,
        actor_id: str | None = None,
        project: Project | None = None,
    ) -> SingleSampleRunBundle:
        """Assemble the immutable Run.

        When ``project`` is given (a user-created project from the project
        lifecycle), the Run, dataset, sample, and experiment are recorded under
        that project's identity; otherwise a content-addressed single-sample
        project derived from the registry, runtime, input, and scope is used.
        """
        if not result.succeeded:
            raise OFIQEngineError(
                "a Run is only recorded for a successful assessment; this result "
                f"failed: {result.row.failure_reason or result.record.state.value}"
            )
        input_resource, image = self.registered_image(result.row.filename)
        registry_identity = artifact_identity(self.registry_path)
        actor = actor_id or f"local-user:{getpass.getuser()}"
        runtime_record = self.registry.raw_resource(self.runtime.resource_id)
        input_record = self.registry.raw_resource(input_resource.resource_id)
        binary = _to_domain(self.executable)
        configuration = _to_domain(self.configuration)
        runtime_libraries = tuple(
            _to_domain(item.identity) for item in self.runtime.artifacts_with_role("RUNTIME_LIBRARY")
        )
        models = tuple(_to_domain(item.identity) for item in self.runtime.artifacts_with_role("MODEL"))
        licenses = self.registry.raw_resource(input_resource.resource_id).get("licenses", [])
        license_paths = [record["artifact_path"] for record in licenses] if licenses else []
        license_artifacts = [
            item.identity for item in input_resource.artifacts if item.role == "LICENSE"
        ]
        input_license = _to_domain(license_artifacts[0]) if license_artifacts else None
        image_identity = _to_domain(image)
        media_type, metadata = sample_metadata(Path(image.path))

        if project is not None:
            project_id = project.project_id
        else:
            project_id = content_id(
                "project", self.registry.registry_id, self.runtime.resource_id, input_resource.resource_id, purpose_scope
            )
        dataset_id = content_id("dataset", project_id, input_resource.resource_id, input_record["version"])
        sample_id = content_id("sample", dataset_id, image.sha256)
        experiment_id = content_id(
            "experiment", project_id, dataset_id, configuration.sha256, self.runtime.resource_id
        )
        stdout_text = result.record.stdout.content.decode("utf-8")
        stderr_text = result.record.stderr.content.decode("utf-8")
        run_id = content_id(
            "run",
            experiment_id,
            result.started_at.isoformat(),
            result.record.stdout.identity.sha256,
            result.record.stderr.identity.sha256,
            image.sha256,
            configuration.sha256,
            *(model.sha256 for model in models),
        )
        command = tuple(result.record.command)
        environment_values = dict(result.record.request.environment)

        project = project or Project(
            project_id=project_id,
            name=f"QualSight OFIQ single-sample quality ({input_resource.resource_id})",
            description=(
                "Local single-sample OFIQ execution of a registered image through the "
                "registered installed runtime under one common approved scope."
            ),
            lifecycle=ProjectLifecycle.SAVED,
            owner_user_id=actor,
            access_policy=AccessPolicy.LOCAL_SINGLE_USER,
            classification=DataClassification(input_record["data_classification"]),
            redistribution_permission=PermissionState.PROHIBITED,
            display_permission=PermissionState.LIMITED,
            use_restrictions=tuple(input_record["use_restrictions"]),
            tags=("OFIQ", "face-quality", "single-sample"),
            notes=tuple(input_record["use_restrictions"]),
            metadata=CanonicalJson.from_value(
                {
                    "approved_scope": purpose_scope,
                    "input_resource_id": input_resource.resource_id,
                    "registry_id": self.registry.registry_id,
                    "runtime_resource_id": self.runtime.resource_id,
                }
            ),
            created_at=result.started_at,
            updated_at=result.ended_at,
        )
        dataset_manifest = CanonicalJson.from_value(
            {
                "dataset_resource_id": input_resource.resource_id,
                "dataset_version": input_record["version"],
                "manifest_version": "1.0.0",
                "samples": [
                    {
                        "media_type": media_type,
                        "mtime_ns": image.mtime_ns,
                        "sample_id": sample_id,
                        "sha256": image.sha256,
                        "size_bytes": image.size_bytes,
                        "source_path": image.path,
                    }
                ],
                "selection_rule": "The registered DATA artifact selected by path for this single-sample execution.",
            }
        )
        dataset = Dataset(
            dataset_id=dataset_id,
            project_id=project_id,
            name=input_record["name"],
            version=input_record["version"],
            source_uri=input_record["source"]["uri"],
            license=input_license,
            rights_status=input_record["third_party_rights_status"],
            rights_evidence=tuple(
                _to_domain(item.identity)
                for item in input_resource.artifacts
                if item.role == "AUTHORIZATION"
            ),
            retrieval=EvidenceText(
                state=EvidenceState.NOT_RECORDED,
                reason="The registered resource records no upstream retrieval date for this input.",
            ),
            manifest=dataset_manifest,
            subject_count=CountEvidence(
                state=EvidenceState.NOT_RECORDED,
                reason="Single-sample quality execution carries no subject mapping.",
            ),
            sample_count=CountEvidence(state=EvidenceState.RECORDED, value=1),
            modality=Modality.FACE,
            identity_schema=_not_applicable("Single-sample OFIQ quality execution does not use identity labels."),
            metadata_schema=_not_applicable("No metadata table participates in this single-image execution."),
            redistribution_permission=PermissionState.PROHIBITED,
            display_permission=PermissionState.LIMITED,
            derived_artifact_policy=(
                "Record measurements and hashes under the approved scope; do not copy or redistribute the registered image."
            ),
            classification=DataClassification(input_record["data_classification"]),
        )
        sample = Sample(
            sample_id=sample_id,
            dataset_id=dataset_id,
            subject_id=EvidenceText(
                state=EvidenceState.NOT_APPLICABLE,
                reason="The registered input has no subject identity mapping.",
            ),
            modality=Modality.FACE,
            source_path=image.path,
            media_type=media_type,
            sha256=image.sha256,
            size_bytes=image.size_bytes,
            mtime_ns=image.mtime_ns,
            metadata=CanonicalJson.from_value({**metadata, "resource_id": input_resource.resource_id}),
        )
        execution_configuration = CanonicalJson.from_value(
            {
                "approved_scope": purpose_scope,
                "command": list(command),
                "environment": environment_values,
                "input_resource": {
                    "resource_id": input_resource.resource_id,
                    "state": input_record["state"],
                    "version": input_record["version"],
                },
                "runtime_resource": {
                    "resource_id": self.runtime.resource_id,
                    "state": runtime_record["state"],
                    "version": runtime_record["version"],
                },
                "engine_reported_version": result.assessment.engine_version,
                "measure_names": list(result.assessment.measure_names),
            }
        )
        experiment = Experiment(
            experiment_id=experiment_id,
            project_id=project_id,
            dataset_id=dataset_id,
            name="OFIQ single-sample quality execution",
            created_by=actor,
            created_at=result.started_at,
            protocol=SingleSampleQualityProtocol(
                kind=ProtocolKind.SINGLE_SAMPLE_QUALITY,
                sample_ids=(sample_id,),
            ),
            configuration=execution_configuration,
        )
        artifact = Artifact(
            artifact_id=content_id("artifact", run_id, image.sha256),
            run_id=run_id,
            kind=ArtifactKind.SOURCE_INPUT,
            storage_backend=StorageBackend.FILESYSTEM,
            uri=image.path,
            sha256=image.sha256,
            size_bytes=image.size_bytes,
            media_type=media_type,
            classification=DataClassification(input_record["data_classification"]),
            created_at=result.ended_at,
        )
        metrics = tuple(
            MetricObservation(
                metric_id=f"{measure.name}.{kind}",
                value=value,
                unit=kind,
            )
            for measure in result.row.measures
            for kind, value in (("native", measure.native), ("scalar", measure.scalar))
            if not measure.failed and value is not None
        )
        failed_measures = tuple(
            f"engine reported no valid value for {name}" for name in result.row.failed_measure_names
        )
        run = CompletedRun(
            run_id=run_id,
            project_id=project_id,
            experiment_id=experiment_id,
            user_id=actor,
            dataset_manifest=dataset_manifest,
            dataset_hash=dataset_manifest.sha256,
            subject_protocol=_not_applicable("The executed single-sample protocol has no subject mapping."),
            pair_search_protocol=_not_applicable(
                "The executed protocol scores one sample and performs no comparison or search."
            ),
            quality_engine=ResourceBinding(
                resource_id=self.runtime.resource_id,
                version=self.runtime.version or runtime_record["version"],
                registry_state=runtime_record["state"],
                artifacts=(binary, *runtime_libraries, *models),
            ),
            quality_engine_configuration=configuration,
            model_hashes=models,
            matcher=_not_applicable("The executed OFIQ quality command invokes no matcher."),
            matcher_preprocessing=_not_applicable("The executed OFIQ quality command has no matcher preprocessing stage."),
            operating_thresholds=_not_applicable(
                "The executed command emits quality measurements without a decision threshold."
            ),
            statistical_methods=_not_applicable(
                "This per-sample execution applies no inferential statistical method."
            ),
            confidence_interval_method=_not_applicable("This per-sample execution computes no confidence interval."),
            randomness=_not_applicable(
                "The executed command and registered configuration contain no random-seed setting."
            ),
            random_seeds=(),
            application_version=application_version(self.repository_root / "backend" / "pyproject.toml"),
            repository=repository_identity(self.repository_root),
            environment=environment_identity(self.repository_root / "uv.lock", environment_values),
            hardware=hardware_identity("OFIQSampleApp output does not identify an execution device."),
            start_time=result.started_at,
            end_time=result.ended_at,
            runtime_seconds=result.runtime_seconds,
            initialization_ms=result.assessment.initialization_ms,
            assessment_ms=result.row.assessment_ms,
            configuration=execution_configuration,
            metrics=metrics,
            artifacts=(artifact,),
            logs=(
                LogRecord(stream="stdout", sha256=sha256_bytes(stdout_text.encode("utf-8")), text=stdout_text),
                LogRecord(stream="stderr", sha256=sha256_bytes(stderr_text.encode("utf-8")), text=stderr_text),
            ),
            warnings=tuple(line for line in stderr_text.splitlines() if line) + failed_measures,
            provenance_limits=tuple(runtime_record["use_restrictions"]) + tuple(input_record["use_restrictions"]),
            exit_code=0,
            status=RunStatus.COMPLETED,
            scientific_support=ScientificSupportState.NOT_ASSESSED,
        )
        run_evidence_sha256 = sha256_bytes(run.model_dump_json().encode("utf-8"))
        audit_event = AuditEvent(
            audit_event_id=content_id("audit-event", run_id, run_evidence_sha256),
            project_id=project_id,
            actor_id=actor,
            event_type=AuditEventType.RUN_COMPLETED,
            subject_type="Run",
            subject_id=run_id,
            occurred_at=result.ended_at,
            details=CanonicalJson.from_value(
                {
                    "exit_code": 0,
                    "stderr_sha256": result.record.stderr.identity.sha256,
                    "stdout_sha256": result.record.stdout.identity.sha256,
                    "status": RunStatus.COMPLETED,
                    "registry_sha256": registry_identity.sha256,
                }
            ),
            current_evidence_sha256=run_evidence_sha256,
            local_only=True,
        )
        return SingleSampleRunBundle(
            project=project,
            dataset=dataset,
            sample=sample,
            experiment=experiment,
            run=run,
            audit_event=audit_event,
        )
