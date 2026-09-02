"""QS-B11: real LFW import through the API with full byte verification.

Imports the registered LFW funneled dataset (13,233 real JPEGs, 5,749
identities) into a project, verifying every file's SHA-256 in place, and checks
the identity mapping, factual rights, statistics, sample records, and the
deterministic real-data subset fixture.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)


ROOT = Path(__file__).resolve().parents[3]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"
IMAGE_MANIFEST = ROOT / "resources" / "manifests" / "lfw-funneled-images-v1.jsonl"
PAIR_MANIFEST = ROOT / "resources" / "manifests" / "lfw-local-pairs-v1.jsonl"


class RealDatasetImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-dataset-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        cls.client = TestClient(create_app(settings), base_url="http://127.0.0.1")
        cls.client.__enter__()
        project = cls.client.post(
            "/api/projects",
            json={"name": "LFW import gate", "description": "QS-B11 real import.", "purpose_scope": RESEARCH_SCOPE},
        )
        assert project.status_code == 201, project.text
        cls.project_id = project.json()["project_id"]
        cls.client.post(f"/api/projects/{cls.project_id}/open")
        cls.imported = cls.client.post(
            f"/api/projects/{cls.project_id}/datasets/import-registered",
            json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": True},
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def test_import_records_the_registered_dataset_with_factual_rights(self) -> None:
        self.assertEqual(self.imported.status_code, 201, self.imported.text)
        body = self.imported.json()
        self.assertEqual(body["project_id"], self.project_id)
        self.assertEqual(body["sample_count"], 13233)
        self.assertEqual(body["subject_count"], 5749)
        self.assertEqual(body["rights_status"], "NO_EXPLICIT_LICENSE_PUBLIC_RESEARCH_DATASET")
        self.assertEqual(body["modality"], "face")
        self.assertEqual(body["classification"], "PRIVATE")
        manifest = body["manifest"]
        self.assertEqual(manifest["image_manifest"]["sha256"], hashlib.sha256(IMAGE_MANIFEST.read_bytes()).hexdigest())
        self.assertEqual(manifest["pair_manifest"]["sha256"], hashlib.sha256(PAIR_MANIFEST.read_bytes()).hexdigest())
        self.assertEqual(manifest["sample_count"], 13233)
        self.assertIn("subject_id is the immediate parent directory", manifest["identity_rule"])
        listed = self.client.get(f"/api/projects/{self.project_id}/datasets").json()
        self.assertEqual([item["dataset_id"] for item in listed], [body["dataset_id"]])
        head = self.client.get(
            f"/api/projects/{self.project_id}/datasets/{body['dataset_id']}"
        ).json()
        self.assertEqual(head["evidence_sha256"], body["evidence_sha256"])

    def test_statistics_report_population_support_and_completeness(self) -> None:
        dataset_id = self.imported.json()["dataset_id"]
        response = self.client.get(
            f"/api/projects/{self.project_id}/datasets/{dataset_id}/statistics"
        )
        self.assertEqual(response.status_code, 200, response.text)
        stats = response.json()["statistics"]
        population = stats["population"]
        self.assertEqual(population["subjects"], 5749)
        self.assertEqual(population["samples"], 13233)
        self.assertEqual(population["samples_per_subject_min"], 1)
        self.assertEqual(population["samples_per_subject_median"], 1)
        self.assertEqual(population["samples_per_subject_max"], 530)
        self.assertEqual(population["subjects_with_at_least_two_samples"], 1680)
        support = stats["evaluation_support"]
        self.assertEqual(support["mated_comparisons"], 3000)
        self.assertEqual(support["nonmated_comparisons"], 3000)
        self.assertEqual(support["unique_protocol_samples"], 7701)
        completeness = stats["data_completeness"]
        self.assertEqual(completeness["missing_files"], 0)
        self.assertEqual(completeness["hash_mismatches"], 0)
        self.assertEqual(completeness["size_mismatches"], 0)
        self.assertTrue(completeness["hashes_verified"])
        self.assertEqual(completeness["unreadable_files"], 0)
        self.assertEqual(completeness["duplicate_files"], 0)
        self.assertEqual(completeness["images_without_detected_faces"]["state"], "NOT_ASSESSED")
        self.assertEqual(stats["quality_coverage"]["state"], "NOT_ASSESSED")
        # LFW funneled images are 250×250.
        self.assertEqual(stats["image_dimensions"]["widths"], [[250, 13233]])
        self.assertEqual(stats["image_dimensions"]["heights"], [[250, 13233]])
        import_statistics = {key: value for key, value in stats.items() if key != "quality_coverage"}
        import_statistics["quality_coverage"] = {"state": "NOT_ASSESSED", "reason": "No quality Run has executed over this dataset yet (QS-B12)."}
        self.assertEqual(
            response.json()["import_statistics_sha256"],
            hashlib.sha256(json.dumps(import_statistics, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest(),
        )

    def test_samples_carry_identity_mapping_and_verified_bytes(self) -> None:
        dataset_id = self.imported.json()["dataset_id"]
        page = self.client.get(
            f"/api/projects/{self.project_id}/datasets/{dataset_id}/samples",
            params={"limit": 3},
        ).json()
        self.assertEqual(len(page), 3)
        first = page[0]
        self.assertEqual(first["media_type"], "image/jpeg")
        self.assertEqual(first["subject_id"], first["metadata"]["subject_id"])
        image = self.client.get(first["links"]["image"])
        self.assertEqual(image.status_code, 200, image.text)
        self.assertEqual(hashlib.sha256(image.content).hexdigest(), first["sha256"])
        self.assertEqual(image.headers["x-qualsight-sha256"], first["sha256"])
        self.assertEqual(first["metadata"]["width"], 250)
        subject = self.client.get(
            f"/api/projects/{self.project_id}/datasets/{dataset_id}/samples",
            params={"subject_id": "George_W_Bush", "limit": 1000},
        ).json()
        self.assertEqual(len(subject), 530)
        self.assertTrue(all(item["subject_id"] == "George_W_Bush" for item in subject))

    def test_subset_fixture_is_deterministic_real_data_with_recorded_provenance(self) -> None:
        from qualsight.datasets import RegisteredDatasetImporter
        from qualsight.domain.primitives import ArtifactIdentity

        def identity(path: Path) -> ArtifactIdentity:
            stat = path.stat()
            return ArtifactIdentity(
                path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns
            )

        rows = [json.loads(line) for line in IMAGE_MANIFEST.read_text(encoding="utf-8").splitlines() if line]
        regenerated = RegisteredDatasetImporter.subset_fixture(PAIR_MANIFEST, identity(IMAGE_MANIFEST), identity(PAIR_MANIFEST), rows)
        recorded_path = ROOT / "resources" / "manifests" / "lfw-subset-fixture-v1.json"
        recorded = json.loads(recorded_path.read_text(encoding="utf-8"))
        self.assertEqual(regenerated, recorded)
        self.assertEqual(recorded_path.read_text(encoding="utf-8"), json.dumps(regenerated, indent=2, ensure_ascii=False) + "\n")
        self.assertEqual(len(recorded["pairs"]), 10)
        self.assertEqual([p["label"] for p in recorded["pairs"]], ["MATED"] * 5 + ["NON_MATED"] * 5)
        self.assertTrue(all(p["fold"] == 1 for p in recorded["pairs"]))
        self.assertGreaterEqual(len(recorded["images"]), 10)
        for image in recorded["images"]:
            absolute = Path("/mnt/projects/datasets/lfw/lfw_funneled") / image["path"]
            self.assertEqual(hashlib.sha256(absolute.read_bytes()).hexdigest(), image["sha256"], image["path"])
            self.assertEqual(image["subject_id"], Path(image["path"]).parent.name)
        self.assertEqual(recorded["provenance"]["image_manifest"]["sha256"], hashlib.sha256(IMAGE_MANIFEST.read_bytes()).hexdigest())

    def test_reimport_is_idempotent_and_archived_projects_refuse_import(self) -> None:
        again = self.client.post(
            f"/api/projects/{self.project_id}/datasets/import-registered",
            json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
        )
        self.assertEqual(again.status_code, 201, again.text)
        self.assertEqual(again.json()["dataset_id"], self.imported.json()["dataset_id"])
        listed = self.client.get(f"/api/projects/{self.project_id}/datasets").json()
        self.assertEqual(len(listed), 1)
        other = self.client.post(
            "/api/projects",
            json={"name": "archived", "description": "archived project", "purpose_scope": RESEARCH_SCOPE},
        ).json()
        self.client.post(f"/api/projects/{other['project_id']}/archive")
        refused = self.client.post(
            f"/api/projects/{other['project_id']}/datasets/import-registered", json={"resource_id": LFW_RESOURCE_ID}
        )
        self.assertEqual(refused.status_code, 409)
        unknown = self.client.post(
            f"/api/projects/{self.project_id}/datasets/import-registered", json={"resource_id": "not-a-registered-dataset"}
        )
        self.assertEqual(unknown.status_code, 422)


if __name__ == "__main__":
    unittest.main()
