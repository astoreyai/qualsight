"""B60: controlled single-image stress over one rights-recorded real LFW image."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
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
OPERATORS = {
    "jpeg": 82,
    "gaussian_blur": 1.25,
    "downscale": 0.5,
    "gamma": 1.2,
    "contrast": 1.1,
    "rotation": 12,
    "occlusion": 0.1,
}


class RealSingleImageStressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-single-stress-"))
        cls.client = cls._client(cls.data_dir)
        cls.client.__enter__()
        cls.project_id, cls.dataset_id, cls.sample = cls._admit_lfw(
            cls.client,
            data_dir=cls.data_dir,
            name="single-image stress gate",
            verify_hashes=True,
        )
        cls.other_project_id, cls.other_dataset_id, cls.other_sample = cls._admit_lfw(
            cls.client,
            data_dir=cls.data_dir,
            name="foreign single-image stress gate",
            verify_hashes=False,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    @staticmethod
    def _client(data_dir: Path) -> TestClient:
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        return TestClient(
            create_app(settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        )

    @staticmethod
    def _admit_lfw(
        client: TestClient,
        *,
        data_dir: Path,
        name: str,
        verify_hashes: bool,
    ) -> tuple[str, str, dict]:
        project = client.post(
            "/api/projects",
            json={
                "name": name,
                "description": "Real registered LFW image stress evidence.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        assert project.status_code == 201, project.text
        project_id = project.json()["project_id"]
        opened = client.post(f"/api/projects/{project_id}/open")
        assert opened.status_code == 200, opened.text
        imported = client.post(
            f"/api/projects/{project_id}/datasets/import-registered",
            json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": verify_hashes},
        )
        assert imported.status_code == 201, imported.text
        assert imported.json()["rights_status"] == "NO_EXPLICIT_LICENSE_PUBLIC_RESEARCH_DATASET"
        dataset_id = imported.json()["dataset_id"]
        samples = client.get(
            f"/api/projects/{project_id}/datasets/{dataset_id}/samples",
            params={"limit": 1},
        )
        assert samples.status_code == 200, samples.text
        sample = samples.json()[0]
        connection = sqlite3.connect(data_dir / "qualsight.sqlite3")
        try:
            row = connection.execute(
                "SELECT source_path FROM samples WHERE sample_id = ?",
                (sample["sample_id"],),
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        sample["source_path"] = row[0]
        return project_id, dataset_id, sample

    @staticmethod
    def _derived_directories(data_dir: Path) -> set[str]:
        root = data_dir / "derived"
        return {item.name for item in root.iterdir() if item.is_dir()} if root.exists() else set()

    @staticmethod
    def _audit(client: TestClient, project_id: str) -> list[dict]:
        response = client.get(f"/api/projects/{project_id}/audit")
        assert response.status_code == 200, response.text
        return response.json()

    def _post(self, body: dict):
        return self.client.post(
            f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/transformations",
            json=body,
        )

    def test_catalog_publishes_the_closed_typed_contract_for_all_seven_operators(self) -> None:
        response = self.client.get("/api/transforms")
        self.assertEqual(response.status_code, 200, response.text)
        operators = response.json()["operators"]
        self.assertEqual([item["kind"] for item in operators], list(OPERATORS))
        for item in operators:
            self.assertEqual(item["version"], "1.0.0")
            contract = item["parameter_contract"]
            self.assertIn(contract["value_type"], {"integer", "number"})
            self.assertIsInstance(contract["minimum_exclusive"], bool)
            self.assertIsInstance(contract["maximum_exclusive"], bool)
            self.assertIn("minimum", contract)
            self.assertIn("maximum", contract)

    def test_sample_id_selection_is_nonempty_unique_and_exact_for_the_route_dataset(self) -> None:
        sample_id = self.sample["sample_id"]
        invalid = {
            "empty": [],
            "duplicate": [sample_id, sample_id],
            "missing": [sample_id, "sample:sha256:" + "0" * 64],
            "foreign": [sample_id, self.other_sample["sample_id"]],
        }
        for index, (label, sample_ids) in enumerate(invalid.items(), start=1):
            with self.subTest(label=label):
                before_directories = self._derived_directories(self.data_dir)
                before_sets = self.client.get(
                    f"/api/projects/{self.project_id}/derived-sets"
                ).json()
                before_audit = self._audit(self.client, self.project_id)
                response = self._post(
                    {
                        "selection": "sample_ids",
                        "sample_ids": sample_ids,
                        "transforms": [{"kind": "jpeg", "parameters": [70 + index]}],
                    }
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIsInstance(response.json()["detail"], list)
                self.assertEqual(self._derived_directories(self.data_dir), before_directories)
                self.assertEqual(
                    self.client.get(f"/api/projects/{self.project_id}/derived-sets").json(),
                    before_sets,
                )
                self.assertEqual(self._audit(self.client, self.project_id), before_audit)

    def test_transform_request_rejects_untyped_unknown_nonfinite_and_out_of_contract_values(self) -> None:
        sample_id = self.sample["sample_id"]
        invalid_transforms = {
            "unknown field": {"kind": "jpeg", "parameters": [73], "unrecorded": "value"},
            "unknown operator": {"kind": "unsharp_mask", "parameters": [1]},
            "parameters not a list": {"kind": "jpeg", "parameters": "73"},
            "boolean number": {"kind": "jpeg", "parameters": [True]},
            "integer contract gets float": {"kind": "jpeg", "parameters": [57.0]},
            "nan": {"kind": "gaussian_blur", "parameters": [float("nan")]},
            "positive infinity": {"kind": "gamma", "parameters": [float("inf")]},
            "negative infinity": {"kind": "contrast", "parameters": [float("-inf")]},
            "jpeg above maximum": {"kind": "jpeg", "parameters": [101]},
            "gaussian blur at exclusive minimum": {"kind": "gaussian_blur", "parameters": [0]},
            "downscale at exclusive maximum": {"kind": "downscale", "parameters": [1]},
            "gamma below minimum": {"kind": "gamma", "parameters": [0.09]},
            "contrast above maximum": {"kind": "contrast", "parameters": [5.01]},
            "rotation above maximum": {"kind": "rotation", "parameters": [181]},
            "occlusion at exclusive maximum": {"kind": "occlusion", "parameters": [1]},
        }
        source_path = Path(self.sample["source_path"])
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        self.assertEqual(source_sha256, self.sample["sha256"])
        for label, transform in invalid_transforms.items():
            with self.subTest(label=label):
                before_directories = self._derived_directories(self.data_dir)
                before_sets = self.client.get(
                    f"/api/projects/{self.project_id}/derived-sets"
                ).json()
                before_audit = self._audit(self.client, self.project_id)
                body = {
                    "selection": "sample_ids",
                    "sample_ids": [sample_id],
                    "transforms": [transform],
                }
                if label in {"nan", "positive infinity", "negative infinity"}:
                    response = self.client.post(
                        f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/transformations",
                        content=json.dumps(body, allow_nan=True),
                        headers={"Content-Type": "application/json"},
                    )
                else:
                    response = self._post(body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIsInstance(response.json()["detail"], list)
                self.assertEqual(self._derived_directories(self.data_dir), before_directories)
                self.assertEqual(
                    self.client.get(f"/api/projects/{self.project_id}/derived-sets").json(),
                    before_sets,
                )
                self.assertEqual(self._audit(self.client, self.project_id), before_audit)
        self.assertEqual(hashlib.sha256(source_path.read_bytes()).hexdigest(), source_sha256)

    def test_every_operator_executes_one_exact_real_sample_with_complete_provenance(self) -> None:
        response = self._post(
            {
                "selection": "sample_ids",
                "sample_ids": [self.sample["sample_id"]],
                "transforms": [
                    {"kind": kind, "parameters": [parameter]}
                    for kind, parameter in OPERATORS.items()
                ],
            }
        )
        self.assertEqual(response.status_code, 201, response.text)
        record = response.json()
        self.assertEqual(record["source_count"], 1)
        self.assertEqual(record["levels"], 7)
        self.assertEqual(record["derived_count"], 7)
        detail = self.client.get(
            f"/api/projects/{self.project_id}/derived-sets/{record['derived_set_id']}"
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        rows = detail.json()["samples"]
        self.assertEqual({row["transform"] for row in rows}, set(OPERATORS))
        for row in rows:
            self.assertEqual(row["parent_sample_id"], self.sample["sample_id"])
            self.assertEqual(row["chain"][0]["root_resource_id"], LFW_RESOURCE_ID)
            self.assertEqual(row["chain"][0]["parent_sample_id"], self.sample["sample_id"])
            self.assertEqual(row["chain"][0]["parent_sha256"], self.sample["sha256"])
            operator = row["chain"][1]
            self.assertEqual(operator["kind"], row["transform"])
            self.assertEqual(operator["version"], "1.0.0")
            self.assertEqual(operator["level_index"], row["level_index"])
            self.assertIn("parameters", operator)
            self.assertIn("extension", operator["encoding"])
            self.assertIn("parameters", operator["encoding"])
            payload = self.client.get(row["links"]["image"])
            self.assertEqual(payload.status_code, 200, payload.text)
            self.assertEqual(hashlib.sha256(payload.content).hexdigest(), row["sha256"])
            self.assertEqual(payload.headers["X-QualSight-Sha256"], row["sha256"])

    def test_operator_bytes_are_hash_reproducible_across_isolated_data_directories(self) -> None:
        observed: list[dict[tuple[str, float], str]] = []
        for index in range(2):
            data_dir = Path(tempfile.mkdtemp(prefix=f"qualsight-stress-repro-{index}-"))
            client = self._client(data_dir)
            client.__enter__()
            try:
                project_id, dataset_id, sample = self._admit_lfw(
                    client,
                    data_dir=data_dir,
                    name="isolated stress reproducibility gate",
                    verify_hashes=True,
                )
                created = client.post(
                    f"/api/projects/{project_id}/datasets/{dataset_id}/transformations",
                    json={
                        "selection": "sample_ids",
                        "sample_ids": [sample["sample_id"]],
                        "transforms": [
                            {"kind": kind, "parameters": [parameter]}
                            for kind, parameter in OPERATORS.items()
                        ],
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                rows = client.get(
                    f"/api/projects/{project_id}/derived-sets/{created.json()['derived_set_id']}"
                ).json()["samples"]
                observed.append(
                    {
                        (row["transform"], row["parameter"]): hashlib.sha256(
                            client.get(row["links"]["image"]).content
                        ).hexdigest()
                        for row in rows
                    }
                )
            finally:
                client.__exit__(None, None, None)
                shutil.rmtree(data_dir, ignore_errors=True)
        self.assertEqual(observed[0], observed[1])

    def test_project_scoped_read_contract_refuses_foreign_and_path_addressed_biometric_reads(self) -> None:
        sample_id = self.sample["sample_id"]
        source_path = self.sample["source_path"]

        legacy_dataset = self.client.get(f"/api/datasets/{self.dataset_id}")
        self.assertEqual(legacy_dataset.status_code, 404, legacy_dataset.text)
        dataset = self.client.get(
            f"/api/projects/{self.project_id}/datasets/{self.dataset_id}"
        )
        self.assertEqual(dataset.status_code, 200, dataset.text)
        self.assertNotIn("/mnt/projects", dataset.text)
        project_datasets = self.client.get(
            f"/api/projects/{self.project_id}/datasets"
        )
        self.assertEqual(project_datasets.status_code, 200, project_datasets.text)
        self.assertNotIn("/mnt/projects", project_datasets.text)
        foreign_dataset = self.client.get(
            f"/api/projects/{self.other_project_id}/datasets/{self.dataset_id}"
        )
        self.assertEqual(foreign_dataset.status_code, 404, foreign_dataset.text)
        samples = self.client.get(
            f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/samples",
            params={"limit": 1},
        )
        self.assertEqual(samples.status_code, 200, samples.text)
        sample_text = json.dumps(samples.json(), sort_keys=True)
        self.assertNotIn("source_path", sample_text)
        self.assertNotIn(source_path, sample_text)
        foreign_samples = self.client.get(
            f"/api/projects/{self.other_project_id}/datasets/{self.dataset_id}/samples",
            params={"limit": 1},
        )
        self.assertEqual(foreign_samples.status_code, 404, foreign_samples.text)

        retired_path_contract = self.client.get(
            "/api/samples/image", params={"path": source_path}
        )
        self.assertEqual(retired_path_contract.status_code, 422, retired_path_contract.text)

        source = self.client.get(
            f"/api/projects/{self.project_id}/samples/{sample_id}/image"
        )
        self.assertEqual(source.status_code, 200, source.text)
        self.assertEqual(hashlib.sha256(source.content).hexdigest(), self.sample["sha256"])
        foreign_source = self.client.get(
            f"/api/projects/{self.other_project_id}/samples/{sample_id}/image"
        )
        self.assertEqual(foreign_source.status_code, 404, foreign_source.text)

        detection = self.client.get(
            f"/api/projects/{self.project_id}/samples/{sample_id}/detection"
        )
        self.assertEqual(detection.status_code, 200, detection.text)
        foreign_detection = self.client.get(
            f"/api/projects/{self.other_project_id}/samples/{sample_id}/detection"
        )
        self.assertEqual(foreign_detection.status_code, 404, foreign_detection.text)
        landmarks = self.client.get(
            f"/api/projects/{self.project_id}/samples/{sample_id}/ofiq-landmarks"
        )
        self.assertEqual(landmarks.status_code, 200, landmarks.text)
        foreign_landmarks = self.client.get(
            f"/api/projects/{self.other_project_id}/samples/{sample_id}/ofiq-landmarks"
        )
        self.assertEqual(foreign_landmarks.status_code, 404, foreign_landmarks.text)

        created = self._post(
            {
                "selection": "sample_ids",
                "sample_ids": [sample_id],
                "transforms": [{"kind": "jpeg", "parameters": [41]}],
            }
        )
        self.assertEqual(created.status_code, 201, created.text)
        record = created.json()
        created_text = json.dumps(record, sort_keys=True)
        self.assertNotIn(str(self.data_dir), created_text)
        self.assertNotIn(source_path, created_text)

        detail = self.client.get(
            f"/api/projects/{self.project_id}/derived-sets/{record['derived_set_id']}"
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        detail_text = json.dumps(detail.json(), sort_keys=True)
        self.assertNotIn(str(self.data_dir), detail_text)
        self.assertNotIn(source_path, detail_text)
        foreign_detail = self.client.get(
            f"/api/projects/{self.other_project_id}/derived-sets/{record['derived_set_id']}"
        )
        self.assertEqual(foreign_detail.status_code, 404, foreign_detail.text)
        legacy_detail = self.client.get(
            f"/api/derived-sets/{record['derived_set_id']}"
        )
        self.assertEqual(legacy_detail.status_code, 404, legacy_detail.text)

        derived_sample = detail.json()["samples"][0]
        derived_record = self.client.get(
            f"/api/projects/{self.project_id}/samples/{derived_sample['derived_sample_id']}"
        )
        self.assertEqual(derived_record.status_code, 200, derived_record.text)
        derived_record_text = json.dumps(derived_record.json(), sort_keys=True)
        self.assertNotIn("parent_path", derived_record_text)
        self.assertNotIn(source_path, derived_record_text)
        self.assertNotIn(str(self.data_dir), derived_record_text)
        with sqlite3.connect(self.data_dir / "qualsight.sqlite3") as connection:
            private_chain = connection.execute(
                "SELECT chain_json FROM derived_samples WHERE derived_sample_id = ?",
                (derived_sample["derived_sample_id"],),
            ).fetchone()
        self.assertIsNotNone(private_chain)
        self.assertEqual(json.loads(private_chain[0])[0]["parent_path"], source_path)
        derived_image = self.client.get(derived_sample["links"]["image"])
        self.assertEqual(derived_image.status_code, 200, derived_image.text)
        self.assertEqual(
            hashlib.sha256(derived_image.content).hexdigest(),
            derived_sample["sha256"],
        )
        foreign_derived_image = self.client.get(
            f"/api/projects/{self.other_project_id}/samples/{derived_sample['derived_sample_id']}/image"
        )
        self.assertEqual(foreign_derived_image.status_code, 404, foreign_derived_image.text)

    def test_failed_rebuild_rolls_back_promoted_files_database_rows_and_audit(self) -> None:
        request = {
            "selection": "sample_ids",
            "sample_ids": [self.sample["sample_id"]],
            "transforms": [{"kind": "jpeg", "parameters": [83]}],
        }
        created = self._post(request)
        self.assertEqual(created.status_code, 201, created.text)
        record = created.json()
        directory = (
            self.data_dir
            / "derived"
            / record["derived_set_id"].replace(":", "_")
        )
        self.assertTrue(directory.is_dir())
        before_sets = self.client.get(f"/api/projects/{self.project_id}/derived-sets").json()
        detail_url = (
            f"/api/projects/{self.project_id}/derived-sets/{record['derived_set_id']}"
        )
        before_rows = self.client.get(detail_url).json()
        before_audit = self._audit(self.client, self.project_id)

        shutil.rmtree(directory)
        failed = self._post(request)
        self.assertEqual(failed.status_code, 409, failed.text)
        self.assertEqual(
            failed.json()["detail"],
            "derived set could not be recorded atomically",
        )
        self.assertFalse(directory.exists())
        self.assertEqual(
            self.client.get(f"/api/projects/{self.project_id}/derived-sets").json(),
            before_sets,
        )
        self.assertEqual(
            self.client.get(detail_url).json(),
            before_rows,
        )
        self.assertEqual(self._audit(self.client, self.project_id), before_audit)


if __name__ == "__main__":
    unittest.main()
