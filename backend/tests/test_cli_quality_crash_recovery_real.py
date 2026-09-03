"""Real CLI quality-batch crash recovery at final-file/SQLite publication."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.api.store import RunStore
from qualsight.domain.primitives import CanonicalJson
from qualsight.engines.ofiq.batch import PREPARED_BATCH_FILENAME
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)
from qualsight.security.purge import purge_targets


ROOT = Path(__file__).resolve().parents[2]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
LFW_TREE = Path("/mnt/projects/datasets/lfw/lfw_funneled")


@unittest.skipUnless(LFW_TREE.is_dir(), "registered local LFW tree is unavailable")
class CLIQualityCrashRecoveryRealTests(unittest.TestCase):
    def test_cli_batch_is_durably_admitted_and_recovered_after_process_death(self) -> None:
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-cli-quality-crash-"))
        self.addCleanup(shutil.rmtree, data_dir, True)
        snapshot = data_dir.with_name(f"{data_dir.name}.snapshot.sqlite3")
        self.addCleanup(snapshot.unlink, missing_ok=True)
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
            project = client.post(
                "/api/projects",
                json={
                    "name": "Real CLI quality crash recovery",
                    "description": "SIGKILL after registered LFW CLI quality evidence publication.",
                    "purpose_scope": "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1",
                },
            )
            self.assertEqual(project.status_code, 201, project.text)
            project_id = project.json()["project_id"]
            opened = client.post(f"/api/projects/{project_id}/open")
            self.assertEqual(opened.status_code, 200, opened.text)
            imported = client.post(
                f"/api/projects/{project_id}/datasets/import-registered",
                json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
            )
            self.assertEqual(imported.status_code, 201, imported.text)
            dataset_id = imported.json()["dataset_id"]

        database = data_dir / "qualsight.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TRIGGER stall_cli_quality_batch_recording "
            "BEFORE INSERT ON jobs BEGIN "
            "SELECT sum(length(a.record_json) + length(b.record_json)) "
            "FROM samples AS a CROSS JOIN samples AS b; END"
        )
        connection.commit()
        connection.close()

        environment = {
            **os.environ,
            "PYTHONPATH": f"{ROOT / 'backend' / 'src'}:{ROOT}",
            "QUALSIGHT_REPOSITORY_ROOT": str(ROOT),
            "QUALSIGHT_DATA_DIR": str(data_dir),
            "QUALSIGHT_INPUT_RESOURCE_IDS": f"{OFIQ_INPUT_RESOURCE_ID},{LFW_RESOURCE_ID}",
            "QUALSIGHT_NETWORK_GUARD": "0",
        }
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "qualsight.cli",
                "batch",
                "--project-id",
                project_id,
                "--dataset-id",
                dataset_id,
                "--selection",
                "subset_fixture",
                "--chunks",
                "1",
            ],
            cwd=ROOT / "backend",
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        operation: tuple[str, str, str, str] | None = None
        result_id: str | None = None
        output_directory: Path | None = None
        parquet_sha256: str | None = None
        try:
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                prepared = tuple(
                    (data_dir / "jobs").glob(f"*/{PREPARED_BATCH_FILENAME}")
                )
                if prepared:
                    self.assertEqual(len(prepared), 1)
                    receipt_text = prepared[0].read_text(encoding="utf-8")
                    receipt = json.loads(receipt_text)
                    self.assertEqual(
                        receipt_text, CanonicalJson.from_value(receipt).text + "\n"
                    )
                    self.assertEqual(receipt["operation_kind"], "QUALITY_BATCH")
                    self.assertEqual(receipt["project_id"], project_id)
                    result_id = str(receipt["job_id"])
                    output_directory = prepared[0].parent
                    break
                if worker.poll() is not None:
                    stdout, stderr = worker.communicate(timeout=30)
                    self.fail(f"CLI quality worker exited before publication: {(stdout, stderr)}")
                time.sleep(0.01)
            else:
                self.fail("timed out waiting for real CLI quality receipt publication")

            assert result_id is not None and output_directory is not None
            self.assertEqual(output_directory.name, result_id.replace(":", "_"))
            parquet = output_directory / "quality_measurements.parquet"
            self.assertTrue(parquet.is_file())
            parquet_sha256 = hashlib.sha256(parquet.read_bytes()).hexdigest()
            time.sleep(0.5)
            self.assertIsNone(worker.poll(), "CLI quality worker left the SQLite stall unexpectedly")
            os.kill(worker.pid, signal.SIGKILL)
            stdout, stderr = worker.communicate(timeout=30)
            self.assertEqual(worker.returncode, -signal.SIGKILL, (stdout, stderr))
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.communicate(timeout=30)
            connection = sqlite3.connect(database)
            connection.execute("DROP TRIGGER IF EXISTS stall_cli_quality_batch_recording")
            connection.commit()
            connection.close()

        assert result_id is not None and output_directory is not None
        connection = sqlite3.connect(database)
        try:
            operation = connection.execute(
                "SELECT operation_id, result_id, state, output_path FROM operations "
                "WHERE operation_kind = 'QUALITY_BATCH' AND project_id = ? "
                "ORDER BY accepted_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            recorded_job = connection.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?", (result_id,)
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(
            operation,
            "CLI quality execution published final evidence without a durable operation",
        )
        assert operation is not None
        operation_id, recorded_result_id, state, output_path = operation
        self.assertEqual(recorded_result_id, result_id)
        self.assertEqual(state, "RUNNING")
        self.assertEqual(Path(output_path), output_directory)
        self.assertIsNone(recorded_job)
        operation_id, result_id, _, _ = operation
        before_restart = RunStore(database)
        try:
            targets = purge_targets(before_restart, data_dir, project_id)
            self.assertIn(str(output_directory), targets["directories"])
        finally:
            before_restart.close()

        restarted = subprocess.run(
            [
                sys.executable,
                "-m",
                "qualsight.cli",
                "snapshot-database",
                "--output",
                str(snapshot),
            ],
            cwd=ROOT / "backend",
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(restarted.returncode, 0, (restarted.stdout, restarted.stderr))

        query = sqlite3.connect(database)
        try:
            recovered_operation = query.execute(
                "SELECT state FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            recovered_job = query.execute(
                "SELECT state, parquet_sha256 FROM jobs WHERE job_id = ?", (result_id,)
            ).fetchone()
        finally:
            query.close()
        self.assertEqual(recovered_operation, ("COMPLETED",))
        self.assertEqual(recovered_job, ("COMPLETED", parquet_sha256))


if __name__ == "__main__":
    unittest.main()
