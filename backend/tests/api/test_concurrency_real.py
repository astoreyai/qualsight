"""Plan F QS-S97 — concurrency and race audit on the real store and engine.

Real fixture data only. The suite exercises the races the API can actually
hit: many reader threads during a real batch write transaction (snapshot
visibility — a dataset must never be visible without its samples), concurrent
identical uploads (single admission, no conflict), and concurrent distinct
uploads (all recorded; content-addressed ids never collide).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)

ROOT = Path(__file__).resolve().parents[3]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
LFW_TREE = Path("/mnt/projects/datasets/lfw/lfw_funneled")
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class ConcurrencyRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-race-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        cls.client = TestClient(create_app(settings))
        cls.client.__enter__()
        project = cls.client.post(
            "/api/projects",
            json={"name": "concurrency gate", "description": "Plan F S97.", "purpose_scope": RESEARCH_SCOPE},
        ).json()
        cls.project_id = project["project_id"]
        cls.client.post(f"/api/projects/{cls.project_id}/open")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def test_readers_never_observe_a_partial_import_transaction(self) -> None:
        """While the 13k-sample LFW import commits, every reader snapshot is all-or-nothing."""

        stop = threading.Event()
        violations: list[str] = []
        errors: list[str] = []

        def reader() -> None:
            while not stop.is_set():
                try:
                    datasets = self.client.get(f"/api/projects/{self.project_id}/datasets")
                    if datasets.status_code != 200:
                        errors.append(f"datasets {datasets.status_code}")
                        continue
                    for row in datasets.json():
                        # Visibility invariant: the LFW dataset (a single 13k-sample import
                        # transaction) is either absent or complete in every reader snapshot.
                        # Single-sample upload datasets from the other tests are exempt.
                        if "LFW" not in (row.get("name") or ""):
                            continue
                        recorded = row.get("sample_count") or 0
                        if recorded != 13233:
                            violations.append(f"LFW dataset visible with live sample_count {recorded}")
                except Exception as error:  # noqa: BLE001 - collected for the assertion
                    errors.append(f"{type(error).__name__}: {error}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(reader) for _ in range(8)]
            imported = self.client.post(
                f"/api/projects/{self.project_id}/datasets/import-registered",
                json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
            )
            time.sleep(1.0)
            stop.set()
            for future in futures:
                future.result(timeout=60)
        self.assertEqual(imported.status_code, 201, imported.text)
        self.assertEqual(violations, [])
        self.assertEqual(errors, [])
        type(self).dataset_id = imported.json()["dataset_id"]

    def test_concurrent_identical_uploads_admit_once_without_conflict(self) -> None:
        data = (LFW_TREE / "Colin_Powell" / "Colin_Powell_0002.jpg").read_bytes()

        def upload(_: int):
            return self.client.post(
                "/api/quality/assess-upload",
                params={"project_id": self.project_id, "purpose_scope": RESEARCH_SCOPE, "filename": "same_photo.jpg"},
                content=data,
                headers={"content-type": "application/octet-stream"},
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(upload, range(4)))
        statuses = sorted(r.status_code for r in responses)
        self.assertTrue(all(code == 201 for code in statuses), [(r.status_code, r.text[:200]) for r in responses])
        shas = {r.json()["image_sha256"] for r in responses}
        self.assertEqual(len(shas), 1)
        stored = {r.json()["stored_path"] for r in responses}
        self.assertEqual(len(stored), 1)
        run_ids = {r.json()["run_id"] for r in responses}
        self.assertEqual(len(run_ids), 4, "each execution is its own recorded Run")

    def test_concurrent_distinct_uploads_all_recorded(self) -> None:
        photos = sorted((LFW_TREE / "George_Robertson").glob("*.jpg"))[:4]
        self.assertEqual(len(photos), 4)

        def upload(path: Path):
            return self.client.post(
                "/api/quality/assess-upload",
                params={"project_id": self.project_id, "purpose_scope": RESEARCH_SCOPE, "filename": path.name},
                content=path.read_bytes(),
                headers={"content-type": "application/octet-stream"},
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(upload, photos))
        self.assertTrue(all(r.status_code == 201 for r in responses), [(r.status_code, r.text[:200]) for r in responses])
        shas = {r.json()["image_sha256"] for r in responses}
        self.assertEqual(len(shas), 4)


if __name__ == "__main__":
    unittest.main()
