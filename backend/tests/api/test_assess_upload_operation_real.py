"""Durable operation evidence for the synchronous real-image quality route."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.domain.primitives import CanonicalJson
from qualsight.engines.ofiq.batch import recover_prepared_quality_batch
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)


ROOT = Path(__file__).resolve().parents[3]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
REAL_IMAGE = Path(
    "/mnt/projects/datasets/lfw/lfw_funneled/Abel_Pacheco/Abel_Pacheco_0004.jpg"
)
REAL_IMAGE_SHA256 = "8050daa859e7e05aedd5c7ec3851e5d5bddcb2b9797d23958664ec2958a230ae"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


@unittest.skipUnless(REAL_IMAGE.is_file(), "registered local LFW image is unavailable")
class AssessUploadOperationRealTests(unittest.TestCase):
    def test_real_synchronous_upload_recovers_result_record_failure(self) -> None:
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-upload-operation-"))
        self.addCleanup(shutil.rmtree, data_dir, True)
        payload = REAL_IMAGE.read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), REAL_IMAGE_SHA256)
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        with TestClient(
            create_app(settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        ) as client:
            project = client.post(
                "/api/projects",
                json={
                    "name": "Real synchronous upload operation",
                    "description": "Durable operation evidence for one registered LFW photograph.",
                    "purpose_scope": RESEARCH_SCOPE,
                },
            )
            self.assertEqual(project.status_code, 201, project.text)
            project_id = project.json()["project_id"]
            opened = client.post(f"/api/projects/{project_id}/open")
            self.assertEqual(opened.status_code, 200, opened.text)
            trigger = sqlite3.connect(data_dir / "qualsight.sqlite3")
            trigger.execute(
                "CREATE TRIGGER refuse_synchronous_upload_job_recording "
                "BEFORE INSERT ON jobs BEGIN "
                "SELECT RAISE(ABORT, 'real synchronous upload result refused'); END"
            )
            trigger.commit()
            trigger.close()
            assessed = client.post(
                "/api/quality/assess-upload",
                params={
                    "project_id": project_id,
                    "purpose_scope": RESEARCH_SCOPE,
                    "filename": REAL_IMAGE.name,
                },
                content=payload,
                headers={"content-type": "application/octet-stream"},
            )
            self.assertEqual(assessed.status_code, 500, assessed.text)

        database = sqlite3.connect(data_dir / "qualsight.sqlite3")
        try:
            operation = database.execute(
                "SELECT operation_id, result_id, project_id, state, request_json, "
                "output_path FROM operations "
                "WHERE operation_kind = 'QUALITY_BATCH' ORDER BY accepted_at DESC LIMIT 1"
            ).fetchone()
            database.execute("DROP TRIGGER refuse_synchronous_upload_job_recording")
            database.commit()
        finally:
            database.close()
        self.assertIsNotNone(operation)
        assert operation is not None
        self.assertEqual(operation[0], operation[1])
        self.assertEqual(operation[2], project_id)
        self.assertEqual(operation[3], "FAILED")
        request = json.loads(operation[4])
        self.assertEqual(request["source_sha256"], REAL_IMAGE_SHA256)
        request_sha256 = CanonicalJson.from_value(request).sha256
        prepared = recover_prepared_quality_batch(
            Path(operation[5]),
            job_id=operation[1],
            project_id=project_id,
            request_sha256=request_sha256,
        )
        self.assertEqual(prepared.job["state"], "COMPLETED")
        self.assertEqual(prepared.job["dataset_id"], request["dataset_id"])
        self.assertEqual(prepared.results[0].sample_id, request["sample_id"])

        with TestClient(create_app(settings), base_url="http://127.0.0.1") as restarted:
            status = restarted.get(
                f"/api/projects/{project_id}/jobs/{operation[1]}"
            )
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["state"], "COMPLETED", status.text)
            self.assertTrue(status.json()["quality_result_available"], status.text)

        database = sqlite3.connect(data_dir / "qualsight.sqlite3")
        try:
            attempts = database.execute(
                "SELECT operation_id, attempt, supersedes_operation_id, state, error "
                "FROM operations WHERE request_id = ? ORDER BY attempt",
                (operation[0],),
            ).fetchall()
        finally:
            database.close()
        self.assertEqual([row[3] for row in attempts], ["FAILED", "COMPLETED"])
        self.assertIn("real synchronous upload result refused", attempts[0][4])
        self.assertEqual(attempts[1][2], attempts[0][0])


if __name__ == "__main__":
    unittest.main()
