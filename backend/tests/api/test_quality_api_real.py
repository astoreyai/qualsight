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
from qualsight.api.store import RunStore
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
        cls.client = TestClient(create_app(cls.settings), base_url="http://127.0.0.1")
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def _registered_sample_id(self, resource_id: str) -> str:
        response = self.client.get("/api/samples/registered")
        self.assertEqual(response.status_code, 200, response.text)
        sample = next(
            item for item in response.json() if item["resource_id"] == resource_id
        )
        return sample["registered_sample_id"]

    def assert_public_payload_has_no_host_paths(self, value: object) -> None:
        private_prefixes = (
            "/mnt/",
            "/home/",
            "/tmp/",
            "file://",
            "C:\\",
            "\\\\",
            "~/",
        )

        def visit(item: object) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    normalized = str(key).lower()
                    self.assertNotEqual(normalized, "path")
                    self.assertFalse(normalized.endswith("_path"), key)
                    self.assertNotIn("directory", normalized, key)
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)
            elif isinstance(item, str):
                self.assertFalse(
                    item.startswith(private_prefixes),
                    f"public payload disclosed a host path: {item}",
                )

        visit(value)

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

    def test_api_contract_and_documentation_name_only_opaque_registered_samples(self) -> None:
        version = self.client.get("/api/version")
        self.assertEqual(version.status_code, 200, version.text)
        self.assertEqual(version.json()["contract_version"], "2.0.0-beta.1")
        self.assertEqual(
            version.json()["compatibility"],
            "all /api routes in this beta implement contract version 2.0.0-beta.1",
        )
        self.assertEqual(
            version.headers["X-QualSight-API-Version"], "2.0.0-beta.1"
        )

        openapi = self.client.get("/openapi.json").json()
        for route in ("/api/samples/image", "/api/detection", "/api/ofiq-landmarks"):
            parameters = openapi["paths"][route]["get"]["parameters"]
            self.assertIn("registered_sample_id", {item["name"] for item in parameters})
            self.assertNotIn("path", {item["name"] for item in parameters})
        assess_schema = openapi["components"]["schemas"]["AssessRequest"]
        self.assertIn("registered_sample_id", assess_schema["required"])
        self.assertNotIn("image_path", assess_schema["properties"])

        api_document = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")
        self.assertIn("API contract `2.0.0-beta.1`", api_document)
        self.assertIn("registered_sample_id", api_document)
        self.assertNotIn("?path=", api_document)

    def test_health_and_readiness_public_records_do_not_disclose_host_paths(self) -> None:
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200, health.text)
        ready = self.client.get("/api/ready")
        self.assertEqual(ready.status_code, 200, ready.text)

        health_body = health.json()
        ready_body = ready.json()
        self.assertNotIn("database_path", health_body)
        self.assertNotIn("path", ready_body["checks"]["storage"])
        for artifact in ready_body["checks"]["core_runtime"]["artifacts"]:
            self.assertEqual(set(artifact), {"role", "resource_id", "sha256", "ok"})

        public_text = json.dumps(
            {"health": health_body, "ready": ready_body}, sort_keys=True
        )
        self.assertNotIn(str(self.data_dir), public_text)
        self.assertNotIn(str(self.settings.database_path), public_text)
        registry = json.loads(
            (ROOT / "resources" / "resource-registry.json").read_text(encoding="utf-8")
        )
        runtime = next(
            resource
            for resource in registry["resources"]
            if resource["resource_id"] == OFIQ_V112_RUNTIME_RESOURCE_ID
        )
        for artifact in runtime["artifacts"]:
            if artifact["role"] in {"BINARY", "CONFIGURATION"}:
                self.assertNotIn(artifact["path"], public_text)

    def test_readiness_rejects_same_size_tampering_of_a_real_runtime_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qualsight-ready-runtime-") as temporary:
            root = Path(temporary)
            registry = json.loads(
                (ROOT / "resources" / "resource-registry.json").read_text(encoding="utf-8")
            )
            runtime = next(
                resource
                for resource in registry["resources"]
                if resource["resource_id"] == OFIQ_V112_RUNTIME_RESOURCE_ID
            )
            copied_configuration: Path | None = None
            path_replacements: dict[str, str] = {}
            for artifact in runtime["artifacts"]:
                if artifact["role"] not in {"BINARY", "CONFIGURATION"}:
                    continue
                source = Path(artifact["path"])
                target = root / artifact["role"].lower() / source.name
                target.parent.mkdir()
                shutil.copy2(source, target)
                path_replacements[str(source)] = str(target)
                artifact["path"] = str(target)
                if artifact["role"] == "CONFIGURATION":
                    copied_configuration = target
            def replace_registered_locations(value: object) -> object:
                if isinstance(value, dict):
                    return {
                        key: replace_registered_locations(child)
                        for key, child in value.items()
                    }
                if isinstance(value, list):
                    return [replace_registered_locations(child) for child in value]
                if isinstance(value, str):
                    return path_replacements.get(value, value)
                return value

            registry = replace_registered_locations(registry)
            self.assertIsInstance(registry, dict)
            self.assertIsNotNone(copied_configuration)
            registry_path = root / "resource-registry.json"
            registry_path.write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            settings = Settings(
                repository_root=ROOT,
                registry_path=registry_path,
                data_dir=root / "data",
                runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
                input_resource_ids=(OFIQ_INPUT_RESOURCE_ID,),
            )
            with TestClient(
                create_app(settings), base_url="http://127.0.0.1"
            ) as client:
                self.assertEqual(client.get("/api/ready").status_code, 200)
                assert copied_configuration is not None
                original = copied_configuration.read_bytes()
                self.assertTrue(original)
                copied_configuration.write_bytes(
                    bytes((original[0] ^ 1,)) + original[1:]
                )
                drifted = client.get("/api/ready")
                self.assertEqual(drifted.status_code, 503, drifted.text)
                configuration = next(
                    item
                    for item in drifted.json()["checks"]["core_runtime"]["artifacts"]
                    if item["role"] == "CONFIGURATION"
                )
                self.assertFalse(configuration["ok"])

    def test_registered_samples_are_listed_with_their_scopes(self) -> None:
        response = self.client.get("/api/samples/registered")
        self.assertEqual(response.status_code, 200)
        by_resource = {item["resource_id"]: item for item in response.json()}
        sample = by_resource[OFIQ_INPUT_RESOURCE_ID]
        self.assertEqual(
            set(sample),
            {
                "registered_sample_id",
                "resource_id",
                "resource_state",
                "approved_scopes",
                "display_name",
                "role",
                "sha256",
                "size_bytes",
                "media_type",
            },
        )
        self.assertTrue(sample["registered_sample_id"].startswith("registered-sample:sha256:"))
        self.assertEqual(sample["approved_scopes"], [CONFORMANCE_SCOPE])
        self.assertEqual(sample["display_name"], "r-01-frontal.png")
        self.assertEqual(sample["role"], "DATA")
        self.assertEqual(sample["media_type"], "image/png")
        self.assert_public_payload_has_no_host_paths(response.json())

    def test_registered_image_bytes_are_served_locally_without_caching(self) -> None:
        registered_sample_id = self._registered_sample_id(OFIQ_INPUT_RESOURCE_ID)
        response = self.client.get(
            "/api/samples/image",
            params={"registered_sample_id": registered_sample_id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), hashlib.sha256(Path(R01).read_bytes()).hexdigest())
        denied = self.client.get(
            "/api/samples/image",
            params={"registered_sample_id": "registered-sample:sha256:" + "0" * 64},
        )
        self.assertEqual(denied.status_code, 404)
        retired_path_contract = self.client.get("/api/samples/image", params={"path": R01})
        self.assertEqual(retired_path_contract.status_code, 422)

    def test_assess_executes_persists_and_returns_the_typed_result(self) -> None:
        registered_sample_id = self._registered_sample_id(OFIQ_INPUT_RESOURCE_ID)
        response = self.client.post(
            "/api/quality/assess",
            json={
                "registered_sample_id": registered_sample_id,
                "purpose_scope": CONFORMANCE_SCOPE,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["kind"], "ENGINE_MEASUREMENT")
        self.assertEqual(body["policy"], "NONE_APPLIED")
        self.assertEqual(body["engine_version"], "1.1.2")
        self.assertEqual(body["registered_sample_id"], registered_sample_id)
        self.assertNotIn("image_path", body)
        self.assert_public_payload_has_no_host_paths(body)
        measures = {m["name"]: m for m in body["measures"]}
        self.assertEqual(len(measures), 28)
        self.assertEqual(measures["UnifiedQualityScore"]["native"], 26.390884)
        self.assertEqual(measures["UnifiedQualityScore"]["scalar"], 79)
        self.assertEqual(body["failed_measures"], [])
        run_id = body["run_id"]
        self.assertTrue(run_id.startswith("run:sha256:"))

        # The persisted Run is re-read from SQLite and agrees with the response.
        stored = self.client.get(body["provenance"]["run"])
        self.assertEqual(stored.status_code, 200, stored.text)
        stored_body = stored.json()
        self.assertEqual(stored_body["evidence_sha256"], body["evidence_sha256"])
        public_run = stored_body["run"]
        self.assertEqual(public_run["run_id"], run_id)
        self.assert_public_payload_has_no_host_paths(stored_body)
        self.assertNotIn(R01, json.dumps(stored_body, sort_keys=True))
        by_metric = {m["metric_id"]: m["value"] for m in public_run["metrics"]}
        for name, measure in measures.items():
            self.assertEqual(by_metric[f"{name}.native"], measure["native"])
            self.assertEqual(by_metric[f"{name}.scalar"], measure["scalar"])
        self.assertEqual(public_run["quality_engine"]["resource_id"], OFIQ_V112_RUNTIME_RESOURCE_ID)

        # The SQLite row's evidence digest is the digest of the stored bytes.
        connection = sqlite3.connect(str(self.settings.database_path))
        row = connection.execute(
            "SELECT record_json, evidence_sha256 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        connection.close()
        self.assertIsNotNone(row)
        self.assertEqual(hashlib.sha256(row[0].encode("utf-8")).hexdigest(), row[1])
        run = CompletedRun.model_validate_json(row[0])
        self.assertEqual(run.run_id, run_id)
        self.assertEqual(run.artifacts[0].uri, R01)
        self.assertEqual(run.quality_engine.resource_id, OFIQ_V112_RUNTIME_RESOURCE_ID)

        # Provenance links resolve to recorded entities.
        for key in ("project", "experiment", "sample", "audit_events"):
            linked = self.client.get(body["provenance"][key])
            self.assertEqual(linked.status_code, 200, (key, linked.text))
            self.assert_public_payload_has_no_host_paths(linked.json())
            self.assertNotIn(R01, linked.text)
        audit = self.client.get(body["provenance"]["audit_events"]).json()
        self.assertEqual(audit[0]["subject_id"], run_id)
        summaries = self.client.get(f"/api/projects/{run.project_id}/runs").json()
        self.assertIn(run_id, {item["run_id"] for item in summaries})

        # No source image was copied into the data directory.
        copied = [p for p in self.data_dir.rglob("*") if p.suffix.lower() in {".png", ".jpg", ".jpeg"}]
        self.assertEqual(copied, [])

    def test_scope_refusal_and_unregistered_paths_are_typed_errors(self) -> None:
        registered_sample_id = self._registered_sample_id(OFIQ_INPUT_RESOURCE_ID)
        refused = self.client.post(
            "/api/quality/assess",
            json={"registered_sample_id": registered_sample_id, "purpose_scope": RESEARCH_SCOPE},
        )
        self.assertEqual(refused.status_code, 403, refused.text)
        missing = self.client.post(
            "/api/quality/assess",
            json={
                "registered_sample_id": "registered-sample:sha256:" + "0" * 64,
                "purpose_scope": CONFORMANCE_SCOPE,
            },
        )
        self.assertEqual(missing.status_code, 404, missing.text)
        retired_path_contract = self.client.post(
            "/api/quality/assess",
            json={"image_path": R01, "purpose_scope": CONFORMANCE_SCOPE},
        )
        self.assertEqual(retired_path_contract.status_code, 422)
        project = self.client.post(
            "/api/projects",
            json={"name": "unknown Run scope", "description": "Real API 404 boundary.", "purpose_scope": RESEARCH_SCOPE},
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["project_id"]
        unknown = self.client.get(f"/api/projects/{project_id}/runs/run:sha256:" + "0" * 64)
        self.assertEqual(unknown.status_code, 404)

    def test_unreadable_registered_input_is_an_explicit_assessment_failure(self) -> None:
        registered_sample_id = self._registered_sample_id("lfw-funneled-local-candidate")
        response = self.client.post(
            "/api/quality/assess",
            json={
                "registered_sample_id": registered_sample_id,
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        body = response.json()
        self.assertEqual(
            set(body),
            {
                "error",
                "registered_sample_id",
                "engine_version",
                "exit_code",
                "reason_code",
                "message",
                "failure_event_id",
                "failure_evidence_sha256",
            },
        )
        self.assertEqual(body["error"], "ASSESSMENT_FAILED")
        self.assertEqual(body["registered_sample_id"], registered_sample_id)
        self.assertNotIn("image_path", body)
        self.assertEqual(body["exit_code"], 0)
        self.assertEqual(body["reason_code"], "ENGINE_RETURNED_NO_MEASURES")
        self.assertEqual(
            body["message"],
            "The registered engine completed without usable quality measures.",
        )
        self.assertEqual(body["engine_version"], "1.1.2")
        self.assert_public_payload_has_no_host_paths(body)
        private_store = RunStore(self.settings.database_path)
        try:
            private_event = next(
                item
                for item in private_store.system_events()
                if item["system_event_id"] == body["failure_event_id"]
            )
        finally:
            private_store.close()
        self.assertIn(LFW_ARCHIVE, private_event["details"]["image_path"])
        self.assertIn("[ERROR]", private_event["details"]["reason"])
        self.assertEqual(
            body["failure_evidence_sha256"], private_event["evidence_sha256"]
        )


if __name__ == "__main__":
    unittest.main()
