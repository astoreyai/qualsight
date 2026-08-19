from __future__ import annotations

import unittest

from scripts.validate_atg_documents import validate_documents


class ATGDocumentTests(unittest.TestCase):
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
        self.assertEqual(summary["current_output_hashes"], 90)
        self.assertEqual(summary["matcher_transforms"], 3)
        self.assertEqual(summary["preprocessing_conditions"], 2)
        self.assertEqual(summary["matcher_condition_leaves"], 6)


if __name__ == "__main__":
    unittest.main()
