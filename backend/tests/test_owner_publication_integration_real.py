"""Startup and purge integration for durable owner-supplied publications."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.api.store import RunStore
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)
from qualsight.importers.owner_supplied import (
    OwnerSuppliedAdmission,
    OwnerSuppliedImporter,
    freeze_owner_pairs,
    prepared_owner_input_admissions,
    prepared_owner_protocols,
    recover_prepared_owner_input_admission,
)
from qualsight.security.purge import purge_targets


ROOT = Path(__file__).resolve().parents[2]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
REAL_PHOTO = Path(
    "/mnt/projects/datasets/lfw/lfw_funneled/Colin_Powell/Colin_Powell_0001.jpg"
)
REAL_PHOTO_SHA256 = "4aeed009e3eebba5e1a145156e0c98eda275cc00df74365c31200906d09d3595"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


@unittest.skipUnless(REAL_PHOTO.is_file(), "registered local LFW image is unavailable")
class OwnerPublicationIntegrationRealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-owner-integration-"))
        self.addCleanup(shutil.rmtree, self.data_dir, True)
        self.settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=self.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        payload = REAL_PHOTO.read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), REAL_PHOTO_SHA256)
        self.payload = payload

    def _project(self, name: str) -> str:
        with TestClient(create_app(self.settings), base_url="http://127.0.0.1") as client:
            created = client.post(
                "/api/projects",
                json={
                    "name": name,
                    "description": "Real LFW owner publication integration evidence.",
                    "purpose_scope": RESEARCH_SCOPE,
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            project_id = created.json()["project_id"]
            opened = client.post(f"/api/projects/{project_id}/open")
            self.assertEqual(opened.status_code, 200, opened.text)
            return project_id

    def _abort_owner_reference(self, project_id: str) -> Path:
        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            with sqlite3.connect(store.database_path) as connection:
                connection.execute(
                    "CREATE TRIGGER reject_owner_reference BEFORE INSERT ON owner_supplied_project_refs "
                    "BEGIN SELECT RAISE(ABORT, 'owner reference integration gate'); END"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                OwnerSuppliedAdmission(
                    store, self.data_dir, "local-user:owner-integration"
                ).admit_bytes(self.payload, REAL_PHOTO.name, project_id)
            receipts = prepared_owner_input_admissions(self.data_dir)
            self.assertEqual(len(receipts), 1)
            prepared = recover_prepared_owner_input_admission(
                receipts[0], data_dir=self.data_dir
            )
            self.assertEqual(prepared.project_id, project_id)
            self.assertEqual(prepared.target_path.read_bytes(), self.payload)
            self.assertEqual(store.owner_supplied_for_project(project_id), [])
            return receipts[0]
        finally:
            with sqlite3.connect(store.database_path) as connection:
                connection.execute("DROP TRIGGER IF EXISTS reject_owner_reference")
            store.close()

    def test_api_restart_recovers_owner_reference_and_finalizes_intent(self) -> None:
        project_id = self._project("owner restart recovery")
        receipt = self._abort_owner_reference(project_id)

        with TestClient(create_app(self.settings), base_url="http://127.0.0.1"):
            pass

        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            records = store.owner_supplied_for_project(project_id)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["sha256"], REAL_PHOTO_SHA256)
            self.assertFalse(receipt.exists())
            self.assertEqual(prepared_owner_input_admissions(self.data_dir), ())
        finally:
            store.close()

    def test_purge_targets_intent_but_retains_a_shared_content_address(self) -> None:
        retained_project = self._project("owner shared target retained")
        purged_project = self._project("owner failed reference purged")
        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            retained = OwnerSuppliedAdmission(
                store, self.data_dir, "local-user:owner-integration"
            ).admit_bytes(self.payload, REAL_PHOTO.name, retained_project)
        finally:
            store.close()
        receipt = self._abort_owner_reference(purged_project)

        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            targets = purge_targets(store, self.data_dir, purged_project)
        finally:
            store.close()
        self.assertIn(str(receipt), targets["files"])
        self.assertNotIn(retained.path, targets["files"])

    def test_api_restart_recovers_owner_protocol_and_purge_discovers_manifest(self) -> None:
        project_id = self._project("owner protocol restart recovery")
        owner_root = self.data_dir / "real-owner-protocol-source"
        for subject in ("George_W_Bush", "Tony_Blair"):
            subject_root = owner_root / subject
            subject_root.mkdir(parents=True)
            sources = sorted((Path("/mnt/projects/datasets/lfw/lfw_funneled") / subject).glob("*.jpg"))[:2]
            self.assertEqual(len(sources), 2)
            for source in sources:
                shutil.copyfile(source, subject_root / source.name)

        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            project = store.project(project_id)
            self.assertIsNotNone(project)
            imported = OwnerSuppliedImporter(
                store, self.data_dir, "local-user:owner-protocol-integration"
            ).import_directory(project, owner_root, "real owner protocol source")
            with sqlite3.connect(store.database_path) as connection:
                connection.execute(
                    "CREATE TRIGGER reject_owner_protocol_audit BEFORE INSERT ON audit_events "
                    "WHEN NEW.subject_type = 'VerificationProtocol' "
                    "BEGIN SELECT RAISE(ABORT, 'owner protocol integration gate'); END"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                freeze_owner_pairs(
                    store,
                    project,
                    imported.dataset,
                    data_dir=self.data_dir,
                    actor_id="local-user:owner-protocol-integration",
                    seed=29,
                    max_pairs=20_000,
                )
            receipts = prepared_owner_protocols(self.data_dir)
            self.assertEqual(len(receipts), 1)
        finally:
            with sqlite3.connect(store.database_path) as connection:
                connection.execute("DROP TRIGGER IF EXISTS reject_owner_protocol_audit")
            store.close()

        with TestClient(create_app(self.settings), base_url="http://127.0.0.1"):
            pass

        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            protocols = store.protocols_for_project(project_id)
            self.assertEqual(len(protocols), 1)
            self.assertEqual(protocols[0].dataset_id, imported.dataset.dataset_id)
            self.assertEqual(prepared_owner_protocols(self.data_dir), ())
            targets = purge_targets(store, self.data_dir, project_id)
            self.assertIn(protocols[0].pair_manifest.path, targets["files"])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
