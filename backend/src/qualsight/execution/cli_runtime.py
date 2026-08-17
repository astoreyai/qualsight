"""Generic process runtime bound to validated registry artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
import subprocess
import threading

from .contracts import (
    AdapterDescriptor,
    ArtifactIdentity,
    CapturedArtifact,
    ContractValidationError,
    ExecutionFailureDetail,
    ExecutionRecord,
    ExecutionRequest,
    LifecycleEvent,
    LifecycleState,
    RepositoryProvenance,
    capture_repository_provenance,
    transition_lifecycle,
    utc_timestamp,
)
from .registry_binding import ResourceRegistry


# Registry states under which a resource may be consumed by a launch. BLOCKED
# resources are refused before Popen; the registry validator guarantees that
# every usable state carries owner authorization and effective approved scopes.
USABLE_RESOURCE_STATES = frozenset(
    {"VERIFIED_LIMITED_USE", "APPROVED_LOCAL_RESEARCH_USE"}
)


@dataclass(frozen=True, slots=True)
class PreparedExecution:
    request: ExecutionRequest
    repository: RepositoryProvenance
    lifecycle: tuple[LifecycleEvent, ...]
    _adapter_token: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, ExecutionRequest):
            raise ContractValidationError("prepared execution request is invalid")
        if not isinstance(self.repository, RepositoryProvenance):
            raise ContractValidationError("prepared execution repository is invalid")
        if not isinstance(self.lifecycle, tuple) or not all(
            isinstance(event, LifecycleEvent) for event in self.lifecycle
        ):
            raise ContractValidationError(
                "prepared execution lifecycle must be a tuple of events"
            )
        if tuple(event.state for event in self.lifecycle) != (
            LifecycleState.CREATED,
            LifecycleState.PREPARED,
        ):
            raise ContractValidationError(
                "prepared execution must contain CREATED and PREPARED events"
            )
        transition_lifecycle(self.lifecycle[0].state, self.lifecycle[1].state)


class RunningExecution:
    """A started process with bounded process-group cancellation and collection."""

    def __init__(
        self,
        *,
        prepared: PreparedExecution,
        process: subprocess.Popen[bytes],
        command: tuple[str, ...],
        running_event: LifecycleEvent,
    ) -> None:
        self._prepared = prepared
        self._process = process
        self._command = command
        self._running_event = running_event
        self._started_at = running_event.observed_at
        self._cancellation_requested = False
        self._termination_signal: int | None = None
        self._record: ExecutionRecord | None = None
        self._collecting = False
        self._lock = threading.Lock()

    @property
    def process_id(self) -> int:
        return self._process.pid

    def cancel(self, grace_seconds: float = 2.0) -> bool:
        if (
            not isinstance(grace_seconds, (int, float))
            or isinstance(grace_seconds, bool)
            or grace_seconds <= 0
        ):
            raise ContractValidationError("cancellation grace must be positive")
        with self._lock:
            if self._record is not None or self._process.poll() is not None:
                return False
            try:
                os.killpg(self._process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return False
            self._cancellation_requested = True
            self._termination_signal = signal.SIGTERM
        try:
            self._process.wait(timeout=float(grace_seconds))
        except subprocess.TimeoutExpired:
            with self._lock:
                if self._process.poll() is None:
                    try:
                        os.killpg(self._process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    else:
                        self._termination_signal = signal.SIGKILL
            self._process.wait()
        return True

    def collect(self) -> ExecutionRecord:
        with self._lock:
            if self._record is not None:
                return self._record
            if self._collecting:
                raise ContractValidationError("execution collection is already in progress")
            self._collecting = True
        try:
            stdout_bytes, stderr_bytes = self._process.communicate()
            exit_code = self._process.returncode
            if exit_code is None:
                raise ContractValidationError("process ended without an exit status")
            with self._lock:
                cancellation_requested = self._cancellation_requested
                termination_signal = self._termination_signal
            if cancellation_requested:
                state = LifecycleState.CANCELLED
                failure = None
            elif exit_code == 0:
                state = LifecycleState.SUCCEEDED
                failure = None
            else:
                state = LifecycleState.FAILED
                failure = ExecutionFailureDetail(
                    kind="NONZERO_EXIT",
                    message=f"process exited with status {exit_code}",
                    exit_code=exit_code,
                )
                termination_signal = -exit_code if exit_code < 0 else None
            terminal_event = LifecycleEvent.observe(state)
            record = ExecutionRecord(
                request=self._prepared.request,
                state=state,
                command=self._command,
                repository=self._prepared.repository,
                started_at=self._started_at,
                ended_at=terminal_event.observed_at,
                exit_code=exit_code,
                termination_signal=termination_signal,
                stdout=CapturedArtifact.capture("STANDARD_OUTPUT", stdout_bytes),
                stderr=CapturedArtifact.capture("STANDARD_ERROR", stderr_bytes),
                lifecycle=(
                    *self._prepared.lifecycle,
                    self._running_event,
                    terminal_event,
                ),
                cancellation_requested=cancellation_requested,
                failure=failure,
            )
            with self._lock:
                self._record = record
            return record
        finally:
            with self._lock:
                self._collecting = False


class RegisteredCLIAdapter:
    """Execute one registered binary without interpreting scientific output."""

    def __init__(
        self,
        *,
        descriptor: AdapterDescriptor,
        registry: ResourceRegistry,
        runtime_resource_id: str,
        input_resource_ids: tuple[str, ...],
        repository_root: Path,
    ) -> None:
        if not isinstance(registry, ResourceRegistry):
            raise ContractValidationError(
                "CLI adapter requires a validated ResourceRegistry"
            )
        if not isinstance(runtime_resource_id, str) or not runtime_resource_id:
            raise ContractValidationError("CLI runtime resource ID is invalid")
        if not isinstance(input_resource_ids, tuple):
            raise ContractValidationError("CLI input resource IDs must be a tuple")
        if not input_resource_ids or not all(
            isinstance(resource_id, str) and resource_id
            for resource_id in input_resource_ids
        ):
            raise ContractValidationError(
                "CLI input resource IDs contain an invalid value"
            )
        if len(input_resource_ids) != len(set(input_resource_ids)):
            raise ContractValidationError("CLI input resource IDs contain duplicates")
        registry.verify_file_membership()
        runtime_resource = registry.resource(runtime_resource_id)
        input_resources = tuple(
            registry.resource(resource_id) for resource_id in input_resource_ids
        )
        self.descriptor = descriptor
        self._registry = registry
        self._runtime_resource = runtime_resource
        self._input_resources = input_resources
        self._repository_root = repository_root.resolve(strict=True)
        self._executable = runtime_resource.artifact("BINARY").identity
        self._working_directory = runtime_resource.source.local_root
        self._environment = runtime_resource.runtime_environment
        self._resources = (runtime_resource, *input_resources)
        self._preparation_token = object()
        self._validate_binding()

    @classmethod
    def from_registry(
        cls,
        *,
        descriptor: AdapterDescriptor,
        registry: ResourceRegistry,
        runtime_resource_id: str,
        input_resource_ids: tuple[str, ...],
        repository_root: Path,
    ) -> RegisteredCLIAdapter:
        return cls(
            descriptor=descriptor,
            registry=registry,
            runtime_resource_id=runtime_resource_id,
            input_resource_ids=input_resource_ids,
            repository_root=repository_root,
        )

    def _validate_binding(self) -> None:
        if self.descriptor.runtime != "CLI_PROCESS":
            raise ContractValidationError("CLI adapter descriptor runtime is not CLI_PROCESS")
        if self.descriptor.version != self._runtime_resource.version:
            raise ContractValidationError("adapter version differs from runtime resource")
        if self.descriptor.source != self._runtime_resource.source:
            raise ContractValidationError("adapter source differs from runtime resource")
        expected_models = tuple(
            item.identity
            for item in self._runtime_resource.artifacts_with_role("MODEL")
        )
        if self.descriptor.model_checkpoint_artifacts != expected_models:
            raise ContractValidationError(
                "adapter model/checkpoint declaration differs from registry"
            )
        expected_configuration = tuple(
            item.identity
            for item in self._runtime_resource.artifacts_with_role("CONFIGURATION")
        )
        if self.descriptor.configuration_artifacts != expected_configuration:
            raise ContractValidationError(
                "adapter configuration declaration differs from registry"
            )
        if self._executable.path is None or not os.access(self._executable.path, os.X_OK):
            raise ContractValidationError("registered CLI binary is not executable")
        if not Path(self._working_directory).is_dir():
            raise ContractValidationError("registered CLI working directory is invalid")
        resource_ids = tuple(resource.resource_id for resource in self._resources)
        if len(resource_ids) != len(set(resource_ids)):
            raise ContractValidationError("CLI binding contains duplicate resources")
        if not self._input_resources:
            raise ContractValidationError(
                "CLI binding must include at least one registered input resource"
            )
        registry_identity = self._runtime_resource.registry_file_identity
        for resource in self._resources:
            if resource.registry_file_identity != registry_identity:
                raise ContractValidationError(
                    "CLI binding resources do not share one registry identity"
                )
        expected_schemas = {
            "input_schema": "qualsight.execution.cli.file-input.v1",
            "output_schema": "qualsight.execution.cli.byte-streams.v1",
            "provenance_schema": "qualsight.execution.provenance.v1",
        }
        for field_name, expected in expected_schemas.items():
            if getattr(self.descriptor, field_name) != expected:
                raise ContractValidationError(
                    f"CLI adapter {field_name} is not the supported registered schema"
                )

    def _validate_batch_invocation(
        self,
        arguments: tuple[str, ...],
        input_artifacts: tuple[ArtifactIdentity, ...],
    ) -> tuple[str, ...]:
        """Batch shape: -c <registered configuration> -i <generated list> -o <output>.

        The list file is a caller-generated artifact (its identity travels in
        ``input_artifacts`` and must match the bytes on disk before every
        launch); it must be a ``.txt`` file whose lines are absolute paths of
        existing regular files. The output path must lie in the list file's own
        directory (the job directory) and must not already exist, so the engine
        can never be pointed at an arbitrary location or overwrite evidence.
        The semantic check that every listed path is a verified dataset sample
        belongs to the batch engine that generated the list.
        """

        configuration = self._runtime_resource.artifact("CONFIGURATION").identity
        if arguments[1] != configuration.path:
            raise ContractValidationError(f"unregistered CLI argument path: {arguments[1]}")
        list_path = arguments[3]
        output_path = arguments[5]
        if len(input_artifacts) != 1 or input_artifacts[0].path != list_path:
            raise ContractValidationError("batch request must carry the generated list artifact as its only input")
        list_identity = input_artifacts[0]
        if not list_path.endswith(".txt"):
            raise ContractValidationError("batch list file must be a .txt list of image paths")
        list_file = Path(list_path)
        if not list_file.is_absolute() or not list_file.is_file():
            raise ContractValidationError("batch list file must be an existing absolute path")
        list_identity.verify_file()
        job_dir = list_file.parent.resolve()
        output_file = Path(output_path)
        if not output_file.is_absolute() or output_file.parent.resolve() != job_dir:
            raise ContractValidationError("batch output must be written inside the list file's job directory")
        if output_file.exists():
            raise ContractValidationError("batch output path already exists; evidence is never overwritten")
        for line_number, line in enumerate(list_file.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                raise ContractValidationError(f"batch list line {line_number} is empty")
            entry = Path(line)
            if not entry.is_absolute() or not entry.is_file():
                raise ContractValidationError(f"batch list line {line_number} is not an existing absolute file: {line}")
        return ("-c", configuration.path, "-i", list_path, "-o", output_path)

    def _validate_invocation(
        self,
        arguments: tuple[str, ...],
        input_artifacts: tuple[ArtifactIdentity, ...],
    ) -> tuple[str, ...]:
        if not isinstance(arguments, tuple):
            raise ContractValidationError("CLI arguments must be a tuple")
        if (
            len(arguments) == 6
            and arguments[0] == "-c"
            and arguments[2] == "-i"
            and arguments[4] == "-o"
        ):
            return self._validate_batch_invocation(arguments, input_artifacts)
        if (
            len(arguments) != 4
            or arguments[0] != "-c"
            or arguments[2] != "-i"
        ):
            raise ContractValidationError(
                "CLI arguments must be exactly -c <registered configuration> "
                "-i <registered data> or the batch form -c <configuration> -i <generated list> -o <output>"
            )

        configuration = self._runtime_resource.artifact("CONFIGURATION").identity
        if arguments[1] != configuration.path:
            raise ContractValidationError(
                f"unregistered CLI argument path: {arguments[1]}"
            )

        registered_data = {
            artifact.identity.path: artifact.identity
            for resource in self._input_resources
            for artifact in resource.artifacts
            if artifact.role == "DATA" and artifact.identity.path is not None
        }
        selected = registered_data.get(arguments[3])
        if selected is None:
            raise ContractValidationError(
                f"unregistered CLI argument path: {arguments[3]}"
            )
        if input_artifacts != (selected,):
            raise ContractValidationError(
                "request input artifacts do not match the registered data argument"
            )
        if configuration.path is None or selected.path is None:
            raise ContractValidationError(
                "registered CLI invocation contains an artifact without a path"
            )
        return ("-c", configuration.path, "-i", selected.path)

    def _validate_authorization(self, purpose_scope: str) -> None:
        if (
            not isinstance(purpose_scope, str)
            or not purpose_scope
            or purpose_scope != purpose_scope.strip()
        ):
            raise ContractValidationError("purpose scope must be a nonempty string")
        for resource in self._resources:
            if (
                resource.state not in USABLE_RESOURCE_STATES
                or purpose_scope not in resource.approved_scopes
            ):
                raise ContractValidationError(
                    f"resource {resource.resource_id} is {resource.state} and does "
                    f"not approve purpose scope {purpose_scope}; no common approved "
                    "scope exists for this CLI binding"
                )

    def _validate_request(self, request: ExecutionRequest) -> None:
        if not isinstance(request, ExecutionRequest):
            raise ContractValidationError("CLI adapter requires an ExecutionRequest")
        self._registry.verify_file_membership()
        expected_resources = tuple(
            resource.execution_identity() for resource in self._resources
        )
        if request.descriptor != self.descriptor:
            raise ContractValidationError("request descriptor differs from adapter")
        if request.executable != self._executable:
            raise ContractValidationError(
                "request executable is not registered for this adapter"
            )
        if request.working_directory != self._working_directory:
            raise ContractValidationError(
                "request working directory differs from registry"
            )
        if request.environment != self._environment:
            raise ContractValidationError("request environment differs from registry")
        if request.resources != expected_resources:
            raise ContractValidationError(
                "request resource provenance differs from registry"
            )
        self._validate_invocation(request.arguments, request.input_artifacts)
        self._validate_authorization(request.purpose_scope)
        self._runtime_resource.registry_file_identity.verify_file()
        for resource in self._resources:
            for artifact in resource.artifacts:
                artifact.identity.verify_file()

    def create_request(
        self,
        *,
        arguments: tuple[str, ...],
        input_artifacts: tuple[ArtifactIdentity, ...],
        purpose_scope: str,
    ) -> ExecutionRequest:
        canonical_arguments = self._validate_invocation(arguments, input_artifacts)
        self._validate_authorization(purpose_scope)
        request = ExecutionRequest(
            descriptor=self.descriptor,
            executable=self._executable,
            arguments=canonical_arguments,
            working_directory=self._working_directory,
            environment=self._environment,
            input_artifacts=input_artifacts,
            resources=tuple(resource.execution_identity() for resource in self._resources),
            purpose_scope=purpose_scope,
        )
        self._validate_request(request)
        return request

    def prepare(self, request: ExecutionRequest) -> PreparedExecution:
        self._validate_request(request)
        created = LifecycleEvent.observe(LifecycleState.CREATED)
        prepared_state = transition_lifecycle(created.state, LifecycleState.PREPARED)
        prepared = LifecycleEvent.observe(prepared_state)
        return PreparedExecution(
            request=request,
            repository=capture_repository_provenance(self._repository_root),
            lifecycle=(created, prepared),
            _adapter_token=self._preparation_token,
        )

    def start(self, prepared: PreparedExecution) -> RunningExecution:
        if not isinstance(prepared, PreparedExecution):
            raise ContractValidationError("CLI start requires a PreparedExecution")
        if prepared._adapter_token is not self._preparation_token:
            raise ContractValidationError(
                "prepared execution was not produced by this adapter"
            )
        self._validate_request(prepared.request)
        running_state = transition_lifecycle(
            prepared.lifecycle[-1].state,
            LifecycleState.RUNNING,
        )
        running_event = LifecycleEvent.observe(running_state)
        executable_path = prepared.request.executable.path
        if executable_path is None:
            raise ContractValidationError("prepared executable has no file path")
        command = (executable_path, *prepared.request.arguments)
        process = subprocess.Popen(
            command,
            cwd=prepared.request.working_directory,
            env=dict(prepared.request.environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
        return RunningExecution(
            prepared=prepared,
            process=process,
            command=command,
            running_event=running_event,
        )

    def execute(self, request: ExecutionRequest) -> ExecutionRecord:
        record = self.start(self.prepare(request)).collect()
        record.raise_for_status()
        return record
