from __future__ import annotations

import unittest
from pathlib import Path

from scripts.validate_atg_documents import validate_documents


ROOT = Path(__file__).resolve().parents[1]


class ATGDocumentTests(unittest.TestCase):
    def test_image_workflow_acceptance_extension_is_bound_to_real_evidence(self) -> None:
        text = (ROOT / "docs" / "IMAGE_WORKFLOW_ACCEPTANCE.md").read_text(encoding="utf-8")
        for requirement_id in (
            "QS-IMG-001",
            "QS-IMG-002",
            "QS-IMG-003",
            "QS-IMG-004",
            "QS-IMG-005",
            "QS-IMG-006",
            "QS-IMG-007",
            "QS-IMG-008",
            "QS-IMG-009",
        ):
            self.assertEqual(text.count(f"| {requirement_id} |"), 1)
        for design_hash in (
            "0d7023fe53e5672c9841f8114825e9b72047008163dc3f403d3a5ee2b8860067",
            "982b40b763e7d45746545f88c594004f2c6b94cf9096ddaa82c752eebd155aaa",
            "77879cbae10f1c2890325f3354fd26ce34a2df5b70b4d66f271be0a4807608a1",
        ):
            self.assertIn(design_hash, text)
        for evidence_path in (
            "scripts/observe_console.py",
            "scripts/validate_upload_working_sample.py",
            "backend/tests/api/test_owner_supplied_real.py",
            "backend/tests/api/test_matcher_api_real.py",
        ):
            self.assertIn(f"`{evidence_path}`", text)
            self.assertTrue((ROOT / evidence_path).is_file(), evidence_path)

    def test_current_real_documents_form_the_frozen_acyclic_graph(self) -> None:
        summary = validate_documents()
        self.assertEqual(summary["requirements"], 81)
        self.assertEqual(summary["sections"], 60)
        self.assertEqual(summary["decisions"], 12)
        self.assertEqual(summary["nodes"], 41)
        self.assertEqual(summary["edges"], 85)
        self.assertGreaterEqual(summary["ledger_records"], 114)
        self.assertEqual(summary["accepted_whole_capabilities"], 41)
        self.assertEqual(summary["current_accepted_refinements"], 2)
        self.assertEqual(summary["current_accepted_children"], 14)
        self.assertEqual(summary["superseded_historical_children"], 11)
        self.assertEqual(summary["blocked_parent_capabilities"], 0)
        self.assertEqual(summary["blocked_refinement_parents"], 0)
        self.assertEqual(summary["ledger_state_claims"], 69)
        self.assertEqual(summary["historical_prefix_records"], 113)
        self.assertEqual(summary["current_output_hashes"], 98)
        self.assertEqual(summary["matcher_transforms"], 3)
        self.assertEqual(summary["preprocessing_conditions"], 2)
        self.assertEqual(summary["matcher_condition_leaves"], 6)


if __name__ == "__main__":
    unittest.main()
