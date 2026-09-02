"""Crash-safety gates for synchronous reporting publications over real LFW evidence."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
import shutil
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
    PREPARED_OUTPUT_FILENAME,
    STAGING_IDENTITY_FILENAME,
    PreparedOutputError,
    create_staging_directory,
)
from qualsight.reporting import bundle as bundle_module
from qualsight.reporting import exports as export_module
from qualsight.reporting import figures as figure_module
from qualsight.reporting import reports as report_module
from qualsight.reporting.recovery import (
    ReportingRecoveryError,
    prepared_reporting_directories,
)


ROOT = Path(__file__).resolve().parents[3]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"
REAL_LFW_IMAGE = Path(
    "/mnt/projects/datasets/lfw/lfw_funneled/Abel_Pacheco/Abel_Pacheco_0001.jpg"
)
REAL_LFW_SHA256 = "8b6fff626f3d778d34027e19fc88c521a9e1307cf735b22a1406c17e55e8a967"


class RealReportingPublicationRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-report-publication-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        cls.settings = settings
        cls.client = TestClient(
            create_app(settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        )
        cls.client.__enter__()
        project = cls.client.post(
            "/api/projects",
            json={
                "name": "real reporting publication recovery",
                "description": "Real LFW evidence for reporting publication recovery.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        assert project.status_code == 201, project.text
        cls.project_id = project.json()["project_id"]
        opened = cls.client.post(f"/api/projects/{cls.project_id}/open")
        assert opened.status_code == 200, opened.text
        imported = cls.client.post(
            f"/api/projects/{cls.project_id}/datasets/import-registered",
            json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
        )
        assert imported.status_code == 201, imported.text
        cls.dataset_id = imported.json()["dataset_id"]
        started = cls.client.post(
            f"/api/projects/{cls.project_id}/datasets/{cls.dataset_id}/quality-batch",
            json={"selection": {"kind": "subset_fixture"}, "chunks": 1},
        )
        assert started.status_code == 202, started.text
        cls.job_id = started.json()["job_id"]
        cls.job = cls._wait_for(
            f"/api/projects/{cls.project_id}/jobs/{cls.job_id}",
            timeout=300,
        )
        assert cls.job["state"] == "COMPLETED", cls.job
        assert cls.job["quality_result_available"] is True, cls.job
        cls.run_id = cls.job["run_id"]
        cls.store = cls._run_store()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    @classmethod
    def _wait_for(cls, path: str, *, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        body: dict = {}
        while time.monotonic() < deadline:
            response = cls.client.get(path)
            assert response.status_code == 200, response.text
            body = response.json()
            if body.get("ended_at") is not None:
                return body
            time.sleep(0.25)
        raise AssertionError(f"real operation did not finish; last response: {body}")

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
    def _refuse_real_insert(self, table: str, *, subject_type: str | None = None):
        supported = {"figures", "reports", "bundles", "audit_events"}
        self.assertIn(table, supported)
        trigger = f"refuse_real_reporting_{table}_insert"
        when = ""
        if subject_type is not None:
            self.assertEqual(table, "audit_events")
            self.assertEqual(subject_type, "Export")
            when = " WHEN NEW.subject_type = 'Export'"
        with self.store._lock:
            self.store._connection.execute(
                f"CREATE TRIGGER {trigger} BEFORE INSERT ON {table}{when} BEGIN "
                "SELECT RAISE(ABORT, 'real reporting persistence refusal'); END"
            )
            self.store._connection.commit()
        try:
            yield
        finally:
            with self.store._lock:
                self.store._connection.rollback()
                self.store._connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
                self.store._connection.commit()

    def _assert_prepared(
        self,
        directory: Path,
        *,
        operation_kind: str,
        artifact_names: set[str],
    ) -> dict:
        receipt_path = directory / PREPARED_OUTPUT_FILENAME
        self.assertTrue(receipt_path.is_file(), receipt_path)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(receipt),
            {
                "schema_version",
                "operation_kind",
                "result_id",
                "project_id",
                "request_sha256",
                "artifacts",
                "record",
            },
        )
        self.assertEqual(receipt["schema_version"], "1.0.0")
        self.assertEqual(receipt["operation_kind"], operation_kind)
        self.assertEqual(receipt["project_id"], self.project_id)
        self.assertRegex(receipt["request_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            {path.name for path in directory.iterdir()},
            {PREPARED_OUTPUT_FILENAME, *artifact_names},
        )
        self.assertEqual(
            [item["name"] for item in receipt["artifacts"]],
            sorted(artifact_names),
        )
        return receipt

    def test_failed_persistence_leaves_no_untracked_final_bytes_after_purge(self) -> None:
        roots = {
            "figures": self.data_dir / "figures",
            "reports": self.data_dir / "reports",
            "bundles": self.data_dir / "bundles",
            "exports": self.data_dir / "exports",
        }

        def children(root: Path) -> set[Path]:
            return set(root.iterdir()) if root.exists() else set()

        published: dict[str, set[Path]] = {}

        before = children(roots["figures"])
        with self._refuse_real_insert("figures"):
            response = self.client.post(
                f"/api/projects/{self.project_id}/figures",
                json={
                    "kind": "quality_histogram",
                    "parameters": {"job_id": self.job_id, "bins": 7},
                },
            )
        self.assertEqual(response.status_code, 500, response.text)
        published["figures"] = children(roots["figures"]) - before
        self.assertEqual(len(published["figures"]), 1, published)
        self.assertEqual(self.store.figures(project_id=self.project_id), [])

        before = children(roots["reports"])
        with self._refuse_real_insert("reports"):
            response = self.client.post(
                f"/api/projects/{self.project_id}/reports",
                json={
                    "spec": {
                        "title": "real LFW persistence refusal evidence",
                        "dataset_id": self.dataset_id,
                        "quality_job_id": self.job_id,
                    }
                },
            )
        self.assertEqual(response.status_code, 500, response.text)
        published["reports"] = children(roots["reports"]) - before
        self.assertEqual(len(published["reports"]), 1, published)
        self.assertEqual(self.store.reports(project_id=self.project_id), [])

        before = children(roots["bundles"])
        with self._refuse_real_insert("bundles"):
            response = self.client.post(
                f"/api/projects/{self.project_id}/bundles",
                json={"run_id": self.run_id},
            )
        self.assertEqual(response.status_code, 500, response.text)
        published["bundles"] = children(roots["bundles"]) - before
        self.assertEqual(len(published["bundles"]), 1, published)
        self.assertEqual(self.store.experiment_bundles(project_id=self.project_id), [])

        before = children(roots["exports"])
        with self._refuse_real_insert("audit_events", subject_type="Export"):
            response = self.client.post(
                f"/api/projects/{self.project_id}/exports/quality_results",
                params={"ref": self.job_id, "format": "csv"},
            )
        self.assertEqual(response.status_code, 500, response.text)
        published["exports"] = children(roots["exports"]) - before
        self.assertEqual(len(published["exports"]), 1, published)
        self.assertFalse(
            any(
                event.subject_type == "Export"
                for event in self.store.audit_events_for_project(self.project_id)
            )
        )

        figure_directory = next(iter(published["figures"]))
        figure_receipt = self._assert_prepared(
            figure_directory,
            operation_kind="FIGURE",
            artifact_names=set(figure_module.FIGURE_ARTIFACT_NAMES),
        )
        self.assertEqual(
            figure_module.recover_prepared_figure(
                figure_directory,
                figure_id=figure_receipt["result_id"],
                project_id=self.project_id,
                request_sha256=figure_receipt["request_sha256"],
            ),
            figure_receipt["record"],
        )

        report_directory = next(iter(published["reports"]))
        report_receipt = self._assert_prepared(
            report_directory,
            operation_kind="REPORT",
            artifact_names=set(report_module.REPORT_ARTIFACT_NAMES),
        )
        self.assertEqual(
            report_module.recover_prepared_report(
                report_directory,
                report_id=report_receipt["result_id"],
                project_id=self.project_id,
                request_sha256=report_receipt["request_sha256"],
            ),
            report_receipt["record"],
        )

        bundle_directory = next(iter(published["bundles"]))
        bundle_receipt = self._assert_prepared(
            bundle_directory,
            operation_kind="EXPERIMENT_BUNDLE",
            artifact_names=set(bundle_module.BUNDLE_ARTIFACT_NAMES),
        )
        self.assertEqual(
            bundle_module.recover_prepared_bundle(
                bundle_directory,
                bundle_id=bundle_receipt["result_id"],
                project_id=self.project_id,
                request_sha256=bundle_receipt["request_sha256"],
            ),
            bundle_receipt["record"],
        )

        export_directory = next(iter(published["exports"]))
        export_receipt = self._assert_prepared(
            export_directory,
            operation_kind="TABULAR_EXPORT",
            artifact_names={
                "quality_results.csv",
                "quality_results.csv.provenance.json",
            },
        )
        self.assertEqual(
            export_module.recover_prepared_export(
                export_directory,
                export_id=export_receipt["result_id"],
                project_id=self.project_id,
                category="quality_results",
                ref=self.job_id,
                fmt="csv",
                request_sha256=export_receipt["request_sha256"],
            ),
            export_receipt["record"],
        )

        scope = self.client.get(f"/api/projects/{self.project_id}/purge-scope")
        self.assertEqual(scope.status_code, 200, scope.text)
        purged = self.client.post(
            f"/api/projects/{self.project_id}/purge",
            json={"confirm": self.project_id},
        )
        self.assertEqual(purged.status_code, 201, purged.text)
        self.assertTrue(purged.json()["verified"], purged.json())

        survivors = {
            family: sorted(
                str(path.relative_to(self.data_dir))
                for path in paths
                if path.exists()
            )
            for family, paths in published.items()
            if any(path.exists() for path in paths)
        }
        self.assertEqual(
            survivors,
            {},
            "verified project purge left final reporting bytes that never reached SQLite",
        )


class RealReportingPublicationStartupRecoveryTests(
    RealReportingPublicationRecoveryTests
):
    test_failed_persistence_leaves_no_untracked_final_bytes_after_purge = None

    @staticmethod
    def _tree_identity(directory: Path) -> tuple[tuple[str, int, str, int], ...]:
        return tuple(
            (
                path.relative_to(directory).as_posix(),
                path.stat().st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mtime_ns,
            )
            for path in sorted(item for item in directory.rglob("*") if item.is_file())
        )

    def test_startup_reconciles_and_retry_reuses_exact_prepared_bytes(self) -> None:
        roots = {
            "figures": self.data_dir / "figures",
            "reports": self.data_dir / "reports",
            "bundles": self.data_dir / "bundles",
            "exports": self.data_dir / "exports",
        }

        def children(root: Path) -> set[Path]:
            return set(root.iterdir()) if root.exists() else set()

        before = {name: children(root) for name, root in roots.items()}
        with self._refuse_real_insert("figures"):
            response = self.client.post(
                f"/api/projects/{self.project_id}/figures",
                json={
                    "kind": "quality_histogram",
                    "parameters": {"job_id": self.job_id, "bins": 7},
                },
            )
        self.assertEqual(response.status_code, 500, response.text)
        with self._refuse_real_insert("reports"):
            response = self.client.post(
                f"/api/projects/{self.project_id}/reports",
                json={
                    "spec": {
                        "title": "real LFW persistence refusal evidence",
                        "dataset_id": self.dataset_id,
                        "quality_job_id": self.job_id,
                    }
                },
            )
        self.assertEqual(response.status_code, 500, response.text)
        with self._refuse_real_insert("bundles"):
            response = self.client.post(
                f"/api/projects/{self.project_id}/bundles",
                json={"run_id": self.run_id},
            )
        self.assertEqual(response.status_code, 500, response.text)
        with self._refuse_real_insert("audit_events", subject_type="Export"):
            response = self.client.post(
                f"/api/projects/{self.project_id}/exports/quality_results",
                params={"ref": self.job_id, "format": "csv"},
            )
        self.assertEqual(response.status_code, 500, response.text)

        published = {
            name: children(root) - before[name] for name, root in roots.items()
        }
        self.assertTrue(
            all(len(paths) == 1 for paths in published.values()), published
        )
        identities = {
            name: self._tree_identity(next(iter(paths)))
            for name, paths in published.items()
        }

        type(self).client.__exit__(None, None, None)
        type(self).client = TestClient(
            create_app(self.settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        )
        type(self).client.__enter__()
        type(self).store = self._run_store()

        self.assertEqual(len(self.store.figures(project_id=self.project_id)), 1)
        self.assertEqual(len(self.store.reports(project_id=self.project_id)), 1)
        self.assertEqual(
            len(self.store.experiment_bundles(project_id=self.project_id)), 1
        )
        events = self.store.audit_events_for_project(self.project_id)
        for subject_type in ("Figure", "Report", "Bundle", "Export"):
            self.assertEqual(
                sum(event.subject_type == subject_type for event in events),
                1,
                subject_type,
            )
        self.assertEqual(
            {
                name: self._tree_identity(next(iter(paths)))
                for name, paths in published.items()
            },
            identities,
        )

        figure = self.client.post(
            f"/api/projects/{self.project_id}/figures",
            json={
                "kind": "quality_histogram",
                "parameters": {"job_id": self.job_id, "bins": 7},
            },
        )
        self.assertEqual(figure.status_code, 201, figure.text)
        report = self.client.post(
            f"/api/projects/{self.project_id}/reports",
            json={
                "spec": {
                    "title": "real LFW persistence refusal evidence",
                    "dataset_id": self.dataset_id,
                    "quality_job_id": self.job_id,
                }
            },
        )
        self.assertEqual(report.status_code, 201, report.text)
        bundle = self.client.post(
            f"/api/projects/{self.project_id}/bundles",
            json={"run_id": self.run_id},
        )
        self.assertEqual(bundle.status_code, 201, bundle.text)
        exported = self.client.post(
            f"/api/projects/{self.project_id}/exports/quality_results",
            params={"ref": self.job_id, "format": "csv"},
        )
        self.assertEqual(exported.status_code, 200, exported.text)
        retry_events = self.store.audit_events_for_project(self.project_id)
        for subject_type in ("Figure", "Report", "Bundle", "Export"):
            self.assertEqual(
                sum(event.subject_type == subject_type for event in retry_events),
                1,
                subject_type,
            )
        self.assertEqual(
            {
                name: self._tree_identity(next(iter(paths)))
                for name, paths in published.items()
            },
            identities,
        )

        recorded_actors = {
            event.actor_id
            for event in self.store.audit_events_for_project(self.project_id)
            if event.subject_type in {"Figure", "Report", "Bundle", "Export"}
        }
        self.assertEqual(len(recorded_actors), 1)
        type(self).client.__exit__(None, None, None)
        type(self).settings = replace(
            self.settings,
            actor_id="real-reporting-recovery-second-operator",
        )
        type(self).client = TestClient(
            create_app(self.settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        )
        type(self).client.__enter__()
        type(self).store = self._run_store()
        self.assertEqual(
            {
                event.actor_id
                for event in self.store.audit_events_for_project(self.project_id)
                if event.subject_type
                in {"Figure", "Report", "Bundle", "Export"}
            },
            recorded_actors,
        )

        purged = self.client.post(
            f"/api/projects/{self.project_id}/purge",
            json={"confirm": self.project_id},
        )
        self.assertEqual(purged.status_code, 201, purged.text)
        self.assertTrue(purged.json()["verified"], purged.json())
        self.assertFalse(
            any(path.exists() for paths in published.values() for path in paths)
        )


class RealReportingReportIdentityTests(RealReportingPublicationRecoveryTests):
    test_failed_persistence_leaves_no_untracked_final_bytes_after_purge = None

    def _create_real_report(self) -> dict:
        response = self.client.post(
            f"/api/projects/{self.project_id}/reports",
            json={
                "spec": {
                    "title": "real LFW deterministic report identity",
                    "dataset_id": self.dataset_id,
                    "quality_job_id": self.job_id,
                }
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_report_identity_binds_project_name_and_configured_actor(self) -> None:
        original = self._create_real_report()
        saved = self.client.post(
            f"/api/projects/{self.project_id}/save",
            json={"name": "real reporting publication recovery renamed"},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        renamed = self._create_real_report()
        self.assertNotEqual(renamed["report_id"], original["report_id"])

        type(self).client.__exit__(None, None, None)
        type(self).settings = replace(
            self.settings,
            actor_id="real-reporting-identity-second-operator",
        )
        type(self).client = TestClient(
            create_app(self.settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        )
        type(self).client.__enter__()
        type(self).store = self._run_store()
        second_actor = self._create_real_report()
        self.assertNotEqual(second_actor["report_id"], renamed["report_id"])

        purged = self.client.post(
            f"/api/projects/{self.project_id}/purge",
            json={"confirm": self.project_id},
        )
        self.assertEqual(purged.status_code, 201, purged.text)
        self.assertTrue(purged.json()["verified"], purged.json())


class ReportingPartialIdentityBoundaryTests(unittest.TestCase):
    def test_foreign_or_malformed_real_stage_identity_is_refused_without_deletion(
        self,
    ) -> None:
        self.assertTrue(REAL_LFW_IMAGE.is_file())
        self.assertEqual(
            hashlib.sha256(REAL_LFW_IMAGE.read_bytes()).hexdigest(),
            REAL_LFW_SHA256,
        )
        data_dir = Path(tempfile.mkdtemp(prefix="qualsight-report-stage-boundary-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        try:
            with TestClient(
                create_app(settings),
                base_url="http://127.0.0.1",
                raise_server_exceptions=False,
            ) as client:
                projects = []
                for name in ("real stage owner", "foreign real stage owner"):
                    response = client.post(
                        "/api/projects",
                        json={
                            "name": name,
                            "description": "Real LFW stage identity boundary evidence.",
                            "purpose_scope": RESEARCH_SCOPE,
                        },
                    )
                    self.assertEqual(response.status_code, 201, response.text)
                    projects.append(response.json()["project_id"])
            owner_project_id, foreign_project_id = projects
            request_sha256 = hashlib.sha256(
                (REAL_LFW_SHA256 + owner_project_id).encode("utf-8")
            ).hexdigest()
            figure_id = "figure:sha256:" + request_sha256
            target = data_dir / "figures" / figure_id.replace(":", "_")
            stage = create_staging_directory(
                target,
                operation_kind="FIGURE",
                result_id=figure_id,
                project_id=owner_project_id,
                request_sha256=request_sha256,
            )
            shutil.copyfile(REAL_LFW_IMAGE, stage / "real-lfw-image.jpg")
            self.assertIn(
                stage,
                prepared_reporting_directories(data_dir, owner_project_id),
            )
            self.assertNotIn(
                stage,
                prepared_reporting_directories(data_dir, foreign_project_id),
            )
            with self.assertRaisesRegex(
                PreparedOutputError, "staging identity differs"
            ):
                create_staging_directory(
                    target,
                    operation_kind="FIGURE",
                    result_id=figure_id,
                    project_id=foreign_project_id,
                    request_sha256=request_sha256,
                )
            self.assertTrue(stage.is_dir())

            marker = stage / STAGING_IDENTITY_FILENAME
            marker_bytes = marker.read_bytes()
            marker.write_bytes(marker_bytes[:-1])
            with self.assertRaisesRegex(
                ReportingRecoveryError, "failed identity verification"
            ):
                prepared_reporting_directories(data_dir, owner_project_id)
            self.assertTrue(stage.is_dir())
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
