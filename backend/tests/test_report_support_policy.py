"""Scientific-support aggregation policy; no biometric data are constructed."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from qualsight.analysis.utility import quality_input_stage
from qualsight.matchers.conditions import ConditionId
from qualsight.reporting.reports import (
    aggregate_scientific_support,
    quality_input_stage_summary,
)


ROOT = Path(__file__).resolve().parents[2]


class ReportSupportPolicyTests(unittest.TestCase):
    def test_quality_input_stage_distinguishes_publisher_prealigned_pass_through(self) -> None:
        self.assertEqual(
            quality_input_stage([ConditionId.PREALIGNED_ARCFACE112.value]),
            {
                "stage_id": "REGISTERED_DATASET_SAMPLE_SOURCE_ARTIFACT_AS_QUALITY_ENGINE_INPUT",
                "artifact_kind": "DATASET_SAMPLE_SOURCE_BYTES",
                "matcher_preprocessing_relationships": [
                    {
                        "condition_id": ConditionId.PREALIGNED_ARCFACE112.value,
                        "relationship": "SAME_PUBLISHER_PREALIGNED_IMAGE_DECODED_WITHOUT_FACE_DETECTION_REALIGNMENT_OR_CROP",
                    }
                ],
            },
        )

    def test_most_restrictive_recorded_state_controls_report_aggregate(self) -> None:
        self.assertEqual(
            aggregate_scientific_support({"SUPPORTED", "NOT_SUPPORTED"}),
            "NOT_SUPPORTED",
        )
        self.assertEqual(
            aggregate_scientific_support({"SUPPORTED", "BLOCKED"}),
            "BLOCKED",
        )
        self.assertEqual(
            aggregate_scientific_support({"SUPPORTED", "NOT_ASSESSED"}),
            "NOT_ASSESSED",
        )
        self.assertEqual(aggregate_scientific_support({"SUPPORTED"}), "SUPPORTED")
        self.assertEqual(aggregate_scientific_support(set()), "NOT_ASSESSED")

    def test_real_historical_utility_sources_receive_a_conservative_stage_label(self) -> None:
        fixture = json.loads(
            (
                ROOT
                / "backend"
                / "tests"
                / "fixtures"
                / "real_legacy_scientific_record_subset_2026-08-17.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            fixture["provenance"]["kind"],
            "REAL_HISTORICAL_QUALSIGHT_RECORD_SUBSET",
        )
        self.assertEqual(
            quality_input_stage_summary(fixture["utility_sources"]),
            (
                "NOT_RECORDED_LEGACY_ARTIFACT",
                "NOT_RECORDED_LEGACY_ARTIFACT",
            ),
        )


if __name__ == "__main__":
    unittest.main()
