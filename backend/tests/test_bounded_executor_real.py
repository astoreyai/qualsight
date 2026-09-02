"""Bounded scientific work admission exercised with a registered real photograph."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.api.store import SCHEMA_MIGRATIONS
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)


REAL_PHOTO = Path("/mnt/projects/02_perception_biometrics/OFIQ-Project/data/tests/images/r-01-frontal.png")
ROOT = Path(__file__).resolve().parents[2]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class BoundedExecutorRealTests(unittest.TestCase):
    def test_worker_and_queue_capacity_refuse_excess_work_without_losing_admitted_tasks(self) -> None:
        from qualsight.execution.bounded_executor import BoundedExecutor, ExecutorOverloaded

        release = threading.Event()
        expected = hashlib.sha256(REAL_PHOTO.read_bytes()).hexdigest()

        def hash_real_photo_after_release() -> str:
            release.wait(timeout=30)
            return hashlib.sha256(REAL_PHOTO.read_bytes()).hexdigest()

        executor = BoundedExecutor(max_workers=1, max_pending=1, thread_name_prefix="qualsight-capacity-gate")
        try:
            first = executor.submit(hash_real_photo_after_release)
            second = executor.submit(hash_real_photo_after_release)
            with self.assertRaises(ExecutorOverloaded):
                executor.submit(hash_real_photo_after_release)
            status = executor.status()
            self.assertEqual(status["capacity"], 2)
            self.assertEqual(status["available"], 0)
            release.set()
            self.assertEqual(first.result(timeout=30), expected)
            self.assertEqual(second.result(timeout=30), expected)
            self.assertEqual(executor.status()["available"], 2)
        finally:
            release.set()
            executor.shutdown(wait=True, cancel_futures=False)

    def test_api_refuses_excess_real_quality_work_before_admitting_an_operation(self) -> None:
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-bounded-api-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
            scientific_workers=1,
            scientific_pending=0,
            shutdown_drain_seconds=5.0,
        )
        try:
            with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
                project = client.post(
                    "/api/projects",
                    json={
                        "name": "Bounded real quality gate",
                        "description": "Real LFW work verifies bounded API admission.",
                        "purpose_scope": RESEARCH_SCOPE,
                    },
                )
                self.assertEqual(project.status_code, 201, project.text)
                project_id = project.json()["project_id"]
                self.assertEqual(client.post(f"/api/projects/{project_id}/open").status_code, 200)
                imported = client.post(
                    f"/api/projects/{project_id}/datasets/import-registered",
                    json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
                )
                self.assertEqual(imported.status_code, 201, imported.text)
                dataset_id = imported.json()["dataset_id"]
                owner_root = data_dir / "real-owner-capacity-input"
                for subject in ("George_W_Bush", "Tony_Blair"):
                    subject_root = owner_root / subject
                    subject_root.mkdir(parents=True)
                    sources = sorted(
                        (
                            Path("/mnt/projects/datasets/lfw/lfw_funneled")
                            / subject
                        ).glob("*.jpg")
                    )[:2]
                    self.assertEqual(len(sources), 2)
                    for source in sources:
                        shutil.copyfile(source, subject_root / source.name)
                owner_import = client.post(
                    f"/api/projects/{project_id}/datasets/import-owner-directory",
                    json={"root": str(owner_root), "name": "Real LFW capacity input"},
                )
                self.assertEqual(owner_import.status_code, 201, owner_import.text)
                owner_dataset_id = owner_import.json()["dataset_id"]
                live = client.get("/api/live")
                self.assertEqual(live.status_code, 200, live.text)
                self.assertEqual(live.json(), {"status": "alive"})
                self.assertRegex(live.headers["X-QualSight-Request-ID"], r"^[0-9a-f]{32}$")
                self.assertEqual(live.headers["X-QualSight-API-Version"], "2.0.0-beta.1")
                self.assertEqual(client.get("/api/version").json()["contract_version"], "2.0.0-beta.1")
                metrics = client.get("/api/metrics")
                self.assertEqual(metrics.status_code, 200, metrics.text)
                self.assertGreater(metrics.json()["http"]["requests"], 0)
                self.assertEqual(
                    metrics.json()["database"]["schema_version"],
                    SCHEMA_MIGRATIONS[-1][0],
                )
                ready = client.get("/api/ready")
                self.assertEqual(ready.status_code, 200, ready.text)
                self.assertEqual(ready.json()["status"], "ready")
                endpoint = f"/api/projects/{project_id}/datasets/{dataset_id}/quality-batch"
                first = client.post(
                    endpoint,
                    json={
                        "selection": {"kind": "subset_fixture"},
                        "chunks": 1,
                        "purpose_scope": RESEARCH_SCOPE,
                    },
                )
                self.assertEqual(first.status_code, 202, first.text)
                saturated = client.get("/api/ready")
                self.assertEqual(saturated.status_code, 503, saturated.text)
                self.assertEqual(saturated.json()["status"], "not_ready")
                self.assertEqual(saturated.json()["checks"]["scientific_capacity"]["available"], 0)
                refused = client.post(
                    endpoint,
                    json={
                        "selection": {"kind": "subset_fixture"},
                        "chunks": 2,
                        "purpose_scope": RESEARCH_SCOPE,
                    },
                )
                self.assertEqual(refused.status_code, 503, refused.text)
                self.assertEqual(refused.json()["detail"]["error"], "SCIENTIFIC_WORK_CAPACITY_EXHAUSTED")
                owner_refused = client.post(
                    f"/api/projects/{project_id}/datasets/{owner_dataset_id}/protocols/freeze-owner-pairs",
                    json={"seed": 73},
                )
                self.assertEqual(owner_refused.status_code, 503, owner_refused.text)
                self.assertEqual(
                    owner_refused.json()["detail"]["error"],
                    "SCIENTIFIC_WORK_CAPACITY_EXHAUSTED",
                )
                protocols = client.get(f"/api/projects/{project_id}/protocols")
                self.assertEqual(protocols.status_code, 200, protocols.text)
                self.assertEqual(protocols.json(), [])
                operations = client.get(f"/api/projects/{project_id}/operations")
                self.assertEqual(operations.status_code, 200, operations.text)
                self.assertEqual(len(operations.json()), 1, operations.text)
                shutdown_started = time.monotonic()
            self.assertLess(time.monotonic() - shutdown_started, 10.0)
            from qualsight.security.process_lease import DataDirectoryLease

            lease = DataDirectoryLease.acquire(data_dir, owner="post-shutdown-verification")
            lease.close()
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_shutdown_wait_is_bounded_until_admitted_real_work_finishes(self) -> None:
        from qualsight.execution.bounded_executor import BoundedExecutor

        release = threading.Event()
        expected = hashlib.sha256(REAL_PHOTO.read_bytes()).hexdigest()

        def hash_real_photo_after_release() -> str:
            release.wait(timeout=30)
            return hashlib.sha256(REAL_PHOTO.read_bytes()).hexdigest()

        executor = BoundedExecutor(max_workers=1, max_pending=0, thread_name_prefix="qualsight-drain-gate")
        future = executor.submit(hash_real_photo_after_release)
        executor.shutdown(wait=False, cancel_futures=True)
        self.assertFalse(executor.wait_for_idle(timeout_seconds=0.01))
        release.set()
        self.assertEqual(future.result(timeout=30), expected)
        self.assertTrue(executor.wait_for_idle(timeout_seconds=5.0))
        executor.shutdown(wait=True, cancel_futures=False)


if __name__ == "__main__":
    unittest.main()
