"""Real OFIQ evidence contract for native units and measure failures.

The fixture is an unmodified, rights-recorded LFW image already used by the
browser acceptance gate.  OFIQ 1.1.2 naturally reports two unavailable
exposure measures for these bytes; no image or score is fabricated.
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
REAL_FAILURE_IMAGE = Path(
    "/mnt/projects/datasets/lfw/lfw_funneled/Abdoulaye_Wade/Abdoulaye_Wade_0003.jpg"
)
REAL_FAILURE_SHA256 = "98b0e314593f91c2bba4983d312c60287bcd29499d1879262ca785009012fc66"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class RealQualityMeasureMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(REAL_FAILURE_IMAGE.is_file())
        self.assertEqual(
            hashlib.sha256(REAL_FAILURE_IMAGE.read_bytes()).hexdigest(),
            REAL_FAILURE_SHA256,
        )
        self.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-quality-metadata-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=self.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"),
        )
        self.client = TestClient(create_app(settings), base_url="http://127.0.0.1")
        self.client.__enter__()
        response = self.client.post(
            "/api/projects",
            json={
                "name": "Quality metadata gate",
                "description": "Real OFIQ native-unit and failure-evidence verification.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.project_id = response.json()["project_id"]
        opened = self.client.post(f"/api/projects/{self.project_id}/open")
        self.assertEqual(opened.status_code, 200, opened.text)

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_real_failed_measures_publish_units_and_observed_failure_reasons(self) -> None:
        response = self.client.post(
            "/api/quality/assess-upload",
            params={
                "project_id": self.project_id,
                "purpose_scope": RESEARCH_SCOPE,
                "filename": REAL_FAILURE_IMAGE.name,
            },
            content=REAL_FAILURE_IMAGE.read_bytes(),
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        measures = {item["name"]: item for item in body["measures"]}
        self.assertEqual(len(measures), 28)
        self.assertTrue(all(item["native_unit"] for item in measures.values()))
        self.assertTrue(all(item["native_semantics"] for item in measures.values()))
        self.assertEqual(measures["InterEyeDistance"]["native_unit"], "pixel")
        self.assertEqual(measures["HeadPoseYaw"]["native_unit"], "degree")
        self.assertEqual(measures["DynamicRange"]["native_unit"], "bit")

        failed = {name: item for name, item in measures.items() if item["failed"]}
        self.assertEqual(
            set(failed),
            {"UnderExposurePrevention", "OverExposurePrevention"},
        )
        for name, item in failed.items():
            self.assertIsNone(item["native"])
            self.assertIsNone(item["scalar"])
            self.assertIn("OFIQ returned", item["failure_reason"])
            self.assertIn(name, item["failure_reason"])
        self.assertTrue(
            all(
                item["failure_reason"] is None
                for item in measures.values()
                if not item["failed"]
            )
        )


if __name__ == "__main__":
    unittest.main()
