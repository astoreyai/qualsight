"""Network integration gate for the real author-published verification protocols."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

import numpy as np
from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.api.store import RunStore
from qualsight.dataset_acquisition import DatasetAcquisitionError, VerificationDatasetStore
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
    load_resource_registry,
)
from qualsight.matchers.conditions import ConditionId
from qualsight.matchers.matrix import MatcherMatrixJob
from qualsight.matchers.runtime import MatcherId
from qualsight.matchers.upstream import UpstreamSources
from qualsight.security.network_guard import permit_explicit_setup_network


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "resources" / "manifests" / "verification-dataset-acquisition-v1.json"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


@unittest.skipUnless(
    os.environ.get("QUALSIGHT_RUN_REAL_DATASET_DOWNLOAD") == "1"
    or bool(os.environ.get("QUALSIGHT_VERIFICATION_DATASET_CACHE_ARCHIVE")),
    "set QUALSIGHT_RUN_REAL_DATASET_DOWNLOAD=1 to fetch the official archive or "
    "QUALSIGHT_VERIFICATION_DATASET_CACHE_ARCHIVE to its verified local cache",
)
class OfficialVerificationDatasetNetworkGate(unittest.TestCase):
    def _store_with_cached_archive(self, data_dir: Path) -> VerificationDatasetStore:
        cached_archive = os.environ.get("QUALSIGHT_VERIFICATION_DATASET_CACHE_ARCHIVE")
        if not cached_archive:
            self.skipTest(
                "receipt durability gates require the pinned real archive through "
                "QUALSIGHT_VERIFICATION_DATASET_CACHE_ARCHIVE"
            )
        source = Path(cached_archive).resolve(strict=True)
        store = VerificationDatasetStore(MANIFEST, data_dir)
        store.archive_path.parent.mkdir(parents=True)
        os.link(source, store.archive_path)
        return store

    def test_receipt_publication_failure_rolls_back_new_real_extraction(self) -> None:
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-verification-receipt-failure-"))
        try:
            store = self._store_with_cached_archive(data_dir)
            receipt_root_blocker = data_dir / "acquisition-receipts"
            receipt_root_blocker.touch(mode=0o600)

            failure: BaseException | None = None
            try:
                store.acquire()
            except BaseException as error:  # noqa: BLE001 - assert the public failure contract below
                failure = error

            self.assertIsInstance(failure, Exception)
            self.assertIsInstance(failure, DatasetAcquisitionError)
            self.assertTrue(store.archive_path.is_file())
            self.assertFalse(
                store.extraction_root.exists(),
                "a newly published real extraction survived absent its acquisition receipt",
            )
            self.assertTrue(receipt_root_blocker.is_file())
            for addon_id in store.protocols:
                self.assertNotEqual(store.status(addon_id)["runtime_state"], "READY")
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_restart_reconciles_verified_real_bytes_only_through_a_durable_receipt(self) -> None:
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-verification-receipt-reconcile-"))
        try:
            store = self._store_with_cached_archive(data_dir)
            first = store.acquire()
            receipt_path = Path(first["path"])
            extraction_stat = store.extraction_root.stat()
            receipt_path.unlink()

            restarted = VerificationDatasetStore(MANIFEST, data_dir)
            for addon_id in restarted.protocols:
                self.assertNotEqual(
                    restarted.status(addon_id)["runtime_state"],
                    "READY",
                    "verified bytes without a valid durable receipt were admitted after restart",
                )

            reconciled = restarted.acquire()
            self.assertEqual(reconciled["action"], "RECONCILED_VERIFIED_LOCAL_STATE")
            self.assertTrue(Path(reconciled["path"]).is_file())
            self.assertEqual(
                (restarted.extraction_root.stat().st_dev, restarted.extraction_root.stat().st_ino),
                (extraction_stat.st_dev, extraction_stat.st_ino),
                "reconciliation replaced already verified publisher bytes",
            )
            for addon_id in restarted.protocols:
                self.assertEqual(restarted.status(addon_id, verify_images=True)["runtime_state"], "READY")
            restarted.rollback_acquisition(reconciled)
            self.assertFalse(Path(reconciled["path"]).exists())
            self.assertEqual(
                (restarted.extraction_root.stat().st_dev, restarted.extraction_root.stat().st_ino),
                (extraction_stat.st_dev, extraction_stat.st_ino),
            )
            for addon_id in restarted.protocols:
                self.assertNotEqual(restarted.status(addon_id)["runtime_state"], "READY")
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_api_completion_event_failure_rolls_back_real_acquisition(self) -> None:
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-verification-event-failure-"))
        try:
            dataset_store = self._store_with_cached_archive(data_dir)
            settings = Settings(
                repository_root=ROOT,
                registry_path=ROOT / "resources" / "resource-registry.json",
                data_dir=data_dir,
                runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
                input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"),
            )
            with TestClient(
                create_app(settings),
                base_url="http://127.0.0.1",
                raise_server_exceptions=False,
            ) as client:
                connection = sqlite3.connect(data_dir / "qualsight.sqlite3")
                try:
                    connection.execute(
                        "CREATE TRIGGER refuse_dataset_completion_event "
                        "BEFORE INSERT ON system_events "
                        "WHEN NEW.event_type = 'VERIFICATION_DATASET_ACQUISITION_COMPLETED' "
                        "BEGIN SELECT RAISE(ABORT, 'refuse dataset completion event'); END"
                    )
                    connection.commit()
                finally:
                    connection.close()

                response = client.post(
                    "/api/dataset-assets/magface-lfw-verification-v1/pull"
                )
                self.assertEqual(response.status_code, 500, response.text)

            receipt_dir = data_dir / "acquisition-receipts" / "datasets"
            self.assertEqual(list(receipt_dir.glob("*.json")), [])
            self.assertFalse(
                dataset_store.extraction_root.exists(),
                "completion-event failure left a newly installed real extraction",
            )
            self.assertTrue(dataset_store.archive_path.is_file())
            for addon_id in dataset_store.protocols:
                self.assertNotEqual(
                    dataset_store.status(addon_id)["runtime_state"], "READY"
                )
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_archive_downloads_validates_and_executes_every_matcher(self) -> None:
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-verification-dataset-network-"))
        try:
            store = VerificationDatasetStore(MANIFEST, data_dir)
            cached_archive = os.environ.get("QUALSIGHT_VERIFICATION_DATASET_CACHE_ARCHIVE")
            if cached_archive:
                source = Path(cached_archive).resolve(strict=True)
                archive_dir = store.archive_path.parent
                archive_dir.mkdir(parents=True)
                os.link(source, store.archive_path)
                receipt = store.acquire()
                self.assertEqual(receipt["action"], "REUSED_VERIFIED_ARCHIVE")
                self.assertEqual(
                    receipt["transport"],
                    {"method": "LOCAL_VERIFIED_REUSE", "network_used": False},
                )
            else:
                with permit_explicit_setup_network():
                    receipt = store.acquire()
                self.assertEqual(receipt["action"], "ACQUIRED_OFFICIAL_HTTPS")
            self.assertEqual(receipt["archive"]["size_bytes"], 249_538_560)
            self.assertEqual(
                receipt["archive"]["sha256"],
                "d16039c1aa5b801b3a121c1748e3b49ccefe3e5b72f3f918d8a0cd09bb597b96",
            )

            self.assertEqual(receipt["extraction_manifest"]["file_count"], 38_006)
            self.assertTrue(Path(receipt["extraction_manifest"]["path"]).is_file())
            for addon_id in sorted(store.protocols):
                status = store.status(addon_id, verify_images=True)
                self.assertEqual(status["runtime_state"], "READY", status)

            settings = Settings(
                repository_root=ROOT,
                registry_path=ROOT / "resources" / "resource-registry.json",
                data_dir=data_dir,
                runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
                input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"),
            )
            imported: dict[str, dict[str, str]] = {}
            with TestClient(
                create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False
            ) as client:
                project_response = client.post(
                    "/api/projects",
                    json={
                        "name": "Official verification add-on execution gate",
                        "description": "Real author-published pair archives through project import and matcher execution.",
                        "purpose_scope": RESEARCH_SCOPE,
                    },
                )
                self.assertEqual(project_response.status_code, 201, project_response.text)
                project_id = project_response.json()["project_id"]
                self.assertEqual(client.post(f"/api/projects/{project_id}/open").status_code, 200)

                for addon_id, definition in sorted(store.protocols.items()):
                    response = client.post(
                        f"/api/projects/{project_id}/dataset-assets/{addon_id}/import"
                    )
                    self.assertEqual(response.status_code, 201, response.text)
                    body = response.json()
                    self.assertEqual(body["samples"], definition.sample_count)
                    self.assertEqual(body["pairs"], definition.pair_count)
                    self.assertIsNone(body["dataset"]["subject_count"])
                    self.assertIsNone(body["protocol"]["fold_count"])
                    self.assertIsNone(body["protocol"]["unique_subject_count"])
                    imported[addon_id] = {
                        "dataset_id": body["dataset"]["dataset_id"],
                        "protocol_id": body["protocol"]["protocol_id"],
                    }

                replay = client.post(
                    f"/api/projects/{project_id}/dataset-assets/magface-lfw-verification-v1/import"
                )
                self.assertEqual(replay.status_code, 201, replay.text)
                self.assertEqual(
                    replay.json()["dataset"]["dataset_id"],
                    imported["magface-lfw-verification-v1"]["dataset_id"],
                )
                self.assertEqual(
                    replay.json()["protocol"]["protocol_id"],
                    imported["magface-lfw-verification-v1"]["protocol_id"],
                )

                second_response = client.post(
                    "/api/projects",
                    json={
                        "name": "Pair add-on project isolation gate",
                        "description": "Verify project-scoped identities for the same acquired real protocol.",
                        "purpose_scope": RESEARCH_SCOPE,
                    },
                )
                self.assertEqual(second_response.status_code, 201, second_response.text)
                second_project_id = second_response.json()["project_id"]
                self.assertEqual(client.post(f"/api/projects/{second_project_id}/open").status_code, 200)
                isolated = client.post(
                    f"/api/projects/{second_project_id}/dataset-assets/magface-lfw-verification-v1/import"
                )
                self.assertEqual(isolated.status_code, 201, isolated.text)
                self.assertNotEqual(
                    isolated.json()["dataset"]["dataset_id"],
                    imported["magface-lfw-verification-v1"]["dataset_id"],
                )
                self.assertNotEqual(
                    isolated.json()["protocol"]["protocol_id"],
                    imported["magface-lfw-verification-v1"]["protocol_id"],
                )

            registry = load_resource_registry(ROOT / "resources" / "resource-registry.json")
            upstream = UpstreamSources(
                ROOT / "resources" / "manifests" / "matcher-upstream-sources-v1.json",
                ROOT / "var",
            )
            with permit_explicit_setup_network():
                upstream.acquire()
            run_store = RunStore(data_dir / "qualsight.sqlite3")
            try:
                project = run_store.project(project_id)
                self.assertIsNotNone(project)
                executions = 0
                for identities in imported.values():
                    dataset = run_store.dataset(identities["dataset_id"])
                    protocol = run_store.protocol(identities["protocol_id"])
                    self.assertIsNotNone(dataset)
                    self.assertIsNotNone(protocol)
                    protocol_pairs = run_store.protocol_pairs(
                        identities["protocol_id"], limit=1_000_000
                    )
                    selected_pairs = [
                        next(pair for pair in protocol_pairs if pair.label.value == label)
                        for label in ("MATED", "NON_MATED")
                    ]
                    job = MatcherMatrixJob(
                        registry=registry,
                        registry_path=ROOT / "resources" / "resource-registry.json",
                        repository_root=ROOT,
                        upstream=upstream,
                        store=run_store,
                        project=project,
                        dataset=dataset,
                        protocol=protocol,
                        pairs=selected_pairs,
                        job_root=data_dir / "network-matrix",
                        matchers=tuple(MatcherId),
                        conditions=(ConditionId.PREALIGNED_ARCFACE112,),
                        selection_rule=(
                            "one author-published mated pair and one author-published non-mated pair, "
                            "selected in source order as a real-data execution fixture"
                        ),
                        execution_timeout_seconds=7200.0,
                    )
                    leaves = job.run()
                    self.assertEqual(len(leaves), 6)
                    for leaf in leaves:
                        self.assertEqual(leaf.condition_id, ConditionId.PREALIGNED_ARCFACE112)
                        self.assertEqual(leaf.comparison_count, 2)
                        self.assertEqual(leaf.scored_count, 2)
                        self.assertEqual(leaf.failed_count, 0)
                        self.assertEqual(
                            leaf.metrics["by_policy"]["EXCLUDE_FAILED_TRIALS"]["view2_tenfold"]["status"],
                            "NOT_ASSESSED",
                        )
                        embeddings = np.load(leaf.embeddings_path)
                        self.assertEqual(embeddings.shape[1], 512)
                        self.assertTrue(np.isfinite(embeddings).all())
                        self.assertTrue(
                            np.allclose(np.linalg.norm(embeddings, axis=1), 1.0, atol=1e-5)
                        )
                        executions += 1
                self.assertEqual(executions, 18)
            finally:
                run_store.close()

            drifted_image = store.protocol_root("magface-lfw-verification-v1") / "imgs" / "0.jpg"
            original_bytes = drifted_image.read_bytes()
            # One-byte-short subsample of the real publisher image; no fabricated test bytes.
            drifted_image.write_bytes(original_bytes[:-1])
            with TestClient(
                create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False
            ) as client:
                drift_project = client.post(
                    "/api/projects",
                    json={
                        "name": "Extracted-file drift rollback gate",
                        "description": "Refuse a changed real extracted image without partial project records.",
                        "purpose_scope": RESEARCH_SCOPE,
                    },
                ).json()["project_id"]
                self.assertEqual(client.post(f"/api/projects/{drift_project}/open").status_code, 200)
                refused = client.post(
                    f"/api/projects/{drift_project}/dataset-assets/magface-lfw-verification-v1/import"
                )
                self.assertEqual(refused.status_code, 409, refused.text)
                self.assertIn("manifested file size differs", refused.json()["detail"])
                self.assertEqual(client.get(f"/api/projects/{drift_project}/datasets").json(), [])
                self.assertEqual(client.get(f"/api/projects/{drift_project}/protocols").json(), [])
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
