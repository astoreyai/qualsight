"""Plan D cycle C27 — owner-supplied local inputs (decisions 30-31).

QS-Y80 admission layer, QS-Y81 single-photo upload -> recorded Run, QS-Y82
identity-labelled directory / CSV-manifest import + batch OFIQ, QS-Y83
deterministic owner pair protocol + matcher execution. Every image byte in
these tests is a real registered LFW photograph copied (unaltered) outside the
registry tree to stand in for an evaluator's own files; nothing is synthetic.
"""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)
from qualsight.security.purge import purge_project

ROOT = Path(__file__).resolve().parents[3]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
LFW_TREE = Path("/mnt/projects/datasets/lfw/lfw_funneled")
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"
ABEL_LEFT = LFW_TREE / "Abel_Pacheco" / "Abel_Pacheco_0001.jpg"
ABEL_RIGHT = LFW_TREE / "Abel_Pacheco" / "Abel_Pacheco_0004.jpg"
ABEL_LEFT_SHA256 = "8b6fff626f3d778d34027e19fc88c521a9e1307cf735b22a1406c17e55e8a967"
ABEL_RIGHT_SHA256 = "8050daa859e7e05aedd5c7ec3851e5d5bddcb2b9797d23958664ec2958a230ae"
ABDEL_MADI = LFW_TREE / "Abdel_Madi_Shabneh" / "Abdel_Madi_Shabneh_0001.jpg"
DEAN_BARKER = LFW_TREE / "Dean_Barker" / "Dean_Barker_0001.jpg"
ABDEL_MADI_SHA256 = "88c6cb6b3427d48b204a826290dff866714d8f89660898aa5a87eaa4a175ef88"
DEAN_BARKER_SHA256 = "8f92327e93346682df3524d05989990cfb60f1e12a97add6056946a48eb80ebd"
FACEOCC_TEXTURE = Path("/mnt/projects/datasets/faceocc/FaceOcc/texture/0001.png")
FACEOCC_TEXTURE_SHA256 = "222b3e538f8ac29a2561d9d7f3a89df2833370a84e993ad5918ea9fdee566696"

# Real people with >= 3 images each in the registered LFW tree; the copies
# below simulate an evaluator's own directory layout.
SUBJECTS = {
    "operator_a": ("George_W_Bush", 4),
    "operator_b": ("Tony_Blair", 4),
    "operator_c": ("Serena_Williams", 3),
}


class OwnerSuppliedRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-owner-"))
        cls.owner_root = Path(tempfile.mkdtemp(prefix="qualsight-evaluator-"))
        for alias, (subject, count) in SUBJECTS.items():
            source_dir = LFW_TREE / subject
            files = sorted(source_dir.glob("*.jpg"))[:count]
            assert len(files) == count, f"registered LFW subject {subject} lacks {count} images"
            target = cls.owner_root / alias
            target.mkdir(parents=True)
            for index, path in enumerate(files):
                shutil.copyfile(path, target / f"photo_{index:02d}.jpg")
        cls.settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        cls.client = TestClient(
            create_app(cls.settings), base_url="http://127.0.0.1"
        )
        cls.client.__enter__()
        cls.uploaded_pairs: dict[str, tuple[dict, dict]] = {}
        project = cls.client.post(
            "/api/projects",
            json={"name": "owner supplied gate", "description": "Plan D C27.", "purpose_scope": RESEARCH_SCOPE},
        ).json()
        cls.project_id = project["project_id"]
        cls.client.post(f"/api/projects/{cls.project_id}/open")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)
        shutil.rmtree(cls.owner_root, ignore_errors=True)

    def _wait(self, job_id: str, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        body: dict = {}
        while time.monotonic() < deadline:
            body = self.client.get(
                f"/api/projects/{self.project_id}/jobs/{job_id}"
            ).json()
            if body["state"] in {"COMPLETED", "CANCELLED", "FAILED"} and body["ended_at"]:
                return body
            time.sleep(0.5)
        raise AssertionError(f"job {job_id} did not finish: {body}")

    def _wait_comparison(
        self, project_id: str, comparison_id: str, timeout: float = 180.0
    ) -> dict:
        deadline = time.monotonic() + timeout
        body: dict = {}
        while time.monotonic() < deadline:
            response = self.client.get(
                f"/api/projects/{project_id}/owner-pair-comparisons/{comparison_id}"
            )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            if body["state"] in {"COMPLETED", "FAILED", "INTERRUPTED"}:
                return body
            time.sleep(0.25)
        raise AssertionError(
            f"direct comparison {comparison_id} did not finish: {body}"
        )

    def _upload_real_photo(self, project_id: str, path: Path) -> dict:
        response = self.client.post(
            "/api/quality/assess-upload",
            params={
                "project_id": project_id,
                "purpose_scope": RESEARCH_SCOPE,
                "filename": path.name,
            },
            content=path.read_bytes(),
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _abel_pair(self, project_id: str) -> tuple[dict, dict]:
        existing = self.uploaded_pairs.get(project_id)
        if existing is not None:
            return existing
        left = self._upload_real_photo(project_id, ABEL_LEFT)
        right = self._upload_real_photo(project_id, ABEL_RIGHT)
        self.assertEqual(left["image_sha256"], ABEL_LEFT_SHA256)
        self.assertEqual(right["image_sha256"], ABEL_RIGHT_SHA256)
        self.uploaded_pairs[project_id] = (left, right)
        return left, right

    def _new_project(self, name: str) -> str:
        project = self.client.post(
            "/api/projects",
            json={
                "name": name,
                "description": "Real owner-pair admission boundary gate.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["project_id"]
        opened = self.client.post(f"/api/projects/{project_id}/open")
        self.assertEqual(opened.status_code, 200, opened.text)
        return project_id

    def _database_count(self, statement: str, parameters: tuple[str, ...]) -> int:
        connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
        try:
            return int(connection.execute(statement, parameters).fetchone()[0])
        finally:
            connection.close()

    def _database_row(self, statement: str, parameters: tuple[str, ...]) -> tuple:
        connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
        try:
            row = connection.execute(statement, parameters).fetchone()
            assert row is not None
            return row
        finally:
            connection.close()

    def _recorded_sample_path(self, sample_id: str) -> Path:
        return Path(
            self._database_row(
                "SELECT source_path FROM samples WHERE sample_id = ?",
                (sample_id,),
            )[0]
        )

    def test_direct_pair_admission_records_operator_assertion_without_population_claims(self) -> None:
        left, right = self._abel_pair(self.project_id)

        response = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-admissions",
            json={
                "left_sample_id": left["sample_id"],
                "right_sample_id": right["sample_id"],
                "relationship": "SAME_PERSON",
            },
        )

        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["project_id"], self.project_id)
        self.assertEqual(body["relationship"], "SAME_PERSON")
        self.assertEqual(body["comparison_label"], "MATED")
        self.assertEqual(body["left_sample_id"], left["sample_id"])
        self.assertEqual(body["right_sample_id"], right["sample_id"])
        self.assertFalse(body["population_error_claims_allowed"])
        self.assertFalse(body["outcome_correctness_allowed"])
        self.assertEqual(
            body["relationship_assertion"]["state"],
            "OPERATOR_ASSERTED_UNVERIFIED",
        )
        self.assertTrue(body["relationship_assertion"]["asserted_by"])
        self.assertNotIn("subject_id", json.dumps(body))
        self.assertNotIn("protocol_id", body)

    def test_direct_pair_admission_is_idempotent_and_audited_once(self) -> None:
        left, right = self._abel_pair(self.project_id)
        request = {
            "left_sample_id": left["sample_id"],
            "right_sample_id": right["sample_id"],
            "relationship": "DIFFERENT_PEOPLE",
        }
        first = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-admissions",
            json=request,
        )
        self.assertIn(first.status_code, (200, 201), first.text)
        second = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-admissions",
            json=request,
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json(), first.json())
        self.assertFalse(second.json()["population_error_claims_allowed"])
        self.assertFalse(second.json()["outcome_correctness_allowed"])
        self.assertTrue(second.json()["relationship_assertion"]["asserted_by"])
        audit_count = self._database_count(
            "SELECT COUNT(*) FROM audit_events WHERE subject_id = ?",
            (first.json()["pair_id"],),
        )
        self.assertEqual(audit_count, 1)

    def test_direct_pair_relationship_validation_is_closed_and_byte_distinct(self) -> None:
        left, right = self._abel_pair(self.project_id)
        endpoint = f"/api/projects/{self.project_id}/owner-pair-admissions"
        invalid_requests = (
            {
                "left_sample_id": left["sample_id"],
                "right_sample_id": right["sample_id"],
            },
            {
                "left_sample_id": left["sample_id"],
                "right_sample_id": right["sample_id"],
                "relationship": "UNKNOWN",
            },
            {
                "left_sample_id": left["sample_id"],
                "right_sample_id": right["sample_id"],
                "relationship": "SAME_PERSON",
                "identity": "Abel_Pacheco",
            },
            {
                "left_sample_id": left["sample_id"],
                "right_sample_id": left["sample_id"],
                "relationship": "SAME_PERSON",
            },
        )
        before = self._database_count(
            "SELECT COUNT(*) FROM owner_pair_admissions WHERE project_id = ?",
            (self.project_id,),
        )
        for request in invalid_requests:
            with self.subTest(request=request):
                response = self.client.post(endpoint, json=request)
                self.assertEqual(response.status_code, 422, response.text)
        after = self._database_count(
            "SELECT COUNT(*) FROM owner_pair_admissions WHERE project_id = ?",
            (self.project_id,),
        )
        self.assertEqual(after, before)

    def test_not_recorded_pair_has_no_label_correctness_or_population_claim(self) -> None:
        left, right = self._abel_pair(self.project_id)
        response = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-admissions",
            json={
                "left_sample_id": left["sample_id"],
                "right_sample_id": right["sample_id"],
                "relationship": "NOT_RECORDED",
            },
        )
        self.assertIn(response.status_code, (200, 201), response.text)
        body = response.json()
        self.assertIsNone(body["comparison_label"])
        self.assertFalse(body["population_error_claims_allowed"])
        self.assertFalse(body["outcome_correctness_allowed"])
        self.assertEqual(body["relationship_assertion"]["relationship"], "NOT_RECORDED")
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM protocols WHERE protocol_id = ?",
                (body["pair_id"],),
            ),
            0,
        )
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM protocol_pairs WHERE pair_id = ?",
                (body["pair_id"],),
            ),
            0,
        )

    def test_unverified_relationships_never_authorize_correctness_and_only_record_agreement(self) -> None:
        same_left, same_right = self._abel_pair(self.project_id)
        different_left = self._upload_real_photo(self.project_id, ABDEL_MADI)
        different_right = self._upload_real_photo(self.project_id, DEAN_BARKER)
        self.assertEqual(different_left["image_sha256"], ABDEL_MADI_SHA256)
        self.assertEqual(different_right["image_sha256"], DEAN_BARKER_SHA256)
        relationships = (
            ("SAME_PERSON", same_left, same_right),
            ("DIFFERENT_PEOPLE", different_left, different_right),
            ("NOT_RECORDED", same_left, same_right),
        )
        admitted_pairs: list[tuple[str, dict]] = []
        for relationship, left, right in relationships:
            with self.subTest(relationship=relationship):
                admitted = self.client.post(
                    f"/api/projects/{self.project_id}/owner-pair-admissions",
                    json={
                        "left_sample_id": left["sample_id"],
                        "right_sample_id": right["sample_id"],
                        "relationship": relationship,
                    },
                )
                self.assertIn(admitted.status_code, (200, 201), admitted.text)
                pair = admitted.json()
                self.assertEqual(
                    pair["relationship_assertion"]["state"],
                    "OPERATOR_ASSERTED_UNVERIFIED",
                )
                self.assertFalse(pair["population_error_claims_allowed"])
                self.assertFalse(pair["outcome_correctness_allowed"])
                admitted_pairs.append((relationship, pair))

        pulled = self.client.post(
            "/api/matcher-assets/adaface-ir101-local-candidate/pull-sources"
        )
        self.assertEqual(pulled.status_code, 200, pulled.text)
        for relationship, pair in admitted_pairs:
            with self.subTest(relationship=relationship):
                started = self.client.post(
                    f"/api/projects/{self.project_id}/owner-pair-comparisons",
                    json={
                        "pair_id": pair["pair_id"],
                        "matcher_id": "adaface-ir101-local-candidate",
                        "preprocessing_id": "SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1",
                        "threshold": {
                            "contract_id": "matcher_threshold",
                            "kind": "FIXED_VALUE",
                            "value": 0.8,
                        },
                    },
                )
                self.assertIn(started.status_code, (200, 202), started.text)
                status = self._wait_comparison(
                    self.project_id, started.json()["comparison_id"]
                )
                self.assertEqual(status["state"], "COMPLETED", status)
                response = self.client.get(status["links"]["evidence"])
                self.assertEqual(response.status_code, 200, response.text)
                evidence = response.json()
                self.assertEqual(
                    evidence["pair"]["relationship_assertion"]["state"],
                    "OPERATOR_ASSERTED_UNVERIFIED",
                )
                self.assertFalse(
                    evidence["claim_boundary"]["population_error_claims_allowed"]
                )
                self.assertFalse(
                    evidence["claim_boundary"]["outcome_correctness_allowed"]
                )
                self.assertFalse(evidence["claim_boundary"]["ground_truth_authority"])
                self.assertEqual(
                    set(evidence["claim_boundary"]["prohibited"]),
                    {"FMR", "FNMR", "EER", "ROC", "DET", "OPERATING_POINT"},
                )
                agreement = evidence["result"]["agreement_with_operator_assertion"]
                if relationship == "NOT_RECORDED":
                    self.assertIsNone(agreement)
                else:
                    self.assertIn(agreement, (True, False))

    def test_direct_pair_comparison_accepts_an_admitted_real_pair(self) -> None:
        left, right = self._abel_pair(self.project_id)
        admitted = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-admissions",
            json={
                "left_sample_id": left["sample_id"],
                "right_sample_id": right["sample_id"],
                "relationship": "NOT_RECORDED",
            },
        )
        self.assertIn(admitted.status_code, (200, 201), admitted.text)
        pulled = self.client.post(
            "/api/matcher-assets/adaface-ir101-local-candidate/pull-sources"
        )
        self.assertEqual(pulled.status_code, 200, pulled.text)

        response = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-comparisons",
            json={
                "pair_id": admitted.json()["pair_id"],
                "matcher_id": "adaface-ir101-local-candidate",
                "preprocessing_id": "SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1",
                "threshold": {
                    "contract_id": "matcher_threshold",
                    "kind": "FIXED_VALUE",
                    "value": 0.8,
                },
            },
        )

        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["state"], "QUEUED")
        self.assertFalse(response.json().get("population_error_claims_allowed", False))
        status = self._wait_comparison(
            self.project_id, response.json()["comparison_id"]
        )
        self.assertEqual(status["state"], "COMPLETED", status)
        evidence_response = self.client.get(status["links"]["evidence"])
        self.assertEqual(evidence_response.status_code, 200, evidence_response.text)
        evidence = evidence_response.json()
        self.assertEqual(evidence["pair"]["relationship"], "NOT_RECORDED")
        self.assertIsNone(evidence["pair"]["comparison_label"])
        self.assertIsNotNone(evidence["result"]["score"])
        self.assertIn(evidence["result"]["decision"], {"MATCH", "NO_MATCH"})
        self.assertIsNone(evidence["result"]["agreement_with_operator_assertion"])
        self.assertFalse(evidence["claim_boundary"]["population_error_claims_allowed"])
        self.assertFalse(evidence["claim_boundary"]["outcome_correctness_allowed"])
        self.assertFalse(evidence["claim_boundary"]["ground_truth_authority"])
        self.assertEqual(
            set(evidence["claim_boundary"]["prohibited"]),
            {"FMR", "FNMR", "EER", "ROC", "DET", "OPERATING_POINT"},
        )
        self.assertEqual(evidence["sides"]["left"]["sha256"], ABEL_LEFT_SHA256)
        self.assertEqual(evidence["sides"]["right"]["sha256"], ABEL_RIGHT_SHA256)
        self.assertNotEqual(
            evidence["sides"]["left"]["dataset_id"],
            evidence["sides"]["right"]["dataset_id"],
        )
        self.assertEqual(evidence["sides"]["left"]["embedding"]["shape"], [512])
        manifest = self.client.get(status["links"]["raw_manifest"])
        self.assertEqual(manifest.status_code, 200, manifest.text)
        for artifact in manifest.json()["artifacts"]:
            raw = self.client.get(artifact["link"])
            self.assertEqual(raw.status_code, 200, raw.text)
            self.assertEqual(hashlib.sha256(raw.content).hexdigest(), artifact["sha256"])
            if artifact["artifact"] == "execution-record":
                self.assertNotIn(b"/mnt/", raw.content)

    def test_direct_pair_reads_are_project_scoped_and_legacy_routes_are_retired(self) -> None:
        left, right = self._abel_pair(self.project_id)
        admitted = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-admissions",
            json={
                "left_sample_id": left["sample_id"],
                "right_sample_id": right["sample_id"],
                "relationship": "NOT_RECORDED",
            },
        )
        self.assertIn(admitted.status_code, (200, 201), admitted.text)
        pulled = self.client.post(
            "/api/matcher-assets/adaface-ir101-local-candidate/pull-sources"
        )
        self.assertEqual(pulled.status_code, 200, pulled.text)
        started = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-comparisons",
            json={
                "pair_id": admitted.json()["pair_id"],
                "matcher_id": "adaface-ir101-local-candidate",
                "preprocessing_id": "SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1",
                "threshold": {
                    "contract_id": "matcher_threshold",
                    "kind": "FIXED_VALUE",
                    "value": 0.8,
                },
            },
        )
        self.assertIn(started.status_code, (200, 202), started.text)
        comparison_id = started.json()["comparison_id"]
        run_id = started.json()["run_id"]
        project_prefix = f"/api/projects/{self.project_id}"
        status_path = f"{project_prefix}/owner-pair-comparisons/{comparison_id}"

        deadline = time.monotonic() + 180
        status_body: dict = {}
        while time.monotonic() < deadline:
            status = self.client.get(status_path)
            self.assertEqual(status.status_code, 200, status.text)
            status_body = status.json()
            if status_body["state"] in {"COMPLETED", "FAILED", "INTERRUPTED"}:
                break
            time.sleep(0.25)
        self.assertEqual(status_body["state"], "COMPLETED", status_body)

        evidence_path = f"{status_path}/evidence"
        run_path = f"{project_prefix}/direct-pair-runs/{run_id}"
        raw_path = f"{status_path}/raw"
        self.assertEqual(
            status_body["links"],
            {
                "self": status_path,
                "evidence": evidence_path,
                "run": run_path,
                "raw_manifest": raw_path,
            },
        )
        evidence = self.client.get(evidence_path)
        self.assertEqual(evidence.status_code, 200, evidence.text)
        run = self.client.get(run_path)
        self.assertEqual(run.status_code, 200, run.text)
        raw_manifest = self.client.get(raw_path)
        self.assertEqual(raw_manifest.status_code, 200, raw_manifest.text)
        artifacts = raw_manifest.json()["artifacts"]
        self.assertTrue(artifacts)
        raw_artifact_prefix = f"{raw_path}/"
        self.assertTrue(
            all(item["link"].startswith(raw_artifact_prefix) for item in artifacts)
        )
        for artifact in artifacts:
            raw = self.client.get(artifact["link"])
            self.assertEqual(raw.status_code, 200, raw.text)
            self.assertEqual(hashlib.sha256(raw.content).hexdigest(), artifact["sha256"])

        foreign_project_id = self._new_project("foreign direct comparison read gate")
        foreign_prefix = f"/api/projects/{foreign_project_id}"
        foreign_routes = (
            f"{foreign_prefix}/owner-pair-comparisons/{comparison_id}",
            f"{foreign_prefix}/owner-pair-comparisons/{comparison_id}/evidence",
            f"{foreign_prefix}/direct-pair-runs/{run_id}",
            f"{foreign_prefix}/owner-pair-comparisons/{comparison_id}/raw",
            f"{foreign_prefix}/owner-pair-comparisons/{comparison_id}/raw/{artifacts[0]['artifact']}",
        )
        for route in foreign_routes:
            with self.subTest(boundary="foreign", route=route):
                refused = self.client.get(route)
                self.assertEqual(refused.status_code, 404, refused.text)

        legacy_routes = (
            f"/api/owner-pair-comparisons/{comparison_id}",
            f"/api/owner-pair-comparisons/{comparison_id}/evidence",
            f"/api/direct-pair-runs/{run_id}",
            f"/api/owner-pair-comparisons/{comparison_id}/raw",
            f"/api/owner-pair-comparisons/{comparison_id}/raw/{artifacts[0]['artifact']}",
        )
        for route in legacy_routes:
            with self.subTest(boundary="legacy", route=route):
                retired = self.client.get(route)
                self.assertEqual(retired.status_code, 404, retired.text)

    def test_direct_pair_comparison_is_concurrently_idempotent_and_audited_once(self) -> None:
        left, right = self._abel_pair(self.project_id)
        admitted = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-admissions",
            json={
                "left_sample_id": left["sample_id"],
                "right_sample_id": right["sample_id"],
                "relationship": "SAME_PERSON",
            },
        )
        self.assertIn(admitted.status_code, (200, 201), admitted.text)
        pulled = self.client.post(
            "/api/matcher-assets/adaface-ir101-local-candidate/pull-sources"
        )
        self.assertEqual(pulled.status_code, 200, pulled.text)
        endpoint = f"/api/projects/{self.project_id}/owner-pair-comparisons"
        request = {
            "pair_id": admitted.json()["pair_id"],
            "matcher_id": "adaface-ir101-local-candidate",
            "preprocessing_id": "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1",
            "threshold": {
                "contract_id": "matcher_threshold",
                "kind": "FIXED_VALUE",
                "value": 0.75,
            },
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.client.post(endpoint, json=request), range(2)))
        for response in responses:
            self.assertIn(response.status_code, (200, 202), response.text)
        comparison_ids = {response.json()["comparison_id"] for response in responses}
        run_ids = {response.json()["run_id"] for response in responses}
        self.assertEqual(len(comparison_ids), 1)
        self.assertEqual(len(run_ids), 1)
        comparison_id = comparison_ids.pop()
        status = self._wait_comparison(self.project_id, comparison_id)
        self.assertEqual(status["state"], "COMPLETED", status)
        retry = self.client.post(endpoint, json=request)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["comparison_id"], comparison_id)
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM direct_pair_comparisons WHERE comparison_id = ?",
                (comparison_id,),
            ),
            1,
        )
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM audit_events WHERE subject_id = ?",
                (status["run_id"],),
            ),
            1,
        )

    def test_direct_pair_comparison_validates_project_addons_profiles_and_threshold(self) -> None:
        left, right = self._abel_pair(self.project_id)
        admitted = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-admissions",
            json={
                "left_sample_id": left["sample_id"],
                "right_sample_id": right["sample_id"],
                "relationship": "DIFFERENT_PEOPLE",
            },
        )
        self.assertIn(admitted.status_code, (200, 201), admitted.text)
        pair_id = admitted.json()["pair_id"]
        other_project = self._new_project("direct comparison authorization boundary")
        base = {
            "pair_id": pair_id,
            "matcher_id": "adaface-ir101-local-candidate",
            "preprocessing_id": "SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1",
            "threshold": {
                "contract_id": "matcher_threshold",
                "kind": "FIXED_VALUE",
                "value": 0.8,
            },
        }
        foreign = self.client.post(
            f"/api/projects/{other_project}/owner-pair-comparisons", json=base
        )
        self.assertEqual(foreign.status_code, 404, foreign.text)
        self.assertNotIn(pair_id, foreign.text)
        self.assertEqual(
            self.client.get(
                f"/api/projects/{other_project}/owner-pair-comparisons"
            ).json(),
            [],
        )
        unknown = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-comparisons",
            json={**base, "matcher_id": "not-a-registered-matcher"},
        )
        self.assertEqual(unknown.status_code, 422, unknown.text)
        incompatible = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-comparisons",
            json={
                **base,
                "preprocessing_id": "AUTHOR_PREALIGNED_ARCFACE112_INMEMORY_V1",
            },
        )
        self.assertEqual(incompatible.status_code, 409, incompatible.text)
        unavailable = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-comparisons",
            json={
                **base,
                "matcher_id": "magface-iresnet100-ms1mv2-local-candidate",
            },
        )
        self.assertEqual(unavailable.status_code, 409, unavailable.text)
        self.assertNotIn("/mnt/", unavailable.text)
        boolean_threshold = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-comparisons",
            json={
                **base,
                "threshold": {
                    "contract_id": "matcher_threshold",
                    "kind": "FIXED_VALUE",
                    "value": True,
                },
            },
        )
        self.assertEqual(boolean_threshold.status_code, 422, boolean_threshold.text)
        extra_field = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-comparisons",
            json={**base, "population_error_claims_allowed": True},
        )
        self.assertEqual(extra_field.status_code, 422, extra_field.text)

    def test_direct_pair_comparison_records_a_real_processing_failure_without_claims(self) -> None:
        fixture_root = Path(tempfile.mkdtemp(prefix="qualsight-faceocc-real-fixture-"))
        self.addCleanup(shutil.rmtree, fixture_root, True)
        subject_root = fixture_root / "faceocc-texture"
        subject_root.mkdir(parents=True, exist_ok=True)
        copied = subject_root / FACEOCC_TEXTURE.name
        shutil.copyfile(FACEOCC_TEXTURE, copied)
        self.assertEqual(
            hashlib.sha256(copied.read_bytes()).hexdigest(), FACEOCC_TEXTURE_SHA256
        )
        imported = self.client.post(
            f"/api/projects/{self.project_id}/datasets/import-owner-directory",
            json={
                "root": str(fixture_root),
                "name": "FaceOcc real texture detector-failure fixture",
            },
        )
        self.assertEqual(imported.status_code, 201, imported.text)
        samples = self.client.get(
            f"/api/projects/{self.project_id}/datasets/"
            f"{imported.json()['dataset_id']}/samples"
        )
        self.assertEqual(samples.status_code, 200, samples.text)
        self.assertEqual(len(samples.json()), 1)
        failure_sample = samples.json()[0]
        self.assertEqual(failure_sample["sha256"], FACEOCC_TEXTURE_SHA256)
        _, right = self._abel_pair(self.project_id)
        admitted = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-admissions",
            json={
                "left_sample_id": failure_sample["sample_id"],
                "right_sample_id": right["sample_id"],
                "relationship": "NOT_RECORDED",
            },
        )
        self.assertEqual(admitted.status_code, 201, admitted.text)
        pulled = self.client.post(
            "/api/matcher-assets/adaface-ir101-local-candidate/pull-sources"
        )
        self.assertEqual(pulled.status_code, 200, pulled.text)
        started = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-comparisons",
            json={
                "pair_id": admitted.json()["pair_id"],
                "matcher_id": "adaface-ir101-local-candidate",
                "preprocessing_id": "SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1",
                "threshold": {
                    "contract_id": "matcher_threshold",
                    "kind": "FIXED_VALUE",
                    "value": 0.71,
                },
            },
        )
        self.assertEqual(started.status_code, 202, started.text)
        status = self._wait_comparison(
            self.project_id, started.json()["comparison_id"]
        )
        self.assertEqual(status["state"], "COMPLETED", status)
        evidence = self.client.get(status["links"]["evidence"])
        self.assertEqual(evidence.status_code, 200, evidence.text)
        body = evidence.json()
        self.assertIsNone(body["result"]["score"])
        self.assertIsNone(body["result"]["decision"])
        self.assertEqual(
            body["result"]["decision_reason"], "NO_SCORE_PROCESSING_FAILURE"
        )
        self.assertEqual(body["result"]["failure"], "FAILURE_TO_DETECT")
        self.assertEqual(body["result"]["failure_side"], "left")
        self.assertEqual(
            body["sides"]["left"]["failure"]["kind"], "FAILURE_TO_DETECT"
        )
        self.assertIsNone(body["sides"]["left"]["crop"])
        self.assertIsNone(body["sides"]["left"]["embedding"])
        self.assertIsNotNone(body["sides"]["right"]["embedding"])
        self.assertFalse(body["claim_boundary"]["population_error_claims_allowed"])
        missing_crop = self.client.get(
            f"/api/projects/{self.project_id}/owner-pair-comparisons/"
            f"{body['comparison_id']}/raw/left-crop"
        )
        self.assertEqual(missing_crop.status_code, 409, missing_crop.text)

    def test_direct_pair_comparison_refuses_recorded_artifact_and_source_hash_drift(self) -> None:
        left, right = self._abel_pair(self.project_id)
        admitted = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-admissions",
            json={
                "left_sample_id": left["sample_id"],
                "right_sample_id": right["sample_id"],
                "relationship": "SAME_PERSON",
            },
        )
        self.assertIn(admitted.status_code, (200, 201), admitted.text)
        pulled = self.client.post(
            "/api/matcher-assets/adaface-ir101-local-candidate/pull-sources"
        )
        self.assertEqual(pulled.status_code, 200, pulled.text)
        started = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-comparisons",
            json={
                "pair_id": admitted.json()["pair_id"],
                "matcher_id": "adaface-ir101-local-candidate",
                "preprocessing_id": "SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1",
                "threshold": {
                    "contract_id": "matcher_threshold",
                    "kind": "FIXED_VALUE",
                    "value": 0.76,
                },
            },
        )
        self.assertEqual(started.status_code, 202, started.text)
        comparison_id = started.json()["comparison_id"]
        status = self._wait_comparison(self.project_id, comparison_id)
        self.assertEqual(status["state"], "COMPLETED", status)
        record_json, evidence_sha256 = self._database_row(
            "SELECT record_json, evidence_sha256 FROM direct_pair_comparisons WHERE comparison_id = ?",
            (comparison_id,),
        )
        record = json.loads(record_json)
        crop_path = Path(
            next(
                item["path"]
                for item in record["left"]["artifacts"]
                if item["artifact"] == "left-crop"
            )
        )
        crop_bytes = crop_path.read_bytes()
        source_path = Path(
            next(
                item["path"]
                for item in record["left"]["artifacts"]
                if item["artifact"] == "left-source"
            )
        )
        source_bytes = source_path.read_bytes()
        try:
            crop_path.write_bytes(crop_bytes + b"recorded-artifact-drift")
            refused = self.client.get(status["links"]["evidence"])
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertNotIn(str(crop_path), refused.text)
            crop_path.write_bytes(crop_bytes)
            self.assertEqual(
                self.client.get(status["links"]["evidence"]).status_code, 200
            )
            source_path.write_bytes(source_bytes + b"recorded-source-drift")
            refused_source = self.client.get(status["links"]["evidence"])
            self.assertEqual(refused_source.status_code, 409, refused_source.text)
            self.assertNotIn(str(source_path), refused_source.text)
        finally:
            crop_path.write_bytes(crop_bytes)
            source_path.write_bytes(source_bytes)
        self.assertEqual(
            hashlib.sha256(source_path.read_bytes()).hexdigest(), ABEL_LEFT_SHA256
        )
        race_root = Path(tempfile.mkdtemp(prefix="qualsight-raw-artifact-race-"))
        original_link = race_root / "recorded-crop.png"
        alternate_file = race_root / DEAN_BARKER.name
        os.link(crop_path, original_link)
        shutil.copyfile(DEAN_BARKER, alternate_file)
        self.assertEqual(
            hashlib.sha256(alternate_file.read_bytes()).hexdigest(),
            DEAN_BARKER_SHA256,
        )
        stop_swapping = threading.Event()

        def swap_recorded_path() -> None:
            staging = race_root / "next-inode"
            candidate = alternate_file
            while not stop_swapping.is_set():
                staging.unlink(missing_ok=True)
                os.link(candidate, staging)
                os.replace(staging, crop_path)
                candidate = original_link if candidate == alternate_file else alternate_file

        swapper = threading.Thread(target=swap_recorded_path, daemon=True)
        swapper.start()
        mismatched: list[tuple[str, str]] = []
        successful_reads = 0
        raw_crop = (
            f"/api/projects/{self.project_id}/owner-pair-comparisons/"
            f"{comparison_id}/raw/left-crop"
        )
        try:
            for _ in range(200):
                response = self.client.get(raw_crop)
                if response.status_code != 200:
                    self.assertEqual(response.status_code, 409, response.text)
                    continue
                successful_reads += 1
                observed = hashlib.sha256(response.content).hexdigest()
                advertised = response.headers["X-QualSight-SHA256"]
                if observed != advertised:
                    mismatched.append((observed, advertised))
                    break
        finally:
            stop_swapping.set()
            swapper.join(timeout=5)
            staging = race_root / "restore-inode"
            staging.unlink(missing_ok=True)
            os.link(original_link, staging)
            os.replace(staging, crop_path)
            shutil.rmtree(race_root, ignore_errors=True)
        self.assertGreater(successful_reads, 0)
        self.assertEqual(mismatched, [])
        connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
        try:
            connection.execute(
                "UPDATE direct_pair_comparisons SET record_json = ? WHERE comparison_id = ?",
                (record_json.replace('"matcher_device":', '"matcher_device_tampered":', 1), comparison_id),
            )
            connection.commit()
            refused_record = self.client.get(status["links"]["evidence"])
            self.assertEqual(refused_record.status_code, 409, refused_record.text)
            self.assertNotIn("matcher_device", refused_record.text)
        finally:
            connection.execute(
                "UPDATE direct_pair_comparisons SET record_json = ?, evidence_sha256 = ? WHERE comparison_id = ?",
                (record_json, evidence_sha256, comparison_id),
            )
            connection.commit()
        tampered = json.loads(record_json)
        tampered["run_id"] = tampered["run_id"] + "-substituted"
        tampered_json = json.dumps(tampered, separators=(",", ":"))
        tampered_digest = hashlib.sha256(tampered_json.encode("utf-8")).hexdigest()
        try:
            connection.execute(
                "UPDATE direct_pair_comparisons SET record_json = ?, evidence_sha256 = ? WHERE comparison_id = ?",
                (tampered_json, tampered_digest, comparison_id),
            )
            connection.commit()
            refused_substitution = self.client.get(status["links"]["evidence"])
            self.assertEqual(
                refused_substitution.status_code, 409, refused_substitution.text
            )
            self.assertNotIn("substituted", refused_substitution.text)
        finally:
            connection.execute(
                "UPDATE direct_pair_comparisons SET record_json = ?, evidence_sha256 = ? WHERE comparison_id = ?",
                (record_json, evidence_sha256, comparison_id),
            )
            connection.commit()
            connection.close()
        self.assertEqual(
            self.client.get(status["links"]["evidence"]).status_code, 200
        )

    def test_direct_pair_comparison_recovers_prepared_output_after_database_failure(self) -> None:
        left, right = self._abel_pair(self.project_id)
        admitted = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-admissions",
            json={
                "left_sample_id": left["sample_id"],
                "right_sample_id": right["sample_id"],
                "relationship": "DIFFERENT_PEOPLE",
            },
        )
        self.assertIn(admitted.status_code, (200, 201), admitted.text)
        pulled = self.client.post(
            "/api/matcher-assets/adaface-ir101-local-candidate/pull-sources"
        )
        self.assertEqual(pulled.status_code, 200, pulled.text)
        request = {
            "pair_id": admitted.json()["pair_id"],
            "matcher_id": "adaface-ir101-local-candidate",
            "preprocessing_id": "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1",
            "threshold": {
                "contract_id": "matcher_threshold",
                "kind": "FIXED_VALUE",
                "value": 0.77,
            },
        }
        connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
        connection.execute(
            "CREATE TRIGGER refuse_direct_comparison_audit BEFORE INSERT ON audit_events "
            "WHEN NEW.subject_type = 'DirectPairComparisonRun' "
            "BEGIN SELECT RAISE(ABORT, 'real rollback gate'); END"
        )
        connection.commit()
        try:
            with self.assertLogs("qualsight.operations", level="ERROR") as diagnostics:
                started = self.client.post(
                    f"/api/projects/{self.project_id}/owner-pair-comparisons",
                    json=request,
                )
                self.assertEqual(started.status_code, 202, started.text)
                comparison_id = started.json()["comparison_id"]
                run_id = started.json()["run_id"]
                status = self._wait_comparison(self.project_id, comparison_id)
                self.assertEqual(status["state"], "FAILED", status)
                self.assertEqual(
                    self._database_count(
                        "SELECT COUNT(*) FROM direct_pair_comparisons WHERE comparison_id = ?",
                        (comparison_id,),
                    ),
                    0,
                )
                self.assertEqual(
                    self._database_count(
                        "SELECT COUNT(*) FROM audit_events WHERE subject_id = ?",
                        (run_id,),
                    ),
                    0,
                )
                output = (
                    self.data_dir
                    / "direct-pair-comparisons"
                    / comparison_id.replace(":", "_")
                )
                receipt = output / ".qualsight-direct-pair.prepared.json"
                self.assertTrue(receipt.is_file())
                prepared_hashes = {
                    str(path.relative_to(output)): hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
                    for path in sorted(output.rglob("*"))
                    if path.is_file()
                }
                self.assertIn(receipt.name, prepared_hashes)
                retry = self.client.post(
                    f"/api/projects/{self.project_id}/owner-pair-comparisons",
                    json=request,
                )
                self.assertEqual(retry.status_code, 200, retry.text)
                self.assertEqual(retry.json()["state"], "FAILED")
            private_diagnostics = "\n".join(diagnostics.output)
            self.assertIn("direct pair comparison execution failed", private_diagnostics)
            self.assertIn("real rollback gate", private_diagnostics)
        finally:
            connection.execute("DROP TRIGGER IF EXISTS refuse_direct_comparison_audit")
            connection.commit()
            connection.close()

        type(self).client.__exit__(None, None, None)
        restarted = TestClient(
            create_app(self.settings), base_url="http://127.0.0.1"
        )
        restarted.__enter__()
        type(self).client = restarted
        retry = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-comparisons",
            json=request,
        )
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["state"], "COMPLETED")
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM direct_pair_comparisons "
                "WHERE comparison_id = ?",
                (comparison_id,),
            ),
            1,
        )
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM audit_events WHERE subject_id = ?",
                (run_id,),
            ),
            1,
        )
        recovered_hashes = {
            str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(output.rglob("*"))
            if path.is_file()
        }
        self.assertEqual(recovered_hashes, prepared_hashes)
        repeated = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-comparisons",
            json=request,
        )
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["state"], "COMPLETED")
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM direct_pair_comparisons "
                "WHERE comparison_id = ?",
                (comparison_id,),
            ),
            1,
        )
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM audit_events WHERE subject_id = ?",
                (run_id,),
            ),
            1,
        )

    def test_direct_pair_comparison_blocks_colliding_purge_then_purges_all_evidence(self) -> None:
        project_id = self._new_project("direct comparison purge boundary")
        left, right = self._abel_pair(project_id)
        admitted = self.client.post(
            f"/api/projects/{project_id}/owner-pair-admissions",
            json={
                "left_sample_id": left["sample_id"],
                "right_sample_id": right["sample_id"],
                "relationship": "SAME_PERSON",
            },
        )
        self.assertEqual(admitted.status_code, 201, admitted.text)
        pulled = self.client.post(
            "/api/matcher-assets/adaface-ir101-local-candidate/pull-sources"
        )
        self.assertEqual(pulled.status_code, 200, pulled.text)
        started = self.client.post(
            f"/api/projects/{project_id}/owner-pair-comparisons",
            json={
                "pair_id": admitted.json()["pair_id"],
                "matcher_id": "adaface-ir101-local-candidate",
                "preprocessing_id": "SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1",
                "threshold": {
                    "contract_id": "matcher_threshold",
                    "kind": "FIXED_VALUE",
                    "value": 0.78,
                },
            },
        )
        self.assertEqual(started.status_code, 202, started.text)
        comparison_id = started.json()["comparison_id"]
        collision = self.client.post(
            f"/api/projects/{project_id}/purge", json={"confirm": project_id}
        )
        self.assertEqual(collision.status_code, 409, collision.text)
        self.assertIn("direct pair comparison", collision.text)
        status = self._wait_comparison(project_id, comparison_id)
        self.assertEqual(status["state"], "COMPLETED", status)
        run_directory = (
            self.data_dir
            / "direct-pair-comparisons"
            / comparison_id.replace(":", "_")
        )
        self.assertTrue(run_directory.is_dir())
        scope = self.client.get(f"/api/projects/{project_id}/purge-scope")
        self.assertEqual(scope.status_code, 200, scope.text)
        self.assertGreater(scope.json()["target_counts"]["directories"], 0)
        self.assertNotIn("targets", scope.json())
        purged = self.client.post(
            f"/api/projects/{project_id}/purge", json={"confirm": project_id}
        )
        self.assertEqual(purged.status_code, 201, purged.text)
        self.assertTrue(purged.json()["verified"])
        self.assertFalse(run_directory.exists())
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM direct_pair_comparisons WHERE project_id = ?",
                (project_id,),
            ),
            0,
        )
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM owner_pair_admissions WHERE project_id = ?",
                (project_id,),
            ),
            0,
        )
        self.assertEqual(
            self.client.get(
                f"/api/projects/{project_id}/owner-pair-comparisons/{comparison_id}"
            ).status_code,
            404,
        )

    def test_direct_pair_stale_admission_cannot_resurrect_operation_or_output_after_purge(self) -> None:
        project_id = self._new_project("direct comparison stale-admission purge gate")
        left, right = self._abel_pair(project_id)
        admitted = self.client.post(
            f"/api/projects/{project_id}/owner-pair-admissions",
            json={
                "left_sample_id": left["sample_id"],
                "right_sample_id": right["sample_id"],
                "relationship": "NOT_RECORDED",
            },
        )
        self.assertEqual(admitted.status_code, 201, admitted.text)
        pulled = self.client.post(
            "/api/matcher-assets/adaface-ir101-local-candidate/pull-sources"
        )
        self.assertEqual(pulled.status_code, 200, pulled.text)
        endpoint = f"/api/projects/{project_id}/owner-pair-comparisons"
        request = {
            "pair_id": admitted.json()["pair_id"],
            "matcher_id": "adaface-ir101-local-candidate",
            "preprocessing_id": "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1",
            "threshold": {
                "contract_id": "matcher_threshold",
                "kind": "FIXED_VALUE",
                "value": 0.713,
            },
        }
        route = next(
            route
            for route in self.client.app.routes
            if getattr(route, "path", None)
            == "/api/projects/{project_id}/owner-pair-comparisons"
        )
        nonlocals = inspect.getclosurevars(route.endpoint).nonlocals
        manager = nonlocals["matcher_manager"]()
        app_store = nonlocals["store"]()
        output_root = self.data_dir / "direct-pair-comparisons"
        before_outputs = set(output_root.glob("*")) if output_root.exists() else set()

        pool = ThreadPoolExecutor(max_workers=2)
        try:
            with manager.exclusive_control():
                comparison_future = pool.submit(
                    self.client.post, endpoint, json=request
                )
                deadline = time.monotonic() + 30
                reached_post_pair_readiness = False
                while time.monotonic() < deadline:
                    for frame in sys._current_frames().values():
                        current = frame
                        while current is not None:
                            if (
                                current.f_code.co_name == "status"
                                and current.f_locals.get("self") is manager
                            ):
                                reached_post_pair_readiness = True
                                break
                            current = current.f_back
                        if reached_post_pair_readiness:
                            break
                    if reached_post_pair_readiness:
                        break
                    if comparison_future.done():
                        self.fail(
                            f"comparison request terminated before the held readiness phase: {comparison_future.result().text}"
                        )
                    time.sleep(0.01)
                self.assertTrue(
                    reached_post_pair_readiness,
                    "comparison request did not reach the held post-pair readiness phase",
                )
                purge_record = pool.submit(
                    purge_project,
                    app_store,
                    data_dir=self.data_dir,
                    project_id=project_id,
                    actor="local-user:test",
                    confirm=project_id,
                ).result(timeout=60)
                self.assertTrue(purge_record["verified"], purge_record)
            refused = comparison_future.result(timeout=60)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(
            refused.json()["detail"],
            "owner-supplied pair was removed before comparison admission",
        )
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM operations WHERE project_id = ?",
                (project_id,),
            ),
            0,
        )
        after_outputs = set(output_root.glob("*")) if output_root.exists() else set()
        self.assertEqual(after_outputs, before_outputs)

    def test_one_admitted_pair_executes_multiple_real_matcher_preprocessing_runs(self) -> None:
        left, right = self._abel_pair(self.project_id)
        admitted = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-admissions",
            json={
                "left_sample_id": left["sample_id"],
                "right_sample_id": right["sample_id"],
                "relationship": "NOT_RECORDED",
            },
        )
        self.assertIn(admitted.status_code, (200, 201), admitted.text)
        for matcher_id in (
            "adaface-ir101-local-candidate",
            "magface-iresnet100-ms1mv2-local-candidate",
        ):
            pulled = self.client.post(
                f"/api/matcher-assets/{matcher_id}/pull-sources"
            )
            self.assertEqual(pulled.status_code, 200, pulled.text)
        configurations = (
            (
                "adaface-ir101-local-candidate",
                "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1",
            ),
            (
                "magface-iresnet100-ms1mv2-local-candidate",
                "SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1",
            ),
        )
        completed: list[dict] = []
        for matcher_id, preprocessing_id in configurations:
            started = self.client.post(
                f"/api/projects/{self.project_id}/owner-pair-comparisons",
                json={
                    "pair_id": admitted.json()["pair_id"],
                    "matcher_id": matcher_id,
                    "preprocessing_id": preprocessing_id,
                    "threshold": {
                        "contract_id": "matcher_threshold",
                        "kind": "FIXED_VALUE",
                        "value": 0.8,
                    },
                },
            )
            self.assertEqual(started.status_code, 202, started.text)
            status = self._wait_comparison(
                self.project_id, started.json()["comparison_id"]
            )
            self.assertEqual(status["state"], "COMPLETED", status)
            evidence = self.client.get(status["links"]["evidence"])
            self.assertEqual(evidence.status_code, 200, evidence.text)
            completed.append(evidence.json())
        self.assertEqual(len({item["run_id"] for item in completed}), 2)
        self.assertEqual(len({item["comparison_id"] for item in completed}), 2)
        self.assertEqual(
            {item["execution"]["matcher_id"] for item in completed},
            {item[0] for item in configurations},
        )
        for item in completed:
            self.assertEqual(item["pair"]["pair_id"], admitted.json()["pair_id"])
            self.assertEqual(item["sides"]["left"]["sha256"], ABEL_LEFT_SHA256)
            self.assertEqual(item["sides"]["right"]["sha256"], ABEL_RIGHT_SHA256)
            self.assertFalse(item["claim_boundary"]["population_error_claims_allowed"])
            self.assertIsNone(item["result"]["agreement_with_operator_assertion"])
        inventory = self.client.get(
            f"/api/projects/{self.project_id}/owner-pair-comparisons"
        )
        self.assertEqual(inventory.status_code, 200, inventory.text)
        inventory_ids = {item["comparison_id"] for item in inventory.json()}
        self.assertTrue(
            {item["comparison_id"] for item in completed}.issubset(inventory_ids)
        )

    def test_direct_pair_cross_project_reference_is_not_enumerated_and_rolls_back(self) -> None:
        left, _ = self._abel_pair(self.project_id)
        foreign_project_id = self._new_project("owner pair foreign-reference gate")
        foreign = self._upload_real_photo(
            foreign_project_id,
            self.owner_root / "operator_b" / "photo_03.jpg",
        )
        before_pairs = self._database_count(
            "SELECT COUNT(*) FROM owner_pair_admissions WHERE project_id = ?",
            (self.project_id,),
        )
        before_audits = self._database_count(
            "SELECT COUNT(*) FROM audit_events WHERE project_id = ?",
            (self.project_id,),
        )

        response = self.client.post(
            f"/api/projects/{self.project_id}/owner-pair-admissions",
            json={
                "left_sample_id": left["sample_id"],
                "right_sample_id": foreign["sample_id"],
                "relationship": "DIFFERENT_PEOPLE",
            },
        )

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(
            response.json()["detail"],
            "owner-supplied sample is not recorded for this project",
        )
        self.assertNotIn(foreign["sample_id"], response.text)
        self.assertNotIn(str(self._recorded_sample_path(foreign["sample_id"])), response.text)
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM owner_pair_admissions WHERE project_id = ?",
                (self.project_id,),
            ),
            before_pairs,
        )
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM audit_events WHERE project_id = ?",
                (self.project_id,),
            ),
            before_audits,
        )

    def test_direct_pair_refuses_owner_byte_hash_drift_without_path_leak(self) -> None:
        left, right = self._abel_pair(self.project_id)
        endpoint = f"/api/projects/{self.project_id}/owner-pair-admissions"
        request = {
            "left_sample_id": left["sample_id"],
            "right_sample_id": right["sample_id"],
            "relationship": "SAME_PERSON",
        }
        recorded = self.client.post(endpoint, json=request)
        self.assertIn(recorded.status_code, (200, 201), recorded.text)
        stored_right = self._recorded_sample_path(right["sample_id"])
        try:
            shutil.copyfile(DEAN_BARKER, stored_right)
            self.assertEqual(
                hashlib.sha256(stored_right.read_bytes()).hexdigest(),
                DEAN_BARKER_SHA256,
            )
            refused = self.client.post(endpoint, json=request)
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(
                refused.json()["detail"],
                "owner-supplied pair evidence failed integrity verification",
            )
            self.assertNotIn(str(stored_right), refused.text)
        finally:
            shutil.copyfile(ABEL_RIGHT, stored_right)
        self.assertEqual(
            hashlib.sha256(stored_right.read_bytes()).hexdigest(),
            ABEL_RIGHT_SHA256,
        )
        restored = self.client.post(endpoint, json=request)
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json(), recorded.json())

    def test_direct_pair_refuses_digest_consistent_structural_record_corruption(self) -> None:
        left, right = self._abel_pair(self.project_id)
        endpoint = f"/api/projects/{self.project_id}/owner-pair-admissions"
        request = {
            "left_sample_id": left["sample_id"],
            "right_sample_id": right["sample_id"],
            "relationship": "SAME_PERSON",
        }
        recorded = self.client.post(endpoint, json=request)
        self.assertIn(recorded.status_code, (200, 201), recorded.text)
        pair_id = recorded.json()["pair_id"]
        connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
        try:
            original_json, original_evidence = connection.execute(
                "SELECT record_json, evidence_sha256 FROM owner_pair_admissions WHERE pair_id = ?",
                (pair_id,),
            ).fetchone()
            corrupt_record = json.loads(original_json)
            del corrupt_record["relationship_assertion"]["state"]
            corrupt_json = json.dumps(
                corrupt_record,
                sort_keys=True,
                separators=(",", ":"),
            )
            corrupt_evidence = hashlib.sha256(corrupt_json.encode("utf-8")).hexdigest()
            connection.execute(
                "UPDATE owner_pair_admissions SET record_json = ?, evidence_sha256 = ? WHERE pair_id = ?",
                (corrupt_json, corrupt_evidence, pair_id),
            )
            connection.commit()
        finally:
            connection.close()
        try:
            refused = self.client.post(endpoint, json=request)
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(
                refused.json()["detail"],
                "owner-supplied pair evidence failed integrity verification",
            )
            self.assertNotIn(
                str(self._recorded_sample_path(right["sample_id"])),
                refused.text,
            )
        finally:
            connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
            try:
                connection.execute(
                    "UPDATE owner_pair_admissions SET record_json = ?, evidence_sha256 = ? WHERE pair_id = ?",
                    (original_json, original_evidence, pair_id),
                )
                connection.commit()
            finally:
                connection.close()

    def test_direct_pair_refuses_digest_consistent_record_identity_substitution(self) -> None:
        left, right = self._abel_pair(self.project_id)
        endpoint = f"/api/projects/{self.project_id}/owner-pair-admissions"
        request = {
            "left_sample_id": left["sample_id"],
            "right_sample_id": right["sample_id"],
            "relationship": "SAME_PERSON",
        }
        recorded = self.client.post(endpoint, json=request)
        self.assertIn(recorded.status_code, (200, 201), recorded.text)
        pair_id = recorded.json()["pair_id"]
        connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
        try:
            original_json, original_evidence = connection.execute(
                "SELECT record_json, evidence_sha256 FROM owner_pair_admissions WHERE pair_id = ?",
                (pair_id,),
            ).fetchone()
            substituted_record = json.loads(original_json)
            substituted_record["pair_id"] = left["sample_id"]
            substituted_json = json.dumps(
                substituted_record,
                sort_keys=True,
                separators=(",", ":"),
            )
            substituted_evidence = hashlib.sha256(
                substituted_json.encode("utf-8")
            ).hexdigest()
            connection.execute(
                "UPDATE owner_pair_admissions SET record_json = ?, evidence_sha256 = ? WHERE pair_id = ?",
                (substituted_json, substituted_evidence, pair_id),
            )
            connection.commit()
        finally:
            connection.close()
        try:
            refused = self.client.post(endpoint, json=request)
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(
                refused.json()["detail"],
                "owner-supplied pair evidence failed integrity verification",
            )
        finally:
            connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
            try:
                connection.execute(
                    "UPDATE owner_pair_admissions SET record_json = ?, evidence_sha256 = ? WHERE pair_id = ?",
                    (original_json, original_evidence, pair_id),
                )
                connection.commit()
            finally:
                connection.close()

    def test_direct_pair_refuses_index_column_drift_from_immutable_record(self) -> None:
        left, right = self._abel_pair(self.project_id)
        endpoint = f"/api/projects/{self.project_id}/owner-pair-admissions"
        request = {
            "left_sample_id": left["sample_id"],
            "right_sample_id": right["sample_id"],
            "relationship": "SAME_PERSON",
        }
        recorded = self.client.post(endpoint, json=request)
        self.assertIn(recorded.status_code, (200, 201), recorded.text)
        pair_id = recorded.json()["pair_id"]
        connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
        try:
            connection.execute(
                "UPDATE owner_pair_admissions SET relationship = 'NOT_RECORDED' WHERE pair_id = ?",
                (pair_id,),
            )
            connection.commit()
        finally:
            connection.close()
        try:
            refused = self.client.post(endpoint, json=request)
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(
                refused.json()["detail"],
                "owner-supplied pair evidence failed integrity verification",
            )
        finally:
            connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
            try:
                connection.execute(
                    "UPDATE owner_pair_admissions SET relationship = 'SAME_PERSON' WHERE pair_id = ?",
                    (pair_id,),
                )
                connection.commit()
            finally:
                connection.close()

    def test_direct_pair_transaction_rolls_back_pair_when_audit_insert_fails(self) -> None:
        left, right = self._abel_pair(self.project_id)
        endpoint = f"/api/projects/{self.project_id}/owner-pair-admissions"
        request = {
            "left_sample_id": right["sample_id"],
            "right_sample_id": left["sample_id"],
            "relationship": "SAME_PERSON",
        }
        before_pairs = self._database_count(
            "SELECT COUNT(*) FROM owner_pair_admissions WHERE project_id = ?",
            (self.project_id,),
        )
        before_audits = self._database_count(
            "SELECT COUNT(*) FROM audit_events WHERE project_id = ?",
            (self.project_id,),
        )
        connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
        try:
            connection.execute(
                "CREATE TRIGGER fail_owner_pair_audit "
                "BEFORE INSERT ON audit_events "
                "WHEN NEW.subject_type = 'OwnerPairAdmission' BEGIN "
                "SELECT RAISE(ABORT, 'owner pair audit rollback gate'); END"
            )
            connection.commit()
        finally:
            connection.close()
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "owner pair audit rollback gate",
            ):
                self.client.post(endpoint, json=request)
        finally:
            connection = sqlite3.connect(self.data_dir / "qualsight.sqlite3")
            try:
                connection.execute("DROP TRIGGER fail_owner_pair_audit")
                connection.commit()
            finally:
                connection.close()
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM owner_pair_admissions WHERE project_id = ?",
                (self.project_id,),
            ),
            before_pairs,
        )
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM audit_events WHERE project_id = ?",
                (self.project_id,),
            ),
            before_audits,
        )
        admitted = self.client.post(endpoint, json=request)
        self.assertEqual(admitted.status_code, 201, admitted.text)
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM owner_pair_admissions WHERE project_id = ?",
                (self.project_id,),
            ),
            before_pairs + 1,
        )
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM audit_events WHERE project_id = ?",
                (self.project_id,),
            ),
            before_audits + 1,
        )

    def test_owner_pair_purge_removes_rows_and_preserves_shared_bytes_until_final_reference(self) -> None:
        projects: list[str] = []
        pairs: list[dict] = []
        uploads: list[tuple[dict, dict]] = []
        for name in ("owner pair purge one", "owner pair purge two"):
            project_id = self._new_project(name)
            left = self._upload_real_photo(project_id, ABDEL_MADI)
            right = self._upload_real_photo(project_id, DEAN_BARKER)
            self.assertEqual(left["image_sha256"], ABDEL_MADI_SHA256)
            self.assertEqual(right["image_sha256"], DEAN_BARKER_SHA256)
            admitted = self.client.post(
                f"/api/projects/{project_id}/owner-pair-admissions",
                json={
                    "left_sample_id": left["sample_id"],
                    "right_sample_id": right["sample_id"],
                    "relationship": "DIFFERENT_PEOPLE",
                },
            )
            self.assertEqual(admitted.status_code, 201, admitted.text)
            projects.append(project_id)
            pairs.append(admitted.json())
            uploads.append((left, right))

        stored_paths = (
            self._recorded_sample_path(uploads[0][0]["sample_id"]),
            self._recorded_sample_path(uploads[0][1]["sample_id"]),
        )
        self.assertEqual(
            self._recorded_sample_path(uploads[1][0]["sample_id"]), stored_paths[0]
        )
        self.assertEqual(
            self._recorded_sample_path(uploads[1][1]["sample_id"]), stored_paths[1]
        )
        first_purge = self.client.post(
            f"/api/projects/{projects[0]}/purge",
            json={"confirm": projects[0]},
        )
        self.assertEqual(first_purge.status_code, 201, first_purge.text)
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM owner_pair_admissions WHERE pair_id = ?",
                (pairs[0]["pair_id"],),
            ),
            0,
        )
        self.assertTrue(all(path.exists() for path in stored_paths))

        second_purge = self.client.post(
            f"/api/projects/{projects[1]}/purge",
            json={"confirm": projects[1]},
        )
        self.assertEqual(second_purge.status_code, 201, second_purge.text)
        self.assertEqual(
            self._database_count(
                "SELECT COUNT(*) FROM owner_pair_admissions WHERE pair_id = ?",
                (pairs[1]["pair_id"],),
            ),
            0,
        )
        self.assertFalse(any(path.exists() for path in stored_paths))

    # -- QS-Y80 + QS-Y81 -------------------------------------------------------

    def test_y81_uploaded_photo_yields_recorded_run_with_source_kind(self) -> None:
        photo = self.owner_root / "operator_a" / "photo_00.jpg"
        data = photo.read_bytes()
        expected_sha = hashlib.sha256(data).hexdigest()
        response = self.client.post(
            "/api/quality/assess-upload",
            params={"project_id": self.project_id, "purpose_scope": RESEARCH_SCOPE, "filename": "my_visit_photo.jpg"},
            content=data,
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["image_sha256"], expected_sha)
        self.assertEqual(body["source_kind"], "OWNER_SUPPLIED_LOCAL_INPUT")
        self.assertEqual(len(body["measures"]), 28)
        unified = {m["name"]: m for m in body["measures"]}["UnifiedQualityScore"]
        self.assertFalse(unified["failed"])
        self.assertGreaterEqual(unified["scalar"], 0)
        self.assertNotIn("stored_path", body)
        self.assertEqual(
            body["provenance"]["sample_image"],
            f"/api/projects/{self.project_id}/samples/{body['sample_id']}/image",
        )
        # The stored copy is content-addressed inside the data directory and re-hashes to itself.
        stored = self._recorded_sample_path(body["sample_id"])
        self.assertTrue(stored.is_relative_to(self.data_dir))
        self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), expected_sha)
        # The Run is recorded and names the owner-supplied source kind.
        run = self.client.get(f"/api/projects/{self.project_id}/runs/{body['run_id']}")
        self.assertEqual(run.status_code, 200)
        run_text = json.dumps(run.json())
        self.assertIn("OWNER_SUPPLIED_LOCAL_INPUT", run_text)

    def test_y81_non_image_bytes_are_refused(self) -> None:
        response = self.client.post(
            "/api/quality/assess-upload",
            params={"project_id": self.project_id, "purpose_scope": RESEARCH_SCOPE, "filename": "notes.txt"},
            content=b"not an image at all",
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("decode", response.json()["detail"].lower())

    def test_y80_path_bearing_assessment_contract_is_retired(self) -> None:
        rogue = self.owner_root / "operator_a" / "photo_01.jpg"
        response = self.client.post(
            "/api/quality/assess",
            json={"image_path": str(rogue), "purpose_scope": RESEARCH_SCOPE},
        )
        self.assertEqual(response.status_code, 422, response.text)

    # -- QS-Y82 ----------------------------------------------------------------

    def test_public_dataset_projections_hide_owner_and_derived_paths_but_preserve_publisher_identity(self) -> None:
        project_id = self._new_project("public dataset path privacy gate")
        imported = self.client.post(
            f"/api/projects/{project_id}/datasets/import-owner-directory",
            json={"root": str(self.owner_root), "name": "real owner directory privacy gate"},
        )
        self.assertEqual(imported.status_code, 201, imported.text)
        owner_dataset_id = imported.json()["dataset_id"]
        owner_detail = self.client.get(
            f"/api/projects/{project_id}/datasets/{owner_dataset_id}"
        )
        self.assertEqual(owner_detail.status_code, 200, owner_detail.text)
        owner_list = self.client.get(f"/api/projects/{project_id}/datasets")
        self.assertEqual(owner_list.status_code, 200, owner_list.text)
        owner_list_item = next(
            item for item in owner_list.json() if item["dataset_id"] == owner_dataset_id
        )
        for payload in (owner_detail.json(), owner_list_item):
            self.assertIsNone(payload["source_uri"])
            public_text = json.dumps(payload, sort_keys=True)
            self.assertNotIn(str(self.owner_root), public_text)
            self.assertNotIn(str(self.data_dir), public_text)

        private_owner = json.loads(
            self._database_row(
                "SELECT record_json FROM datasets WHERE dataset_id = ?",
                (owner_dataset_id,),
            )[0]
        )
        self.assertEqual(private_owner["source_uri"], f"file://{self.owner_root}")

        samples = self.client.get(
            f"/api/projects/{project_id}/datasets/{owner_dataset_id}/samples"
        )
        self.assertEqual(samples.status_code, 200, samples.text)
        derived = self.client.post(
            f"/api/projects/{project_id}/datasets/{owner_dataset_id}/transformations",
            json={
                "selection": "sample_ids",
                "sample_ids": [samples.json()[0]["sample_id"]],
                "transforms": [{"kind": "jpeg", "parameters": [73]}],
            },
        )
        self.assertEqual(derived.status_code, 201, derived.text)
        derived_dataset_id = derived.json()["derived_dataset_id"]
        derived_detail = self.client.get(
            f"/api/projects/{project_id}/datasets/{derived_dataset_id}"
        )
        self.assertEqual(derived_detail.status_code, 200, derived_detail.text)
        self.assertIsNone(derived_detail.json()["source_uri"])
        derived_text = json.dumps(derived_detail.json(), sort_keys=True)
        self.assertNotIn(str(self.owner_root), derived_text)
        self.assertNotIn(str(self.data_dir), derived_text)
        private_derived = json.loads(
            self._database_row(
                "SELECT record_json FROM datasets WHERE dataset_id = ?",
                (derived_dataset_id,),
            )[0]
        )
        self.assertTrue(Path(private_derived["source_uri"]).is_absolute())
        self.assertTrue(Path(private_derived["source_uri"]).is_relative_to(self.data_dir))

        registered = self.client.post(
            f"/api/projects/{project_id}/datasets/import-registered",
            json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        registered_dataset_id = registered.json()["dataset_id"]
        private_registered = json.loads(
            self._database_row(
                "SELECT record_json FROM datasets WHERE dataset_id = ?",
                (registered_dataset_id,),
            )[0]
        )
        self.assertTrue(private_registered["source_uri"].startswith(("http://", "https://")))
        self.assertEqual(
            registered.json()["source_uri"], private_registered["source_uri"]
        )
        registered_detail = self.client.get(
            f"/api/projects/{project_id}/datasets/{registered_dataset_id}"
        )
        self.assertEqual(registered_detail.status_code, 200, registered_detail.text)
        self.assertEqual(
            registered_detail.json()["source_uri"], private_registered["source_uri"]
        )

    def test_y82_directory_import_records_identity_labelled_dataset_and_batch_runs(self) -> None:
        response = self.client.post(
            f"/api/projects/{self.project_id}/datasets/import-owner-directory",
            json={"root": str(self.owner_root), "name": "evaluator directory"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["subject_count"], 3)
        self.assertEqual(body["sample_count"], 11)
        self.assertEqual(body["source_kind"], "OWNER_SUPPLIED_LOCAL_INPUT")
        dataset_id = body["dataset_id"]
        type(self).directory_dataset_id = dataset_id
        started = self.client.post(
            f"/api/projects/{self.project_id}/datasets/{dataset_id}/quality-batch",
            json={"selection": {"kind": "all"}, "chunks": 1},
        )
        self.assertEqual(started.status_code, 202, started.text)
        finished = self._wait(started.json()["job_id"], timeout=180)
        self.assertEqual(finished["state"], "COMPLETED")
        self.assertEqual(finished["assessed"], 11)
        self.assertEqual(finished["failed"], 0)
        self.assertTrue(finished["run_id"])

    def test_y82_import_failures_are_path_free_at_the_public_boundary(self) -> None:
        directory_source = ROOT / "README.md"
        self.assertTrue(directory_source.is_file())
        directory = self.client.post(
            f"/api/projects/{self.project_id}/datasets/import-owner-directory",
            json={"root": str(directory_source), "name": "Rejected real file import"},
        )
        self.assertEqual(directory.status_code, 400, directory.text)
        self.assertEqual(
            directory.json()["detail"],
            "owner directory import was refused; review the private local diagnostic log",
        )
        self.assertNotIn(str(directory_source), directory.text)

        manifest_source = LFW_TREE
        self.assertTrue(manifest_source.is_dir())
        manifest = self.client.post(
            f"/api/projects/{self.project_id}/datasets/import-owner-manifest",
            json={
                "manifest_path": str(manifest_source),
                "name": "Rejected real directory manifest",
                "mapping": {"subject_id": "person_id", "path": "file"},
            },
        )
        self.assertEqual(manifest.status_code, 400, manifest.text)
        self.assertEqual(
            manifest.json()["detail"],
            "owner manifest import was refused; review the private local diagnostic log",
        )
        self.assertNotIn(str(manifest_source), manifest.text)

    def test_batch_run_warning_is_path_free_while_private_parquet_retains_source(self) -> None:
        fixture_root = Path(tempfile.mkdtemp(prefix="qualsight-real-warning-fixture-"))
        self.addCleanup(shutil.rmtree, fixture_root, True)
        subject_root = fixture_root / "Abel_Pacheco"
        subject_root.mkdir(parents=True)
        for source in (ABEL_LEFT, ABEL_RIGHT):
            copied = subject_root / source.name
            shutil.copyfile(source, copied)
            self.assertEqual(
                hashlib.sha256(copied.read_bytes()).hexdigest(),
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
        imported = self.client.post(
            f"/api/projects/{self.project_id}/datasets/import-owner-directory",
            json={"root": str(fixture_root), "name": "Real warning privacy fixture"},
        )
        self.assertEqual(imported.status_code, 201, imported.text)
        samples_response = self.client.get(
            f"/api/projects/{self.project_id}/datasets/{imported.json()['dataset_id']}/samples"
        )
        self.assertEqual(samples_response.status_code, 200, samples_response.text)
        samples = samples_response.json()
        self.assertEqual(len(samples), 2)
        from qualsight.api.store import RunStore
        from qualsight.engines.ofiq import OFIQQualityEngine
        from qualsight.engines.ofiq.batch import OFIQBatchJob

        private_store = RunStore(self.data_dir / "qualsight.sqlite3")
        self.addCleanup(private_store.close)
        project = private_store.project(self.project_id)
        dataset = private_store.dataset(imported.json()["dataset_id"])
        self.assertIsNotNone(project)
        self.assertIsNotNone(dataset)
        private_samples = private_store.samples_for_dataset(dataset.dataset_id, limit=100)
        engine = OFIQQualityEngine(
            registry_path=ROOT / "resources" / "resource-registry.json",
            repository_root=ROOT,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        job = OFIQBatchJob(
            engine=engine,
            store=private_store,
            project=project,
            dataset=dataset,
            samples=private_samples,
            purpose_scope=RESEARCH_SCOPE,
            job_root=self.data_dir / "direct-real-warning-jobs",
            chunks=1,
            selection_rule="all two admitted real LFW owner photographs",
        )
        unavailable_sample = samples[0]
        unavailable_path = self._recorded_sample_path(unavailable_sample["sample_id"])
        self.assertEqual(
            hashlib.sha256(unavailable_path.read_bytes()).hexdigest(),
            unavailable_sample["sha256"],
        )
        unavailable_path.unlink()
        self.assertFalse(unavailable_path.exists())

        finished = job.run()
        self.assertEqual(finished.state, "COMPLETED", finished)
        self.assertEqual(finished.integrity_failures, 1, finished)
        self.assertIsNotNone(finished.run_id)

        from pyarrow import parquet as pq

        self.assertIsNotNone(job.parquet_path)
        private_rows = pq.read_table(job.parquet_path).to_pylist()
        private_failure = next(
            row
            for row in private_rows
            if row["sample_id"] == unavailable_sample["sample_id"]
        )
        self.assertEqual(private_failure["source_path"], str(unavailable_path))
        self.assertEqual(private_failure["outcome"], "INTEGRITY_FAILURE")
        self.assertIn(str(unavailable_path), private_failure["failure_reason"])

        self.assertIsNotNone(job.completed_run)
        warnings = job.completed_run.warnings
        self.assertEqual(len(warnings), 1, warnings)
        warning = json.loads(warnings[0])
        self.assertEqual(warning["outcome"], "INTEGRITY_FAILURE")
        self.assertEqual(warning["sample_id"], unavailable_sample["sample_id"])
        self.assertIn("recorded sample", warning["reason"])
        warning_text = json.dumps(warnings, sort_keys=True)
        self.assertNotIn(str(unavailable_path), warning_text)
        self.assertNotIn(str(self.data_dir), warning_text)
        self.assertNotIn(str(fixture_root), warning_text)

        public_results = self.client.get(
            f"/api/projects/{self.project_id}/jobs/{job.job_id}/results",
            params={"limit": 100},
        )
        self.assertEqual(public_results.status_code, 200, public_results.text)
        public_failure = next(
            row
            for row in public_results.json()["rows"]
            if row["sample_id"] == unavailable_sample["sample_id"]
        )
        self.assertIn("recorded local resource", public_failure["failure_reason"])

        drilldown = self.client.get(
            f"/api/projects/{self.project_id}/jobs/{job.job_id}/drilldown",
            params={"outcome": "INTEGRITY_FAILURE", "limit": 100},
        )
        self.assertEqual(drilldown.status_code, 200, drilldown.text)
        self.assertEqual(drilldown.json()["total"], 1)
        self.assertIn(
            "recorded local resource", drilldown.json()["rows"][0]["failure_reason"]
        )

        sample_quality = self.client.get(
            f"/api/projects/{self.project_id}/samples/{unavailable_sample['sample_id']}/quality"
        )
        self.assertEqual(sample_quality.status_code, 200, sample_quality.text)
        recorded_failure = next(
            result
            for result in sample_quality.json()["results"]
            if result["job_id"] == job.job_id
        )
        self.assertIn("recorded local resource", recorded_failure["failure_reason"])

        for response in (public_results, drilldown, sample_quality):
            self.assertNotIn(str(unavailable_path), response.text)
            self.assertNotIn(str(self.data_dir), response.text)

    def test_y82_manifest_import_with_saved_mapping(self) -> None:
        manifest = self.owner_root / "evaluation_manifest.csv"
        with manifest.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["person_id", "file", "notes"])
            for alias in SUBJECTS:
                for path in sorted((self.owner_root / alias).glob("*.jpg")):
                    writer.writerow([alias, str(path), "evaluator row"])
        mapping = {"subject_id": "person_id", "path": "file"}
        response = self.client.post(
            f"/api/projects/{self.project_id}/datasets/import-owner-manifest",
            json={
                "manifest_path": str(manifest),
                "name": "evaluator manifest",
                "mapping": mapping,
                "mapping_name": "evaluator-csv-v1",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["subject_count"], 3)
        self.assertEqual(body["sample_count"], 11)
        # The mapping is saved and reusable by name (PRD section 12).
        mappings = self.client.get("/api/import-mappings").json()
        names = {item["mapping_name"]: item["mapping"] for item in mappings}
        self.assertEqual(names.get("evaluator-csv-v1"), mapping)
        reused = self.client.post(
            f"/api/projects/{self.project_id}/datasets/import-owner-manifest",
            json={
                "manifest_path": str(manifest),
                "name": "evaluator manifest reuse",
                "mapping_name": "evaluator-csv-v1",
            },
        )
        self.assertEqual(reused.status_code, 201, reused.text)
        self.assertEqual(reused.json()["sample_count"], 11)

    def test_y82_traversal_and_symlink_rows_are_refused(self) -> None:
        manifest = self.owner_root / "hostile_manifest.csv"
        outside = LFW_TREE / "Colin_Powell" / "Colin_Powell_0001.jpg"
        with manifest.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["person_id", "file"])
            writer.writerow(["mallory", str(outside)])
        response = self.client.post(
            f"/api/projects/{self.project_id}/datasets/import-owner-manifest",
            json={
                "manifest_path": str(manifest),
                "name": "hostile manifest",
                "mapping": {"subject_id": "person_id", "path": "file"},
            },
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(
            response.json()["detail"],
            "owner manifest import was refused; review the private local diagnostic log",
        )
        self.assertNotIn(str(manifest), response.text)
        self.assertNotIn(str(outside), response.text)

    # -- QS-Y83 ----------------------------------------------------------------

    def test_y83_owner_protocol_is_deterministic_and_matcher_executes(self) -> None:
        if not hasattr(type(self), "directory_dataset_id"):
            self.test_y82_directory_import_records_identity_labelled_dataset_and_batch_runs()
        dataset_id = type(self).directory_dataset_id
        request = {"seed": 20260819, "max_mated_pairs_per_subject": 6, "nonmated_multiple": 1.0}
        first = self.client.post(
            f"/api/projects/{self.project_id}/datasets/{dataset_id}/protocols/freeze-owner-pairs",
            json=request,
        )
        self.assertEqual(first.status_code, 201, first.text)
        record = first.json()
        self.assertGreaterEqual(record["mated_count"], 9)
        self.assertEqual(record["nonmated_count"], record["mated_count"])
        self.assertTrue(record["pairs_sha256"])
        # Determinism: identical dataset + seed -> the identical frozen pair list.
        second = self.client.post(
            f"/api/projects/{self.project_id}/datasets/{dataset_id}/protocols/freeze-owner-pairs",
            json=request,
        )
        self.assertIn(second.status_code, (200, 201), second.text)
        self.assertEqual(second.json()["pairs_sha256"], record["pairs_sha256"])
        # The frozen owner protocol executes through the real matcher matrix.
        pulled = self.client.post("/api/matcher-assets/adaface-ir101-local-candidate/pull-sources")
        self.assertEqual(pulled.status_code, 200, pulled.text)
        started = self.client.post(
            f"/api/projects/{self.project_id}/protocols/{record['protocol_id']}/matrix",
            params={"leaves": "QS-C22.AS"},
        )
        self.assertEqual(started.status_code, 202, started.text)
        matrix_id = started.json()["matrix_id"]
        deadline = time.monotonic() + 600
        status: dict = {}
        while time.monotonic() < deadline:
            status = self.client.get(f"/api/projects/{self.project_id}/matrices/{matrix_id}").json()
            if status["progress"]["phase"] in {"completed", "failed", "partial"} and status["leaves"]:
                break
            if status["progress"]["phase"] == "failed":
                break
            time.sleep(1.0)
        self.assertNotEqual(status["progress"]["phase"], "failed", status)
        self.assertEqual(len(status["leaves"]), 1, status)
        leaf = status["leaves"][0]
        self.assertEqual(leaf["comparison_count"], record["pair_count"])
        self.assertIn("EXCLUDE_FAILED_TRIALS", leaf["eer"])

    # -- QS-Y80 purge ----------------------------------------------------------

    def test_y80_purge_removes_owner_supplied_copies(self) -> None:
        photo = self.owner_root / "operator_b" / "photo_00.jpg"
        data = photo.read_bytes()
        project = self.client.post(
            "/api/projects",
            json={"name": "owner purge gate", "description": "Plan D C27 purge.", "purpose_scope": RESEARCH_SCOPE},
        ).json()
        self.client.post(f"/api/projects/{project['project_id']}/open")
        uploaded = self.client.post(
            "/api/quality/assess-upload",
            params={"project_id": project["project_id"], "purpose_scope": RESEARCH_SCOPE, "filename": "purge_me.jpg"},
            content=data,
            headers={"content-type": "application/octet-stream"},
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        uploaded_body = uploaded.json()
        self.assertNotIn("stored_path", uploaded_body)
        self.assertEqual(
            uploaded_body["provenance"]["sample_image"],
            f"/api/projects/{project['project_id']}/samples/{uploaded_body['sample_id']}/image",
        )
        stored = self._recorded_sample_path(uploaded_body["sample_id"])
        self.assertTrue(stored.exists())
        purged = self.client.post(
            f"/api/projects/{project['project_id']}/purge",
            json={"confirm": project["project_id"]},
        )
        self.assertIn(purged.status_code, (200, 201), purged.text)
        self.assertFalse(stored.exists(), "purge must remove the owner-supplied stored copy")

    def test_y80_shared_content_addressed_copy_survives_until_last_project_purge(self) -> None:
        data = (self.owner_root / "operator_c" / "photo_02.jpg").read_bytes()
        projects: list[str] = []
        uploads: list[dict] = []
        for name in ("shared owner reference one", "shared owner reference two"):
            project = self.client.post(
                "/api/projects",
                json={"name": name, "description": "Cross-project owner-input purge gate.", "purpose_scope": RESEARCH_SCOPE},
            ).json()
            project_id = project["project_id"]
            projects.append(project_id)
            self.client.post(f"/api/projects/{project_id}/open")
            response = self.client.post(
                "/api/quality/assess-upload",
                params={"project_id": project_id, "purpose_scope": RESEARCH_SCOPE, "filename": "shared_real_photo.jpg"},
                content=data,
                headers={"content-type": "application/octet-stream"},
            )
            self.assertEqual(response.status_code, 201, response.text)
            uploads.append(response.json())

        stored = self._recorded_sample_path(uploads[0]["sample_id"])
        self.assertEqual(
            self._recorded_sample_path(uploads[1]["sample_id"]), stored
        )
        self.assertTrue(stored.exists())

        first_purge = self.client.post(
            f"/api/projects/{projects[0]}/purge",
            json={"confirm": projects[0]},
        )
        self.assertIn(first_purge.status_code, (200, 201), first_purge.text)
        self.assertTrue(stored.exists(), "a managed copy referenced by the second project must survive the first purge")
        self.assertEqual(len(self.client.get(f"/api/projects/{projects[1]}/datasets").json()), 1)

        second_purge = self.client.post(
            f"/api/projects/{projects[1]}/purge",
            json={"confirm": projects[1]},
        )
        self.assertIn(second_purge.status_code, (200, 201), second_purge.text)
        self.assertFalse(stored.exists(), "the managed copy must be deleted after its final project reference is purged")


if __name__ == "__main__":
    unittest.main()
