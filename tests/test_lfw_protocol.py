from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.validate_lfw_protocol import (
    capture_protocol_manifest,
    verify_protocol_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "resources" / "resource-registry.json"
PROTOCOL_MANIFEST_PATH = (
    ROOT / "resources" / "manifests" / "lfw-local-pairs-v1.jsonl"
)


class LFWProtocolEvidenceTests(unittest.TestCase):
    def test_real_archive_tree_and_pair_files_reconcile(self) -> None:
        summary = verify_protocol_manifest(REGISTRY_PATH, PROTOCOL_MANIFEST_PATH)

        self.assertEqual(summary["resource_id"], "lfw-funneled-local-candidate")
        self.assertEqual(summary["archive_members"], 18_994)
        self.assertEqual(summary["archive_images"], 13_233)
        self.assertEqual(summary["protocol_files"], 11)
        self.assertEqual(summary["folds"], 10)
        self.assertEqual(summary["mated_pairs"], 3_000)
        self.assertEqual(summary["nonmated_pairs"], 3_000)
        self.assertEqual(summary["pairs"], 6_000)
        self.assertEqual(summary["unique_protocol_samples"], 7_701)
        self.assertEqual(summary["unique_protocol_subjects"], 4_281)
        self.assertEqual(summary["missing_manifest_paths"], 0)
        self.assertEqual(summary["expanded_fold_matches"], 10)

    def test_generator_reproduces_registered_real_protocol_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            regenerated = Path(directory) / PROTOCOL_MANIFEST_PATH.name
            capture_protocol_manifest(REGISTRY_PATH, regenerated)

            self.assertEqual(
                regenerated.read_bytes(),
                PROTOCOL_MANIFEST_PATH.read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
