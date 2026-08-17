"""Strict binding from the validated local resource registry to execution types."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from scripts.validate_resource_registry import validate_registry_file

from .contracts import (
    AdapterDescriptor,
    ArtifactIdentity,
    ContractValidationError,
    ResourceExecutionIdentity,
    SourceIdentity,
)


OFIQ_RUNTIME_RESOURCE_ID = "ofiq-v1-1-0-build-runtime"
OFIQ_V112_RUNTIME_RESOURCE_ID = "ofiq-v1-1-2-linux-runtime"
OFIQ_INPUT_RESOURCE_ID = "ofiq-r-01-conformance-input"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractValidationError(f"cannot load resource registry: {error}") from error
    if not isinstance(value, dict):
        raise ContractValidationError("resource registry must be an object")
    return value


def _tuple_of_strings(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContractValidationError(f"{location} must be an array of strings")
    return tuple(value)


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ContractValidationError(
            f"resource record cannot be represented as canonical JSON: {error}"
        ) from error


def _freeze_string_tuple(instance: Any, field_name: str) -> tuple[str, ...]:
    value = getattr(instance, field_name)
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ContractValidationError(f"{field_name} must be an array of strings")
    frozen = tuple(value)
    object.__setattr__(instance, field_name, frozen)
    return frozen


@dataclass(frozen=True, slots=True)
class RegistryValidationSummary:
    registry_id: str
    resources: int
    artifacts: int
    checks: int
    states: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.states, (list, tuple)):
            raise ContractValidationError("registry validation states must be an array")
        states: list[tuple[str, int]] = []
        for item in self.states:
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], int)
                or isinstance(item[1], bool)
                or item[1] < 0
            ):
                raise ContractValidationError("registry validation state is invalid")
            states.append((item[0], item[1]))
        object.__setattr__(self, "states", tuple(states))


@dataclass(frozen=True, slots=True)
class RegisteredArtifact:
    role: str
    identity: ArtifactIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role:
            raise ContractValidationError("registered artifact role must be nonempty")
        if not isinstance(self.identity, ArtifactIdentity):
            raise ContractValidationError(
                "registered artifact identity must be an ArtifactIdentity"
            )


@dataclass(frozen=True, slots=True)
class RegisteredResource:
    registry_id: str
    registry_schema_version: str
    registry_recorded_at: str
    resource_id: str
    kind: str
    name: str
    state: str
    version: str | None
    source: SourceIdentity
    license_expressions: tuple[str, ...]
    use_restrictions: tuple[str, ...]
    redistribution_status: str
    research_use_status: str
    data_classification: str
    runtime_platform: str
    runtime_environment: tuple[tuple[str, str], ...]
    consumers: tuple[str, ...]
    approved_scopes: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    artifacts: tuple[RegisteredArtifact, ...]
    registry_file_identity: ArtifactIdentity
    resource_record_canonical_json: str
    resource_record_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "license_expressions",
            "use_restrictions",
            "consumers",
            "approved_scopes",
            "blocked_reasons",
        ):
            _freeze_string_tuple(self, field_name)

        if not isinstance(self.runtime_environment, (list, tuple)):
            raise ContractValidationError(
                "runtime_environment must be an array of name/value pairs"
            )
        environment: list[tuple[str, str]] = []
        for item in self.runtime_environment:
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 2
                or not isinstance(item[0], str)
                or not item[0]
                or not isinstance(item[1], str)
            ):
                raise ContractValidationError(
                    "runtime_environment contains an invalid name/value pair"
                )
            environment.append((item[0], item[1]))
        environment.sort()
        if len({name for name, _ in environment}) != len(environment):
            raise ContractValidationError(
                "runtime_environment contains duplicate variable names"
            )
        object.__setattr__(self, "runtime_environment", tuple(environment))

        if not isinstance(self.artifacts, (list, tuple)) or not self.artifacts:
            raise ContractValidationError("registered resource artifacts must not be empty")
        artifacts = tuple(self.artifacts)
        if not all(isinstance(item, RegisteredArtifact) for item in artifacts):
            raise ContractValidationError(
                "registered resource artifacts contain an invalid value"
            )
        object.__setattr__(self, "artifacts", artifacts)

        if not isinstance(self.registry_file_identity, ArtifactIdentity):
            raise ContractValidationError(
                "registered resource must retain the registry file identity"
            )
        if not isinstance(self.resource_record_canonical_json, str):
            raise ContractValidationError(
                "registered resource canonical record must be a string"
            )
        try:
            record = json.loads(
                self.resource_record_canonical_json,
                object_pairs_hook=_unique_object,
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ContractValidationError(
                f"registered resource canonical record is invalid JSON: {error}"
            ) from error
        if not isinstance(record, dict):
            raise ContractValidationError(
                "registered resource canonical record must be an object"
            )
        if _canonical_json(record) != self.resource_record_canonical_json:
            raise ContractValidationError(
                "registered resource record is not canonical JSON"
            )
        if record.get("resource_id") != self.resource_id:
            raise ContractValidationError(
                "registered resource record does not match its resource ID"
            )
        observed_sha256 = hashlib.sha256(
            self.resource_record_canonical_json.encode("utf-8")
        ).hexdigest()
        if self.resource_record_sha256 != observed_sha256:
            raise ContractValidationError(
                "registered resource record SHA-256 does not match its canonical JSON"
            )

        try:
            source_value = record["source"]
            runtime_value = record["runtime_requirements"]
            environment_value = runtime_value["environment"]
            expected_source = SourceIdentity(
                uri=source_value["uri"],
                local_root=str(Path(source_value["local_root"]).resolve(strict=True)),
                commit=source_value.get("commit"),
                tag=source_value.get("tag"),
            )
            expected_artifacts = tuple(
                RegisteredArtifact(
                    role=item["role"],
                    identity=ArtifactIdentity(
                        sha256=item["sha256"],
                        size_bytes=item["size_bytes"],
                        path=str(Path(item["path"]).resolve(strict=True)),
                        mtime_ns=item["mtime_ns"],
                    ),
                )
                for item in record["artifacts"]
            )
            expected_projections: dict[str, Any] = {
                "kind": record["kind"],
                "name": record["name"],
                "state": record["state"],
                "version": record.get("version"),
                "source": expected_source,
                "license_expressions": tuple(
                    license_value["expression"] for license_value in record["licenses"]
                ),
                "use_restrictions": _tuple_of_strings(
                    record["use_restrictions"], "resource.use_restrictions"
                ),
                "redistribution_status": record["redistribution_status"],
                "research_use_status": record["research_use_status"],
                "data_classification": record["data_classification"],
                "runtime_platform": runtime_value["platform"],
                "runtime_environment": tuple(sorted(environment_value.items())),
                "consumers": _tuple_of_strings(
                    record["consumers"], "resource.consumers"
                ),
                "approved_scopes": _tuple_of_strings(
                    record["approved_scopes"], "resource.approved_scopes"
                ),
                "blocked_reasons": _tuple_of_strings(
                    record["blocked_reasons"], "resource.blocked_reasons"
                ),
                "artifacts": expected_artifacts,
            }
        except (KeyError, OSError, TypeError) as error:
            raise ContractValidationError(
                "registered resource canonical record lacks a valid operational projection"
            ) from error
        for field_name, expected in expected_projections.items():
            if getattr(self, field_name) != expected:
                raise ContractValidationError(
                    f"registered resource {field_name} contradicts canonical registry record"
                )

    def artifact(self, role: str) -> RegisteredArtifact:
        matches = tuple(item for item in self.artifacts if item.role == role)
        if len(matches) != 1:
            raise ContractValidationError(
                f"resource {self.resource_id} has {len(matches)} artifacts with role {role}"
            )
        return matches[0]

    def artifacts_with_role(self, role: str) -> tuple[RegisteredArtifact, ...]:
        return tuple(item for item in self.artifacts if item.role == role)

    def execution_identity(self) -> ResourceExecutionIdentity:
        return ResourceExecutionIdentity(
            registry_id=self.registry_id,
            registry_schema_version=self.registry_schema_version,
            registry_recorded_at=self.registry_recorded_at,
            resource_id=self.resource_id,
            kind=self.kind,
            state=self.state,
            version=self.version,
            research_use_status=self.research_use_status,
            approved_scopes=self.approved_scopes,
            blocked_reasons=self.blocked_reasons,
            artifacts=tuple(item.identity for item in self.artifacts),
            registry_file_identity=self.registry_file_identity,
            resource_record_canonical_json=self.resource_record_canonical_json,
            resource_record_sha256=self.resource_record_sha256,
        )


@dataclass(frozen=True, slots=True)
class ResourceRegistry:
    schema_version: str
    registry_id: str
    recorded_at: str
    source_requirements: tuple[str, ...]
    resources: tuple[RegisteredResource, ...]
    validation: RegistryValidationSummary
    file_identity: ArtifactIdentity

    def __post_init__(self) -> None:
        _freeze_string_tuple(self, "source_requirements")
        if not isinstance(self.resources, (list, tuple)) or not self.resources:
            raise ContractValidationError("resource registry resources must not be empty")
        resources = tuple(self.resources)
        if not all(isinstance(item, RegisteredResource) for item in resources):
            raise ContractValidationError("resource registry contains an invalid resource")
        object.__setattr__(self, "resources", resources)
        if not isinstance(self.file_identity, ArtifactIdentity):
            raise ContractValidationError(
                "resource registry file identity must be an ArtifactIdentity"
            )
        resource_ids = tuple(resource.resource_id for resource in resources)
        if len(resource_ids) != len(set(resource_ids)):
            raise ContractValidationError("resource registry contains duplicate resource IDs")
        for resource in resources:
            if resource.registry_file_identity != self.file_identity:
                raise ContractValidationError(
                    f"resource {resource.resource_id} has a different registry file identity"
                )
            if (
                resource.registry_id != self.registry_id
                or resource.registry_schema_version != self.schema_version
                or resource.registry_recorded_at != self.recorded_at
            ):
                raise ContractValidationError(
                    f"resource {resource.resource_id} has inconsistent registry provenance"
                )
        if self.validation.resources != len(resources):
            raise ContractValidationError(
                "registry validation resource count differs from bound resources"
            )
        self.verify_file_membership()

    def verify_file_membership(self) -> None:
        if self.file_identity.path is None:
            raise ContractValidationError("resource registry identity has no file path")
        self.file_identity.verify_file()
        raw_registry = _load_json(Path(self.file_identity.path))
        for field_name, expected in (
            ("schema_version", self.schema_version),
            ("registry_id", self.registry_id),
            ("recorded_at", self.recorded_at),
        ):
            if raw_registry.get(field_name) != expected:
                raise ContractValidationError(
                    f"bound registry {field_name} differs from the registry file"
                )
        raw_requirements = raw_registry.get("source_requirements")
        if (
            not isinstance(raw_requirements, list)
            or tuple(raw_requirements) != self.source_requirements
        ):
            raise ContractValidationError(
                "bound registry source requirements differ from the registry file"
            )
        raw_resources = raw_registry.get("resources")
        if not isinstance(raw_resources, list) or not all(
            isinstance(record, dict) for record in raw_resources
        ):
            raise ContractValidationError("registry file resources must be an array")
        raw_by_id: dict[str, Mapping[str, Any]] = {}
        for record in raw_resources:
            resource_id = record.get("resource_id")
            if not isinstance(resource_id, str) or not resource_id:
                raise ContractValidationError(
                    "registry file contains a resource without an ID"
                )
            if resource_id in raw_by_id:
                raise ContractValidationError(
                    f"registry file contains duplicate resource ID {resource_id}"
                )
            raw_by_id[resource_id] = record
        bound_ids = {resource.resource_id for resource in self.resources}
        if set(raw_by_id) != bound_ids:
            raise ContractValidationError(
                "bound registry resources differ from registry-file membership"
            )
        for resource in self.resources:
            if (
                _canonical_json(raw_by_id[resource.resource_id])
                != resource.resource_record_canonical_json
            ):
                raise ContractValidationError(
                    f"resource {resource.resource_id} is not the canonical registry member"
                )

    def raw_resource(self, resource_id: str) -> dict[str, Any]:
        """Return the exact registered record (canonical JSON) as a plain mapping."""

        return json.loads(self.resource(resource_id).resource_record_canonical_json)

    def resource(self, resource_id: str) -> RegisteredResource:
        matches = tuple(item for item in self.resources if item.resource_id == resource_id)
        if len(matches) != 1:
            raise ContractValidationError(
                f"registry has {len(matches)} resources named {resource_id}"
            )
        return matches[0]

    def common_approved_scopes(
        self,
        resource_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not isinstance(resource_ids, (list, tuple)) or not resource_ids:
            raise ContractValidationError(
                "consumed resource IDs must be a nonempty array"
            )
        frozen_ids = tuple(resource_ids)
        if not all(isinstance(item, str) and item for item in frozen_ids):
            raise ContractValidationError("consumed resource ID is invalid")
        if len(frozen_ids) != len(set(frozen_ids)):
            raise ContractValidationError("consumed resource IDs contain duplicates")
        resources = tuple(self.resource(resource_id) for resource_id in frozen_ids)
        common = set(resources[0].approved_scopes)
        for resource in resources[1:]:
            common.intersection_update(resource.approved_scopes)
        return tuple(sorted(common))

    def require_common_approved_scope(
        self,
        resource_ids: tuple[str, ...],
        purpose_scope: str,
    ) -> tuple[RegisteredResource, ...]:
        if not isinstance(purpose_scope, str) or not purpose_scope:
            raise ContractValidationError("purpose scope must be a nonempty string")
        if not isinstance(resource_ids, (list, tuple)) or not resource_ids:
            raise ContractValidationError(
                "consumed resource IDs must be a nonempty array"
            )
        frozen_ids = tuple(resource_ids)
        if not all(isinstance(item, str) and item for item in frozen_ids):
            raise ContractValidationError("consumed resource ID is invalid")
        if len(frozen_ids) != len(set(frozen_ids)):
            raise ContractValidationError("consumed resource IDs contain duplicates")
        resources = tuple(self.resource(resource_id) for resource_id in frozen_ids)
        for resource in resources:
            if purpose_scope not in resource.approved_scopes:
                raise ContractValidationError(
                    f"resource {resource.resource_id} does not approve scope {purpose_scope}"
                )
        return resources


def _registered_artifact(value: Mapping[str, Any]) -> RegisteredArtifact:
    return RegisteredArtifact(
        role=value["role"],
        identity=ArtifactIdentity(
            sha256=value["sha256"],
            size_bytes=value["size_bytes"],
            path=str(Path(value["path"]).resolve(strict=True)),
            mtime_ns=value["mtime_ns"],
        ),
    )


def _registered_resource(
    value: Mapping[str, Any],
    *,
    registry_id: str,
    schema_version: str,
    recorded_at: str,
    registry_file_identity: ArtifactIdentity,
) -> RegisteredResource:
    source_value = value["source"]
    runtime_value = value["runtime_requirements"]
    environment_value = runtime_value["environment"]
    canonical_record = _canonical_json(value)
    return RegisteredResource(
        registry_id=registry_id,
        registry_schema_version=schema_version,
        registry_recorded_at=recorded_at,
        resource_id=value["resource_id"],
        kind=value["kind"],
        name=value["name"],
        state=value["state"],
        version=value.get("version"),
        source=SourceIdentity(
            uri=source_value["uri"],
            local_root=str(Path(source_value["local_root"]).resolve(strict=True)),
            commit=source_value.get("commit"),
            tag=source_value.get("tag"),
        ),
        license_expressions=tuple(
            license_value["expression"] for license_value in value["licenses"]
        ),
        use_restrictions=_tuple_of_strings(
            value["use_restrictions"], "resource.use_restrictions"
        ),
        redistribution_status=value["redistribution_status"],
        research_use_status=value["research_use_status"],
        data_classification=value["data_classification"],
        runtime_platform=runtime_value["platform"],
        runtime_environment=tuple(sorted(environment_value.items())),
        consumers=_tuple_of_strings(value["consumers"], "resource.consumers"),
        approved_scopes=_tuple_of_strings(
            value["approved_scopes"], "resource.approved_scopes"
        ),
        blocked_reasons=_tuple_of_strings(
            value["blocked_reasons"], "resource.blocked_reasons"
        ),
        artifacts=tuple(_registered_artifact(item) for item in value["artifacts"]),
        registry_file_identity=registry_file_identity,
        resource_record_canonical_json=canonical_record,
        resource_record_sha256=hashlib.sha256(
            canonical_record.encode("utf-8")
        ).hexdigest(),
    )


def load_resource_registry(path: Path) -> ResourceRegistry:
    resolved = path.resolve(strict=True)
    identity_before = ArtifactIdentity.from_file(resolved)
    validation_value = validate_registry_file(resolved)
    value = _load_json(resolved)
    identity_after = ArtifactIdentity.from_file(resolved)
    if identity_before != identity_after:
        raise ContractValidationError("resource registry changed while it was validated")

    schema_version = value["schema_version"]
    registry_id = value["registry_id"]
    recorded_at = value["recorded_at"]
    resources = tuple(
        _registered_resource(
            item,
            registry_id=registry_id,
            schema_version=schema_version,
            recorded_at=recorded_at,
            registry_file_identity=identity_after,
        )
        for item in value["resources"]
    )
    validation = RegistryValidationSummary(
        registry_id=validation_value["registry_id"],
        resources=validation_value["resources"],
        artifacts=validation_value["artifacts"],
        checks=validation_value["checks"],
        states=tuple(sorted(validation_value["states"].items())),
    )
    if len(resources) != validation.resources:
        raise ContractValidationError("validated resource count changed during binding")
    return ResourceRegistry(
        schema_version=schema_version,
        registry_id=registry_id,
        recorded_at=recorded_at,
        source_requirements=_tuple_of_strings(
            value["source_requirements"], "registry.source_requirements"
        ),
        resources=resources,
        validation=validation,
        file_identity=identity_after,
    )


def build_ofiq_transport_descriptor(
    registry: ResourceRegistry,
    runtime_resource_id: str = OFIQ_RUNTIME_RESOURCE_ID,
) -> AdapterDescriptor:
    """Declare the transport boundary without interpreting OFIQ measurements."""

    runtime = registry.resource(runtime_resource_id)
    if runtime.version is None:
        raise ContractValidationError("registered OFIQ runtime has no version")
    license_expressions = tuple(dict.fromkeys(runtime.license_expressions))
    return AdapterDescriptor(
        schema_version="1.0.0",
        identity=f"{runtime.resource_id}.cli",
        version=runtime.version,
        modality="FACE",
        capabilities=("PROCESS_EXECUTION", "CANCELLATION", "BYTE_STREAM_CAPTURE"),
        license_expressions=license_expressions,
        source=runtime.source,
        model_checkpoint_artifacts=tuple(
            item.identity for item in runtime.artifacts_with_role("MODEL")
        ),
        input_schema="qualsight.execution.cli.file-input.v1",
        output_schema="qualsight.execution.cli.byte-streams.v1",
        preprocessing="REGISTERED_RUNTIME_INTERNAL_UNINTERPRETED",
        configuration_artifacts=tuple(
            item.identity for item in runtime.artifacts_with_role("CONFIGURATION")
        ),
        runtime="CLI_PROCESS",
        device_requirements=(runtime.runtime_platform,),
        determinism="NOT_DECLARED_BY_REGISTERED_RESOURCE",
        seed_support=False,
        metrics_exposed=(),
        artifact_roles=("STANDARD_OUTPUT", "STANDARD_ERROR", "PROCESS_STATUS"),
        provenance_schema="qualsight.execution.provenance.v1",
    )
