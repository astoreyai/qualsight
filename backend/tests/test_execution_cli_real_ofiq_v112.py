"""Real lifecycle evidence for the registered OFIQ v1.1.2 runtime.

Every test launches the actual installed OFIQSampleApp on the registered r-01
conformance input under the shared REPRODUCE_OFIQ_CONFORMANCE_TESTS scope. No
fixture, stub, or historical record substitutes for the process.
"""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path
import signal
import time
import unittest

from qualsight.execution.cli_runtime import RegisteredCLIAdapter
from qualsight.execution.contracts import (
    ContractValidationError,
    ExecutionRecord,
    LifecycleState,
)
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
    build_ofiq_transport_descriptor,
    load_resource_registry,
)


ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "resources" / "resource-registry.json"
CONFORMANCE_SCOPE = "REPRODUCE_OFIQ_CONFORMANCE_TESTS"
# The r-01 target values reproduced by the historical 1.1.0 engine and by the
# 1.1.2 conformance table (data/tests/expected_results/expected_results.csv).
EXPECTED_UQS_NATIVE = "26.390884"
EXPECTED_UQS_SCALAR = "79"


class RealOFIQv112LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_resource_registry(REGISTRY_PATH)
        cls.runtime = cls.registry.resource(OFIQ_V112_RUNTIME_RESOURCE_ID)
        cls.input_resource = cls.registry.resource(OFIQ_INPUT_RESOURCE_ID)
        cls.config = cls.runtime.artifact("CONFIGURATION").identity
        cls.image = cls.input_resource.artifact("DATA").identity
        cls.adapter = RegisteredCLIAdapter.from_registry(
            descriptor=build_ofiq_transport_descriptor(
                cls.registry, OFIQ_V112_RUNTIME_RESOURCE_ID
            ),
            registry=cls.registry,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID,),
            repository_root=ROOT,
        )

    def _request(self):
        return self.adapter.create_request(
            arguments=("-c", self.config.path, "-i", self.image.path),
            input_artifacts=(self.image,),
            purpose_scope=CONFORMANCE_SCOPE,
        )

    def test_runtime_and_input_share_the_conformance_scope(self) -> None:
        self.assertEqual(self.runtime.state, "APPROVED_LOCAL_RESEARCH_USE")
        self.assertEqual(self.runtime.version, "1.1.2")
        self.assertIn(CONFORMANCE_SCOPE, self.runtime.approved_scopes)
        self.assertIn(CONFORMANCE_SCOPE, self.input_resource.approved_scopes)
        self.assertEqual(
            self.registry.common_approved_scopes(
                (OFIQ_V112_RUNTIME_RESOURCE_ID, OFIQ_INPUT_RESOURCE_ID)
            ),
            (CONFORMANCE_SCOPE,),
        )

    def test_scope_outside_the_input_grant_is_refused_before_launch(self) -> None:
        # The runtime approves general local research; the restricted
        # conformance image does not, so the pair has no common scope for it.
        with self.assertRaisesRegex(ContractValidationError, r"(?i)common approved scope"):
            self.adapter.create_request(
                arguments=("-c", self.config.path, "-i", self.image.path),
                input_artifacts=(self.image,),
                purpose_scope="QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1",
            )

    def test_real_execution_succeeds_and_reproduces_the_r01_target(self) -> None:
        record = self.adapter.execute(self._request())
        self.assertIsInstance(record, ExecutionRecord)
        self.assertEqual(record.state, LifecycleState.SUCCEEDED)
        self.assertEqual(record.exit_code, 0)
        self.assertIsNone(record.termination_signal)
        self.assertIsNone(record.failure)
        self.assertFalse(record.cancellation_requested)
        self.assertEqual(
            tuple(event.state for event in record.lifecycle),
            (
                LifecycleState.CREATED,
                LifecycleState.PREPARED,
                LifecycleState.RUNNING,
                LifecycleState.SUCCEEDED,
            ),
        )
        self.assertEqual(record.command[0], self.runtime.artifact("BINARY").identity.path)
        stdout = record.stdout.content.decode("utf-8")
        self.assertIn("OFIQ library version: 1.1.2", stdout)
        csv_lines = [line for line in stdout.splitlines() if line.startswith("Filename;") or line.startswith("/")]
        self.assertEqual(len(csv_lines), 2, stdout)
        rows = list(csv.DictReader(io.StringIO("\n".join(csv_lines)), delimiter=";"))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["Filename"], self.image.path)
        self.assertEqual(row["UnifiedQualityScore.native"], EXPECTED_UQS_NATIVE)
        self.assertEqual(row["UnifiedQualityScore.scalar"], EXPECTED_UQS_SCALAR)
        self.assertEqual(record.stdout.identity.size_bytes, len(record.stdout.content))
        self.assertGreater(record.ended_at, record.started_at)

    def test_cancellation_terminates_the_process_group(self) -> None:
        running = self.adapter.start(self.adapter.prepare(self._request()))
        pid = running.process_id
        # The engine spends >2 s initializing models, so it is still alive here.
        self.assertTrue(running.cancel(grace_seconds=5.0))
        record = running.collect()
        self.assertEqual(record.state, LifecycleState.CANCELLED)
        self.assertTrue(record.cancellation_requested)
        self.assertEqual(record.termination_signal, int(signal.SIGTERM))
        self.assertLess(record.exit_code, 0)
        self.assertIsNone(record.failure)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        # A second cancellation is a no-op once the record exists.
        self.assertFalse(running.cancel())
        self.assertIs(running.collect(), record)

    def test_cancel_after_completion_returns_false(self) -> None:
        running = self.adapter.start(self.adapter.prepare(self._request()))
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            try:
                os.kill(running.process_id, 0)
            except ProcessLookupError:
                break
            if running._process.poll() is not None:
                break
            time.sleep(0.2)
        record = running.collect()
        self.assertEqual(record.state, LifecycleState.SUCCEEDED)
        self.assertFalse(running.cancel())


if __name__ == "__main__":
    unittest.main()
