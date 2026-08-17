from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = (
    ROOT / "resources" / "manifests" / "lfw-official-protocol-authority-v1.json"
)
VALIDATOR = ROOT / "scripts" / "validate_lfw_protocol_authority.py"
REGISTRY = ROOT / "resources" / "resource-registry.json"
PAIR_MANIFEST = ROOT / "resources" / "manifests" / "lfw-local-pairs-v1.jsonl"
HUANG_2008 = ROOT / "resources" / "upstream" / "lfw" / "Huang_eccv2008-lfw.pdf"
ORIGINAL_2007 = ROOT / "resources" / "upstream" / "lfw" / "lfw-2007.pdf"
UPDATES_2014 = ROOT / "resources" / "upstream" / "lfw" / "UM-CS-2014-003.pdf"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_lfw_protocol_authority", VALIDATOR
    )
    if spec is None or spec.loader is None:
        raise AssertionError("LFW protocol-authority validator is absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LFWOfficialProtocolAuthorityTests(unittest.TestCase):
    def test_official_umass_protocol_bytes_and_text_are_bound(self) -> None:
        self.assertTrue(AUTHORITY.is_file(), "protocol-authority receipt is absent")
        self.assertTrue(VALIDATOR.is_file(), "protocol-authority validator is absent")
        self.assertEqual(
            _sha256(HUANG_2008),
            "004a66b3930dc6aa15d54bafbc7b734b1333669a8bcc08ea5ac93d89c251ae86",
        )
        self.assertEqual(HUANG_2008.stat().st_size, 361179)
        self.assertEqual(
            _sha256(ORIGINAL_2007),
            "87d9b19c74de1fc04c4748faec470956912ecfee39ed73213a87f5b87e516329",
        )
        self.assertEqual(ORIGINAL_2007.stat().st_size, 223526)
        self.assertEqual(
            _sha256(UPDATES_2014),
            "cceb20f7902b7c8094df3e14f711f7e7454e0e52cf1a4b6e1abbf70dc000b944",
        )
        self.assertEqual(UPDATES_2014.stat().st_size, 135174)

        summary = _load_validator().validate_authority(
            AUTHORITY,
            REGISTRY,
            PAIR_MANIFEST,
        )
        self.assertEqual(summary["official_documents"], 3)
        self.assertEqual(summary["view"], "VIEW_2_PERFORMANCE_REPORTING")
        self.assertEqual(summary["pair_count"], 6000)
        self.assertEqual(summary["fold_count"], 10)
        self.assertTrue(summary["pdf_text_reproduced"])
        self.assertEqual(
            summary["transport_provenance_status"],
            "PROJECT_RECORDED_URLS_AND_RESPONSE_HEADERS_NO_VERBOSE_REQUEST_TRACES",
        )

    def test_selected_protocol_is_conservative_and_not_a_rights_claim(self) -> None:
        receipt = json.loads(AUTHORITY.read_text(encoding="utf-8"))
        finding = receipt["protocol_finding"]
        self.assertEqual(
            finding["selected_category"],
            "UNRESTRICTED_WITH_LABELED_OUTSIDE_DATA",
        )
        self.assertEqual(
            finding["status"],
            "OWNER_AUTHORIZED_LOCAL_RESEARCH_USE_MATCHER_TRAINING_EXCLUSION_"
            "RECORDED_AS_PUBLISHED_CLAIM",
        )
        self.assertFalse(receipt["claims"]["complete_official_protocol_conformance"])
        self.assertFalse(receipt["claims"]["dataset_rights_closed"])
        self.assertFalse(receipt["claims"]["image_or_model_execution_performed"])

        summary = _load_validator().validate_authority(
            AUTHORITY,
            REGISTRY,
            PAIR_MANIFEST,
        )
        # The protocol child itself never promotes; the current state is backed
        # only by the owner use-permission receipt, which the validator binds.
        self.assertEqual(summary["dataset_resource_state"], "APPROVED_LOCAL_RESEARCH_USE")
        self.assertEqual(summary["dataset_approved_scopes"], ["QUALSIGHT_LOCAL_LFW_THREE_MATCHER_TWO_PREPROCESSING_V1", "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"])
        self.assertEqual(summary["matcher_resource_states"], ["APPROVED_LOCAL_RESEARCH_USE"] * 3)
        self.assertEqual(summary["matcher_approved_scopes"], [["QUALSIGHT_LOCAL_LFW_THREE_MATCHER_TWO_PREPROCESSING_V1", "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"]] * 3)
        self.assertFalse(summary["complete_official_protocol_conformance"])
        self.assertFalse(summary["dataset_rights_closed"])


if __name__ == "__main__":
    unittest.main()
