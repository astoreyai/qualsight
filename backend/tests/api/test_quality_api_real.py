"""Real API vertical slice: registered image → registered engine → immutable Run.

The FastAPI app runs in-process with a fresh SQLite store in a temporary data
directory; every assessment launches the real installed OFIQ v1.1.2 runtime.
"""

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
from qualsight.domain.run import CompletedRun
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)


ROOT = Path(__file__).resolve().parents[3]
R01 = "/mnt/projects/02_perception_biometrics/OFIQ-Project/data/tests/images/r-01-frontal.png"
LFW_ARCHIVE = "/mnt/projects/datasets/lfw/lfw-funneled.tgz"
CONFORMANCE_SCOPE = "REPRODUCE_OFIQ_CONFORMANCE_TESTS"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class RealQualityApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-api-"))
        cls.settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"),
        )
        cls.client = TestClient(create_app(cls.settings))
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def test_health_reports_the_registered_engine_and_registry_identity(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["engine"]["resource_id"], OFIQ_V112_RUNTIME_RESOURCE_ID)
        self.assertEqual(body["engine"]["version"], "1.1.2")
        self.assertEqual(body["engine"]["state"], "APPROVED_LOCAL_RESEARCH_USE")
        registry_sha = hashlib.sha256((ROOT / "resources" / "resource-registry.json").read_bytes()).hexdigest()
        self.assertEqual(body["registry_sha256"], registry_sha)

    def test_registered_samples_are_listed_with_their_scopes(self) -> None:
        response = self.client.get("/api/samples/registered")
        self.assertEqual(response.status_code, 200)
        by_path = {item["path"]: item for item in response.json()}
        self.assertIn(R01, by_path)
        self.assertEqual(by_path[R01]["approved_scopes"], [CONFORMANCE_SCOPE])
        self.assertEqual(by_path[R01]["media_type"], "image/png")

    def test_registered_image_bytes_are_served_locally_without_caching(self) -> None:
        response = self.client.get("/api/samples/image", params={"path": R01})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), hashlib.sha256(Path(R01).read_bytes()).hexdigest())
        denied = self.client.get("/api/samples/image", params={"path": str(ROOT / "PRD.md")})
        self.assertEqual(denied.status_code, 404)

    def test_assess_executes_persists_and_returns_the_typed_result(self) -> None:
        response = self.client.post(
            "/api/quality/assess", json={"image_path": R01, "purpose_scope": CONFORMANCE_SCOPE}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["kind"], "ENGINE_MEASUREMENT")
        self.assertEqual(body["policy"], "NONE_APPLIED")
        self.assertEqual(body["engine_version"], "1.1.2")
        self.assertEqual(body["image_path"], R01)
        measures = {m["name"]: m for m in body["measures"]}
        self.assertEqual(len(measures), 28)
        self.assertEqual(measures["UnifiedQualityScore"]["native"], 26.390884)
        self.assertEqual(measures["UnifiedQualityScore"]["scalar"], 79)
        self.assertEqual(body["failed_measures"], [])
        run_id = body["run_id"]
        self.assertTrue(run_id.startswith("run:sha256:"))

        # The persisted Run is re-read from SQLite and agrees with the response.
        stored = self.client.get(f"/api/runs/{run_id}")
        self.assertEqual(stored.status_code, 200, stored.text)
        stored_body = stored.json()
        self.assertEqual(stored_body["evidence_sha256"], body["evidence_sha256"])
        run = CompletedRun.model_validate_json(json.dumps(stored_body["run"]))
        self.assertEqual(run.run_id, run_id)
        by_metric = {m.metric_id: m.value for m in run.metrics}
        for name, measure in measures.items():
            self.assertEqual(by_metric[f"{name}.native"], measure["native"])
            self.assertEqual(by_metric[f"{name}.scalar"], measure["scalar"])
        self.assertEqual(run.artifacts[0].uri, R01)
        self.assertEqual(run.quality_engine.resource_id, OFIQ_V112_RUNTIME_RESOURCE_ID)

        # The SQLite row's evidence digest is the digest of the stored bytes.
        connection = sqlite3.connect(str(self.settings.database_path))
        row = connection.execute(
            "SELECT record_json, evidence_sha256 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        connection.close()
        self.assertIsNotNone(row)
        self.assertEqual(hashlib.sha256(row[0].encode("utf-8")).hexdigest(), row[1])
        self.assertEqual(json.loads(row[0])["run_id"], run_id)

        # Provenance links resolve to recorded entities.
        for key in ("project", "experiment", "sample", "audit_events"):
            linked = self.client.get(body["provenance"][key])
            self.assertEqual(linked.status_code, 200, (key, linked.text))
        audit = self.client.get(body["provenance"]["audit_events"]).json()
        self.assertEqual(audit[0]["subject_id"], run_id)
        summaries = self.client.get("/api/runs").json()
        self.assertIn(run_id, {item["run_id"] for item in summaries})

        # No source image was copied into the data directory.
        copied = [p for p in self.data_dir.rglob("*") if p.suffix.lower() in {".png", ".jpg", ".jpeg"}]
        self.assertEqual(copied, [])

    def test_scope_refusal_and_unregistered_paths_are_typed_errors(self) -> None:
        refused = self.client.post(
            "/api/quality/assess", json={"image_path": R01, "purpose_scope": RESEARCH_SCOPE}
        )
        self.assertEqual(refused.status_code, 403, refused.text)
        missing = self.client.post(
            "/api/quality/assess", json={"image_path": str(ROOT / "PRD.md"), "purpose_scope": CONFORMANCE_SCOPE}
        )
        self.assertEqual(missing.status_code, 404, missing.text)
        unknown = self.client.get("/api/runs/run:sha256:" + "0" * 64)
        self.assertEqual(unknown.status_code, 404)

    def test_unreadable_registered_input_is_an_explicit_assessment_failure(self) -> None:
        response = self.client.post(
            "/api/quality/assess", json={"image_path": LFW_ARCHIVE, "purpose_scope": RESEARCH_SCOPE}
        )
        self.assertEqual(response.status_code, 422, response.text)
        body = response.json()
        self.assertEqual(body["error"], "ASSESSMENT_FAILED")
        self.assertEqual(body["exit_code"], 0)
        self.assertIn("[ERROR]", body["reason"])
        self.assertEqual(body["engine_version"], "1.1.2")


if __name__ == "__main__":
    unittest.main()
