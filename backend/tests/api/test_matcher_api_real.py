"""QS-C22/C23/C24 API on real data: matrix over the fixture pairs, metrics, comparison and error drilldown."""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID


ROOT = Path(__file__).resolve().parents[3]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class RealMatcherApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-matcher-api-"))
        settings = Settings(repository_root=ROOT, registry_path=ROOT / "resources" / "resource-registry.json", data_dir=cls.data_dir,
                            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID, input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID))
        cls.client = TestClient(create_app(settings))
        cls.client.__enter__()
        project = cls.client.post("/api/projects", json={"name": "matcher api gate", "description": "QS-C22 API.", "purpose_scope": RESEARCH_SCOPE}).json()
        cls.project_id = project["project_id"]
        cls.client.post(f"/api/projects/{cls.project_id}/open")
        cls.dataset_id = cls.client.post(f"/api/projects/{cls.project_id}/datasets/import-registered", json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False}).json()["dataset_id"]
        cls.protocol_id = cls.client.post(f"/api/projects/{cls.project_id}/datasets/{cls.dataset_id}/protocols/freeze-lfw-view2").json()["protocol_id"]
        started = cls.client.post(f"/api/projects/{cls.project_id}/protocols/{cls.protocol_id}/matrix", params={"fixture_pairs_only": "true"})
        assert started.status_code == 202, started.text
        cls.matrix_id = started.json()["matrix_id"]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            status = cls.client.get(f"/api/matrices/{cls.matrix_id}").json()
            if status["progress"]["phase"] in {"completed", "failed"}:
                break
            time.sleep(1)
        cls.status = status

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def test_matrix_records_six_leaves_with_separate_identities(self) -> None:
        self.assertEqual(self.status["progress"]["phase"], "completed", self.status)
        leaves = self.status["leaves"]
        self.assertEqual(len(leaves), 6)
        self.assertEqual({leaf["leaf_id"] for leaf in leaves}, {"QS-C22.AS", "QS-C22.AN", "QS-C22.ES", "QS-C22.EN", "QS-C22.TS", "QS-C22.TN"})
        self.assertEqual({leaf["condition_id"] for leaf in leaves}, {"SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1", "SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1"})
        for leaf in leaves:
            self.assertEqual(leaf["comparison_count"], 10)
            self.assertEqual(leaf["scored_count"] + leaf["failed_count"], 10)
            self.assertIn("EXCLUDE_FAILED_TRIALS", leaf["eer"])
            self.assertIn("FAILURES_AS_REJECT", leaf["eer"])
            run = self.client.get(leaf["links"]["run"])
            self.assertEqual(run.status_code, 200)
            self.assertIsNone(run.json()["run"]["quality_engine"])
            self.assertEqual(run.json()["run"]["matcher"]["component_id"], leaf["matcher_id"])
        listed = self.client.get(f"/api/projects/{self.project_id}/matcher-leaves").json()
        self.assertEqual(len(listed), 6)

    def test_metrics_comparisons_and_errors_resolve_to_real_pairs_and_samples(self) -> None:
        leaf = self.status["leaves"][0]
        metrics = self.client.get(leaf["links"]["metrics"]).json()
        self.assertEqual(metrics["metrics"]["counts"]["trials"], 10)
        self.assertEqual(metrics["metrics"]["definitions"]["eer"], ["biometric.verification.eer", "1.0.0"])
        comparisons = self.client.get(leaf["links"]["comparisons"], params={"limit": 100}).json()
        self.assertEqual(comparisons["total"], 10)
        for row in comparisons["rows"]:
            self.assertEqual((row["score"] is None), (row["failure"] is not None))
            left = self.client.get(row["links"]["left_sample"])
            self.assertEqual(left.status_code, 200)
            self.assertEqual(left.json()["sample_id"], row["left_sample_id"])
            image = self.client.get(row["links"]["left_image"])
            self.assertEqual(image.status_code, 200)
        mated = self.client.get(leaf["links"]["comparisons"], params={"label": "MATED"}).json()
        self.assertEqual(mated["total"], 5)
        errors = self.client.get(leaf["links"]["errors"]).json()
        self.assertEqual(errors["leaf_id"], leaf["leaf_id"])
        self.assertEqual(errors["threshold_equality_rule"], "ACCEPT_WHEN_SCORE_GE_THRESHOLD")
        self.assertIsNotNone(errors["transform_id"])
        # On the fixture pairs the six matchers separate mated from non-mated: no errors at the EER threshold.
        self.assertEqual(errors["counts"]["false_matches"], 0)
        self.assertEqual(errors["counts"]["false_non_matches"], 0)
        # A stricter threshold produces real false non-matches that resolve to their samples.
        strict = self.client.get(leaf["links"]["errors"], params={"threshold": 0.9}).json()
        self.assertGreater(strict["counts"]["false_non_matches"], 0)
        first = strict["false_non_matches"][0]
        self.assertEqual(first["label"], "MATED")
        self.assertLess(first["score"], 0.9)
        self.assertEqual(self.client.get(first["links"]["right_sample"]).status_code, 200)

    @classmethod
    def _quality_job(cls) -> dict:
        """One real quality job over the fixture images (UnifiedQualityScore per sample), started once per class."""

        cached = getattr(cls, "_quality_job_cache", None)
        if cached is not None:
            return cached
        started = cls.client.post(f"/api/projects/{cls.project_id}/datasets/{cls.dataset_id}/quality-batch", json={"selection": {"kind": "subset_fixture"}, "chunks": 1})
        assert started.status_code == 202, started.text
        job_id = started.json()["job_id"]
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            job = cls.client.get(f"/api/jobs/{job_id}").json()
            if job["ended_at"]:
                break
            time.sleep(0.5)
        assert job["state"] == "COMPLETED", job
        cls._quality_job_cache = job
        return job

    def test_erc_reconciles_to_an_independent_recomputation_over_real_quality_and_scores(self) -> None:
        import numpy as np
        import pyarrow.parquet as pq

        job = self._quality_job()
        job_id = job["job_id"]
        leaf = self.status["leaves"][0]
        response = self.client.get(f"/api/matcher-runs/{leaf['run_id']}/erc", params={"quality_job_id": job_id, "policy": "pair.quality.min", "threshold": 0.8})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        erc = body["erc"]
        self.assertEqual(erc["definition"], ["biometric.quality.utility.erc", "1.0.0"])
        self.assertEqual(erc["pair_quality_policy"]["id"], "pair.quality.min")
        self.assertEqual(erc["fixed_matcher_threshold"]["value"], 0.8)
        fnmr = erc["curves"]["FNMR"]
        self.assertEqual(fnmr["comparison_units"], 5)
        # Independent recomputation: min UQS per mated pair, worst rejected first, FNMR at 0.8.
        quality = {r["sample_id"]: r["UnifiedQualityScore.scalar"] for r in pq.read_table(job["parquet_path"]).to_pylist()}
        comparisons = self.client.get(leaf["links"]["comparisons"], params={"label": "MATED", "limit": 100}).json()["rows"]
        units = sorted(((min(quality[r["left_sample_id"]], quality[r["right_sample_id"]]), r["score"]) for r in comparisons), key=lambda item: item[0])
        n = len(units)
        expected_first = sum(1 for _, score in units if score < 0.8) / n
        self.assertEqual(fnmr["points"][0]["rejected_fraction"], 0.0)
        self.assertAlmostEqual(fnmr["points"][0]["error_rate"], expected_first, places=12)
        for point in fnmr["points"]:
            if point["error_rate"] is None:
                continue
            start = round(point["rejected_fraction"] * n)
            retained = units[start:]
            self.assertAlmostEqual(point["error_rate"], sum(1 for _, score in retained if score < 0.8) / len(retained), places=12)
        # FMR-ERC is reported separately over the non-mated units, never pooled.
        self.assertEqual(erc["curves"]["FMR"]["comparison_units"], 5)
        self.assertIn("metric registry v1.1.0", erc["derived_summaries"]["FNMR"]["note"])
        unknown_policy = self.client.get(f"/api/matcher-runs/{leaf['run_id']}/erc", params={"quality_job_id": job_id, "policy": "pair.quality.nope"})
        self.assertEqual(unknown_policy.status_code, 422)

    def test_unknown_run_is_a_typed_404(self) -> None:
        self.assertEqual(self.client.get("/api/matcher-runs/run:sha256:" + "0" * 64 + "/metrics").status_code, 404)
        self.assertEqual(self.client.get("/api/matrices/matrix:sha256:" + "0" * 64).status_code, 404)



    def test_utility_analysis_run_is_recorded_traceable_and_independently_verified(self) -> None:
        """QS-D30–QS-D33 on the real fixture: policies registry, analysis Run, exact policy reconciliation, unsupported-precision flags."""

        import subprocess
        import sys

        policies = self.client.get("/api/pair-quality-policies").json()
        self.assertEqual({p["id"] for p in policies["policies"]}, {"pair.quality.min", "pair.quality.max", "pair.quality.mean", "pair.quality.geometric_mean", "pair.quality.enrollment", "pair.quality.probe"})
        self.assertTrue(all(p["version"] == "1.0.0" and p["definition"] for p in policies["policies"]))
        job = self._quality_job()
        leaf = self.status["leaves"][0]
        body = {"matcher_run_id": leaf["run_id"], "quality_job_id": job["job_id"], "pair_quality_policy": "pair.quality.min", "matcher_threshold": 0.8, "quality_threshold": 40.0, "bootstrap_replicates": 200, "seed": 7}
        started = self.client.post(f"/api/projects/{self.project_id}/utility-analyses", json=body)
        self.assertEqual(started.status_code, 202, started.text)
        analysis_id = started.json()["analysis_id"]
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            status = self.client.get(f"/api/utility-analyses/{analysis_id}").json()
            if status["state"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.5)
        self.assertEqual(status["state"], "COMPLETED", status)
        summary = status["summary"]
        self.assertEqual(summary["quality_measure"], "UnifiedQualityScore")
        self.assertEqual(summary["matcher_threshold"], 0.8)
        self.assertEqual(summary["quality_threshold"], 40.0)
        # The analysis is an immutable Run binding both the matcher and the quality engine of its sources.
        run = self.client.get(summary["links"]["run"]).json()["run"]
        self.assertEqual(run["matcher"]["component_id"], leaf["matcher_id"])
        self.assertIsNotNone(run["quality_engine"])
        self.assertEqual(run["random_seeds"], [7])
        self.assertTrue(any(m["metric_id"].startswith("biometric.quality.utility.au-erc@1.0.0[FNMR]") for m in run["metrics"]))
        self.assertEqual(len(run["artifacts"]), 3)
        # Threshold kinds are never conflated.
        policy = status["policy"]
        self.assertEqual(policy["thresholds"]["matcher"]["kind"], "MATCHER_THRESHOLD")
        self.assertEqual(policy["thresholds"]["quality_policy"]["kind"], "QUALITY_POLICY_THRESHOLD")
        self.assertFalse(policy["thresholds"]["matcher"]["reestimated_during_policy"])
        # Exact reconciliation of the policy block against the pair table served by the API.
        pairs = self.client.get(summary["links"]["pairs"], params={"limit": 1000}).json()
        self.assertEqual(pairs["total"], 10)
        usable = [r for r in pairs["rows"] if r["failure"] is None and r["pair_quality"] is not None]
        rejected = [r for r in usable if r["pair_quality"] < 40.0]
        self.assertEqual(policy["counts"]["rejected"], len(rejected))
        self.assertEqual(policy["impact"]["false_non_matches_prevented"], sum(1 for r in rejected if r["label"] == "MATED" and r["score"] < 0.8))
        self.assertEqual(policy["impact"]["successful_comparisons_removed"], sum(1 for r in rejected if (r["label"] == "MATED") == (r["score"] >= 0.8)))
        for row in pairs["rows"]:
            self.assertEqual(row["retained_by_quality_policy"], None if row["pair_quality"] is None else row["pair_quality"] >= 40.0)
            self.assertEqual(self.client.get(row["links"]["left_quality"]).status_code, 200)
        # Five mated units cannot support a logistic model or credible bins; the analysis says so instead of printing precision.
        utility = status["utility"]["utility"]
        self.assertEqual(utility["association"]["FNMR"]["logistic_error_on_quality"]["status"], "UNSUPPORTED_BY_SAMPLE")
        self.assertTrue(all(not b["supported"] for b in utility["quality_conditioned"]["FNMR"]["bins"]))
        self.assertEqual(utility["uncertainty"]["replicates"], 200)
        # Independent recomputation script over the recorded artifacts.
        row_directory = None
        for artifact in run["artifacts"]:
            if artifact["uri"].endswith("utility.json"):
                row_directory = Path(artifact["uri"]).parent
        self.assertIsNotNone(row_directory)
        verified = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_utility_analysis.py"), str(row_directory)], capture_output=True, text=True, check=False)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertIn("UTILITY_ANALYSIS_VERIFIED", verified.stdout)
        # Re-posting the same request returns the recorded analysis instead of recomputing.
        again = self.client.post(f"/api/projects/{self.project_id}/utility-analyses", json=body)
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()["already_recorded"])
        listed = self.client.get(f"/api/projects/{self.project_id}/utility-analyses").json()
        self.assertEqual([item["analysis_id"] for item in listed], [analysis_id])

    def test_cross_matcher_contrasts_hold_identities_fixed_and_resolve_disagreements_to_pairs(self) -> None:
        """QS-E40–QS-E42 on the real fixture: matcher sets, nine contrasts, overlap counts verified, workbench pairs and annotations."""

        import subprocess
        import sys

        sets = self.client.get(f"/api/matrices/{self.matrix_id}/sets").json()
        self.assertTrue(sets["same_protocol"])
        self.assertEqual(sets["matcher_contrasts_available"], 6)
        self.assertEqual(sets["preprocessing_contrasts_available"], 3)
        self.assertEqual(set(sets["matcher_rights"].values()), {"APPROVED_LOCAL_RESEARCH_USE"})
        job = self._quality_job()
        body = {"matrix_id": self.matrix_id, "quality_job_id": job["job_id"], "bootstrap_replicates": 100, "seed": 3}
        started = self.client.post(f"/api/projects/{self.project_id}/cross-matcher", json=body)
        self.assertEqual(started.status_code, 202, started.text)
        analysis_id = started.json()["analysis_id"]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            status = self.client.get(f"/api/cross-matcher/{analysis_id}").json()
            if status["state"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.5)
        self.assertEqual(status["state"], "COMPLETED", status)
        document = status["document"]
        self.assertEqual(len(document["contrasts"]), 9)
        kinds = [c["kind"] for c in document["contrasts"]]
        self.assertEqual(kinds.count("MATCHER_CONTRAST_FIXED_CONDITION"), 6)
        self.assertEqual(kinds.count("PREPROCESSING_CONTRAST_FIXED_MATCHER"), 3)
        for contrast in document["contrasts"]:
            if contrast["kind"] == "MATCHER_CONTRAST_FIXED_CONDITION":
                self.assertEqual(contrast["a"]["condition_id"], contrast["b"]["condition_id"])
                self.assertNotEqual(contrast["a"]["matcher_id"], contrast["b"]["matcher_id"])
                self.assertEqual(contrast["held_fixed"], {"condition_id": contrast["a"]["condition_id"]})
            else:
                self.assertEqual(contrast["a"]["matcher_id"], contrast["b"]["matcher_id"])
                self.assertNotEqual(contrast["a"]["condition_id"], contrast["b"]["condition_id"])
            overlap = contrast["error_overlap"]["mated_false_non_match"]
            self.assertEqual(overlap["units"], overlap["NEITHER"] + overlap["A_ONLY"] + overlap["B_ONLY"] + overlap["BOTH"])
            self.assertEqual(contrast["a"]["threshold_provenance"]["kind"], "MATCHER_THRESHOLD")
            self.assertIn(contrast["quality_matcher_interaction"]["status"], {"UNSUPPORTED_BY_SAMPLE", "ESTIMATED", "NOT_CONVERGED_OR_SEPARATED"})
        # The analysis is a Run that binds every leaf's model bytes and the quality engine.
        run = self.client.get(status["summary"]["links"]["run"]).json()["run"]
        self.assertEqual(run["matcher"]["component_id"], "cross-matcher-set")
        self.assertGreaterEqual(len(run["model_hashes"]), 4)
        self.assertIsNotNone(run["quality_engine"])
        # Workbench: every pair of a contrast resolves to samples, both scores, both thresholds, quality values, and run links.
        contrast = document["contrasts"][0]
        pairs = self.client.get(status["summary"]["links"]["pairs"], params={"contrast_id": contrast["contrast_id"], "limit": 100}).json()
        self.assertEqual(pairs["total"], 10)
        self.assertEqual(pairs["contrast"]["a"]["run_id"], contrast["a"]["run_id"])
        for row in pairs["rows"]:
            self.assertIn(row["category"], {"NEITHER", "A_ONLY", "B_ONLY", "BOTH", "FAILURE_A", "FAILURE_B", "FAILURE_BOTH"})
            self.assertEqual(self.client.get(row["links"]["left_sample"]).status_code, 200)
            self.assertEqual(self.client.get(row["links"]["run_a"]).status_code, 200)
            self.assertEqual(self.client.get(row["links"]["run_b"]).status_code, 200)
        # Annotations are append-only records attached to a pair and echoed by the workbench.
        note = self.client.post("/api/annotations", json={"project_id": self.project_id, "subject_type": "PROTOCOL_PAIR", "subject_id": pairs["rows"][0]["pair_id"], "text": "reviewed in the QS-E42 gate", "context": {"contrast_id": contrast["contrast_id"], "analysis_id": analysis_id}})
        self.assertEqual(note.status_code, 201, note.text)
        listed = self.client.get("/api/annotations", params={"subject_id": pairs["rows"][0]["pair_id"]}).json()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["text"], "reviewed in the QS-E42 gate")
        again = self.client.get(status["summary"]["links"]["pairs"], params={"contrast_id": contrast["contrast_id"], "limit": 100}).json()
        self.assertEqual(len(again["rows"][0]["annotations"]), 1)
        # Independent recomputation of overlaps, Jaccard, transferred thresholds, and areas from the recorded artifacts.
        directory = None
        for artifact in run["artifacts"]:
            if artifact["uri"].endswith("crossmatcher.json"):
                directory = Path(artifact["uri"]).parent
        self.assertIsNotNone(directory)
        verified = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_cross_matcher.py"), str(directory), str(self.data_dir / "qualsight.sqlite3")], capture_output=True, text=True, check=False)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertIn("CROSS_MATCHER_VERIFIED", verified.stdout)
        # Re-posting returns the recorded analysis.
        self.assertTrue(self.client.post(f"/api/projects/{self.project_id}/cross-matcher", json=body).json()["already_recorded"])


if __name__ == "__main__":
    unittest.main()
