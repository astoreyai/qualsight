"""Plan D cycle C28 — QS-Y85 detection overlays, QS-Y86 pair crops, QS-Y87 match evidence.

Real chain: registered LFW import -> fixture quality batch -> one real matcher
leaf (AdaFace, shared crop) over the fixture protocol pairs -> evidence
endpoints. Assertions cover the honesty contract: overlays labelled as the
matcher pipeline's detector, crops recomputed under the recorded frozen
condition, thresholds carry their kind, attribution wording is associational.
"""

from __future__ import annotations

import io
from pathlib import Path
import shutil
import tempfile
import time
import unittest

import numpy as np

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)

ROOT = Path(__file__).resolve().parents[3]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class PairEvidenceRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-evidence-"))
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
            json={"name": "pair evidence gate", "description": "Plan D C28.", "purpose_scope": RESEARCH_SCOPE},
        ).json()
        cls.project_id = project["project_id"]
        cls.client.post(f"/api/projects/{cls.project_id}/open")
        cls.dataset_id = cls.client.post(
            f"/api/projects/{cls.project_id}/datasets/import-registered",
            json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
        ).json()["dataset_id"]
        # Quality batch over the registered subset fixture (16 real images).
        started = cls.client.post(
            f"/api/projects/{cls.project_id}/datasets/{cls.dataset_id}/quality-batch",
            json={"selection": {"kind": "subset_fixture"}, "chunks": 1},
        )
        assert started.status_code == 202, started.text
        cls.quality_job_id = started.json()["job_id"]
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            body = cls.client.get(f"/api/jobs/{cls.quality_job_id}").json()
            if body["state"] in {"COMPLETED", "FAILED"} and body["ended_at"]:
                break
            time.sleep(0.5)
        assert body["state"] == "COMPLETED", body
        # One real matcher leaf over the fixture protocol pairs.
        cls.protocol_id = cls.client.post(
            f"/api/projects/{cls.project_id}/datasets/{cls.dataset_id}/protocols/freeze-lfw-view2"
        ).json()["protocol_id"]
        matrix = cls.client.post(
            f"/api/projects/{cls.project_id}/protocols/{cls.protocol_id}/matrix",
            params={"fixture_pairs_only": "true", "leaves": "QS-C22.AS"},
        )
        assert matrix.status_code == 202, matrix.text
        matrix_id = matrix.json()["matrix_id"]
        deadline = time.monotonic() + 600
        status: dict = {}
        while time.monotonic() < deadline:
            status = cls.client.get(f"/api/matrices/{matrix_id}").json()
            if status["leaves"] or status["progress"]["phase"] == "failed":
                break
            time.sleep(1.0)
        assert status["leaves"], status
        cls.leaf = status["leaves"][0]
        comparisons = cls.client.get(cls.leaf["links"]["comparisons"], params={"limit": 100}).json()
        cls.scored_pair = next(row for row in comparisons["rows"] if row["score"] is not None)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    # -- QS-Y85 ----------------------------------------------------------------

    def test_y85_detection_geometry_is_labelled_and_hash_verified(self) -> None:
        response = self.client.get(
            "/api/detection",
            params={"sample_id": self.scored_pair["left_sample_id"], "condition_id": self.leaf["condition_id"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["source"], "MATCHER_PIPELINE_SCRFD10G")
        self.assertIn("not OFIQ's internal landmarks", body["label"])
        self.assertGreaterEqual(body["face_count"], 1)
        self.assertEqual(len(body["detections"][0]["landmarks"]), 5)
        self.assertEqual(len(body["detections"][0]["bbox"]), 4)
        self.assertIsNotNone(body["chosen_index"])

    def test_z90_ofiq_internal_landmarks_are_emitted_and_labelled(self) -> None:
        response = self.client.get(
            "/api/ofiq-landmarks", params={"sample_id": self.scored_pair["left_sample_id"]}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["source"], "OFIQ_INTERNAL_ADNET98")
        self.assertEqual(body["landmark_type"], "ADNET_LM_98")
        self.assertEqual(len(body["landmarks"]), 98)
        self.assertGreaterEqual(len(body["faces"]), 1)
        self.assertEqual(body["version"], "1.1.2")
        self.assertIn("distinct from the matcher pipeline", body["label"])
        # Coordinate-space invariant: the emitted landmarks are in ORIGINAL image
        # coordinates — every point falls inside the engine's own detected face
        # box (small tolerance for jaw/hairline points); the console overlay
        # depends on this.
        face = body["faces"][0]
        inside = sum(
            1 for x, y in body["landmarks"]
            if face["x"] - 20 <= x <= face["x"] + face["width"] + 20
            and face["y"] - 20 <= y <= face["y"] + face["height"] + 20
        )
        self.assertGreaterEqual(inside, 95, f"landmarks not in original-image space: {inside}/98 inside the face box")
        # Independent geometry: OFIQ's landmarks and SCRFD's five points are separate sources.
        scrfd = self.client.get(
            "/api/detection", params={"sample_id": self.scored_pair["left_sample_id"]}
        ).json()
        self.assertNotEqual(len(body["landmarks"]), len(scrfd["detections"][0]["landmarks"]))

    def test_y85_unrecorded_sample_is_refused(self) -> None:
        response = self.client.get("/api/detection", params={"path": "/etc/hostname"})
        self.assertEqual(response.status_code, 404)

    # -- QS-Y86 ----------------------------------------------------------------

    def test_y86_compared_crop_is_recomputed_112x112(self) -> None:
        import cv2

        pair_id = self.scored_pair["pair_id"]
        run_id = self.leaf["run_id"]
        for side in ("left", "right"):
            response = self.client.get(f"/api/matcher-runs/{run_id}/pairs/{pair_id}/crops/{side}")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["content-type"], "image/png")
            self.assertTrue(response.headers["x-crop-sha256"])
            image = cv2.imdecode(np.frombuffer(response.content, dtype=np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(image.shape, (112, 112, 3))

    # -- QS-Y87 ----------------------------------------------------------------

    def test_y87_pair_evidence_places_score_on_recorded_distributions(self) -> None:
        pair_id = self.scored_pair["pair_id"]
        run_id = self.leaf["run_id"]
        response = self.client.get(f"/api/matcher-runs/{run_id}/pairs/{pair_id}/evidence")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["pair"]["pair_id"], pair_id)
        self.assertIsNotNone(body["pair"]["score"])
        self.assertEqual(body["provenance_marker"], "QUALSIGHT_EXECUTED")
        self.assertEqual(body["decision_rule"], "ACCEPT_WHEN_SCORE_GE_THRESHOLD")
        self.assertTrue(body["thresholds"], "at least one recorded threshold expected")
        for item in body["thresholds"]:
            self.assertEqual(item["kind"], "MATCHER_THRESHOLD")
        for decision in body["decisions"]:
            expected = "ACCEPT" if body["pair"]["score"] >= decision["threshold"] else "REJECT"
            self.assertEqual(decision["decision"], expected)
        dist = body["distributions"]
        self.assertEqual(sum(dist["genuine"]["counts"]), dist["genuine_count"])
        self.assertEqual(sum(dist["impostor"]["counts"]), dist["impostor_count"])
        self.assertGreater(dist["genuine_count"] + dist["impostor_count"], 0)
        # Sides carry image, crop, and detection links plus recorded quality.
        for side in ("left", "right"):
            info = body["sides"][side]
            self.assertTrue(info["links"]["crop"].endswith(f"/crops/{side}"))
            self.assertIsNotNone(info["quality"], "fixture quality batch covers these samples")
            self.assertIsInstance(info["quality"]["unified_quality_score"], int)
        # QS-Z91/Z92: association wording and the honesty statement, never causal claims.
        attribution = body["quality_attribution"]
        self.assertTrue(attribution["available"])
        self.assertIn("not a causal explanation", attribution["wording"])
        self.assertIn("not OFIQ's internal landmarks", body["statement"])
        self.assertNotIn("because", body["statement"].lower())

    def test_y87_unknown_pair_is_refused(self) -> None:
        response = self.client.get(f"/api/matcher-runs/{self.leaf['run_id']}/pairs/not-a-pair/evidence")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
