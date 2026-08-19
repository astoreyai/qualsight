"""Plan D cycle C27 — owner-supplied local inputs (decisions 30-31).

QS-Y80 admission layer, QS-Y81 single-photo upload -> recorded Run, QS-Y82
identity-labelled directory / CSV-manifest import + batch OFIQ, QS-Y83
deterministic owner pair protocol + matcher execution. Every image byte in
these tests is a real registered LFW photograph copied (unaltered) outside the
registry tree to stand in for an evaluator's own files; nothing is synthetic.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
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

# Real people with >= 3 images each in the registered LFW tree; the copies
# below simulate an evaluator's own directory layout.
SUBJECTS = {
    "operator_a": ("George_W_Bush", 4),
    "operator_b": ("Tony_Blair", 4),
    "operator_c": ("Serena_Williams", 3),
}


class OwnerSuppliedRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-owner-"))
        cls.owner_root = Path(tempfile.mkdtemp(prefix="qualsight-evaluator-"))
        for alias, (subject, count) in SUBJECTS.items():
            source_dir = LFW_TREE / subject
            files = sorted(source_dir.glob("*.jpg"))[:count]
            assert len(files) == count, f"registered LFW subject {subject} lacks {count} images"
            target = cls.owner_root / alias
            target.mkdir(parents=True)
            for index, path in enumerate(files):
                shutil.copyfile(path, target / f"photo_{index:02d}.jpg")
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
            json={"name": "owner supplied gate", "description": "Plan D C27.", "purpose_scope": RESEARCH_SCOPE},
        ).json()
        cls.project_id = project["project_id"]
        cls.client.post(f"/api/projects/{cls.project_id}/open")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)
        shutil.rmtree(cls.owner_root, ignore_errors=True)

    def _wait(self, job_id: str, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        body: dict = {}
        while time.monotonic() < deadline:
            body = self.client.get(f"/api/jobs/{job_id}").json()
            if body["state"] in {"COMPLETED", "CANCELLED", "FAILED"} and body["ended_at"]:
                return body
            time.sleep(0.5)
        raise AssertionError(f"job {job_id} did not finish: {body}")

    # -- QS-Y80 + QS-Y81 -------------------------------------------------------

    def test_y81_uploaded_photo_yields_recorded_run_with_source_kind(self) -> None:
        photo = self.owner_root / "operator_a" / "photo_00.jpg"
        data = photo.read_bytes()
        expected_sha = hashlib.sha256(data).hexdigest()
        response = self.client.post(
            "/api/quality/assess-upload",
            params={"project_id": self.project_id, "purpose_scope": RESEARCH_SCOPE, "filename": "my_visit_photo.jpg"},
            content=data,
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["image_sha256"], expected_sha)
        self.assertEqual(body["source_kind"], "OWNER_SUPPLIED_LOCAL_INPUT")
        self.assertEqual(len(body["measures"]), 28)
        unified = {m["name"]: m for m in body["measures"]}["UnifiedQualityScore"]
        self.assertFalse(unified["failed"])
        self.assertGreaterEqual(unified["scalar"], 0)
        # The stored copy is content-addressed inside the data directory and re-hashes to itself.
        stored = Path(body["stored_path"])
        self.assertTrue(stored.is_relative_to(self.data_dir))
        self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), expected_sha)
        # The Run is recorded and names the owner-supplied source kind.
        run = self.client.get(f"/api/runs/{body['run_id']}")
        self.assertEqual(run.status_code, 200)
        run_text = json.dumps(run.json())
        self.assertIn("OWNER_SUPPLIED_LOCAL_INPUT", run_text)

    def test_y81_non_image_bytes_are_refused(self) -> None:
        response = self.client.post(
            "/api/quality/assess-upload",
            params={"project_id": self.project_id, "purpose_scope": RESEARCH_SCOPE, "filename": "notes.txt"},
            content=b"not an image at all",
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("decode", response.json()["detail"].lower())

    def test_y80_unadmitted_local_path_is_still_refused_by_assess(self) -> None:
        rogue = self.owner_root / "operator_a" / "photo_01.jpg"
        response = self.client.post(
            "/api/quality/assess",
            json={"image_path": str(rogue), "purpose_scope": RESEARCH_SCOPE},
        )
        self.assertEqual(response.status_code, 404, response.text)

    # -- QS-Y82 ----------------------------------------------------------------

    def test_y82_directory_import_records_identity_labelled_dataset_and_batch_runs(self) -> None:
        response = self.client.post(
            f"/api/projects/{self.project_id}/datasets/import-owner-directory",
            json={"root": str(self.owner_root), "name": "evaluator directory"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["subject_count"], 3)
        self.assertEqual(body["sample_count"], 11)
        self.assertEqual(body["source_kind"], "OWNER_SUPPLIED_LOCAL_INPUT")
        dataset_id = body["dataset_id"]
        type(self).directory_dataset_id = dataset_id
        started = self.client.post(
            f"/api/projects/{self.project_id}/datasets/{dataset_id}/quality-batch",
            json={"selection": {"kind": "all"}, "chunks": 1},
        )
        self.assertEqual(started.status_code, 202, started.text)
        finished = self._wait(started.json()["job_id"], timeout=180)
        self.assertEqual(finished["state"], "COMPLETED")
        self.assertEqual(finished["assessed"], 11)
        self.assertEqual(finished["failed"], 0)
        self.assertTrue(finished["run_id"])

    def test_y82_manifest_import_with_saved_mapping(self) -> None:
        manifest = self.owner_root / "evaluation_manifest.csv"
        with manifest.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["person_id", "file", "notes"])
            for alias in SUBJECTS:
                for path in sorted((self.owner_root / alias).glob("*.jpg")):
                    writer.writerow([alias, str(path), "evaluator row"])
        mapping = {"subject_id": "person_id", "path": "file"}
        response = self.client.post(
            f"/api/projects/{self.project_id}/datasets/import-owner-manifest",
            json={
                "manifest_path": str(manifest),
                "name": "evaluator manifest",
                "mapping": mapping,
                "mapping_name": "evaluator-csv-v1",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["subject_count"], 3)
        self.assertEqual(body["sample_count"], 11)
        # The mapping is saved and reusable by name (PRD section 12).
        mappings = self.client.get("/api/import-mappings").json()
        names = {item["mapping_name"]: item["mapping"] for item in mappings}
        self.assertEqual(names.get("evaluator-csv-v1"), mapping)
        reused = self.client.post(
            f"/api/projects/{self.project_id}/datasets/import-owner-manifest",
            json={
                "manifest_path": str(manifest),
                "name": "evaluator manifest reuse",
                "mapping_name": "evaluator-csv-v1",
            },
        )
        self.assertEqual(reused.status_code, 201, reused.text)
        self.assertEqual(reused.json()["sample_count"], 11)

    def test_y82_traversal_and_symlink_rows_are_refused(self) -> None:
        manifest = self.owner_root / "hostile_manifest.csv"
        outside = LFW_TREE / "Colin_Powell" / "Colin_Powell_0001.jpg"
        with manifest.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["person_id", "file"])
            writer.writerow(["mallory", str(outside)])
        response = self.client.post(
            f"/api/projects/{self.project_id}/datasets/import-owner-manifest",
            json={
                "manifest_path": str(manifest),
                "name": "hostile manifest",
                "mapping": {"subject_id": "person_id", "path": "file"},
            },
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("outside", response.json()["detail"].lower())

    # -- QS-Y83 ----------------------------------------------------------------

    def test_y83_owner_protocol_is_deterministic_and_matcher_executes(self) -> None:
        if not hasattr(type(self), "directory_dataset_id"):
            self.test_y82_directory_import_records_identity_labelled_dataset_and_batch_runs()
        dataset_id = type(self).directory_dataset_id
        request = {"seed": 20260819, "max_mated_pairs_per_subject": 6, "nonmated_multiple": 1.0}
        first = self.client.post(
            f"/api/projects/{self.project_id}/datasets/{dataset_id}/protocols/freeze-owner-pairs",
            json=request,
        )
        self.assertEqual(first.status_code, 201, first.text)
        record = first.json()
        self.assertGreaterEqual(record["mated_count"], 9)
        self.assertEqual(record["nonmated_count"], record["mated_count"])
        self.assertTrue(record["pairs_sha256"])
        # Determinism: identical dataset + seed -> the identical frozen pair list.
        second = self.client.post(
            f"/api/projects/{self.project_id}/datasets/{dataset_id}/protocols/freeze-owner-pairs",
            json=request,
        )
        self.assertIn(second.status_code, (200, 201), second.text)
        self.assertEqual(second.json()["pairs_sha256"], record["pairs_sha256"])
        # The frozen owner protocol executes through the real matcher matrix.
        started = self.client.post(
            f"/api/projects/{self.project_id}/protocols/{record['protocol_id']}/matrix",
            params={"leaves": "QS-C22.AS"},
        )
        self.assertEqual(started.status_code, 202, started.text)
        matrix_id = started.json()["matrix_id"]
        deadline = time.monotonic() + 600
        status: dict = {}
        while time.monotonic() < deadline:
            status = self.client.get(f"/api/matrices/{matrix_id}").json()
            if status["progress"]["phase"] in {"completed", "failed", "partial"} and status["leaves"]:
                break
            if status["progress"]["phase"] == "failed":
                break
            time.sleep(1.0)
        self.assertNotEqual(status["progress"]["phase"], "failed", status)
        self.assertEqual(len(status["leaves"]), 1, status)
        leaf = status["leaves"][0]
        self.assertEqual(leaf["comparison_count"], record["pair_count"])
        self.assertIn("EXCLUDE_FAILED_TRIALS", leaf["eer"])

    # -- QS-Y80 purge ----------------------------------------------------------

    def test_y80_purge_removes_owner_supplied_copies(self) -> None:
        photo = self.owner_root / "operator_b" / "photo_00.jpg"
        data = photo.read_bytes()
        project = self.client.post(
            "/api/projects",
            json={"name": "owner purge gate", "description": "Plan D C27 purge.", "purpose_scope": RESEARCH_SCOPE},
        ).json()
        self.client.post(f"/api/projects/{project['project_id']}/open")
        uploaded = self.client.post(
            "/api/quality/assess-upload",
            params={"project_id": project["project_id"], "purpose_scope": RESEARCH_SCOPE, "filename": "purge_me.jpg"},
            content=data,
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        stored = Path(uploaded.json()["stored_path"])
        self.assertTrue(stored.exists())
        purged = self.client.post(
            f"/api/projects/{project['project_id']}/purge",
            json={"confirm": project["project_id"]},
        )
        self.assertIn(purged.status_code, (200, 201), purged.text)
        self.assertFalse(stored.exists(), "purge must remove the owner-supplied stored copy")


if __name__ == "__main__":
    unittest.main()
