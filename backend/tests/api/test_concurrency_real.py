"""Plan F QS-S97 — concurrency and race audit on the real store and engine.

Real fixture data only. The suite exercises the races the API can actually
hit: many reader threads during a real batch write transaction (snapshot
visibility — a dataset must never be visible without its samples), concurrent
identical uploads (single admission, no conflict), and concurrent distinct
uploads (all recorded; content-addressed ids never collide).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import sqlite3
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
from qualsight.api.store import RunStore
from qualsight.importers.owner_supplied import OwnerSuppliedAdmission, SOURCE_KIND
from qualsight.security.purge import purge_project

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
        cls.database_path = settings.database_path
        cls.client = TestClient(create_app(settings), base_url="http://127.0.0.1")
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

    def test_z_purge_and_real_upload_are_mutually_exclusive_and_leave_no_residual_project_data(self) -> None:
        """A real owner-photo assessment either finishes before purge or is refused; it cannot write through purge."""

        data = (LFW_TREE / "Colin_Powell" / "Colin_Powell_0002.jpg").read_bytes()
        start = threading.Barrier(2)

        def upload():
            start.wait()
            return self.client.post(
                "/api/quality/assess-upload",
                params={"project_id": self.project_id, "purpose_scope": RESEARCH_SCOPE, "filename": "purge_race_photo.jpg"},
                content=data,
                headers={"content-type": "application/octet-stream"},
            )

        def purge():
            start.wait()
            return self.client.post(f"/api/projects/{self.project_id}/purge", json={"confirm": self.project_id})

        with ThreadPoolExecutor(max_workers=2) as pool:
            upload_future = pool.submit(upload)
            purge_future = pool.submit(purge)
            upload_response = upload_future.result(timeout=120)
            purge_response = purge_future.result(timeout=120)

        self.assertIn(upload_response.status_code, {201, 409}, upload_response.text)
        self.assertIn(purge_response.status_code, {201, 409}, purge_response.text)
        if purge_response.status_code == 409:
            purge_response = self.client.post(f"/api/projects/{self.project_id}/purge", json={"confirm": self.project_id})
        self.assertEqual(purge_response.status_code, 201, purge_response.text)
        self.assertTrue(purge_response.json()["verified"], purge_response.json())
        self.assertEqual(self.client.get(f"/api/projects/{self.project_id}/datasets").json(), [])

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
        sample_ids = {r.json()["sample_id"] for r in responses}
        self.assertEqual(len(sample_ids), 1)
        store = RunStore(self.database_path)
        try:
            recorded_samples = [store.sample(sample_id) for sample_id in sample_ids]
        finally:
            store.close()
        self.assertTrue(all(sample is not None for sample in recorded_samples))
        stored = {Path(sample.source_path) for sample in recorded_samples if sample}
        self.assertEqual(len(stored), 1)
        managed_path = next(iter(stored))
        self.assertTrue(managed_path.is_relative_to(self.data_dir / "owner_supplied"))
        self.assertEqual(hashlib.sha256(managed_path.read_bytes()).hexdigest(), next(iter(shas)))
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


class OwnerFileCrossProjectRaceTests(unittest.TestCase):
    def test_cross_project_admission_during_purge_preserves_the_new_reference_bytes(self) -> None:
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-owner-race-", dir="/mnt/projects"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        try:
            with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
                projects = []
                for name in ("owner-file race source", "owner-file race destination"):
                    response = client.post(
                        "/api/projects",
                        json={"name": name, "description": "Real cross-project owner-file purge race gate.", "purpose_scope": RESEARCH_SCOPE},
                    )
                    self.assertEqual(response.status_code, 201, response.text)
                    projects.append(response.json()["project_id"])

            store = RunStore(settings.database_path)
            try:
                owner_root = data_dir / "owner_supplied"
                owner_root.mkdir(parents=True, exist_ok=True)
                managed_paths: list[Path] = []
                admitted_at = datetime.now(timezone.utc).astimezone().isoformat()
                for source in sorted(LFW_TREE.glob("*/*.jpg")):
                    digest = hashlib.sha256(source.read_bytes()).hexdigest()
                    target = owner_root / f"{digest}.jpg"
                    if target.exists():
                        continue
                    target.hardlink_to(source)
                    store.record_owner_supplied(
                        {
                            "path": str(target),
                            "sha256": digest,
                            "project_id": projects[0],
                            "actor_id": "local-user:race-gate",
                            "original_name": source.name,
                            "size_bytes": source.stat().st_size,
                            "storage": "COPIED",
                            "source_kind": SOURCE_KIND,
                            "admitted_at": admitted_at,
                        }
                    )
                    managed_paths.append(target)

                managed_paths.sort()
                self.assertGreaterEqual(len(managed_paths), 13_000)
                sentinel = managed_paths[0]
                shared_target = managed_paths[-1]
                shared_bytes = shared_target.read_bytes()
                admission = OwnerSuppliedAdmission(store, data_dir, "local-user:race-gate")

                with ThreadPoolExecutor(max_workers=1) as pool:
                    purge_future = pool.submit(
                        purge_project,
                        store,
                        data_dir=data_dir,
                        project_id=projects[0],
                        actor="local-user:race-gate",
                        confirm=projects[0],
                    )
                    deadline = time.monotonic() + 30
                    while sentinel.exists() and time.monotonic() < deadline:
                        if purge_future.done():
                            self.fail("purge completed before the real-file traversal race window was observed")
                        time.sleep(0.0005)
                    self.assertFalse(sentinel.exists(), "purge did not begin unlinking the recorded real files")
                    self.assertFalse(purge_future.done(), "purge traversal ended before the destination admission")
                    self.assertTrue(shared_target.exists(), "the last sorted managed file was deleted before admission could start")

                    admitted = admission.admit_bytes(shared_bytes, "shared_during_purge.jpg", projects[1])
                    purge_record = purge_future.result(timeout=60)

                self.assertTrue(purge_record["verified"], purge_record)
                self.assertEqual(admitted.path, str(shared_target))
                self.assertTrue(shared_target.exists(), "the purge deleted bytes after the second project admitted the same managed path")
                self.assertEqual(hashlib.sha256(shared_target.read_bytes()).hexdigest(), admitted.sha256)
                destination_records = store.owner_supplied_for_project(projects[1])
                self.assertEqual([record["path"] for record in destination_records], [str(shared_target)])
            finally:
                store.close()
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


class PurgeCompletionFenceRealTests(unittest.TestCase):
    def test_verified_purge_keeps_mutation_fence_through_completion_audit(self) -> None:
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-purge-fence-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        database = settings.database_path
        try:
            with TestClient(
                create_app(settings),
                base_url="http://127.0.0.1",
                raise_server_exceptions=False,
            ) as client:
                anchor = client.post(
                    "/api/projects",
                    json={
                        "name": "purge fence anchor",
                        "description": "Retains the real LFW rows used to expose the audit window.",
                        "purpose_scope": RESEARCH_SCOPE,
                    },
                )
                self.assertEqual(anchor.status_code, 201, anchor.text)
                anchor_id = anchor.json()["project_id"]
                self.assertEqual(client.post(f"/api/projects/{anchor_id}/open").status_code, 200)
                imported = client.post(
                    f"/api/projects/{anchor_id}/datasets/import-registered",
                    json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
                )
                self.assertEqual(imported.status_code, 201, imported.text)

                victim = client.post(
                    "/api/projects",
                    json={
                        "name": "purge fence victim",
                        "description": "Real owner bytes exercise the post-purge mutation boundary.",
                        "purpose_scope": RESEARCH_SCOPE,
                    },
                )
                self.assertEqual(victim.status_code, 201, victim.text)
                victim_id = victim.json()["project_id"]
                self.assertEqual(client.post(f"/api/projects/{victim_id}/open").status_code, 200)
                first_source = LFW_TREE / "Colin_Powell" / "Colin_Powell_0002.jpg"
                first = client.post(
                    "/api/quality/assess-upload",
                    params={
                        "project_id": victim_id,
                        "purpose_scope": RESEARCH_SCOPE,
                        "filename": first_source.name,
                    },
                    content=first_source.read_bytes(),
                    headers={"content-type": "application/octet-stream"},
                )
                self.assertEqual(first.status_code, 201, first.text)

                connection = sqlite3.connect(database)
                connection.execute(
                    "CREATE TRIGGER stall_verified_purge_completion_audit "
                    "BEFORE INSERT ON audit_events "
                    "WHEN instr(NEW.record_json, '\"event_type\":\"DATASET_PURGED\"') > 0 "
                    "BEGIN SELECT sum(length(a.record_json) + length(b.record_json)) "
                    "FROM samples AS a CROSS JOIN samples AS b; END"
                )
                connection.commit()
                connection.close()

                try:
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        purge_future = pool.submit(
                            client.post,
                            f"/api/projects/{victim_id}/purge",
                            json={"confirm": victim_id},
                        )

                        def completion_audit_holds_writer() -> bool:
                            probe = sqlite3.connect(database, timeout=0.01)
                            try:
                                recorded = probe.execute(
                                    "SELECT 1 FROM purges WHERE project_id = ? LIMIT 1",
                                    (victim_id,),
                                ).fetchone()
                                if recorded is None:
                                    return False
                                try:
                                    probe.execute("BEGIN IMMEDIATE")
                                except sqlite3.OperationalError as error:
                                    return "locked" in str(error).lower()
                                probe.execute("ROLLBACK")
                                return False
                            finally:
                                probe.close()

                        deadline = time.monotonic() + 120
                        while not completion_audit_holds_writer():
                            if purge_future.done():
                                self.fail(
                                    "purge completed before the real completion-audit window was observed"
                                )
                            if time.monotonic() >= deadline:
                                self.fail(
                                    "timed out waiting for the real completion-audit writer"
                                )
                            time.sleep(0.005)

                        second_source = (
                            LFW_TREE / "Colin_Powell" / "Colin_Powell_0003.jpg"
                        )
                        during_audit = client.post(
                            "/api/quality/assess-upload",
                            params={
                                "project_id": victim_id,
                                "purpose_scope": RESEARCH_SCOPE,
                                "filename": second_source.name,
                            },
                            content=second_source.read_bytes(),
                            headers={"content-type": "application/octet-stream"},
                        )
                        purge = purge_future.result(timeout=120)
                finally:
                    connection = sqlite3.connect(database)
                    connection.execute(
                        "DROP TRIGGER IF EXISTS stall_verified_purge_completion_audit"
                    )
                    connection.commit()
                    connection.close()

                self.assertEqual(purge.status_code, 201, purge.text)
                self.assertTrue(purge.json()["verified"], purge.json())
                self.assertEqual(during_audit.status_code, 409, during_audit.text)
                self.assertEqual(
                    client.get(f"/api/projects/{victim_id}/datasets").json(),
                    [],
                )
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
