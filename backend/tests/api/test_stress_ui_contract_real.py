"""Stress Studio integration over one rights-recorded real LFW upload."""

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
from qualsight.api.store import RunStore
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)


ROOT = Path(__file__).resolve().parents[3]
REAL_RELATIVE_PATH = "Aaron_Peirsol/Aaron_Peirsol_0001.jpg"
REAL_IMAGE = ROOT.parent / "datasets" / "lfw" / "lfw_funneled" / REAL_RELATIVE_PATH
IMAGE_MANIFEST = ROOT / "resources" / "manifests" / "lfw-funneled-images-v1.jsonl"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
TRANSFORMS = {
    "jpeg": 20,
    "gaussian_blur": 5.0,
    "downscale": 0.25,
    "gamma": 0.3,
    "contrast": 0.3,
    "rotation": 45.0,
    "occlusion": 0.35,
}


class RealStressUiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-stress-ui-contract-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=self.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        self.client = TestClient(
            create_app(settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        )
        self.client.__enter__()
        self.client_closed = False

    def tearDown(self) -> None:
        if not self.client_closed:
            self.client.__exit__(None, None, None)
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_owner_working_sample_admits_all_published_stress_operators_atomically(self) -> None:
        self.assertTrue(REAL_IMAGE.is_file(), str(REAL_IMAGE))
        manifest_record = next(
            json.loads(line)
            for line in IMAGE_MANIFEST.read_text(encoding="utf-8").splitlines()
            if json.loads(line)["path"] == REAL_RELATIVE_PATH
        )
        image_bytes = REAL_IMAGE.read_bytes()
        self.assertEqual(hashlib.sha256(image_bytes).hexdigest(), manifest_record["sha256"])
        self.assertEqual(len(image_bytes), manifest_record["size_bytes"])

        project = self.client.post(
            "/api/projects",
            json={
                "name": "Stress Studio owner-upload contract",
                "description": "Real recorded LFW bytes through the integrated owner workflow.",
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
                "filename": REAL_IMAGE.name,
            },
            content=image_bytes,
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        source = uploaded.json()
        public_source = self.client.get(
            f"/api/projects/{project_id}/samples/{source['sample_id']}"
        )
        self.assertEqual(public_source.status_code, 200, public_source.text)
        public_source_text = json.dumps(public_source.json(), sort_keys=True)
        self.assertNotIn("original_name", public_source_text)
        self.assertNotIn(REAL_IMAGE.name, public_source_text)
        with sqlite3.connect(self.data_dir / "qualsight.sqlite3") as connection:
            private_sample = connection.execute(
                "SELECT record_json FROM samples WHERE sample_id = ?",
                (source["sample_id"],),
            ).fetchone()
        self.assertIsNotNone(private_sample)
        private_record = json.loads(private_sample[0])
        private_metadata = json.loads(private_record["metadata"]["text"])
        self.assertEqual(private_metadata["original_name"], REAL_IMAGE.name)

        created = self.client.post(
            f"/api/projects/{project_id}/datasets/{source['dataset_id']}/transformations",
            json={
                "selection": "sample_ids",
                "sample_ids": [source["sample_id"]],
                "transforms": [
                    {"kind": kind, "parameters": [parameter]}
                    for kind, parameter in TRANSFORMS.items()
                ],
            },
        )

        self.assertEqual(created.status_code, 201, created.text)
        record = created.json()
        self.assertEqual(record["source_count"], 1)
        self.assertEqual(record["levels"], 7)
        self.assertEqual(record["derived_count"], 7)
        self.assertIsNone(record["source_resource_id"])
        derived_dataset = self.client.get(
            f"/api/projects/{project_id}/datasets/{record['derived_dataset_id']}"
        )
        self.assertEqual(derived_dataset.status_code, 200, derived_dataset.text)
        self.assertIsNone(derived_dataset.json()["resource_id"])
        self.assertIsNone(derived_dataset.json()["subject_count"])
        self.assertIn(
            RESEARCH_SCOPE,
            derived_dataset.json()["common_scopes_with_engine"],
        )
        detail = self.client.get(record["links"]["detail"])
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(len(detail.json()["samples"]), 7)
        for row in detail.json()["samples"]:
            sample = self.client.get(
                f"/api/projects/{project_id}/samples/{row['derived_sample_id']}"
            )
            self.assertEqual(sample.status_code, 200, sample.text)
            self.assertIsNone(sample.json()["subject_id"])
        admitted_batch = self.client.post(
            f"/api/projects/{project_id}/datasets/{record['derived_dataset_id']}/quality-batch",
            json={"selection": {"kind": "all"}, "chunks": 1},
        )
        self.assertEqual(admitted_batch.status_code, 202, admitted_batch.text)
        deadline = time.monotonic() + 240
        job = admitted_batch.json()
        while time.monotonic() < deadline:
            job_response = self.client.get(
                f"/api/projects/{project_id}/jobs/{job['job_id']}"
            )
            self.assertEqual(job_response.status_code, 200, job_response.text)
            job = job_response.json()
            if job["state"] in {"COMPLETED", "FAILED", "CANCELLED"} and job["ended_at"]:
                break
            time.sleep(0.25)
        self.assertEqual(job["state"], "COMPLETED", job)

    def test_real_v7_derived_record_is_snapshotted_and_migrated_to_nullable_source_authority(self) -> None:
        project = self.client.post(
            "/api/projects",
            json={
                "name": "Real v7 derived-set migration contract",
                "description": "Registered LFW record retained through the schema boundary.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["project_id"]
        self.assertEqual(self.client.post(f"/api/projects/{project_id}/open").status_code, 200)
        imported = self.client.post(
            f"/api/projects/{project_id}/datasets/import-registered",
            json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
        )
        self.assertEqual(imported.status_code, 201, imported.text)
        dataset_id = imported.json()["dataset_id"]
        samples = self.client.get(
            f"/api/projects/{project_id}/datasets/{dataset_id}/samples",
            params={"limit": 1},
        )
        self.assertEqual(samples.status_code, 200, samples.text)
        created = self.client.post(
            f"/api/projects/{project_id}/datasets/{dataset_id}/transformations",
            json={
                "selection": "sample_ids",
                "sample_ids": [samples.json()[0]["sample_id"]],
                "transforms": [{"kind": "jpeg", "parameters": [20]}],
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        derived_set_id = created.json()["derived_set_id"]
        database = self.data_dir / "qualsight.sqlite3"
        self.client.__exit__(None, None, None)
        self.client_closed = True

        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            before = connection.execute(
                "SELECT derived_set_id, source_resource_id, record_json FROM derived_sets WHERE derived_set_id = ?",
                (derived_set_id,),
            ).fetchone()
            self.assertIsNotNone(before)
            self.assertEqual(before[1], LFW_RESOURCE_ID)
            connection.execute(
                "CREATE TABLE derived_sets_v7 ("
                "derived_set_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
                "source_dataset_id TEXT NOT NULL, source_resource_id TEXT NOT NULL, "
                "derived_dataset_id TEXT NOT NULL, directory TEXT NOT NULL, "
                "record_json TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO derived_sets_v7 SELECT derived_set_id, project_id, source_dataset_id, "
                "source_resource_id, derived_dataset_id, directory, record_json, created_at FROM derived_sets"
            )
            connection.execute("DROP TABLE derived_sets")
            connection.execute("ALTER TABLE derived_sets_v7 RENAME TO derived_sets")
            connection.execute("DELETE FROM schema_migrations WHERE version >= 8")
            connection.execute("PRAGMA user_version=7")
            connection.commit()
        finally:
            connection.close()

        preflight = database.with_name(
            f"{database.name}.pre-migration-v7-to-v8.snapshot.sqlite3"
        )
        migrated = RunStore(database)
        try:
            connection = migrated._connection
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 8)
            columns = {
                row[1]: row for row in connection.execute("PRAGMA table_info(derived_sets)").fetchall()
            }
            self.assertEqual(columns["source_resource_id"][3], 0)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                connection.execute(
                    "SELECT derived_set_id, source_resource_id, record_json FROM derived_sets WHERE derived_set_id = ?",
                    (derived_set_id,),
                ).fetchone(),
                before,
            )
        finally:
            migrated.close()
        self.assertTrue(preflight.is_file())
        snapshot = sqlite3.connect(f"file:{preflight}?mode=ro", uri=True)
        try:
            self.assertEqual(snapshot.execute("PRAGMA user_version").fetchone()[0], 7)
            snapshot_columns = {
                row[1]: row for row in snapshot.execute("PRAGMA table_info(derived_sets)").fetchall()
            }
            self.assertEqual(snapshot_columns["source_resource_id"][3], 1)
            self.assertEqual(
                snapshot.execute(
                    "SELECT derived_set_id, source_resource_id, record_json FROM derived_sets WHERE derived_set_id = ?",
                    (derived_set_id,),
                ).fetchone(),
                before,
            )
        finally:
            snapshot.close()

    def test_two_real_images_of_one_recorded_lfw_person_retain_one_subject(self) -> None:
        project = self.client.post(
            "/api/projects",
            json={
                "name": "Real same-person derived identity contract",
                "description": "Two registered LFW images of one recorded person.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["project_id"]
        self.assertEqual(self.client.post(f"/api/projects/{project_id}/open").status_code, 200)
        imported = self.client.post(
            f"/api/projects/{project_id}/datasets/import-registered",
            json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
        )
        self.assertEqual(imported.status_code, 201, imported.text)
        dataset_id = imported.json()["dataset_id"]
        samples = self.client.get(
            f"/api/projects/{project_id}/datasets/{dataset_id}/samples",
            params={"limit": 10000},
        )
        self.assertEqual(samples.status_code, 200, samples.text)
        by_subject: dict[str, list[dict]] = {}
        for sample in samples.json():
            subject = sample["subject_id"]
            if subject is not None:
                by_subject.setdefault(subject, []).append(sample)
        same_person = next(rows[:2] for rows in by_subject.values() if len(rows) >= 2)

        created = self.client.post(
            f"/api/projects/{project_id}/datasets/{dataset_id}/transformations",
            json={
                "selection": "sample_ids",
                "sample_ids": [sample["sample_id"] for sample in same_person],
                "transforms": [{"kind": "jpeg", "parameters": [20]}],
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        derived_dataset = self.client.get(
            f"/api/projects/{project_id}/datasets/"
            f"{created.json()['derived_dataset_id']}"
        )
        self.assertEqual(derived_dataset.status_code, 200, derived_dataset.text)
        self.assertEqual(derived_dataset.json()["subject_count"], 1)
        derived_samples = self.client.get(
            f"/api/projects/{project_id}/datasets/"
            f"{created.json()['derived_dataset_id']}/samples"
        )
        self.assertEqual(derived_samples.status_code, 200, derived_samples.text)
        self.assertEqual(
            {sample["subject_id"] for sample in derived_samples.json()},
            {same_person[0]["subject_id"]},
        )

    def test_owner_derived_batch_refuses_corrupted_real_lineage_without_admitting_a_job(self) -> None:
        image_bytes = REAL_IMAGE.read_bytes()
        project = self.client.post(
            "/api/projects",
            json={
                "name": "Owner-derived integrity refusal contract",
                "description": "Adversarial metadata and byte drift over one real LFW upload.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["project_id"]
        self.assertEqual(self.client.post(f"/api/projects/{project_id}/open").status_code, 200)
        uploaded = self.client.post(
            "/api/quality/assess-upload",
            params={
                "project_id": project_id,
                "purpose_scope": RESEARCH_SCOPE,
                "filename": REAL_IMAGE.name,
            },
            content=image_bytes,
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        source = uploaded.json()
        created = self.client.post(
            f"/api/projects/{project_id}/datasets/{source['dataset_id']}/transformations",
            json={
                "selection": "sample_ids",
                "sample_ids": [source["sample_id"]],
                "transforms": [{"kind": "jpeg", "parameters": [20]}],
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        derived = created.json()
        detail_response = self.client.get(derived["links"]["detail"])
        self.assertEqual(detail_response.status_code, 200, detail_response.text)
        detail = detail_response.json()
        derived_row = detail["samples"][0]
        jobs_before = self.client.get(f"/api/projects/{project_id}/jobs").json()

        def refused() -> None:
            response = self.client.post(
                f"/api/projects/{project_id}/datasets/{derived['derived_dataset_id']}/quality-batch",
                json={"selection": {"kind": "all"}, "chunks": 1},
            )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(
                response.json()["detail"],
                "owner-derived sample provenance failed integrity verification",
            )
            self.assertNotIn(str(self.data_dir), response.text)
            self.assertEqual(
                self.client.get(f"/api/projects/{project_id}/jobs").json(),
                jobs_before,
            )

        database = self.data_dir / "qualsight.sqlite3"
        connection = sqlite3.connect(database)
        try:
            parent_path = Path(
                connection.execute(
                    "SELECT source_path FROM samples WHERE sample_id = ?",
                    (source["sample_id"],),
                ).fetchone()[0]
            )
            derived_path = Path(
                connection.execute(
                    "SELECT source_path FROM samples WHERE sample_id = ?",
                    (derived_row["derived_sample_id"],),
                ).fetchone()[0]
            )
            chain_text = connection.execute(
                "SELECT chain_json FROM derived_samples WHERE derived_sample_id = ?",
                (derived_row["derived_sample_id"],),
            ).fetchone()[0]
            tampered_chain = json.loads(chain_text)
            tampered_chain[0]["parent_sha256"] = "0" * 64
            connection.execute(
                "UPDATE derived_samples SET chain_json = ? WHERE derived_sample_id = ?",
                (json.dumps(tampered_chain, sort_keys=True), derived_row["derived_sample_id"]),
            )
            connection.commit()
            refused()
            connection.execute(
                "UPDATE derived_samples SET chain_json = ? WHERE derived_sample_id = ?",
                (chain_text, derived_row["derived_sample_id"]),
            )
            connection.commit()

            parent_bytes = parent_path.read_bytes()
            drifted_parent = bytearray(parent_bytes)
            drifted_parent[0] ^= 1
            parent_path.write_bytes(drifted_parent)
            try:
                refused()
            finally:
                parent_path.write_bytes(parent_bytes)

            derived_bytes = derived_path.read_bytes()
            drifted_derived = bytearray(derived_bytes)
            drifted_derived[0] ^= 1
            derived_path.write_bytes(drifted_derived)
            try:
                refused()
            finally:
                derived_path.write_bytes(derived_bytes)

            other_project = self.client.post(
                "/api/projects",
                json={
                    "name": "Real cross-project integrity boundary",
                    "description": "A distinct recorded project for lineage mismatch refusal.",
                    "purpose_scope": RESEARCH_SCOPE,
                },
            )
            self.assertEqual(other_project.status_code, 201, other_project.text)
            record_text = connection.execute(
                "SELECT record_json FROM derived_sets WHERE derived_set_id = ?",
                (derived["derived_set_id"],),
            ).fetchone()[0]
            tampered_record = json.loads(record_text)
            tampered_record["project_id"] = other_project.json()["project_id"]
            connection.execute(
                "UPDATE derived_sets SET record_json = ? WHERE derived_set_id = ?",
                (json.dumps(tampered_record, sort_keys=True), derived["derived_set_id"]),
            )
            connection.commit()
            refused()
            connection.execute(
                "UPDATE derived_sets SET record_json = ? WHERE derived_set_id = ?",
                (record_text, derived["derived_set_id"]),
            )
            connection.commit()

            reference = connection.execute(
                "SELECT record_json FROM owner_supplied_project_refs WHERE path = ? AND project_id = ?",
                (str(parent_path), project_id),
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM owner_supplied_project_refs WHERE path = ? AND project_id = ?",
                (str(parent_path), project_id),
            )
            connection.commit()
            refused()
            connection.execute(
                "INSERT INTO owner_supplied_project_refs (path, project_id, record_json) VALUES (?, ?, ?)",
                (str(parent_path), project_id, reference),
            )
            connection.commit()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
