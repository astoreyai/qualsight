from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from qualsight.domain.run import (
    CompletedRun,
    ReproductionState,
    RunAttestationKind,
    RunStatus,
)
from tests.real_fixture import FIXTURE_PATH, load_fixture


class CompletedRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = load_fixture(FIXTURE_PATH)
        cls.completed_run = cls.fixture.run

    def test_shipped_run_is_the_real_ofiq_conformance_capture(self) -> None:
        self.assertEqual(self.completed_run.status, RunStatus.COMPLETED)
        self.assertEqual(self.completed_run.quality_engine.version, "1.1.0")
        metrics = {
            metric.metric_id: metric.value for metric in self.completed_run.metrics
        }
        self.assertEqual(metrics["UnifiedQualityScore"], 26.390884)
        self.assertEqual(metrics["UnifiedQualityScore.scalar"], 79)
        self.assertEqual(
            self.fixture.sample.sha256,
            "3c3655e6ac9f16bc6909b6f71d7c5ec385008aa6d2352167bdb77acd61d8c947",
        )
        self.assertEqual(self.completed_run.scientific_support.value, "NOT_ASSESSED")
        self.assertEqual(
            self.completed_run.dataset_hash,
            self.fixture.dataset.manifest.sha256,
        )

    def test_completed_run_and_nested_metrics_are_frozen(self) -> None:
        with self.assertRaises(ValidationError):
            self.completed_run.runtime_seconds = self.completed_run.runtime_seconds
        with self.assertRaises(ValidationError):
            self.completed_run.metrics[0].value = self.completed_run.metrics[0].value
        self.assertIsInstance(self.completed_run.metrics, tuple)
        self.assertIsInstance(self.completed_run.model_hashes, tuple)
        self.assertIsInstance(self.completed_run.warnings, tuple)

    def test_completed_run_round_trip_preserves_every_recorded_value(self) -> None:
        restored = CompletedRun.model_validate_json(
            self.completed_run.model_dump_json()
        )
        self.assertEqual(restored, self.completed_run)

    def test_execution_attestation_and_reproduction_states_remain_distinct(self) -> None:
        self.assertEqual(
            {state.value for state in RunStatus},
            {
                "QUEUED",
                "RUNNING",
                "FAILED",
                "CANCELLED",
                "COMPLETED",
                "VALIDATED",
                "REPRODUCED",
            },
        )
        self.assertEqual(
            {kind.value for kind in RunAttestationKind},
            {"VALIDATED", "REPRODUCED", "SCIENTIFIC_SUPPORT"},
        )
        self.assertEqual(
            {state.value for state in ReproductionState},
            {
                "EXACT",
                "WITHIN_TOLERANCE",
                "DIFFERENT",
                "MISSING_DEPENDENCY",
                "MISSING_DATA",
                "BLOCKED",
            },
        )
        schema_text = json.dumps(CompletedRun.model_json_schema(), sort_keys=True)
        self.assertIn('"supersedes_run_id"', schema_text)
        self.assertIn('"additionalProperties": false', schema_text)
        self.assertNotIn('"SUPPORTED"', schema_text)


if __name__ == "__main__":
    unittest.main()
