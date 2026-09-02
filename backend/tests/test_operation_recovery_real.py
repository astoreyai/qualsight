"""Durable operation recovery over a project backed by the registered real LFW dataset."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.api.store import ImmutableRecordConflict, RunStore
from qualsight.engines.ofiq.adapter import content_id
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID
from qualsight.security.purge import purge_targets
from qualsight.provenance import application_version


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

    def _downgrade_real_operation_rows_to_v4(self) -> Path:
        database = self.data_dir / "qualsight.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP INDEX operations_by_project")
            connection.execute("DROP INDEX operations_by_request_attempt")
            connection.execute("ALTER TABLE operations RENAME TO operations_v5")
            connection.execute(
                "CREATE TABLE operations ("
                "operation_id TEXT PRIMARY KEY, operation_kind TEXT NOT NULL, "
                "project_id TEXT NOT NULL REFERENCES projects(project_id), "
                "state TEXT NOT NULL CHECK (state IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'INTERRUPTED')), "
                "accepted_at TEXT NOT NULL, started_at TEXT, ended_at TEXT, "
                "request_json TEXT NOT NULL, output_path TEXT, error TEXT)"
            )
            connection.execute(
                "INSERT INTO operations "
                "(operation_id, operation_kind, project_id, state, accepted_at, started_at, ended_at, request_json, output_path, error) "
                "SELECT operation_id, operation_kind, project_id, state, accepted_at, started_at, ended_at, request_json, output_path, error "
                "FROM operations_v5"
            )
            connection.execute("DROP TABLE operations_v5")
            connection.execute(
                "CREATE INDEX operations_by_project ON operations(project_id, accepted_at)"
            )
            connection.execute("DELETE FROM schema_migrations WHERE version = 5")
            connection.execute("PRAGMA user_version=4")
            connection.commit()
        finally:
            connection.close()
        return database

    def test_interrupted_request_can_be_explicitly_readmitted_as_a_distinct_attempt(self) -> None:
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

            retry, created_again = restarted.admit_operation(
                operation_id=operation_id,
                operation_kind="QUALITY_BATCH",
                project_id=self.project_id,
                request=request,
                output_path=self.data_dir / "jobs" / operation_id.replace(":", "_"),
                retry_interrupted=True,
            )
            self.assertTrue(created_again)
            self.assertNotEqual(retry["operation_id"], operation_id)
            self.assertEqual(retry["request_id"], operation_id)
            self.assertEqual(retry["attempt"], 2)
            self.assertEqual(retry["supersedes_operation_id"], operation_id)
            self.assertEqual(retry["state"], "QUEUED")

            repeated, repeated_created = restarted.admit_operation(
                operation_id=operation_id,
                operation_kind="QUALITY_BATCH",
                project_id=self.project_id,
                request=request,
                output_path=self.data_dir / "jobs" / operation_id.replace(":", "_"),
            )
            self.assertFalse(repeated_created)
            self.assertEqual(repeated["operation_id"], retry["operation_id"])
            self.assertEqual(repeated["attempt"], 2)

            restarted.start_operation(retry["operation_id"])
            restarted.finish_operation(retry["operation_id"], state="COMPLETED")
            self.assertEqual(restarted.operation(operation_id)["state"], "INTERRUPTED")
            self.assertEqual(restarted.operation(retry["operation_id"])["state"], "COMPLETED")
        finally:
            restarted.close()

    def test_v4_operation_rows_are_preflight_snapshotted_and_migrated_compatibly(self) -> None:
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

        database = self._downgrade_real_operation_rows_to_v4()
        preflight = database.with_name(
            f"{database.name}.pre-migration-v4-to-v8.snapshot.sqlite3"
        )
        migrated = RunStore(database)
        try:
            operation = migrated.operation(operation_id)
            self.assertEqual(operation["request_id"], operation_id)
            self.assertEqual(operation["attempt"], 1)
            self.assertIsNone(operation["supersedes_operation_id"])
        finally:
            migrated.close()
        self.assertTrue(preflight.is_file())
        connection = sqlite3.connect(f"file:{preflight}?mode=ro", uri=True)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(
                connection.execute(
                    "SELECT operation_id FROM operations WHERE operation_id = ?", (operation_id,)
                ).fetchone(),
                (operation_id,),
            )
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            connection.close()

    def test_failed_v4_migration_restores_the_verified_preflight_snapshot(self) -> None:
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
        database = self._downgrade_real_operation_rows_to_v4()
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "CREATE TRIGGER fail_operation_identity_migration "
                "BEFORE UPDATE OF request_id ON operations BEGIN "
                "SELECT RAISE(ABORT, 'operation identity migration rollback gate'); END"
            )
            connection.commit()
        finally:
            connection.close()

        preflight = database.with_name(
            f"{database.name}.pre-migration-v4-to-v8.snapshot.sqlite3"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "rollback gate"):
            RunStore(database)
        self.assertEqual(database.read_bytes(), preflight.read_bytes())
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertNotIn(
                "request_id",
                {row[1] for row in connection.execute("PRAGMA table_info(operations)").fetchall()},
            )
            self.assertEqual(
                connection.execute(
                    "SELECT operation_id FROM operations WHERE operation_id = ?", (operation_id,)
                ).fetchone(),
                (operation_id,),
            )
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            connection.close()

    def test_failed_migration_preserves_prior_snapshot_and_allows_a_repaired_retry(self) -> None:
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
        database = self._downgrade_real_operation_rows_to_v4()
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "CREATE TRIGGER fail_operation_identity_migration "
                "BEFORE UPDATE OF request_id ON operations BEGIN "
                "SELECT RAISE(ABORT, 'operation identity migration retry gate'); END"
            )
            connection.commit()
        finally:
            connection.close()

        canonical = database.with_name(
            f"{database.name}.pre-migration-v4-to-v8.snapshot.sqlite3"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "retry gate"):
            RunStore(database)
        canonical_bytes = canonical.read_bytes()

        connection = sqlite3.connect(database)
        try:
            connection.execute("DROP TRIGGER fail_operation_identity_migration")
            connection.commit()
        finally:
            connection.close()

        retried = RunStore(database)
        try:
            self.assertEqual(retried.operation(operation_id)["request_id"], operation_id)
        finally:
            retried.close()
        self.assertEqual(canonical.read_bytes(), canonical_bytes)
        retry_snapshots = sorted(
            database.parent.glob(
                f"{database.name}.pre-migration-v4-to-v8.source-*.snapshot.sqlite3"
            )
        )
        self.assertEqual(len(retry_snapshots), 1)
        self.assertNotEqual(retry_snapshots[0].read_bytes(), canonical_bytes)

    def test_incomplete_target_ledger_is_snapshotted_and_restored_before_schema_repair(self) -> None:
        database = self.data_dir / "qualsight.sqlite3"
        connection = sqlite3.connect(database)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 8)
            self.assertEqual(
                [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")],
                list(range(1, 9)),
            )
            connection.execute("DROP TABLE direct_pair_comparisons")
            connection.execute("DELETE FROM schema_migrations WHERE version = 8")
            connection.execute(
                "CREATE TRIGGER refuse_v8_history BEFORE INSERT ON schema_migrations "
                "WHEN NEW.version = 8 BEGIN SELECT RAISE(ABORT, 'v8 ledger rollback gate'); END"
            )
            connection.commit()
        finally:
            connection.close()

        preflight = database.with_name(
            f"{database.name}.pre-migration-v8-to-v8.snapshot.sqlite3"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "v8 ledger rollback gate"):
            RunStore(database)
        self.assertTrue(preflight.is_file())
        self.assertEqual(database.read_bytes(), preflight.read_bytes())
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 8)
            self.assertEqual(
                [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")],
                list(range(1, 8)),
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'direct_pair_comparisons'"
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "SELECT project_id FROM projects WHERE project_id = ?", (self.project_id,)
                ).fetchone(),
                (self.project_id,),
            )
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            connection.close()

        connection = sqlite3.connect(database)
        try:
            connection.execute("DROP TRIGGER refuse_v8_history")
            connection.commit()
        finally:
            connection.close()
        repaired = RunStore(database)
        try:
            self.assertIsNotNone(repaired.project(self.project_id))
            self.assertIsNotNone(repaired.dataset(self.dataset_id))
        finally:
            repaired.close()
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 8)
            self.assertEqual(
                [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")],
                list(range(1, 9)),
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'direct_pair_comparisons'"
                ).fetchone()
            )
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            connection.close()

    def test_fastapi_advertises_the_exact_beta_package_version(self) -> None:
        app = create_app(self.settings)
        self.assertEqual(
            app.version,
            application_version(ROOT / "backend" / "pyproject.toml"),
        )

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
            status = restarted.get(f"/api/projects/{self.project_id}/operations/{operation_id}")
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["state"], "INTERRUPTED")
            project_operations = restarted.get(f"/api/projects/{self.project_id}/operations")
            self.assertEqual(project_operations.status_code, 200, project_operations.text)
            self.assertEqual(
                [row["operation_id"] for row in project_operations.json()],
                [operation_id],
            )

    def test_project_jobs_projects_one_result_for_multiple_interrupted_attempts(self) -> None:
        fixture = json.loads(
            (ROOT / "resources" / "manifests" / "lfw-subset-fixture-v1.json").read_text(
                encoding="utf-8"
            )
        )
        request = {
            "selection": {
                "kind": "subset_fixture",
                "sample_ids": [],
                "subject_id": None,
                "limit": None,
            },
            "chunks": 1,
            "dataset_id": self.dataset_id,
            "purpose_scope": RESEARCH_SCOPE,
            "execution": "host",
            "sample_count": len(fixture["images"]),
            "selection_rule": (
                f"registered subset fixture {fixture['fixture_id']}: {fixture['selection_rule']}"
            ),
        }
        identity = json.dumps(
            {"kind": "QUALITY_BATCH", "project_id": self.project_id, "request": request},
            sort_keys=True,
            separators=(",", ":"),
        )
        operation_id = f"operation:sha256:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"
        result_id = f"job:sha256:{hashlib.sha256((identity + ':result').encode('utf-8')).hexdigest()}"
        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            first, created = store.admit_operation(
                operation_id=operation_id,
                operation_kind="QUALITY_BATCH",
                project_id=self.project_id,
                request=request,
                output_path=self.data_dir / "jobs" / result_id.replace(":", "_"),
                result_id=result_id,
            )
            self.assertTrue(created)
            store.finish_operation(
                first["operation_id"],
                state="INTERRUPTED",
                error="real quality batch process ended before recording a result",
            )
            retry, retried = store.admit_operation(
                operation_id=operation_id,
                operation_kind="QUALITY_BATCH",
                project_id=self.project_id,
                request=request,
                output_path=self.data_dir / "jobs" / result_id.replace(":", "_"),
                retry_interrupted=True,
                result_id=result_id,
            )
            self.assertTrue(retried)
            self.assertEqual(retry["attempt"], 2)
        finally:
            store.close()

        with TestClient(create_app(self.settings), base_url="http://127.0.0.1") as client:
            response = client.get(f"/api/projects/{self.project_id}/jobs")
            self.assertEqual(response.status_code, 200, response.text)
            projected = [row for row in response.json() if row["job_id"] == result_id]
            self.assertEqual(len(projected), 1)
            self.assertEqual(projected[0]["state"], "INTERRUPTED")
            self.assertFalse(projected[0]["quality_result_available"])
            stable_status = client.get(
                f"/api/projects/{self.project_id}/jobs/{result_id}"
            )
            self.assertEqual(stable_status.status_code, 200, stable_status.text)
            self.assertEqual(stable_status.json()["job_id"], result_id)
            self.assertEqual(stable_status.json()["state"], "INTERRUPTED")
            self.assertFalse(stable_status.json()["quality_result_available"])

    def test_api_explicitly_retries_and_executes_an_interrupted_real_quality_batch(self) -> None:
        fixture = json.loads(
            (ROOT / "resources" / "manifests" / "lfw-subset-fixture-v1.json").read_text(
                encoding="utf-8"
            )
        )
        request = {
            "selection": {"kind": "subset_fixture", "sample_ids": [], "subject_id": None, "limit": None},
            "chunks": 1,
            "purpose_scope": RESEARCH_SCOPE,
            "execution": "host",
            "dataset_id": self.dataset_id,
            "sample_count": len(fixture["images"]),
            "selection_rule": (
                f"registered subset fixture {fixture['fixture_id']}: {fixture['selection_rule']}"
            ),
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

        with TestClient(create_app(self.settings), base_url="http://127.0.0.1") as client:
            interrupted = client.get(f"/api/projects/{self.project_id}/operations/{operation_id}")
            self.assertEqual(interrupted.status_code, 200, interrupted.text)
            self.assertEqual(interrupted.json()["state"], "INTERRUPTED")

            response = client.post(f"/api/projects/{self.project_id}/operations/{operation_id}/retry")
            self.assertEqual(response.status_code, 202, response.text)
            retry = response.json()["operation"]
            self.assertEqual(retry["request_id"], operation_id)
            self.assertEqual(retry["attempt"], 2)
            self.assertEqual(retry["supersedes_operation_id"], operation_id)
            self.assertNotEqual(retry["operation_id"], operation_id)
            self.assertEqual(response.json()["execution"]["job_id"], response.json()["result_id"])
            self.assertEqual(retry["result_id"], response.json()["result_id"])

            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                status = client.get(f"/api/projects/{self.project_id}/operations/{retry['operation_id']}")
                self.assertEqual(status.status_code, 200, status.text)
                if status.json()["state"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                    break
                time.sleep(0.25)
            else:
                self.fail(f"retried real quality operation did not terminate: {status.text}")
            self.assertEqual(status.json()["state"], "COMPLETED", status.text)
            self.assertEqual(
                client.get(f"/api/projects/{self.project_id}/operations/{operation_id}").json()["state"],
                "INTERRUPTED",
            )
            jobs = client.get(f"/api/projects/{self.project_id}/jobs")
            self.assertEqual(jobs.status_code, 200, jobs.text)
            projected_ids = [item["job_id"] for item in jobs.json()]
            self.assertEqual(projected_ids.count(response.json()["result_id"]), 1)
            self.assertNotIn(retry["operation_id"], projected_ids)
            self.assertEqual(
                client.get(
                    f"/api/projects/{self.project_id}/jobs/{retry['operation_id']}"
                ).status_code,
                404,
            )

    def test_api_retries_an_interrupted_real_matcher_matrix_request(self) -> None:
        with TestClient(create_app(self.settings), base_url="http://127.0.0.1") as client:
            frozen = client.post(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/protocols/freeze-lfw-view2"
            )
            self.assertEqual(frozen.status_code, 201, frozen.text)
            protocol_id = frozen.json()["protocol_id"]
            pulled = client.post(
                "/api/matcher-assets/adaface-ir101-local-candidate/pull-sources"
            )
            self.assertEqual(pulled.status_code, 200, pulled.text)

        fixture = json.loads(
            (ROOT / "resources" / "manifests" / "lfw-subset-fixture-v1.json").read_text(
                encoding="utf-8"
            )
        )
        request = {
            "dataset_id": self.dataset_id,
            "protocol_id": protocol_id,
            "fixture_pairs_only": True,
            "leaves": "QS-C22.AS",
            "leaf_ids": ["QS-C22.AS"],
            "pair_count": len(fixture["pairs"]),
            "sample_count": len(fixture["images"]),
            "expected_leaf_count": 1,
            "selection_rule": (
                "the registered subset-fixture pairs of the frozen protocol "
                "(leaf selection ['QS-C22.AS']: the matcher x condition cross "
                "product of the named leaves)"
            ),
        }
        stored = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            protocol = stored.protocol(protocol_id)
            assert protocol is not None
            operation_id = content_id(
                "matrix",
                self.project_id,
                protocol_id,
                protocol.pairs_sha256,
                json.dumps(["QS-C22.AS"], separators=(",", ":")),
                request["selection_rule"],
            )
        finally:
            stored.close()
        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            operation, created = store.admit_operation(
                operation_id=operation_id,
                operation_kind="MATCHER_MATRIX",
                project_id=self.project_id,
                request=request,
                output_path=self.data_dir / "matrix" / operation_id.replace(":", "_"),
            )
            self.assertTrue(created)
            self.assertEqual(operation["state"], "QUEUED")
        finally:
            store.close()

        with TestClient(create_app(self.settings), base_url="http://127.0.0.1") as client:
            interrupted = client.get(f"/api/projects/{self.project_id}/operations/{operation_id}")
            self.assertEqual(interrupted.status_code, 200, interrupted.text)
            self.assertEqual(interrupted.json()["state"], "INTERRUPTED")

            retried = client.post(
                f"/api/projects/{self.project_id}/operations/{operation_id}/retry"
            )
            self.assertEqual(retried.status_code, 202, retried.text)
            deadline = time.monotonic() + 300
            status: dict = {}
            while time.monotonic() < deadline:
                response = client.get(
                    f"/api/projects/{self.project_id}/matrices/{operation_id}"
                )
                self.assertEqual(response.status_code, 200, response.text)
                status = response.json()
                if status["progress"]["phase"] in {"completed", "failed"}:
                    break
                time.sleep(0.05)
            self.assertEqual(status.get("progress", {}).get("phase"), "completed", status)
            self.assertEqual(
                [leaf["leaf_id"] for leaf in status["leaves"]],
                ["QS-C22.AS"],
            )
            attempts = [
                item
                for item in client.get(
                    f"/api/projects/{self.project_id}/operations"
                ).json()
                if item["request_id"] == operation_id
            ]
            attempts.sort(key=lambda item: int(item["attempt"]))
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0]["operation_id"], operation_id)
            self.assertEqual(attempts[0]["state"], "INTERRUPTED")
            self.assertEqual(attempts[1]["state"], "COMPLETED")
            self.assertEqual(attempts[1]["supersedes_operation_id"], operation_id)

    def test_audit_reads_verify_evidence_and_project_chain_linkage(self) -> None:
        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            events = store.audit_events_for_project(self.project_id)
            self.assertGreaterEqual(len(events), 3)
            previous_evidence: str | None = None
            rows = store._read().execute(
                "SELECT record_json, evidence_sha256 FROM audit_events "
                "WHERE project_id = ? ORDER BY rowid",
                (self.project_id,),
            ).fetchall()
            for event, (_, evidence_sha256) in zip(events, rows, strict=True):
                self.assertEqual(event.previous_evidence_sha256, previous_evidence)
                previous_evidence = evidence_sha256
        finally:
            store.close()

        connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
        try:
            subject_id, record_json = connection.execute(
                "SELECT subject_id, record_json FROM audit_events "
                "WHERE project_id = ? ORDER BY rowid LIMIT 1",
                (self.project_id,),
            ).fetchone()
            changed = json.loads(record_json)
            changed["actor_id"] = "local-integrity-tamper-detection"
            connection.execute(
                "UPDATE audit_events SET record_json = ? WHERE project_id = ? AND subject_id = ?",
                (
                    json.dumps(changed, sort_keys=True, separators=(",", ":")),
                    self.project_id,
                    subject_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        reopened = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            with self.assertRaisesRegex(ImmutableRecordConflict, "evidence digest"):
                reopened.audit_events_for_project(self.project_id)
            with self.assertRaisesRegex(ImmutableRecordConflict, "evidence digest"):
                reopened.audit_events_for(subject_id)
        finally:
            reopened.close()

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
            admitted = client.get(f"/api/projects/{self.project_id}/operations/{operation_id}")
            self.assertEqual(admitted.status_code, 200, admitted.text)
            self.assertIn(admitted.json()["state"], {"QUEUED", "RUNNING", "COMPLETED"})
            self.assertIsNotNone(admitted.json()["accepted_at"])
            self.assertEqual(admitted.json()["request"]["dataset_id"], self.dataset_id)

            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                status = client.get(
                    f"/api/projects/{self.project_id}/jobs/{operation_id}"
                )
                self.assertEqual(status.status_code, 200, status.text)
                if status.json()["state"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                    break
                time.sleep(0.25)
            else:
                self.fail(f"real quality operation did not reach a terminal state: {status.text}")
            self.assertEqual(status.json()["state"], "COMPLETED", status.text)
            operation_deadline = time.monotonic() + 30
            while time.monotonic() < operation_deadline:
                terminal_response = client.get(f"/api/projects/{self.project_id}/operations/{operation_id}")
                self.assertEqual(terminal_response.status_code, 200, terminal_response.text)
                terminal = terminal_response.json()
                if terminal["state"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"operation envelope did not converge after its real job: {terminal}")
            self.assertEqual(terminal["state"], "COMPLETED")
            self.assertIsNotNone(terminal["started_at"])
            self.assertIsNotNone(terminal["ended_at"])
            self.assertIsNone(terminal["error"])
            self.assertNotIn("output_path", terminal)

        with TestClient(create_app(self.settings), base_url="http://127.0.0.1") as restarted:
            recovered = restarted.get(f"/api/projects/{self.project_id}/operations/{operation_id}")
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
                status = client.get(
                    f"/api/projects/{self.project_id}/jobs/{operation_id}"
                )
                if status.json()["state"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                    break
                time.sleep(0.25)
            else:
                self.fail(f"real quality operation did not reach a terminal state: {status.text}")
            self.assertEqual(status.json()["state"], "COMPLETED", status.text)
            public_operation = client.get(f"/api/projects/{self.project_id}/operations/{operation_id}")
            self.assertEqual(public_operation.status_code, 200, public_operation.text)
            self.assertNotIn("output_path", public_operation.json())

        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            operation = store.operation(operation_id)
            self.assertIsNotNone(operation)
            output_path = Path(operation["output_path"])
            self.assertTrue(output_path.is_dir())
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
            self.assertEqual(restarted.get(f"/api/projects/{self.project_id}/operations/{operation_id}").status_code, 404)
            project = restarted.get(f"/api/projects/{self.project_id}")
            self.assertEqual(project.status_code, 200, project.text)


if __name__ == "__main__":
    unittest.main()
