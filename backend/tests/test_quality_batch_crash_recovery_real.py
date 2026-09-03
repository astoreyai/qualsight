"""Real OFIQ quality-batch crash recovery at final-file/SQLite publication."""

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
from qualsight.domain.primitives import CanonicalJson
from qualsight.engines.ofiq.batch import recover_prepared_quality_batch
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)


ROOT = Path(__file__).resolve().parents[2]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
LFW_TREE = Path("/mnt/projects/datasets/lfw/lfw_funneled")


@unittest.skipUnless(LFW_TREE.is_dir(), "registered local LFW tree is unavailable")
class QualityBatchCrashRecoveryRealTests(unittest.TestCase):
    def test_final_real_quality_output_is_reconciled_after_process_death(self) -> None:
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-quality-crash-"))
        self.addCleanup(shutil.rmtree, data_dir, True)
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
                    "name": "Real quality crash recovery",
                    "description": "SIGKILL after registered LFW quality evidence publication.",
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
            "CREATE TRIGGER stall_quality_batch_recording "
            "BEFORE INSERT ON jobs BEGIN "
            "SELECT sum(length(a.record_json) + length(b.record_json)) "
            "FROM samples AS a CROSS JOIN samples AS b; END"
        )
        connection.commit()
        connection.close()

        worker_source = """
import json
from pathlib import Path
import sys
import time
from fastapi.testclient import TestClient
from backend.tests.test_output_crash_recovery_real import _publish_worker_response
from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID

root = Path(sys.argv[1])
data_dir = Path(sys.argv[2])
project_id = sys.argv[3]
dataset_id = sys.argv[4]
response_marker = Path(sys.argv[5])
settings = Settings(repository_root=root, registry_path=root / "resources" / "resource-registry.json", data_dir=data_dir, runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID, input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"))
with TestClient(create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
    response = client.post(f"/api/projects/{project_id}/datasets/{dataset_id}/quality-batch", json={"selection": {"kind": "subset_fixture"}, "chunks": 1})
    if response.status_code != 202:
        raise RuntimeError(response.text)
    _publish_worker_response(response_marker, {"job_id": response.json()["job_id"]})
    while True:
        time.sleep(0.05)
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = f"{ROOT / 'backend' / 'src'}:{ROOT}"
        response_marker = data_dir / ".quality-worker-response.json"
        worker = subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker_source,
                str(ROOT),
                str(data_dir),
                project_id,
                dataset_id,
                str(response_marker),
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        job_id: str | None = None
        output_directory: Path | None = None
        deadline = time.monotonic() + 300
        try:
            while time.monotonic() < deadline:
                if response_marker.is_file():
                    marker_text = response_marker.read_text(encoding="utf-8")
                    marker = json.loads(marker_text)
                    self.assertEqual(
                        marker_text, CanonicalJson.from_value(marker).text + "\n"
                    )
                    self.assertEqual(set(marker), {"job_id"})
                    job_id = str(marker["job_id"])
                    output_directory = data_dir / "jobs" / job_id.replace(":", "_")
                    parquet = output_directory / "quality_measurements.parquet"
                    if parquet.is_file():
                        break
                time.sleep(0.01)
            else:
                raise AssertionError(
                    "timed out waiting for real quality Parquet publication before SQLite job metadata"
                )
            assert job_id is not None and output_directory is not None
            time.sleep(0.5)
            self.assertIsNone(worker.poll(), "quality worker left the SQLite stall unexpectedly")
            parquet = output_directory / "quality_measurements.parquet"
            parquet_sha256 = hashlib.sha256(parquet.read_bytes()).hexdigest()
            os.kill(worker.pid, signal.SIGKILL)
            stdout, stderr = worker.communicate(timeout=30)
            self.assertEqual(worker.returncode, -signal.SIGKILL, (stdout, stderr))
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.communicate(timeout=30)

        connection = sqlite3.connect(database)
        connection.execute("DROP TRIGGER stall_quality_batch_recording")
        operation_request = json.loads(
            connection.execute(
                "SELECT request_json FROM operations WHERE result_id = ? ORDER BY attempt DESC LIMIT 1",
                (job_id,),
            ).fetchone()[0]
        )
        self.assertIsNone(
            connection.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        )
        connection.commit()
        connection.close()

        recovered = recover_prepared_quality_batch(
            output_directory,
            job_id=job_id,
            project_id=project_id,
            request_sha256=CanonicalJson.from_value(operation_request).sha256,
        )
        self.assertEqual(recovered.job["state"], "COMPLETED")

        with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
            status = client.get(f"/api/projects/{project_id}/jobs/{job_id}")
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["state"], "COMPLETED", status.text)
            self.assertTrue(status.json()["quality_result_available"], status.text)

        self.assertEqual(hashlib.sha256(parquet.read_bytes()).hexdigest(), parquet_sha256)
        connection = sqlite3.connect(database)
        try:
            recorded = connection.execute(
                "SELECT state, parquet_sha256 FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(recorded, ("COMPLETED", parquet_sha256))


if __name__ == "__main__":
    unittest.main()
