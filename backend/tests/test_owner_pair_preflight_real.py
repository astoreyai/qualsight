"""Real LFW evidence for the bounded owner-pair beta preflight.

The fixture is a deterministic byte-for-byte subsample of the registered local
LFW tree.  Its former API maxima request plans more than 20,000 output pairs;
the request must be refused before any protocol artifact or authority row is
created.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)
from qualsight.importers import owner_supplied


ROOT = Path(__file__).resolve().parents[2]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
LFW_TREE = Path("/mnt/projects/datasets/lfw/lfw_funneled")
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


@unittest.skipUnless(LFW_TREE.is_dir(), "registered local LFW tree is unavailable")
class OwnerPairPreflightRealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-owner-pair-cap-"))
        self.owner_root = Path(tempfile.mkdtemp(prefix="qualsight-owner-pair-lfw-"))
        self.addCleanup(shutil.rmtree, self.owner_root, True)
        self.addCleanup(shutil.rmtree, self.data_dir, True)

        mated_subject = self.owner_root / "George_W_Bush"
        mated_subject.mkdir()
        bush_sources = sorted((LFW_TREE / "George_W_Bush").glob("*.jpg"))[:21]
        self.assertEqual(len(bush_sources), 21)
        for source in bush_sources:
            shutil.copyfile(source, mated_subject / source.name)

        singleton_sources: list[Path] = []
        for subject_root in sorted(LFW_TREE.iterdir()):
            if not subject_root.is_dir() or subject_root.name == "George_W_Bush":
                continue
            sources = sorted(subject_root.glob("*.jpg"))
            if sources:
                singleton_sources.append(sources[0])
            if len(singleton_sources) == 150:
                break
        self.assertEqual(len(singleton_sources), 150)
        for source in singleton_sources:
            subject_root = self.owner_root / source.parent.name
            subject_root.mkdir()
            shutil.copyfile(source, subject_root / source.name)

        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=self.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        self.client = TestClient(
            create_app(settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        )
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        created = self.client.post(
            "/api/projects",
            json={
                "name": "owner pair beta cap gate",
                "description": "Real registered-LFW subsample boundary evidence.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.project_id = created.json()["project_id"]
        opened = self.client.post(f"/api/projects/{self.project_id}/open")
        self.assertEqual(opened.status_code, 200, opened.text)
        imported = self.client.post(
            f"/api/projects/{self.project_id}/datasets/import-owner-directory",
            json={"root": str(self.owner_root), "name": "real LFW pair-cap input"},
        )
        self.assertEqual(imported.status_code, 201, imported.text)
        self.dataset_id = imported.json()["dataset_id"]
        self.assertEqual(imported.json()["sample_count"], 171)
        self.assertEqual(imported.json()["subject_count"], 151)

    def _authority_counts(self) -> tuple[int, int, int]:
        with sqlite3.connect(self.data_dir / "qualsight.sqlite3") as connection:
            return (
                int(connection.execute("SELECT COUNT(*) FROM protocols").fetchone()[0]),
                int(connection.execute("SELECT COUNT(*) FROM protocol_pairs").fetchone()[0]),
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM audit_events "
                        "WHERE subject_type = 'VerificationProtocol'"
                    ).fetchone()[0]
                ),
            )

    def test_registered_lfw_population_fits_the_beta_default_plan(self) -> None:
        subject_counts = {
            subject_root.name: len(tuple(subject_root.glob("*.jpg")))
            for subject_root in LFW_TREE.iterdir()
            if subject_root.is_dir()
        }
        self.assertEqual(sum(subject_counts.values()), 13_233)
        self.assertEqual(len(subject_counts), 5_749)
        mated, nonmated = owner_supplied._owner_pair_preflight(
            subject_counts,
            max_mated_pairs_per_subject=6,
            nonmated_multiple=1.0,
            max_pairs=20_000,
        )
        self.assertEqual((mated, nonmated), (5_312, 5_312))
        self.assertLessEqual(mated + nonmated, 20_000)

    def test_preflight_refuses_parameters_that_could_bypass_pair_counting(self) -> None:
        subject_counts = {
            subject_root.name: len(tuple(subject_root.glob("*.jpg")))
            for subject_root in LFW_TREE.iterdir()
            if subject_root.is_dir()
        }
        with self.assertRaisesRegex(
            owner_supplied.OwnerSuppliedError,
            "max_mated_pairs_per_subject must be a positive integer",
        ):
            owner_supplied._owner_pair_preflight(
                subject_counts,
                max_mated_pairs_per_subject=-1,
                nonmated_multiple=1.0,
                max_pairs=20_000,
            )
        with self.assertRaisesRegex(
            owner_supplied.OwnerSuppliedError,
            "nonmated_multiple must be finite and positive",
        ):
            owner_supplied._owner_pair_preflight(
                subject_counts,
                max_mated_pairs_per_subject=6,
                nonmated_multiple=float("inf"),
                max_pairs=20_000,
            )
        bush_count = len(tuple((LFW_TREE / "George_W_Bush").glob("*.jpg")))
        self.assertEqual(bush_count, 530)
        with self.assertRaisesRegex(
            owner_supplied.OwnerSuppliedError,
            "supports at most 0 distinct non-mated pairs",
        ):
            owner_supplied._owner_pair_preflight(
                {"George_W_Bush": bush_count},
                max_mated_pairs_per_subject=6,
                nonmated_multiple=1.0,
                max_pairs=20_000,
            )

    def test_former_maxima_are_refused_before_protocol_materialization(self) -> None:
        self.assertEqual(self._authority_counts(), (0, 0, 0))
        response = self.client.post(
            f"/api/projects/{self.project_id}/datasets/{self.dataset_id}"
            "/protocols/freeze-owner-pairs",
            json={
                "seed": 19,
                "max_mated_pairs_per_subject": 1000,
                "nonmated_multiple": 100,
            },
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("20,000", response.json()["detail"])
        self.assertEqual(self._authority_counts(), (0, 0, 0))
        protocol_root = self.data_dir / "protocols"
        self.assertFalse(protocol_root.exists() and any(protocol_root.iterdir()))


if __name__ == "__main__":
    unittest.main()
