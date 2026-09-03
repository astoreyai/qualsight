"""Crash recovery at filesystem-promotion/SQLite-record boundaries.

Every biometric byte used here comes unchanged from the registered local LFW
tree.  The worker processes are terminated with SIGKILL only after the real
output bytes have reached their final directory while a real SQLite writer
lock prevents the corresponding metadata transaction from starting.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import shutil

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.api.store import RunStore
from qualsight.domain.primitives import CanonicalJson
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)
from qualsight.execution.prepared_output import (
    PreparedOutputError,
    create_staging_directory,
    discard_staging_directory,
    load_prepared_output,
    prepare_and_publish,
)
from qualsight.matchers.direct_pair import recover_prepared_direct_pair
from qualsight.transforms.derived import _recover_prepared_set


ROOT = Path(__file__).resolve().parents[2]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
LFW_TREE = Path("/mnt/projects/datasets/lfw/lfw_funneled")
ABEL_LEFT = LFW_TREE / "Abel_Pacheco" / "Abel_Pacheco_0001.jpg"
ABEL_RIGHT = LFW_TREE / "Abel_Pacheco" / "Abel_Pacheco_0004.jpg"
ABEL_LEFT_SHA256 = "8b6fff626f3d778d34027e19fc88c521a9e1307cf735b22a1406c17e55e8a967"
ABEL_RIGHT_SHA256 = "8050daa859e7e05aedd5c7ec3851e5d5bddcb2b9797d23958664ec2958a230ae"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


def _publish_worker_response(path: Path, payload: dict[str, str]) -> None:
    """Atomically publish the real accepted-response identity to the parent."""

    body = (CanonicalJson.from_value(payload).text + "\n").encode("utf-8")
    partial = path.with_name(f".{path.name}.writing")
    descriptor = os.open(
        partial,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(partial, path, follow_symlinks=False)
    finally:
        partial.unlink(missing_ok=True)
    directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _read_worker_response_id(path: Path, field: str) -> str | None:
    if not path.exists():
        return None
    observed = path.lstat()
    if not stat.S_ISREG(observed.st_mode):
        raise AssertionError("worker response is not a regular file")
    if stat.S_IMODE(observed.st_mode) != 0o600:
        raise AssertionError("worker response mode differs")
    if observed.st_nlink not in {1, 2}:
        raise AssertionError("worker response link count differs")
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    if set(payload) != {field}:
        raise AssertionError("worker response fields differ")
    if text != CanonicalJson.from_value(payload).text + "\n":
        raise AssertionError("worker response is not canonical")
    value = payload[field]
    if not isinstance(value, str) or not value:
        raise AssertionError("worker response identity is absent")
    return value


class OutputCrashRecoveryRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-output-crash-"))
        cls.settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        with cls._client() as client:
            project = client.post(
                "/api/projects",
                json={
                    "name": "Output crash-recovery gate",
                    "description": "Real LFW process-death recovery boundary.",
                    "purpose_scope": RESEARCH_SCOPE,
                },
            )
            assert project.status_code == 201, project.text
            cls.project_id = project.json()["project_id"]
            opened = client.post(f"/api/projects/{cls.project_id}/open")
            assert opened.status_code == 200, opened.text
            imported = client.post(
                f"/api/projects/{cls.project_id}/datasets/import-registered",
                json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
            )
            assert imported.status_code == 201, imported.text
            cls.dataset_id = imported.json()["dataset_id"]
            samples = client.get(
                f"/api/projects/{cls.project_id}/datasets/{cls.dataset_id}/samples",
                params={"limit": 1},
            )
            assert samples.status_code == 200, samples.text
            cls.source_sample_id = samples.json()[0]["sample_id"]

            left = cls._upload(client, ABEL_LEFT, ABEL_LEFT_SHA256)
            right = cls._upload(client, ABEL_RIGHT, ABEL_RIGHT_SHA256)
            pair = client.post(
                f"/api/projects/{cls.project_id}/owner-pair-admissions",
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

    @classmethod
    def _upload(cls, client: TestClient, source: Path, expected_sha256: str) -> dict:
        payload = source.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        response = client.post(
            "/api/quality/assess-upload",
            params={
                "project_id": cls.project_id,
                "purpose_scope": RESEARCH_SCOPE,
                "filename": source.name,
            },
            content=payload,
            headers={"content-type": "application/octet-stream"},
        )
        assert response.status_code == 201, response.text
        return response.json()

    @staticmethod
    def _wait_for(predicate, *, timeout: float, description: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError(f"timed out waiting for {description}")

    def _worker(self, source: str, *arguments: str) -> subprocess.Popen[str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = f"{ROOT / 'backend' / 'src'}:{ROOT}"
        return subprocess.Popen(
            [sys.executable, "-c", source, *arguments],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _kill(self, worker: subprocess.Popen[str]) -> tuple[str, str]:
        os.kill(worker.pid, signal.SIGKILL)
        stdout, stderr = worker.communicate(timeout=30)
        self.assertEqual(worker.returncode, -signal.SIGKILL, (stdout, stderr))
        return stdout, stderr

    def _worker_response_id(self, path: Path, field: str) -> str | None:
        return _read_worker_response_id(path, field)

    def _utility_inputs(
        self, *, leaves: str = "QS-C22.AS"
    ) -> tuple[str, str, dict]:
        with self._client() as client:
            protocol = client.post(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/protocols/freeze-lfw-view2"
            )
            self.assertEqual(protocol.status_code, 201, protocol.text)
            protocol_id = protocol.json()["protocol_id"]
            if "QS-C22.ES" in leaves:
                pulled = client.post(
                    "/api/matcher-assets/elasticface-arc-r100-local-candidate/pull-sources"
                )
                self.assertEqual(pulled.status_code, 200, pulled.text)
            matrix = client.post(
                f"/api/projects/{self.project_id}/protocols/{protocol_id}/matrix",
                params={"fixture_pairs_only": "true", "leaves": leaves},
            )
            self.assertEqual(matrix.status_code, 202, matrix.text)
            matrix_id = matrix.json()["matrix_id"]
            matrix_status: dict = {}
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                response = client.get(
                    f"/api/projects/{self.project_id}/matrices/{matrix_id}"
                )
                self.assertEqual(response.status_code, 200, response.text)
                matrix_status = response.json()
                if matrix_status["progress"]["phase"] in {"completed", "failed"}:
                    break
                time.sleep(0.05)
            self.assertEqual(
                matrix_status.get("progress", {}).get("phase"),
                "completed",
                matrix_status,
            )
            matcher_run_id = matrix_status["leaves"][0]["run_id"]

            quality = client.post(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/quality-batch",
                json={"selection": {"kind": "subset_fixture"}, "chunks": 1},
            )
            self.assertEqual(quality.status_code, 202, quality.text)
            quality_job_id = quality.json()["job_id"]
            quality_status: dict = {}
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                response = client.get(
                    f"/api/projects/{self.project_id}/jobs/{quality_job_id}"
                )
                self.assertEqual(response.status_code, 200, response.text)
                quality_status = response.json()
                if quality_status["ended_at"] is not None:
                    break
                time.sleep(0.05)
            self.assertEqual(quality_status.get("state"), "COMPLETED", quality_status)

        request = {
            "matcher_run_id": matcher_run_id,
            "quality_job_id": quality_job_id,
            "pair_quality_policy": "pair.quality.min",
            "matcher_threshold": 0.8,
            "quality_threshold": 40.0,
            "bootstrap_replicates": 50,
            "seed": 11,
        }
        return protocol_id, matrix_id, request

    def test_cross_matcher_final_output_is_reconciled_after_real_process_death(self) -> None:
        _, matrix_id, utility_request = self._utility_inputs(
            leaves="QS-C22.AS,QS-C22.ES"
        )
        request = {
            "matrix_id": matrix_id,
            "quality_job_id": utility_request["quality_job_id"],
            "quality_measure": "UnifiedQualityScore",
            "pair_quality_policy": "pair.quality.min",
            "target_fmr": 0.001,
            "partial_interval": 0.2,
            "bootstrap_replicates": 50,
            "seed": 13,
        }
        worker_source = """
import json
from pathlib import Path
import sys
import time
from fastapi.testclient import TestClient
from backend.tests.test_output_crash_recovery_real import _publish_worker_response
from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID

root = Path(sys.argv[1])
data_dir = Path(sys.argv[2])
project_id = sys.argv[3]
request = json.loads(sys.argv[4])
response_marker = Path(sys.argv[5])
settings = Settings(repository_root=root, registry_path=root / "resources" / "resource-registry.json", data_dir=data_dir, runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID, input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"))
with TestClient(create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
    response = client.post(f"/api/projects/{project_id}/cross-matcher", json=request)
    if response.status_code != 202:
        raise RuntimeError(response.text)
    analysis_id = response.json()["analysis_id"]
    _publish_worker_response(response_marker, {"analysis_id": analysis_id})
    while True:
        state = client.get(f"/api/projects/{project_id}/cross-matcher/{analysis_id}").json()["state"]
        if state in {"COMPLETED", "FAILED", "INTERRUPTED"}:
            raise RuntimeError(f"cross-matcher analysis reached {state} before parent termination")
        time.sleep(0.05)
"""
        database = self.data_dir / "qualsight.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TRIGGER stall_cross_matcher_recording "
            "BEFORE INSERT ON cross_matcher_analyses BEGIN "
            "SELECT sum(length(a.record_json) + length(b.record_json)) "
            "FROM samples AS a CROSS JOIN samples AS b; END"
        )
        connection.commit()
        connection.close()
        response_marker = self.data_dir / ".cross-matcher-worker-response.json"
        worker = self._worker(
            worker_source,
            str(ROOT),
            str(self.data_dir),
            self.project_id,
            json.dumps(request, sort_keys=True),
            str(response_marker),
        )
        analysis_id: str | None = None

        def final_files_exist() -> bool:
            nonlocal analysis_id
            analysis_id = self._worker_response_id(response_marker, "analysis_id")
            if analysis_id is None:
                return False
            directory = self.data_dir / "analyses" / analysis_id.replace(":", "_")
            return all(
                (directory / name).is_file()
                for name in (
                    "contrast_pairs.parquet",
                    "crossmatcher.json",
                    ".qualsight-prepared-output.json",
                )
            )

        try:
            self._wait_for(
                final_files_exist,
                timeout=300,
                description="real cross-matcher artifacts promoted before SQLite",
            )
            assert analysis_id is not None
            directory = self.data_dir / "analyses" / analysis_id.replace(":", "_")
            hashes_before = {
                name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                for name in ("contrast_pairs.parquet", "crossmatcher.json")
            }
            self._kill(worker)
            connection = sqlite3.connect(database)
            try:
                self.assertIsNone(
                    connection.execute(
                        "SELECT analysis_id FROM cross_matcher_analyses "
                        "WHERE analysis_id = ?",
                        (analysis_id,),
                    ).fetchone()
                )
            finally:
                connection.close()
        finally:
            if worker.poll() is None:
                self._kill(worker)
            connection = sqlite3.connect(database)
            connection.execute("DROP TRIGGER IF EXISTS stall_cross_matcher_recording")
            connection.commit()
            connection.close()

        assert analysis_id is not None
        with self._client() as restarted:
            recovered = restarted.post(
                f"/api/projects/{self.project_id}/cross-matcher", json=request
            )
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(recovered.json()["analysis_id"], analysis_id)
            self.assertEqual(recovered.json()["state"], "COMPLETED", recovered.text)
            detail = restarted.get(
                f"/api/projects/{self.project_id}/cross-matcher/{analysis_id}"
            )
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json()["state"], "COMPLETED", detail.text)
        directory = self.data_dir / "analyses" / analysis_id.replace(":", "_")
        self.assertEqual(
            {
                name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                for name in ("contrast_pairs.parquet", "crossmatcher.json")
            },
            hashes_before,
        )

    def test_identification_final_output_is_reconciled_after_real_process_death(self) -> None:
        with self._client() as client:
            frozen = client.post(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/protocols/freeze-lfw-identification",
                json={"selection": "subset_fixture", "rank_depth": 20},
            )
            self.assertEqual(frozen.status_code, 201, frozen.text)
            protocol_id = frozen.json()["protocol_id"]
        request = {
            "matcher_id": "adaface-ir101-local-candidate",
            "condition_id": "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1",
        }
        worker_source = """
import json
from pathlib import Path
import sys
import time
from fastapi.testclient import TestClient
from backend.tests.test_output_crash_recovery_real import _publish_worker_response
from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID

root = Path(sys.argv[1])
data_dir = Path(sys.argv[2])
project_id = sys.argv[3]
protocol_id = sys.argv[4]
request = json.loads(sys.argv[5])
response_marker = Path(sys.argv[6])
settings = Settings(repository_root=root, registry_path=root / "resources" / "resource-registry.json", data_dir=data_dir, runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID, input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"))
with TestClient(create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
    response = client.post(f"/api/projects/{project_id}/identification-protocols/{protocol_id}/search", json=request)
    if response.status_code != 202:
        raise RuntimeError(response.text)
    search_id = response.json()["search_id"]
    _publish_worker_response(response_marker, {"search_id": search_id})
    while True:
        state = client.get(f"/api/projects/{project_id}/identification-searches/{search_id}").json()["state"]
        if state in {"COMPLETED", "FAILED", "INTERRUPTED"}:
            raise RuntimeError(f"identification search reached {state} before parent termination")
        time.sleep(0.05)
"""
        database = self.data_dir / "qualsight.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TRIGGER stall_identification_recording "
            "BEFORE INSERT ON identification_searches BEGIN "
            "SELECT sum(length(a.record_json) + length(b.record_json)) "
            "FROM samples AS a CROSS JOIN samples AS b; END"
        )
        connection.commit()
        connection.close()
        response_marker = self.data_dir / ".identification-worker-response.json"
        worker = self._worker(
            worker_source,
            str(ROOT),
            str(self.data_dir),
            self.project_id,
            protocol_id,
            json.dumps(request, sort_keys=True),
            str(response_marker),
        )
        search_id: str | None = None

        def final_files_exist() -> bool:
            nonlocal search_id
            search_id = self._worker_response_id(response_marker, "search_id")
            if search_id is None:
                return False
            directory = (
                self.data_dir / "identification" / search_id.replace(":", "_")
            )
            return all(
                (directory / name).is_file()
                for name in (
                    "candidates.parquet",
                    "gallery_manifest.tsv",
                    "identification_metrics.json",
                    "probe_manifest.tsv",
                    ".qualsight-prepared-output.json",
                )
            )

        try:
            self._wait_for(
                final_files_exist,
                timeout=300,
                description="real identification artifacts promoted before SQLite",
            )
            assert search_id is not None
            directory = self.data_dir / "identification" / search_id.replace(":", "_")
            hashes_before = {
                name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                for name in (
                    "candidates.parquet",
                    "gallery_manifest.tsv",
                    "identification_metrics.json",
                    "probe_manifest.tsv",
                )
            }
            self._kill(worker)
            connection = sqlite3.connect(database)
            try:
                self.assertIsNone(
                    connection.execute(
                        "SELECT search_id FROM identification_searches "
                        "WHERE search_id = ?",
                        (search_id,),
                    ).fetchone()
                )
            finally:
                connection.close()
        finally:
            if worker.poll() is None:
                self._kill(worker)
            connection = sqlite3.connect(database)
            connection.execute("DROP TRIGGER IF EXISTS stall_identification_recording")
            connection.commit()
            connection.close()

        assert search_id is not None
        with self._client() as restarted:
            recovered = restarted.post(
                f"/api/projects/{self.project_id}/identification-protocols/{protocol_id}/search",
                json=request,
            )
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(recovered.json()["search_id"], search_id)
            self.assertEqual(recovered.json()["state"], "COMPLETED", recovered.text)
            detail = restarted.get(
                f"/api/projects/{self.project_id}/identification-searches/{search_id}"
            )
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json()["state"], "COMPLETED", detail.text)
        directory = self.data_dir / "identification" / search_id.replace(":", "_")
        self.assertEqual(
            {
                name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                for name in (
                    "candidates.parquet",
                    "gallery_manifest.tsv",
                    "identification_metrics.json",
                    "probe_manifest.tsv",
                )
            },
            hashes_before,
        )

    def test_degradation_final_output_is_reconciled_after_real_process_death(self) -> None:
        with self._client() as client:
            protocol = client.post(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/protocols/freeze-lfw-view2"
            )
            self.assertEqual(protocol.status_code, 201, protocol.text)
            protocol_id = protocol.json()["protocol_id"]
            derived = client.post(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/transformations",
                json={
                    "selection": "subset_fixture",
                    "transforms": [{"kind": "jpeg", "parameters": [90]}],
                },
            )
            self.assertEqual(derived.status_code, 201, derived.text)
            derived_record = derived.json()
            quality = client.post(
                f"/api/projects/{self.project_id}/datasets/{derived_record['derived_dataset_id']}/quality-batch",
                json={"selection": {"kind": "all"}, "chunks": 1},
            )
            self.assertEqual(quality.status_code, 202, quality.text)
            quality_job_id = quality.json()["job_id"]
            quality_status: dict = {}
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                response = client.get(
                    f"/api/projects/{self.project_id}/jobs/{quality_job_id}"
                )
                self.assertEqual(response.status_code, 200, response.text)
                quality_status = response.json()
                if quality_status["ended_at"] is not None:
                    break
                time.sleep(0.05)
            self.assertEqual(quality_status.get("state"), "COMPLETED", quality_status)

        request = {
            "derived_set_id": derived_record["derived_set_id"],
            "quality_job_id": quality_job_id,
            "baseline_quality_job_id": None,
            "matcher_id": "adaface-ir101-local-candidate",
            "condition_id": "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1",
            "matcher_threshold": 0.8,
            "protocol_id": protocol_id,
        }
        worker_source = """
import json
from pathlib import Path
import sys
import time
from fastapi.testclient import TestClient
from backend.tests.test_output_crash_recovery_real import _publish_worker_response
from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID

root = Path(sys.argv[1])
data_dir = Path(sys.argv[2])
project_id = sys.argv[3]
request = json.loads(sys.argv[4])
response_marker = Path(sys.argv[5])
settings = Settings(repository_root=root, registry_path=root / "resources" / "resource-registry.json", data_dir=data_dir, runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID, input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"))
with TestClient(create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
    response = client.post(f"/api/projects/{project_id}/degradation-analyses", json=request)
    if response.status_code != 202:
        raise RuntimeError(response.text)
    analysis_id = response.json()["analysis_id"]
    _publish_worker_response(response_marker, {"analysis_id": analysis_id})
    while True:
        state = client.get(f"/api/projects/{project_id}/degradation-analyses/{analysis_id}").json()["state"]
        if state in {"COMPLETED", "FAILED", "INTERRUPTED"}:
            raise RuntimeError(f"degradation analysis reached {state} before parent termination")
        time.sleep(0.05)
"""
        database = self.data_dir / "qualsight.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TRIGGER stall_degradation_recording "
            "BEFORE INSERT ON degradation_analyses BEGIN "
            "SELECT sum(length(a.record_json) + length(b.record_json)) "
            "FROM samples AS a CROSS JOIN samples AS b; END"
        )
        connection.commit()
        connection.close()
        response_marker = self.data_dir / ".degradation-worker-response.json"
        worker = self._worker(
            worker_source,
            str(ROOT),
            str(self.data_dir),
            self.project_id,
            json.dumps(request, sort_keys=True),
            str(response_marker),
        )
        analysis_id: str | None = None

        def final_files_exist() -> bool:
            nonlocal analysis_id
            analysis_id = self._worker_response_id(response_marker, "analysis_id")
            if analysis_id is None:
                return False
            directory = self.data_dir / "analyses" / analysis_id.replace(":", "_")
            return all(
                (directory / name).is_file()
                for name in (
                    "degradation.json",
                    ".qualsight-prepared-output.json",
                )
            )

        try:
            self._wait_for(
                final_files_exist,
                timeout=600,
                description="real degradation artifact promoted before SQLite",
            )
            assert analysis_id is not None
            directory = self.data_dir / "analyses" / analysis_id.replace(":", "_")
            digest_before = hashlib.sha256(
                (directory / "degradation.json").read_bytes()
            ).hexdigest()
            self._kill(worker)
            connection = sqlite3.connect(database)
            try:
                self.assertIsNone(
                    connection.execute(
                        "SELECT analysis_id FROM degradation_analyses "
                        "WHERE analysis_id = ?",
                        (analysis_id,),
                    ).fetchone()
                )
            finally:
                connection.close()
        finally:
            if worker.poll() is None:
                self._kill(worker)
            connection = sqlite3.connect(database)
            connection.execute("DROP TRIGGER IF EXISTS stall_degradation_recording")
            connection.commit()
            connection.close()

        assert analysis_id is not None
        with self._client() as restarted:
            recovered = restarted.post(
                f"/api/projects/{self.project_id}/degradation-analyses", json=request
            )
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(recovered.json()["analysis_id"], analysis_id)
            self.assertEqual(recovered.json()["state"], "COMPLETED", recovered.text)
            detail = restarted.get(
                f"/api/projects/{self.project_id}/degradation-analyses/{analysis_id}"
            )
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json()["state"], "COMPLETED", detail.text)
        directory = self.data_dir / "analyses" / analysis_id.replace(":", "_")
        self.assertEqual(
            hashlib.sha256((directory / "degradation.json").read_bytes()).hexdigest(),
            digest_before,
        )

    def test_analysis_final_output_is_reconciled_on_restart_after_real_process_death(self) -> None:
        _, _, request = self._utility_inputs()
        worker_source = """
import json
from pathlib import Path
import sys
import time
from fastapi.testclient import TestClient
from backend.tests.test_output_crash_recovery_real import _publish_worker_response
from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID

root = Path(sys.argv[1])
data_dir = Path(sys.argv[2])
project_id = sys.argv[3]
request = json.loads(sys.argv[4])
response_marker = Path(sys.argv[5])
settings = Settings(repository_root=root, registry_path=root / "resources" / "resource-registry.json", data_dir=data_dir, runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID, input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"))
with TestClient(create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
    response = client.post(f"/api/projects/{project_id}/utility-analyses", json=request)
    if response.status_code != 202:
        raise RuntimeError(response.text)
    analysis_id = response.json()["analysis_id"]
    _publish_worker_response(response_marker, {"analysis_id": analysis_id})
    while True:
        state = client.get(f"/api/projects/{project_id}/utility-analyses/{analysis_id}").json()["state"]
        if state in {"COMPLETED", "FAILED", "INTERRUPTED"}:
            raise RuntimeError(f"analysis reached {state} before parent termination")
        time.sleep(0.05)
"""
        database = self.data_dir / "qualsight.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TRIGGER stall_utility_analysis_recording "
            "BEFORE INSERT ON utility_analyses BEGIN "
            "SELECT sum(length(a.record_json) + length(b.record_json)) "
            "FROM samples AS a CROSS JOIN samples AS b; END"
        )
        connection.commit()
        connection.close()

        response_marker = self.data_dir / ".utility-worker-response.json"
        worker = self._worker(
            worker_source,
            str(ROOT),
            str(self.data_dir),
            self.project_id,
            json.dumps(request, sort_keys=True),
            str(response_marker),
        )
        analysis_id: str | None = None

        def final_files_exist_without_database_result() -> bool:
            nonlocal analysis_id
            analysis_id = self._worker_response_id(response_marker, "analysis_id")
            if analysis_id is None:
                return False
            directory = (
                self.data_dir / "analyses" / analysis_id.replace(":", "_")
            )
            return all(
                (directory / name).is_file()
                for name in ("pair_quality.parquet", "utility.json", "policy.json")
            )

        try:
            self._wait_for(
                final_files_exist_without_database_result,
                timeout=300,
                description="real utility artifacts promoted before their SQLite result",
            )
            assert analysis_id is not None
            directory = self.data_dir / "analyses" / analysis_id.replace(":", "_")
            hashes_before_restart = {
                name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                for name in ("pair_quality.parquet", "utility.json", "policy.json")
            }
            self._kill(worker)
        finally:
            if worker.poll() is None:
                self._kill(worker)
            connection = sqlite3.connect(database)
            connection.execute(
                "DROP TRIGGER IF EXISTS stall_utility_analysis_recording"
            )
            connection.commit()
            connection.close()

        assert analysis_id is not None
        connection = sqlite3.connect(database)
        try:
            self.assertIsNone(
                connection.execute(
                    "SELECT analysis_id FROM utility_analyses WHERE analysis_id = ?",
                    (analysis_id,),
                ).fetchone()
            )
        finally:
            connection.close()

        with self._client() as restarted:
            recovered = restarted.post(
                f"/api/projects/{self.project_id}/utility-analyses",
                json=request,
            )
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(recovered.json()["analysis_id"], analysis_id)
            self.assertEqual(recovered.json()["state"], "COMPLETED", recovered.text)
            detail = restarted.get(
                f"/api/projects/{self.project_id}/utility-analyses/{analysis_id}"
            )
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json()["state"], "COMPLETED", detail.text)

        directory = self.data_dir / "analyses" / analysis_id.replace(":", "_")
        self.assertEqual(
            {
                name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                for name in ("pair_quality.parquet", "utility.json", "policy.json")
            },
            hashes_before_restart,
        )

    def test_dataset_matcher_embed_obeys_native_deadline_and_releases_capacity(self) -> None:
        with self._client() as client:
            protocol = client.post(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/protocols/freeze-lfw-view2"
            )
            self.assertEqual(protocol.status_code, 201, protocol.text)
            protocol_id = protocol.json()["protocol_id"]

        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=self.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
            scientific_workers=1,
            scientific_pending=0,
            native_execution_timeout_seconds=0.01,
            shutdown_drain_seconds=30,
        )
        started_at = time.monotonic()
        with TestClient(
            create_app(settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        ) as client:
            started = client.post(
                f"/api/projects/{self.project_id}/protocols/{protocol_id}/matrix",
                params={"fixture_pairs_only": "true", "leaves": "QS-C22.AN"},
            )
            self.assertEqual(started.status_code, 202, started.text)
            matrix_id = started.json()["matrix_id"]
            status: dict = {}
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                response = client.get(
                    f"/api/projects/{self.project_id}/matrices/{matrix_id}"
                )
                self.assertEqual(response.status_code, 200, response.text)
                status = response.json()
                if status["progress"]["phase"] == "leaf:QS-C22.AN":
                    break
                if status["progress"]["phase"] in {"completed", "failed"}:
                    break
                time.sleep(0.005)
            self.assertEqual(
                status.get("progress", {}).get("phase"),
                "leaf:QS-C22.AN",
                status,
            )
            time.sleep(0.1)
            elapsed = time.monotonic() - started_at
            status_response = client.get(
                f"/api/projects/{self.project_id}/matrices/{matrix_id}"
            )
            self.assertEqual(status_response.status_code, 200, status_response.text)
            status = status_response.json()
            readiness = client.get("/api/ready")
            capacity = readiness.json()["checks"]["scientific_capacity"]
            self.assertGreater(elapsed, settings.native_execution_timeout_seconds)
            self.assertEqual(status["progress"]["phase"], "failed", status)
            self.assertEqual(readiness.status_code, 200, readiness.text)
            self.assertEqual(capacity["available"], 1, capacity)

    def test_direct_pair_final_output_is_reconciled_after_real_process_death(self) -> None:
        request = {
            "pair_id": self.pair_id,
            "matcher_id": "adaface-ir101-local-candidate",
            "preprocessing_id": "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1",
            "threshold": {
                "contract_id": "matcher_threshold",
                "kind": "FIXED_VALUE",
                "value": 0.731,
            },
        }
        worker_source = """
import json
from pathlib import Path
import sys
import time
from fastapi.testclient import TestClient
from backend.tests.test_output_crash_recovery_real import _publish_worker_response
from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID

root = Path(sys.argv[1])
data_dir = Path(sys.argv[2])
project_id = sys.argv[3]
request = json.loads(sys.argv[4])
response_marker = Path(sys.argv[5])
settings = Settings(repository_root=root, registry_path=root / "resources" / "resource-registry.json", data_dir=data_dir, runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID, input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"))
with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
    response = client.post(f"/api/projects/{project_id}/owner-pair-comparisons", json=request)
    if response.status_code != 202:
        raise RuntimeError(response.text)
    comparison_id = response.json()["comparison_id"]
    _publish_worker_response(response_marker, {"comparison_id": comparison_id})
    while True:
        state = client.get(f"/api/projects/{project_id}/owner-pair-comparisons/{comparison_id}").json()["state"]
        if state in {"COMPLETED", "FAILED", "INTERRUPTED"}:
            raise RuntimeError(f"comparison reached {state} before parent termination")
        time.sleep(0.05)
"""
        database = self.data_dir / "qualsight.sqlite3"
        trigger_connection = sqlite3.connect(database)
        trigger_connection.execute(
            "CREATE TRIGGER stall_direct_pair_recording "
            "BEFORE INSERT ON direct_pair_comparisons BEGIN "
            "SELECT sum(length(a.record_json) + length(b.record_json)) "
            "FROM samples AS a CROSS JOIN samples AS b; END"
        )
        trigger_connection.commit()
        trigger_connection.close()
        response_marker = self.data_dir / ".direct-pair-worker-response.json"
        worker = self._worker(
            worker_source,
            str(ROOT),
            str(self.data_dir),
            self.project_id,
            json.dumps(request, sort_keys=True),
            str(response_marker),
        )
        comparison_id: str | None = None
        try:
            def final_files_exist() -> bool:
                nonlocal comparison_id
                comparison_id = self._worker_response_id(
                    response_marker, "comparison_id"
                )
                if comparison_id is None:
                    return False
                directory = (
                    self.data_dir
                    / "direct-pair-comparisons"
                    / comparison_id.replace(":", "_")
                )
                return (
                    (directory / ".qualsight-direct-pair.prepared.json").is_file()
                    or (
                        (directory / "left-embedding.npy").is_file()
                        and (directory / "right-embedding.npy").is_file()
                    )
                )

            self._wait_for(
                final_files_exist,
                timeout=180,
                description="the complete real direct-pair output boundary",
            )
            assert comparison_id is not None
            self._kill(worker)
        finally:
            if worker.poll() is None:
                self._kill(worker)
            trigger_connection = sqlite3.connect(database)
            trigger_connection.execute("DROP TRIGGER IF EXISTS stall_direct_pair_recording")
            trigger_connection.commit()
            trigger_connection.close()

        assert comparison_id is not None
        directory = (
            self.data_dir
            / "direct-pair-comparisons"
            / comparison_id.replace(":", "_")
        )
        connection = sqlite3.connect(database)
        try:
            self.assertIsNone(
                connection.execute(
                    "SELECT comparison_id FROM direct_pair_comparisons WHERE comparison_id = ?",
                    (comparison_id,),
                ).fetchone()
            )
            operation_record = connection.execute(
                "SELECT result_id, project_id, request_json FROM operations WHERE operation_id = ?",
                (comparison_id,),
            ).fetchone()
            assert operation_record is not None
        finally:
            connection.close()
        prepared_run = recover_prepared_direct_pair(
            directory,
            comparison_id=comparison_id,
            run_id=operation_record[0],
            project_id=operation_record[1],
            request_sha256=CanonicalJson.from_value(
                json.loads(operation_record[2])
            ).sha256,
        )
        self.assertEqual(prepared_run.comparison_id, comparison_id)

        with self._client() as restarted:
            status = restarted.get(
                f"/api/projects/{self.project_id}/owner-pair-comparisons/{comparison_id}"
            )
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["state"], "COMPLETED", status.text)
            evidence = restarted.get(status.json()["links"]["evidence"])
            self.assertEqual(evidence.status_code, 200, evidence.text)
            self.assertEqual(evidence.json()["comparison_id"], comparison_id)

    def test_direct_pair_matcher_deadline_terminates_real_execution_and_releases_capacity(self) -> None:
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=self.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
            scientific_workers=1,
            scientific_pending=0,
            native_execution_timeout_seconds=0.01,
        )
        request = {
            "pair_id": self.pair_id,
            "matcher_id": "adaface-ir101-local-candidate",
            "preprocessing_id": "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1",
            "threshold": {
                "contract_id": "matcher_threshold",
                "kind": "FIXED_VALUE",
                "value": 0.732,
            },
        }
        started_at = time.monotonic()
        with TestClient(
            create_app(settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        ) as client:
            started = client.post(
                f"/api/projects/{self.project_id}/owner-pair-comparisons",
                json=request,
            )
            self.assertEqual(started.status_code, 202, started.text)
            comparison_id = started.json()["comparison_id"]
            status: dict = {}
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                response = client.get(
                    f"/api/projects/{self.project_id}/owner-pair-comparisons/{comparison_id}"
                )
                self.assertEqual(response.status_code, 200, response.text)
                status = response.json()
                if status["state"] in {"COMPLETED", "FAILED", "INTERRUPTED"}:
                    break
                time.sleep(0.02)
            self.assertEqual(status["state"], "FAILED", status)
            self.assertLess(time.monotonic() - started_at, 30)
            self.assertFalse(
                (
                    self.data_dir
                    / "direct-pair-comparisons"
                    / comparison_id.replace(":", "_")
                ).exists()
            )
            readiness = client.get("/api/ready")
            self.assertEqual(readiness.status_code, 200, readiness.text)
            self.assertEqual(
                readiness.json()["checks"]["scientific_capacity"]["available"],
                1,
            )

    def test_derived_set_final_output_is_recovered_on_real_retry_after_process_death(self) -> None:
        request = {
            "selection": "sample_ids",
            "sample_ids": [self.source_sample_id],
            "transforms": [{"kind": "jpeg", "parameters": [84]}],
        }
        ready = self.data_dir / "derived-worker.ready"
        proceed = self.data_dir / "derived-worker.proceed"
        response_path = self.data_dir / "derived-worker.response.json"
        worker_source = """
import hashlib
import json
from pathlib import Path
import sys
import time
from backend.tests.test_output_crash_recovery_real import _publish_worker_response
from qualsight.api.store import RunStore
from qualsight.domain.primitives import CanonicalJson
from qualsight.transforms.derived import build_derived_set, derived_set_identity

data_dir = Path(sys.argv[1])
project_id = sys.argv[2]
dataset_id = sys.argv[3]
sample_id = sys.argv[4]
ready = Path(sys.argv[5])
proceed = Path(sys.argv[6])
response_path = Path(sys.argv[7])
store = RunStore(data_dir / "qualsight.sqlite3")
try:
    project = store.project(project_id)
    dataset = store.dataset(dataset_id)
    sample = store.sample(sample_id)
    if project is None or dataset is None or sample is None:
        raise RuntimeError("recorded real source context is unavailable")
    ready.write_text("ready\\n", encoding="utf-8")
    while not proceed.is_file():
        time.sleep(0.01)
    transforms = [{"kind": "jpeg", "parameters": [84]}]
    derived_set_id = derived_set_identity(
        project_id=project_id,
        dataset_id=dataset_id,
        samples=[sample],
        transforms=transforms,
    )
    _publish_worker_response(response_path, {"derived_set_id": derived_set_id})
    operation_request = {
        "dataset_id": dataset_id,
        "derived_set_id": derived_set_id,
        "output_count": 1,
        "sample_ids": [sample_id],
        "sample_count": 1,
        "selection": "sample_ids",
        "source_sample_ids_sha256": hashlib.sha256(sample_id.encode("utf-8")).hexdigest(),
        "transforms": transforms,
    }
    build_derived_set(
        store,
        project=project,
        dataset=dataset,
        dataset_resource_id="lfw-funneled-local-candidate",
        samples=[sample],
        transforms=transforms,
        root=data_dir / "derived",
        actor="local-operator",
        request_sha256=CanonicalJson.from_value(operation_request).sha256,
    )
finally:
    store.close()
"""
        worker = self._worker(
            worker_source,
            str(self.data_dir),
            self.project_id,
            self.dataset_id,
            self.source_sample_id,
            str(ready),
            str(proceed),
            str(response_path),
        )
        ready_deadline = time.monotonic() + 60
        while time.monotonic() < ready_deadline and not ready.is_file():
            if worker.poll() is not None:
                stdout, stderr = worker.communicate(timeout=30)
                self.fail(
                    "derived worker exited before loading its recorded source context: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            time.sleep(0.01)
        if not ready.is_file():
            self._kill(worker)
            self.fail("timed out waiting for the derived worker's real recorded source context")
        database = self.data_dir / "qualsight.sqlite3"
        derived_root = self.data_dir / "derived"
        with sqlite3.connect(database) as trigger_connection:
            trigger_connection.execute(
                "CREATE TRIGGER stall_derived_set_recording "
                "BEFORE INSERT ON derived_sets BEGIN "
                "SELECT sum(length(a.record_json) + length(b.record_json)) "
                "FROM samples AS a CROSS JOIN samples AS b; END"
            )
        writer_lock = sqlite3.connect(database)
        writer_lock.execute("BEGIN IMMEDIATE")
        proceed.write_text("proceed\n", encoding="utf-8")
        derived_set_id: str | None = None
        final_directory: Path | None = None

        def final_output_is_complete() -> bool:
            nonlocal derived_set_id, final_directory
            if derived_set_id is None:
                derived_set_id = self._worker_response_id(
                    response_path, "derived_set_id"
                )
            if derived_set_id is None:
                return False
            final_directory = derived_root / derived_set_id.replace(":", "_")
            return (
                final_directory / ".qualsight-derived-set.prepared.json"
            ).is_file()

        try:
            output_deadline = time.monotonic() + 60
            while time.monotonic() < output_deadline and not final_output_is_complete():
                if worker.poll() is not None:
                    stdout, stderr = worker.communicate(timeout=30)
                    self.fail(
                        "derived worker exited before final output promotion: "
                        f"stdout={stdout!r}, stderr={stderr!r}"
                    )
                time.sleep(0.01)
            if not final_output_is_complete():
                observed = (
                    sorted(str(path.relative_to(self.data_dir)) for path in self.data_dir.rglob("*"))
                    if self.data_dir.exists()
                    else []
                )
                self._kill(worker)
                self.fail(f"timed out waiting for final derived output; observed={observed!r}")
            self._kill(worker)
        finally:
            if worker.poll() is None:
                self._kill(worker)
            writer_lock.rollback()
            writer_lock.close()
            with sqlite3.connect(database) as trigger_connection:
                trigger_connection.execute(
                    "DROP TRIGGER IF EXISTS stall_derived_set_recording"
                )
        assert derived_set_id is not None and final_directory is not None

        connection = sqlite3.connect(database)
        try:
            self.assertIsNone(
                connection.execute(
                    "SELECT derived_set_id FROM derived_sets WHERE derived_set_id = ?",
                    (derived_set_id,),
                ).fetchone()
            )
        finally:
            connection.close()

        store = RunStore(database)
        try:
            project = store.project(self.project_id)
            dataset = store.dataset(self.dataset_id)
            sample = store.sample(self.source_sample_id)
            assert project is not None and dataset is not None and sample is not None
            prepared = _recover_prepared_set(
                final_directory,
                derived_set_id=derived_set_id,
                project=project,
                dataset=dataset,
                dataset_resource_id=LFW_RESOURCE_ID,
                samples=[sample],
                transforms=request["transforms"],
                request_sha256=CanonicalJson.from_value(
                    {
                        "dataset_id": self.dataset_id,
                        "derived_set_id": derived_set_id,
                        "output_count": 1,
                        "sample_ids": [self.source_sample_id],
                        "sample_count": 1,
                        "selection": "sample_ids",
                        "source_sample_ids_sha256": hashlib.sha256(
                            self.source_sample_id.encode("utf-8")
                        ).hexdigest(),
                        "transforms": request["transforms"],
                    }
                ).sha256,
            )
            self.assertEqual(prepared.derived_set_id, derived_set_id)
        finally:
            store.close()

        with self._client() as restarted:
            recovered = restarted.post(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/transformations",
                json=request,
            )
            self.assertEqual(recovered.status_code, 201, recovered.text)
            self.assertEqual(recovered.json()["derived_set_id"], derived_set_id)
            detail = restarted.get(
                f"/api/projects/{self.project_id}/derived-sets/{derived_set_id}"
            )
            self.assertEqual(detail.status_code, 200, detail.text)
            image = restarted.get(detail.json()["samples"][0]["links"]["image"])
            self.assertEqual(image.status_code, 200, image.text)
            self.assertEqual(
                hashlib.sha256(image.content).hexdigest(),
                detail.json()["samples"][0]["sha256"],
            )

    def test_transform_work_is_bounded_and_durably_admitted_before_execution(self) -> None:
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=self.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
            scientific_workers=1,
            scientific_pending=0,
            shutdown_drain_seconds=30,
        )
        database = self.data_dir / "qualsight.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute(
            "CREATE TRIGGER stall_bounded_derived_recording "
            "BEFORE INSERT ON derived_sets BEGIN "
            "SELECT sum(length(a.record_json) + length(b.record_json)) "
            "FROM (SELECT record_json FROM samples LIMIT 5000) AS a "
            "CROSS JOIN (SELECT record_json FROM samples LIMIT 5000) AS b; END"
        )
        connection.commit()
        connection.close()
        endpoint = (
            f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/transformations"
        )
        first_request = {
            "selection": "sample_ids",
            "sample_ids": [self.source_sample_id],
            "transforms": [{"kind": "jpeg", "parameters": [87]}],
        }
        second_request = {
            "selection": "sample_ids",
            "sample_ids": [self.source_sample_id],
            "transforms": [{"kind": "jpeg", "parameters": [88]}],
        }
        try:
            with TestClient(
                create_app(settings),
                base_url="http://127.0.0.1",
                raise_server_exceptions=False,
            ) as client, ThreadPoolExecutor(max_workers=1) as requests:
                first = requests.submit(client.post, endpoint, json=first_request)
                saturated: dict | None = None
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    readiness = client.get("/api/ready")
                    candidate = readiness.json()["checks"]["scientific_capacity"]
                    if candidate["available"] == 0:
                        saturated = candidate
                        break
                    if first.done():
                        break
                    time.sleep(0.01)
                self.assertIsNotNone(
                    saturated,
                    "the real transformation never occupied bounded scientific capacity",
                )
                refused = client.post(endpoint, json=second_request)
                self.assertEqual(refused.status_code, 503, refused.text)
                self.assertEqual(
                    refused.json()["detail"]["error"],
                    "SCIENTIFIC_WORK_CAPACITY_EXHAUSTED",
                )
                completed = first.result(timeout=120)
                self.assertEqual(completed.status_code, 201, completed.text)
                operation_id = "operation:sha256:" + hashlib.sha256(
                    completed.json()["derived_set_id"].encode("utf-8")
                ).hexdigest()
                operation = client.get(
                    f"/api/projects/{self.project_id}/operations/{operation_id}"
                )
                self.assertEqual(operation.status_code, 200, operation.text)
                self.assertEqual(operation.json()["state"], "COMPLETED")
                self.assertEqual(
                    operation.json()["result_id"], completed.json()["derived_set_id"]
                )
        finally:
            connection = sqlite3.connect(database)
            connection.execute("DROP TRIGGER IF EXISTS stall_bounded_derived_recording")
            connection.commit()
            connection.close()

    def test_transform_request_refuses_unbounded_real_output_cardinality_before_execution(self) -> None:
        response_path = self.data_dir / "unbounded-transform-response.json"
        worker_source = """
import json
from pathlib import Path
import sys
from fastapi.testclient import TestClient
from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID

root = Path(sys.argv[1])
data_dir = Path(sys.argv[2])
project_id = sys.argv[3]
dataset_id = sys.argv[4]
response_path = Path(sys.argv[5])
settings = Settings(repository_root=root, registry_path=root / "resources" / "resource-registry.json", data_dir=data_dir, runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID, input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"))
with TestClient(create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
    response = client.post(f"/api/projects/{project_id}/datasets/{dataset_id}/transformations", json={"selection": "all", "transforms": [{"kind": "jpeg", "parameters": [89, 90]}]})
    response_path.write_text(json.dumps({"status_code": response.status_code, "body": response.json()}, sort_keys=True), encoding="utf-8")
"""
        worker = self._worker(
            worker_source,
            str(ROOT),
            str(self.data_dir),
            self.project_id,
            self.dataset_id,
            str(response_path),
        )
        derived_root = self.data_dir / "derived"
        started_execution = False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if response_path.is_file():
                break
            if derived_root.exists() and any(
                path.is_dir() and path.name.startswith(".derived-set_sha256_")
                for path in derived_root.iterdir()
            ):
                started_execution = True
                break
            if worker.poll() is not None:
                stdout, stderr = worker.communicate(timeout=30)
                self.fail(
                    "unbounded transform worker exited without a response: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            time.sleep(0.01)
        if worker.poll() is None:
            self._kill(worker)
        self.assertTrue(
            response_path.is_file(),
            "the unbounded request began processing real images instead of being refused",
        )
        self.assertFalse(started_execution)
        response = json.loads(response_path.read_text(encoding="utf-8"))
        self.assertEqual(response["status_code"], 422, response)
        self.assertEqual(
            response["body"]["detail"],
            "derived output cardinality exceeds the controlled beta limit of 20000 files",
        )


class PreparedOutputIntegrityRealTests(unittest.TestCase):
    def test_promoted_real_artifact_survives_stage_cleanup_and_unbound_member_is_rejected(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="qualsight-prepared-integrity-"))
        try:
            target = root / "analysis_sha256_real-file-integrity"
            request_sha256 = CanonicalJson.from_value(
                {
                    "input_sha256": ABEL_LEFT_SHA256,
                    "source_kind": "REGISTERED_LFW_FUNNELED",
                }
            ).sha256
            stage = create_staging_directory(
                target,
                operation_kind="REAL_FILE_INTEGRITY_GATE",
                result_id="analysis:sha256:" + ABEL_LEFT_SHA256,
                project_id="project:sha256:" + ABEL_RIGHT_SHA256,
                request_sha256=request_sha256,
            )
            shutil.copyfile(ABEL_LEFT, stage / "artifact.jpg")
            prepare_and_publish(
                stage,
                target,
                operation_kind="REAL_FILE_INTEGRITY_GATE",
                result_id="analysis:sha256:" + ABEL_LEFT_SHA256,
                project_id="project:sha256:" + ABEL_RIGHT_SHA256,
                request_sha256=request_sha256,
                artifact_names=("artifact.jpg",),
                record={"input_sha256": ABEL_LEFT_SHA256},
            )
            discard_staging_directory(stage)
            self.assertTrue(target.is_dir())
            recovered = load_prepared_output(
                target,
                operation_kind="REAL_FILE_INTEGRITY_GATE",
                result_id="analysis:sha256:" + ABEL_LEFT_SHA256,
                project_id="project:sha256:" + ABEL_RIGHT_SHA256,
                request_sha256=request_sha256,
                artifact_names=("artifact.jpg",),
            )
            self.assertEqual(recovered["record"]["input_sha256"], ABEL_LEFT_SHA256)
            shutil.copyfile(ABEL_RIGHT, target / "unbound-real-photo.jpg")
            with self.assertRaisesRegex(
                PreparedOutputError,
                "contains unbound members",
            ):
                load_prepared_output(
                    target,
                    operation_kind="REAL_FILE_INTEGRITY_GATE",
                    result_id="analysis:sha256:" + ABEL_LEFT_SHA256,
                    project_id="project:sha256:" + ABEL_RIGHT_SHA256,
                    request_sha256=request_sha256,
                    artifact_names=("artifact.jpg",),
                )
        finally:
            shutil.rmtree(root, ignore_errors=True)


class FailedPreparedOutputRecoveryRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-failed-recovery-"))
        cls.settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        with cls._client() as client:
            pulled = client.post(
                "/api/matcher-assets/adaface-ir101-local-candidate/pull-sources"
            )
            assert pulled.status_code == 200, pulled.text

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def setUp(self) -> None:
        with self._client() as client:
            project = client.post(
                "/api/projects",
                json={
                    "name": f"Failed recovery {self._testMethodName}",
                    "description": "Distinct real LFW recovery request identity.",
                    "purpose_scope": RESEARCH_SCOPE,
                },
            )
            self.assertEqual(project.status_code, 201, project.text)
            self.project_id = project.json()["project_id"]
            opened = client.post(f"/api/projects/{self.project_id}/open")
            self.assertEqual(opened.status_code, 200, opened.text)
            imported = client.post(
                f"/api/projects/{self.project_id}/datasets/import-registered",
                json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
            )
            self.assertEqual(imported.status_code, 201, imported.text)
            self.dataset_id = imported.json()["dataset_id"]
            samples = client.get(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/samples",
                params={"limit": 1},
            )
            self.assertEqual(samples.status_code, 200, samples.text)
            self.source_sample_id = samples.json()[0]["sample_id"]
            if "direct_pair" in self._testMethodName:
                left = self._upload(client, ABEL_LEFT, ABEL_LEFT_SHA256)
                right = self._upload(client, ABEL_RIGHT, ABEL_RIGHT_SHA256)
                pair = client.post(
                    f"/api/projects/{self.project_id}/owner-pair-admissions",
                    json={
                        "left_sample_id": left["sample_id"],
                        "right_sample_id": right["sample_id"],
                        "relationship": "SAME_PERSON",
                    },
                )
                self.assertEqual(pair.status_code, 201, pair.text)
                self.pair_id = pair.json()["pair_id"]

    @classmethod
    def _client(cls) -> TestClient:
        return TestClient(
            create_app(cls.settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        )

    def _upload(
        self, client: TestClient, source: Path, expected_sha256: str
    ) -> dict:
        payload = source.read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), expected_sha256)
        response = client.post(
            "/api/quality/assess-upload",
            params={
                "project_id": self.project_id,
                "purpose_scope": RESEARCH_SCOPE,
                "filename": source.name,
            },
            content=payload,
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    _wait_for = staticmethod(OutputCrashRecoveryRealTests._wait_for)
    _worker = OutputCrashRecoveryRealTests._worker
    _kill = OutputCrashRecoveryRealTests._kill
    _utility_inputs = OutputCrashRecoveryRealTests._utility_inputs

    def test_failed_utility_post_publication_is_reconciled_on_live_retry(self) -> None:
        _, _, request = self._utility_inputs()
        database = self.data_dir / "qualsight.sqlite3"
        trigger = sqlite3.connect(database)
        trigger.execute(
            "CREATE TRIGGER refuse_utility_analysis_recording "
            "BEFORE INSERT ON utility_analyses BEGIN "
            "SELECT RAISE(ABORT, 'real utility result transaction refused'); END"
        )
        trigger.commit()
        trigger.close()

        with self._client() as client:
            started = client.post(
                f"/api/projects/{self.project_id}/utility-analyses", json=request
            )
            self.assertEqual(started.status_code, 202, started.text)
            analysis_id = started.json()["analysis_id"]
            state: dict = {}
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                response = client.get(
                    f"/api/projects/{self.project_id}/utility-analyses/{analysis_id}"
                )
                self.assertEqual(response.status_code, 200, response.text)
                state = response.json()
                if state["state"] in {"COMPLETED", "FAILED", "INTERRUPTED"}:
                    break
                time.sleep(0.02)
            self.assertEqual(state.get("state"), "FAILED", state)
            directory = self.data_dir / "analyses" / analysis_id.replace(":", "_")
            self.assertTrue(
                (directory / ".qualsight-prepared-output.json").is_file()
            )
            trigger = sqlite3.connect(database)
            trigger.execute("DROP TRIGGER refuse_utility_analysis_recording")
            trigger.commit()
            trigger.close()

            recovered = client.post(
                f"/api/projects/{self.project_id}/utility-analyses", json=request
            )
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(recovered.json()["analysis_id"], analysis_id)
            self.assertEqual(recovered.json()["state"], "COMPLETED", recovered.text)

        connection = sqlite3.connect(database)
        try:
            attempts = connection.execute(
                "SELECT operation_id, attempt, supersedes_operation_id, state, error "
                "FROM operations WHERE request_id = ? ORDER BY attempt",
                (analysis_id,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([row[3] for row in attempts], ["FAILED", "COMPLETED"])
        self.assertIn("utility result transaction refused", attempts[0][4])
        self.assertEqual(attempts[1][2], attempts[0][0])

    def test_failed_direct_pair_post_publication_keeps_receipt_and_reconciles(self) -> None:
        request = {
            "pair_id": self.pair_id,
            "matcher_id": "adaface-ir101-local-candidate",
            "preprocessing_id": "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1",
            "threshold": {
                "contract_id": "matcher_threshold",
                "kind": "FIXED_VALUE",
                "value": 0.733,
            },
        }
        database = self.data_dir / "qualsight.sqlite3"
        trigger = sqlite3.connect(database)
        trigger.execute(
            "CREATE TRIGGER refuse_direct_pair_recording "
            "BEFORE INSERT ON direct_pair_comparisons BEGIN "
            "SELECT RAISE(ABORT, 'real direct pair result transaction refused'); END"
        )
        trigger.commit()
        trigger.close()

        with self._client() as client:
            started = client.post(
                f"/api/projects/{self.project_id}/owner-pair-comparisons",
                json=request,
            )
            self.assertEqual(started.status_code, 202, started.text)
            comparison_id = started.json()["comparison_id"]
            state: dict = {}
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                response = client.get(
                    f"/api/projects/{self.project_id}/owner-pair-comparisons/{comparison_id}"
                )
                self.assertEqual(response.status_code, 200, response.text)
                state = response.json()
                if state["state"] in {"COMPLETED", "FAILED", "INTERRUPTED"}:
                    break
                time.sleep(0.02)
            self.assertEqual(state.get("state"), "FAILED", state)
            directory = (
                self.data_dir
                / "direct-pair-comparisons"
                / comparison_id.replace(":", "_")
            )
            self.assertTrue(
                (directory / ".qualsight-direct-pair.prepared.json").is_file()
            )
            trigger = sqlite3.connect(database)
            trigger.execute("DROP TRIGGER refuse_direct_pair_recording")
            trigger.commit()
            trigger.close()

            recovered = client.post(
                f"/api/projects/{self.project_id}/owner-pair-comparisons",
                json=request,
            )
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(recovered.json()["comparison_id"], comparison_id)
            self.assertEqual(recovered.json()["state"], "COMPLETED", recovered.text)
            evidence = client.get(recovered.json()["links"]["evidence"])
            self.assertEqual(evidence.status_code, 200, evidence.text)

        connection = sqlite3.connect(database)
        try:
            attempts = connection.execute(
                "SELECT operation_id, attempt, supersedes_operation_id, state "
                "FROM operations WHERE request_id = ? ORDER BY attempt",
                (comparison_id,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([row[3] for row in attempts], ["FAILED", "COMPLETED"])
        self.assertEqual(attempts[1][2], attempts[0][0])

    def test_failed_derived_set_post_publication_keeps_receipt_and_reconciles(self) -> None:
        request = {
            "selection": "sample_ids",
            "sample_ids": [self.source_sample_id],
            "transforms": [{"kind": "jpeg", "parameters": [83]}],
        }
        database = self.data_dir / "qualsight.sqlite3"
        trigger = sqlite3.connect(database)
        trigger.execute(
            "CREATE TRIGGER refuse_derived_set_recording "
            "BEFORE INSERT ON derived_sets BEGIN "
            "SELECT RAISE(ABORT, 'real derived set result transaction refused'); END"
        )
        trigger.commit()
        trigger.close()

        with self._client() as client:
            failed = client.post(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/transformations",
                json=request,
            )
            self.assertEqual(failed.status_code, 409, failed.text)
            connection = sqlite3.connect(database)
            try:
                operation = connection.execute(
                    "SELECT operation_id, result_id, output_path, state FROM operations "
                    "WHERE operation_kind = 'DERIVED_SET' ORDER BY accepted_at DESC LIMIT 1"
                ).fetchone()
            finally:
                connection.close()
            assert operation is not None
            self.assertEqual(operation[3], "FAILED")
            operation_id, derived_set_id, output_path, _ = operation
            directory = Path(output_path)
            self.assertTrue(
                (directory / ".qualsight-derived-set.prepared.json").is_file()
            )
            trigger = sqlite3.connect(database)
            trigger.execute("DROP TRIGGER refuse_derived_set_recording")
            trigger.commit()
            trigger.close()

            recovered = client.post(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/transformations",
                json=request,
            )
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(recovered.json()["derived_set_id"], derived_set_id)

        connection = sqlite3.connect(database)
        try:
            attempts = connection.execute(
                "SELECT operation_id, attempt, supersedes_operation_id, state "
                "FROM operations WHERE request_id = ? ORDER BY attempt",
                (operation_id,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([row[3] for row in attempts], ["FAILED", "COMPLETED"])
        self.assertEqual(attempts[1][2], attempts[0][0])

    def test_a_matcher_leaf_final_output_is_reconciled_after_real_process_death(self) -> None:
        with self._client() as client:
            protocol = client.post(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/protocols/freeze-lfw-view2"
            )
            self.assertEqual(protocol.status_code, 201, protocol.text)
            protocol_id = protocol.json()["protocol_id"]
        worker_source = """
from pathlib import Path
import sys
import time
from fastapi.testclient import TestClient
from backend.tests.test_output_crash_recovery_real import _publish_worker_response
from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID

root = Path(sys.argv[1])
data_dir = Path(sys.argv[2])
project_id = sys.argv[3]
protocol_id = sys.argv[4]
response_marker = Path(sys.argv[5])
settings = Settings(repository_root=root, registry_path=root / "resources" / "resource-registry.json", data_dir=data_dir, runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID, input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"))
with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
    response = client.post(f"/api/projects/{project_id}/protocols/{protocol_id}/matrix", params={"fixture_pairs_only": "true", "leaves": "QS-C22.AS"})
    if response.status_code != 202:
        raise RuntimeError(response.text)
    _publish_worker_response(response_marker, {"matrix_id": response.json()["matrix_id"]})
    while True:
        time.sleep(0.05)
"""
        database = self.data_dir / "qualsight.sqlite3"
        response_marker = self.data_dir / ".matrix-worker-response.json"
        worker = self._worker(
            worker_source,
            str(ROOT),
            str(self.data_dir),
            self.project_id,
            protocol_id,
            str(response_marker),
        )
        matrix_id: str | None = None
        leaf_directory: Path | None = None
        writer_lock: sqlite3.Connection | None = None

        def admitted_operation_exists() -> bool:
            nonlocal matrix_id, leaf_directory
            if worker.poll() is not None:
                stdout, stderr = worker.communicate(timeout=30)
                raise AssertionError(
                    "matcher worker exited before publishing its prepared leaf: "
                    f"returncode={worker.returncode}; stdout={stdout[-1000:]!r}; "
                    f"stderr={stderr[-2000:]!r}"
                )
            matrix_id = _read_worker_response_id(response_marker, "matrix_id")
            if matrix_id is None:
                return False
            leaf_directory = (
                self.data_dir / "matrix" / matrix_id.replace(":", "_") / "QS-C22_AS"
            )
            return True

        def final_leaf_exists_without_database_record() -> bool:
            assert matrix_id is not None and leaf_directory is not None
            if worker.poll() is not None:
                stdout, stderr = worker.communicate(timeout=30)
                raise AssertionError(
                    "matcher worker exited before publishing its prepared leaf: "
                    f"returncode={worker.returncode}; stdout={stdout[-1000:]!r}; "
                    f"stderr={stderr[-2000:]!r}"
                )
            return all(
                (leaf_directory / name).is_file()
                for name in (
                    "embeddings.npy",
                    "comparisons.parquet",
                    "verification_metrics.json",
                )
            )

        try:
            self._wait_for(
                admitted_operation_exists,
                timeout=60,
                description="durably admitted matcher matrix operation",
            )
            writer_lock = sqlite3.connect(database, timeout=30)
            writer_lock.execute("BEGIN IMMEDIATE")
            self._wait_for(
                final_leaf_exists_without_database_record,
                timeout=300,
                description="real matcher leaf files before SQLite recording",
            )
            assert leaf_directory is not None
            hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in leaf_directory.iterdir()
                if path.is_file() and not path.name.startswith(".qualsight-")
            }
            self._kill(worker)
        finally:
            if worker.poll() is None:
                self._kill(worker)
            if writer_lock is not None:
                writer_lock.rollback()
                writer_lock.close()

        assert matrix_id is not None and leaf_directory is not None
        connection = sqlite3.connect(database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM operations WHERE operation_id = ?", (matrix_id,)
                ).fetchone(),
                ("RUNNING",),
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM matcher_leaves WHERE matrix_id = ?", (matrix_id,)
                ).fetchone()
            )
        finally:
            connection.close()
        with self._client() as restarted:
            recovered = restarted.post(
                f"/api/projects/{self.project_id}/protocols/{protocol_id}/matrix",
                params={"fixture_pairs_only": "true", "leaves": "QS-C22.AS"},
            )
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(recovered.json()["matrix_id"], matrix_id, recovered.text)
            self.assertEqual(recovered.json()["state"], "COMPLETED", recovered.text)
            leaves = restarted.get(
                f"/api/projects/{self.project_id}/matrices/{matrix_id}"
            )
            self.assertEqual(leaves.status_code, 200, leaves.text)
            self.assertEqual(len(leaves.json()["leaves"]), 1, leaves.text)
        self.assertEqual(
            {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in leaf_directory.iterdir()
                if path.is_file() and not path.name.startswith(".qualsight-")
            },
            hashes,
        )

    def test_two_leaf_matrix_resumes_only_missing_leaf_after_real_process_death(self) -> None:
        with self._client() as client:
            protocol = client.post(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/protocols/freeze-lfw-view2"
            )
            self.assertEqual(protocol.status_code, 201, protocol.text)
            protocol_id = protocol.json()["protocol_id"]
        worker_source = """
from pathlib import Path
import sys
import time
from fastapi.testclient import TestClient
from backend.tests.test_output_crash_recovery_real import _publish_worker_response
from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID

root = Path(sys.argv[1])
data_dir = Path(sys.argv[2])
project_id = sys.argv[3]
protocol_id = sys.argv[4]
response_marker = Path(sys.argv[5])
settings = Settings(repository_root=root, registry_path=root / "resources" / "resource-registry.json", data_dir=data_dir, runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID, input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"))
with TestClient(create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
    response = client.post(f"/api/projects/{project_id}/protocols/{protocol_id}/matrix", params={"fixture_pairs_only": "true", "leaves": "QS-C22.AS,QS-C22.AN"})
    if response.status_code != 202:
        raise RuntimeError(response.text)
    _publish_worker_response(response_marker, {"matrix_id": response.json()["matrix_id"]})
    while True:
        time.sleep(0.05)
"""
        database = self.data_dir / "qualsight.sqlite3"
        trigger = sqlite3.connect(database)
        trigger.execute(
            "CREATE TRIGGER stall_first_two_leaf_recording "
            "BEFORE INSERT ON matcher_leaves BEGIN "
            "SELECT sum(length(a.record_json) + length(b.record_json) + "
            "length(c.record_json)) FROM samples AS a CROSS JOIN samples AS b "
            "CROSS JOIN samples AS c; END"
        )
        trigger.commit()
        trigger.close()
        response_marker = self.data_dir / ".two-leaf-worker-response.json"
        worker = self._worker(
            worker_source,
            str(ROOT),
            str(self.data_dir),
            self.project_id,
            protocol_id,
            str(response_marker),
        )
        matrix_id: str | None = None
        first_directory: Path | None = None
        second_directory: Path | None = None

        def first_leaf_is_prepared_before_second_leaf() -> bool:
            nonlocal matrix_id, first_directory, second_directory
            matrix_id = _read_worker_response_id(response_marker, "matrix_id")
            if matrix_id is None:
                return False
            matrix_directory = self.data_dir / "matrix" / matrix_id.replace(":", "_")
            first_directory = matrix_directory / "QS-C22_AN"
            second_directory = matrix_directory / "QS-C22_AS"
            return (
                all(
                    (first_directory / name).is_file()
                    for name in (
                        ".qualsight-prepared-output.json",
                        "embeddings.npy",
                        "comparisons.parquet",
                        "verification_metrics.json",
                    )
                )
                and not second_directory.exists()
            )

        try:
            self._wait_for(
                first_leaf_is_prepared_before_second_leaf,
                timeout=300,
                description="first real AdaFace leaf before second-leaf execution",
            )
            assert first_directory is not None
            first_hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in first_directory.iterdir()
                if path.is_file()
            }
            time.sleep(0.5)
            self.assertIsNone(worker.poll(), "matrix worker left the SQLite stall")
            self._kill(worker)
        finally:
            if worker.poll() is None:
                self._kill(worker)
            trigger = sqlite3.connect(database)
            trigger.execute("DROP TRIGGER IF EXISTS stall_first_two_leaf_recording")
            trigger.commit()
            trigger.close()

        assert matrix_id is not None
        assert first_directory is not None
        assert second_directory is not None
        connection = sqlite3.connect(database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM operations WHERE operation_id = ?", (matrix_id,)
                ).fetchone(),
                ("RUNNING",),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM matcher_leaves WHERE matrix_id = ?",
                    (matrix_id,),
                ).fetchone(),
                (0,),
            )
        finally:
            connection.close()
        with self._client() as restarted:
            connection = sqlite3.connect(database)
            try:
                prepared_rows = connection.execute(
                    "SELECT leaf_id, run_id FROM matcher_leaves WHERE matrix_id = ?",
                    (matrix_id,),
                ).fetchall()
                interrupted = connection.execute(
                    "SELECT state FROM operations WHERE operation_id = ?",
                    (matrix_id,),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual([row[0] for row in prepared_rows], ["QS-C22.AN"])
            self.assertEqual(interrupted, ("INTERRUPTED",))
            first_run_id = prepared_rows[0][1]

            retried = restarted.post(
                f"/api/projects/{self.project_id}/operations/{matrix_id}/retry"
            )
            self.assertEqual(retried.status_code, 202, retried.text)
            final: dict = {}
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                response = restarted.get(
                    f"/api/projects/{self.project_id}/matrices/{matrix_id}"
                )
                self.assertEqual(response.status_code, 200, response.text)
                final = response.json()
                if final["progress"]["phase"] in {"completed", "failed"}:
                    break
                time.sleep(0.05)
            self.assertEqual(final.get("progress", {}).get("phase"), "completed", final)
            self.assertEqual(
                {leaf["leaf_id"] for leaf in final["leaves"]},
                {"QS-C22.AS", "QS-C22.AN"},
            )

        connection = sqlite3.connect(database)
        try:
            recorded_rows = connection.execute(
                "SELECT leaf_id, run_id FROM matcher_leaves WHERE matrix_id = ? "
                "ORDER BY leaf_id",
                (matrix_id,),
            ).fetchall()
            attempts = connection.execute(
                "SELECT state, supersedes_operation_id FROM operations "
                "WHERE request_id = ? ORDER BY attempt",
                (matrix_id,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([row[0] for row in recorded_rows], ["QS-C22.AN", "QS-C22.AS"])
        self.assertEqual(dict(recorded_rows)["QS-C22.AN"], first_run_id)
        self.assertEqual([row[0] for row in attempts], ["INTERRUPTED", "COMPLETED"])
        self.assertEqual(attempts[1][1], matrix_id)
        self.assertEqual(
            {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in first_directory.iterdir()
                if path.is_file()
            },
            first_hashes,
        )
        self.assertTrue(second_directory.is_dir())


if __name__ == "__main__":
    unittest.main()
