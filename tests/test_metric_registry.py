from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from scripts.validate_metric_registry import validate_registry_file


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "metrics" / "metric-registry.json"
CORE_IDS = {
    "biometric.verification.fmr",
    "biometric.verification.fnmr",
    "biometric.verification.roc",
    "biometric.verification.det",
    "biometric.verification.eer",
    "biometric.quality.utility.erc",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class MetricRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        cls.metrics = {
            metric["metric_id"]: metric for metric in cls.registry["metrics"]
        }

    def test_shipped_registry_validates_against_local_authorities(self) -> None:
        self.assertEqual(
            validate_registry_file(REGISTRY_PATH),
            {
                "registry_id": "qualsight-core-metrics",
                "registry_version": "1.0.1",
                "metrics": 6,
                "definition_versions": ["1.0.0"],
                "authorities": 2,
                "requirements": 9,
            },
        )

    def test_authority_records_match_repository_bytes(self) -> None:
        authorities = {
            authority["source_id"]: authority
            for authority in self.registry["authority_documents"]
        }
        self.assertEqual(
            authorities,
            {
                "prd-0.3": {
                    "source_id": "prd-0.3",
                    "path": "PRD.md",
                    "version": "0.3",
                    "sha256": sha256(ROOT / "PRD.md"),
                },
                "requirements-0.2": {
                    "source_id": "requirements-0.2",
                    "path": "REQUIREMENTS.md",
                    "version": "0.2",
                    "sha256": sha256(ROOT / "REQUIREMENTS.md"),
                },
            },
        )

    def test_registry_contains_only_the_six_versioned_core_definitions(self) -> None:
        self.assertEqual(set(self.metrics), CORE_IDS)
        self.assertEqual(
            {metric["definition_version"] for metric in self.metrics.values()},
            {"1.0.0"},
        )

    def test_every_metric_carries_the_prd_definition_contract(self) -> None:
        required_fields = {
            "metric_id",
            "canonical_name",
            "aliases",
            "domain",
            "definition",
            "formula",
            "higher_or_lower_is_better",
            "required_inputs",
            "aggregation_unit",
            "threshold_semantics",
            "confidence_interval_methods",
            "source_standard",
            "source_version",
            "references",
            "implementation",
            "implementation_version",
            "limitations",
            "warnings",
        }
        for metric in self.metrics.values():
            with self.subTest(metric_id=metric["metric_id"]):
                self.assertLessEqual(required_fields, metric.keys())
                self.assertEqual(metric["source_standard"], "QUALSIGHT_PRD")
                self.assertEqual(metric["source_version"], "0.3")
                self.assertEqual(
                    {reference["source_id"] for reference in metric["references"]},
                    {"prd-0.3", "requirements-0.2"},
                )

    def test_fmr_and_fnmr_define_distinct_real_trial_populations(self) -> None:
        fmr = self.metrics["biometric.verification.fmr"]["formula"]
        fnmr = self.metrics["biometric.verification.fnmr"]["formula"]
        self.assertEqual(
            (
                fmr["numerator_population"],
                fmr["numerator_event"],
                fmr["denominator_population"],
                fmr["denominator_policy"],
            ),
            (
                "NON_MATED",
                "ACCEPT",
                "INCLUDED_NON_MATED_TRIALS",
                "RECORDED_FAILURE_POLICY",
            ),
        )
        self.assertEqual(
            (
                fnmr["numerator_population"],
                fnmr["numerator_event"],
                fnmr["denominator_population"],
                fnmr["denominator_policy"],
            ),
            (
                "MATED",
                "REJECT",
                "INCLUDED_MATED_TRIALS",
                "RECORDED_FAILURE_POLICY",
            ),
        )

    def test_roc_and_det_derive_from_the_same_fmr_fnmr_threshold_sweep(self) -> None:
        roc = self.metrics["biometric.verification.roc"]["formula"]
        det = self.metrics["biometric.verification.det"]["formula"]
        for formula in (roc, det):
            self.assertEqual(formula["x_metric_id"], "biometric.verification.fmr")
            self.assertEqual(formula["x_definition_version"], "1.0.0")
            self.assertEqual(formula["y_metric_id"], "biometric.verification.fnmr")
            self.assertEqual(formula["y_definition_version"], "1.0.0")
            self.assertEqual(formula["threshold_sweep"], "RECORDED_MATCHER_THRESHOLD_SET")
        self.assertEqual(roc["y_operation"], "ONE_MINUS")
        self.assertEqual(det["y_operation"], "IDENTITY")

    def test_eer_requires_equality_and_a_recorded_empirical_method(self) -> None:
        eer = self.metrics["biometric.verification.eer"]
        self.assertEqual(
            eer["formula"],
            {
                "kind": "EQUALITY_OPERATING_POINT",
                "formula_version": "1.0.0",
                "left_metric_id": "biometric.verification.fmr",
                "left_definition_version": "1.0.0",
                "relation": "EQUALS",
                "right_metric_id": "biometric.verification.fnmr",
                "right_definition_version": "1.0.0",
                "empirical_estimator": "REQUIRES_RECORDED_VERSIONED_METHOD",
            },
        )
        self.assertIn("eer_estimation_method", eer["required_inputs"])

    def test_erc_freezes_threshold_pair_quality_and_rejection_semantics(self) -> None:
        erc = self.metrics["biometric.quality.utility.erc"]
        self.assertEqual(
            erc["formula"],
            {
                "kind": "REJECTION_CURVE",
                "formula_version": "1.0.0",
                "x_quantity": "REJECTED_FRACTION",
                "y_quantity": "RETAINED_BASE_ERROR_RATE",
                "rejection_order": "WORST_PAIR_QUALITY_FIRST",
                "tie_policy": "REJECT_EQUAL_QUALITY_TOGETHER",
                "matcher_threshold_policy": "FIXED_ACROSS_CURVE",
                "base_error_metric": "REQUIRED_REGISTERED_INPUT",
            },
        )
        self.assertEqual(
            erc["threshold_semantics"]["threshold_reestimation"],
            "FORBIDDEN_ACROSS_CURVE",
        )
        self.assertLessEqual(
            {
                "registered_base_error_metric",
                "fixed_matcher_threshold",
                "score_direction",
                "threshold_equality_rule",
                "pair_quality_policy_id",
                "quality_direction",
                "processing_failure_policy",
                "rejection_domain",
            },
            set(erc["required_inputs"]),
        )
        for metric in self.metrics.values():
            with self.subTest(metric_id=metric["metric_id"]):
                self.assertEqual(
                    metric["implementation"],
                    {
                        "kind": "DEFINITION_ONLY",
                        "artifact": "metrics/metric-registry.json",
                    },
                )


if __name__ == "__main__":
    unittest.main()
