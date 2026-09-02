"""Manage runtime evidence from a live QualSight application process."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.api.store import RunStore
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)
from qualsight.matchers.upstream import UpstreamSources


ROOT = Path(__file__).resolve().parents[3]


class RealManageRuntimeEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-manage-runtime-"))
        self.settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=self.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID,),
        )
        self.client = TestClient(
            create_app(self.settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        )
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_readiness_publishes_the_live_process_lease_without_an_invented_expiry(self) -> None:
        response = self.client.get("/api/ready")

        self.assertEqual(response.status_code, 200, response.text)
        lease = response.json()["checks"]["data_directory_lease"]
        self.assertEqual(
            set(lease),
            {
                "state",
                "owner",
                "pid",
                "acquired_at",
                "expires_at",
                "release_condition",
            },
        )
        self.assertEqual(lease["state"], "HELD")
        self.assertEqual(lease["owner"], "api-server")
        self.assertEqual(lease["pid"], os.getpid())
        self.assertIsNotNone(datetime.fromisoformat(lease["acquired_at"]))
        self.assertIsNone(lease["expires_at"])
        self.assertEqual(lease["release_condition"], "PROCESS_SHUTDOWN")

    def assert_public_payload_has_no_host_locations(self, value: object) -> None:
        def visit(item: object) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    normalized = str(key).lower()
                    self.assertNotEqual(normalized, "path")
                    self.assertFalse(normalized.endswith("_path"), key)
                    self.assertNotIn("directory", normalized, key)
                    self.assertNotEqual(normalized, "local_root")
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)
            elif isinstance(item, str):
                self.assertNotIn(str(self.data_dir), item)
                self.assertNotIn(str(ROOT), item)

        visit(value)

    def test_manage_catalogs_and_system_events_are_path_free_public_projections(self) -> None:
        occurred_at = datetime.now().astimezone().isoformat()
        details = {
            "reason": f"verified local runtime inspection failed at {self.data_dir / 'models' / 'checkpoint.pth'}"
        }
        event_basis = {
            "actor_id": self.settings.actor,
            "event_type": "REAL_PATH_PRIVACY_REGRESSION",
            "subject_id": OFIQ_V112_RUNTIME_RESOURCE_ID,
            "occurred_at": occurred_at,
            "details": details,
        }
        event_id = "system-event:sha256:" + hashlib.sha256(
            json.dumps(
                event_basis,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        private_store = RunStore(self.settings.database_path)
        try:
            private_store.record_system_event(
                {"system_event_id": event_id, **event_basis}
            )
        finally:
            private_store.close()

        for endpoint in ("/api/matcher-assets", "/api/addons", "/api/system-events"):
            with self.subTest(endpoint=endpoint):
                response = self.client.get(endpoint)
                self.assertEqual(response.status_code, 200, response.text)
                self.assert_public_payload_has_no_host_locations(response.json())

        private_store = RunStore(self.settings.database_path)
        try:
            private_event = next(
                item
                for item in private_store.system_events()
                if item["system_event_id"] == event_id
            )
        finally:
            private_store.close()
        self.assertIn(str(self.data_dir), private_event["details"]["reason"])

    def test_source_pull_refuses_a_nonregular_target_without_disclosing_its_path(self) -> None:
        upstream = UpstreamSources(
            ROOT / "resources" / "manifests" / "matcher-upstream-sources-v1.json",
            self.data_dir,
        )
        target = upstream.path_for("adaface_net")
        target.parent.mkdir(parents=True)
        real_source = upstream.cache_root / target.relative_to(upstream.root)
        self.assertTrue(real_source.is_file())
        source = upstream.sources["adaface_net"]
        self.assertEqual(real_source.stat().st_size, source.size_bytes)
        self.assertEqual(hashlib.sha256(real_source.read_bytes()).hexdigest(), source.sha256)
        target.symlink_to(real_source)

        response = self.client.post(
            "/api/matcher-assets/adaface-ir101-local-candidate/pull-sources",
            json={},
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertNotIn(str(target), response.text)
        self.assertNotIn("/mnt/", response.text)
        self.assertNotIn("/tmp/", response.text)


if __name__ == "__main__":
    unittest.main()
