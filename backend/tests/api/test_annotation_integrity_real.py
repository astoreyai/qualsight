"""Annotation ownership and evidence transactions against the real API and SQLite store."""

from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3
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


class AnnotationIntegrityTests(unittest.TestCase):
    """Exercise annotation writes through the production FastAPI and SQLite path."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-annotation-integrity-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        cls.client = TestClient(
            create_app(settings),
            base_url="http://127.0.0.1",
            raise_server_exceptions=False,
        )
        cls.client.__enter__()
        cls.database_path = cls.data_dir / "qualsight.sqlite3"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def _create_project(self, name: str) -> str:
        created = self.client.post(
            "/api/projects",
            json={
                "name": name,
                "description": "QualSight annotation evidence-integrity verification.",
                "purpose_scope": CONFORMANCE_SCOPE,
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        project_id = created.json()["project_id"]
        opened = self.client.post(f"/api/projects/{project_id}/open")
        self.assertEqual(opened.status_code, 200, opened.text)
        return project_id

    def _annotations(self, project_id: str) -> list[dict[str, object]]:
        response = self.client.get(f"/api/projects/{project_id}/annotations")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_foreign_project_cannot_be_bound_as_an_annotation_subject(self) -> None:
        owner_project_id = self._create_project("Annotation owner")
        foreign_project_id = self._create_project("Foreign annotation subject")
        before = self._annotations(owner_project_id)

        response = self.client.post(
            f"/api/projects/{owner_project_id}/annotations",
            json={
                "subject_type": "Project",
                "subject_id": foreign_project_id,
                "text": "Cross-project evidence binding must be rejected.",
                "context": {},
            },
        )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self._annotations(owner_project_id), before)

    def test_unknown_annotation_subject_type_is_rejected_before_persistence(self) -> None:
        project_id = self._create_project("Closed annotation subject types")
        before = self._annotations(project_id)

        response = self.client.post(
            f"/api/projects/{project_id}/annotations",
            json={
                "subject_type": "UNSUPPORTED_EVIDENCE_SUBJECT",
                "subject_id": project_id,
                "text": "Unsupported subject types must not enter evidence.",
                "context": {},
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self._annotations(project_id), before)

    def test_audit_insert_failure_rolls_back_the_annotation(self) -> None:
        project_id = self._create_project("Atomic annotation evidence")
        before = self._annotations(project_id)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_annotation_audit BEFORE INSERT ON audit_events "
                "BEGIN SELECT RAISE(ABORT, 'annotation audit persistence rejected'); END"
            )

        try:
            response = self.client.post(
                f"/api/projects/{project_id}/annotations",
                json={
                    "subject_type": "Project",
                    "subject_id": project_id,
                    "text": "The annotation and its audit event must commit together.",
                    "context": {},
                },
            )
        finally:
            with sqlite3.connect(self.database_path) as connection:
                connection.execute("DROP TRIGGER reject_annotation_audit")

        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(self._annotations(project_id), before)

    def test_owned_project_annotation_records_one_linked_audit_event(self) -> None:
        project_id = self._create_project("Owned annotation subject")
        with sqlite3.connect(self.database_path) as connection:
            audit_before = connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE project_id = ?", (project_id,)
            ).fetchone()[0]

        response = self.client.post(
            f"/api/projects/{project_id}/annotations",
            json={
                "subject_type": "Project",
                "subject_id": project_id,
                "text": "This project owns the annotated evidence subject.",
                "context": {},
            },
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(len(self._annotations(project_id)), 1)
        with sqlite3.connect(self.database_path) as connection:
            annotation_audits = connection.execute(
                "SELECT record_json FROM audit_events WHERE project_id = ? ORDER BY rowid",
                (project_id,),
            ).fetchall()[audit_before:]
        self.assertEqual(len(annotation_audits), 1)
        self.assertIn('"event_type":"ANNOTATION_MODIFIED"', annotation_audits[0][0])


if __name__ == "__main__":
    unittest.main()
