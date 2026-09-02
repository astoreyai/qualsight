"""Public diagnostics preserve useful meaning without native host error text."""

from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest
from urllib.parse import quote, unquote

from qualsight.security.privacy import public_diagnostic


class PublicDiagnosticRealTests(unittest.TestCase):
    def test_real_publisher_and_same_origin_api_urls_are_not_filesystem_paths(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        acquisition_manifest = json.loads(
            (
                repository_root
                / "resources"
                / "manifests"
                / "verification-dataset-acquisition-v1.json"
            ).read_text(encoding="utf-8")
        )
        publisher_url = acquisition_manifest["archive"]["published_url"]
        project_route = "/api/projects/{project_id}/runs/{run_id}"
        self.assertEqual(public_diagnostic(publisher_url), publisher_url)
        self.assertEqual(public_diagnostic(project_route), project_route)
        query_with_private_locator = f"{publisher_url}?source={Path(__file__).resolve()}"
        self.assertNotIn(
            str(Path(__file__).resolve()),
            public_diagnostic(query_with_private_locator),
        )
        encoded_private_locator = quote(str(Path(__file__).resolve()), safe="")
        encoded_query = f"{publisher_url}?source={encoded_private_locator}"
        encoded_public_diagnostic = public_diagnostic(encoded_query)
        self.assertNotIn(encoded_private_locator, encoded_public_diagnostic)
        self.assertNotIn(
            str(Path(__file__).resolve()),
            unquote(encoded_public_diagnostic),
        )

    def test_real_filesystem_error_is_replaced_by_a_stable_private_evidence_message(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qualsight-public-diagnostic-") as directory:
            missing = Path(directory) / "absent-recorded-file"
            try:
                missing.read_bytes()
            except OSError as error:
                native = f"{type(error).__name__}: {error}"
            else:
                self.fail("the real missing-file read unexpectedly succeeded")

        self.assertEqual(
            public_diagnostic(native),
            "private local diagnostic recorded",
        )

    def test_non_native_diagnostic_keeps_meaning_but_redacts_its_real_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qualsight-public-diagnostic-") as directory:
            message = f"recorded input drifted at {Path(directory) / 'input.jpg'}"
        self.assertEqual(
            public_diagnostic(message),
            "recorded input drifted at recorded local resource",
        )

    def test_api_path_traversal_does_not_bypass_real_path_redaction(self) -> None:
        private_locator = Path(__file__).resolve()
        traversal = f"/api/../..{private_locator}"
        self.assertNotIn(str(private_locator), public_diagnostic(traversal))

    def test_api_locator_does_not_shield_appended_real_private_evidence(self) -> None:
        private_locator = str(Path(__file__).resolve())
        diagnostics = (
            f"/api/runs/recorded;{private_locator}",
            f"/api/runs/recorded/{quote(private_locator, safe='')}",
            f"/api/runs/recorded/{quote(quote(private_locator, safe=''), safe='')}",
        )
        for diagnostic in diagnostics:
            with self.subTest(diagnostic=diagnostic):
                observed = public_diagnostic(diagnostic)
                self.assertNotIn(private_locator, unquote(unquote(observed)))


if __name__ == "__main__":
    unittest.main()
