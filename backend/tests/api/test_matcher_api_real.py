"""QS-C22/C23/C24 API on real data: matrix over the fixture pairs, metrics, comparison and error drilldown."""

from __future__ import annotations

import json
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
        self.assertEqual(sum(1 for item in listed if item["provenance_marker"] == "QUALSIGHT_EXECUTED"), 6)

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
        self.assertIn(analysis_id, [item["analysis_id"] for item in listed])

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

    def test_figures_dashboards_reports_exports_and_reproduction_bundles_on_the_real_fixture(self) -> None:
        """QS-F50–QS-F53: provenance-bearing figures, exact dashboard reload, classified report, traceable exports, bundle + reproduction states."""

        import zipfile

        job = self._quality_job()
        leaf = self.status["leaves"][0]
        # A utility analysis to draw from (recorded once per class by the utility test or here).
        started = self.client.post(f"/api/projects/{self.project_id}/utility-analyses", json={"matcher_run_id": leaf["run_id"], "quality_job_id": job["job_id"], "matcher_threshold": 0.8, "quality_threshold": 40.0, "bootstrap_replicates": 50, "seed": 11})
        self.assertIn(started.status_code, (200, 202), started.text)
        analysis_id = started.json()["analysis_id"]
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            status = self.client.get(f"/api/utility-analyses/{analysis_id}").json()
            if status["state"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.5)
        self.assertEqual(status["state"], "COMPLETED", status)
        # Figures: PNG/SVG/PDF/CSV + provenance sidecar; content-addressed; every series names its source Run.
        figures = {}
        for kind, params in (("det_overlay", {"matrix_id": self.matrix_id}), ("operating_points", {"matrix_id": self.matrix_id}), ("score_distributions", {"run_id": leaf["run_id"], "bins": 10}), ("erc_overlay", {"analysis_ids": [analysis_id]}), ("quality_conditioned_fnmr", {"analysis_id": analysis_id}), ("quality_histogram", {"job_id": job["job_id"], "bins": 10})):
            response = self.client.post(f"/api/projects/{self.project_id}/figures", json={"kind": kind, "parameters": params})
            self.assertEqual(response.status_code, 201, response.text)
            figures[kind] = response.json()
        det = figures["det_overlay"]
        self.assertEqual(len(det["source_run_ids"]), 6)
        for fmt in ("png", "svg", "pdf", "csv", "provenance"):
            file_response = self.client.get(det["files"][fmt])
            self.assertEqual(file_response.status_code, 200, fmt)
        svg = self.client.get(det["files"]["svg"]).text
        self.assertIn("<svg", svg)
        provenance = self.client.get(det["files"]["provenance"]).json()
        self.assertEqual(provenance["metric_definitions"][0], ["biometric.verification.det", "1.0.0"])
        self.assertTrue(all(t["kind"] == "MATCHER_THRESHOLD" for t in provenance["thresholds"]))
        again = self.client.post(f"/api/projects/{self.project_id}/figures", json={"kind": "det_overlay", "parameters": {"matrix_id": self.matrix_id}}).json()
        self.assertEqual(again["figure_id"], det["figure_id"])
        # Dashboard: saved layout reloads exactly with every panel's Run references.
        panels = [{"kind": "figure", "ref": det["figure_id"]}, {"kind": "figure", "ref": figures["erc_overlay"]["figure_id"]}, {"kind": "operating_points", "ref": leaf["run_id"]}, {"kind": "policy_table", "ref": analysis_id}]
        saved = self.client.post(f"/api/projects/{self.project_id}/dashboards", json={"name": "fixture dashboard", "template": "FIQA Evaluation", "columns": 2, "panels": panels})
        self.assertEqual(saved.status_code, 201, saved.text)
        loaded = self.client.get(f"/api/dashboards/{saved.json()['dashboard_id']}").json()
        self.assertEqual(loaded["layout"], saved.json()["layout"])
        self.assertEqual([p["kind"] for p in loaded["panels"]], [p["kind"] for p in panels])
        self.assertTrue(all(p["run_ids"] for p in loaded["panels"]))
        self.assertIn(leaf["run_id"], loaded["panels"][2]["run_ids"])
        # Report: HTML + PDF + JSON; failure examples with images only under the permitted policy (LFW is PRIVATE), never for RESTRICTED.
        spec = {"title": "fixture report", "executive_summary": "gate report over the fixture pairs", "dataset_id": self.dataset_id, "protocol_id": self.protocol_id, "quality_job_id": job["job_id"], "matrix_id": self.matrix_id, "utility_analysis_ids": [analysis_id],
                "figure_ids": [det["figure_id"], figures["erc_overlay"]["figure_id"]], "failure_examples": {"analysis_id": analysis_id, "limit": 3}, "image_policy": "EXCLUDE"}
        report = self.client.post(f"/api/projects/{self.project_id}/reports", json={"spec": spec})
        self.assertEqual(report.status_code, 201, report.text)
        body = report.json()
        self.assertEqual(body["images_embedded"], 0)
        html_text = self.client.get(body["links"]["html"]).text
        self.assertIn("<svg", html_text)
        self.assertIn("MATCHER THRESHOLD", html_text)
        self.assertIn(leaf["run_id"], html_text)
        pdf = self.client.get(body["links"]["pdf"])
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        permitted = self.client.post(f"/api/projects/{self.project_id}/reports", json={"spec": {**spec, "image_policy": "INCLUDE_PERMITTED"}}).json()
        self.assertGreaterEqual(permitted["images_embedded"] + permitted["images_withheld"], 0)
        self.assertEqual(permitted["provenance"]["image_policy"], "INCLUDE_PERMITTED")
        self.assertTrue(permitted["provenance"]["runs"] and all(run["evidence_sha256"] for run in permitted["provenance"]["runs"]))
        # Exports carry run identifiers and classification headers; every category and format serves a file.
        for category, ref in (("quality_results", job["job_id"]), ("comparison_records", leaf["run_id"]), ("embeddings", leaf["run_id"]), ("metric_tables", leaf["run_id"]), ("statistical_results", analysis_id), ("annotations", self.project_id)):
            for fmt in ("csv", "json", "parquet"):
                response = self.client.get(f"/api/exports/{category}", params={"ref": ref, "format": fmt})
                self.assertEqual(response.status_code, 200, (category, fmt, response.text[:200]))
                self.assertIn(response.headers["X-QualSight-Classification"], {"SENSITIVE_BIOMETRIC_DERIVED", "PRIVATE"})
        embeddings = self.client.get("/api/exports/embeddings", params={"ref": leaf["run_id"], "format": "json"}).json()
        self.assertTrue(all(row["run_id"] == leaf["run_id"] and len(row["embedding"]) == 512 for row in embeddings["rows"]))
        # Bundle: portable package with checksums and no image bytes; reproduction re-executes the analysis and reports EXACT.
        run_id = status["summary"]["run_id"]
        bundle = self.client.post(f"/api/projects/{self.project_id}/bundles", json={"run_id": run_id})
        self.assertEqual(bundle.status_code, 201, bundle.text)
        record = bundle.json()
        with zipfile.ZipFile(record["path"]) as archive:
            names = set(archive.namelist())
            self.assertTrue({"manifest.json", "experiment.yaml", "protocol.yaml", "dataset-manifest.json", "environment.json", "metrics.json", "results.parquet", "checksums.sha256", "README.md"} <= names, names)
            self.assertFalse(any(name.endswith((".jpg", ".jpeg", ".png")) and not name.startswith("figures/") for name in names))
        reproduced = self.client.post("/api/reproductions", json={"bundle_path": record["path"]})
        self.assertEqual(reproduced.status_code, 201, reproduced.text)
        self.assertEqual(reproduced.json()["state"], "EXACT", reproduced.json())
        self.assertGreater(reproduced.json()["compared_values"], 100)
        self.assertEqual([s["step"] for s in reproduced.json()["steps"]], ["validate-checksums", "validate-dependencies", "validate-model-versions", "validate-input-hashes", "execute-and-compare"])
        # A matcher-leaf bundle re-runs the matcher on the bundled pairs (fixture: ten pairs) and reproduces the recorded scores.
        leaf_bundle = self.client.post(f"/api/projects/{self.project_id}/bundles", json={"run_id": leaf["run_id"]}).json()
        leaf_reproduced = self.client.post("/api/reproductions", json={"bundle_path": leaf_bundle["path"]}).json()
        self.assertIn(leaf_reproduced["state"], {"EXACT", "WITHIN_TOLERANCE"}, leaf_reproduced)
        # A tampered bundle is BLOCKED at the checksum step; the target results are untouched.
        tampered = Path(record["path"]).with_name("tampered.qsexp")
        with zipfile.ZipFile(record["path"]) as source, zipfile.ZipFile(tampered, "w") as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == "metrics.json":
                    data = data.replace(b"1.0.0", b"9.9.9", 1)
                target.writestr(item, data)
        blocked = self.client.post("/api/reproductions", json={"bundle_path": str(tampered)}).json()
        self.assertEqual(blocked["state"], "BLOCKED")
        listed = self.client.get("/api/reproductions", params={"original_run_id": run_id}).json()
        self.assertGreaterEqual(len(listed), 2)

    def test_network_guard_refuses_non_loopback_and_admits_loopback_in_a_fresh_process(self) -> None:
        """QS-H70: the runtime network guard is real — checked in a subprocess so this test process stays unguarded."""

        import subprocess
        import sys

        code = (
            "import socket, json\n"
            "from qualsight.security.network_guard import NetworkGuardViolation, guard_status, install_network_guard\n"
            "install_network_guard()\n"
            "refused = False\n"
            "try:\n"
            "    socket.create_connection(('192.0.2.1', 9), timeout=1)\n"
            "except NetworkGuardViolation:\n"
            "    refused = True\n"
            "except OSError:\n"
            "    refused = False\n"
            "server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(1)\n"
            "client = socket.create_connection(server.getsockname(), timeout=2)\n"
            "client.close(); server.close()\n"
            "print(json.dumps({'refused': refused, 'status': guard_status()}))\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, cwd=str(ROOT / "backend"), env={**__import__("os").environ, "PYTHONPATH": "src:.."})
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(report["refused"], report)
        self.assertEqual(len(report["status"]["refused_attempts"]), 1)

    def test_zz_audit_attribution_and_verified_purge(self) -> None:
        """QS-H70/QS-H71: every meaningful action carries the configured user and a timestamp; purge removes and re-verifies derived artifacts and is itself audited."""

        import subprocess
        import sys

        health = self.client.get("/api/health").json()
        actor = health["actor_id"]
        self.assertTrue(actor)
        events = self.client.get(f"/api/projects/{self.project_id}/audit").json()
        types = {event["event_type"] for event in events}
        for expected in ("PROJECT_CREATED", "DATASET_IMPORTED", "RUN_STARTED", "RUN_COMPLETED", "ARTIFACT_EXPORTED", "ANNOTATION_MODIFIED", "CONFIGURATION_CHANGED"):
            self.assertIn(expected, types, sorted(types))
        self.assertTrue(all(event["actor_id"] == actor and event["occurred_at"] for event in events))
        subjects = {event["subject_type"] for event in events}
        for expected_subject in ("Figure", "Report", "Bundle", "Dashboard", "Reproduction", "MatcherMatrix", "QualityBatchJob"):
            self.assertIn(expected_subject, subjects, sorted(subjects))
        # Local data controls audit over this test's data directory finds no secrets and the sanctioned network posture.
        audit = subprocess.run([sys.executable, str(ROOT / "scripts" / "audit_local_data_controls.py"), "--data-dir", str(self.data_dir)], capture_output=True, text=True, check=False)
        self.assertEqual(audit.returncode, 0, audit.stdout[-800:] + audit.stderr[-400:])
        # Purge: scope is listed, confirmation is required, files are deleted and re-verified, records remain as tombstones, and the action is audited.
        scope = self.client.get(f"/api/projects/{self.project_id}/purge-scope").json()
        self.assertGreater(len(scope["targets"]["files"]) + len(scope["targets"]["directories"]), 5)
        self.assertIn("not scrubbed", scope["scope"])
        self.assertEqual(self.client.post(f"/api/projects/{self.project_id}/purge", json={"confirm": "wrong"}).status_code, 409)
        before = [path for path in scope["targets"]["files"] + scope["targets"]["directories"] if Path(path).exists()]
        self.assertTrue(before)
        purged = self.client.post(f"/api/projects/{self.project_id}/purge", json={"confirm": self.project_id})
        self.assertEqual(purged.status_code, 201, purged.text)
        record = purged.json()
        self.assertTrue(record["verified"], record)
        self.assertEqual(record["remaining_after_purge"], [])
        self.assertFalse(any(Path(path).exists() for path in before))
        self.assertIsNotNone(self.client.get(f"/api/runs/{self.status['leaves'][0]['run_id']}").json()["run"])  # tombstone metadata remains
        purges = self.client.get(f"/api/projects/{self.project_id}/purges").json()
        self.assertEqual(purges[-1]["purge_id"], record["purge_id"])
        after = self.client.get(f"/api/projects/{self.project_id}/audit").json()
        self.assertTrue(any(event["event_type"] == "DATASET_PURGED" and event["actor_id"] == actor for event in after))

    def test_external_score_artifact_imports_as_an_externally_computed_run_without_row_invention(self) -> None:
        """QS-C21: the registered option-A artifact (insightface buffalo_l, computed outside QualSight) maps to the frozen protocol; refusals are typed; the Run carries the marker."""

        resources = self.client.get("/api/resources").json()
        external = [item for item in resources if item["kind"] == "EXTERNAL_SCORE_ARTIFACT"]
        self.assertEqual(len(external), 1, external)
        resource_id = external[0]["resource_id"]
        self.assertEqual(self.client.post(f"/api/projects/{self.project_id}/protocols/{self.protocol_id}/import-external-scores", json={"resource_id": "lfw-funneled-local-candidate"}).status_code, 409)
        imported = self.client.post(f"/api/projects/{self.project_id}/protocols/{self.protocol_id}/import-external-scores", json={"resource_id": resource_id})
        self.assertEqual(imported.status_code, 201, imported.text)
        leaf = imported.json()
        self.assertEqual(leaf["leaf_id"], "QS-C21.EXTERNAL")
        self.assertEqual(leaf["provenance_marker"], "EXTERNALLY_COMPUTED")
        self.assertEqual(leaf["condition_id"], "EXTERNALLY_COMPUTED")
        self.assertEqual(leaf["matcher_id"], resource_id)
        self.assertEqual(leaf["comparison_count"], 6000)
        self.assertEqual(leaf["mapping"]["artifact_rows"], 6000)
        self.assertEqual(leaf["mapping"]["mapped_pairs"], 6000)
        self.assertEqual(leaf["scored_count"] + leaf["failed_count"], 6000)
        self.assertGreater(leaf["scored_count"], 5900)
        self.assertIn("EXCLUDE_FAILED_TRIALS", leaf["eer"])
        run = self.client.get(leaf["links"]["run"]).json()["run"]
        self.assertEqual(run["matcher"]["component_id"], resource_id)
        self.assertEqual(run["matcher_preprocessing"]["component_id"], "EXTERNALLY_COMPUTED")
        self.assertTrue(any("EXTERNALLY COMPUTED" in limit for limit in run["provenance_limits"]))
        self.assertIsNone(run["quality_engine"])
        self.assertTrue(any(h["path"].endswith("external_scores.csv") for h in run["model_hashes"]))
        # The imported Run behaves like any other leaf: comparisons resolve to real pairs and metrics are served.
        comparisons = self.client.get(leaf["links"]["comparisons"], params={"limit": 5}).json()
        self.assertEqual(comparisons["total"], 6000)
        self.assertEqual(self.client.get(leaf["links"]["metrics"]).json()["metrics"]["counts"]["trials"], 6000)
        # Importing the same artifact twice into the same project/protocol is refused (no duplicate Runs).
        self.assertEqual(self.client.post(f"/api/projects/{self.project_id}/protocols/{self.protocol_id}/import-external-scores", json={"resource_id": resource_id}).status_code, 409)
        # The external leaf is listed with its marker and does not enter the QualSight-executed matrix.
        listed = self.client.get(f"/api/projects/{self.project_id}/matcher-leaves").json()
        markers = {item["leaf_id"]: item["provenance_marker"] for item in listed}
        self.assertEqual(markers["QS-C21.EXTERNAL"], "EXTERNALLY_COMPUTED")
        self.assertEqual(markers["QS-C22.AS"], "QUALSIGHT_EXECUTED")
        self.assertNotEqual(leaf["matrix_id"], self.matrix_id)

    def test_identification_protocol_and_open_set_search_on_the_real_fixture(self) -> None:
        """QS-X60: a frozen 1:N protocol over the fixture subjects and an open-set search Run with registered identification definitions."""

        fixture = json.loads((ROOT / "resources" / "manifests" / "lfw-subset-fixture-v1.json").read_text(encoding="utf-8"))
        subjects: dict[str, list[str]] = {}
        for image in fixture["images"]:
            subjects.setdefault(image["path"].split("/")[0], []).append(image["path"])
        expected_gallery = sum(1 for paths in subjects.values() if len(paths) >= 2)
        expected_mated = sum(len(paths) - 1 for paths in subjects.values() if len(paths) >= 2)
        expected_nonmated = sum(1 for paths in subjects.values() if len(paths) == 1)
        frozen = self.client.post(f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/protocols/freeze-lfw-identification", json={"selection": "subset_fixture", "rank_depth": 20})
        self.assertEqual(frozen.status_code, 201, frozen.text)
        protocol = frozen.json()
        self.assertEqual(protocol["mode"], "OPEN_SET")
        self.assertEqual(protocol["gallery_count"], expected_gallery)
        self.assertEqual(protocol["mated_probe_count"], expected_mated)
        self.assertEqual(protocol["nonmated_probe_count"], expected_nonmated)
        self.assertEqual(protocol["rank_depth"], 20)
        started = self.client.post(f"/api/projects/{self.project_id}/identification-protocols/{protocol['protocol_id']}/search", json={"matcher_id": "adaface-ir101-local-candidate", "condition_id": "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1"})
        self.assertEqual(started.status_code, 202, started.text)
        search_id = started.json()["search_id"]
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            status = self.client.get(f"/api/identification-searches/{search_id}").json()
            if status["state"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(1)
        self.assertEqual(status["state"], "COMPLETED", status)
        metrics = status["metrics"]
        self.assertEqual(metrics["definitions"]["fnir"], ["biometric.identification.fnir", "1.0.0"])
        self.assertEqual(metrics["definitions"]["cmc"], ["biometric.identification.cmc", "1.0.0"])
        self.assertEqual(metrics["counts"]["gallery_templates"] + metrics["counts"]["gallery_template_failures"], expected_gallery)
        self.assertEqual(metrics["counts"]["mated_searches"], expected_mated)
        self.assertEqual(metrics["counts"]["nonmated_searches"], expected_nonmated)
        closed = metrics["closed_set"]["EXCLUDE_FAILED_TRIALS"]
        available = [k for k in (1, 5, 10, 20, 100) if k <= min(20, expected_gallery)]
        self.assertEqual(sorted(int(k) for k in closed["rank_k"]), available)
        ranks = [closed["rank_k"][str(k)]["rate"] for k in available]
        self.assertTrue(all(0.0 <= r <= 1.0 for r in ranks if r is not None))
        cmc = closed["cmc"]["points"]
        self.assertTrue(all(cmc[i]["rate"] <= cmc[i + 1]["rate"] for i in range(len(cmc) - 1)))
        self.assertIn("ci_lower", closed["rank_k"]["1"])
        open_set = metrics["open_set"]["EXCLUDE_FAILED_TRIALS"]
        self.assertEqual([p["target_fpir"] for p in open_set["operating_points"]], [0.1, 0.01, 0.001])
        for point in open_set["operating_points"]:
            if point["threshold"] is None:
                self.assertIn("reason", point)  # a small non-mated population cannot reach the target FPIR; said so, not invented
            else:
                self.assertIn("fnir", point); self.assertIn("tpir", point); self.assertIn("fnir_ci95", point)
        self.assertIn("true_mate_rank_distribution", closed)
        self.assertTrue(metrics["gallery_effects"]["gallery_sizes"])
        # The Run is immutable, GPU-embedded by the frozen matcher, and lists candidates that resolve to real samples.
        run = self.client.get(f"/api/runs/{status['run_id']}").json()["run"]
        self.assertEqual(run["matcher"]["component_id"], "adaface-ir101-local-candidate")
        self.assertEqual(run["pair_search_protocol"]["component_id"], protocol["protocol_id"])
        candidates = self.client.get(f"/api/identification-searches/{search_id}/candidates", params={"limit": 1000}).json()
        self.assertGreater(candidates["total"], 0)
        self.assertTrue(all(row["rank"] <= 20 for row in candidates["rows"]))
        self.assertEqual(self.client.get(candidates["rows"][0]["links"]["probe_sample"]).status_code, 200)
        # Independent recomputation of rank-1 identification and FNIR at the recorded FPIR-0.1 threshold from the candidate lists.
        by_probe: dict[str, list[dict]] = {}
        for row in candidates["rows"]:
            by_probe.setdefault(row["probe_sample_id"], []).append(row)
        mated_probes = [rows for rows in by_probe.values() if rows[0]["probe_kind"] == "MATED"]
        rank1 = sum(1 for rows in mated_probes if any(r["rank"] == 1 and r["is_true_mate"] for r in rows)) / len(mated_probes)
        self.assertAlmostEqual(closed["rank_k"]["1"]["rate"], rank1, places=12)
        point = open_set["operating_points"][0]
        if point.get("threshold") is not None:
            misses = sum(1 for rows in mated_probes if not any(r["is_true_mate"] and r["score"] >= point["threshold"] for r in rows))
            self.assertAlmostEqual(point["fnir"], misses / len(mated_probes), places=12)
        # Metrics survive the store round trip and the search is listed for the project.
        listed = self.client.get(f"/api/projects/{self.project_id}/identification-searches").json()
        self.assertIn(search_id, [item["search_id"] for item in listed])

    def test_controlled_transformations_of_real_inputs_carry_full_provenance_and_feed_degradation_analysis(self) -> None:
        """QS-X61: derived images from registered real inputs (JPEG, blur, downscale) with complete chains; OFIQ and the frozen matcher respond per degradation level."""

        spec = {"selection": "subset_fixture", "transforms": [{"kind": "jpeg", "parameters": [90, 50, 20]}, {"kind": "gaussian_blur", "parameters": [1.0, 2.0]}, {"kind": "downscale", "parameters": [0.5]}]}
        created = self.client.post(f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/transformations", json=spec)
        self.assertEqual(created.status_code, 201, created.text)
        derived = created.json()
        self.assertEqual(derived["source_count"], 16)
        self.assertEqual(derived["derived_count"], 16 * 6)
        self.assertEqual(derived["levels"], 6)
        listing = self.client.get(f"/api/derived-sets/{derived['derived_set_id']}").json()
        self.assertEqual(len(listing["samples"]), 96)
        first = listing["samples"][0]
        self.assertTrue(first["parent_sample_id"].startswith("sample:sha256:"))
        self.assertEqual(first["chain"][0]["root_resource_id"], "lfw-funneled-local-candidate")
        self.assertIn("kind", first["chain"][-1]); self.assertIn("parameters", first["chain"][-1]); self.assertIn("version", first["chain"][-1])
        self.assertTrue(Path(first["path"]).exists())
        self.assertEqual(self.client.get(f"/api/samples/{first['parent_sample_id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/samples/{first['derived_sample_id']}").status_code, 200)
        # OFIQ runs over the derived dataset through the registered engine (the batch engine admits derived samples whose chain roots at a registered input).
        started = self.client.post(f"/api/projects/{self.project_id}/datasets/{derived['derived_dataset_id']}/quality-batch", json={"selection": {"kind": "all"}, "chunks": 1})
        self.assertEqual(started.status_code, 202, started.text)
        job_id = started.json()["job_id"]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            job = self.client.get(f"/api/jobs/{job_id}").json()
            if job["ended_at"]:
                break
            time.sleep(0.5)
        self.assertEqual(job["state"], "COMPLETED", job)
        self.assertEqual(job["assessed"], 96)
        # Degradation analysis: quality response, matcher score/FNMR response, slope, area, threshold crossing — recorded as a Run.
        analysis = self.client.post(f"/api/projects/{self.project_id}/degradation-analyses", json={"derived_set_id": derived["derived_set_id"], "quality_job_id": job_id, "baseline_quality_job_id": self._quality_job()["job_id"], "matcher_id": "adaface-ir101-local-candidate", "condition_id": "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1", "matcher_threshold": 0.8})
        self.assertEqual(analysis.status_code, 202, analysis.text)
        analysis_id = analysis.json()["analysis_id"]
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            status = self.client.get(f"/api/degradation-analyses/{analysis_id}").json()
            if status["state"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(1)
        self.assertEqual(status["state"], "COMPLETED", status)
        document = status["document"]
        self.assertEqual({r["transform"] for r in document["responses"]}, {"jpeg", "gaussian_blur", "downscale"})
        jpeg = next(r for r in document["responses"] if r["transform"] == "jpeg")
        self.assertEqual([lvl["parameter"] for lvl in jpeg["levels"]], [90, 50, 20])
        for level in jpeg["levels"]:
            self.assertIn("quality", level); self.assertIn("UnifiedQualityScore", level["quality"])
            self.assertIn("mated_score_mean", level); self.assertIn("fnmr", level); self.assertIn("fmr", level)
            self.assertGreaterEqual(level["mated_pairs"], 5)  # every recorded protocol pair whose left image is a source, not only the fixture's own five
            self.assertEqual(len(level["pair_scores"]), level["mated_pairs"] + level["nonmated_pairs"])
            self.assertTrue(all(ps["derived_sample_id"] for ps in level["pair_scores"]))
        self.assertIn("baseline", jpeg)
        self.assertIn("area_under_response", jpeg["derived_summaries"]["fnmr"])
        self.assertIn("threshold_crossing", jpeg["derived_summaries"]["fnmr"])
        run = self.client.get(f"/api/runs/{status['run_id']}").json()["run"]
        self.assertEqual(run["matcher"]["component_id"], "adaface-ir101-local-candidate")
        self.assertIsNotNone(run["quality_engine"])
        self.assertTrue(any("derived" in limit.lower() or "transform" in limit.lower() for limit in run["provenance_limits"]))

    def test_container_execution_adapter_runs_the_registered_ofiq_runtime_in_an_immutable_container(self) -> None:
        """QS-X64: the batch engine can execute the registered OFIQ runtime inside a network-less container built from registered bytes; results equal the host adapter's."""

        import pyarrow.parquet as pq

        host_job = self._quality_job()
        started = self.client.post(f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/quality-batch", json={"selection": {"kind": "subset_fixture"}, "chunks": 1, "execution": "container"})
        self.assertEqual(started.status_code, 202, started.text)
        job_id = started.json()["job_id"]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            job = self.client.get(f"/api/jobs/{job_id}").json()
            if job["ended_at"]:
                break
            time.sleep(0.5)
        self.assertEqual(job["state"], "COMPLETED", job)
        self.assertEqual(job["assessed"], 16)
        run = self.client.get(f"/api/runs/{job['run_id']}").json()["run"]
        configuration = json.loads(run["configuration"]["text"]) if isinstance(run["configuration"], dict) and "text" in run["configuration"] else run["configuration"]
        environment = configuration.get("execution_environment") or {}
        self.assertEqual(environment.get("kind"), "CONTAINER")
        self.assertTrue(str(environment.get("image_id", "")).startswith("sha256:"))
        self.assertEqual(environment.get("network"), "none")
        self.assertTrue(environment.get("rootfs_manifest_sha256"))
        # Same measures as the host adapter over the same samples.
        host = {r["sample_id"]: r for r in pq.read_table(host_job["parquet_path"]).to_pylist()}
        container = {r["sample_id"]: r for r in pq.read_table(job["parquet_path"]).to_pylist()}
        self.assertEqual(set(host), set(container))
        measure_columns = [c for c in next(iter(host.values())) if c.endswith(".native") or c.endswith(".scalar") or c.endswith(".failed")]
        for sample_id, row in host.items():
            for column in measure_columns:
                self.assertEqual(row[column], container[sample_id][column], (sample_id, column))


if __name__ == "__main__":
    unittest.main()
