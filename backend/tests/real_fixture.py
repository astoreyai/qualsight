from __future__ import annotations

import argparse
import csv
from datetime import datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys
import time
import tomllib
from typing import Any, Literal

from pydantic import model_validator
from pydantic.version import VERSION as PYDANTIC_VERSION

from qualsight.domain.entities import (
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
from qualsight.domain.primitives import (
    ArtifactIdentity,
    AwareDateTime,
    CanonicalJson,
    ComponentBinding,
    ComponentState,
    CountEvidence,
    EnvironmentIdentity,
    EvidenceState,
    EvidenceText,
    HardwareIdentity,
    KeyValue,
    NonEmptyStr,
    RepositoryIdentity,
    RepositoryState,
    ResourceBinding,
    StrictFrozenModel,
)
from qualsight.domain.run import (
    CompletedRun,
    LogRecord,
    MetricObservation,
    RunStatus,
    ScientificSupportState,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = PROJECT_ROOT / "resources" / "resource-registry.json"
FIXTURE_PATH = PROJECT_ROOT / "backend/tests/fixtures/ofiq_r01_completed_run.json"
RUNTIME_RESOURCE_ID = "ofiq-v1-1-0-build-runtime"
INPUT_RESOURCE_ID = "ofiq-r-01-conformance-input"
APPROVED_SCOPE = "REPRODUCE_OFIQ_CONFORMANCE_TESTS"


class FixtureProvenance(StrictFrozenModel):
    captured_at: AwareDateTime
    capture_completed_at: AwareDateTime
    registry: ArtifactIdentity
    registry_preservation: EvidenceText
    registry_id: NonEmptyStr
    registry_recorded_at: AwareDateTime
    runtime_resource_id: NonEmptyStr
    input_resource_id: NonEmptyStr
    approved_scope: NonEmptyStr
    command: tuple[NonEmptyStr, ...]
    capture_tool: ArtifactIdentity | None
    capture_tool_evidence: EvidenceText
    verification_tool: ArtifactIdentity
    source_license: ArtifactIdentity

    @model_validator(mode="after")
    def validate_capture_tool_evidence(self) -> FixtureProvenance:
        if self.registry_preservation.state is EvidenceState.RECORDED:
            if self.registry_preservation.value != self.registry.sha256:
                raise ValueError(
                    "recorded registry preservation must identify the registry SHA-256"
                )
        elif self.registry_preservation.state is not EvidenceState.NOT_RECORDED:
            raise ValueError(
                "registry preservation must be RECORDED or NOT_RECORDED"
            )
        if self.capture_tool is None:
            if self.capture_tool_evidence.state is not EvidenceState.NOT_RECORDED:
                raise ValueError(
                    "missing capture-tool bytes require NOT_RECORDED evidence"
                )
        else:
            if (
                self.capture_tool_evidence.state is not EvidenceState.RECORDED
                or self.capture_tool_evidence.value != self.capture_tool.sha256
            ):
                raise ValueError(
                    "recorded capture-tool evidence must identify the tool SHA-256"
                )
            capture_completed_ns = int(
                self.capture_completed_at.timestamp() * 1_000_000_000
            )
            if self.capture_tool.mtime_ns > capture_completed_ns:
                raise ValueError("capture-tool bytes postdate capture completion")
        return self


class RealCapturedFixture(StrictFrozenModel):
    fixture_schema_version: Literal["1.2.0"]
    provenance: FixtureProvenance
    project: Project
    dataset: Dataset
    sample: Sample
    experiment: Experiment
    run: CompletedRun
    audit_event: AuditEvent

    @model_validator(mode="after")
    def validate_evidence_graph(self) -> RealCapturedFixture:
        project_id = self.project.project_id
        dataset_id = self.dataset.dataset_id
        sample_id = self.sample.sample_id
        experiment_id = self.experiment.experiment_id
        run_id = self.run.run_id
        actor_id = self.project.owner_user_id

        if self.dataset.project_id != project_id:
            raise ValueError("Dataset does not belong to the captured Project")
        if self.sample.dataset_id != dataset_id:
            raise ValueError("Sample does not belong to the captured Dataset")
        if (
            self.experiment.project_id != project_id
            or self.experiment.dataset_id != dataset_id
        ):
            raise ValueError("Experiment does not belong to the captured Project/Dataset")
        if not isinstance(self.experiment.protocol, SingleSampleQualityProtocol):
            raise ValueError("captured OFIQ fixture requires a single-sample protocol")
        if self.experiment.protocol.sample_ids != (sample_id,):
            raise ValueError("Experiment protocol does not identify the captured Sample")
        if (
            self.run.project_id != project_id
            or self.run.experiment_id != experiment_id
            or self.run.user_id != actor_id
        ):
            raise ValueError("Run does not link to the captured Project/Experiment/actor")
        if self.experiment.created_by != actor_id:
            raise ValueError("Experiment actor differs from the captured Project owner")
        if (
            self.audit_event.project_id != project_id
            or self.audit_event.subject_id != run_id
            or self.audit_event.actor_id != actor_id
            or self.audit_event.event_type is not AuditEventType.RUN_COMPLETED
            or self.audit_event.subject_type != "Run"
        ):
            raise ValueError("Audit event does not identify the completed captured Run")
        if self.run.quality_engine.resource_id != self.provenance.runtime_resource_id:
            raise ValueError("Run quality engine differs from captured runtime provenance")
        if self.dataset.license != self.provenance.source_license:
            raise ValueError("Dataset license differs from captured source-license evidence")

        if not (
            self.provenance.captured_at
            == self.project.created_at
            == self.experiment.created_at
            == self.run.start_time
        ):
            raise ValueError("capture start chronology is inconsistent")
        if not (
            self.provenance.capture_completed_at
            == self.project.updated_at
            == self.run.end_time
            == self.audit_event.occurred_at
        ):
            raise ValueError("capture completion chronology is inconsistent")

        if self.project.classification is not self.dataset.classification:
            raise ValueError("Project classification weakens its Dataset classification")
        if (
            self.project.redistribution_permission
            is not self.dataset.redistribution_permission
            or self.project.display_permission is not self.dataset.display_permission
        ):
            raise ValueError("Project permissions differ from its Dataset permissions")
        if any(
            artifact.classification is not self.dataset.classification
            for artifact in self.run.artifacts
        ):
            raise ValueError("Run artifact classification differs from its Dataset")

        manifest = json.loads(self.dataset.manifest.text)
        expected_manifest = {
            "dataset_resource_id": self.provenance.input_resource_id,
            "dataset_version": self.dataset.version,
            "manifest_version": "1.0.0",
            "samples": [
                {
                    "media_type": self.sample.media_type,
                    "mtime_ns": self.sample.mtime_ns,
                    "sample_id": sample_id,
                    "sha256": self.sample.sha256,
                    "size_bytes": self.sample.size_bytes,
                    "source_path": self.sample.source_path,
                }
            ],
            "selection_rule": (
                "The sole DATA artifact registered for the approved OFIQ r-01 "
                "conformance resource."
            ),
        }
        if manifest != expected_manifest:
            raise ValueError("Dataset manifest does not identify the captured real Sample")
        if self.run.dataset_manifest != self.dataset.manifest:
            raise ValueError("Run and Dataset use different canonical manifests")

        execution_configuration = json.loads(self.run.configuration.text)
        if execution_configuration != json.loads(self.experiment.configuration.text):
            raise ValueError("Run and Experiment use different execution configurations")
        runtime_configuration = execution_configuration.get("runtime_resource")
        input_configuration = execution_configuration.get("input_resource")
        if not isinstance(runtime_configuration, dict) or (
            runtime_configuration.get("resource_id")
            != self.run.quality_engine.resource_id
            or runtime_configuration.get("version") != self.run.quality_engine.version
            or runtime_configuration.get("state")
            != self.run.quality_engine.registry_state
        ):
            raise ValueError("execution configuration contradicts runtime provenance")
        if not isinstance(input_configuration, dict) or (
            input_configuration.get("resource_id") != self.provenance.input_resource_id
            or input_configuration.get("version") != self.dataset.version
        ):
            raise ValueError("execution configuration contradicts input provenance")
        if execution_configuration.get("approved_scope") != self.provenance.approved_scope:
            raise ValueError("execution configuration contradicts the approved scope")
        if tuple(execution_configuration.get("command", ())) != self.provenance.command:
            raise ValueError("execution configuration contradicts the captured command")

        run_evidence_sha256 = _sha256_bytes(
            self.run.model_dump_json().encode("utf-8")
        )
        if self.audit_event.current_evidence_sha256 != run_evidence_sha256:
            raise ValueError("Audit event digest does not identify the captured Run")
        return self


class ExecutionObservation(StrictFrozenModel):
    start_time: AwareDateTime
    end_time: AwareDateTime
    runtime_seconds: float
    initialization_ms: int
    assessment_ms: int
    engine_version: NonEmptyStr
    input_path: NonEmptyStr
    metrics: tuple[MetricObservation, ...]
    stdout: str
    stderr: str
    exit_code: Literal[0]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _artifact_identity(path: Path) -> ArtifactIdentity:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"artifact is not a regular file: {resolved}")
    stat = resolved.stat()
    return ArtifactIdentity(
        path=str(resolved),
        sha256=_sha256_file(resolved),
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def _registry_artifact(value: dict[str, Any]) -> ArtifactIdentity:
    return ArtifactIdentity(
        path=value["path"],
        sha256=value["sha256"],
        size_bytes=value["size_bytes"],
        mtime_ns=value["mtime_ns"],
    )


def _assert_identity(identity: ArtifactIdentity) -> None:
    observed = _artifact_identity(Path(identity.path))
    if observed != identity:
        raise ValueError(
            f"artifact identity drift: {identity.path}; "
            f"recorded={identity.model_dump(mode='json')}; "
            f"observed={observed.model_dump(mode='json')}"
        )


def _load_registry(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("resources"), list):
        raise ValueError(f"resource registry has no resource array: {path}")
    return value


def _resource(registry: dict[str, Any], resource_id: str) -> dict[str, Any]:
    matches = [
        value
        for value in registry["resources"]
        if value.get("resource_id") == resource_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one registry resource {resource_id}; found {len(matches)}"
        )
    return matches[0]


def _require_execution_authorization(
    runtime_resource: dict[str, Any],
    input_resource: dict[str, Any],
) -> None:
    for resource in (runtime_resource, input_resource):
        if (
            resource.get("state") != "VERIFIED_LIMITED_USE"
            or APPROVED_SCOPE not in resource.get("approved_scopes", ())
        ):
            raise ValueError(
                f"resource {resource.get('resource_id')} is "
                f"{resource.get('state')} and does not approve {APPROVED_SCOPE}"
            )


def _artifacts_by_role(
    resource: dict[str, Any],
    roles: set[str],
) -> tuple[ArtifactIdentity, ...]:
    values = tuple(
        _registry_artifact(value)
        for value in resource["artifacts"]
        if value["role"] in roles
    )
    if not values:
        raise ValueError(
            f"resource {resource['resource_id']} has no artifact with role in {roles}"
        )
    return values


def _one_artifact(resource: dict[str, Any], role: str) -> ArtifactIdentity:
    values = _artifacts_by_role(resource, {role})
    if len(values) != 1:
        raise ValueError(
            f"resource {resource['resource_id']} has {len(values)} {role} artifacts"
        )
    return values[0]


def _content_id(kind: str, *parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return f"{kind}:sha256:{digest.hexdigest()}"


def _read_png_metadata(path: Path) -> dict[str, int]:
    with path.open("rb") as handle:
        header = handle.read(29)
    if (
        len(header) != 29
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or struct.unpack(">I", header[8:12])[0] != 13
        or header[12:16] != b"IHDR"
    ):
        raise ValueError(f"registered input is not a PNG with a valid IHDR: {path}")
    width, height, bit_depth, color_type, compression, filtering, interlace = (
        struct.unpack(">IIBBBBB", header[16:29])
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


def _parse_metric_value(text: str) -> int | float:
    if re.fullmatch(r"-?[0-9]+", text):
        return int(text)
    return float(text)


def _parse_ofiq_output(
    stdout: str,
) -> tuple[int, int, str, str, tuple[MetricObservation, ...]]:
    lines = stdout.splitlines()
    initialization = next(
        (
            int(match.group(1))
            for line in lines
            if (match := re.fullmatch(r"\[INFO\] Initialization took: ([0-9]+)ms", line))
        ),
        None,
    )
    engine_version = next(
        (
            match.group(1)
            for line in lines
            if (match := re.fullmatch(r"OFIQ library version: (\S+)", line))
        ),
        None,
    )
    table_start = next(
        (index for index, line in enumerate(lines) if line.startswith("Filename;")),
        None,
    )
    if initialization is None or engine_version is None or table_start is None:
        raise ValueError("OFIQ output is missing initialization, version, or result table")
    if table_start + 1 >= len(lines):
        raise ValueError("OFIQ output has no result row")
    rows = list(
        csv.reader(
            [lines[table_start], lines[table_start + 1]],
            delimiter=";",
            strict=True,
        )
    )
    header, values = rows
    while header and header[-1] == "":
        header.pop()
    while values and values[-1] == "":
        values.pop()
    if len(header) != len(values) or header[-1] != "assessment_time_in_ms":
        raise ValueError("OFIQ result columns and values do not align")
    if len(header) != len(set(header)):
        raise ValueError("OFIQ result contains duplicate column names")
    record = dict(zip(header, values, strict=True))
    input_path = record.pop("Filename")
    assessment = int(record.pop("assessment_time_in_ms"))
    metrics = tuple(
        MetricObservation(
            metric_id=metric_id,
            value=_parse_metric_value(value),
            unit="scalar" if metric_id.endswith(".scalar") else "native",
        )
        for metric_id, value in record.items()
    )
    return initialization, assessment, engine_version, input_path, metrics


def _execute_ofiq(
    command: tuple[str, ...],
    environment_values: dict[str, str],
) -> ExecutionObservation:
    environment = os.environ.copy()
    environment.update(environment_values)
    start_time = datetime.now().astimezone()
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=environment,
    )
    runtime_seconds = float(time.perf_counter() - started)
    end_time = datetime.now().astimezone()
    if completed.returncode != 0:
        raise RuntimeError(
            f"OFIQ execution exited {completed.returncode}: {completed.stderr}"
        )
    initialization, assessment, engine_version, input_path, metrics = (
        _parse_ofiq_output(completed.stdout)
    )
    return ExecutionObservation(
        start_time=start_time,
        end_time=end_time,
        runtime_seconds=runtime_seconds,
        initialization_ms=initialization,
        assessment_ms=assessment,
        engine_version=engine_version,
        input_path=input_path,
        metrics=metrics,
        stdout=completed.stdout,
        stderr=completed.stderr,
        exit_code=0,
    )


def _repository_identity() -> RepositoryIdentity:
    commit_result = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--verify", "HEAD"],
        check=False,
        capture_output=True,
    )
    status_result = subprocess.run(
        [
            "git",
            "-C",
            str(PROJECT_ROOT),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
        check=True,
        capture_output=True,
    )
    if commit_result.returncode == 0:
        state = RepositoryState.COMMITTED
        commit = commit_result.stdout.decode("ascii").strip()
    else:
        state = RepositoryState.UNBORN
        commit = None
    return RepositoryIdentity(
        root=str(PROJECT_ROOT.resolve()),
        state=state,
        commit=commit,
        dirty=bool(status_result.stdout),
        status_sha256=_sha256_bytes(status_result.stdout),
    )


def _not_applicable(reason: str) -> ComponentBinding:
    return ComponentBinding(
        state=ComponentState.NOT_APPLICABLE,
        reason=reason,
    )


def _application_version() -> str:
    value = tomllib.loads(
        (PROJECT_ROOT / "backend/pyproject.toml").read_text(encoding="utf-8")
    )
    return str(value["project"]["version"])


def capture_fixture(
    registry_path: Path,
    runtime_resource_id: str,
    input_resource_id: str,
) -> RealCapturedFixture:
    registry_path = registry_path.resolve(strict=True)
    registry = _load_registry(registry_path)
    runtime_resource = _resource(registry, runtime_resource_id)
    input_resource = _resource(registry, input_resource_id)
    _require_execution_authorization(runtime_resource, input_resource)

    registry_identity = _artifact_identity(registry_path)
    capture_tool = _artifact_identity(Path(__file__))
    binary = _one_artifact(runtime_resource, "BINARY")
    configuration = _one_artifact(runtime_resource, "CONFIGURATION")
    runtime_libraries = _artifacts_by_role(runtime_resource, {"RUNTIME_LIBRARY"})
    models = _artifacts_by_role(runtime_resource, {"MODEL"})
    image = _one_artifact(input_resource, "DATA")
    input_license = _one_artifact(input_resource, "LICENSE")
    runtime_artifacts = (binary, *runtime_libraries, *models)
    for identity in (
        registry_identity,
        capture_tool,
        configuration,
        image,
        input_license,
        *runtime_artifacts,
    ):
        _assert_identity(identity)

    environment_values = dict(runtime_resource["runtime_requirements"]["environment"])
    command = (
        binary.path,
        "-c",
        configuration.path,
        "-i",
        image.path,
    )
    observation = _execute_ofiq(command, environment_values)
    if observation.input_path != image.path:
        raise ValueError(
            f"OFIQ reported input {observation.input_path}, expected {image.path}"
        )
    if observation.engine_version != runtime_resource["version"]:
        raise ValueError(
            f"OFIQ reported version {observation.engine_version}, "
            f"registry records {runtime_resource['version']}"
        )

    actor_id = f"local-user:{getpass.getuser()}"
    project_id = _content_id(
        "project",
        registry["registry_id"],
        runtime_resource_id,
        input_resource_id,
        APPROVED_SCOPE,
    )
    dataset_id = _content_id(
        "dataset",
        project_id,
        input_resource_id,
        input_resource["version"],
    )
    sample_id = _content_id("sample", dataset_id, image.sha256)
    experiment_id = _content_id(
        "experiment",
        project_id,
        dataset_id,
        configuration.sha256,
        runtime_resource_id,
    )
    run_id = _content_id(
        "run",
        experiment_id,
        observation.start_time.isoformat(),
        _sha256_bytes(observation.stdout.encode("utf-8")),
        _sha256_bytes(observation.stderr.encode("utf-8")),
        image.sha256,
        configuration.sha256,
        *(model.sha256 for model in models),
    )

    project = Project(
        project_id=project_id,
        name="QualSight OFIQ r-01 conformance reproduction",
        description=(
            "Local single-sample OFIQ execution using the registered r-01 "
            "conformance input and build-tree runtime."
        ),
        lifecycle=ProjectLifecycle.SAVED,
        owner_user_id=actor_id,
        access_policy=AccessPolicy.LOCAL_SINGLE_USER,
        classification=DataClassification.RESTRICTED,
        redistribution_permission=PermissionState.PROHIBITED,
        display_permission=PermissionState.LIMITED,
        use_restrictions=tuple(input_resource["use_restrictions"]),
        tags=("OFIQ", "conformance", "face-quality"),
        notes=tuple(input_resource["use_restrictions"]),
        metadata=CanonicalJson.from_value(
            {
                "approved_scope": APPROVED_SCOPE,
                "input_resource_id": input_resource_id,
                "registry_id": registry["registry_id"],
                "runtime_resource_id": runtime_resource_id,
            }
        ),
        created_at=observation.start_time,
        updated_at=observation.end_time,
    )
    dataset_manifest = CanonicalJson.from_value(
        {
            "dataset_resource_id": input_resource_id,
            "dataset_version": input_resource["version"],
            "manifest_version": "1.0.0",
            "samples": [
                {
                    "media_type": "image/png",
                    "mtime_ns": image.mtime_ns,
                    "sample_id": sample_id,
                    "sha256": image.sha256,
                    "size_bytes": image.size_bytes,
                    "source_path": image.path,
                }
            ],
            "selection_rule": (
                "The sole DATA artifact registered for the approved OFIQ r-01 "
                "conformance resource."
            ),
        }
    )
    dataset = Dataset(
        dataset_id=dataset_id,
        project_id=project_id,
        name=input_resource["name"],
        version=input_resource["version"],
        source_uri=input_resource["source"]["uri"],
        license=input_license,
        retrieval=EvidenceText(
            state=EvidenceState.NOT_RECORDED,
            reason="The registered resource records no upstream retrieval date.",
        ),
        manifest=dataset_manifest,
        subject_count=CountEvidence(
            state=EvidenceState.NOT_RECORDED,
            reason="The registered resource records no subject mapping.",
        ),
        sample_count=CountEvidence(state=EvidenceState.RECORDED, value=1),
        modality=Modality.FACE,
        identity_schema=_not_applicable(
            "Single-sample OFIQ quality execution does not use identity labels."
        ),
        metadata_schema=_not_applicable(
            "No metadata table participates in this registered single-image execution."
        ),
        redistribution_permission=PermissionState.PROHIBITED,
        display_permission=PermissionState.LIMITED,
        derived_artifact_policy=(
            "Record measurements and hashes for the approved conformance scope; "
            "do not copy or redistribute the registered image."
        ),
        classification=DataClassification.RESTRICTED,
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
        media_type="image/png",
        sha256=image.sha256,
        size_bytes=image.size_bytes,
        mtime_ns=image.mtime_ns,
        metadata=CanonicalJson.from_value(
            {
                **_read_png_metadata(Path(image.path)),
                "resource_id": input_resource_id,
            }
        ),
    )
    execution_configuration = CanonicalJson.from_value(
        {
            "approved_scope": APPROVED_SCOPE,
            "command": command,
            "environment": environment_values,
            "input_resource": {
                "resource_id": input_resource_id,
                "state": input_resource["state"],
                "version": input_resource["version"],
            },
            "runtime_resource": {
                "blocked_reasons": runtime_resource["blocked_reasons"],
                "resource_id": runtime_resource_id,
                "state": runtime_resource["state"],
                "version": runtime_resource["version"],
            },
        }
    )
    experiment = Experiment(
        experiment_id=experiment_id,
        project_id=project_id,
        dataset_id=dataset_id,
        name="OFIQ r-01 single-sample quality execution",
        created_by=actor_id,
        created_at=observation.start_time,
        protocol=SingleSampleQualityProtocol(
            kind=ProtocolKind.SINGLE_SAMPLE_QUALITY,
            sample_ids=(sample_id,),
        ),
        configuration=execution_configuration,
    )
    artifact = Artifact(
        artifact_id=_content_id("artifact", run_id, image.sha256),
        run_id=run_id,
        kind=ArtifactKind.SOURCE_INPUT,
        storage_backend=StorageBackend.FILESYSTEM,
        uri=image.path,
        sha256=image.sha256,
        size_bytes=image.size_bytes,
        media_type="image/png",
        classification=DataClassification.RESTRICTED,
        created_at=observation.end_time,
    )
    dependency_manifest = _artifact_identity(PROJECT_ROOT / "uv.lock")
    repository = _repository_identity()
    run = CompletedRun(
        run_id=run_id,
        project_id=project_id,
        experiment_id=experiment_id,
        user_id=actor_id,
        dataset_manifest=dataset_manifest,
        dataset_hash=dataset_manifest.sha256,
        subject_protocol=_not_applicable(
            "The executed single-sample protocol has no subject mapping."
        ),
        pair_search_protocol=_not_applicable(
            "The executed protocol scores one sample and performs no comparison or search."
        ),
        quality_engine=ResourceBinding(
            resource_id=runtime_resource_id,
            version=runtime_resource["version"],
            registry_state=runtime_resource["state"],
            artifacts=runtime_artifacts,
        ),
        quality_engine_configuration=configuration,
        model_hashes=models,
        matcher=_not_applicable("The executed OFIQ quality command invokes no matcher."),
        matcher_preprocessing=_not_applicable(
            "The executed OFIQ quality command has no matcher preprocessing stage."
        ),
        operating_thresholds=_not_applicable(
            "The executed command emits quality measurements without a decision threshold."
        ),
        statistical_methods=_not_applicable(
            "This captured per-sample execution applies no inferential statistical method."
        ),
        confidence_interval_method=_not_applicable(
            "This captured per-sample execution computes no confidence interval."
        ),
        randomness=_not_applicable(
            "The executed command and registered configuration contain no random-seed setting."
        ),
        random_seeds=(),
        application_version=_application_version(),
        repository=repository,
        environment=EnvironmentIdentity(
            operating_system=platform.platform(),
            hostname=platform.node(),
            machine=platform.machine(),
            python_version=platform.python_version(),
            pydantic_version=PYDANTIC_VERSION,
            dependency_manifest=dependency_manifest,
            variables=tuple(
                KeyValue(key=key, value=value)
                for key, value in sorted(environment_values.items())
            ),
        ),
        hardware=HardwareIdentity(
            platform=sys.platform,
            architecture=platform.machine(),
            logical_cpu_count=os.cpu_count() or 1,
            device=EvidenceText(
                state=EvidenceState.NOT_RECORDED,
                reason="OFIQSampleApp output does not identify an execution device.",
            ),
        ),
        start_time=observation.start_time,
        end_time=observation.end_time,
        runtime_seconds=observation.runtime_seconds,
        initialization_ms=observation.initialization_ms,
        assessment_ms=observation.assessment_ms,
        configuration=execution_configuration,
        metrics=observation.metrics,
        artifacts=(artifact,),
        logs=(
            LogRecord(
                stream="stdout",
                sha256=_sha256_bytes(observation.stdout.encode("utf-8")),
                text=observation.stdout,
            ),
            LogRecord(
                stream="stderr",
                sha256=_sha256_bytes(observation.stderr.encode("utf-8")),
                text=observation.stderr,
            ),
        ),
        warnings=tuple(line for line in observation.stderr.splitlines() if line),
        provenance_limits=tuple(
            runtime_resource["blocked_reasons"]
            + runtime_resource["use_restrictions"]
            + input_resource["use_restrictions"]
        ),
        exit_code=0,
        status=RunStatus.COMPLETED,
        scientific_support=ScientificSupportState.NOT_ASSESSED,
    )
    run_evidence_sha256 = _sha256_bytes(run.model_dump_json().encode("utf-8"))
    audit_event = AuditEvent(
        audit_event_id=_content_id("audit-event", run_id, run_evidence_sha256),
        project_id=project_id,
        actor_id=actor_id,
        event_type=AuditEventType.RUN_COMPLETED,
        subject_type="Run",
        subject_id=run_id,
        occurred_at=observation.end_time,
        details=CanonicalJson.from_value(
            {
                "exit_code": observation.exit_code,
                "stderr_sha256": _sha256_bytes(observation.stderr.encode("utf-8")),
                "stdout_sha256": _sha256_bytes(observation.stdout.encode("utf-8")),
                "status": RunStatus.COMPLETED,
            }
        ),
        current_evidence_sha256=run_evidence_sha256,
        local_only=True,
    )
    return RealCapturedFixture(
        fixture_schema_version="1.2.0",
        provenance=FixtureProvenance(
            captured_at=observation.start_time,
            capture_completed_at=observation.end_time,
            registry=registry_identity,
            registry_preservation=EvidenceText(
                state=EvidenceState.RECORDED,
                value=registry_identity.sha256,
            ),
            registry_id=registry["registry_id"],
            registry_recorded_at=datetime.fromisoformat(registry["recorded_at"]),
            runtime_resource_id=runtime_resource_id,
            input_resource_id=input_resource_id,
            approved_scope=APPROVED_SCOPE,
            command=command,
            capture_tool=capture_tool,
            capture_tool_evidence=EvidenceText(
                state=EvidenceState.RECORDED,
                value=capture_tool.sha256,
            ),
            verification_tool=capture_tool,
            source_license=input_license,
        ),
        project=project,
        dataset=dataset,
        sample=sample,
        experiment=experiment,
        run=run,
        audit_event=audit_event,
    )


def load_fixture(path: Path) -> RealCapturedFixture:
    return RealCapturedFixture.model_validate_json(path.read_text(encoding="utf-8"))


def verify_recorded_bytes(fixture: RealCapturedFixture) -> int:
    sample_identity = ArtifactIdentity(
        path=fixture.sample.source_path,
        sha256=fixture.sample.sha256,
        size_bytes=fixture.sample.size_bytes,
        mtime_ns=fixture.sample.mtime_ns,
    )
    # The Python dependency manifest is environment evidence, not a scientific
    # input: it legitimately evolves as the application grows, so the historical
    # Run keeps its recorded identity and dependency_manifest_is_current()
    # reports whether the live lock still matches it.
    identities = (
        fixture.provenance.verification_tool,
        fixture.provenance.source_license,
        fixture.dataset.license,
        sample_identity,
        fixture.run.quality_engine_configuration,
        *fixture.run.quality_engine.artifacts,
        *fixture.run.model_hashes,
    )
    if fixture.provenance.capture_tool is not None:
        identities = (fixture.provenance.capture_tool, *identities)
    if fixture.provenance.registry_preservation.state is EvidenceState.RECORDED:
        identities = (fixture.provenance.registry, *identities)
    unique: dict[Path, ArtifactIdentity] = {}
    for identity in identities:
        resolved = Path(identity.path).resolve()
        prior = unique.get(resolved)
        if prior is not None and prior != identity:
            raise ValueError(f"conflicting identities for recorded path: {resolved}")
        unique[resolved] = identity
    for identity in unique.values():
        _assert_identity(identity)
    artifact_by_hash = {
        identity.sha256: identity
        for identity in unique.values()
    }
    for artifact in fixture.run.artifacts:
        identity = artifact_by_hash.get(artifact.sha256)
        if identity is None or Path(identity.path).resolve() != Path(artifact.uri).resolve():
            raise ValueError(f"Run artifact has no byte identity: {artifact.artifact_id}")
        if identity.size_bytes != artifact.size_bytes:
            raise ValueError(f"Run artifact size disagrees: {artifact.artifact_id}")
    return len(unique)


def dependency_manifest_is_current(fixture: RealCapturedFixture) -> bool:
    """True when the live dependency lock is byte-identical to the recorded one."""

    recorded = fixture.run.environment.dependency_manifest
    try:
        _assert_identity(recorded)
    except ValueError:
        return False
    return True


def verify_live(
    fixture: RealCapturedFixture,
    registry_path: Path,
) -> dict[str, Any]:
    verified_artifacts = verify_recorded_bytes(fixture)
    if fixture.provenance.registry_preservation.state is not EvidenceState.RECORDED:
        raise ValueError(
            "live verification requires preserved historical registry bytes"
        )
    registry = _load_registry(registry_path.resolve(strict=True))
    if _artifact_identity(registry_path) != fixture.provenance.registry:
        raise ValueError("fixture registry identity does not match the requested registry")
    if registry["registry_id"] != fixture.provenance.registry_id:
        raise ValueError("fixture registry ID drift")
    runtime_resource = _resource(registry, fixture.provenance.runtime_resource_id)
    input_resource = _resource(registry, fixture.provenance.input_resource_id)
    _require_execution_authorization(runtime_resource, input_resource)
    expected_command = (
        _one_artifact(runtime_resource, "BINARY").path,
        "-c",
        _one_artifact(runtime_resource, "CONFIGURATION").path,
        "-i",
        _one_artifact(input_resource, "DATA").path,
    )
    if fixture.provenance.command != expected_command:
        raise ValueError("recorded command no longer resolves from the registry")
    environment_values = {
        item.key: item.value for item in fixture.run.environment.variables
    }
    observation = _execute_ofiq(fixture.provenance.command, environment_values)
    recorded_metrics = {
        metric.metric_id: (metric.value, metric.unit) for metric in fixture.run.metrics
    }
    observed_metrics = {
        metric.metric_id: (metric.value, metric.unit) for metric in observation.metrics
    }
    if observation.engine_version != fixture.run.quality_engine.version:
        raise ValueError("live OFIQ engine version differs from the captured Run")
    if observation.input_path != fixture.sample.source_path:
        raise ValueError("live OFIQ result references a different input")
    if observed_metrics != recorded_metrics:
        raise ValueError("live OFIQ metrics differ from the captured Run")
    return {
        "engine_version": observation.engine_version,
        "input_sha256": fixture.sample.sha256,
        "metric_count": len(observation.metrics),
        "unified_quality_score": observed_metrics["UnifiedQualityScore"][0],
        "unified_quality_score_scalar": observed_metrics[
            "UnifiedQualityScore.scalar"
        ][0],
        "verified_artifacts": verified_artifacts,
    }


def _write_new_fixture(path: Path, fixture: RealCapturedFixture) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(fixture.model_dump_json(indent=2))
        handle.write("\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture or verify the registered real OFIQ r-01 completed Run."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    capture.add_argument("--runtime-resource", default=RUNTIME_RESOURCE_ID)
    capture.add_argument("--input-resource", default=INPUT_RESOURCE_ID)
    capture.add_argument("--output", type=Path, default=FIXTURE_PATH)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    verify.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.action == "capture":
        fixture = capture_fixture(
            arguments.registry,
            arguments.runtime_resource,
            arguments.input_resource,
        )
        _write_new_fixture(arguments.output, fixture)
        summary = {
            "fixture": str(arguments.output.resolve()),
            "fixture_sha256": _sha256_file(arguments.output.resolve()),
            "run_id": fixture.run.run_id,
            "status": fixture.run.status,
        }
    else:
        fixture = load_fixture(arguments.fixture)
        summary = verify_live(fixture, arguments.registry)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
