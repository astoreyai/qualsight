"""QS-B12: batch OFIQ quality execution over real imported LFW samples.

Runs the registered engine over the deterministic real subset fixture (all
outcomes explicit), verifies Parquet + immutable batch Run + job bookkeeping,
and exercises real cancellation of a larger batch with process-group
termination and explicit NOT_ASSESSED rows.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from fastapi.testclient import TestClient
import pyarrow.parquet as pq

from qualsight.api import Settings, create_app
from qualsight.domain.run import CompletedRun
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)


ROOT = Path(__file__).resolve().parents[3]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class RealQualityBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-batch-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        cls.client = TestClient(create_app(settings))
        cls.client.__enter__()
        project = cls.client.post(
            "/api/projects",
            json={"name": "LFW batch gate", "description": "QS-B12 real batch.", "purpose_scope": RESEARCH_SCOPE},
        ).json()
        cls.project_id = project["project_id"]
        cls.client.post(f"/api/projects/{cls.project_id}/open")
        imported = cls.client.post(
            f"/api/projects/{cls.project_id}/datasets/import-registered",
            json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
        )
        assert imported.status_code == 201, imported.text
        cls.dataset_id = imported.json()["dataset_id"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def _wait(self, job_id: str, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            body = self.client.get(f"/api/jobs/{job_id}").json()
            if body["state"] in {"COMPLETED", "CANCELLED", "FAILED"} and body["ended_at"]:
                return body
            time.sleep(0.5)
        raise AssertionError(f"job {job_id} did not finish: {body}")

    def test_subset_fixture_batch_yields_a_result_or_explicit_failure_per_image(self) -> None:
        started = self.client.post(
            f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/quality-batch",
            json={"selection": {"kind": "subset_fixture"}, "chunks": 2, "purpose_scope": RESEARCH_SCOPE},
        )
        self.assertEqual(started.status_code, 202, started.text)
        job_id = started.json()["job_id"]
        fixture = json.loads((ROOT / "resources" / "manifests" / "lfw-subset-fixture-v1.json").read_text(encoding="utf-8"))
        expected_total = len(fixture["images"])
        self.assertEqual(started.json()["total"], expected_total)
        finished = self._wait(job_id, timeout=300)
        self.assertEqual(finished["state"], "COMPLETED", finished)
        self.assertEqual(finished["error"], None)
        self.assertEqual(finished["integrity_failures"], 0)
        self.assertEqual(finished["not_assessed"], 0)
        self.assertEqual(finished["assessed"] + finished["failed"], expected_total)
        self.assertEqual(finished["engine_version"], "1.1.2")
        self.assertIsNotNone(finished["run_id"])
        self.assertGreater(finished["resource_use"]["wall_seconds"], 0)
        self.assertGreater(finished["resource_use"]["children_user_cpu_seconds"], 0)
        self.assertEqual(finished["resource_use"]["processes"], 2.0)

        # Parquet holds exactly one row per selected sample with per-measure values.
        table = pq.read_table(finished["parquet_path"])
        self.assertEqual(table.num_rows, expected_total)
        outcomes = table.column("outcome").to_pylist()
        self.assertTrue(all(value in {"MEASURED", "ENGINE_ROW_FAILURE"} for value in outcomes), outcomes)
        rows = table.to_pylist()
        measured = [row for row in rows if row["outcome"] == "MEASURED"]
        self.assertGreater(len(measured), 0)
        for row in measured:
            self.assertIsNotNone(row["UnifiedQualityScore.scalar"])
            self.assertGreaterEqual(row["UnifiedQualityScore.scalar"], 0)
            self.assertLessEqual(row["UnifiedQualityScore.scalar"], 100)
            self.assertIsNotNone(row["assessment_ms"])
            self.assertEqual(row["subject_id"], Path(row["source_path"]).parent.name)
        by_path = {image["path"]: image for image in fixture["images"]}
        for row in rows:
            relative = row["source_path"].split("/lfw_funneled/", 1)[1]
            self.assertEqual(row["sha256"], by_path[relative]["sha256"])

        # The immutable batch Run binds the engine, models, and Parquet artifact.
        stored = self.client.get(f"/api/runs/{finished['run_id']}")
        self.assertEqual(stored.status_code, 200, stored.text)
        run = CompletedRun.model_validate_json(json.dumps(stored.json()["run"]))
        metrics = {m.metric_id: m.value for m in run.metrics}
        self.assertEqual(metrics["batch.samples_selected"], expected_total)
        self.assertEqual(metrics["batch.samples_measured"], len(measured))
        self.assertEqual(metrics["batch.engine_row_failures"], expected_total - len(measured))
        self.assertEqual(len(run.model_hashes), 12)
        parquet_artifacts = [a for a in run.artifacts if a.storage_backend.value == "PARQUET"]
        self.assertEqual(len(parquet_artifacts), 1)
        self.assertEqual(parquet_artifacts[0].sha256, hashlib.sha256(Path(finished["parquet_path"]).read_bytes()).hexdigest())
        self.assertEqual(json.loads(run.dataset_manifest.text)["dataset_id"], self.dataset_id)
        self.assertEqual(len(json.loads(run.dataset_manifest.text)["chunks"]), 2)

        # Results endpoint pages the Parquet rows; project job list shows the job.
        results = self.client.get(f"/api/jobs/{job_id}/results", params={"limit": 5}).json()
        self.assertEqual(results["total"], expected_total)
        self.assertEqual(len(results["rows"]), 5)
        listed = self.client.get(f"/api/projects/{self.project_id}/jobs").json()
        self.assertIn(job_id, {item["job_id"] for item in listed})
        # Every recorded outcome is explicit — no silent drop.
        self.assertEqual(sorted(row["sample_id"] for row in rows), sorted(self.client.get(f"/api/jobs/{job_id}/results", params={"limit": 1000}).json()["rows"][i]["sample_id"] for i in range(expected_total)))

    def test_explorer_distribution_reconciles_to_batch_rows_and_drills_down_to_real_samples(self) -> None:
        # Reuse a completed subset-fixture batch (start one if the other test has not run yet).
        listed = [j for j in self.client.get(f"/api/projects/{self.project_id}/jobs").json() if j["state"] == "COMPLETED"]
        if not listed:
            started = self.client.post(
                f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/quality-batch",
                json={"selection": {"kind": "subset_fixture"}, "chunks": 1, "purpose_scope": RESEARCH_SCOPE},
            )
            self.assertEqual(started.status_code, 202, started.text)
            listed = [self._wait(started.json()["job_id"], timeout=300)]
        job = listed[0]
        job_id = job["job_id"]
        distribution = self.client.get(f"/api/jobs/{job_id}/distribution", params={"measure": "UnifiedQualityScore", "component": "scalar", "bins": 10})
        self.assertEqual(distribution.status_code, 200, distribution.text)
        body = distribution.json()
        table = pq.read_table(job["parquet_path"]).to_pylist()
        measured = [row for row in table if row["outcome"] == "MEASURED" and row["UnifiedQualityScore.scalar"] is not None]
        # Bin counts reconcile exactly to the Parquet rows.
        self.assertEqual(sum(bin_["count"] for bin_ in body["bins"]), len(measured))
        self.assertEqual(body["measured_rows_with_value"], len(measured))
        expected_counts = [0] * 10
        for row in measured:
            index = min(int(row["UnifiedQualityScore.scalar"] // 10), 9)
            expected_counts[index] += 1
        self.assertEqual([bin_["count"] for bin_ in body["bins"]], expected_counts)
        self.assertEqual(body["outcomes"].get("MEASURED", 0), len(measured))
        self.assertEqual(sum(body["outcomes"].values()), len(table))
        self.assertEqual(body["summary"]["min"], min(row["UnifiedQualityScore.scalar"] for row in measured))
        self.assertEqual(body["summary"]["max"], max(row["UnifiedQualityScore.scalar"] for row in measured))
        self.assertEqual(body["run_id"], job["run_id"])
        # Drill into the most populated bin and resolve every point to its sample, Run, and provenance.
        top = max(body["bins"], key=lambda bin_: bin_["count"])
        low, high = int(top["low"]), int(top["high"]) - (1 if top["bin"] < 9 else 0)
        drill = self.client.get(f"/api/jobs/{job_id}/drilldown", params={"min_scalar": low, "max_scalar": high, "outcome": "MEASURED", "limit": 500}).json()
        self.assertEqual(drill["total"], top["count"])
        self.assertEqual(len(drill["rows"]), top["count"])
        for point in drill["rows"]:
            self.assertGreaterEqual(point["uqs_scalar"], low)
            self.assertLessEqual(point["uqs_scalar"], high)
            sample = self.client.get(point["links"]["sample"])
            self.assertEqual(sample.status_code, 200, sample.text)
            self.assertEqual(sample.json()["sha256"], point["sha256"])
            quality = self.client.get(point["links"]["quality"]).json()
            self.assertEqual(quality["sample"]["sample_id"], point["sample_id"])
            recorded = [r for r in quality["results"] if r["job_id"] == job_id]
            self.assertEqual(len(recorded), 1)
            self.assertEqual(recorded[0]["uqs_scalar"], point["uqs_scalar"])
            self.assertEqual(recorded[0]["run_id"], job["run_id"])
            run = self.client.get(point["links"]["run"])
            self.assertEqual(run.status_code, 200)
            self.assertEqual(run.json()["run"]["run_id"], job["run_id"])
            image = self.client.get(point["links"]["image"])
            self.assertEqual(image.status_code, 200)
            self.assertEqual(hashlib.sha256(image.content).hexdigest(), point["sha256"])
            self.assertEqual(image.headers["cache-control"], "no-store")
            # The parquet row for this sample carries the same value.
            parquet_row = next(row for row in table if row["sample_id"] == point["sample_id"])
            self.assertEqual(parquet_row["UnifiedQualityScore.scalar"], point["uqs_scalar"])
        native = self.client.get(f"/api/jobs/{job_id}/distribution", params={"measure": "Sharpness", "component": "native", "bins": 5}).json()
        self.assertEqual(sum(bin_["count"] for bin_ in native["bins"]), sum(1 for row in table if row["outcome"] == "MEASURED" and row["Sharpness.native"] is not None))
        unknown = self.client.get(f"/api/jobs/{job_id}/distribution", params={"measure": "NotAMeasure"})
        self.assertEqual(unknown.status_code, 422)

    def test_real_engine_failure_sentinel_is_an_explicit_measure_failure(self) -> None:
        # Observed on the full LFW batch of 2026-08-17: OFIQ reports native "nan"
        # and scalar -1 for UnderExposurePrevention and OverExposurePrevention on
        # this real image. The row must stay MEASURED with two explicit measure
        # failures and no fabricated number.
        path = "/mnt/projects/datasets/lfw/lfw_funneled/Abdoulaye_Wade/Abdoulaye_Wade_0003.jpg"
        samples = self.client.get(f"/api/datasets/{self.dataset_id}/samples", params={"subject_id": "Abdoulaye_Wade", "limit": 50}).json()
        sample = next(item for item in samples if item["source_path"] == path)
        started = self.client.post(
            f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/quality-batch",
            json={"selection": {"kind": "sample_ids", "sample_ids": [sample["sample_id"]]}, "chunks": 1},
        )
        self.assertEqual(started.status_code, 202, started.text)
        finished = self._wait(started.json()["job_id"], timeout=300)
        self.assertEqual(finished["state"], "COMPLETED", finished)
        row = pq.read_table(finished["parquet_path"]).to_pylist()[0]
        self.assertEqual(row["outcome"], "MEASURED")
        for measure in ("UnderExposurePrevention", "OverExposurePrevention"):
            self.assertTrue(row[f"{measure}.failed"], measure)
            self.assertIsNone(row[f"{measure}.scalar"], measure)
            self.assertIsNone(row[f"{measure}.native"], measure)
        self.assertFalse(row["UnifiedQualityScore.failed"])
        self.assertIsNotNone(row["UnifiedQualityScore.scalar"])
        run = CompletedRun.model_validate_json(json.dumps(self.client.get(f"/api/runs/{finished['run_id']}").json()["run"]))
        metrics = {m.metric_id: m.value for m in run.metrics}
        self.assertEqual(metrics["batch.measure_level_failures"], 2)
        self.assertEqual(metrics["batch.samples_measured"], 1)
        quality = self.client.get(f"/api/samples/{sample['sample_id']}/quality").json()
        recorded = next(r for r in quality["results"] if r["job_id"] == finished["job_id"])
        self.assertTrue(recorded["measures"]["UnderExposurePrevention"]["failed"])
        self.assertIsNone(recorded["measures"]["UnderExposurePrevention"]["scalar"])
        distribution = self.client.get(f"/api/jobs/{finished['job_id']}/distribution", params={"measure": "UnderExposurePrevention"}).json()
        self.assertEqual(distribution["measure_failures"], 1)
        self.assertEqual(distribution["measured_rows_with_value"], 0)
        coverage = self.client.get(f"/api/datasets/{self.dataset_id}/statistics").json()["statistics"]["quality_coverage"]
        self.assertEqual(coverage["state"], "COMPUTED")
        self.assertIn(finished["job_id"], coverage["jobs"])
        self.assertGreaterEqual(coverage["component_failures"].get("UnderExposurePrevention", 0), 1)
        self.assertGreater(coverage["quality_assessment_success_rate"], 0)

    def test_cancellation_terminates_engine_processes_and_marks_remaining_samples(self) -> None:
        started = self.client.post(
            f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/quality-batch",
            json={"selection": {"kind": "subject", "subject_id": "George_W_Bush"}, "chunks": 1, "purpose_scope": RESEARCH_SCOPE},
        )
        self.assertEqual(started.status_code, 202, started.text)
        job_id = started.json()["job_id"]
        self.assertEqual(started.json()["total"], 530)
        # Let the engine initialize and process a few images, then cancel.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            body = self.client.get(f"/api/jobs/{job_id}").json()
            if body["assessed"] >= 3:
                break
            time.sleep(0.5)
        cancelled = self.client.post(f"/api/jobs/{job_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        finished = self._wait(job_id, timeout=120)
        self.assertEqual(finished["state"], "CANCELLED", finished)
        self.assertTrue(finished["cancellation_requested"])
        self.assertIsNone(finished["run_id"])
        self.assertGreater(finished["not_assessed"], 0)
        self.assertEqual(finished["assessed"] + finished["failed"] + finished["not_assessed"], 530)
        table = pq.read_table(finished["parquet_path"])
        outcomes = table.column("outcome").to_pylist()
        self.assertEqual(len(outcomes), 530)
        self.assertIn("NOT_ASSESSED", outcomes)
        reasons = {row["failure_reason"] for row in table.to_pylist() if row["outcome"] == "NOT_ASSESSED"}
        self.assertEqual(reasons, {"cancelled before the engine reached this image"})
        # No engine process of this job survives the cancellation (other jobs may run on the host).
        import subprocess

        job_dir = str(Path(finished["parquet_path"]).parent)
        remaining = subprocess.run(["pgrep", "-f", f"OFIQSampleApp -c .* -i {job_dir}/"], capture_output=True, text=True)
        self.assertEqual(remaining.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
