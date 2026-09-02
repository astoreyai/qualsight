"""Project ownership gates for figures, reports, and saved dashboards on real LFW evidence."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
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


ROOT = Path(__file__).resolve().parents[3]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class RealReportingProjectOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-report-ownership-"))
        cls.settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        cls.client = TestClient(
            create_app(cls.settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        )
        cls.client.__enter__()

        source = cls.client.post(
            "/api/projects",
            json={
                "name": "report ownership source",
                "description": "Real LFW reporting ownership source evidence.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        assert source.status_code == 201, source.text
        cls.source_project_id = source.json()["project_id"]
        opened = cls.client.post(f"/api/projects/{cls.source_project_id}/open")
        assert opened.status_code == 200, opened.text
        imported = cls.client.post(
            f"/api/projects/{cls.source_project_id}/datasets/import-registered",
            json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
        )
        assert imported.status_code == 201, imported.text
        cls.source_dataset_id = imported.json()["dataset_id"]
        protocol = cls.client.post(
            f"/api/projects/{cls.source_project_id}/datasets/{cls.source_dataset_id}/protocols/freeze-lfw-view2"
        )
        assert protocol.status_code == 201, protocol.text
        cls.source_protocol_id = protocol.json()["protocol_id"]

        for matcher_id in (
            "adaface-ir101-local-candidate",
            "elasticface-arc-r100-local-candidate",
        ):
            pulled = cls.client.post(f"/api/matcher-assets/{matcher_id}/pull-sources")
            assert pulled.status_code == 200, pulled.text
        started = cls.client.post(
            f"/api/projects/{cls.source_project_id}/protocols/{cls.source_protocol_id}/matrix",
            params={
                "fixture_pairs_only": "true",
                "leaves": "QS-C22.AS,QS-C22.ES",
            },
        )
        assert started.status_code == 202, started.text
        cls.matrix_id = started.json()["matrix_id"]
        cls.matrix = cls._wait_for(
            f"/api/projects/{cls.source_project_id}/matrices/{cls.matrix_id}",
            terminal=lambda body: body["progress"]["phase"] in {"completed", "failed"},
            timeout=600,
        )
        assert cls.matrix["progress"]["phase"] == "completed", cls.matrix
        assert len(cls.matrix["leaves"]) == 2, cls.matrix
        cls.matcher_run_id = cls.matrix["leaves"][0]["run_id"]

        quality = cls.client.post(
            f"/api/projects/{cls.source_project_id}/datasets/{cls.source_dataset_id}/quality-batch",
            json={"selection": {"kind": "subset_fixture"}, "chunks": 1},
        )
        assert quality.status_code == 202, quality.text
        cls.quality_job_id = quality.json()["job_id"]
        cls.quality_job = cls._wait_for(
            f"/api/projects/{cls.source_project_id}/jobs/{cls.quality_job_id}",
            terminal=lambda body: body["ended_at"] is not None,
            timeout=300,
        )
        assert cls.quality_job["state"] == "COMPLETED", cls.quality_job

        utility = cls.client.post(
            f"/api/projects/{cls.source_project_id}/utility-analyses",
            json={
                "matcher_run_id": cls.matcher_run_id,
                "quality_job_id": cls.quality_job_id,
                "pair_quality_policy": "pair.quality.min",
                "matcher_threshold": 0.8,
                "quality_threshold": 40.0,
                "bootstrap_replicates": 50,
                "seed": 11,
            },
        )
        assert utility.status_code == 202, utility.text
        cls.utility_analysis_id = utility.json()["analysis_id"]
        cls.utility = cls._wait_for(
            f"/api/projects/{cls.source_project_id}/utility-analyses/{cls.utility_analysis_id}",
            terminal=lambda body: body["state"] in {"COMPLETED", "FAILED"},
            timeout=300,
        )
        assert cls.utility["state"] == "COMPLETED", cls.utility

        cross = cls.client.post(
            f"/api/projects/{cls.source_project_id}/cross-matcher",
            json={
                "matrix_id": cls.matrix_id,
                "quality_job_id": cls.quality_job_id,
                "bootstrap_replicates": 50,
                "seed": 13,
            },
        )
        assert cross.status_code == 202, cross.text
        cls.cross_analysis_id = cross.json()["analysis_id"]
        cls.cross = cls._wait_for(
            f"/api/projects/{cls.source_project_id}/cross-matcher/{cls.cross_analysis_id}",
            terminal=lambda body: body["state"] in {"COMPLETED", "FAILED"},
            timeout=600,
        )
        assert cls.cross["state"] == "COMPLETED", cls.cross
        cls.cross_contrast_id = cls.cross["document"]["contrasts"][0]["contrast_id"]

        figure = cls.client.post(
            f"/api/projects/{cls.source_project_id}/figures",
            json={"kind": "det_overlay", "parameters": {"matrix_id": cls.matrix_id}},
        )
        assert figure.status_code == 201, figure.text
        cls.figure_id = figure.json()["figure_id"]
        dashboard = cls.client.post(
            f"/api/projects/{cls.source_project_id}/dashboards",
            json={
                "name": "source figure dashboard",
                "template": "Matcher Evaluation",
                "columns": 1,
                "panels": [{"kind": "figure", "ref": cls.figure_id}],
            },
        )
        assert dashboard.status_code == 201, dashboard.text
        cls.source_dashboard_id = dashboard.json()["dashboard_id"]

        destination = cls.client.post(
            "/api/projects",
            json={
                "name": "report ownership destination",
                "description": "Independent real LFW project for ownership denial.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        assert destination.status_code == 201, destination.text
        cls.destination_project_id = destination.json()["project_id"]
        opened = cls.client.post(f"/api/projects/{cls.destination_project_id}/open")
        assert opened.status_code == 200, opened.text
        imported = cls.client.post(
            f"/api/projects/{cls.destination_project_id}/datasets/import-registered",
            json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
        )
        assert imported.status_code == 201, imported.text
        cls.destination_dataset_id = imported.json()["dataset_id"]
        cls.store = cls._run_store()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    @classmethod
    def _wait_for(cls, path: str, *, terminal, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        body: dict = {}
        while time.monotonic() < deadline:
            response = cls.client.get(path)
            assert response.status_code == 200, response.text
            body = response.json()
            if terminal(body):
                return body
            time.sleep(0.5)
        raise AssertionError(f"real operation did not finish: {path}; last response: {body}")

    @classmethod
    def _run_store(cls):
        route = next(
            route
            for route in cls.client.app.routes
            if getattr(route, "path", None)
            == "/api/projects/{project_id}/reports/{report_id}/file"
        )
        return inspect.getclosurevars(route.endpoint).nonlocals["store"]()

    @contextmanager
    def _withheld_real_artifact(self, path: Path | None):
        if path is None:
            yield
            return
        self.assertTrue(path.is_file(), path)
        held = path.with_name(path.name + ".ownership-test-held")
        self.assertFalse(held.exists(), held)
        path.rename(held)
        try:
            yield
            self.assertFalse(path.exists(), "the source artifact was unexpectedly recreated")
            self.assertTrue(held.is_file(), "the held source artifact disappeared")
        finally:
            if held.exists():
                held.rename(path)

    def _assert_foreign_figure_refused(
        self, *, kind: str, parameters: dict, artifact: Path | None = None
    ) -> None:
        before = len(self.store.figures(project_id=self.destination_project_id))
        with self._withheld_real_artifact(artifact):
            response = self.client.post(
                f"/api/projects/{self.destination_project_id}/figures",
                json={"kind": kind, "parameters": parameters},
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("not recorded", response.json()["detail"])
        self.assertEqual(
            len(self.store.figures(project_id=self.destination_project_id)), before
        )

    def _assert_foreign_report_refused(
        self, *, spec: dict, artifact: Path | None = None
    ) -> None:
        before_rows = len(self.store.reports(project_id=self.destination_project_id))
        report_root = self.data_dir / "reports"
        before_directories = set(report_root.iterdir()) if report_root.exists() else set()
        with self._withheld_real_artifact(artifact):
            response = self.client.post(
                f"/api/projects/{self.destination_project_id}/reports",
                json={"spec": {"title": "foreign evidence must be refused", **spec}},
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("not recorded", response.json()["detail"])
        self.assertEqual(
            len(self.store.reports(project_id=self.destination_project_id)), before_rows
        )
        after_directories = set(report_root.iterdir()) if report_root.exists() else set()
        self.assertEqual(after_directories, before_directories)

    def test_foreign_sources_fail_before_bytes_or_destination_evidence(self) -> None:
        matcher = self.store.matcher_leaf(self.matcher_run_id)
        quality = self.store.job(self.quality_job_id)
        utility = self.store.utility_analysis(self.utility_analysis_id)
        cross = self.store.cross_matcher_analysis(self.cross_analysis_id)
        figure = self.store.figure(self.figure_id)
        self.assertIsNotNone(matcher)
        self.assertIsNotNone(quality)
        self.assertIsNotNone(utility)
        self.assertIsNotNone(cross)
        self.assertIsNotNone(figure)
        figure_provenance = json.loads(figure["provenance_json"])

        figure_cases = (
            ("matrix", "det_overlay", {"matrix_id": self.matrix_id}, None),
            (
                "matcher run",
                "score_distributions",
                {"run_id": self.matcher_run_id, "bins": 10},
                Path(matcher["comparisons_parquet"]),
            ),
            (
                "utility analysis",
                "erc_overlay",
                {"analysis_ids": [self.utility_analysis_id]},
                Path(utility["directory"]) / "utility.json",
            ),
            (
                "quality job",
                "quality_histogram",
                {"job_id": self.quality_job_id, "bins": 10},
                Path(quality["parquet_path"]),
            ),
            (
                "cross-matcher analysis",
                "error_overlap",
                {"analysis_id": self.cross_analysis_id},
                Path(cross["directory"]) / "crossmatcher.json",
            ),
        )
        for family, kind, parameters, artifact in figure_cases:
            with self.subTest(surface="figure", family=family):
                self._assert_foreign_figure_refused(
                    kind=kind, parameters=parameters, artifact=artifact
                )

        report_cases = (
            ("dataset", {"dataset_id": self.source_dataset_id}, None),
            ("protocol", {"protocol_id": self.source_protocol_id}, None),
            ("quality", {"quality_job_id": self.quality_job_id}, None),
            ("matcher", {"matrix_id": self.matrix_id}, None),
            (
                "utility",
                {"utility_analysis_ids": [self.utility_analysis_id]},
                Path(utility["directory"]) / "utility.json",
            ),
            (
                "cross-matcher",
                {"cross_matcher_analysis_id": self.cross_analysis_id},
                Path(cross["directory"]) / "crossmatcher.json",
            ),
            (
                "figure",
                {"figure_ids": [self.figure_id]},
                Path(figure_provenance["files"]["svg"]["path"]),
            ),
            (
                "utility failure images",
                {
                    "failure_examples": {
                        "analysis_id": self.utility_analysis_id,
                        "limit": 3,
                    },
                    "image_policy": "INCLUDE_PERMITTED",
                },
                Path(utility["directory"]) / "pair_quality.parquet",
            ),
            (
                "cross-matcher failure images",
                {
                    "failure_examples": {
                        "analysis_id": self.cross_analysis_id,
                        "contrast_id": self.cross_contrast_id,
                        "category": "BOTH",
                        "limit": 3,
                    },
                    "image_policy": "INCLUDE_PERMITTED",
                },
                Path(cross["directory"]) / "contrast_pairs.parquet",
            ),
        )
        for family, spec, artifact in report_cases:
            with self.subTest(surface="report", family=family):
                self._assert_foreign_report_refused(spec=spec, artifact=artifact)

        before = len(self.store.dashboards(project_id=self.destination_project_id))
        response = self.client.post(
            f"/api/projects/{self.destination_project_id}/dashboards",
            json={
                "name": "must not supersede a foreign dashboard",
                "template": "Dataset Overview",
                "columns": 1,
                "panels": [
                    {"kind": "dataset_statistics", "ref": self.destination_dataset_id}
                ],
                "supersedes": self.source_dashboard_id,
            },
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("not recorded", response.json()["detail"])
        self.assertEqual(
            len(self.store.dashboards(project_id=self.destination_project_id)), before
        )

        for family, kind, parameters, _artifact in figure_cases:
            with self.subTest(surface="same-project figure", family=family):
                response = self.client.post(
                    f"/api/projects/{self.source_project_id}/figures",
                    json={"kind": kind, "parameters": parameters},
                )
                self.assertEqual(response.status_code, 201, response.text)

        report = self.client.post(
            f"/api/projects/{self.source_project_id}/reports",
            json={
                "spec": {
                    "title": "project-owned real LFW evidence",
                    "dataset_id": self.source_dataset_id,
                    "protocol_id": self.source_protocol_id,
                    "quality_job_id": self.quality_job_id,
                    "matrix_id": self.matrix_id,
                    "utility_analysis_ids": [self.utility_analysis_id],
                    "cross_matcher_analysis_id": self.cross_analysis_id,
                    "figure_ids": [self.figure_id],
                    "failure_examples": {
                        "analysis_id": self.utility_analysis_id,
                        "limit": 3,
                    },
                    "image_policy": "EXCLUDE",
                }
            },
        )
        self.assertEqual(report.status_code, 201, report.text)

        superseded = self.client.post(
            f"/api/projects/{self.source_project_id}/dashboards",
            json={
                "name": "same-project successor",
                "template": "Matcher Evaluation",
                "columns": 1,
                "panels": [{"kind": "figure", "ref": self.figure_id}],
                "supersedes": self.source_dashboard_id,
            },
        )
        self.assertEqual(superseded.status_code, 201, superseded.text)
        self.assertEqual(superseded.json()["version"], 2)

    def test_sigkill_include_report_partial_is_removed_by_verified_project_purge(
        self,
    ) -> None:
        sample_hashes = {
            sample.sha256
            for sample in self.store.samples_for_dataset(
                self.source_dataset_id, limit=1_000_000
            )
        }
        self.assertTrue(sample_hashes)
        type(self).client.__exit__(None, None, None)
        worker_source = """
from pathlib import Path
import sys
from fastapi.testclient import TestClient
from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID

root = Path(sys.argv[1])
data_dir = Path(sys.argv[2])
project_id = sys.argv[3]
analysis_id = sys.argv[4]
settings = Settings(
    repository_root=root,
    registry_path=root / "resources" / "resource-registry.json",
    data_dir=data_dir,
    runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
    input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, "lfw-funneled-local-candidate"),
)
with TestClient(create_app(settings), base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
    response = client.post(
        f"/api/projects/{project_id}/reports",
        json={
            "spec": {
                "title": "real LFW interrupted INCLUDE report",
                "failure_examples": {"analysis_id": analysis_id, "limit": 10},
                "image_policy": "INCLUDE_PERMITTED",
            }
        },
    )
    if response.status_code != 201:
        raise RuntimeError(response.text)
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
                str(ROOT),
                str(self.data_dir),
                self.source_project_id,
                self.utility_analysis_id,
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        partial: Path | None = None
        embedded_sha256: str | None = None
        deadline = time.monotonic() + 120
        try:
            while time.monotonic() < deadline and worker.poll() is None:
                for candidate in (self.data_dir / "reports").glob(".*.partial"):
                    html_path = candidate / "report.html"
                    if not html_path.is_file():
                        continue
                    html = html_path.read_text(encoding="utf-8")
                    matched = re.search(
                        r"data:image/jpeg;base64,([A-Za-z0-9+/=]+)", html
                    )
                    if matched is None:
                        continue
                    embedded_sha256 = hashlib.sha256(
                        base64.b64decode(matched.group(1), validate=True)
                    ).hexdigest()
                    partial = candidate
                    os.killpg(worker.pid, signal.SIGKILL)
                    worker.wait(timeout=30)
                    break
                if partial is not None:
                    break
                time.sleep(0.002)
            if partial is None:
                output = worker.communicate(timeout=30)[0]
                self.fail(
                    "real INCLUDE report did not expose an interruptible private stage: "
                    + output
                )
        finally:
            if worker.poll() is None:
                os.killpg(worker.pid, signal.SIGKILL)
                worker.wait(timeout=30)
            if worker.stdout is not None:
                worker.stdout.close()
        self.assertIn(embedded_sha256, sample_hashes)
        assert partial is not None
        self.assertTrue(partial.is_dir())

        type(self).client = TestClient(
            create_app(self.settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        )
        type(self).client.__enter__()
        type(self).store = self._run_store()
        purged = self.client.post(
            f"/api/projects/{self.source_project_id}/purge",
            json={"confirm": self.source_project_id},
        )
        self.assertEqual(purged.status_code, 201, purged.text)
        self.assertTrue(purged.json()["verified"], purged.json())
        self.assertFalse(
            partial.exists(),
            "verified purge retained a real biometric reporting stage",
        )

if __name__ == "__main__":
    unittest.main()
