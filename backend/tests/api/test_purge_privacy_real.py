"""Public purge receipts protect local paths while private evidence stays complete.

Every biometric byte used here is an unmodified photograph from the registered
local LFW resource.  The failure case uses real filesystem permissions to
produce a real ``PermissionError``; it does not replace the purge implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
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
from qualsight.security.purge import (
    public_purge_record,
    purge_project,
    purge_targets,
    reconcile_incomplete_purges,
)


ROOT = Path(__file__).resolve().parents[3]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"
ABEL_LEFT = Path(
    "/mnt/projects/datasets/lfw/lfw_funneled/Abel_Pacheco/Abel_Pacheco_0001.jpg"
)
ABEL_RIGHT = Path(
    "/mnt/projects/datasets/lfw/lfw_funneled/Abel_Pacheco/Abel_Pacheco_0004.jpg"
)
ABEL_LEFT_SHA256 = "8b6fff626f3d778d34027e19fc88c521a9e1307cf735b22a1406c17e55e8a967"
ABEL_RIGHT_SHA256 = "8050daa859e7e05aedd5c7ec3851e5d5bddcb2b9797d23958664ec2958a230ae"


class PurgePrivacyRealTests(unittest.TestCase):
    def setUp(self) -> None:
        for path, expected_sha256 in (
            (ABEL_LEFT, ABEL_LEFT_SHA256),
            (ABEL_RIGHT, ABEL_RIGHT_SHA256),
        ):
            self.assertTrue(path.is_file(), path)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected_sha256)
        self.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-purge-privacy-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=self.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        self.client = TestClient(create_app(settings), base_url="http://127.0.0.1")
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def _project_with_real_photo(self, name: str, photo: Path, expected_sha256: str) -> tuple[str, str]:
        project = self.client.post(
            "/api/projects",
            json={
                "name": name,
                "description": "Real LFW purge privacy verification.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["project_id"]
        opened = self.client.post(f"/api/projects/{project_id}/open")
        self.assertEqual(opened.status_code, 200, opened.text)
        uploaded = self.client.post(
            "/api/quality/assess-upload",
            params={
                "project_id": project_id,
                "purpose_scope": RESEARCH_SCOPE,
                "filename": photo.name,
            },
            content=photo.read_bytes(),
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        self.assertEqual(uploaded.json()["image_sha256"], expected_sha256)
        return project_id, uploaded.json()["sample_id"]

    def _recorded_sample_path(self, sample_id: str) -> Path:
        with sqlite3.connect(self.data_dir / "qualsight.sqlite3") as connection:
            row = connection.execute(
                "SELECT source_path FROM samples WHERE sample_id = ?", (sample_id,)
            ).fetchone()
        self.assertIsNotNone(row)
        return Path(str(row[0]))

    def _project_job_parquet_path(self, project_id: str) -> Path:
        with sqlite3.connect(self.data_dir / "qualsight.sqlite3") as connection:
            row = connection.execute(
                "SELECT parquet_path FROM jobs WHERE project_id = ? "
                "AND parquet_path IS NOT NULL ORDER BY started_at LIMIT 1",
                (project_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        return Path(str(row[0]))

    def _project_metadata_counts(self, project_id: str) -> dict[str, int]:
        with sqlite3.connect(self.data_dir / "qualsight.sqlite3") as connection:
            datasets = int(
                connection.execute(
                    "SELECT COUNT(*) FROM datasets WHERE project_id = ?", (project_id,)
                ).fetchone()[0]
            )
            samples = int(
                connection.execute(
                    "SELECT COUNT(*) FROM samples WHERE dataset_id IN "
                    "(SELECT dataset_id FROM datasets WHERE project_id = ?)",
                    (project_id,),
                ).fetchone()[0]
            )
            runs = int(
                connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE project_id = ?", (project_id,)
                ).fetchone()[0]
            )
        return {"datasets": datasets, "samples": samples, "runs": runs}

    def _assert_public_payload_is_path_free(self, payload: object) -> None:
        encoded = json.dumps(payload, sort_keys=True)
        self.assertNotIn(str(self.data_dir), encoded)
        self.assertNotIn("Permission denied", encoded)
        self.assertNotIn("Errno", encoded)

        def visit(value: object) -> None:
            if isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, str):
                self.assertFalse(Path(value).is_absolute(), value)

        visit(payload)

    @staticmethod
    def _private_evidence_sha256(record: dict[str, object]) -> str:
        canonical = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def test_scope_success_history_and_failure_receipts_are_path_free_but_auditable(self) -> None:
        project_id, _ = self._project_with_real_photo(
            "Path-free purge success", ABEL_LEFT, ABEL_LEFT_SHA256
        )
        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            private_targets = purge_targets(store, self.data_dir, project_id)
        finally:
            store.close()
        expected_target_counts = {
            "files": len(private_targets["files"]),
            "directories": len(private_targets["directories"]),
        }
        self.assertGreater(sum(expected_target_counts.values()), 0)

        scope_response = self.client.get(f"/api/projects/{project_id}/purge-scope")
        self.assertEqual(scope_response.status_code, 200, scope_response.text)
        scope = scope_response.json()
        self.assertEqual(scope["target_counts"], expected_target_counts)
        self.assertNotIn("targets", scope)
        self._assert_public_payload_is_path_free(scope)

        purge_response = self.client.post(
            f"/api/projects/{project_id}/purge", json={"confirm": project_id}
        )
        self.assertEqual(purge_response.status_code, 201, purge_response.text)
        receipt = purge_response.json()
        self.assertTrue(receipt["verified"], receipt)
        self.assertEqual(receipt["target_counts"], expected_target_counts)
        self.assertEqual(receipt["outcome_counts"]["failed"], 0)
        self.assertEqual(receipt["outcome_counts"]["remaining_after_purge"], 0)
        for private_key in (
            "targets",
            "deleted",
            "already_absent",
            "failed",
            "remaining_after_purge",
            "os_pid",
        ):
            self.assertNotIn(private_key, receipt)
        self._assert_public_payload_is_path_free(receipt)

        stored = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            private_records = stored.purges(project_id=project_id)
        finally:
            stored.close()
        self.assertEqual(len(private_records), 1)
        private_record = private_records[0]
        self.assertEqual(private_record["targets"], private_targets)
        self.assertTrue(any(str(self.data_dir) in path for path in private_record["deleted"]))
        self.assertEqual(
            receipt["evidence_sha256"], self._private_evidence_sha256(private_record)
        )
        self.assertEqual(
            receipt["outcome_counts"],
            {
                "deleted": len(private_record["deleted"]),
                "already_absent": len(private_record["already_absent"]),
                "reconciled_after_deletion_intent": len(
                    private_record["reconciled_after_deletion_intent"]
                ),
                "failed": len(private_record["failed"]),
                "remaining_after_purge": len(private_record["remaining_after_purge"]),
            },
        )

        history_response = self.client.get(f"/api/projects/{project_id}/purges")
        self.assertEqual(history_response.status_code, 200, history_response.text)
        history = history_response.json()
        self.assertEqual(history, [receipt])
        self._assert_public_payload_is_path_free(history)

        failed_project_id, failed_sample_id = self._project_with_real_photo(
            "Path-free purge failure", ABEL_RIGHT, ABEL_RIGHT_SHA256
        )
        protected_file = self._recorded_sample_path(failed_sample_id)
        protected_parent = protected_file.parent
        original_mode = protected_parent.stat().st_mode & 0o777
        os.chmod(protected_parent, 0o500)
        try:
            failed_response = self.client.post(
                f"/api/projects/{failed_project_id}/purge",
                json={"confirm": failed_project_id},
            )
        finally:
            os.chmod(protected_parent, original_mode)
        self.assertEqual(failed_response.status_code, 409, failed_response.text)
        failure_receipt = failed_response.json()["detail"]
        self.assertEqual(failure_receipt["project_id"], failed_project_id)
        self.assertFalse(failure_receipt["verified"])
        self.assertGreater(failure_receipt["outcome_counts"]["failed"], 0)
        self.assertGreater(
            failure_receipt["outcome_counts"]["remaining_after_purge"], 0
        )
        self.assertRegex(failure_receipt["evidence_sha256"], r"^[0-9a-f]{64}$")
        self._assert_public_payload_is_path_free(failure_receipt)

        stored_failure = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            private_failure_record = stored_failure.purge_evidence(
                failure_receipt["evidence_sha256"]
            )
        finally:
            stored_failure.close()
        self.assertIsNotNone(private_failure_record)
        self.assertEqual(private_failure_record["purge_id"], failure_receipt["purge_id"])
        self.assertEqual(private_failure_record["project_id"], failed_project_id)
        self.assertFalse(private_failure_record["verified"])
        self.assertEqual(
            self._private_evidence_sha256(private_failure_record),
            failure_receipt["evidence_sha256"],
        )
        self.assertIn(str(protected_file), json.dumps(private_failure_record))

        with sqlite3.connect(self.data_dir / "qualsight.sqlite3") as connection:
            journal = connection.execute(
                "SELECT targets_json, progress_json, error FROM purge_journal "
                "WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
                (failed_project_id,),
            ).fetchone()
        self.assertIsNotNone(journal)
        private_failure = "".join(str(value) for value in journal)
        self.assertIn(str(protected_file), private_failure)
        self.assertIn("Permission denied", private_failure)

        readiness_during_failed_purge = self.client.get("/api/ready")
        self.assertEqual(
            readiness_during_failed_purge.status_code,
            503,
            readiness_during_failed_purge.text,
        )
        self.assertEqual(
            readiness_during_failed_purge.json()["checks"]["database"][
                "unresolved_purges"
            ],
            1,
        )

        # A failed purge is a durable admission barrier, not merely an in-memory
        # request marker.  This real owner-directory mutation must remain refused
        # until the same purge journal is successfully reconciled.
        owner_root = Path(tempfile.mkdtemp(prefix="qualsight-purge-reconcile-owner-"))
        self.addCleanup(shutil.rmtree, owner_root, True)
        owner_subject = owner_root / "Abel_Pacheco"
        owner_subject.mkdir()
        for source, expected_sha256 in (
            (ABEL_LEFT, ABEL_LEFT_SHA256),
            (ABEL_RIGHT, ABEL_RIGHT_SHA256),
        ):
            copied = owner_subject / source.name
            shutil.copyfile(source, copied)
            self.assertEqual(
                hashlib.sha256(copied.read_bytes()).hexdigest(), expected_sha256
            )
        with sqlite3.connect(self.data_dir / "qualsight.sqlite3") as connection:
            datasets_before_refusal = int(
                connection.execute(
                    "SELECT COUNT(*) FROM datasets WHERE project_id = ?",
                    (failed_project_id,),
                ).fetchone()[0]
            )
        refused_mutation = self.client.post(
            f"/api/projects/{failed_project_id}/datasets/import-owner-directory",
            json={
                "root": str(owner_root),
                "name": "Real LFW admission after failed purge",
            },
        )
        self.assertEqual(refused_mutation.status_code, 409, refused_mutation.text)
        self.assertEqual(
            refused_mutation.json()["detail"],
            "project purge is in progress; new work is refused",
        )
        with sqlite3.connect(self.data_dir / "qualsight.sqlite3") as connection:
            self.assertEqual(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM datasets WHERE project_id = ?",
                        (failed_project_id,),
                    ).fetchone()[0]
                ),
                datasets_before_refusal,
            )

        reconciled_response = self.client.post(
            f"/api/projects/{failed_project_id}/purge",
            json={"confirm": failed_project_id},
        )
        self.assertEqual(reconciled_response.status_code, 201, reconciled_response.text)
        self.assertTrue(reconciled_response.json()["verified"])
        readiness_after_reconciliation = self.client.get("/api/ready")
        self.assertEqual(
            readiness_after_reconciliation.status_code,
            200,
            readiness_after_reconciliation.text,
        )
        self.assertEqual(
            readiness_after_reconciliation.json()["checks"]["database"][
                "unresolved_purges"
            ],
            0,
        )
        admitted_after_reconciliation = self.client.post(
            f"/api/projects/{failed_project_id}/datasets/import-owner-directory",
            json={
                "root": str(owner_root),
                "name": "Real LFW admission after purge reconciliation",
            },
        )
        self.assertEqual(
            admitted_after_reconciliation.status_code,
            201,
            admitted_after_reconciliation.text,
        )

    def test_readiness_dependency_failures_are_path_free_machine_codes(self) -> None:
        unavailable_data_dir = self.data_dir.with_name(
            f"{self.data_dir.name}-temporarily-unavailable"
        )
        self.data_dir.rename(unavailable_data_dir)
        try:
            storage_failure = self.client.get("/api/ready")
        finally:
            unavailable_data_dir.rename(self.data_dir)
        self.assertEqual(storage_failure.status_code, 503, storage_failure.text)
        self.assertEqual(
            storage_failure.json()["checks"]["storage"],
            {"ok": False, "error_code": "STORAGE_UNAVAILABLE"},
        )
        self._assert_public_payload_is_path_free(storage_failure.json())

        with sqlite3.connect(self.data_dir / "qualsight.sqlite3") as connection:
            connection.execute(
                "ALTER TABLE operations RENAME TO operations_temporarily_unavailable"
            )
        try:
            database_failure = self.client.get("/api/ready")
        finally:
            with sqlite3.connect(self.data_dir / "qualsight.sqlite3") as connection:
                connection.execute(
                    "ALTER TABLE operations_temporarily_unavailable "
                    "RENAME TO operations"
                )
        self.assertEqual(database_failure.status_code, 503, database_failure.text)
        self.assertEqual(
            database_failure.json()["checks"]["database"],
            {"ok": False, "error_code": "DATABASE_UNAVAILABLE"},
        )
        self._assert_public_payload_is_path_free(database_failure.json())

    def test_transient_real_metadata_deletion_failure_retries_same_purge_to_completion(self) -> None:
        project_id, _ = self._project_with_real_photo(
            "Transient metadata deletion retry", ABEL_LEFT, ABEL_LEFT_SHA256
        )
        before = self._project_metadata_counts(project_id)
        self.assertGreater(before["datasets"], 0)
        self.assertGreater(before["samples"], 0)
        database_path = self.data_dir / "qualsight.sqlite3"
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "CREATE TRIGGER fail_one_real_metadata_delete "
                "BEFORE DELETE ON datasets BEGIN "
                "SELECT RAISE(ABORT, 'transient metadata deletion failure'); END"
            )

        store = RunStore(database_path)
        try:
            first = purge_project(
                store,
                data_dir=self.data_dir,
                project_id=project_id,
                actor="local-purge-retry-test",
                confirm=project_id,
            )
            self.assertFalse(first["verified"], first)
            self.assertEqual(self._project_metadata_counts(project_id), before)
            with sqlite3.connect(database_path) as connection:
                connection.execute("DROP TRIGGER fail_one_real_metadata_delete")
            retried = purge_project(
                store,
                data_dir=self.data_dir,
                project_id=project_id,
                actor="local-purge-retry-test",
                confirm=project_id,
            )
        finally:
            with sqlite3.connect(database_path) as connection:
                connection.execute("DROP TRIGGER IF EXISTS fail_one_real_metadata_delete")
            store.close()

        self.assertTrue(retried["verified"], retried)
        self.assertEqual(retried["purge_id"], first["purge_id"])
        self.assertEqual(retried["metadata"]["removed"]["datasets"], before["datasets"])
        self.assertEqual(retried["metadata"]["removed"]["samples"], before["samples"])
        self.assertEqual(retried["metadata"]["removed"]["runs"], before["runs"])
        self.assertEqual(
            self._project_metadata_counts(project_id),
            {"datasets": 0, "samples": 0, "runs": 0},
        )

    def test_metadata_deletion_and_journal_accounting_are_one_real_sqlite_transaction(self) -> None:
        project_id, _ = self._project_with_real_photo(
            "Atomic metadata purge accounting", ABEL_RIGHT, ABEL_RIGHT_SHA256
        )
        before = self._project_metadata_counts(project_id)
        self.assertGreater(before["datasets"], 0)
        self.assertGreater(before["samples"], 0)
        database_path = self.data_dir / "qualsight.sqlite3"
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "CREATE TRIGGER interrupt_metadata_journal_accounting "
                "BEFORE UPDATE OF state, progress_json ON purge_journal "
                "WHEN NEW.state = 'METADATA_VERIFIED' BEGIN "
                "SELECT RAISE(ABORT, 'metadata journal accounting interruption'); END"
            )

        store = RunStore(database_path)
        try:
            interrupted = purge_project(
                store,
                data_dir=self.data_dir,
                project_id=project_id,
                actor="local-purge-atomicity-test",
                confirm=project_id,
            )
            self.assertFalse(interrupted["verified"], interrupted)
            self.assertEqual(
                self._project_metadata_counts(project_id),
                before,
                "metadata deletion committed without its durable journal accounting",
            )
            with sqlite3.connect(database_path) as connection:
                connection.execute("DROP TRIGGER interrupt_metadata_journal_accounting")
            reconciled = purge_project(
                store,
                data_dir=self.data_dir,
                project_id=project_id,
                actor="local-purge-atomicity-test",
                confirm=project_id,
            )
        finally:
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "DROP TRIGGER IF EXISTS interrupt_metadata_journal_accounting"
                )
            store.close()

        self.assertTrue(reconciled["verified"], reconciled)
        self.assertEqual(reconciled["purge_id"], interrupted["purge_id"])
        self.assertEqual(reconciled["metadata"]["removed"]["datasets"], before["datasets"])
        self.assertEqual(reconciled["metadata"]["removed"]["samples"], before["samples"])
        self.assertEqual(reconciled["metadata"]["removed"]["runs"], before["runs"])

    def test_restart_distinguishes_post_intent_absence_from_preexisting_absence(self) -> None:
        project_id, sample_id = self._project_with_real_photo(
            "Crash-reconciled real deletion intent", ABEL_LEFT, ABEL_LEFT_SHA256
        )
        managed_photo = self._recorded_sample_path(sample_id)
        preexisting_absence = self._project_job_parquet_path(project_id)
        self.assertTrue(managed_photo.is_file(), managed_photo)
        self.assertEqual(
            hashlib.sha256(managed_photo.read_bytes()).hexdigest(), ABEL_LEFT_SHA256
        )
        self.assertTrue(preexisting_absence.is_file(), preexisting_absence)
        preexisting_absence.unlink()

        database_path = self.data_dir / "qualsight.sqlite3"
        quoted_photo = str(managed_photo).replace("'", "''")
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "CREATE TRIGGER interrupt_after_real_managed_photo_unlink "
                "BEFORE UPDATE OF progress_json ON purge_journal "
                "WHEN EXISTS ("
                "SELECT 1 FROM json_each(NEW.progress_json, '$.deleted') "
                f"WHERE value = '{quoted_photo}'"
                ") AND NOT EXISTS ("
                "SELECT 1 FROM json_each(OLD.progress_json, '$.deleted') "
                f"WHERE value = '{quoted_photo}'"
                ") BEGIN "
                "SELECT RAISE(ABORT, 'process loss after real managed photo unlink'); END"
            )

        store = RunStore(database_path)
        interrupted_progress: dict[str, object]
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "process loss after real managed photo unlink",
            ):
                purge_project(
                    store,
                    data_dir=self.data_dir,
                    project_id=project_id,
                    actor="local-purge-process-loss-test",
                    confirm=project_id,
                )
            self.assertFalse(managed_photo.exists())
            journal = store.active_purge_journal(project_id)
            self.assertIsNotNone(journal)
            interrupted_progress = json.loads(str(journal["progress_json"]))
        finally:
            store.close()
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "DROP TRIGGER IF EXISTS interrupt_after_real_managed_photo_unlink"
                )

        restarted_store = RunStore(database_path)
        try:
            reconciled = reconcile_incomplete_purges(
                restarted_store,
                data_dir=self.data_dir,
            )
        finally:
            restarted_store.close()

        self.assertEqual(len(reconciled), 1)
        receipt = reconciled[0]
        self.assertTrue(receipt["verified"], receipt)
        self.assertIn(str(managed_photo), interrupted_progress["deletion_intents"])
        self.assertIn(
            str(managed_photo), receipt["reconciled_after_deletion_intent"]
        )
        self.assertNotIn(str(managed_photo), receipt["deleted"])
        self.assertNotIn(str(managed_photo), receipt["already_absent"])
        self.assertIn(str(preexisting_absence), receipt["already_absent"])
        self.assertNotIn(
            str(preexisting_absence), receipt["reconciled_after_deletion_intent"]
        )
        public_receipt = public_purge_record(receipt)
        self.assertEqual(
            public_receipt["outcome_counts"]["reconciled_after_deletion_intent"],
            1,
        )
        self.assertNotIn(str(managed_photo), json.dumps(public_receipt, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
