"""Real-data gates for deadline-bounded dataset matcher execution."""

from __future__ import annotations

import hashlib
import inspect
import multiprocessing
from pathlib import Path
import shutil
import tempfile
import time
import unittest

import cv2
import numpy as np
from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)
from qualsight.execution.registry_binding import load_resource_registry
from qualsight.identification.search import IdentificationSearchJob
from qualsight.matchers.bounded import embed_with_deadline
from qualsight.matchers.conditions import ConditionId, preprocess
from qualsight.matchers.detection import SCRFDDetector
from qualsight.matchers.matrix import MatcherMatrixJob, SCRFD_RESOURCE_ID
from qualsight.matchers.runtime import MatcherId
from qualsight.matchers.upstream import UpstreamSources
from qualsight.transforms.degradation import DegradationAnalysisJob


ROOT = Path(__file__).resolve().parents[2]
LFW_IMAGE = Path("/mnt/projects/datasets/lfw/lfw_funneled/Abel_Pacheco/Abel_Pacheco_0001.jpg")
LFW_IMAGE_SHA256 = "8b6fff626f3d778d34027e19fc88c521a9e1307cf735b22a1406c17e55e8a967"
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class BoundedMatcherExecutionRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        payload = LFW_IMAGE.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == LFW_IMAGE_SHA256
        cls.registry = load_resource_registry(
            ROOT / "resources" / "resource-registry.json"
        )
        cls.upstream = UpstreamSources(
            ROOT / "resources" / "manifests" / "matcher-upstream-sources-v1.json",
            ROOT / "var",
        )
        for source_key in ("adaface_net",):
            cls.upstream.verify(source_key)
        scrfd = cls.registry.resource(SCRFD_RESOURCE_ID)
        detector_identity = next(
            item.identity for item in scrfd.artifacts if item.role == "MODEL"
        )
        detector = SCRFDDetector(
            Path(detector_identity.path), detector_identity.sha256
        )
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert image is not None
        prepared = preprocess(
            image,
            ConditionId.ADAPTIVE_LARGEST_JPEG95,
            detector,
        )
        assert prepared.failure is None and prepared.crop_bgr is not None
        cls.real_crop = prepared.crop_bgr[np.newaxis, ...]

    def test_real_registered_embedding_completes_and_private_handoff_is_removed(self) -> None:
        scratch_before = set((ROOT / "var").glob(".qualsight-embedding-*"))
        child_pids_before = {
            process.pid for process in multiprocessing.active_children()
        }

        embeddings = embed_with_deadline(
            matcher_id=MatcherId.ADAFACE_IR101,
            registry=self.registry,
            upstream=self.upstream,
            crops_bgr=self.real_crop,
            timeout_seconds=300.0,
            process_label="real-lfw-success-gate",
        )

        self.assertEqual(embeddings.shape, (1, 512))
        self.assertEqual(embeddings.dtype, np.float32)
        self.assertTrue(np.all(np.isfinite(embeddings)))
        self.assertAlmostEqual(float(np.linalg.norm(embeddings[0])), 1.0, places=5)
        self.assertEqual(
            set((ROOT / "var").glob(".qualsight-embedding-*")),
            scratch_before,
        )
        self.assertEqual(
            {process.pid for process in multiprocessing.active_children()},
            child_pids_before,
        )

    def test_all_dataset_matcher_jobs_require_an_explicit_execution_timeout(self) -> None:
        for job_type, execution_method in (
            (MatcherMatrixJob, MatcherMatrixJob._leaf),
            (DegradationAnalysisJob, DegradationAnalysisJob._execute),
            (IdentificationSearchJob, IdentificationSearchJob._execute),
        ):
            with self.subTest(job=job_type.__name__):
                parameter = inspect.signature(job_type).parameters[
                    "execution_timeout_seconds"
                ]
                self.assertIs(parameter.default, inspect.Parameter.empty)
                self.assertIn("embed_with_deadline", execution_method.__code__.co_names)
                self.assertNotIn("embed", execution_method.__code__.co_names)

    def test_one_leaf_matrix_completes_through_the_configured_process_boundary(self) -> None:
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-bounded-matrix-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
            scientific_workers=1,
            scientific_pending=0,
            native_execution_timeout_seconds=300.0,
        )
        try:
            with TestClient(
                create_app(settings),
                base_url="http://127.0.0.1",
                raise_server_exceptions=False,
            ) as client:
                project = client.post(
                    "/api/projects",
                    json={
                        "name": "Bounded matcher execution gate",
                        "description": "Real LFW one-leaf process-boundary validation.",
                        "purpose_scope": RESEARCH_SCOPE,
                    },
                )
                self.assertEqual(project.status_code, 201, project.text)
                project_id = project.json()["project_id"]
                opened = client.post(f"/api/projects/{project_id}/open")
                self.assertEqual(opened.status_code, 200, opened.text)
                imported = client.post(
                    f"/api/projects/{project_id}/datasets/import-registered",
                    json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
                )
                self.assertEqual(imported.status_code, 201, imported.text)
                dataset_id = imported.json()["dataset_id"]
                frozen = client.post(
                    f"/api/projects/{project_id}/datasets/{dataset_id}/protocols/freeze-lfw-view2"
                )
                self.assertEqual(frozen.status_code, 201, frozen.text)
                pulled = client.post(
                    "/api/matcher-assets/adaface-ir101-local-candidate/pull-sources"
                )
                self.assertEqual(pulled.status_code, 200, pulled.text)
                started = client.post(
                    f"/api/projects/{project_id}/protocols/{frozen.json()['protocol_id']}/matrix",
                    params={"fixture_pairs_only": "true", "leaves": "QS-C22.AS"},
                )
                self.assertEqual(started.status_code, 202, started.text)
                matrix_id = started.json()["matrix_id"]
                status: dict = {}
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline:
                    response = client.get(
                        f"/api/projects/{project_id}/matrices/{matrix_id}"
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    status = response.json()
                    if status["progress"]["phase"] in {"completed", "failed"}:
                        break
                    time.sleep(0.05)
                self.assertEqual(status["progress"]["phase"], "completed", status)
                self.assertEqual(
                    [leaf["leaf_id"] for leaf in status["leaves"]],
                    ["QS-C22.AS"],
                )
                readiness = client.get("/api/ready")
                self.assertEqual(readiness.status_code, 200, readiness.text)
                self.assertEqual(
                    readiness.json()["checks"]["scientific_capacity"]["available"],
                    1,
                )
                self.assertEqual(
                    list(data_dir.glob(".qualsight-embedding-*")),
                    [],
                )
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
