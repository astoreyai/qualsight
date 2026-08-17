from __future__ import annotations

from pathlib import Path
import unittest

from qualsight.execution.cli_runtime import PreparedExecution, RegisteredCLIAdapter
from qualsight.execution.contracts import (
    ArtifactIdentity,
    ContractValidationError,
    ExecutionRequest,
    LifecycleEvent,
    LifecycleState,
    capture_repository_provenance,
)
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_RUNTIME_RESOURCE_ID,
    build_ofiq_transport_descriptor,
    load_resource_registry,
)


ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "resources" / "resource-registry.json"
PRD_PATH = (ROOT / "PRD.md").resolve(strict=True)
ETC_PATH = Path("/etc/passwd").resolve(strict=True)


class RealOFIQCLIRefusalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_resource_registry(REGISTRY_PATH)
        cls.runtime = cls.registry.resource(OFIQ_RUNTIME_RESOURCE_ID)
        cls.input_resource = cls.registry.resource(OFIQ_INPUT_RESOURCE_ID)
        cls.executable = cls.runtime.artifact("BINARY").identity
        cls.config = cls.runtime.artifact("CONFIGURATION").identity
        cls.image = cls.input_resource.artifact("DATA").identity
        cls.prd = ArtifactIdentity.from_file(PRD_PATH)
        cls.adapter = RegisteredCLIAdapter.from_registry(
            descriptor=build_ofiq_transport_descriptor(cls.registry),
            registry=cls.registry,
            runtime_resource_id=OFIQ_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID,),
            repository_root=ROOT,
        )
        cls.resource_identities = tuple(
            resource.execution_identity()
            for resource in (cls.runtime, cls.input_resource)
        )
        registered_paths = {
            artifact.identity.path
            for resource in (cls.runtime, cls.input_resource)
            for artifact in resource.artifacts
        }
        if str(PRD_PATH) in registered_paths or str(ETC_PATH) in registered_paths:
            raise AssertionError("refusal paths unexpectedly appear in the real registry")

    def _caller_forged_request(
        self,
        *,
        executable: ArtifactIdentity,
        selected_input: Path,
        input_artifact: ArtifactIdentity,
    ) -> ExecutionRequest:
        return ExecutionRequest(
            descriptor=self.adapter.descriptor,
            executable=executable,
            arguments=(
                "-c",
                self.config.path,
                "-i",
                str(selected_input),
            ),
            working_directory=self.runtime.source.local_root,
            environment=self.runtime.runtime_environment,
            input_artifacts=(input_artifact,),
            resources=self.resource_identities,
            purpose_scope=self.input_resource.approved_scopes[0],
        )

    def test_raw_arguments_cannot_select_an_unregistered_etc_path(self) -> None:
        with self.assertRaisesRegex(
            ContractValidationError,
            r"(?i)unregistered.*(?:argument|path)|(?:argument|path).*unregistered",
        ):
            self.adapter.create_request(
                arguments=(
                    "-c",
                    str(ETC_PATH),
                    "-i",
                    self.image.path,
                ),
                input_artifacts=(self.image,),
                purpose_scope=self.input_resource.approved_scopes[0],
            )

    def test_raw_arguments_cannot_select_the_unregistered_prd(self) -> None:
        with self.assertRaisesRegex(
            ContractValidationError,
            r"(?i)unregistered.*(?:argument|path)|(?:argument|path).*unregistered",
        ):
            self.adapter.create_request(
                arguments=(
                    "-c",
                    self.config.path,
                    "-i",
                    str(PRD_PATH),
                ),
                input_artifacts=(self.image,),
                purpose_scope=self.input_resource.approved_scopes[0],
            )

    def test_caller_forged_request_cannot_bypass_registration_at_prepare(self) -> None:
        request = self._caller_forged_request(
            executable=self.executable,
            selected_input=PRD_PATH,
            input_artifact=self.prd,
        )
        with self.assertRaisesRegex(
            ContractValidationError,
            r"(?i)unregistered|registration",
        ):
            self.adapter.prepare(request)

    def test_caller_forged_prepared_value_is_revalidated_before_start(self) -> None:
        request = self._caller_forged_request(
            executable=self.prd,
            selected_input=Path(self.image.path),
            input_artifact=self.image,
        )
        prepared = PreparedExecution(
            request=request,
            repository=capture_repository_provenance(ROOT),
            lifecycle=(
                LifecycleEvent.observe(LifecycleState.CREATED),
                LifecycleEvent.observe(LifecycleState.PREPARED),
            ),
        )
        with self.assertRaisesRegex(
            ContractValidationError,
            r"(?i)unregistered|registration|prepared",
        ):
            self.adapter.start(prepared)

    def test_blocked_runtime_without_common_scope_is_refused(self) -> None:
        common_scopes = set(self.runtime.approved_scopes).intersection(
            self.input_resource.approved_scopes
        )
        self.assertEqual(self.runtime.state, "BLOCKED")
        self.assertEqual(self.runtime.approved_scopes, ())
        self.assertEqual(common_scopes, set())
        with self.assertRaisesRegex(
            ContractValidationError,
            r"(?i)blocked|common approved scope",
        ):
            self.adapter.create_request(
                arguments=(
                    "-c",
                    self.config.path,
                    "-i",
                    self.image.path,
                ),
                input_artifacts=(self.image,),
                purpose_scope=self.input_resource.approved_scopes[0],
            )


if __name__ == "__main__":
    unittest.main()
