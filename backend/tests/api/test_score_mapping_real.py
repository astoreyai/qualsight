"""QS-Y84 — generalized external-score field mapping (PRD §12) on the real registered artifact.

The source table is the real registered external score artifact (decision 29,
insightface buffalo_l over the frozen LFW View 2 pairs). The test renames its
columns the way a vendor file might arrive and asserts the mapping translates
rows back to the canonical fields byte-for-value, refuses missing columns, and
refuses unknown canonical fields — no value invented, no row dropped.
"""

from __future__ import annotations

import csv
from pathlib import Path
import unittest

from qualsight.importers.external_scores import (
    CANONICAL_SCORE_FIELDS,
    ExternalScoreImportError,
    apply_field_mapping,
)

SCORE_CSV = Path(
    "/mnt/projects/02_perception_biometrics/ofiq_29794/data/scores/"
    "lfw-view2-insightface-buffalo_l/external_scores.csv"
)


class ScoreFieldMappingRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert SCORE_CSV.is_file(), f"registered external score artifact missing: {SCORE_CSV}"
        with SCORE_CSV.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            cls.columns = list(reader.fieldnames or [])
            cls.rows = list(reader)
        assert len(cls.rows) == 6000, f"expected the 6000 View 2 rows, found {len(cls.rows)}"

    def _renamed(self) -> tuple[list[dict], list[str], dict[str, str]]:
        rename = {"pair_id": "comparison_ref", "score": "similarity", "label": "ground_truth"}
        renamed_rows = [
            {rename.get(key, key): value for key, value in row.items()} for row in self.rows
        ]
        renamed_columns = [rename.get(column, column) for column in self.columns]
        mapping = {field: source for field, source in rename.items()}
        return renamed_rows, renamed_columns, mapping

    def test_mapping_translates_every_row_without_inventing_values(self) -> None:
        renamed_rows, renamed_columns, mapping = self._renamed()
        translated, columns = apply_field_mapping(renamed_rows, renamed_columns, mapping)
        self.assertEqual(len(translated), len(self.rows))
        for field in ("pair_id", "score", "label"):
            self.assertIn(field, columns)
        for original, mapped in zip(self.rows, translated):
            self.assertEqual(mapped["pair_id"], original["pair_id"])
            self.assertEqual(mapped["score"], original["score"])
            self.assertEqual(mapped["label"], original["label"])

    def test_identity_mapping_is_a_no_op(self) -> None:
        translated, columns = apply_field_mapping(self.rows, self.columns, None)
        self.assertIs(translated, self.rows)
        self.assertEqual(columns, self.columns)

    def test_mapping_to_a_missing_column_is_refused(self) -> None:
        renamed_rows, renamed_columns, _ = self._renamed()
        with self.assertRaises(ExternalScoreImportError):
            apply_field_mapping(renamed_rows, renamed_columns, {"score": "no_such_column"})

    def test_unknown_canonical_field_is_refused(self) -> None:
        with self.assertRaises(ExternalScoreImportError):
            apply_field_mapping(self.rows, self.columns, {"verdict": "score"})
        self.assertNotIn("verdict", CANONICAL_SCORE_FIELDS)


if __name__ == "__main__":
    unittest.main()
