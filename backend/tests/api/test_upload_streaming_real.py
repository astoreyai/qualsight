"""HTTP upload byte admission exercised with the registered real LFW archive."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID
from qualsight.importers.owner_supplied import MAX_UPLOAD_BYTES


ROOT = Path(__file__).resolve().parents[3]
REAL_LFW_ARCHIVE = Path("/mnt/projects/datasets/lfw/lfw-funneled.tgz")
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class UploadStreamingRealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-upload-stream-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=self.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID,),
        )
        self.client = TestClient(create_app(settings), base_url="http://127.0.0.1")
        self.client.__enter__()
        project = self.client.post(
            "/api/projects",
            json={
                "name": "Upload streaming gate",
                "description": "HTTP byte-boundary verification with registered archive bytes.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        self.assertEqual(project.status_code, 201, project.text)
        self.project_id = project.json()["project_id"]
        self.client.post(f"/api/projects/{self.project_id}/open")

    def tearDown(self) -> None:
        if self.client is not None:
            self.client.__exit__(None, None, None)
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_real_archive_stream_is_rejected_at_the_http_cap_before_full_consumption(self) -> None:
        self.assertGreater(REAL_LFW_ARCHIVE.stat().st_size, MAX_UPLOAD_BYTES)
        self.client.__exit__(None, None, None)
        self.client = None

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        server_code = (
            "import uvicorn\n"
            "from qualsight.api import Settings, create_app\n"
            f"uvicorn.run(create_app(Settings.from_environment()), host='127.0.0.1', port={port}, log_level='warning')\n"
        )
        server = subprocess.Popen(
            [sys.executable, "-c", server_code],
            cwd=ROOT / "backend",
            env={
                **os.environ,
                "PYTHONPATH": "src:..",
                "QUALSIGHT_REPOSITORY_ROOT": str(ROOT),
                "QUALSIGHT_DATA_DIR": str(self.data_dir),
                "QUALSIGHT_NETWORK_GUARD": "false",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            health_url = f"http://127.0.0.1:{port}/api/health"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    self.fail(server.stderr.read() if server.stderr else "upload server exited before readiness")
                try:
                    with urlopen(health_url, timeout=1) as health:
                        if health.status == 200:
                            break
                except (URLError, TimeoutError):
                    time.sleep(0.1)
            else:
                self.fail("upload server did not become ready")

            query = urlencode(
                {
                    "project_id": self.project_id,
                    "purpose_scope": RESEARCH_SCOPE,
                    "filename": REAL_LFW_ARCHIVE.name,
                }
            )
            response_headers = self.data_dir / "upload-response.headers"
            curl = subprocess.run(
                [
                    shutil.which("curl") or "/usr/bin/curl",
                    "--silent",
                    "--show-error",
                    "--http1.1",
                    "--header",
                    "Content-Type: application/octet-stream",
                    "--header",
                    "Transfer-Encoding: chunked",
                    "--header",
                    "Expect:",
                    "--data-binary",
                    f"@{REAL_LFW_ARCHIVE}",
                    "--output",
                    "/dev/null",
                    "--dump-header",
                    str(response_headers),
                    "--write-out",
                    "%{http_code} %{size_upload}",
                    f"http://127.0.0.1:{port}/api/quality/assess-upload?{query}",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(curl.returncode, 0, curl.stderr)
            status_text, uploaded_text = curl.stdout.strip().split()
            self.assertEqual(status_text, "413", curl.stdout)
            headers = {}
            for line in response_headers.read_text(encoding="utf-8").splitlines():
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            self.assertLessEqual(int(headers["x-qualsight-bytes-read"]), MAX_UPLOAD_BYTES)
            self.assertLess(float(uploaded_text), REAL_LFW_ARCHIVE.stat().st_size)
        finally:
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=30)
            for stream in (server.stdout, server.stderr):
                if stream is not None:
                    stream.close()
        self.assertFalse((self.data_dir / "owner_supplied").exists())


if __name__ == "__main__":
    unittest.main()
