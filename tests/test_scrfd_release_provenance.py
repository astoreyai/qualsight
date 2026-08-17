from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROVENANCE = (
    ROOT
    / "resources"
    / "manifests"
    / "insightface-buffalo-l-release-provenance-v1.json"
)
VALIDATOR = ROOT / "scripts" / "validate_scrfd_release_provenance.py"
REGISTRY = ROOT / "resources" / "resource-registry.json"
LOCAL_ARCHIVE = Path("/home/aaron/.insightface/models/buffalo_l.zip")
LOCAL_DETECTOR = Path("/home/aaron/.insightface/models/buffalo_l/det_10g.onnx")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_scrfd_release_provenance", VALIDATOR
    )
    if spec is None or spec.loader is None:
        raise AssertionError("SCRFD release-provenance validator is absent")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SCRFDReleaseProvenanceTests(unittest.TestCase):
    def test_official_buffalo_l_transfer_is_byte_equal_to_the_real_local_archive(self) -> None:
        self.assertTrue(PROVENANCE.is_file(), "release-provenance receipt is absent")
        self.assertTrue(VALIDATOR.is_file(), "release-provenance validator is absent")
        self.assertEqual(LOCAL_ARCHIVE.stat().st_size, 288621354)
        self.assertEqual(
            _sha256(LOCAL_ARCHIVE),
            "80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f",
        )
        self.assertEqual(LOCAL_DETECTOR.stat().st_size, 16923827)
        self.assertEqual(
            _sha256(LOCAL_DETECTOR),
            "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
        )

        summary = _load_validator().validate_provenance(PROVENANCE, REGISTRY)
        self.assertEqual(summary["release_tag"], "v0.7")
        self.assertEqual(summary["release_asset_id"], 101975360)
        self.assertEqual(summary["archive_size_bytes"], 288621354)
        self.assertEqual(summary["archive_members"], 5)
        self.assertTrue(summary["fresh_transfer_equals_local_archive"])
        self.assertTrue(summary["detector_member_equals_registered_model"])

    def test_static_provenance_does_not_promote_or_execute_scrfd(self) -> None:
        receipt = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        self.assertFalse(receipt["claims"]["model_rights_closed"])
        self.assertFalse(receipt["claims"]["runtime_validated"])
        self.assertFalse(receipt["claims"]["model_execution_performed"])

        summary = _load_validator().validate_provenance(PROVENANCE, REGISTRY)
        self.assertEqual(summary["resource_id"], "insightface-scrfd-10g-local-candidate")
        # Static provenance never promotes; the current state comes only from
        # the owner use-permission receipt bound by the validator.
        self.assertEqual(summary["resource_state"], "APPROVED_LOCAL_RESEARCH_USE")
        self.assertEqual(summary["third_party_rights_status"], "RESEARCH_NOTICE_ONLY")
        self.assertEqual(summary["approved_scopes"], ["QUALSIGHT_LOCAL_LFW_THREE_MATCHER_TWO_PREPROCESSING_V1", "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"])
        self.assertFalse(summary["model_rights_closed"])
        self.assertFalse(summary["runtime_validated"])


if __name__ == "__main__":
    unittest.main()
