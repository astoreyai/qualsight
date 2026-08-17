"""Typed execution transport contracts and lifecycle invariants."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping, Protocol, runtime_checkable


SCHEMA_VERSION = "1.0.0"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class ContractValidationError(ValueError):
    """Raised when an execution value violates its declared contract."""


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{location} must be an object")
    return value


def _strict_keys(
    value: Mapping[str, Any],
    location: str,
    expected: set[str],
) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise ContractValidationError(
            f"{location} has unknown fields: {', '.join(unknown)}"
        )
    if missing:
        raise ContractValidationError(
            f"{location} is missing fields: {', '.join(missing)}"
        )


def _text(value: Any, location: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ContractValidationError(
            f"{location} must be a nonempty string without surrounding whitespace"
        )
    if "\x00" in value or len(value) > maximum:
        raise ContractValidationError(f"{location} is not a valid bounded string")
    return value


def _optional_text(value: Any, location: str, maximum: int = 4096) -> str | None:
    if value is None:
        return None
    return _text(value, location, maximum)


def _string_tuple(
    value: Any,
    location: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ContractValidationError(f"{location} must be a tuple")
    result = tuple(_text(item, f"{location}[{index}]") for index, item in enumerate(value))
    if not allow_empty and not result:
        raise ContractValidationError(f"{location} must not be empty")
    if len(result) != len(set(result)):
        raise ContractValidationError(f"{location} contains duplicate values")
    return result


def _string_tuple_from_json_array(
    value: Any,
    location: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContractValidationError(f"{location} must be an array")
    return _string_tuple(tuple(value), location, allow_empty=allow_empty)


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _parse_canonical_json(text: Any, location: str) -> Mapping[str, Any]:
    canonical_text = _text(text, location, 4 * 1024 * 1024)
    try:
        value = json.loads(canonical_text, parse_constant=_reject_nonfinite_json)
    except (json.JSONDecodeError, ValueError) as error:
        raise ContractValidationError(f"{location} must contain finite JSON") from error
    if not isinstance(value, dict):
        raise ContractValidationError(f"{location} must contain a JSON object")
    observed = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if observed != canonical_text:
        raise ContractValidationError(f"{location} is not canonical JSON")
    return value


def _timestamp(value: str, location: str) -> datetime:
    text = _text(value, location, 64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ContractValidationError(f"{location} must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise ContractValidationError(f"{location} must include a UTC offset")
    return parsed


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    """Content identity for either a registered file or captured bytes."""

    sha256: str
    size_bytes: int
    path: str | None = None
    mtime_ns: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.sha256, str) or not SHA256_RE.fullmatch(self.sha256):
            raise ContractValidationError("artifact sha256 must be a lowercase SHA-256")
        if (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes < 0
        ):
            raise ContractValidationError("artifact size_bytes must be a nonnegative integer")
        if (self.path is None) != (self.mtime_ns is None):
            raise ContractValidationError(
                "artifact path and mtime_ns must either both be set or both be absent"
            )
        if self.path is not None:
            path = Path(_text(self.path, "artifact.path"))
            if not path.is_absolute():
                raise ContractValidationError("artifact path must be absolute")
            if (
                not isinstance(self.mtime_ns, int)
                or isinstance(self.mtime_ns, bool)
                or self.mtime_ns < 1
            ):
                raise ContractValidationError("artifact mtime_ns must be a positive integer")

    @classmethod
    def from_file(cls, path: Path) -> ArtifactIdentity:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ContractValidationError(f"artifact is not a regular file: {resolved}")
        digest = hashlib.sha256()
        with resolved.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        stat = resolved.stat()
        return cls(
            sha256=digest.hexdigest(),
            size_bytes=stat.st_size,
            path=str(resolved),
            mtime_ns=stat.st_mtime_ns,
        )

    @classmethod
    def from_bytes(cls, content: bytes) -> ArtifactIdentity:
        if not isinstance(content, bytes):
            raise ContractValidationError("captured artifact content must be bytes")
        return cls(
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )

    @classmethod
    def from_mapping(cls, value: Any) -> ArtifactIdentity:
        mapping = _mapping(value, "artifact")
        expected = {"sha256", "size_bytes", "path", "mtime_ns"}
        _strict_keys(mapping, "artifact", expected)
        return cls(
            sha256=mapping["sha256"],
            size_bytes=mapping["size_bytes"],
            path=mapping["path"],
            mtime_ns=mapping["mtime_ns"],
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "path": self.path,
            "mtime_ns": self.mtime_ns,
        }

    def verify_file(self) -> None:
        if self.path is None:
            raise ContractValidationError("captured bytes do not identify a file")
        observed = type(self).from_file(Path(self.path))
        if observed != self:
            raise ContractValidationError(f"artifact identity drift: {self.path}")


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    uri: str
    local_root: str
    commit: str | None
    tag: str | None

    def __post_init__(self) -> None:
        _text(self.uri, "source.uri")
        root = Path(_text(self.local_root, "source.local_root"))
        if not root.is_absolute():
            raise ContractValidationError("source.local_root must be absolute")
        if self.commit is not None and not re.fullmatch(r"[0-9a-f]{40}", self.commit):
            raise ContractValidationError("source.commit must be a full lowercase Git commit")
        _optional_text(self.tag, "source.tag", 256)

    @classmethod
    def from_mapping(cls, value: Any) -> SourceIdentity:
        mapping = _mapping(value, "source")
        expected = {"uri", "local_root", "commit", "tag"}
        _strict_keys(mapping, "source", expected)
        return cls(
            uri=mapping["uri"],
            local_root=mapping["local_root"],
            commit=mapping["commit"],
            tag=mapping["tag"],
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "local_root": self.local_root,
            "commit": self.commit,
            "tag": self.tag,
        }


@dataclass(frozen=True, slots=True)
class AdapterDescriptor:
    """Complete declaration required of an execution adapter by PRD section 51."""

    schema_version: str
    identity: str
    version: str
    modality: str
    capabilities: tuple[str, ...]
    license_expressions: tuple[str, ...]
    source: SourceIdentity
    model_checkpoint_artifacts: tuple[ArtifactIdentity, ...]
    input_schema: str
    output_schema: str
    preprocessing: str
    configuration_artifacts: tuple[ArtifactIdentity, ...]
    runtime: str
    device_requirements: tuple[str, ...]
    determinism: str
    seed_support: bool
    metrics_exposed: tuple[str, ...]
    artifact_roles: tuple[str, ...]
    provenance_schema: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractValidationError(
                f"unsupported adapter schema version {self.schema_version}"
            )
        for name, value in {
            "identity": self.identity,
            "version": self.version,
            "modality": self.modality,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "preprocessing": self.preprocessing,
            "runtime": self.runtime,
            "determinism": self.determinism,
            "provenance_schema": self.provenance_schema,
        }.items():
            _text(value, f"adapter.{name}", 512)
        if not IDENTIFIER_RE.fullmatch(self.identity):
            raise ContractValidationError("adapter.identity has invalid characters")
        for name, value, allow_empty in (
            ("capabilities", self.capabilities, False),
            ("license_expressions", self.license_expressions, False),
            ("device_requirements", self.device_requirements, False),
            ("metrics_exposed", self.metrics_exposed, True),
            ("artifact_roles", self.artifact_roles, False),
        ):
            _string_tuple(value, f"adapter.{name}", allow_empty=allow_empty)
        if not isinstance(self.source, SourceIdentity):
            raise ContractValidationError("adapter.source must be a SourceIdentity")
        for name, artifacts in (
            ("model_checkpoint_artifacts", self.model_checkpoint_artifacts),
            ("configuration_artifacts", self.configuration_artifacts),
        ):
            if not isinstance(artifacts, tuple):
                raise ContractValidationError(f"adapter.{name} must be a tuple")
            if len(artifacts) != len(set(artifacts)):
                raise ContractValidationError(f"adapter.{name} contains duplicates")
            if not all(isinstance(item, ArtifactIdentity) for item in artifacts):
                raise ContractValidationError(f"adapter.{name} has an invalid artifact")
        if not isinstance(self.seed_support, bool):
            raise ContractValidationError("adapter.seed_support must be boolean")

    @classmethod
    def from_mapping(cls, value: Any) -> AdapterDescriptor:
        mapping = _mapping(value, "adapter")
        expected = {
            "schema_version",
            "identity",
            "version",
            "modality",
            "capabilities",
            "license_expressions",
            "source",
            "model_checkpoint_artifacts",
            "input_schema",
            "output_schema",
            "preprocessing",
            "configuration_artifacts",
            "runtime",
            "device_requirements",
            "determinism",
            "seed_support",
            "metrics_exposed",
            "artifact_roles",
            "provenance_schema",
        }
        _strict_keys(mapping, "adapter", expected)
        if not isinstance(mapping["model_checkpoint_artifacts"], list):
            raise ContractValidationError(
                "adapter.model_checkpoint_artifacts must be an array"
            )
        if not isinstance(mapping["configuration_artifacts"], list):
            raise ContractValidationError(
                "adapter.configuration_artifacts must be an array"
            )
        return cls(
            schema_version=mapping["schema_version"],
            identity=mapping["identity"],
            version=mapping["version"],
            modality=mapping["modality"],
            capabilities=_string_tuple_from_json_array(
                mapping["capabilities"], "adapter.capabilities", allow_empty=False
            ),
            license_expressions=_string_tuple_from_json_array(
                mapping["license_expressions"],
                "adapter.license_expressions",
                allow_empty=False,
            ),
            source=SourceIdentity.from_mapping(mapping["source"]),
            model_checkpoint_artifacts=tuple(
                ArtifactIdentity.from_mapping(item)
                for item in mapping["model_checkpoint_artifacts"]
            ),
            input_schema=mapping["input_schema"],
            output_schema=mapping["output_schema"],
            preprocessing=mapping["preprocessing"],
            configuration_artifacts=tuple(
                ArtifactIdentity.from_mapping(item)
                for item in mapping["configuration_artifacts"]
            ),
            runtime=mapping["runtime"],
            device_requirements=_string_tuple_from_json_array(
                mapping["device_requirements"],
                "adapter.device_requirements",
                allow_empty=False,
            ),
            determinism=mapping["determinism"],
            seed_support=mapping["seed_support"],
            metrics_exposed=_string_tuple_from_json_array(
                mapping["metrics_exposed"],
                "adapter.metrics_exposed",
                allow_empty=True,
            ),
            artifact_roles=_string_tuple_from_json_array(
                mapping["artifact_roles"],
                "adapter.artifact_roles",
                allow_empty=False,
            ),
            provenance_schema=mapping["provenance_schema"],
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity": self.identity,
            "version": self.version,
            "modality": self.modality,
            "capabilities": list(self.capabilities),
            "license_expressions": list(self.license_expressions),
            "source": self.source.to_mapping(),
            "model_checkpoint_artifacts": [
                artifact.to_mapping() for artifact in self.model_checkpoint_artifacts
            ],
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "preprocessing": self.preprocessing,
            "configuration_artifacts": [
                artifact.to_mapping() for artifact in self.configuration_artifacts
            ],
            "runtime": self.runtime,
            "device_requirements": list(self.device_requirements),
            "determinism": self.determinism,
            "seed_support": self.seed_support,
            "metrics_exposed": list(self.metrics_exposed),
            "artifact_roles": list(self.artifact_roles),
            "provenance_schema": self.provenance_schema,
        }


@dataclass(frozen=True, slots=True)
class ResourceExecutionIdentity:
    registry_id: str
    registry_schema_version: str
    registry_recorded_at: str
    registry_file_identity: ArtifactIdentity
    resource_id: str
    kind: str
    state: str
    version: str | None
    research_use_status: str
    approved_scopes: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    artifacts: tuple[ArtifactIdentity, ...]
    resource_record_canonical_json: str
    resource_record_sha256: str

    def __post_init__(self) -> None:
        for name, value in {
            "registry_id": self.registry_id,
            "registry_schema_version": self.registry_schema_version,
            "registry_recorded_at": self.registry_recorded_at,
            "resource_id": self.resource_id,
            "kind": self.kind,
            "state": self.state,
            "research_use_status": self.research_use_status,
        }.items():
            _text(value, f"resource_identity.{name}")
        _timestamp(self.registry_recorded_at, "resource_identity.registry_recorded_at")
        _optional_text(self.version, "resource_identity.version")
        if (
            not isinstance(self.registry_file_identity, ArtifactIdentity)
            or self.registry_file_identity.path is None
        ):
            raise ContractValidationError(
                "resource identity must include the registry file identity"
            )
        _string_tuple(
            self.approved_scopes,
            "resource_identity.approved_scopes",
            allow_empty=True,
        )
        _string_tuple(
            self.blocked_reasons,
            "resource_identity.blocked_reasons",
            allow_empty=True,
        )
        if not isinstance(self.artifacts, tuple) or not self.artifacts:
            raise ContractValidationError("resource identity must include artifacts")
        if not all(isinstance(item, ArtifactIdentity) for item in self.artifacts):
            raise ContractValidationError("resource identity artifacts are invalid")
        if len(self.artifacts) != len(set(self.artifacts)):
            raise ContractValidationError("resource identity contains duplicate artifacts")

        record = _parse_canonical_json(
            self.resource_record_canonical_json,
            "resource_identity.resource_record_canonical_json",
        )
        if not isinstance(self.resource_record_sha256, str) or not SHA256_RE.fullmatch(
            self.resource_record_sha256
        ):
            raise ContractValidationError(
                "resource_identity.resource_record_sha256 must be SHA-256"
            )
        observed_record_sha256 = hashlib.sha256(
            self.resource_record_canonical_json.encode("utf-8")
        ).hexdigest()
        if observed_record_sha256 != self.resource_record_sha256:
            raise ContractValidationError(
                "resource identity record SHA-256 does not match its canonical JSON"
            )

        scalar_projections = {
            "resource_id": self.resource_id,
            "kind": self.kind,
            "state": self.state,
            "version": self.version,
            "research_use_status": self.research_use_status,
        }
        for field, expected in scalar_projections.items():
            if record.get(field) != expected:
                raise ContractValidationError(
                    f"resource record {field} contradicts execution identity"
                )
        for field, expected in (
            ("approved_scopes", self.approved_scopes),
            ("blocked_reasons", self.blocked_reasons),
        ):
            observed = record.get(field)
            if not isinstance(observed, list) or tuple(observed) != expected:
                raise ContractValidationError(
                    f"resource record {field} contradicts execution identity"
                )

        record_artifacts = record.get("artifacts")
        if not isinstance(record_artifacts, list):
            raise ContractValidationError("resource record artifacts must be an array")
        projected_artifacts: list[ArtifactIdentity] = []
        for index, value in enumerate(record_artifacts):
            if not isinstance(value, dict):
                raise ContractValidationError(
                    f"resource record artifact {index} must be an object"
                )
            try:
                projected_artifacts.append(
                    ArtifactIdentity(
                        sha256=value["sha256"],
                        size_bytes=value["size_bytes"],
                        path=value["path"],
                        mtime_ns=value["mtime_ns"],
                    )
                )
            except KeyError as error:
                raise ContractValidationError(
                    f"resource record artifact {index} lacks identity evidence"
                ) from error
        if tuple(projected_artifacts) != self.artifacts:
            raise ContractValidationError(
                "resource record artifacts contradict execution identity"
            )

    @classmethod
    def from_mapping(cls, value: Any) -> ResourceExecutionIdentity:
        mapping = _mapping(value, "resource_identity")
        expected = {
            "registry_id",
            "registry_schema_version",
            "registry_recorded_at",
            "registry_file_identity",
            "resource_id",
            "kind",
            "state",
            "version",
            "research_use_status",
            "approved_scopes",
            "blocked_reasons",
            "artifacts",
            "resource_record_canonical_json",
            "resource_record_sha256",
        }
        _strict_keys(mapping, "resource_identity", expected)
        if not isinstance(mapping["artifacts"], list):
            raise ContractValidationError("resource_identity.artifacts must be an array")
        return cls(
            registry_id=mapping["registry_id"],
            registry_schema_version=mapping["registry_schema_version"],
            registry_recorded_at=mapping["registry_recorded_at"],
            registry_file_identity=ArtifactIdentity.from_mapping(
                mapping["registry_file_identity"]
            ),
            resource_id=mapping["resource_id"],
            kind=mapping["kind"],
            state=mapping["state"],
            version=mapping["version"],
            research_use_status=mapping["research_use_status"],
            approved_scopes=_string_tuple_from_json_array(
                mapping["approved_scopes"],
                "resource_identity.approved_scopes",
                allow_empty=True,
            ),
            blocked_reasons=_string_tuple_from_json_array(
                mapping["blocked_reasons"],
                "resource_identity.blocked_reasons",
                allow_empty=True,
            ),
            artifacts=tuple(
                ArtifactIdentity.from_mapping(item) for item in mapping["artifacts"]
            ),
            resource_record_canonical_json=mapping[
                "resource_record_canonical_json"
            ],
            resource_record_sha256=mapping["resource_record_sha256"],
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "registry_id": self.registry_id,
            "registry_schema_version": self.registry_schema_version,
            "registry_recorded_at": self.registry_recorded_at,
            "registry_file_identity": self.registry_file_identity.to_mapping(),
            "resource_id": self.resource_id,
            "kind": self.kind,
            "state": self.state,
            "version": self.version,
            "research_use_status": self.research_use_status,
            "approved_scopes": list(self.approved_scopes),
            "blocked_reasons": list(self.blocked_reasons),
            "artifacts": [artifact.to_mapping() for artifact in self.artifacts],
            "resource_record_canonical_json": self.resource_record_canonical_json,
            "resource_record_sha256": self.resource_record_sha256,
        }


@dataclass(frozen=True, slots=True)
class RepositoryProvenance:
    root: str
    head_commit: str | None
    branch: str | None
    dirty: bool
    status_sha256: str

    def __post_init__(self) -> None:
        root = Path(_text(self.root, "repository.root"))
        if not root.is_absolute():
            raise ContractValidationError("repository.root must be absolute")
        if self.head_commit is not None and not re.fullmatch(
            r"[0-9a-f]{40}", self.head_commit
        ):
            raise ContractValidationError("repository.head_commit must be a Git commit")
        _optional_text(self.branch, "repository.branch", 512)
        if not isinstance(self.dirty, bool):
            raise ContractValidationError("repository.dirty must be boolean")
        if not SHA256_RE.fullmatch(self.status_sha256):
            raise ContractValidationError("repository.status_sha256 must be SHA-256")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "head_commit": self.head_commit,
            "branch": self.branch,
            "dirty": self.dirty,
            "status_sha256": self.status_sha256,
        }


def _run_git(root: Path, arguments: tuple[str, ...], *, allow_failure: bool = False) -> bytes:
    result = subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0 and not allow_failure:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise ContractValidationError(f"Git provenance command failed: {message}")
    return result.stdout if result.returncode == 0 else b""


def capture_repository_provenance(root: Path) -> RepositoryProvenance:
    resolved = root.resolve(strict=True)
    observed_root = _run_git(resolved, ("rev-parse", "--show-toplevel"))
    git_root = Path(observed_root.decode("utf-8").strip()).resolve(strict=True)
    if git_root != resolved:
        raise ContractValidationError(
            f"repository root mismatch: requested {resolved}, observed {git_root}"
        )
    head_bytes = _run_git(
        resolved,
        ("rev-parse", "--verify", "HEAD"),
        allow_failure=True,
    )
    branch_bytes = _run_git(resolved, ("branch", "--show-current"))
    status = _run_git(
        resolved,
        ("status", "--porcelain=v1", "--untracked-files=normal"),
    )
    head = head_bytes.decode("utf-8").strip() or None
    branch = branch_bytes.decode("utf-8").strip() or None
    return RepositoryProvenance(
        root=str(resolved),
        head_commit=head,
        branch=branch,
        dirty=bool(status),
        status_sha256=hashlib.sha256(status).hexdigest(),
    )


class LifecycleState(str, Enum):
    CREATED = "CREATED"
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = frozenset(
    {
        LifecycleState.SUCCEEDED,
        LifecycleState.FAILED,
        LifecycleState.CANCELLED,
    }
)


def transition_lifecycle(
    current: LifecycleState,
    target: LifecycleState,
) -> LifecycleState:
    allowed = {
        LifecycleState.CREATED: {LifecycleState.PREPARED},
        LifecycleState.PREPARED: {LifecycleState.RUNNING},
        LifecycleState.RUNNING: TERMINAL_STATES,
    }
    if target not in allowed.get(current, frozenset()):
        raise ContractValidationError(
            f"invalid execution lifecycle transition {current.value} -> {target.value}"
        )
    return target


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    state: LifecycleState
    observed_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.state, LifecycleState):
            raise ContractValidationError("lifecycle event state is invalid")
        _timestamp(self.observed_at, "lifecycle.observed_at")

    @classmethod
    def observe(cls, state: LifecycleState) -> LifecycleEvent:
        return cls(state=state, observed_at=utc_timestamp())

    def to_mapping(self) -> dict[str, str]:
        return {"state": self.state.value, "observed_at": self.observed_at}


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    descriptor: AdapterDescriptor
    executable: ArtifactIdentity
    arguments: tuple[str, ...]
    working_directory: str
    environment: tuple[tuple[str, str], ...]
    input_artifacts: tuple[ArtifactIdentity, ...]
    resources: tuple[ResourceExecutionIdentity, ...]
    purpose_scope: str

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, AdapterDescriptor):
            raise ContractValidationError("request descriptor is invalid")
        if self.executable.path is None:
            raise ContractValidationError("request executable must identify a file")
        if not isinstance(self.arguments, tuple):
            raise ContractValidationError("request arguments must be a tuple")
        for index, argument in enumerate(self.arguments):
            if not isinstance(argument, str) or "\x00" in argument:
                raise ContractValidationError(f"request argument {index} is invalid")
        working_directory = Path(_text(self.working_directory, "request.working_directory"))
        if not working_directory.is_absolute():
            raise ContractValidationError("request working_directory must be absolute")
        if not isinstance(self.environment, tuple):
            raise ContractValidationError("request environment must be a tuple")
        names: list[str] = []
        for index, item in enumerate(self.environment):
            if not isinstance(item, tuple) or len(item) != 2:
                raise ContractValidationError(
                    f"request environment item {index} must be a name/value pair"
                )
            name, value = item
            if not isinstance(name, str) or not name or "=" in name or "\x00" in name:
                raise ContractValidationError(f"request environment name {index} is invalid")
            if not isinstance(value, str) or "\x00" in value:
                raise ContractValidationError(f"request environment value {index} is invalid")
            names.append(name)
        if names != sorted(names) or len(names) != len(set(names)):
            raise ContractValidationError(
                "request environment must have unique names in lexical order"
            )
        if not isinstance(self.input_artifacts, tuple) or not all(
            isinstance(item, ArtifactIdentity) and item.path is not None
            for item in self.input_artifacts
        ):
            raise ContractValidationError("request input artifacts are invalid")
        if not isinstance(self.resources, tuple) or not self.resources:
            raise ContractValidationError("request resources must not be empty")
        if not all(isinstance(item, ResourceExecutionIdentity) for item in self.resources):
            raise ContractValidationError("request resource identity is invalid")
        _text(self.purpose_scope, "request.purpose_scope", 512)

    @property
    def request_id(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.to_mapping())).hexdigest()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "descriptor": self.descriptor.to_mapping(),
            "executable": self.executable.to_mapping(),
            "arguments": list(self.arguments),
            "working_directory": self.working_directory,
            "environment": [
                {"name": name, "value": value} for name, value in self.environment
            ],
            "input_artifacts": [item.to_mapping() for item in self.input_artifacts],
            "resources": [item.to_mapping() for item in self.resources],
            "purpose_scope": self.purpose_scope,
        }


@dataclass(frozen=True, slots=True)
class CapturedArtifact:
    role: str
    media_type: str
    content: bytes
    identity: ArtifactIdentity

    def __post_init__(self) -> None:
        _text(self.role, "captured_artifact.role", 128)
        _text(self.media_type, "captured_artifact.media_type", 256)
        if not isinstance(self.content, bytes):
            raise ContractValidationError("captured artifact content must be bytes")
        if self.identity != ArtifactIdentity.from_bytes(self.content):
            raise ContractValidationError("captured artifact identity does not match bytes")

    @classmethod
    def capture(cls, role: str, content: bytes) -> CapturedArtifact:
        return cls(
            role=role,
            media_type="application/octet-stream",
            content=content,
            identity=ArtifactIdentity.from_bytes(content),
        )


@dataclass(frozen=True, slots=True)
class ExecutionFailureDetail:
    kind: str
    message: str
    exit_code: int

    def __post_init__(self) -> None:
        _text(self.kind, "failure.kind", 128)
        _text(self.message, "failure.message")
        if not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool):
            raise ContractValidationError("failure.exit_code must be an integer")


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    request: ExecutionRequest
    state: LifecycleState
    command: tuple[str, ...]
    repository: RepositoryProvenance
    started_at: str
    ended_at: str
    exit_code: int
    termination_signal: int | None
    stdout: CapturedArtifact
    stderr: CapturedArtifact
    lifecycle: tuple[LifecycleEvent, ...]
    cancellation_requested: bool
    failure: ExecutionFailureDetail | None

    def __post_init__(self) -> None:
        if self.state not in TERMINAL_STATES:
            raise ContractValidationError("execution record must be terminal")
        if not isinstance(self.command, tuple) or not self.command:
            raise ContractValidationError("execution command must be a nonempty tuple")
        if not all(isinstance(item, str) and "\x00" not in item for item in self.command):
            raise ContractValidationError("execution command contains an invalid argument")
        if self.command[0] != self.request.executable.path:
            raise ContractValidationError("execution command does not match executable")
        started = _timestamp(self.started_at, "execution.started_at")
        ended = _timestamp(self.ended_at, "execution.ended_at")
        if ended < started:
            raise ContractValidationError("execution ended before it started")
        if not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool):
            raise ContractValidationError("execution exit_code must be an integer")
        if self.termination_signal is not None and (
            not isinstance(self.termination_signal, int)
            or isinstance(self.termination_signal, bool)
            or self.termination_signal < 1
        ):
            raise ContractValidationError("execution termination_signal is invalid")
        if not isinstance(self.cancellation_requested, bool):
            raise ContractValidationError("execution cancellation_requested must be boolean")
        if not isinstance(self.lifecycle, tuple) or not all(
            isinstance(event, LifecycleEvent) for event in self.lifecycle
        ):
            raise ContractValidationError("execution lifecycle must be a tuple of events")
        expected_states = [event.state for event in self.lifecycle]
        if not expected_states or expected_states[0] != LifecycleState.CREATED:
            raise ContractValidationError("execution lifecycle must start at CREATED")
        for current, target in zip(expected_states, expected_states[1:]):
            transition_lifecycle(current, target)
        if expected_states[-1] != self.state:
            raise ContractValidationError("execution lifecycle does not reach record state")
        if self.state == LifecycleState.SUCCEEDED:
            if self.exit_code != 0 or self.failure is not None or self.cancellation_requested:
                raise ContractValidationError("successful execution has failure state")
        elif self.state == LifecycleState.FAILED:
            if self.exit_code == 0 or self.failure is None or self.cancellation_requested:
                raise ContractValidationError("failed execution has inconsistent outcome")
        else:
            if not self.cancellation_requested or self.failure is not None:
                raise ContractValidationError("cancelled execution has inconsistent outcome")
            if self.termination_signal is None:
                raise ContractValidationError("cancelled execution lacks a termination signal")

    @property
    def input_artifacts(self) -> tuple[ArtifactIdentity, ...]:
        return self.request.input_artifacts

    def raise_for_status(self) -> None:
        if self.state == LifecycleState.FAILED:
            raise ExecutionFailed(self)
        if self.state == LifecycleState.CANCELLED:
            raise ExecutionCancelled(self)


class ExecutionOutcomeError(RuntimeError):
    def __init__(self, record: ExecutionRecord) -> None:
        self.record = record
        super().__init__(
            f"execution {record.request.request_id} ended as {record.state.value} "
            f"with exit code {record.exit_code}"
        )


class ExecutionFailed(ExecutionOutcomeError):
    """A real process returned a nonzero exit status."""


class ExecutionCancelled(ExecutionOutcomeError):
    """A real process was cancelled after it entered RUNNING."""


@runtime_checkable
class PreparedExecutionContract(Protocol):
    request: ExecutionRequest
    repository: RepositoryProvenance
    lifecycle: tuple[LifecycleEvent, ...]


@runtime_checkable
class ExecutionHandle(Protocol):
    process_id: int
    cancel: Callable[[float], bool]
    collect: Callable[[], ExecutionRecord]


@runtime_checkable
class ExecutionAdapter(Protocol):
    descriptor: AdapterDescriptor
    prepare: Callable[[ExecutionRequest], PreparedExecutionContract]
    start: Callable[[PreparedExecutionContract], ExecutionHandle]
    execute: Callable[[ExecutionRequest], ExecutionRecord]
