"""Real CLI reporting publication and crash-recovery evidence gates."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.execution.prepared_output import PREPARED_OUTPUT_FILENAME
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)


ROOT = Path(__file__).resolve().parents[2]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
LFW_TREE = Path("/mnt/projects/datasets/lfw/lfw_funneled")


@unittest.skipUnless(LFW_TREE.is_dir(), "registered local LFW tree is unavailable")
class CLIReportingPublicationRecoveryRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-cli-reporting-"))
        cls.snapshot = cls.data_dir.with_name(f"{cls.data_dir.name}.snapshot.sqlite3")
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
            project = client.post(
                "/api/projects",
                json={
                    "name": "Real CLI reporting recovery",
                    "description": "Registered LFW CLI reporting durability evidence.",
                    "purpose_scope": "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1",
                },
            )
            assert project.status_code == 201, project.text
            cls.project_id = project.json()["project_id"]
            opened = client.post(f"/api/projects/{cls.project_id}/open")
            assert opened.status_code == 200, opened.text
            imported = client.post(
                f"/api/projects/{cls.project_id}/datasets/import-registered",
                json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
            )
            assert imported.status_code == 201, imported.text
            started = client.post(
                f"/api/projects/{cls.project_id}/datasets/{imported.json()['dataset_id']}/quality-batch",
                json={"selection": {"kind": "subset_fixture"}, "chunks": 1},
            )
            assert started.status_code == 202, started.text
            cls.job_id = started.json()["job_id"]
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                response = client.get(
                    f"/api/projects/{cls.project_id}/jobs/{cls.job_id}"
                )
                assert response.status_code == 200, response.text
                body = response.json()
                if body.get("ended_at") is not None:
                    assert body["state"] == "COMPLETED", body
                    cls.run_id = body["run_id"]
                    break
                time.sleep(0.25)
            else:
                raise AssertionError("real LFW quality job did not finish")
        cls.database = cls.data_dir / "qualsight.sqlite3"
        cls.environment = {
            **os.environ,
            "PYTHONPATH": f"{ROOT / 'backend' / 'src'}:{ROOT}",
            "QUALSIGHT_REPOSITORY_ROOT": str(ROOT),
            "QUALSIGHT_DATA_DIR": str(cls.data_dir),
            "QUALSIGHT_INPUT_RESOURCE_IDS": (
                f"{OFIQ_INPUT_RESOURCE_ID},{LFW_RESOURCE_ID}"
            ),
            "QUALSIGHT_NETWORK_GUARD": "0",
            "QUALSIGHT_ACTOR": "cli-reporting-real",
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.snapshot.unlink(missing_ok=True)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def _run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "qualsight.cli", *arguments],
            cwd=ROOT / "backend",
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300,
            check=False,
        )

    def _refuse_insert(self, table: str) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                f"CREATE TRIGGER refuse_cli_reporting_{table} "
                f"BEFORE INSERT ON {table} BEGIN "
                "SELECT RAISE(ABORT, 'real CLI reporting persistence refusal'); END"
            )
            connection.commit()
        finally:
            connection.close()

    def _permit_insert(self, table: str) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(f"DROP TRIGGER IF EXISTS refuse_cli_reporting_{table}")
            connection.commit()
        finally:
            connection.close()

    def _prepared_record(self, directory: Path) -> dict:
        receipt = json.loads(
            (directory / PREPARED_OUTPUT_FILENAME).read_text(encoding="utf-8")
        )
        self.assertEqual(receipt["project_id"], self.project_id)
        return receipt["record"]

    def _assert_one_audit(self, subject_id: str, subject_type: str) -> None:
        connection = sqlite3.connect(self.database)
        try:
            rows = connection.execute(
                "SELECT record_json FROM audit_events WHERE subject_id = ?",
                (subject_id,),
            ).fetchall()
        finally:
            connection.close()
        records = [json.loads(row[0]) for row in rows]
        self.assertEqual(
            [
                (
                    record["actor_id"],
                    record["event_type"],
                    record["subject_type"],
                )
                for record in records
            ],
            [("cli-reporting-real", "ARTIFACT_EXPORTED", subject_type)],
        )

    def test_cli_reporting_is_atomic_and_restart_recovers_prepared_publications(self) -> None:
        successful = self._run_cli(
            "figure",
            "--project-id",
            self.project_id,
            "--kind",
            "quality_histogram",
            "--params",
            json.dumps({"job_id": self.job_id, "bins": 7}),
        )
        self.assertEqual(successful.returncode, 0, (successful.stdout, successful.stderr))
        successful_id = json.loads(successful.stdout)["figure_id"]
        self._assert_one_audit(successful_id, "Figure")

        figure_root = self.data_dir / "figures"
        before_figures = set(figure_root.iterdir())
        self._refuse_insert("figures")
        try:
            refused_figure = self._run_cli(
                "figure",
                "--project-id",
                self.project_id,
                "--kind",
                "quality_histogram",
                "--params",
                json.dumps({"job_id": self.job_id, "bins": 8}),
            )
        finally:
            self._permit_insert("figures")
        self.assertNotEqual(refused_figure.returncode, 0)
        pending_figures = set(figure_root.iterdir()) - before_figures
        self.assertEqual(len(pending_figures), 1, pending_figures)
        pending_figure = self._prepared_record(next(iter(pending_figures)))
        connection = sqlite3.connect(self.database)
        try:
            self.assertIsNone(
                connection.execute(
                    "SELECT figure_id FROM figures WHERE figure_id = ?",
                    (pending_figure["figure_id"],),
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE subject_id = ?",
                    (pending_figure["figure_id"],),
                ).fetchone(),
                (0,),
            )
        finally:
            connection.close()

        bundle_root = self.data_dir / "bundles"
        bundle_root.mkdir(parents=True, exist_ok=True)
        before_bundles = set(bundle_root.iterdir())
        self._refuse_insert("bundles")
        try:
            refused_bundle = self._run_cli(
                "bundle-export", "--run-id", self.run_id
            )
        finally:
            self._permit_insert("bundles")
        self.assertNotEqual(refused_bundle.returncode, 0)
        pending_bundles = set(bundle_root.iterdir()) - before_bundles
        self.assertEqual(len(pending_bundles), 1, pending_bundles)
        pending_bundle = self._prepared_record(next(iter(pending_bundles)))

        connection = sqlite3.connect(self.database)
        try:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT figure_id FROM figures WHERE figure_id = ?",
                    (pending_figure["figure_id"],),
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT bundle_id FROM bundles WHERE bundle_id = ?",
                    (pending_bundle["bundle_id"],),
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE subject_id = ?",
                    (pending_bundle["bundle_id"],),
                ).fetchone(),
                (0,),
            )
        finally:
            connection.close()
        self._assert_one_audit(pending_figure["figure_id"], "Figure")

        restarted = self._run_cli(
            "snapshot-database", "--output", str(self.snapshot)
        )
        self.assertEqual(restarted.returncode, 0, (restarted.stdout, restarted.stderr))

        connection = sqlite3.connect(self.database)
        try:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT figure_id FROM figures WHERE figure_id = ?",
                    (pending_figure["figure_id"],),
                ).fetchone()
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT bundle_id FROM bundles WHERE bundle_id = ?",
                    (pending_bundle["bundle_id"],),
                ).fetchone()
            )
        finally:
            connection.close()
        self._assert_one_audit(pending_bundle["bundle_id"], "Bundle")


if __name__ == "__main__":
    unittest.main()
