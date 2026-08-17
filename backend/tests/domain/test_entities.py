from __future__ import annotations

import json
import unittest

from pydantic import TypeAdapter

from qualsight.domain.entities import (
    DataClassification,
    Experiment,
    IdentificationMode,
    Modality,
)
from tests.real_fixture import FIXTURE_PATH, load_fixture


class RealEntitySchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = load_fixture(FIXTURE_PATH)

    def test_real_entities_round_trip_through_strict_schemas(self) -> None:
        entities = (
            self.fixture.project,
            self.fixture.dataset,
            self.fixture.sample,
            self.fixture.experiment,
            self.fixture.run.artifacts[0],
            self.fixture.audit_event,
        )
        for entity in entities:
            restored = type(entity).model_validate_json(entity.model_dump_json())
            self.assertEqual(restored, entity)

    def test_dataset_classification_excludes_prohibited_data_classes(self) -> None:
        schema_text = json.dumps(
            TypeAdapter(DataClassification).json_schema(),
            sort_keys=True,
        )
        self.assertNotIn("SYNTHETIC", schema_text)
        self.assertNotIn("GENERATED", schema_text)
        self.assertEqual(
            {member.value for member in DataClassification},
            {
                "PUBLIC",
                "PRIVATE",
                "RESTRICTED",
                "SENSITIVE_BIOMETRIC_DERIVED",
            },
        )

    def test_modality_and_identification_contract_are_present(self) -> None:
        self.assertEqual(self.fixture.sample.modality, Modality.FACE)
        schema_text = json.dumps(Experiment.model_json_schema(), sort_keys=True)
        self.assertIn('"IDENTIFICATION"', schema_text)
        self.assertIn('"gallery_manifest"', schema_text)
        self.assertIn('"probe_manifest"', schema_text)
        self.assertIn('"rank_depth"', schema_text)
        self.assertIn('"mode"', schema_text)
        self.assertEqual(
            {mode.value for mode in IdentificationMode},
            {"OPEN_SET", "CLOSED_SET"},
        )

    def test_real_project_carries_typed_restrictions(self) -> None:
        project = self.fixture.project
        dataset = self.fixture.dataset
        self.assertEqual(project.classification, dataset.classification)
        self.assertEqual(
            project.redistribution_permission,
            dataset.redistribution_permission,
        )
        self.assertEqual(project.display_permission, dataset.display_permission)
        self.assertEqual(project.use_restrictions, tuple(project.notes))

    def test_real_single_sample_context_records_explicit_non_applicability(self) -> None:
        run = self.fixture.run
        for binding in (
            run.subject_protocol,
            run.pair_search_protocol,
            run.matcher,
            run.matcher_preprocessing,
            run.operating_thresholds,
            run.statistical_methods,
            run.confidence_interval_method,
            run.randomness,
        ):
            self.assertEqual(binding.state.value, "NOT_APPLICABLE")
            self.assertTrue(binding.reason)


if __name__ == "__main__":
    unittest.main()
