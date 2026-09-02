"""Real-byte gates for the rights-bounded matcher checkpoint acquisition contract."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
    load_resource_registry,
)
from qualsight.matchers.acquisition import (
    ADMITTED_STATE,
    CheckpointAcquisitionError,
    CheckpointStore,
)
from qualsight.matchers.runtime import FrozenMatcher, MatcherId
from qualsight.matchers.upstream import UpstreamSources
from qualsight.security.network_guard import permit_explicit_setup_network


ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "resources" / "resource-registry.json"
MANIFEST_PATH = ROOT / "resources" / "manifests" / "matcher-checkpoint-acquisition-v1.json"
UPSTREAM_MANIFEST_PATH = ROOT / "resources" / "manifests" / "matcher-upstream-sources-v1.json"
ELASTICFACE_ID = "elasticface-arc-r100-local-candidate"
ADAFACE_ID = "adaface-ir101-local-candidate"
TRANSFACE_ID = "transface-s-glint360k-local-candidate"
MAGFACE_ID = "magface-iresnet100-ms1mv2-local-candidate"
TOPOFR_ID = "topofr-r100-glint360k-local-candidate"
TRANSFACE_B_ID = "transface-b-glint360k-local-candidate"
ELASTICFACE_SHA256 = "8ea1772a9dc0e95b77c171a1713f667f353d86ad66f2e3d418e7973360d2fb28"
ELASTICFACE_SIZE_BYTES = 261_216_371
ELASTICFACE_FILE_ID = "1n0o1SGhtr_X7Y3scI6VFIUa6KW8aAQtb"
ELASTICFACE_EVIDENCE = ROOT / "resources" / "upstream" / "elasticface" / "README.5496ec06.md"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class CheckpointAcquisitionRealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-checkpoint-acquisition-"))
        self.registry = load_resource_registry(REGISTRY_PATH)
        self.store = CheckpointStore(self.registry, MANIFEST_PATH, self.data_dir)

    def tearDown(self) -> None:
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_manifest_admits_the_six_operator_selected_research_matchers_and_preserves_rights_evidence(self) -> None:
        expected_ids = {
            ADAFACE_ID,
            ELASTICFACE_ID,
            MAGFACE_ID,
            TOPOFR_ID,
            TRANSFACE_B_ID,
            TRANSFACE_ID,
        }
        self.assertEqual(set(self.store.entries), expected_ids)
        self.assertTrue(all(self.store.entry(resource_id).admitted for resource_id in expected_ids))
        elasticface = self.store.entry(ELASTICFACE_ID)
        self.assertEqual(elasticface.admission_state, ADMITTED_STATE)
        self.assertEqual(elasticface.third_party_rights_status, "VERIFIED_LICENSE")
        self.assertEqual(elasticface.published_evidence.author_link_id, "1q3ws_BQLmgXyiy2msvHummXq4pRqc1rx")
        self.assertEqual(elasticface.published_evidence.resolved_file_id, ELASTICFACE_FILE_ID)
        self.assertEqual(
            elasticface.published_evidence.resolution_method,
            "FILE_ID_FROM_AUTHOR_LINKED_GOOGLE_DRIVE_FOLDER_METADATA",
        )
        self.assertEqual(elasticface.source.host, "drive.usercontent.google.com")
        self.assertEqual(elasticface.source.allowed_redirect_hosts, ())
        self.assertEqual(elasticface.source.observed_redirect_hosts, ())
        self.assertEqual(elasticface.artifact.sha256, ELASTICFACE_SHA256)
        self.assertEqual(elasticface.artifact.size_bytes, ELASTICFACE_SIZE_BYTES)
        self.assertEqual(self.store.entry(ADAFACE_ID).third_party_rights_status, "REPOSITORY_LICENSE_ONLY")
        self.assertEqual(self.store.entry(TRANSFACE_ID).third_party_rights_status, "NO_LICENSE_FOUND")
        self.assertEqual(self.store.entry(TRANSFACE_B_ID).third_party_rights_status, "NO_LICENSE_FOUND")
        self.assertFalse((self.data_dir / "checkpoints").exists())

    def test_verified_registered_elasticface_import_is_private_receipted_and_preferred(self) -> None:
        identity, legacy_path, location = self.store.resolve(ELASTICFACE_ID)
        self.assertEqual(location, "LEGACY_EXTERNAL")
        self.assertEqual(_sha256(legacy_path), identity.sha256)

        receipt = self.store.acquire(ELASTICFACE_ID)
        self.assertEqual(receipt["action"], "IMPORTED_VERIFIED_LEGACY")
        self.assertFalse(receipt["transport"]["network_used"])
        managed_path = self.store.managed_path(ELASTICFACE_ID)
        self.assertTrue(managed_path.is_file())
        self.assertEqual(managed_path.stat().st_size, ELASTICFACE_SIZE_BYTES)
        self.assertEqual(_sha256(managed_path), ELASTICFACE_SHA256)
        self.assertEqual(stat.S_IMODE(managed_path.stat().st_mode), 0o600)
        receipt_path = Path(receipt["path"])
        self.assertTrue(receipt_path.is_file())
        self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)
        recorded = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(recorded["receipt_id"], receipt["receipt_id"])
        self.assertEqual(recorded["artifact"]["sha256"], ELASTICFACE_SHA256)

        resolved_identity, resolved_path, resolved_location = self.store.resolve(ELASTICFACE_ID)
        self.assertEqual(resolved_identity.sha256, ELASTICFACE_SHA256)
        self.assertEqual(resolved_path, managed_path)
        self.assertEqual(resolved_location, "MANAGED")
        upstream = UpstreamSources(UPSTREAM_MANIFEST_PATH, ROOT / "var")
        matcher = FrozenMatcher(
            MatcherId.ELASTICFACE_ARC_R100,
            self.registry,
            upstream,
            device="cpu",
            checkpoint_store=self.store,
        )
        self.assertEqual(matcher.binding.checkpoint_path, str(managed_path))
        self.assertEqual(matcher.binding.checkpoint_sha256, ELASTICFACE_SHA256)

        reused = self.store.acquire(ELASTICFACE_ID)
        self.assertEqual(reused["action"], "REUSED_VERIFIED_MANAGED")
        self.assertFalse(reused["transport"]["network_used"])

    def test_drifted_managed_bytes_are_never_overwritten_or_hidden_by_legacy_fallback(self) -> None:
        target = self.store.managed_path(ELASTICFACE_ID)
        target.parent.mkdir(parents=True)
        shutil.copyfile(ELASTICFACE_EVIDENCE, target)
        before_sha256 = _sha256(target)
        before_size = target.stat().st_size

        with self.assertRaisesRegex(CheckpointAcquisitionError, "refusing to overwrite"):
            self.store.acquire(ELASTICFACE_ID)

        self.assertEqual(_sha256(target), before_sha256)
        self.assertEqual(target.stat().st_size, before_size)
        _, resolved, location = self.store.resolve(ELASTICFACE_ID)
        self.assertEqual((resolved, location), (target, "MANAGED"))
        self.assertEqual(list(target.parent.glob("*.partial")), [])
        self.assertFalse((self.data_dir / "acquisition-receipts").exists())

    def test_receipt_persistence_failure_rolls_back_only_newly_imported_payload(self) -> None:
        receipt_root = self.data_dir / "acquisition-receipts"
        shutil.copyfile(ELASTICFACE_EVIDENCE, receipt_root)
        target = self.store.managed_path(ELASTICFACE_ID)

        with self.assertRaisesRegex(CheckpointAcquisitionError, "persist checkpoint acquisition receipt"):
            self.store.acquire(ELASTICFACE_ID)

        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob("*.partial")), [])
        self.assertTrue(receipt_root.is_file())
        self.assertEqual(_sha256(receipt_root), _sha256(ELASTICFACE_EVIDENCE))

    def test_api_action_imports_real_bytes_and_records_request_receipt_and_completion(self) -> None:
        settings = Settings(
            repository_root=ROOT,
            registry_path=REGISTRY_PATH,
            data_dir=self.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"),
        )
        with TestClient(
            create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False
        ) as client:
            response = client.post(f"/api/matcher-assets/{ELASTICFACE_ID}/pull-checkpoint")
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["receipt"]["action"], "IMPORTED_VERIFIED_LEGACY")
            self.assertEqual(body["asset"]["checkpoint"]["state"], "VERIFIED")
            self.assertEqual(body["asset"]["checkpoint"]["location_state"], "MANAGED")
            self.assertNotIn("path", body["asset"]["checkpoint"])
            self.assertNotIn("managed_path", body["asset"]["checkpoint_acquisition"])
            self.assertNotIn("path", body["receipt"])
            self.assertNotIn("target_path", body["receipt"])
            managed_path = self.store.managed_path(ELASTICFACE_ID)
            self.assertTrue(managed_path.is_file())
            self.assertEqual(_sha256(managed_path), body["receipt"]["artifact"]["sha256"])
            self.assertEqual(body["event"]["event_type"], "MATCHER_CHECKPOINT_ACQUISITION_COMPLETED")
            events = client.get("/api/system-events").json()
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "MATCHER_CHECKPOINT_ACQUISITION_COMPLETED",
                    "MATCHER_CHECKPOINT_ACQUISITION_REQUESTED",
                ],
            )
            self.assertTrue(all(event["evidence_sha256"] for event in events))

    def test_request_audit_failure_prevents_checkpoint_import(self) -> None:
        settings = Settings(
            repository_root=ROOT,
            registry_path=REGISTRY_PATH,
            data_dir=self.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"),
        )
        with TestClient(
            create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False
        ) as client:
            connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
            try:
                connection.execute(
                    "CREATE TRIGGER refuse_checkpoint_event BEFORE INSERT ON system_events "
                    "BEGIN SELECT RAISE(ABORT, 'checkpoint event persistence refused'); END"
                )
                connection.commit()
                response = client.post(f"/api/matcher-assets/{ELASTICFACE_ID}/pull-checkpoint")
                self.assertEqual(response.status_code, 500, response.text)
                self.assertFalse(self.store.managed_path(ELASTICFACE_ID).exists())
            finally:
                connection.execute("DROP TRIGGER IF EXISTS refuse_checkpoint_event")
                connection.commit()
                connection.close()


@unittest.skipUnless(
    os.environ.get("QUALSIGHT_RUN_REAL_CHECKPOINT_DOWNLOAD") == "1",
    "set QUALSIGHT_RUN_REAL_CHECKPOINT_DOWNLOAD=1 to fetch the official 261216371-byte ElasticFace checkpoint",
)
class OfficialElasticFaceCheckpointNetworkGate(unittest.TestCase):
    def test_official_endpoint_streams_the_registered_bytes_without_environment_proxy(self) -> None:
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-elasticface-official-download-"))
        previous = {name: os.environ.get(name) for name in ("HTTPS_PROXY", "https_proxy")}
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
        os.environ["https_proxy"] = "http://127.0.0.1:9"
        try:
            registry = load_resource_registry(REGISTRY_PATH)
            store = CheckpointStore(registry, MANIFEST_PATH, data_dir)
            with permit_explicit_setup_network():
                receipt = store.acquire_from_network(ELASTICFACE_ID)
            target = store.managed_path(ELASTICFACE_ID)
            self.assertEqual(receipt["action"], "ACQUIRED_OFFICIAL_HTTPS")
            self.assertEqual(receipt["transport"]["observed_redirect_hosts"], [])
            self.assertEqual(receipt["transport"]["final_url"], store.entry(ELASTICFACE_ID).source.url)
            self.assertEqual(target.stat().st_size, ELASTICFACE_SIZE_BYTES)
            self.assertEqual(_sha256(target), ELASTICFACE_SHA256)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
