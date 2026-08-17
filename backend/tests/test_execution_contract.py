from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
from typing import Callable, get_type_hints
import unittest

from qualsight.execution.cli_runtime import (
    RegisteredCLIAdapter,
    RunningExecution,
)
from qualsight.execution.contracts import (
    ContractValidationError,
    ExecutionAdapter,
    ExecutionHandle,
    ExecutionRecord,
    ExecutionRequest,
    PreparedExecutionContract,
)
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_RUNTIME_RESOURCE_ID,
    build_ofiq_transport_descriptor,
    load_resource_registry,
)


ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "resources" / "resource-registry.json"


class ExecutionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry_mapping = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        cls.registry = load_resource_registry(REGISTRY_PATH)
        cls.runtime = cls.registry.resource(OFIQ_RUNTIME_RESOURCE_ID)
        cls.input_resource = cls.registry.resource(OFIQ_INPUT_RESOURCE_ID)
        cls.descriptor = build_ofiq_transport_descriptor(cls.registry)

    def _adapter(self) -> RegisteredCLIAdapter:
        return RegisteredCLIAdapter.from_registry(
            descriptor=self.descriptor,
            registry=self.registry,
            runtime_resource_id=OFIQ_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID,),
            repository_root=ROOT,
        )

    def _request_arguments(self) -> tuple[str, ...]:
        configuration = self.runtime.artifact("CONFIGURATION").identity
        image = self.input_resource.artifact("DATA").identity
        self.assertIsNotNone(configuration.path)
        self.assertIsNotNone(image.path)
        return ("-c", configuration.path, "-i", image.path)

    def _real_request(self) -> ExecutionRequest:
        image = self.input_resource.artifact("DATA").identity
        return ExecutionRequest(
            descriptor=self.descriptor,
            executable=self.runtime.artifact("BINARY").identity,
            arguments=self._request_arguments(),
            working_directory=self.runtime.source.local_root,
            environment=self.runtime.runtime_environment,
            input_artifacts=(image,),
            resources=(
                self.runtime.execution_identity(),
                self.input_resource.execution_identity(),
            ),
            purpose_scope=self.input_resource.approved_scopes[0],
        )

    def test_execution_adapter_and_running_handle_have_typed_surfaces(self) -> None:
        adapter_hints = get_type_hints(ExecutionAdapter)
        expected_adapter_hints = {
            "prepare": Callable[[ExecutionRequest], PreparedExecutionContract],
            "start": Callable[[PreparedExecutionContract], ExecutionHandle],
            "execute": Callable[[ExecutionRequest], ExecutionRecord],
        }
        for member, expected in expected_adapter_hints.items():
            with self.subTest(member=member):
                self.assertEqual(adapter_hints[member], expected)

        self.assertEqual(get_type_hints(RunningExecution.cancel)["return"], bool)
        self.assertEqual(
            get_type_hints(RunningExecution.collect)["return"],
            ExecutionRecord,
        )

    def test_request_and_nested_provenance_are_deeply_immutable(self) -> None:
        request = self._real_request()
        self.assertIsInstance(request.arguments, tuple)
        self.assertIsInstance(request.environment, tuple)
        self.assertIsInstance(request.input_artifacts, tuple)
        self.assertIsInstance(request.resources, tuple)
        for resource in request.resources:
            self.assertIsInstance(resource.approved_scopes, tuple)
            self.assertIsInstance(resource.blocked_reasons, tuple)
            self.assertIsInstance(resource.artifacts, tuple)

        with self.assertRaises(FrozenInstanceError):
            request.arguments = request.arguments
        with self.assertRaises(FrozenInstanceError):
            request.resources[0].approved_scopes = request.resources[0].approved_scopes
        with self.assertRaises(FrozenInstanceError):
            request.resources[0].artifacts[0].sha256 = (
                request.resources[0].artifacts[0].sha256
            )

    def test_adapter_binding_rejects_a_mutable_resource_sequence(self) -> None:
        with self.assertRaisesRegex(
            ContractValidationError,
            r"input resource IDs must be a tuple",
        ):
            RegisteredCLIAdapter.from_registry(
                descriptor=self.descriptor,
                registry=self.registry,
                runtime_resource_id=OFIQ_RUNTIME_RESOURCE_ID,
                input_resource_ids=[OFIQ_INPUT_RESOURCE_ID],  # type: ignore[arg-type]
                repository_root=ROOT,
            )

    def test_adapter_factory_resolves_exact_members_of_the_validated_registry(
        self,
    ) -> None:
        adapter = self._adapter()
        self.assertEqual(adapter.descriptor, self.descriptor)

    def test_registered_resource_rejects_every_operational_projection_drift(
        self,
    ) -> None:
        projection_fields = (
            "kind",
            "name",
            "state",
            "version",
            "source",
            "license_expressions",
            "use_restrictions",
            "redistribution_status",
            "research_use_status",
            "data_classification",
            "runtime_platform",
            "runtime_environment",
            "consumers",
            "approved_scopes",
            "blocked_reasons",
            "artifacts",
        )
        for field_name in projection_fields:
            alternate = next(
                resource
                for resource in self.registry.resources
                if getattr(resource, field_name) != getattr(self.runtime, field_name)
            )
            with self.subTest(field=field_name), self.assertRaisesRegex(
                ContractValidationError,
                rf"{field_name}.*canonical registry record",
            ):
                replace(
                    self.runtime,
                    **{field_name: getattr(alternate, field_name)},
                )

        artifacts = list(self.runtime.artifacts)
        binary_index = next(
            index for index, artifact in enumerate(artifacts) if artifact.role == "BINARY"
        )
        configuration_index = next(
            index
            for index, artifact in enumerate(artifacts)
            if artifact.role == "CONFIGURATION"
        )
        binary = artifacts[binary_index]
        configuration = artifacts[configuration_index]
        artifacts[binary_index] = replace(binary, role=configuration.role)
        artifacts[configuration_index] = replace(configuration, role=binary.role)
        with self.subTest(field="artifact_roles"), self.assertRaisesRegex(
            ContractValidationError,
            r"artifacts.*canonical registry record",
        ):
            replace(self.runtime, artifacts=tuple(artifacts))

    def test_execution_provenance_identifies_registry_and_full_resource_records(
        self,
    ) -> None:
        raw_resources = {
            resource["resource_id"]: resource
            for resource in self.registry_mapping["resources"]
        }
        expected_registry_identity = self.registry.file_identity.to_mapping()
        for resource in (self.runtime, self.input_resource):
            raw_record = raw_resources[resource.resource_id]
            canonical_record = json.dumps(
                raw_record,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            expected = {
                "registry_file_identity": expected_registry_identity,
                "resource_record_canonical_json": canonical_record,
                "resource_record_sha256": hashlib.sha256(
                    canonical_record.encode("utf-8")
                ).hexdigest(),
            }
            observed = resource.execution_identity().to_mapping()
            for field, value in expected.items():
                with self.subTest(resource=resource.resource_id, field=field):
                    self.assertEqual(observed.get(field), value)

    def test_same_named_scope_is_required_on_every_consumed_resource(self) -> None:
        purpose_scope = self.input_resource.approved_scopes[0]
        self.assertIn(purpose_scope, self.input_resource.approved_scopes)
        self.assertNotIn(purpose_scope, self.runtime.approved_scopes)
        image = self.input_resource.artifact("DATA").identity
        with self.assertRaisesRegex(
            ContractValidationError,
            rf"{OFIQ_RUNTIME_RESOURCE_ID}.*{purpose_scope}|{purpose_scope}.*{OFIQ_RUNTIME_RESOURCE_ID}",
        ):
            self._adapter().create_request(
                arguments=self._request_arguments(),
                input_artifacts=(image,),
                purpose_scope=purpose_scope,
            )

    def test_blocked_runtime_is_refused_during_prepare_before_launch(self) -> None:
        self.assertEqual(self.runtime.state, "BLOCKED")
        with self.assertRaisesRegex(
            ContractValidationError,
            rf"{OFIQ_RUNTIME_RESOURCE_ID}.*BLOCKED|BLOCKED.*{OFIQ_RUNTIME_RESOURCE_ID}",
        ):
            self._adapter().prepare(self._real_request())


if __name__ == "__main__":
    unittest.main()
