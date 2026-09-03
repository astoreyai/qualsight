"""Real external-score publication recovery after SQLite refusal."""

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
from qualsight.api.store import RunStore
from qualsight.domain.primitives import CanonicalJson
from qualsight.execution.prepared_output import PREPARED_OUTPUT_FILENAME
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
    load_resource_registry,
)
from qualsight.importers.external_scores import (
    ExternalScoreImporter,
    recover_prepared_external_score_import,
)
from qualsight.security.purge import purge_targets


ROOT = Path(__file__).resolve().parents[2]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
EXTERNAL_RESOURCE_ID = "external-scores-lfw-view2-insightface-buffalo-l"
EXTERNAL_SCORE_TABLE = Path(
    "/mnt/projects/02_perception_biometrics/ofiq_29794/data/scores/"
    "lfw-view2-insightface-buffalo_l/external_scores.csv"
)
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


@unittest.skipUnless(
    EXTERNAL_SCORE_TABLE.is_file(), "registered external LFW score table is unavailable"
)
class ExternalScoreCrashRecoveryRealTests(unittest.TestCase):
    def test_api_sqlite_abort_is_operation_bound_recovered_and_purgeable(self) -> None:
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-api-external-score-"))
        self.addCleanup(shutil.rmtree, data_dir, True)
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        database = data_dir / "qualsight.sqlite3"
        with TestClient(
            create_app(settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        ) as client:
            project = client.post(
                "/api/projects",
                json={
                    "name": "API external score crash recovery",
                    "description": "SQLite refusal after registered LFW score publication.",
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
            protocol_response = client.post(
                f"/api/projects/{project_id}/datasets/{dataset_id}/protocols/freeze-lfw-view2"
            )
            self.assertEqual(protocol_response.status_code, 201, protocol_response.text)
            protocol_id = protocol_response.json()["protocol_id"]

            trigger = sqlite3.connect(database)
            try:
                trigger.execute(
                    "CREATE TRIGGER abort_api_external_score_record "
                    "BEFORE INSERT ON matcher_leaves BEGIN "
                    "SELECT RAISE(ABORT, 'injected API external score metadata refusal'); END"
                )
                trigger.commit()
            finally:
                trigger.close()
            try:
                response = client.post(
                    f"/api/projects/{project_id}/protocols/{protocol_id}/import-external-scores",
                    json={"resource_id": EXTERNAL_RESOURCE_ID},
                )
            finally:
                trigger = sqlite3.connect(database)
                try:
                    trigger.execute("DROP TRIGGER abort_api_external_score_record")
                    trigger.commit()
                finally:
                    trigger.close()
            self.assertEqual(response.status_code, 500, response.text)

            query = sqlite3.connect(database)
            query.row_factory = sqlite3.Row
            try:
                operation = query.execute(
                    "SELECT * FROM operations WHERE operation_kind = 'EXTERNAL_SCORE_IMPORT' "
                    "AND project_id = ?",
                    (project_id,),
                ).fetchone()
                leaf = query.execute(
                    "SELECT * FROM matcher_leaves WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
            finally:
                query.close()
            self.assertIsNotNone(
                operation,
                "API import exposed final registered-score evidence without a durable operation",
            )
            assert operation is not None
            self.assertEqual(operation["state"], "FAILED")
            self.assertIsNone(leaf)
            output_directory = Path(operation["output_path"])
            leaf_directory = output_directory / "QS-C21_EXTERNAL"
            self.assertTrue((leaf_directory / PREPARED_OUTPUT_FILENAME).is_file())

            purge_store = RunStore(database)
            try:
                self.assertIn(
                    str(output_directory),
                    purge_targets(purge_store, data_dir, project_id)["directories"],
                )
            finally:
                purge_store.close()

            retried = client.post(
                f"/api/projects/{project_id}/protocols/{protocol_id}/import-external-scores",
                json={"resource_id": EXTERNAL_RESOURCE_ID},
            )
            self.assertEqual(retried.status_code, 200, retried.text)
            self.assertEqual(retried.json()["provenance_marker"], "EXTERNALLY_COMPUTED")

        with TestClient(create_app(settings), base_url="http://127.0.0.1") as restarted:
            query = sqlite3.connect(database)
            query.row_factory = sqlite3.Row
            try:
                recovered_operation = query.execute(
                    "SELECT * FROM operations WHERE operation_kind = 'EXTERNAL_SCORE_IMPORT' "
                    "AND project_id = ? ORDER BY attempt DESC LIMIT 1",
                    (project_id,),
                ).fetchone()
                recovered_leaf = query.execute(
                    "SELECT * FROM matcher_leaves WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                attempts = query.execute(
                    "SELECT * FROM operations WHERE operation_kind = 'EXTERNAL_SCORE_IMPORT' "
                    "AND project_id = ? ORDER BY attempt",
                    (project_id,),
                ).fetchall()
            finally:
                query.close()
            self.assertIsNotNone(recovered_operation)
            self.assertIsNotNone(recovered_leaf)
            assert recovered_operation is not None and recovered_leaf is not None
            self.assertEqual([row["state"] for row in attempts], ["FAILED", "COMPLETED"])
            self.assertEqual([row["attempt"] for row in attempts], [1, 2])
            self.assertEqual(attempts[1]["supersedes_operation_id"], attempts[0]["operation_id"])
            self.assertEqual(attempts[1]["request_id"], attempts[0]["request_id"])
            self.assertEqual(recovered_operation["state"], "COMPLETED")
            self.assertEqual(recovered_operation["result_id"], recovered_leaf["matrix_id"])
            purged = restarted.post(
                f"/api/projects/{project_id}/purge", json={"confirm": project_id}
            )
            self.assertEqual(purged.status_code, 201, purged.text)
            self.assertTrue(purged.json()["verified"], purged.json())
            self.assertFalse(output_directory.exists())

    def test_sqlite_abort_preserves_exact_prepared_import_for_retry(self) -> None:
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-external-score-crash-"))
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
                    "name": "External score crash recovery",
                    "description": "SQLite abort after real registered external-score publication.",
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
            protocol_response = client.post(
                f"/api/projects/{project_id}/datasets/{dataset_id}/protocols/freeze-lfw-view2"
            )
            self.assertEqual(protocol_response.status_code, 201, protocol_response.text)
            protocol_id = protocol_response.json()["protocol_id"]

        database = data_dir / "qualsight.sqlite3"
        trigger_connection = sqlite3.connect(database)
        trigger_connection.execute(
            "CREATE TRIGGER abort_external_score_record "
            "BEFORE INSERT ON matcher_leaves BEGIN "
            "SELECT RAISE(ABORT, 'injected external score metadata refusal'); END"
        )
        trigger_connection.commit()
        trigger_connection.close()

        registry = load_resource_registry(settings.registry_path)
        store = RunStore(database)
        try:
            project_record = store.project(project_id)
            protocol = store.protocol(protocol_id)
            self.assertIsNotNone(project_record)
            self.assertIsNotNone(protocol)
            importer = ExternalScoreImporter(
                store=store,
                registry=registry,
                repository_root=ROOT,
                project=project_record,
                protocol=protocol,
                resource_id=EXTERNAL_RESOURCE_ID,
                job_root=data_dir / "external",
                actor="local-user:external-score-crash-test",
                purpose_scope=RESEARCH_SCOPE,
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "injected external score metadata refusal"
            ):
                importer.run()
            job_dir = importer.job_dir
            leaf_dir = importer.leaf_dir
            self.assertTrue(leaf_dir.is_dir())
            self.assertTrue(
                (leaf_dir / PREPARED_OUTPUT_FILENAME).is_file(),
                "external-score bytes were exposed without a durable prepared receipt",
            )
            request_sha256 = importer.request_sha256
            matrix_id = importer.matrix_id
        finally:
            store.close()

        trigger_connection = sqlite3.connect(database)
        trigger_connection.execute("DROP TRIGGER abort_external_score_record")
        trigger_connection.commit()
        trigger_connection.close()

        recovered = recover_prepared_external_score_import(
            leaf_dir,
            matrix_id=matrix_id,
            project_id=project_id,
            dataset_id=dataset_id,
            protocol_id=protocol_id,
            resource_id=EXTERNAL_RESOURCE_ID,
            request_sha256=request_sha256,
        )
        self.assertEqual(recovered.run.project_id, project_id)
        self.assertEqual(recovered.run.matcher.component_id, EXTERNAL_RESOURCE_ID)
        self.assertEqual(recovered.run.matcher_preprocessing.component_id, "EXTERNALLY_COMPUTED")
        self.assertEqual(
            hashlib.sha256(recovered.comparisons_parquet.read_bytes()).hexdigest(),
            recovered.run.artifacts[0].sha256,
        )

        retry_store = RunStore(database)
        try:
            project_record = retry_store.project(project_id)
            protocol = retry_store.protocol(protocol_id)
            retry = ExternalScoreImporter(
                store=retry_store,
                registry=registry,
                repository_root=ROOT,
                project=project_record,
                protocol=protocol,
                resource_id=EXTERNAL_RESOURCE_ID,
                job_root=data_dir / "external",
                actor="local-user:external-score-crash-test",
                purpose_scope=RESEARCH_SCOPE,
            ).run()
            recorded = retry_store.matcher_leaf(retry.run.run_id)
            self.assertIsNotNone(recorded)
            self.assertEqual(recorded["matrix_id"], matrix_id)
            self.assertEqual(recorded["condition_id"], "EXTERNALLY_COMPUTED")
            self.assertEqual(retry.mapping["artifact_rows"], 6000)
        finally:
            retry_store.close()

    def test_cli_external_import_is_bound_to_a_durable_operation(self) -> None:
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-cli-external-score-"))
        self.addCleanup(shutil.rmtree, data_dir, True)
        snapshot = data_dir.with_name(f"{data_dir.name}.snapshot.sqlite3")
        self.addCleanup(snapshot.unlink, missing_ok=True)
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
                    "name": "CLI external score operation",
                    "description": "Durable operation identity for a real registered score import.",
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
            protocol_response = client.post(
                f"/api/projects/{project_id}/datasets/{dataset_id}/protocols/freeze-lfw-view2"
            )
            self.assertEqual(protocol_response.status_code, 201, protocol_response.text)
            protocol_id = protocol_response.json()["protocol_id"]

        environment = {
            **os.environ,
            "PYTHONPATH": f"{ROOT / 'backend' / 'src'}:{ROOT}",
            "QUALSIGHT_REPOSITORY_ROOT": str(ROOT),
            "QUALSIGHT_DATA_DIR": str(data_dir),
            "QUALSIGHT_INPUT_RESOURCE_IDS": f"{OFIQ_INPUT_RESOURCE_ID},{LFW_RESOURCE_ID}",
            "QUALSIGHT_NETWORK_GUARD": "0",
        }
        database = data_dir / "qualsight.sqlite3"
        trigger = sqlite3.connect(database)
        try:
            trigger.execute(
                "CREATE TRIGGER stall_cli_external_score_record "
                "BEFORE INSERT ON matcher_leaves BEGIN "
                "SELECT sum(length(a.record_json) + length(b.record_json)) "
                "FROM samples AS a CROSS JOIN samples AS b; END"
            )
            trigger.commit()
        finally:
            trigger.close()
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "qualsight.cli",
                "import-external-scores",
                "--project-id",
                project_id,
                "--protocol-id",
                protocol_id,
                "--resource-id",
                EXTERNAL_RESOURCE_ID,
            ],
            cwd=ROOT / "backend",
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        operation = None
        result_id: str | None = None
        leaf_directory: Path | None = None
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                receipts = tuple(
                    (data_dir / "external").glob(
                        f"*/QS-C21_EXTERNAL/{PREPARED_OUTPUT_FILENAME}"
                    )
                )
                if receipts:
                    self.assertEqual(len(receipts), 1, receipts)
                    receipt_text = receipts[0].read_text(encoding="utf-8")
                    receipt = json.loads(receipt_text)
                    self.assertEqual(
                        receipt_text, CanonicalJson.from_value(receipt).text + "\n"
                    )
                    self.assertEqual(
                        receipt["operation_kind"], "EXTERNAL_SCORE_IMPORT"
                    )
                    self.assertEqual(receipt["project_id"], project_id)
                    result_id = str(receipt["result_id"])
                    leaf_directory = receipts[0].parent
                    break
                if worker.poll() is not None:
                    stdout, stderr = worker.communicate(timeout=30)
                    self.fail(
                        f"CLI external-score worker exited before publication: {(stdout, stderr)}"
                    )
                time.sleep(0.01)
            else:
                self.fail("timed out waiting for external-score prepared receipt")
            assert result_id is not None and leaf_directory is not None
            self.assertEqual(
                leaf_directory.parent.name, result_id.replace(":", "_")
            )
            time.sleep(0.5)
            self.assertIsNone(
                worker.poll(), "CLI external-score worker left the SQLite stall"
            )
            os.kill(worker.pid, signal.SIGKILL)
            stdout, stderr = worker.communicate(timeout=30)
            self.assertEqual(worker.returncode, -signal.SIGKILL, (stdout, stderr))
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.communicate(timeout=30)
            trigger = sqlite3.connect(database)
            try:
                trigger.execute("DROP TRIGGER IF EXISTS stall_cli_external_score_record")
                trigger.commit()
            finally:
                trigger.close()

        assert result_id is not None and leaf_directory is not None
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            operation = connection.execute(
                "SELECT * FROM operations WHERE operation_kind = 'EXTERNAL_SCORE_IMPORT' "
                "AND project_id = ? ORDER BY attempt DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            recorded_leaf = connection.execute(
                "SELECT 1 FROM matcher_leaves WHERE matrix_id = ?", (result_id,)
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(operation)
        assert operation is not None
        self.assertEqual(operation["state"], "RUNNING")
        self.assertEqual(operation["result_id"], result_id)
        self.assertEqual(Path(operation["output_path"]), leaf_directory.parent)
        self.assertIsNone(recorded_leaf)
        before_restart = RunStore(database)
        try:
            self.assertIn(
                str(Path(operation["output_path"])),
                purge_targets(before_restart, data_dir, project_id)["directories"],
            )
        finally:
            before_restart.close()
        restarted = subprocess.run(
            [
                sys.executable,
                "-m",
                "qualsight.cli",
                "snapshot-database",
                "--output",
                str(snapshot),
            ],
            cwd=ROOT / "backend",
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(restarted.returncode, 0, (restarted.stdout, restarted.stderr))

        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            operation = connection.execute(
                "SELECT * FROM operations WHERE operation_kind = 'EXTERNAL_SCORE_IMPORT' "
                "AND project_id = ? ORDER BY attempt DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            leaf = connection.execute(
                "SELECT * FROM matcher_leaves WHERE project_id = ? AND condition_id = ?",
                (project_id, "EXTERNALLY_COMPUTED"),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(
            operation,
            "CLI external import persisted scientific output without a durable operation",
        )
        self.assertIsNotNone(leaf)
        assert operation is not None and leaf is not None
        self.assertEqual(operation["state"], "COMPLETED")
        self.assertEqual(operation["result_id"], leaf["matrix_id"])
        recovered = recover_prepared_external_score_import(
            Path(leaf["comparisons_parquet"]).parent,
            matrix_id=leaf["matrix_id"],
            project_id=project_id,
            dataset_id=dataset_id,
            protocol_id=protocol_id,
            resource_id=EXTERNAL_RESOURCE_ID,
            request_sha256=CanonicalJson.from_value(
                json.loads(operation["request_json"])
            ).sha256,
        )
        self.assertEqual(recovered.run.project_id, project_id)
        self.assertEqual(recovered.run.matcher.component_id, EXTERNAL_RESOURCE_ID)


if __name__ == "__main__":
    unittest.main()
