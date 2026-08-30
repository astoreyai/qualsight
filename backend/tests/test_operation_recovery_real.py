"""Durable operation recovery over a project backed by the registered real LFW dataset."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.api.store import RunStore
from qualsight.engines.ofiq.adapter import content_id
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID
from qualsight.security.purge import purge_targets


ROOT = Path(__file__).resolve().parents[2]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class OperationRecoveryRealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-operation-recovery-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=self.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        self.settings = settings
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
            project = client.post(
                "/api/projects",
                json={
                    "name": "Operation recovery gate",
                    "description": "Crash-boundary verification over registered LFW records.",
                    "purpose_scope": RESEARCH_SCOPE,
                },
            )
            self.assertEqual(project.status_code, 201, project.text)
            self.project_id = project.json()["project_id"]
            client.post(f"/api/projects/{self.project_id}/open")
            imported = client.post(
                f"/api/projects/{self.project_id}/datasets/import-registered",
                json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
            )
            self.assertEqual(imported.status_code, 201, imported.text)
            self.dataset_id = imported.json()["dataset_id"]

    def tearDown(self) -> None:
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_admitted_operation_is_interrupted_truthfully_after_restart_and_not_readmitted(self) -> None:
        request = {
            "dataset_id": self.dataset_id,
            "purpose_scope": RESEARCH_SCOPE,
            "selection": {"kind": "subset_fixture"},
        }
        identity = json.dumps(
            {"kind": "QUALITY_BATCH", "project_id": self.project_id, "request": request},
            sort_keys=True,
            separators=(",", ":"),
        )
        operation_id = f"operation:sha256:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"

        store = RunStore(self.data_dir / "qualsight.sqlite3")
        admitted, created = store.admit_operation(
            operation_id=operation_id,
            operation_kind="QUALITY_BATCH",
            project_id=self.project_id,
            request=request,
            output_path=self.data_dir / "jobs" / operation_id.replace(":", "_"),
        )
        self.assertTrue(created)
        self.assertEqual(admitted["state"], "QUEUED")
        store.close()

        restarted = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            reconciled = restarted.reconcile_interrupted_operations()
            self.assertEqual([row["operation_id"] for row in reconciled], [operation_id])
            recovered = restarted.operation(operation_id)
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered["state"], "INTERRUPTED")
            self.assertIsNotNone(recovered["ended_at"])
            self.assertIn("process ended before a terminal state", recovered["error"])

            existing, created_again = restarted.admit_operation(
                operation_id=operation_id,
                operation_kind="QUALITY_BATCH",
                project_id=self.project_id,
                request=request,
                output_path=self.data_dir / "jobs" / operation_id.replace(":", "_"),
            )
            self.assertFalse(created_again)
            self.assertEqual(existing["state"], "INTERRUPTED")
        finally:
            restarted.close()

    def test_api_startup_reconciles_and_exposes_an_abandoned_operation(self) -> None:
        request = {
            "dataset_id": self.dataset_id,
            "purpose_scope": RESEARCH_SCOPE,
            "selection": {"kind": "subset_fixture"},
        }
        identity = json.dumps(
            {"kind": "QUALITY_BATCH", "project_id": self.project_id, "request": request},
            sort_keys=True,
            separators=(",", ":"),
        )
        operation_id = f"operation:sha256:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"
        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            store.admit_operation(
                operation_id=operation_id,
                operation_kind="QUALITY_BATCH",
                project_id=self.project_id,
                request=request,
                output_path=self.data_dir / "jobs" / operation_id.replace(":", "_"),
            )
        finally:
            store.close()

        with TestClient(create_app(self.settings), base_url="http://127.0.0.1") as restarted:
            status = restarted.get(f"/api/operations/{operation_id}")
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["state"], "INTERRUPTED")
            project_operations = restarted.get(f"/api/projects/{self.project_id}/operations")
            self.assertEqual(project_operations.status_code, 200, project_operations.text)
            self.assertEqual(
                [row["operation_id"] for row in project_operations.json()],
                [operation_id],
            )

    def test_real_quality_batch_operation_is_durable_from_admission_through_restart(self) -> None:
        with TestClient(create_app(self.settings), base_url="http://127.0.0.1") as client:
            started = client.post(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/quality-batch",
                json={
                    "selection": {"kind": "subset_fixture"},
                    "chunks": 2,
                    "purpose_scope": RESEARCH_SCOPE,
                },
            )
            self.assertEqual(started.status_code, 202, started.text)
            operation_id = started.json()["job_id"]
            admitted = client.get(f"/api/operations/{operation_id}")
            self.assertEqual(admitted.status_code, 200, admitted.text)
            self.assertIn(admitted.json()["state"], {"QUEUED", "RUNNING", "COMPLETED"})
            self.assertIsNotNone(admitted.json()["accepted_at"])
            self.assertEqual(admitted.json()["request"]["dataset_id"], self.dataset_id)

            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                status = client.get(f"/api/jobs/{operation_id}")
                self.assertEqual(status.status_code, 200, status.text)
                if status.json()["state"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                    break
                time.sleep(0.25)
            else:
                self.fail(f"real quality operation did not reach a terminal state: {status.text}")
            self.assertEqual(status.json()["state"], "COMPLETED", status.text)
            terminal = client.get(f"/api/operations/{operation_id}").json()
            self.assertEqual(terminal["state"], "COMPLETED")
            self.assertIsNotNone(terminal["started_at"])
            self.assertIsNotNone(terminal["ended_at"])
            self.assertIsNone(terminal["error"])
            self.assertTrue(Path(terminal["output_path"]).is_dir())

        with TestClient(create_app(self.settings), base_url="http://127.0.0.1") as restarted:
            recovered = restarted.get(f"/api/operations/{operation_id}")
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(recovered.json()["state"], "COMPLETED")

    def test_confirmed_purge_intent_resumes_after_restart_and_attests_real_file_and_metadata_deletion(self) -> None:
        with TestClient(create_app(self.settings), base_url="http://127.0.0.1") as client:
            started = client.post(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/quality-batch",
                json={
                    "selection": {"kind": "subset_fixture"},
                    "chunks": 2,
                    "purpose_scope": RESEARCH_SCOPE,
                },
            )
            self.assertEqual(started.status_code, 202, started.text)
            operation_id = started.json()["job_id"]
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                status = client.get(f"/api/jobs/{operation_id}")
                if status.json()["state"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                    break
                time.sleep(0.25)
            else:
                self.fail(f"real quality operation did not reach a terminal state: {status.text}")
            self.assertEqual(status.json()["state"], "COMPLETED", status.text)
            output_path = Path(client.get(f"/api/operations/{operation_id}").json()["output_path"])
            self.assertTrue(output_path.is_dir())

        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            targets = purge_targets(store, self.data_dir, self.project_id)
            self.assertIn(str(output_path), targets["directories"])
            purge_id = content_id("purge", self.project_id, "restart-recovery-gate-v1")
            journal, created = store.admit_purge(
                purge_id=purge_id,
                project_id=self.project_id,
                actor_id="local-recovery-gate",
                targets=targets,
            )
            self.assertTrue(created)
            self.assertEqual(journal["state"], "INTENT_RECORDED")
        finally:
            store.close()

        with TestClient(create_app(self.settings), base_url="http://127.0.0.1") as restarted:
            purges = restarted.get(f"/api/projects/{self.project_id}/purges")
            self.assertEqual(purges.status_code, 200, purges.text)
            self.assertEqual(len(purges.json()), 1)
            self.assertEqual(purges.json()[0]["purge_id"], purge_id)
            self.assertTrue(purges.json()[0]["verified"])
            self.assertFalse(output_path.exists())
            self.assertEqual(restarted.get(f"/api/operations/{operation_id}").status_code, 404)
            project = restarted.get(f"/api/projects/{self.project_id}")
            self.assertEqual(project.status_code, 200, project.text)


if __name__ == "__main__":
    unittest.main()
