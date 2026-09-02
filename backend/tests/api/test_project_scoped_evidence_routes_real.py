"""Project ownership is mandatory at every recorded-evidence HTTP boundary."""

from __future__ import annotations

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
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
CONFORMANCE_SCOPE = "REPRODUCE_OFIQ_CONFORMANCE_TESTS"


class ProjectScopedEvidenceRouteContractTests(unittest.TestCase):
    """Exercise the real FastAPI route table; no substitute application is used."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-evidence-routes-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        cls.client = TestClient(create_app(settings), base_url="http://127.0.0.1")
        cls.client.__enter__()
        cls.registered_sample_id = next(
            item["registered_sample_id"]
            for item in cls.client.get("/api/samples/registered").json()
            if item["resource_id"] == OFIQ_INPUT_RESOURCE_ID
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def test_recorded_evidence_routes_require_project_identity_and_globals_are_retired(self) -> None:
        routes = {
            (method, route.path)
            for route in self.client.app.routes
            for method in getattr(route, "methods", set())
        }
        scoped = {
            ("GET", "/api/projects/{project_id}/operations/{operation_id}"),
            ("POST", "/api/projects/{project_id}/operations/{operation_id}/retry"),
            ("GET", "/api/projects/{project_id}/protocols/{protocol_id}"),
            ("GET", "/api/projects/{project_id}/protocols/{protocol_id}/pairs"),
            ("GET", "/api/projects/{project_id}/degradation-analyses/{analysis_id}"),
            ("GET", "/api/projects/{project_id}/identification-protocols/{protocol_id}"),
            ("GET", "/api/projects/{project_id}/identification-searches/{search_id}"),
            ("GET", "/api/projects/{project_id}/identification-searches/{search_id}/candidates"),
            ("GET", "/api/projects/{project_id}/matrices/{matrix_id}"),
            ("GET", "/api/projects/{project_id}/matrices/{matrix_id}/sets"),
            ("GET", "/api/projects/{project_id}/matcher-runs/{run_id}/metrics"),
            ("GET", "/api/projects/{project_id}/matcher-runs/{run_id}/comparisons"),
            ("GET", "/api/projects/{project_id}/matcher-runs/{run_id}/errors"),
            ("GET", "/api/projects/{project_id}/matcher-runs/{run_id}/erc"),
            ("GET", "/api/projects/{project_id}/matcher-runs/{run_id}/pairs/{pair_id}/evidence"),
            ("GET", "/api/projects/{project_id}/matcher-runs/{run_id}/pairs/{pair_id}/crops/{side}"),
            ("GET", "/api/projects/{project_id}/utility-analyses/{analysis_id}"),
            ("GET", "/api/projects/{project_id}/utility-analyses/{analysis_id}/pairs"),
            ("GET", "/api/projects/{project_id}/cross-matcher/{analysis_id}"),
            ("GET", "/api/projects/{project_id}/cross-matcher/{analysis_id}/pairs"),
            ("POST", "/api/projects/{project_id}/annotations"),
            ("GET", "/api/projects/{project_id}/annotations"),
            ("GET", "/api/projects/{project_id}/figures/{figure_id}"),
            ("GET", "/api/projects/{project_id}/figures/{figure_id}/file"),
            ("GET", "/api/projects/{project_id}/dashboards/{dashboard_id}"),
            ("GET", "/api/projects/{project_id}/reports/{report_id}/file"),
            ("POST", "/api/projects/{project_id}/exports/{category}"),
            ("GET", "/api/projects/{project_id}/bundles/{bundle_id}/file"),
            ("POST", "/api/projects/{project_id}/reproductions"),
            ("GET", "/api/projects/{project_id}/reproductions"),
            ("GET", "/api/projects/{project_id}/runs"),
            ("GET", "/api/projects/{project_id}/runs/{run_id}"),
            ("GET", "/api/projects/{project_id}/runs/{run_id}/audit"),
            ("GET", "/api/projects/{project_id}/experiments/{experiment_id}"),
        }
        self.assertEqual(scoped - routes, set())

        retired = {
            ("POST", "/api/operations/{operation_id}/retry"),
            ("GET", "/api/operations/{operation_id}"),
            ("GET", "/api/protocols/{protocol_id}"),
            ("GET", "/api/protocols/{protocol_id}/pairs"),
            ("GET", "/api/degradation-analyses/{analysis_id}"),
            ("GET", "/api/identification-protocols/{protocol_id}"),
            ("GET", "/api/identification-searches/{search_id}"),
            ("GET", "/api/identification-searches/{search_id}/candidates"),
            ("GET", "/api/matrices/{matrix_id}"),
            ("GET", "/api/matrices/{matrix_id}/sets"),
            ("GET", "/api/matcher-runs/{run_id}/metrics"),
            ("GET", "/api/matcher-runs/{run_id}/comparisons"),
            ("GET", "/api/matcher-runs/{run_id}/errors"),
            ("GET", "/api/matcher-runs/{run_id}/erc"),
            ("GET", "/api/matcher-runs/{run_id}/pairs/{pair_id}/evidence"),
            ("GET", "/api/matcher-runs/{run_id}/pairs/{pair_id}/crops/{side}"),
            ("GET", "/api/utility-analyses/{analysis_id}"),
            ("GET", "/api/utility-analyses/{analysis_id}/pairs"),
            ("GET", "/api/cross-matcher/{analysis_id}"),
            ("GET", "/api/cross-matcher/{analysis_id}/pairs"),
            ("POST", "/api/annotations"),
            ("GET", "/api/annotations"),
            ("GET", "/api/figures/{figure_id}"),
            ("GET", "/api/figures/{figure_id}/file"),
            ("GET", "/api/dashboards/{dashboard_id}"),
            ("GET", "/api/reports/{report_id}/file"),
            ("GET", "/api/exports/{category}"),
            ("GET", "/api/bundles/{bundle_id}/file"),
            ("POST", "/api/reproductions"),
            ("GET", "/api/reproductions"),
            ("GET", "/api/runs"),
            ("GET", "/api/runs/{run_id}"),
            ("GET", "/api/runs/{run_id}/audit"),
            ("GET", "/api/experiments/{experiment_id}"),
        }
        self.assertEqual(retired & routes, set())
        replacements = {
            "{operation_id}": "operation:sha256:" + "1" * 64,
            "{protocol_id}": "protocol:sha256:" + "2" * 64,
            "{analysis_id}": "analysis:sha256:" + "3" * 64,
            "{search_id}": "identification-search:sha256:" + "4" * 64,
            "{matrix_id}": "matrix:sha256:" + "5" * 64,
            "{run_id}": "run:sha256:" + "6" * 64,
            "{pair_id}": "pair:sha256:" + "7" * 64,
            "{side}": "left",
            "{figure_id}": "figure:sha256:" + "8" * 64,
            "{dashboard_id}": "dashboard:sha256:" + "9" * 64,
            "{report_id}": "report:sha256:" + "a" * 64,
            "{category}": "quality_results",
            "{bundle_id}": "bundle:sha256:" + "b" * 64,
            "{experiment_id}": "experiment:sha256:" + "c" * 64,
        }
        for method, template in sorted(retired):
            concrete = template
            for marker, value in replacements.items():
                concrete = concrete.replace(marker, value)
            with self.subTest(method=method, retired_route=template):
                response = self.client.request(method, concrete)
                self.assertEqual(response.status_code, 404, response.text)
                lowered = {name.lower() for name in response.headers}
                self.assertNotIn("content-disposition", lowered)
                self.assertFalse(any("sha256" in name or "classification" in name for name in lowered))

    def test_unknown_project_cannot_be_used_as_an_empty_evidence_inventory(self) -> None:
        unknown_project_id = "project:sha256:" + "f" * 64
        response = self.client.get(f"/api/projects/{unknown_project_id}/protocols")
        self.assertEqual(response.status_code, 404, response.text)

    def test_real_run_reads_are_project_bound_and_retired_globals_disclose_nothing(self) -> None:
        projects: list[str] = []
        for name in ("Recorded evidence owner", "Recorded evidence foreign project"):
            created = self.client.post(
                "/api/projects",
                json={
                    "name": name,
                    "description": "Real OFIQ conformance evidence authorization gate.",
                    "purpose_scope": CONFORMANCE_SCOPE,
                },
            )
            self.assertEqual(created.status_code, 201, created.text)
            project_id = created.json()["project_id"]
            opened = self.client.post(f"/api/projects/{project_id}/open")
            self.assertEqual(opened.status_code, 200, opened.text)
            projects.append(project_id)

        assessed = self.client.post(
            "/api/quality/assess",
            json={
                "registered_sample_id": self.registered_sample_id,
                "purpose_scope": CONFORMANCE_SCOPE,
                "project_id": projects[0],
            },
        )
        self.assertEqual(assessed.status_code, 200, assessed.text)
        assessment = assessed.json()
        run_id = assessment["run_id"]
        owned_run = self.client.get(f"/api/projects/{projects[0]}/runs/{run_id}")
        self.assertEqual(owned_run.status_code, 200, owned_run.text)
        experiment_id = owned_run.json()["run"]["experiment_id"]

        owned_urls = (
            f"/api/projects/{projects[0]}/runs/{run_id}",
            f"/api/projects/{projects[0]}/runs/{run_id}/audit",
            f"/api/projects/{projects[0]}/experiments/{experiment_id}",
        )
        foreign_urls = (
            f"/api/projects/{projects[1]}/runs/{run_id}",
            f"/api/projects/{projects[1]}/runs/{run_id}/audit",
            f"/api/projects/{projects[1]}/experiments/{experiment_id}",
        )
        retired_urls = (
            f"/api/runs/{run_id}",
            f"/api/runs/{run_id}/audit",
            f"/api/experiments/{experiment_id}",
        )
        for url in owned_urls:
            with self.subTest(scope="owned", url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, response.text)
        for scope, urls in (("foreign", foreign_urls), ("retired", retired_urls)):
            for url in urls:
                with self.subTest(scope=scope, url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 404, response.text)
                    lowered = {name.lower() for name in response.headers}
                    self.assertNotIn("content-disposition", lowered)
                    self.assertFalse(any("sha256" in name or "classification" in name for name in lowered))


if __name__ == "__main__":
    unittest.main()
