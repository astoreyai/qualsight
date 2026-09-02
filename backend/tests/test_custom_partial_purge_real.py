"""Project purge must remove interrupted custom scientific-output stages.

Every biometric byte in this gate is an unchanged image from the registered
local LFW resource.  Each worker is terminated with SIGKILL only after its
real direct-comparison or derived-image output stage exists on disk.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)
from qualsight.execution.prepared_output import (
    PreparedOutputError,
    discover_partial_output_identity,
)


ROOT = Path(__file__).resolve().parents[2]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
LFW_TREE = Path("/mnt/projects/datasets/lfw/lfw_funneled")
ABEL_LEFT = LFW_TREE / "Abel_Pacheco" / "Abel_Pacheco_0001.jpg"
ABEL_RIGHT = LFW_TREE / "Abel_Pacheco" / "Abel_Pacheco_0004.jpg"
ABEL_LEFT_SHA256 = "8b6fff626f3d778d34027e19fc88c521a9e1307cf735b22a1406c17e55e8a967"
ABEL_RIGHT_SHA256 = "8050daa859e7e05aedd5c7ec3851e5d5bddcb2b9797d23958664ec2958a230ae"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class CustomPartialPurgeRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-custom-partial-purge-"))
        cls.settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        with cls._client() as client:
            cls.direct_project_id = cls._create_project(
                client, "direct-pair interrupted-stage purge gate"
            )
            left = cls._upload_real_photo(
                client, cls.direct_project_id, ABEL_LEFT, ABEL_LEFT_SHA256
            )
            right = cls._upload_real_photo(
                client, cls.direct_project_id, ABEL_RIGHT, ABEL_RIGHT_SHA256
            )
            pair = client.post(
                f"/api/projects/{cls.direct_project_id}/owner-pair-admissions",
                json={
                    "left_sample_id": left["sample_id"],
                    "right_sample_id": right["sample_id"],
                    "relationship": "SAME_PERSON",
                },
            )
            assert pair.status_code == 201, pair.text
            cls.pair_id = pair.json()["pair_id"]
            pulled = client.post(
                "/api/matcher-assets/adaface-ir101-local-candidate/pull-sources"
            )
            assert pulled.status_code == 200, pulled.text

            cls.derived_project_id = cls._create_project(
                client, "derived-set interrupted-stage purge gate"
            )
            imported = client.post(
                f"/api/projects/{cls.derived_project_id}/datasets/import-registered",
                json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
            )
            assert imported.status_code == 201, imported.text
            cls.derived_dataset_id = imported.json()["dataset_id"]
            samples = client.get(
                f"/api/projects/{cls.derived_project_id}/datasets/"
                f"{cls.derived_dataset_id}/samples",
                params={"limit": 1},
            )
            assert samples.status_code == 200, samples.text
            cls.derived_sample_id = samples.json()[0]["sample_id"]

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    @classmethod
    def _client(cls) -> TestClient:
        return TestClient(
            create_app(cls.settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        )

    @staticmethod
    def _create_project(client: TestClient, name: str) -> str:
        created = client.post(
            "/api/projects",
            json={
                "name": name,
                "description": "Real LFW interrupted-output purge evidence.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["project_id"]
        opened = client.post(f"/api/projects/{project_id}/open")
        assert opened.status_code == 200, opened.text
        return project_id

    @staticmethod
    def _upload_real_photo(
        client: TestClient,
        project_id: str,
        source: Path,
        expected_sha256: str,
    ) -> dict:
        payload = source.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        admitted = client.post(
            "/api/quality/assess-upload",
            params={
                "project_id": project_id,
                "purpose_scope": RESEARCH_SCOPE,
                "filename": source.name,
            },
            content=payload,
            headers={"content-type": "application/octet-stream"},
        )
        assert admitted.status_code == 201, admitted.text
        return admitted.json()

    @staticmethod
    def _worker_environment() -> dict[str, str]:
        return {
            **os.environ,
            "PYTHONPATH": str(ROOT / "backend" / "src") + os.pathsep + str(ROOT),
        }

    def _start_worker(self, source: str, *arguments: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                source,
                str(ROOT),
                str(self.data_dir),
                *arguments,
            ],
            cwd=ROOT,
            env=self._worker_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

    def _kill_after_private_stage(
        self,
        worker: subprocess.Popen[str],
        output_root: Path,
        *,
        operation_kind: str,
        project_id: str,
        timeout: float,
    ) -> Path:
        stage: Path | None = None
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline and worker.poll() is None:
                if output_root.is_dir():
                    for candidate in output_root.glob(".*.partial"):
                        if not candidate.is_dir():
                            continue
                        try:
                            identity = discover_partial_output_identity(candidate)
                        except PreparedOutputError:
                            continue
                        if (
                            identity["project_id"] == project_id
                            and identity["operation_kind"] == operation_kind
                        ):
                            stage = candidate
                            break
                if stage is not None:
                    os.killpg(worker.pid, signal.SIGKILL)
                    worker.wait(timeout=30)
                    break
                time.sleep(0.002)
            if stage is None:
                output = worker.communicate(timeout=30)[0]
                self.fail(
                    "real scientific operation did not expose an interruptible "
                    f"private output stage: {output}"
                )
        finally:
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGKILL)
                worker.wait(timeout=30)
            if worker.stdout is not None:
                worker.stdout.close()
        self.assertEqual(worker.returncode, -signal.SIGKILL)
        assert stage is not None
        self.assertTrue(stage.is_dir())
        return stage

    def _assert_verified_purge_removes(self, project_id: str, stage: Path) -> None:
        with self._client() as restarted:
            purged = restarted.post(
                f"/api/projects/{project_id}/purge",
                json={"confirm": project_id},
            )
            self.assertEqual(purged.status_code, 201, purged.text)
            self.assertTrue(purged.json()["verified"], purged.json())
        self.assertFalse(
            stage.exists(),
            "verified project purge retained an interrupted biometric output stage",
        )

    def test_direct_pair_private_stage_is_removed_after_real_process_death(self) -> None:
        worker_source = """
from pathlib import Path
import sys
import time
from fastapi.testclient import TestClient
from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID

root = Path(sys.argv[1])
data_dir = Path(sys.argv[2])
project_id = sys.argv[3]
pair_id = sys.argv[4]
settings = Settings(
    repository_root=root,
    registry_path=root / "resources" / "resource-registry.json",
    data_dir=data_dir,
    runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
    input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"),
)
with TestClient(create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
    response = client.post(
        f"/api/projects/{project_id}/owner-pair-comparisons",
        json={
            "pair_id": pair_id,
            "matcher_id": "adaface-ir101-local-candidate",
            "preprocessing_id": "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1",
            "threshold": {
                "contract_id": "matcher_threshold",
                "kind": "FIXED_VALUE",
                "value": 0.713,
            },
        },
    )
    if response.status_code != 202:
        raise RuntimeError(response.text)
    while True:
        time.sleep(1)
"""
        worker = self._start_worker(
            worker_source, self.direct_project_id, self.pair_id
        )
        stage = self._kill_after_private_stage(
            worker,
            self.data_dir / "direct-pair-comparisons",
            operation_kind="DIRECT_PAIR_COMPARISON",
            project_id=self.direct_project_id,
            timeout=120,
        )
        self._assert_verified_purge_removes(self.direct_project_id, stage)

    def test_derived_set_private_stage_is_removed_after_real_process_death(self) -> None:
        worker_source = """
from pathlib import Path
import sys
from fastapi.testclient import TestClient
from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID

root = Path(sys.argv[1])
data_dir = Path(sys.argv[2])
project_id = sys.argv[3]
dataset_id = sys.argv[4]
sample_id = sys.argv[5]
settings = Settings(
    repository_root=root,
    registry_path=root / "resources" / "resource-registry.json",
    data_dir=data_dir,
    runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
    input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"),
)
with TestClient(create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
    response = client.post(
        f"/api/projects/{project_id}/datasets/{dataset_id}/transformations",
        json={
            "selection": "sample_ids",
            "sample_ids": [sample_id],
            "transforms": [{"kind": "jpeg", "parameters": list(range(1, 101))}],
        },
    )
    if response.status_code != 201:
        raise RuntimeError(response.text)
"""
        worker = self._start_worker(
            worker_source,
            self.derived_project_id,
            self.derived_dataset_id,
            self.derived_sample_id,
        )
        stage = self._kill_after_private_stage(
            worker,
            self.data_dir / "derived",
            operation_kind="DERIVED_SET",
            project_id=self.derived_project_id,
            timeout=120,
        )
        self._assert_verified_purge_removes(self.derived_project_id, stage)


if __name__ == "__main__":
    unittest.main()
