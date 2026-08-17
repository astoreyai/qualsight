from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_MANIFEST = Path(
    "/mnt/projects/02_perception_biometrics/ofiq_29794/artifacts/local/runs/"
    "20260814-branch-b/aligned_crop_runs/lfw-full-v1/manifest.json"
)
GENERATOR_SOURCE = Path(
    "/mnt/projects/02_perception_biometrics/ofiq_29794/research/paper39/"
    "scripts/extract_aligned_crops.py"
)
SCRFD_SOURCE = Path(
    "/mnt/projects/02_perception_biometrics/ofiq_29794/packages/openfiqa/"
    "src/openfiqa/models/scrfd.py"
)
SOURCE_SEAL = Path(
    "/mnt/projects/02_perception_biometrics/ofiq_29794/artifacts/local/runs/"
    "20260814-branch-b/source_seals/usfiqa-component-source-seal-v2/manifest.json"
)
SEALED_SCRFD_SOURCE = SOURCE_SEAL.parent / "repository" / Path(
    "packages/openfiqa/src/openfiqa/models/scrfd.py"
)
RECONCILIATION = (
    ROOT
    / "resources"
    / "manifests"
    / "lfw-full-v1-shared-crop-lineage-reconciliation.json"
)
VALIDATOR = ROOT / "scripts" / "validate_shared_crop_lineage.py"
REGISTRY = ROOT / "resources" / "resource-registry.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SharedCropLineageTests(unittest.TestCase):
    def test_real_manifest_contradiction_has_append_only_reconciliation(self) -> None:
        manifest = json.loads(HISTORICAL_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(
            _sha256(HISTORICAL_MANIFEST),
            "b379a01f75dc9e47d2146f2c99a2d0c1b0e8a03dc9288c0f70ded1d82802f23e",
        )
        self.assertEqual(
            manifest["contract"]["detector"]["preprocessing"],
            "OpenCV BGR input, 640 by 640 detector canvas, highest-confidence face, five-point 112 by 112 norm_crop.",
        )
        self.assertEqual(
            manifest["contract"]["selection"],
            "largest SCRFD face by bounding-box area",
        )
        self.assertEqual(
            manifest["contract"]["encoding"], {"format": "JPEG", "quality": 95}
        )
        # The mutable generator path was rewritten outside QualSight on
        # 2026-08-17; the reconciliation retains the historical digest and binds
        # the current bytes separately.
        reconciliation = json.loads(RECONCILIATION.read_text(encoding="utf-8"))
        self.assertEqual(
            reconciliation["generator_source"]["sha256"],
            "6a44a96e4846a687ca44fb78e3e3592f675acd318232659b91a7049820f3c77c",
        )
        self.assertEqual(
            _sha256(GENERATOR_SOURCE),
            reconciliation["generator_source_drift"]["current"]["sha256"],
        )
        historical_source_record = manifest["contract"]["source_code"][
            str(SCRFD_SOURCE)
        ]
        self.assertEqual(
            historical_source_record["sha256"],
            "48b30da81ee67360bec8bf75dcfc2f9a27f9e263f5c94110a42659144bec4aae",
        )
        self.assertEqual(
            _sha256(SOURCE_SEAL),
            "782b5d7b296b646c924dfce1849f6f3ee994077de4a2d301149910a885dd7e7e",
        )
        self.assertEqual(
            _sha256(SEALED_SCRFD_SOURCE), historical_source_record["sha256"]
        )
        self.assertTrue(
            RECONCILIATION.is_file(),
            "the confirmed fixed-640/highest-confidence versus adaptive/largest-area contradiction has no append-only reconciliation",
        )

    def test_reconciliation_maps_only_the_executed_shared_crop_condition(
        self,
    ) -> None:
        self.assertTrue(RECONCILIATION.is_file(), "lineage reconciliation is absent")
        self.assertTrue(VALIDATOR.is_file(), "lineage validator is absent")
        spec = importlib.util.spec_from_file_location(
            "validate_shared_crop_lineage", VALIDATOR
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec is not None else None)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
        summary = module.validate_reconciliation(RECONCILIATION, REGISTRY)

        receipt = json.loads(RECONCILIATION.read_text(encoding="utf-8"))
        producer = receipt["reconciled_producer"]
        self.assertEqual(
            producer["producer_id"],
            "SCRFD10G_ADAPTIVE_LARGEST_NORMCROP112_UNRESOLVED_MODE_"
            "JPEG95_ENCODED_HISTORICAL_V1",
        )
        self.assertEqual(
            producer["alignment"],
            "INSIGHTFACE_FIVE_POINT_NORM_CROP_112_MODE_NOT_EXPLICIT_IN_GENERATOR",
        )
        self.assertEqual(producer["storage_boundary"], "JPEG_QUALITY_95_ENCODED")
        target_mapping = receipt["target_condition_mapping"]
        self.assertEqual(
            target_mapping["condition_id"],
            "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1",
        )
        self.assertEqual(
            target_mapping["status"],
            "BLOCKED_PENDING_BYTE_BOUND_ARCFACE_TEMPLATE_AND_AUTHORIZED_"
            "BGR_DECODE_MATCHER_CONSUMPTION",
        )
        self.assertFalse(receipt["claims"]["target_condition_mapping_complete"])
        self.assertEqual(summary["producer_id"], producer["producer_id"])
        self.assertEqual(
            summary["target_condition_mapping_status"], target_mapping["status"]
        )
        self.assertEqual(
            summary["excluded_condition_ids"],
            ["SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1"],
        )
        self.assertEqual(summary["scrfd_resource_state"], "APPROVED_LOCAL_RESEARCH_USE")
        self.assertEqual(summary["scrfd_approved_scopes"], ["QUALSIGHT_LOCAL_LFW_THREE_MATCHER_TWO_PREPROCESSING_V1", "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"])
        self.assertFalse(summary["crop_consumption_authorized"])
        self.assertFalse(summary["output_validity_claimed"])

        historical_manifest = json.loads(
            HISTORICAL_MANIFEST.read_text(encoding="utf-8")
        )
        historical_detector = historical_manifest["contract"]["detector"]["artifact"]
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        scrfd_resource = next(
            resource
            for resource in registry["resources"]
            if resource["resource_id"] == "insightface-scrfd-10g-local-candidate"
        )
        registered_detector = next(
            artifact
            for artifact in scrfd_resource["artifacts"]
            if artifact["role"] == "MODEL"
        )
        expected_detector = {
            "path": historical_detector["path"],
            "sha256": historical_detector["sha256"],
            "size_bytes": historical_detector["size_bytes"],
        }
        self.assertEqual(
            expected_detector,
            {key: registered_detector[key] for key in expected_detector},
        )
        self.assertEqual(summary["historical_detector_artifact"], expected_detector)
        self.assertEqual(summary["registered_detector_artifact"], expected_detector)

        validator_tree = ast.parse(VALIDATOR.read_text(encoding="utf-8"))
        imported_roots = {
            alias.name.split(".")[0]
            for node in ast.walk(validator_tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(validator_tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        self.assertTrue(
            imported_roots
            <= {
                "__future__",
                "argparse",
                "ast",
                "datetime",
                "hashlib",
                "json",
                "pathlib",
                "typing",
            }
        )


if __name__ == "__main__":
    unittest.main()
