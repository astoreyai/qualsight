"""Crash recovery for owner input and owner-pair protocol publication.

All image bytes are unchanged files from the registered local LFW tree.  A
real SQLite writer lock holds each worker after filesystem publication, and
the worker is terminated with SIGKILL at that observed durability boundary.
"""

from __future__ import annotations

import hashlib
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
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)
from qualsight.importers import owner_supplied


ROOT = Path(__file__).resolve().parents[2]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
LFW_TREE = Path("/mnt/projects/datasets/lfw/lfw_funneled")
REAL_PHOTO = LFW_TREE / "Colin_Powell" / "Colin_Powell_0001.jpg"
REAL_PHOTO_SHA256 = "4aeed009e3eebba5e1a145156e0c98eda275cc00df74365c31200906d09d3595"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class OwnerPublicationRecoveryRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-owner-publication-"))
        cls.owner_root = Path(tempfile.mkdtemp(prefix="qualsight-owner-protocol-"))
        for subject in ("George_W_Bush", "Tony_Blair"):
            target = cls.owner_root / subject
            target.mkdir(parents=True)
            sources = sorted((LFW_TREE / subject).glob("*.jpg"))[:2]
            assert len(sources) == 2
            for source in sources:
                shutil.copyfile(source, target / source.name)

        cls.settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        with cls._client() as client:
            input_project = cls._create_project(client, "owner input recovery gate")
            cls.input_project_id = input_project
            protocol_project = cls._create_project(client, "owner protocol recovery gate")
            cls.protocol_project_id = protocol_project
            imported = client.post(
                f"/api/projects/{protocol_project}/datasets/import-owner-directory",
                json={
                    "root": str(cls.owner_root),
                    "name": "real LFW owner protocol recovery input",
                },
            )
            assert imported.status_code == 201, imported.text
            cls.protocol_dataset_id = imported.json()["dataset_id"]

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.data_dir, ignore_errors=True)
        shutil.rmtree(cls.owner_root, ignore_errors=True)

    @classmethod
    def _client(cls) -> TestClient:
        return TestClient(
            create_app(cls.settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        )

    @staticmethod
    def _create_project(client: TestClient, name: str) -> str:
        created = client.post(
            "/api/projects",
            json={
                "name": name,
                "description": "Real owner publication crash-recovery evidence.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["project_id"]
        opened = client.post(f"/api/projects/{project_id}/open")
        assert opened.status_code == 200, opened.text
        return project_id

    @staticmethod
    def _environment() -> dict[str, str]:
        return {
            **os.environ,
            "PYTHONPATH": str(ROOT / "backend" / "src") + os.pathsep + str(ROOT),
        }

    def _worker(self, source: str, *arguments: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-c", source, str(self.data_dir), *arguments],
            cwd=ROOT,
            env=self._environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

    def _wait_for_file(
        self,
        worker: subprocess.Popen[str],
        predicate,
        *,
        timeout: float,
        description: str,
    ) -> Path:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and worker.poll() is None:
            observed = predicate()
            if observed is not None:
                return observed
            time.sleep(0.002)
        output = worker.communicate(timeout=30)[0]
        self.fail(f"{description} was not published before worker exit: {output}")

    @staticmethod
    def _kill(worker: subprocess.Popen[str]) -> None:
        if worker.poll() is None:
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait(timeout=30)
        if worker.stdout is not None:
            worker.stdout.close()

    def test_copied_owner_bytes_recover_exact_project_reference_after_process_death(
        self,
    ) -> None:
        payload = REAL_PHOTO.read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), REAL_PHOTO_SHA256)
        target = self.data_dir / "owner_supplied" / f"{REAL_PHOTO_SHA256}.jpg"
        ready = self.data_dir / "owner-input-worker.ready"
        proceed = self.data_dir / "owner-input-worker.proceed"
        worker_source = """
from pathlib import Path
import sys
import time
from qualsight.api.store import RunStore
from qualsight.importers.owner_supplied import OwnerSuppliedAdmission

data_dir = Path(sys.argv[1])
source = Path(sys.argv[2])
project_id = sys.argv[3]
ready = Path(sys.argv[4])
proceed = Path(sys.argv[5])
store = RunStore(data_dir / "qualsight.sqlite3")
try:
    ready.write_text("ready\\n", encoding="utf-8")
    while not proceed.is_file():
        time.sleep(0.01)
    OwnerSuppliedAdmission(store, data_dir, "local-user:owner-input-crash").admit_bytes(
        source.read_bytes(), source.name, project_id
    )
finally:
    store.close()
"""
        worker = self._worker(
            worker_source,
            str(REAL_PHOTO),
            self.input_project_id,
            str(ready),
            str(proceed),
        )
        lock = sqlite3.connect(self.data_dir / "qualsight.sqlite3", timeout=30)
        try:
            self._wait_for_file(
                worker,
                lambda: ready if ready.is_file() else None,
                timeout=30,
                description="owner input worker readiness",
            )
            lock.execute("BEGIN IMMEDIATE")
            proceed.write_text("proceed\n", encoding="utf-8")
            self._wait_for_file(
                worker,
                lambda: target if target.is_file() else None,
                timeout=30,
                description="real copied owner input",
            )
            self._kill(worker)
        finally:
            if worker.poll() is None:
                self._kill(worker)
            lock.rollback()
            lock.close()

        self.assertEqual(worker.returncode, -signal.SIGKILL)
        self.assertEqual(target.read_bytes(), payload)
        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            self.assertEqual(
                store.owner_supplied_for_project(self.input_project_id), []
            )
            receipts = owner_supplied.prepared_owner_input_admissions(self.data_dir)
            self.assertEqual(len(receipts), 1)
            prepared = owner_supplied.recover_prepared_owner_input_admission(
                receipts[0], data_dir=self.data_dir
            )
            self.assertEqual(prepared.project_id, self.input_project_id)
            self.assertEqual(prepared.admitted.sha256, REAL_PHOTO_SHA256)
            self.assertEqual(prepared.admitted.path, str(target))
            store.record_owner_supplied(prepared.record)
            owner_supplied.finalize_prepared_owner_input_admission(prepared)
            self.assertEqual(
                len(store.owner_supplied_for_project(self.input_project_id)), 1
            )
            self.assertFalse(prepared.receipt_path.exists())
            self.assertFalse(prepared.stage_path.exists())
            self.assertEqual(target.read_bytes(), payload)
        finally:
            store.close()

    def test_owner_pair_protocol_recovers_manifest_pairs_and_audit_after_process_death(
        self,
    ) -> None:
        ready = self.data_dir / "owner-protocol-worker.ready"
        proceed = self.data_dir / "owner-protocol-worker.proceed"
        worker_source = """
from pathlib import Path
import sys
import time
from qualsight.api.store import RunStore
from qualsight.importers.owner_supplied import freeze_owner_pairs

data_dir = Path(sys.argv[1])
project_id = sys.argv[2]
dataset_id = sys.argv[3]
ready = Path(sys.argv[4])
proceed = Path(sys.argv[5])
store = RunStore(data_dir / "qualsight.sqlite3")
try:
    project = store.project(project_id)
    dataset = store.dataset(dataset_id)
    if project is None or dataset is None:
        raise RuntimeError("owner protocol source records are unavailable")
    ready.write_text("ready\\n", encoding="utf-8")
    while not proceed.is_file():
        time.sleep(0.01)
    freeze_owner_pairs(
        store,
        project,
        dataset,
        data_dir=data_dir,
        actor_id="local-user:owner-protocol-crash",
        seed=19,
        max_pairs=20_000,
        max_mated_pairs_per_subject=6,
        nonmated_multiple=1.0,
    )
finally:
    store.close()
"""
        worker = self._worker(
            worker_source,
            self.protocol_project_id,
            self.protocol_dataset_id,
            str(ready),
            str(proceed),
        )
        lock = sqlite3.connect(self.data_dir / "qualsight.sqlite3", timeout=30)
        manifest: Path | None = None
        try:
            self._wait_for_file(
                worker,
                lambda: ready if ready.is_file() else None,
                timeout=30,
                description="owner protocol worker readiness",
            )
            lock.execute("BEGIN IMMEDIATE")
            proceed.write_text("proceed\n", encoding="utf-8")

            def published_manifest() -> Path | None:
                manifests = list((self.data_dir / "protocols").glob("*.jsonl"))
                return manifests[0] if len(manifests) == 1 else None

            manifest = self._wait_for_file(
                worker,
                published_manifest,
                timeout=30,
                description="real owner-pair protocol manifest",
            )
            self._kill(worker)
        finally:
            if worker.poll() is None:
                self._kill(worker)
            lock.rollback()
            lock.close()

        self.assertEqual(worker.returncode, -signal.SIGKILL)
        assert manifest is not None
        manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            with sqlite3.connect(store.database_path) as connection:
                protocol_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM protocols WHERE project_id = ?",
                        (self.protocol_project_id,),
                    ).fetchone()[0]
                )
            self.assertEqual(protocol_count, 0)
            receipts = owner_supplied.prepared_owner_protocols(self.data_dir)
            self.assertEqual(len(receipts), 1)
            prepared = owner_supplied.recover_prepared_owner_protocol(
                receipts[0], data_dir=self.data_dir
            )
            self.assertEqual(prepared.record.project_id, self.protocol_project_id)
            self.assertEqual(prepared.record.dataset_id, self.protocol_dataset_id)
            self.assertEqual(prepared.record.pair_manifest.sha256, manifest_sha256)
            self.assertTrue(prepared.pairs)
            self.assertEqual(
                {pair.protocol_id for pair in prepared.pairs},
                {prepared.record.protocol_id},
            )
            self.assertEqual(
                prepared.audit_event.subject_id, prepared.record.protocol_id
            )
            store.record_protocol(
                prepared.record, list(prepared.pairs), prepared.audit_event
            )
            owner_supplied.finalize_prepared_owner_protocol(prepared)
            recovered = store.protocol(prepared.record.protocol_id)
            self.assertIsNotNone(recovered)
            self.assertFalse(prepared.receipt_path.exists())
            self.assertFalse(prepared.stage_path.exists())
            self.assertEqual(hashlib.sha256(manifest.read_bytes()).hexdigest(), manifest_sha256)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
