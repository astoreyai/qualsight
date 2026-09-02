"""QS-B10: project lifecycle preserves scientific configuration and immutable Run references.

Runs recorded inside a project are real OFIQ v1.1.2 executions on the
registered r-01 image; every lifecycle transition is exercised through the
real API and re-read from SQLite.
"""

from __future__ import annotations

import hashlib
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
SCOPE = "REPRODUCE_OFIQ_CONFORMANCE_TESTS"


class RealProjectLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-projects-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID,),
        )
        cls.client = TestClient(create_app(settings), base_url="http://127.0.0.1")
        cls.client.__enter__()
        cls.registered_sample_id = next(
            item["registered_sample_id"]
            for item in cls.client.get("/api/samples/registered").json()
            if item["resource_id"] == OFIQ_INPUT_RESOURCE_ID
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def _create(self, name: str) -> dict:
        response = self.client.post(
            "/api/projects",
            json={
                "name": name,
                "description": "Real OFIQ conformance-image quality project for the lifecycle gate.",
                "purpose_scope": SCOPE,
                "tags": ["OFIQ", "lifecycle"],
                "notes": ["Created by the QS-B10 real test."],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_full_lifecycle_preserves_configuration_and_run_references(self) -> None:
        created = self._create("Cross-Matcher Face Quality Evaluation — Fall 2026")
        project_id = created["project_id"]
        self.assertEqual(created["lifecycle"], "NEW")
        self.assertEqual(created["version"], 1)
        configuration = created["scientific_configuration"]
        self.assertEqual(configuration["quality_engine"]["resource_id"], OFIQ_V112_RUNTIME_RESOURCE_ID)
        self.assertEqual(configuration["quality_engine"]["version"], "1.1.2")
        self.assertEqual(len(configuration["quality_engine"]["model_hashes"]), 12)
        registry_sha = hashlib.sha256((ROOT / "resources" / "resource-registry.json").read_bytes()).hexdigest()
        self.assertEqual(configuration["registry_sha256"], registry_sha)

        # Open, then record a real Run inside the project.
        opened = self.client.post(f"/api/projects/{project_id}/open").json()
        self.assertEqual(opened["lifecycle"], "OPEN")
        self.assertEqual(opened["version"], 2)
        assessed = self.client.post(
            "/api/quality/assess", json={"registered_sample_id": self.registered_sample_id, "purpose_scope": SCOPE, "project_id": project_id}
        )
        self.assertEqual(assessed.status_code, 200, assessed.text)
        run_id = assessed.json()["run_id"]
        stored_run = self.client.get(f"/api/projects/{project_id}/runs/{run_id}").json()["run"]
        self.assertEqual(stored_run["project_id"], project_id)

        # Save (rename) keeps configuration and references the immutable Run.
        saved = self.client.post(
            f"/api/projects/{project_id}/save", json={"name": "Face Quality Evaluation — Fall 2026 (renamed)"}
        ).json()
        self.assertEqual(saved["lifecycle"], "SAVED")
        self.assertEqual(saved["version"], 3)
        self.assertEqual(saved["name"], "Face Quality Evaluation — Fall 2026 (renamed)")
        self.assertEqual(saved["scientific_configuration"], configuration)
        self.assertEqual(saved["run_ids"], [run_id])

        # Close/reopen are lifecycle transitions that never lose state.
        reopened = self.client.post(f"/api/projects/{project_id}/open").json()
        self.assertEqual(reopened["lifecycle"], "OPEN")
        closed = self.client.post(f"/api/projects/{project_id}/close").json()
        self.assertEqual(closed["lifecycle"], "SAVED")
        self.assertEqual(closed["run_ids"], [run_id])
        self.assertEqual(closed["scientific_configuration"], configuration)

        # Clone and Save As create new projects carrying the exact configuration
        # and a reference to the source project's immutable Runs.
        clone = self.client.post(f"/api/projects/{project_id}/clone", json={"name": "Clone for threshold study"})
        self.assertEqual(clone.status_code, 201, clone.text)
        clone_body = clone.json()
        self.assertNotEqual(clone_body["project_id"], project_id)
        self.assertEqual(clone_body["lifecycle"], "NEW")
        self.assertEqual(clone_body["scientific_configuration"], configuration)
        self.assertEqual(clone_body["derived_from"]["project_id"], project_id)
        self.assertEqual(clone_body["derived_from"]["mode"], "CLONE")
        self.assertEqual(clone_body["derived_from"]["run_references"], [run_id])
        self.assertEqual(clone_body["run_ids"], [])
        save_as = self.client.post(f"/api/projects/{project_id}/save-as", json={"name": "Saved as copy"}).json()
        self.assertEqual(save_as["derived_from"]["mode"], "SAVE_AS")
        self.assertEqual(save_as["scientific_configuration"], configuration)

        # Archive refuses new Runs but keeps every reference; reopen lifts it.
        archived = self.client.post(f"/api/projects/{project_id}/archive").json()
        self.assertEqual(archived["lifecycle"], "ARCHIVED")
        self.assertEqual(archived["run_ids"], [run_id])
        refused = self.client.post(
            "/api/quality/assess", json={"registered_sample_id": self.registered_sample_id, "purpose_scope": SCOPE, "project_id": project_id}
        )
        self.assertEqual(refused.status_code, 409, refused.text)
        double_archive = self.client.post(f"/api/projects/{project_id}/archive")
        self.assertEqual(double_archive.status_code, 409)
        reopened_again = self.client.post(f"/api/projects/{project_id}/open").json()
        self.assertEqual(reopened_again["lifecycle"], "OPEN")

        # Every version is retained append-only and the head is the latest.
        versions = self.client.get(f"/api/projects/{project_id}/versions").json()
        self.assertEqual([v["version"] for v in versions], list(range(1, len(versions) + 1)))
        self.assertEqual(versions[0]["project"]["lifecycle"], "NEW")
        self.assertEqual(versions[-1]["project"]["lifecycle"], "OPEN")
        self.assertEqual(versions[0]["project"]["created_at"], versions[-1]["project"]["created_at"])
        head = self.client.get(f"/api/projects/{project_id}").json()
        self.assertEqual(head["version"], len(versions))
        self.assertEqual(head["run_ids"], [run_id])
        listed = {item["project_id"]: item for item in self.client.get("/api/projects").json()}
        self.assertIn(project_id, listed)
        self.assertIn(clone_body["project_id"], listed)

        # Audit trail: creation plus one CONFIGURATION_CHANGED per transition, plus RUN_COMPLETED.
        audit = self.client.get(f"/api/projects/{project_id}/audit").json()
        types = [event["event_type"] for event in audit]
        self.assertEqual(types.count("PROJECT_CREATED"), 1)
        self.assertEqual(types.count("RUN_COMPLETED"), 1)
        self.assertEqual(types.count("CONFIGURATION_CHANGED"), len(versions) - 1)

    def test_unknown_project_is_a_typed_404(self) -> None:
        missing = self.client.get("/api/projects/project:sha256:" + "0" * 64)
        self.assertEqual(missing.status_code, 404)
        refused = self.client.post(
            "/api/quality/assess",
            json={"registered_sample_id": self.registered_sample_id, "purpose_scope": SCOPE, "project_id": "project:sha256:" + "1" * 64},
        )
        self.assertEqual(refused.status_code, 404)

    def test_run_inventory_can_be_scoped_to_one_real_project(self) -> None:
        unscoped = self.client.get("/api/runs")
        self.assertEqual(unscoped.status_code, 404, unscoped.text)
        projects = [self._create(name) for name in ("Run inventory scope A", "Run inventory scope B")]
        run_ids: list[str] = []
        for project in projects:
            assessed = self.client.post(
                "/api/quality/assess",
                json={"registered_sample_id": self.registered_sample_id, "purpose_scope": SCOPE, "project_id": project["project_id"]},
            )
            self.assertEqual(assessed.status_code, 200, assessed.text)
            run_ids.append(assessed.json()["run_id"])

        for project, expected_run_id, excluded_run_id in (
            (projects[0], run_ids[0], run_ids[1]),
            (projects[1], run_ids[1], run_ids[0]),
        ):
            response = self.client.get(f"/api/projects/{project['project_id']}/runs")
            self.assertEqual(response.status_code, 200, response.text)
            observed = [row["run_id"] for row in response.json()]
            self.assertEqual(observed, [expected_run_id])
            self.assertNotIn(excluded_run_id, observed)


if __name__ == "__main__":
    unittest.main()
