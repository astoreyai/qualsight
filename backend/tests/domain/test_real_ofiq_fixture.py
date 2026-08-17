from __future__ import annotations

import ast
import json
import hashlib
import inspect
from datetime import datetime
from typing import get_args
import unittest
from pathlib import Path

from qualsight.domain.primitives import EvidenceState
from tests.real_fixture import (
    FIXTURE_PATH,
    PROJECT_ROOT,
    REGISTRY_PATH,
    RUNTIME_RESOURCE_ID,
    INPUT_RESOURCE_ID,
    RealCapturedFixture,
    _require_execution_authorization,
    capture_fixture,
    dependency_manifest_is_current,
    load_fixture,
    verify_live,
    verify_recorded_bytes,
)


class RealOfiqFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = load_fixture(FIXTURE_PATH)

    def test_fixture_provenance_and_every_preserved_byte_are_current(self) -> None:
        verified = verify_recorded_bytes(self.fixture)
        self.assertGreaterEqual(verified, 17)
        # The recorded dependency lock is historical environment evidence: the
        # live uv.lock gained fastapi/uvicorn/httpx on 2026-08-17 for QS-A08, so
        # the historical Run keeps its own recorded identity.
        self.assertFalse(dependency_manifest_is_current(self.fixture))
        self.assertEqual(
            self.fixture.run.environment.dependency_manifest.path,
            str(PROJECT_ROOT / "uv.lock"),
        )
        self.assertEqual(
            self.fixture.provenance.approved_scope,
            "REPRODUCE_OFIQ_CONFORMANCE_TESTS",
        )
        self.assertNotEqual(
            self.fixture.dataset.manifest.sha256,
            self.fixture.provenance.registry.sha256,
        )
        self.assertEqual(
            self.fixture.provenance.registry_preservation.state,
            EvidenceState.NOT_RECORDED,
        )
        self.assertIsNone(self.fixture.provenance.registry_preservation.value)

    def test_missing_historical_capture_tool_is_recorded_without_relabeling(self) -> None:
        completed_ns = int(
            datetime.fromisoformat(
                self.fixture.provenance.capture_completed_at.isoformat()
            ).timestamp()
            * 1_000_000_000
        )
        self.assertIsNone(self.fixture.provenance.capture_tool)
        self.assertEqual(
            self.fixture.provenance.capture_tool_evidence.state,
            EvidenceState.NOT_RECORDED,
        )
        self.assertGreater(
            self.fixture.provenance.verification_tool.mtime_ns,
            completed_ns,
        )

    def test_unpreserved_historical_registry_refuses_live_reproduction(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "live verification requires preserved historical registry bytes",
        ):
            verify_live(self.fixture, REGISTRY_PATH)

    def test_blocked_runtime_has_no_execution_authorization(self) -> None:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        resources = {
            resource["resource_id"]: resource for resource in registry["resources"]
        }
        with self.assertRaisesRegex(
            ValueError,
            rf"{RUNTIME_RESOURCE_ID}.*BLOCKED",
        ):
            _require_execution_authorization(
                resources[RUNTIME_RESOURCE_ID],
                resources[INPUT_RESOURCE_ID],
            )

    def test_capture_emits_the_schema_version_accepted_by_the_fixture_model(self) -> None:
        function = ast.parse(inspect.getsource(capture_fixture)).body[0]
        fixture_returns = [
            node.value
            for node in ast.walk(function)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == RealCapturedFixture.__name__
        ]
        self.assertEqual(len(fixture_returns), 1)
        schema_arguments = [
            keyword.value
            for keyword in fixture_returns[0].keywords
            if keyword.arg == "fixture_schema_version"
        ]
        self.assertEqual(len(schema_arguments), 1)
        accepted_versions = get_args(
            RealCapturedFixture.model_fields["fixture_schema_version"].annotation
        )
        self.assertIn(ast.literal_eval(schema_arguments[0]), accepted_versions)

    def test_restricted_image_remains_external_and_uncopied(self) -> None:
        image_path = Path(self.fixture.sample.source_path).resolve()
        self.assertFalse(image_path.is_relative_to(PROJECT_ROOT.resolve()))
        matching_project_files = []
        for candidate in PROJECT_ROOT.rglob("*"):
            if not candidate.is_file():
                continue
            if candidate.stat().st_size != self.fixture.sample.size_bytes:
                continue
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if digest == self.fixture.sample.sha256:
                matching_project_files.append(candidate)
        self.assertEqual(matching_project_files, [])

    def test_actual_completion_audit_links_to_the_captured_run(self) -> None:
        event = self.fixture.audit_event
        self.assertTrue(event.local_only)
        self.assertEqual(event.subject_id, self.fixture.run.run_id)
        self.assertEqual(event.project_id, self.fixture.project.project_id)
        self.assertEqual(event.event_type.value, "RUN_COMPLETED")
        for log in self.fixture.run.logs:
            self.assertEqual(
                log.sha256,
                hashlib.sha256(log.text.encode("utf-8")).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
