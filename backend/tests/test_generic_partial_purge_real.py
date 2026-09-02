"""Verified purge coverage for generic interrupted scientific-output stages."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.api.store import RunStore
from qualsight.execution.prepared_output import (
    STAGING_IDENTITY_FILENAME,
    create_staging_directory,
    discover_partial_output_identity,
)
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)
from qualsight.security.purge import purge_targets


ROOT = Path(__file__).resolve().parents[2]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
REAL_LFW_IMAGE = Path(
    "/mnt/projects/datasets/lfw/lfw_funneled/Abel_Pacheco/Abel_Pacheco_0001.jpg"
)
REAL_LFW_SHA256 = "8b6fff626f3d778d34027e19fc88c521a9e1307cf735b22a1406c17e55e8a967"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


@unittest.skipUnless(REAL_LFW_IMAGE.is_file(), "registered local LFW image is unavailable")
class GenericPartialPurgeRealTests(unittest.TestCase):
    def test_sigkill_analysis_stage_is_purged_but_untrusted_and_foreign_stages_are_retained(self) -> None:
        real_bytes = REAL_LFW_IMAGE.read_bytes()
        self.assertEqual(hashlib.sha256(real_bytes).hexdigest(), REAL_LFW_SHA256)
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-generic-partial-purge-"))
        self.addCleanup(shutil.rmtree, data_dir, True)
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
            project_id = self._project(client, "owned interrupted analysis stage")
            foreign_project_id = self._project(client, "foreign retained analysis stage")

        result_id = f"analysis:sha256:{REAL_LFW_SHA256}"
        worker_source = """
from pathlib import Path
import shutil
import sys
import time
from qualsight.execution.prepared_output import create_staging_directory

data_dir = Path(sys.argv[1])
source = Path(sys.argv[2])
project_id = sys.argv[3]
result_id = sys.argv[4]
request_sha256 = sys.argv[5]
target = data_dir / "analyses" / result_id.replace(":", "_")
stage = create_staging_directory(
    target,
    operation_kind="UTILITY_ANALYSIS",
    result_id=result_id,
    project_id=project_id,
    request_sha256=request_sha256,
)
shutil.copyfile(source, stage / "registered-lfw-source.jpg")
print(stage, flush=True)
while True:
    time.sleep(1)
"""
        environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "backend" / "src") + os.pathsep + str(ROOT),
        }
        worker = subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker_source,
                str(data_dir),
                str(REAL_LFW_IMAGE),
                project_id,
                result_id,
                REAL_LFW_SHA256,
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            assert worker.stdout is not None
            stage_line = worker.stdout.readline().strip()
            self.assertTrue(stage_line, "analysis worker did not publish its private stage")
            owned_stage = Path(stage_line)
            self.assertEqual(
                discover_partial_output_identity(owned_stage)["project_id"],
                project_id,
            )
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait(timeout=30)
        finally:
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGKILL)
                worker.wait(timeout=30)
            if worker.stdout is not None:
                worker.stdout.close()
        self.assertEqual(worker.returncode, -signal.SIGKILL)
        self.assertTrue(owned_stage.is_dir())

        analyses_root = data_dir / "analyses"
        foreign_target = analyses_root / f"foreign-analysis-{REAL_LFW_SHA256}"
        foreign_stage = create_staging_directory(
            foreign_target,
            operation_kind="UTILITY_ANALYSIS",
            result_id=f"foreign-analysis:sha256:{REAL_LFW_SHA256}",
            project_id=foreign_project_id,
            request_sha256=REAL_LFW_SHA256,
        )
        shutil.copyfile(REAL_LFW_IMAGE, foreign_stage / "registered-lfw-source.jpg")

        malformed_stage = analyses_root / f".malformed-{REAL_LFW_SHA256}.x.partial"
        malformed_stage.mkdir(mode=0o700)
        shutil.copyfile(
            REAL_LFW_IMAGE,
            malformed_stage / STAGING_IDENTITY_FILENAME,
        )

        store = RunStore(data_dir / "qualsight.sqlite3")
        try:
            interrupted_result_id = (
                f"interrupted-analysis:sha256:{REAL_LFW_SHA256}"
            )
            interrupted_target = analyses_root / interrupted_result_id.replace(
                ":", "_"
            )
            store.admit_operation(
                operation_id=f"operation:sha256:{REAL_LFW_SHA256}",
                operation_kind="UTILITY_ANALYSIS",
                project_id=project_id,
                request={"source_sha256": REAL_LFW_SHA256},
                output_path=interrupted_target,
                result_id=interrupted_result_id,
            )
            incomplete_marker_stage = analyses_root / (
                f".{interrupted_target.name}.interrupted.partial"
            )
            incomplete_marker_stage.mkdir(mode=0o700)
            shutil.copyfile(
                REAL_LFW_IMAGE,
                incomplete_marker_stage / STAGING_IDENTITY_FILENAME,
            )
            targets = purge_targets(store, data_dir, project_id)
        finally:
            store.close()
        self.assertIn(str(owned_stage), targets["directories"])
        self.assertIn(str(incomplete_marker_stage), targets["directories"])
        self.assertNotIn(str(foreign_stage), targets["directories"])
        self.assertNotIn(str(malformed_stage), targets["directories"])

        with TestClient(create_app(settings), base_url="http://127.0.0.1") as restarted:
            purged = restarted.post(
                f"/api/projects/{project_id}/purge", json={"confirm": project_id}
            )
            self.assertEqual(purged.status_code, 201, purged.text)
            self.assertTrue(purged.json()["verified"], purged.json())
        self.assertFalse(owned_stage.exists())
        self.assertFalse(incomplete_marker_stage.exists())
        self.assertTrue(foreign_stage.is_dir())
        self.assertTrue(malformed_stage.is_dir())

    @staticmethod
    def _project(client: TestClient, name: str) -> str:
        created = client.post(
            "/api/projects",
            json={
                "name": name,
                "description": "Real registered LFW partial-output purge evidence.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["project_id"]
        opened = client.post(f"/api/projects/{project_id}/open")
        assert opened.status_code == 200, opened.text
        return project_id


if __name__ == "__main__":
    unittest.main()
