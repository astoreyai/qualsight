"""QS-C22/C23/C24 API on real data: matrix over the fixture pairs, metrics, comparison and error drilldown."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID
from qualsight.security.purge import purge_targets


ROOT = Path(__file__).resolve().parents[3]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class RealMatcherApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-matcher-api-"))
        cls.settings = Settings(repository_root=ROOT, registry_path=ROOT / "resources" / "resource-registry.json", data_dir=cls.data_dir,
                                runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID, input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID))
        cls.client = TestClient(create_app(cls.settings), base_url="http://127.0.0.1")
        cls.client.__enter__()
        project = cls.client.post("/api/projects", json={"name": "matcher api gate", "description": "QS-C22 API.", "purpose_scope": RESEARCH_SCOPE}).json()
        cls.project_id = project["project_id"]
        cls.client.post(f"/api/projects/{cls.project_id}/open")
        cls.dataset_id = cls.client.post(f"/api/projects/{cls.project_id}/datasets/import-registered", json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False}).json()["dataset_id"]
        cls.protocol_id = cls.client.post(f"/api/projects/{cls.project_id}/datasets/{cls.dataset_id}/protocols/freeze-lfw-view2").json()["protocol_id"]
        for matcher_id in (
            "adaface-ir101-local-candidate",
            "elasticface-arc-r100-local-candidate",
            "transface-s-glint360k-local-candidate",
        ):
            pulled = cls.client.post(f"/api/matcher-assets/{matcher_id}/pull-sources")
            assert pulled.status_code == 200, pulled.text
        started = cls.client.post(f"/api/projects/{cls.project_id}/protocols/{cls.protocol_id}/matrix", params={"fixture_pairs_only": "true"})
        assert started.status_code == 202, started.text
        cls.matrix_id = started.json()["matrix_id"]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            status = cls.client.get(f"/api/projects/{cls.project_id}/matrices/{cls.matrix_id}").json()
            if status["progress"]["phase"] in {"completed", "failed"}:
                break
            time.sleep(1)
        cls.status = status

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    @classmethod
    def _run_store(cls):
        route = next(
            route
            for route in cls.client.app.routes
            if getattr(route, "path", None) == "/api/projects/{project_id}/reports/{report_id}/file"
        )
        return inspect.getclosurevars(route.endpoint).nonlocals["store"]()

    @classmethod
    def _private_job(cls, job_id: str) -> dict:
        record = cls._run_store().job(job_id)
        assert record is not None, job_id
        return record

    @classmethod
    def _private_run(cls, run_id: str):
        record = cls._run_store().run(run_id)
        assert record is not None, run_id
        return record[0]

    def test_matrix_records_twelve_leaves_with_separate_identities(self) -> None:
        self.assertEqual(self.status["progress"]["phase"], "completed", self.status)
        leaves = self.status["leaves"]
        self.assertEqual(len(leaves), 12)
        self.assertEqual(
            {leaf["leaf_id"] for leaf in leaves},
            {
                "QS-C22.AS", "QS-C22.AN", "QS-C22.ES", "QS-C22.EN",
                "QS-C22.MS", "QS-C22.MN", "QS-C22.PS", "QS-C22.PN",
                "QS-C22.BS", "QS-C22.BN", "QS-C22.TS", "QS-C22.TN",
            },
        )
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
        self.assertEqual(
            sum(1 for item in listed if item["provenance_marker"] == "QUALSIGHT_EXECUTED" and item["matrix_id"] == self.matrix_id),
            12,
        )

    def test_catalog_leaf_selection_executes_each_preprocessing_profile_as_its_own_real_run(self) -> None:
        observed: dict[str, dict] = {}
        for leaf_id in ("QS-C22.AS", "QS-C22.AN"):
            started = self.client.post(
                f"/api/projects/{self.project_id}/protocols/{self.protocol_id}/matrix",
                params={"fixture_pairs_only": "true", "leaves": leaf_id},
            )
            self.assertEqual(started.status_code, 202, started.text)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                status = self.client.get(f"/api/projects/{self.project_id}/matrices/{started.json()['matrix_id']}").json()
                if status["progress"]["phase"] in {"completed", "failed"}:
                    break
                time.sleep(0.5)
            self.assertEqual(status["progress"]["phase"], "completed", status)
            self.assertEqual([leaf["leaf_id"] for leaf in status["leaves"]], [leaf_id])
            self.assertEqual(status["leaves"][0]["comparison_count"], 10)
            observed[leaf_id] = status["leaves"][0]

        self.assertEqual(
            {item["matcher_id"] for item in observed.values()},
            {"adaface-ir101-local-candidate"},
        )
        self.assertEqual(
            {item["condition_id"] for item in observed.values()},
            {
                "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1",
                "SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1",
            },
        )
        self.assertEqual(len({item["run_id"] for item in observed.values()}), 2)
        with TestClient(create_app(self.settings), base_url="http://127.0.0.1") as restarted:
            for item in observed.values():
                persisted = restarted.get(f"/api/projects/{self.project_id}/matrices/{item['matrix_id']}")
                self.assertEqual(persisted.status_code, 200, persisted.text)
                self.assertEqual(persisted.json()["progress"]["phase"], "completed", persisted.json())
                self.assertEqual(persisted.json()["progress"]["detail"], "1/1 leaves recorded")

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
            job = cls.client.get(
                f"/api/projects/{cls.project_id}/jobs/{job_id}"
            ).json()
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
        response = self.client.get(f"/api/projects/{self.project_id}/matcher-runs/{leaf['run_id']}/erc", params={"quality_job_id": job_id, "policy": "pair.quality.min", "threshold": 0.8})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        erc = body["erc"]
        self.assertEqual(erc["definition"], ["biometric.quality.utility.erc", "1.0.0"])
        self.assertEqual(erc["pair_quality_policy"]["id"], "pair.quality.min")
        self.assertEqual(erc["fixed_matcher_threshold"]["value"], 0.8)
        fnmr = erc["curves"]["FNMR"]
        self.assertEqual(fnmr["comparison_units"], 5)
        # Independent recomputation: min UQS per mated pair, worst rejected first, FNMR at 0.8.
        quality_path = self._private_job(job_id)["parquet_path"]
        quality = {r["sample_id"]: r["UnifiedQualityScore.scalar"] for r in pq.read_table(quality_path).to_pylist()}
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
        self.assertIn("metric registry v1.2.0", erc["derived_summaries"]["FNMR"]["note"])
        unknown_policy = self.client.get(f"/api/projects/{self.project_id}/matcher-runs/{leaf['run_id']}/erc", params={"quality_job_id": job_id, "policy": "pair.quality.nope"})
        self.assertEqual(unknown_policy.status_code, 422)

    def test_unknown_run_is_a_typed_404(self) -> None:
        self.assertEqual(self.client.get(f"/api/projects/{self.project_id}/matcher-runs/run:sha256:" + "0" * 64 + "/metrics").status_code, 404)
        self.assertEqual(self.client.get(f"/api/projects/{self.project_id}/matrices/matrix:sha256:" + "0" * 64).status_code, 404)



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
            status = self.client.get(f"/api/projects/{self.project_id}/utility-analyses/{analysis_id}").json()
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
        self.assertEqual(
            status["utility"]["sources"]["quality_input_stage"],
            {
                "stage_id": "REGISTERED_DATASET_SAMPLE_SOURCE_ARTIFACT_AS_QUALITY_ENGINE_INPUT",
                "artifact_kind": "DATASET_SAMPLE_SOURCE_BYTES",
                "matcher_preprocessing_relationships": [
                    {
                        "condition_id": leaf["condition_id"],
                        "relationship": "PRECEDES_QUALSIGHT_EXECUTED_FACE_DETECTION_ALIGNMENT_AND_CROP",
                    }
                ],
            },
        )
        self.assertEqual(utility["association"]["FNMR"]["logistic_error_on_quality"]["status"], "UNSUPPORTED_BY_SAMPLE")
        self.assertTrue(all(not b["supported"] for b in utility["quality_conditioned"]["FNMR"]["bins"]))
        self.assertEqual(utility["uncertainty"]["replicates"], 200)
        self.assertEqual(
            utility["uncertainty"]["method"],
            "PAIR_SUBJECT_MULTI_MEMBERSHIP_BOOTSTRAP_PERCENTILE",
        )
        self.assertEqual(
            utility["uncertainty"]["sampling_unit"],
            "SUBJECT_ID_IN_EITHER_PAIR_POSITION",
        )

        self.assertEqual(
            utility["areas"]["FNMR"]["au_erc"]["uncertainty"]["subjects"], 4
        )
        self.assertEqual(
            utility["areas"]["FMR"]["au_erc"]["uncertainty"]["subjects"], 8
        )
        # Independent recomputation script over the recorded artifacts.
        row_directory = None
        self.assertTrue(all("uri" not in artifact for artifact in run["artifacts"]))
        for artifact in self._private_run(summary["run_id"]).artifacts:
            if artifact.uri.endswith("utility.json"):
                row_directory = Path(artifact.uri).parent
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

    def test_utility_overlay_excludes_incompatible_real_analyses_with_reasons(self) -> None:
        """Only explicitly selected matcher/preprocessing axes may vary in a real ERC overlay."""

        def complete(
            leaf_id: str,
            *,
            measure: str = "UnifiedQualityScore",
            matcher_threshold: float = 0.8,
            quality_threshold: float = 40.0,
            partial_interval: float = 0.2,
            bootstrap_replicates: int = 0,
            seed: int = 23,
        ) -> dict:
            leaf = next(item for item in self.status["leaves"] if item["leaf_id"] == leaf_id)
            started = self.client.post(
                f"/api/projects/{self.project_id}/utility-analyses",
                json={
                    "matcher_run_id": leaf["run_id"],
                    "quality_job_id": self._quality_job()["job_id"],
                    "quality_measure": measure,
                    "pair_quality_policy": "pair.quality.min",
                    "matcher_threshold": matcher_threshold,
                    "quality_threshold": quality_threshold,
                    "partial_interval": partial_interval,
                    "bootstrap_replicates": bootstrap_replicates,
                    "seed": seed,
                },
            )
            self.assertIn(started.status_code, (200, 202), started.text)
            analysis_id = started.json()["analysis_id"]
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = self.client.get(f"/api/projects/{self.project_id}/utility-analyses/{analysis_id}").json()
                if status["state"] in {"COMPLETED", "FAILED"}:
                    break
                time.sleep(0.05)
            self.assertEqual(status["state"], "COMPLETED", status)
            return status["summary"]

        baseline = complete("QS-C22.AS")
        matcher_axis = complete("QS-C22.ES")
        preprocessing_axis = complete("QS-C22.AN")
        composite_axis = complete("QS-C22.EN")
        wrong_measure = complete("QS-C22.AS", measure="Sharpness")
        wrong_matcher_threshold = complete("QS-C22.AS", matcher_threshold=0.81)
        wrong_quality_threshold = complete("QS-C22.AS", quality_threshold=41.0)
        wrong_partial_interval = complete("QS-C22.AS", partial_interval=0.25)
        wrong_uncertainty = complete(
            "QS-C22.AS", bootstrap_replicates=10, seed=29
        )
        legacy = dict(baseline)
        legacy.pop("overlay_identity")

        identity = baseline["overlay_identity"]
        self.assertEqual(identity["schema_version"], "1.0.0")
        self.assertEqual(identity["project_id"], self.project_id)
        self.assertEqual(identity["dataset_id"], self.dataset_id)
        self.assertEqual(identity["protocol_id"], self.protocol_id)
        self.assertEqual(
            identity["quality_parquet_sha256"],
            hashlib.sha256(
                Path(self._private_job(self._quality_job()["job_id"])["parquet_path"]).read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(
            identity["metric_registry"],
            {
                "id": "qualsight-core-metrics",
                "version": "1.2.0",
                "definition_sha256": hashlib.sha256(
                    (ROOT / "metrics" / "metric-registry.json").read_bytes()
                ).hexdigest(),
            },
        )
        self.assertEqual(identity["pair_quality_policy"]["id"], "pair.quality.min")
        self.assertEqual(identity["pair_quality_policy"]["version"], "1.0.0")
        self.assertEqual(identity["matcher_threshold_value"], 0.8)
        self.assertEqual(
            identity["quality_policy_threshold"],
            {
                "kind": "QUALITY_POLICY_THRESHOLD",
                "source": "CALLER_SUPPLIED",
                "value": 40.0,
            },
        )
        self.assertEqual(identity["partial_interval"], 0.2)
        self.assertEqual(identity["bootstrap_replicates"], 0)
        self.assertEqual(identity["seed"], 23)
        self.assertEqual(
            identity["threshold_score_semantics"],
            {
                "threshold_kind": "MATCHER_THRESHOLD",
                "threshold_source": "CALLER_SUPPLIED",
                "threshold_selection": "CALLER_SUPPLIED_SELECTION_EVIDENCE_NOT_RECORDED",
                "target_fmr": None,
                "failure_policy": None,
                "evaluation_scope": "DESCRIPTIVE_APPLICATION_TO_RECORDED_TRIALS",
                "score_direction": "HIGHER_IS_MORE_SIMILAR",
                "equality_rule": "ACCEPT_WHEN_SCORE_GE_THRESHOLD",
            },
        )
        recorded_document = self.client.get(baseline["links"]["analysis"]).json()
        self.assertEqual(recorded_document["utility"]["overlay_identity"], identity)

        from qualsight.analysis.utility import classify_utility_overlay

        matcher_result = classify_utility_overlay(
            baseline, matcher_axis, comparison_axes=("MATCHER",)
        )
        self.assertEqual(
            matcher_result,
            {"eligible": True, "comparison_axis": "MATCHER", "reasons": []},
        )
        self.assertNotEqual(
            baseline["overlay_identity"]["comparisons_sha256"],
            matcher_axis["overlay_identity"]["comparisons_sha256"],
        )
        self.assertEqual(
            baseline["overlay_identity"]["pair_universe_sha256"],
            matcher_axis["overlay_identity"]["pair_universe_sha256"],
        )

        rejected_preprocessing = classify_utility_overlay(
            baseline, preprocessing_axis, comparison_axes=("MATCHER",)
        )
        self.assertFalse(rejected_preprocessing["eligible"])
        self.assertIn(
            "PREPROCESSING_AXIS_NOT_SELECTED",
            rejected_preprocessing["reasons"],
        )

        accepted_composite = classify_utility_overlay(
            baseline,
            composite_axis,
            comparison_axes=("MATCHER", "PREPROCESSING"),
        )
        self.assertEqual(accepted_composite["comparison_axis"], "COMPOSITE")
        self.assertTrue(accepted_composite["eligible"])

        rejected_measure = classify_utility_overlay(
            baseline,
            wrong_measure,
            comparison_axes=("MATCHER", "PREPROCESSING"),
        )
        self.assertFalse(rejected_measure["eligible"])
        self.assertIn("QUALITY_MEASURE_MISMATCH", rejected_measure["reasons"])

        for candidate, expected_reason in (
            (wrong_matcher_threshold, "MATCHER_THRESHOLD_MISMATCH"),
            (wrong_quality_threshold, "QUALITY_POLICY_THRESHOLD_MISMATCH"),
            (wrong_partial_interval, "PARTIAL_INTERVAL_MISMATCH"),
            (wrong_uncertainty, "BOOTSTRAP_CONFIGURATION_MISMATCH"),
        ):
            with self.subTest(expected_reason=expected_reason):
                rejected_configuration = classify_utility_overlay(
                    baseline,
                    candidate,
                    comparison_axes=("MATCHER", "PREPROCESSING"),
                )
                self.assertFalse(rejected_configuration["eligible"])
                self.assertIn(expected_reason, rejected_configuration["reasons"])

        rejected_legacy = classify_utility_overlay(
            baseline, legacy, comparison_axes=("MATCHER", "PREPROCESSING")
        )
        self.assertEqual(
            rejected_legacy,
            {
                "eligible": False,
                "comparison_axis": "UNKNOWN",
                "reasons": ["INCOMPLETE_OVERLAY_IDENTITY"],
            },
        )

        candidates = [matcher_axis, preprocessing_axis, wrong_measure, legacy]
        eligible = [
            item["analysis_id"]
            for item in candidates
            if classify_utility_overlay(
                baseline, item, comparison_axes=("MATCHER",)
            )["eligible"]
        ]
        self.assertEqual(eligible, [matcher_axis["analysis_id"]])

    def test_quality_measure_catalog_executes_every_recorded_measure_and_rejects_unrecorded_measure(self) -> None:
        """Every selector option executes from the real job; caller-invented names are refused."""

        import pyarrow.parquet as pq
        import subprocess
        import sys

        from qualsight.engines.ofiq.batch import MEASURE_NAMES

        job = self._quality_job()
        private_parquet_path = Path(self._private_job(job["job_id"])["parquet_path"])
        schema = pq.read_schema(private_parquet_path)
        recorded = {
            name[: -len(".scalar")]
            for name in schema.names
            if name.endswith(".scalar")
        }
        expected = [name for name in MEASURE_NAMES if name in recorded]
        expected.extend(sorted(recorded - set(MEASURE_NAMES)))

        catalog = self.client.get(
            f"/api/projects/{self.project_id}/jobs/{job['job_id']}/quality-measures"
        )
        self.assertEqual(catalog.status_code, 200, catalog.text)
        body = catalog.json()
        self.assertEqual(body["job_id"], job["job_id"])
        self.assertEqual(body["run_id"], job["run_id"])
        self.assertEqual(
            body["parquet_sha256"],
            hashlib.sha256(private_parquet_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(body["measures"], expected)
        self.assertEqual(set(body["measures"]), recorded)
        self.assertEqual(len(body["measures"]), 28)
        self.assertEqual(body["default_measure"], "UnifiedQualityScore")

        leaf = self.status["leaves"][0]
        for measure in body["measures"]:
            started = self.client.post(
                f"/api/projects/{self.project_id}/utility-analyses",
                json={
                    "matcher_run_id": leaf["run_id"],
                    "quality_job_id": job["job_id"],
                    "quality_measure": measure,
                    "matcher_threshold": 0.8,
                    "quality_threshold": 40.0,
                    "bootstrap_replicates": 0,
                    "seed": 19,
                },
            )
            self.assertEqual(started.status_code, 202, started.text)
            analysis_id = started.json()["analysis_id"]
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = self.client.get(
                    f"/api/projects/{self.project_id}/utility-analyses/{analysis_id}"
                ).json()
                if status["state"] in {"COMPLETED", "FAILED"}:
                    break
                time.sleep(0.05)
            self.assertEqual(status["state"], "COMPLETED", status)
            self.assertEqual(status["summary"]["quality_measure"], measure)
            run = self.client.get(status["summary"]["links"]["run"]).json()["run"]
            self.assertTrue(all("uri" not in artifact for artifact in run["artifacts"]))
            utility_path = next(
                Path(artifact.uri)
                for artifact in self._private_run(status["summary"]["run_id"]).artifacts
                if artifact.uri.endswith("utility.json")
            )
            verified = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "verify_utility_analysis.py"),
                    str(utility_path.parent),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                verified.returncode,
                0,
                f"{measure}: {verified.stdout}{verified.stderr}",
            )

        rejected = self.client.post(
            f"/api/projects/{self.project_id}/utility-analyses",
            json={
                "matcher_run_id": leaf["run_id"],
                "quality_job_id": job["job_id"],
                "quality_measure": "NotRecordedByTheSelectedJob",
                "matcher_threshold": 0.8,
                "quality_threshold": 40.0,
                "bootstrap_replicates": 0,
            },
        )
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(
            rejected.json()["detail"],
            "quality measure NotRecordedByTheSelectedJob is not a scalar column of the quality job",
        )

    def test_quality_measure_catalog_refuses_recorded_parquet_hash_drift(self) -> None:
        """A valid Parquet from a different real job cannot speak for this job's recorded hash."""

        job = self._quality_job()
        samples = self.client.get(
            f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/samples",
            params={"limit": 1},
        ).json()
        self.assertEqual(len(samples), 1)
        replacement_started = self.client.post(
            f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/quality-batch",
            json={
                "selection": {
                    "kind": "sample_ids",
                    "sample_ids": [samples[0]["sample_id"]],
                },
                "chunks": 1,
            },
        )
        self.assertEqual(replacement_started.status_code, 202, replacement_started.text)
        replacement_job_id = replacement_started.json()["job_id"]
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            replacement_job = self.client.get(
                f"/api/projects/{self.project_id}/jobs/{replacement_job_id}"
            ).json()
            if replacement_job["state"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.05)
        self.assertEqual(replacement_job["state"], "COMPLETED", replacement_job)

        target_path = Path(self._private_job(job["job_id"])["parquet_path"])
        original_bytes = target_path.read_bytes()
        replacement_bytes = Path(
            self._private_job(replacement_job["job_id"])["parquet_path"]
        ).read_bytes()
        self.assertNotEqual(
            hashlib.sha256(original_bytes).hexdigest(),
            hashlib.sha256(replacement_bytes).hexdigest(),
        )
        try:
            target_path.write_bytes(replacement_bytes)
            refused = self.client.get(
                f"/api/projects/{self.project_id}/jobs/{job['job_id']}/quality-measures"
            )
            self.assertEqual(refused.status_code, 409, refused.text)
            self.assertEqual(
                refused.json()["detail"],
                "quality job result artifact hash does not match its recorded SHA-256",
            )
            refused_distribution = self.client.get(
                f"/api/projects/{self.project_id}/jobs/{job['job_id']}/distribution",
                params={"measure": "UnifiedQualityScore"},
            )
            self.assertEqual(
                refused_distribution.status_code,
                409,
                refused_distribution.text,
            )
            self.assertEqual(
                refused_distribution.json()["detail"],
                "quality job result artifact hash does not match its recorded SHA-256",
            )
        finally:
            target_path.write_bytes(original_bytes)
        self.assertEqual(
            hashlib.sha256(target_path.read_bytes()).hexdigest(),
            hashlib.sha256(original_bytes).hexdigest(),
        )

    def test_quality_job_surface_requires_the_owning_project_identity(self) -> None:
        job = self._quality_job()
        job_id = job["job_id"]
        foreign = self.client.post(
            "/api/projects",
            json={
                "name": "foreign quality job read boundary",
                "description": "Real project-identity refusal gate.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        self.assertEqual(foreign.status_code, 201, foreign.text)
        foreign_project_id = foreign.json()["project_id"]
        suffixes = (
            "",
            "/quality-measures",
            "/distribution",
            "/drilldown",
            "/results",
        )
        for suffix in suffixes:
            with self.subTest(boundary="owner", suffix=suffix):
                response = self.client.get(
                    f"/api/projects/{self.project_id}/jobs/{job_id}{suffix}"
                )
                self.assertEqual(response.status_code, 200, response.text)
            with self.subTest(boundary="foreign", suffix=suffix):
                response = self.client.get(
                    f"/api/projects/{foreign_project_id}/jobs/{job_id}{suffix}"
                )
                self.assertEqual(response.status_code, 404, response.text)
            with self.subTest(boundary="retired-global", suffix=suffix):
                response = self.client.get(f"/api/jobs/{job_id}{suffix}")
                self.assertEqual(response.status_code, 404, response.text)

        foreign_cancel = self.client.post(
            f"/api/projects/{foreign_project_id}/jobs/{job_id}/cancel"
        )
        self.assertEqual(foreign_cancel.status_code, 404, foreign_cancel.text)
        retired_cancel = self.client.post(f"/api/jobs/{job_id}/cancel")
        self.assertEqual(retired_cancel.status_code, 404, retired_cancel.text)

    def test_utility_recorded_threshold_preserves_scientific_support(self) -> None:
        """A utility analysis must carry the recorded operating point's support boundary."""

        job = self._quality_job()
        leaf = self.status["leaves"][0]
        body = {
            "matcher_run_id": leaf["run_id"],
            "quality_job_id": job["job_id"],
            "pair_quality_policy": "pair.quality.min",
            "quality_threshold": 40.0,
            "bootstrap_replicates": 50,
            "seed": 7,
        }
        started = self.client.post(f"/api/projects/{self.project_id}/utility-analyses", json=body)
        self.assertEqual(started.status_code, 202, started.text)
        analysis_id = started.json()["analysis_id"]
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            status = self.client.get(f"/api/projects/{self.project_id}/utility-analyses/{analysis_id}").json()
            if status["state"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.5)
        self.assertEqual(status["state"], "COMPLETED", status)
        provenance = status["policy"]["thresholds"]["matcher"]["provenance"]
        self.assertEqual(provenance["source"], "RECORDED_OPERATING_POINT")
        self.assertEqual(provenance["evaluation_scope"], "DESCRIPTIVE_IN_SAMPLE")
        self.assertEqual(
            provenance["threshold_selection"],
            "SELECTED_AND_EVALUATED_ON_THE_SAME_RECORDED_TRIALS",
        )
        self.assertEqual(provenance["scientific_support"]["state"], "NOT_SUPPORTED")
        self.assertEqual(
            provenance["scientific_support"]["claim_scope"],
            "PROSPECTIVE_OR_OPERATIONAL_THRESHOLD_PERFORMANCE",
        )
        self.assertEqual(provenance["event_support"]["nonmated_trials"], 5)

    def test_legacy_scientific_records_remain_readable_with_conservative_support_labels(self) -> None:
        """Pre-1.2 real record shapes remain readable without gaining scientific support."""

        job = self._quality_job()
        leaf = self.status["leaves"][0]
        store = self._run_store()
        recorded = store.matcher_leaf(leaf["run_id"])
        self.assertIsNotNone(recorded)
        original_metrics_json = recorded["metrics_json"]
        legacy_fixture = json.loads(
            (
                ROOT
                / "backend"
                / "tests"
                / "fixtures"
                / "real_legacy_scientific_record_subset_2026-08-17.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            legacy_fixture["provenance"]["kind"],
            "REAL_HISTORICAL_QUALSIGHT_RECORD_SUBSET",
        )
        legacy_metrics = legacy_fixture["metrics"]
        legacy_metrics_json = json.dumps(
            legacy_metrics,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with store._lock:
            store._connection.execute(
                "UPDATE matcher_leaves SET metrics_json = ? WHERE run_id = ?",
                (legacy_metrics_json, leaf["run_id"]),
            )

        try:
            started = self.client.post(
                f"/api/projects/{self.project_id}/utility-analyses",
                json={
                    "matcher_run_id": leaf["run_id"],
                    "quality_job_id": job["job_id"],
                    "pair_quality_policy": "pair.quality.min",
                    "quality_threshold": 40.0,
                    "bootstrap_replicates": 50,
                    "seed": 29,
                },
            )
            self.assertEqual(started.status_code, 202, started.text)
            analysis_id = started.json()["analysis_id"]
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                status = self.client.get(f"/api/projects/{self.project_id}/utility-analyses/{analysis_id}").json()
                if status["state"] in {"COMPLETED", "FAILED"}:
                    break
                time.sleep(0.5)
            self.assertEqual(status["state"], "COMPLETED", status)
            threshold = status["policy"]["thresholds"]["matcher"]["provenance"]
            self.assertEqual(threshold["evaluation_scope"], "DESCRIPTIVE_IN_SAMPLE")
            self.assertEqual(threshold["scientific_support"]["state"], "NOT_SUPPORTED")

            cross_started = self.client.post(
                f"/api/projects/{self.project_id}/cross-matcher",
                json={
                    "matrix_id": self.matrix_id,
                    "quality_job_id": job["job_id"],
                    "bootstrap_replicates": 50,
                    "seed": 31,
                },
            )
            self.assertEqual(cross_started.status_code, 202, cross_started.text)
            cross_id = cross_started.json()["analysis_id"]
            cross_deadline = time.monotonic() + 600
            while time.monotonic() < cross_deadline:
                cross_status = self.client.get(f"/api/projects/{self.project_id}/cross-matcher/{cross_id}").json()
                if cross_status["state"] in {"COMPLETED", "FAILED"}:
                    break
                time.sleep(0.5)
            self.assertEqual(cross_status["state"], "COMPLETED", cross_status)
            legacy_thresholds = [
                side["threshold_provenance"]
                for contrast in cross_status["document"]["contrasts"]
                for side in (contrast["a"], contrast["b"])
                if side["run_id"] == leaf["run_id"]
            ]
            self.assertTrue(legacy_thresholds)
            self.assertTrue(
                all(
                    provenance["evaluation_scope"] == "DESCRIPTIVE_IN_SAMPLE"
                    and provenance["scientific_support"]["state"] == "NOT_SUPPORTED"
                    for provenance in legacy_thresholds
                )
            )

        finally:
            with store._lock:
                store._connection.execute(
                    "UPDATE matcher_leaves SET metrics_json = ? WHERE run_id = ?",
                    (original_metrics_json, leaf["run_id"]),
                )

    def test_cross_matcher_contrasts_hold_identities_fixed_and_resolve_disagreements_to_pairs(self) -> None:
        """QS-E40–QS-E42 on the real fixture: all fixed-axis contrasts and pair-resolved evidence."""

        import subprocess
        import sys

        sets = self.client.get(f"/api/projects/{self.project_id}/matrices/{self.matrix_id}/sets").json()
        self.assertTrue(sets["same_protocol"])
        self.assertEqual(sets["matcher_contrasts_available"], 30)
        self.assertEqual(sets["preprocessing_contrasts_available"], 6)
        self.assertEqual(set(sets["matcher_rights"].values()), {"APPROVED_LOCAL_RESEARCH_USE"})
        job = self._quality_job()
        body = {"matrix_id": self.matrix_id, "quality_job_id": job["job_id"], "bootstrap_replicates": 100, "seed": 3}
        started = self.client.post(f"/api/projects/{self.project_id}/cross-matcher", json=body)
        self.assertEqual(started.status_code, 202, started.text)
        analysis_id = started.json()["analysis_id"]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            status = self.client.get(f"/api/projects/{self.project_id}/cross-matcher/{analysis_id}").json()
            if status["state"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.5)
        self.assertEqual(status["state"], "COMPLETED", status)
        document = status["document"]
        self.assertEqual(len(document["contrasts"]), 36)
        self.assertEqual(
            document["uncertainty"]["method"],
            "PAIRED_PAIR_SUBJECT_MULTI_MEMBERSHIP_BOOTSTRAP_PERCENTILE",
        )
        self.assertEqual(
            document["uncertainty"]["sampling_unit"],
            "SUBJECT_ID_IN_EITHER_PAIR_POSITION_SHARED_BY_BOTH_RUNS",
        )
        self.assertEqual(
            document["sources"]["quality_input_stage"],
            {
                "stage_id": "REGISTERED_DATASET_SAMPLE_SOURCE_ARTIFACT_AS_QUALITY_ENGINE_INPUT",
                "artifact_kind": "DATASET_SAMPLE_SOURCE_BYTES",
                "matcher_preprocessing_relationships": [
                    {
                        "condition_id": condition_id,
                        "relationship": "PRECEDES_QUALSIGHT_EXECUTED_FACE_DETECTION_ALIGNMENT_AND_CROP",
                    }
                    for condition_id in sorted(
                        {leaf["condition_id"] for leaf in self.status["leaves"]}
                    )
                ],
            },
        )
        kinds = [c["kind"] for c in document["contrasts"]]
        self.assertEqual(kinds.count("MATCHER_CONTRAST_FIXED_CONDITION"), 30)
        self.assertEqual(kinds.count("PREPROCESSING_CONTRAST_FIXED_MATCHER"), 6)
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
            threshold_provenance = contrast["a"]["threshold_provenance"]
            self.assertEqual(threshold_provenance["kind"], "MATCHER_THRESHOLD")
            self.assertEqual(threshold_provenance["evaluation_scope"], "DESCRIPTIVE_IN_SAMPLE")
            self.assertEqual(
                threshold_provenance["threshold_selection"],
                "SELECTED_AND_EVALUATED_ON_THE_SAME_RECORDED_TRIALS",
            )
            self.assertEqual(threshold_provenance["scientific_support"]["state"], "NOT_SUPPORTED")
            self.assertEqual(
                threshold_provenance["scientific_support"]["claim_scope"],
                "PROSPECTIVE_OR_OPERATIONAL_THRESHOLD_PERFORMANCE",
            )
            self.assertEqual(threshold_provenance["event_support"]["nonmated_trials"], 5)
            self.assertIn(contrast["quality_matcher_interaction"]["status"], {"UNSUPPORTED_BY_SAMPLE", "ESTIMATED", "NOT_CONVERGED_OR_SEPARATED"})
            self.assertEqual(
                contrast["au_erc_differences"]["FNMR"]["au_erc"]["uncertainty"]["subjects"],
                4,
            )
            self.assertEqual(
                contrast["au_erc_differences"]["FMR"]["au_erc"]["uncertainty"]["subjects"],
                8,
            )
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
        note = self.client.post(f"/api/projects/{self.project_id}/annotations", json={"subject_type": "PROTOCOL_PAIR", "subject_id": pairs["rows"][0]["pair_id"], "text": "reviewed in the QS-E42 gate", "context": {"contrast_id": contrast["contrast_id"], "analysis_id": analysis_id}})
        self.assertEqual(note.status_code, 201, note.text)
        listed = self.client.get(f"/api/projects/{self.project_id}/annotations", params={"subject_id": pairs["rows"][0]["pair_id"]}).json()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["text"], "reviewed in the QS-E42 gate")
        again = self.client.get(status["summary"]["links"]["pairs"], params={"contrast_id": contrast["contrast_id"], "limit": 100}).json()
        self.assertEqual(len(again["rows"][0]["annotations"]), 1)
        # Independent recomputation of overlaps, Jaccard, transferred thresholds, and areas from the recorded artifacts.
        directory = None
        self.assertTrue(all("uri" not in artifact for artifact in run["artifacts"]))
        for artifact in self._private_run(status["summary"]["run_id"]).artifacts:
            if artifact.uri.endswith("crossmatcher.json"):
                directory = Path(artifact.uri).parent
        self.assertIsNotNone(directory)
        database_path = self.data_dir / "qualsight.sqlite3"
        sidecar_paths = [Path(f"{database_path}-wal"), Path(f"{database_path}-shm")]
        def sidecar_identities() -> dict[str, tuple[int, int] | None]:
            return {
                path.name: (path.stat().st_dev, path.stat().st_ino) if path.exists() else None
                for path in sidecar_paths
            }

        sidecars_before = sidecar_identities()
        snapshot_path = self.data_dir / "cross-matcher-verifier.snapshot.sqlite3"
        snapshot = self._run_store().snapshot_database(snapshot_path)
        self.assertEqual(Path(snapshot["path"]), snapshot_path)
        self.assertEqual(snapshot["size_bytes"], snapshot_path.stat().st_size)
        with self.assertRaises(FileExistsError):
            self._run_store().snapshot_database(snapshot_path)
        try:
            verified = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_cross_matcher.py"), str(directory), str(snapshot_path)], capture_output=True, text=True, check=False)
        finally:
            snapshot_path.unlink(missing_ok=True)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertIn("CROSS_MATCHER_VERIFIED", verified.stdout)
        self.assertEqual(
            sidecar_identities(),
            sidecars_before,
            "the independent verifier replaced a live SQLite WAL/SHM sidecar",
        )
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
            status = self.client.get(f"/api/projects/{self.project_id}/utility-analyses/{analysis_id}").json()
            if status["state"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.5)
        self.assertEqual(status["state"], "COMPLETED", status)
        caller_threshold = status["policy"]["thresholds"]["matcher"]["provenance"]
        self.assertEqual(
            caller_threshold["evaluation_scope"],
            "DESCRIPTIVE_APPLICATION_TO_RECORDED_TRIALS",
        )
        self.assertEqual(
            caller_threshold["threshold_selection"],
            "CALLER_SUPPLIED_SELECTION_EVIDENCE_NOT_RECORDED",
        )
        self.assertEqual(
            caller_threshold["scientific_support"]["state"], "NOT_SUPPORTED"
        )
        self.assertEqual(caller_threshold["event_support"]["nonmated_trials"], 5)
        self.assertEqual(caller_threshold["event_support"]["observed_false_matches"], 0)
        # Figures: PNG/SVG/PDF/CSV + provenance sidecar; content-addressed; every series names its source Run.
        figures = {}
        for kind, params in (("det_overlay", {"matrix_id": self.matrix_id}), ("operating_points", {"matrix_id": self.matrix_id}), ("score_distributions", {"run_id": leaf["run_id"], "bins": 10}), ("erc_overlay", {"analysis_ids": [analysis_id]}), ("quality_conditioned_fnmr", {"analysis_id": analysis_id}), ("quality_histogram", {"job_id": job["job_id"], "bins": 10})):
            response = self.client.post(f"/api/projects/{self.project_id}/figures", json={"kind": kind, "parameters": params})
            self.assertEqual(response.status_code, 201, response.text)
            figures[kind] = response.json()
        det = figures["det_overlay"]
        self.assertEqual(len(det["source_run_ids"]), 12)
        for fmt in ("png", "svg", "pdf", "csv", "provenance"):
            file_response = self.client.get(det["files"][fmt])
            self.assertEqual(file_response.status_code, 200, fmt)
        svg = self.client.get(det["files"]["svg"]).text
        self.assertIn("<svg", svg)
        provenance = self.client.get(det["files"]["provenance"]).json()
        self.assertEqual(provenance["metric_definitions"][0], ["biometric.verification.det", "1.0.0"])
        self.assertTrue(all(t["kind"] == "MATCHER_THRESHOLD" for t in provenance["thresholds"]))
        figure_detail = self.client.get(f"/api/projects/{self.project_id}/figures/{det['figure_id']}").json()
        self.assertNotIn("provenance", figure_detail)
        private_figure = self._run_store().figure(det["figure_id"])
        self.assertIsNotNone(private_figure)
        figure_provenance_path = Path(private_figure["directory"]) / "figure.provenance.json"
        original_figure_provenance = figure_provenance_path.read_bytes()
        original_figure_provenance_sha256 = hashlib.sha256(original_figure_provenance).hexdigest()
        self.assertEqual(det["provenance_sha256"], original_figure_provenance_sha256)
        self.assertEqual(figure_detail["provenance_sha256"], original_figure_provenance_sha256)
        self.assertEqual(
            self.client.get(det["files"]["provenance"]).headers["X-QualSight-Sha256"],
            original_figure_provenance_sha256,
        )
        reformatted_figure_provenance = (
            json.dumps(json.loads(original_figure_provenance), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self.assertNotEqual(reformatted_figure_provenance, original_figure_provenance)
        self.assertEqual(json.loads(reformatted_figure_provenance), json.loads(original_figure_provenance))
        try:
            figure_provenance_path.write_bytes(reformatted_figure_provenance)
            drifted_figure_provenance = self.client.get(det["files"]["provenance"])
            self.assertEqual(drifted_figure_provenance.status_code, 409, drifted_figure_provenance.text)
        finally:
            figure_provenance_path.write_bytes(original_figure_provenance)
        again = self.client.post(f"/api/projects/{self.project_id}/figures", json={"kind": "det_overlay", "parameters": {"matrix_id": self.matrix_id}}).json()
        self.assertEqual(again["figure_id"], det["figure_id"])
        # Dashboard: saved layout reloads exactly with every panel's Run references.
        panels = [{"kind": "figure", "ref": det["figure_id"]}, {"kind": "figure", "ref": figures["erc_overlay"]["figure_id"]}, {"kind": "operating_points", "ref": leaf["run_id"]}, {"kind": "policy_table", "ref": analysis_id}]
        saved = self.client.post(f"/api/projects/{self.project_id}/dashboards", json={"name": "fixture dashboard", "template": "FIQA Evaluation", "columns": 2, "panels": panels})
        self.assertEqual(saved.status_code, 201, saved.text)
        loaded = self.client.get(f"/api/projects/{self.project_id}/dashboards/{saved.json()['dashboard_id']}").json()
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
        self.assertNotIn("directory", body)
        self.assertNotIn("files", body)
        self.assertNotIn("provenance", body)
        private_report = self._run_store().report(body["report_id"])
        self.assertIsNotNone(private_report)
        private_report_provenance = json.loads(private_report["provenance_json"])
        self.assertEqual(
            private_report_provenance["metric_registry"],
            {"id": "qualsight-core-metrics", "version": "1.2.0"},
        )
        self.assertEqual(
            private_report_provenance["scientific_support"],
            {
                "overall_state": "NOT_SUPPORTED",
                "auto_approved": False,
                "basis": "RECORDED_RUN_AND_CLAIM_SPECIFIC_SCIENTIFIC_SUPPORT_STATES",
            },
        )
        self.assertTrue(
            private_report_provenance["runs"]
            and all(
                run["scientific_support"] == "NOT_ASSESSED"
                for run in private_report_provenance["runs"]
            )
        )
        html_text = self.client.get(body["links"]["html"]).text
        self.assertIn("<svg", html_text)
        self.assertIn("MATCHER THRESHOLD", html_text)
        self.assertIn("DESCRIPTIVE_IN_SAMPLE", html_text)
        self.assertIn("PROSPECTIVE_OR_OPERATIONAL_THRESHOLD_PERFORMANCE", html_text)
        self.assertIn(leaf["run_id"], html_text)
        self.assertIn("Scientific support", html_text)
        self.assertIn("NOT_ASSESSED", html_text)
        self.assertIn("metric registry v1.2.0", html_text)
        self.assertIn(
            "REGISTERED_DATASET_SAMPLE_SOURCE_ARTIFACT_AS_QUALITY_ENGINE_INPUT", html_text
        )
        report_content = self.client.get(body["links"]["json"]).json()
        matcher_section = next(
            section
            for section in report_content["sections"]
            if section["title"].startswith("Matcher Runs")
        )
        self.assertEqual(
            matcher_section["content"]["columns"][-5:],
            [
                "EER evaluation scope",
                "Threshold selection",
                "Prospective / operational support",
                "Claim scope",
                "Run",
            ],
        )
        for row in matcher_section["content"]["rows"]:
            self.assertEqual(row[-5], "DESCRIPTIVE_IN_SAMPLE")
            self.assertEqual(
                row[-4], "SELECTED_AND_EVALUATED_ON_THE_SAME_RECORDED_TRIALS"
            )
            self.assertEqual(row[-3], "NOT_SUPPORTED")
            self.assertEqual(
                row[-2], "PROSPECTIVE_OR_OPERATIONAL_THRESHOLD_PERFORMANCE"
            )
        pdf = self.client.get(body["links"]["pdf"])
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        report_path = Path(private_report["directory"]) / "report.html"
        report_json_path = Path(private_report["directory"]) / "report.json"
        provenance_path = Path(private_report["directory"]) / "report.provenance.json"
        original_report = report_path.read_bytes()
        original_provenance = provenance_path.read_bytes()
        report_route = next(
            route
            for route in self.client.app.routes
            if getattr(route, "path", None) == "/api/projects/{project_id}/reports/{report_id}/file"
        )
        prepared_response = report_route.endpoint(self.project_id, body["report_id"], "html")
        replacement = report_path.with_name("report-race-replacement.html")
        self.assertGreater(len(original_report), 1)
        replacement.write_bytes(original_report[:-1])
        os.replace(replacement, report_path)
        messages: list[dict[str, object]] = []

        receive_wait = asyncio.Event()

        async def receive() -> dict[str, object]:
            await receive_wait.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        try:
            asyncio.run(
                prepared_response(
                    {"type": "http", "method": "GET", "path": "/", "headers": []},
                    receive,
                    send,
                )
            )
            streamed = b"".join(
                message.get("body", b"")
                for message in messages
                if message["type"] == "http.response.body"
            )
            self.assertEqual(hashlib.sha256(streamed).hexdigest(), private_report["html_sha256"])
        finally:
            report_path.write_bytes(original_report)
        try:
            report_path.write_bytes(original_report + report_json_path.read_bytes())
            tampered_provenance = json.loads(original_provenance)
            tampered_provenance["files"]["html"]["sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
            provenance_path.write_text(json.dumps(tampered_provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            self.assertEqual(self.client.get(body["links"]["html"]).status_code, 409)
            self.assertEqual(self.client.get(f"/api/projects/{self.project_id}/reports/{body['report_id']}/file", params={"format": "provenance"}).status_code, 409)
            listed_report = next(item for item in self.client.get(f"/api/projects/{self.project_id}/reports").json() if item["report_id"] == body["report_id"])
            self.assertEqual(listed_report["downloads"]["html"]["state"], "DRIFTED")
            self.assertNotIn("html", listed_report["links"])
        finally:
            report_path.write_bytes(original_report)
            provenance_path.write_bytes(original_provenance)
        permitted = self.client.post(f"/api/projects/{self.project_id}/reports", json={"spec": {**spec, "image_policy": "INCLUDE_PERMITTED"}}).json()
        permitted_row = self._run_store().report(permitted["report_id"])
        self.assertIsNotNone(permitted_row)
        permitted_record = json.loads(
            (Path(permitted_row["directory"]) / "report.provenance.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertGreaterEqual(
            permitted_record["images_embedded"] + permitted_record["images_withheld"], 0
        )
        self.assertEqual(permitted_record["provenance"]["image_policy"], "INCLUDE_PERMITTED")
        self.assertTrue(
            permitted_record["provenance"]["runs"]
            and all(
                run["evidence_sha256"]
                for run in permitted_record["provenance"]["runs"]
            )
        )
        # Exports carry run identifiers and classification headers; every category and format serves a file.
        export_root = self.data_dir / "exports"
        export_files_before_uncreated_get = sorted(
            path.relative_to(export_root)
            for path in export_root.rglob("*")
            if path.is_file()
        ) if export_root.exists() else []
        uncreated_get = self.client.get(
            f"/api/projects/{self.project_id}/exports/quality_results/file",
            params={"ref": job["job_id"], "format": "csv"},
        )
        self.assertEqual(uncreated_get.status_code, 410, uncreated_get.text)
        self.assertEqual(
            sorted(
                path.relative_to(export_root)
                for path in export_root.rglob("*")
                if path.is_file()
            ) if export_root.exists() else [],
            export_files_before_uncreated_get,
        )
        for category, ref in (("quality_results", job["job_id"]), ("comparison_records", leaf["run_id"]), ("embeddings", leaf["run_id"]), ("metric_tables", leaf["run_id"]), ("statistical_results", analysis_id), ("annotations", self.project_id)):
            for fmt in ("csv", "json", "parquet"):
                response = self.client.post(f"/api/projects/{self.project_id}/exports/{category}", params={"ref": ref, "format": fmt})
                self.assertEqual(response.status_code, 200, (category, fmt, response.text[:200]))
                self.assertIn(response.headers["X-QualSight-Classification"], {"SENSITIVE_BIOMETRIC_DERIVED", "PRIVATE"})
        receipt_response = self.client.post(
            f"/api/projects/{self.project_id}/exports/quality_results",
            params={"ref": job["job_id"], "format": "csv"},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(receipt_response.status_code, 200, receipt_response.text)
        receipt = receipt_response.json()
        self.assertEqual(
            set(receipt),
            {
                "export_id",
                "project_id",
                "category",
                "ref",
                "format",
                "rows",
                "classification",
                "sha256",
                "size_bytes",
                "download",
            },
        )
        self.assertEqual(receipt["project_id"], self.project_id)
        self.assertEqual(receipt["category"], "quality_results")
        self.assertEqual(receipt["ref"], job["job_id"])
        self.assertEqual(receipt["format"], "csv")
        self.assertEqual(receipt["download"]["state"], "VERIFIED")
        export_download = self.client.get(receipt["download"]["href"])
        self.assertEqual(export_download.status_code, 200, export_download.text[:200])
        self.assertEqual(
            hashlib.sha256(export_download.content).hexdigest(), receipt["sha256"]
        )
        self.assertEqual(len(export_download.content), receipt["size_bytes"])
        self.assertEqual(
            export_download.headers["X-QualSight-Export-Id"], receipt["export_id"]
        )
        export_files_before_retired_get = {
            path.relative_to(self.data_dir / "exports"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.data_dir / "exports").rglob("*")
            if path.is_file()
        }
        retired_get = self.client.get(
            f"/api/projects/{self.project_id}/exports/embeddings",
            params={"ref": leaf["run_id"], "format": "json"},
        )
        self.assertEqual(retired_get.status_code, 404, retired_get.text)
        self.assertEqual(retired_get.json(), {"detail": "API route is not available"})
        self.assertEqual(
            {
                path.relative_to(self.data_dir / "exports"): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (self.data_dir / "exports").rglob("*")
                if path.is_file()
            },
            export_files_before_retired_get,
        )
        cross_site = self.client.post(
            f"/api/projects/{self.project_id}/exports/embeddings",
            params={"ref": leaf["run_id"], "format": "json"},
            headers={"Origin": "https://attacker.invalid"},
        )
        self.assertEqual(cross_site.status_code, 403, cross_site.text)
        cross_port = self.client.post(
            f"/api/projects/{self.project_id}/exports/embeddings",
            params={"ref": leaf["run_id"], "format": "json"},
            headers={"Origin": "http://127.0.0.1:65535"},
        )
        self.assertEqual(cross_port.status_code, 403, cross_port.text)
        embeddings = self.client.post(f"/api/projects/{self.project_id}/exports/embeddings", params={"ref": leaf["run_id"], "format": "json"}).json()
        self.assertTrue(all(row["run_id"] == leaf["run_id"] and len(row["embedding"]) == 512 for row in embeddings["rows"]))
        foreign = self.client.post(
            "/api/projects",
            json={
                "name": "artifact authorization foreign project",
                "description": "Real project used to prove recorded artifact isolation.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        self.assertEqual(foreign.status_code, 201, foreign.text)
        foreign_project_id = foreign.json()["project_id"]
        exports_before_denial = sorted(path.relative_to(export_root) for path in export_root.rglob("*") if path.is_file())
        denied_export = self.client.post(
            f"/api/projects/{foreign_project_id}/exports/embeddings",
            params={"ref": leaf["run_id"], "format": "json"},
        )
        self.assertEqual(denied_export.status_code, 404, denied_export.text)
        self.assertEqual(
            sorted(path.relative_to(export_root) for path in export_root.rglob("*") if path.is_file()),
            exports_before_denial,
        )
        denied_figure = self.client.get(
            f"/api/projects/{foreign_project_id}/figures/{det['figure_id']}/file"
        )
        denied_report = self.client.get(
            f"/api/projects/{foreign_project_id}/reports/{body['report_id']}/file"
        )
        for response in (denied_export, denied_figure, denied_report):
            lowered_headers = {name.lower() for name in response.headers}
            self.assertNotIn("content-disposition", lowered_headers)
            self.assertFalse(
                any("sha256" in name or "classification" in name for name in lowered_headers)
            )
        # Bundle: portable package with checksums and no image bytes; reproduction re-executes the analysis and reports EXACT.
        run_id = status["summary"]["run_id"]
        bundle = self.client.post(f"/api/projects/{self.project_id}/bundles", json={"run_id": run_id})
        self.assertEqual(bundle.status_code, 201, bundle.text)
        record = bundle.json()
        self.assertNotIn("path", record)
        private_bundle = next(
            item
            for item in self._run_store().experiment_bundles(project_id=self.project_id)
            if item["bundle_id"] == record["bundle_id"]
        )
        private_bundle_path = Path(private_bundle["path"])
        denied_bundle = self.client.get(
            f"/api/projects/{foreign_project_id}/bundles/{record['bundle_id']}/file"
        )
        self.assertEqual(denied_bundle.status_code, 404, denied_bundle.text)
        lowered_bundle_headers = {name.lower() for name in denied_bundle.headers}
        self.assertNotIn("content-disposition", lowered_bundle_headers)
        self.assertFalse(
            any("sha256" in name or "classification" in name for name in lowered_bundle_headers)
        )
        with zipfile.ZipFile(private_bundle_path) as archive:
            names = set(archive.namelist())
            self.assertTrue({"manifest.json", "experiment.yaml", "protocol.yaml", "dataset-manifest.json", "environment.json", "metrics.json", "results.parquet", "checksums.sha256", "README.md"} <= names, names)
            self.assertFalse(any(name.endswith((".jpg", ".jpeg", ".png")) and not name.startswith("figures/") for name in names))
        reproduced = self.client.post(f"/api/projects/{self.project_id}/reproductions", json={"bundle_id": record["bundle_id"]})
        self.assertEqual(reproduced.status_code, 201, reproduced.text)
        reproduction_payload = reproduced.json()
        self.assertEqual(reproduction_payload["state"], "EXACT", reproduction_payload)
        self.assertGreater(reproduction_payload["compared_values"], 100)
        self.assertEqual([s["step"] for s in reproduction_payload["steps"]], ["validate-checksums", "validate-dependencies", "validate-model-versions", "validate-input-hashes", "execute-and-compare"])
        model_validation = next(
            step
            for step in reproduction_payload["steps"]
            if step["step"] == "validate-model-versions"
        )
        self.assertNotIn("missing_or_drifted", model_validation)
        self.assertEqual(model_validation["missing_or_drifted_count"], 0)
        self.assertFalse(
            {"error", "exception", "message", "reason", "stderr", "traceback"}
            & {key for step in reproduction_payload["steps"] for key in step}
        )
        private_reproduction = json.loads(
            self._run_store().reproductions(
                original_run_id=run_id,
                project_id=self.project_id,
            )[0]["report_json"]
        )
        private_model_validation = next(
            step
            for step in private_reproduction["steps"]
            if step["step"] == "validate-model-versions"
        )
        self.assertIn("missing_or_drifted", private_model_validation)
        # A matcher-leaf bundle re-runs the matcher on the bundled pairs (fixture: ten pairs) and reproduces the recorded scores.
        leaf_bundle = self.client.post(f"/api/projects/{self.project_id}/bundles", json={"run_id": leaf["run_id"]}).json()
        leaf_reproduced = self.client.post(f"/api/projects/{self.project_id}/reproductions", json={"bundle_id": leaf_bundle["bundle_id"]}).json()
        self.assertIn(leaf_reproduced["state"], {"EXACT", "WITHIN_TOLERANCE"}, leaf_reproduced)
        recorded_before_refusal = self.client.get(
            f"/api/projects/{self.project_id}/reproductions",
            params={"original_run_id": run_id},
        ).json()
        self.assertEqual(len(recorded_before_refusal), 1)
        denied_reproduction = self.client.post(
            f"/api/projects/{foreign_project_id}/reproductions",
            json={"bundle_id": record["bundle_id"]},
        )
        self.assertEqual(denied_reproduction.status_code, 404, denied_reproduction.text)
        self.assertEqual(
            self.client.get(f"/api/projects/{foreign_project_id}/reproductions").json(),
            [],
        )
        # A tampered bundle is BLOCKED at the checksum step; the target results are untouched.
        with tempfile.TemporaryDirectory(
            prefix="qualsight-tampered-bundle-"
        ) as tamper_scratch:
            tampered = Path(tamper_scratch) / "tampered.qsexp"
            with zipfile.ZipFile(private_bundle_path) as source, zipfile.ZipFile(
                tampered, "w"
            ) as target:
                for item in source.infolist():
                    data = source.read(item.filename)
                    if item.filename == "metrics.json":
                        data = data.replace(b"1.0.0", b"9.9.9", 1)
                    target.writestr(item, data)
            bundle_path = private_bundle_path
            original_bundle = bundle_path.read_bytes()
            try:
                bundle_path.write_bytes(tampered.read_bytes())
                blocked = self.client.post(f"/api/projects/{self.project_id}/reproductions", json={"bundle_id": record["bundle_id"]})
                self.assertEqual(blocked.status_code, 409, blocked.text)
                listed_bundle = next(item for item in self.client.get(f"/api/projects/{self.project_id}/bundles").json() if item["bundle_id"] == record["bundle_id"])
                self.assertEqual(listed_bundle["download"]["state"], "DRIFTED")
                self.assertNotIn("download", listed_bundle["links"])
            finally:
                bundle_path.write_bytes(original_bundle)
        listed = self.client.get(f"/api/projects/{self.project_id}/reproductions", params={"original_run_id": run_id}).json()
        self.assertEqual(listed, recorded_before_refusal)

    def test_network_guard_refuses_non_loopback_and_admits_loopback_in_a_fresh_process(self) -> None:
        """QS-H70: the runtime network guard is real — checked in a subprocess so this test process stays unguarded."""

        import subprocess
        import sys

        code = (
            "import socket, json\n"
            "from qualsight.security.network_guard import NetworkGuardViolation, guard_status, install_network_guard\n"
            "install_network_guard()\n"
            "refused = refused_udp = False\n"
            "try:\n"
            "    socket.create_connection(('192.0.2.1', 9), timeout=1)\n"
            "except NetworkGuardViolation:\n"
            "    refused = True\n"
            "except OSError:\n"
            "    refused = False\n"
            "udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
            "try:\n"
            "    udp.sendto(b'qualsight', ('192.0.2.1', 9))\n"
            "except NetworkGuardViolation:\n"
            "    refused_udp = True\n"
            "finally:\n"
            "    udp.close()\n"
            "server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(1)\n"
            "client = socket.create_connection(server.getsockname(), timeout=2)\n"
            "client.close(); server.close()\n"
            "print(json.dumps({'refused': refused, 'refused_udp': refused_udp, 'status': guard_status()}))\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, cwd=str(ROOT / "backend"), env={**__import__("os").environ, "PYTHONPATH": "src:.."})
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(report["refused"], report)
        self.assertTrue(report["refused_udp"], report)
        self.assertEqual(len(report["status"]["refused_attempts"]), 2)

    def test_project_embedding_inventory_rehashes_recorded_artifacts(self) -> None:
        inventory = self.client.get(f"/api/projects/{self.project_id}/embeddings")
        self.assertEqual(inventory.status_code, 200, inventory.text)
        items = inventory.json()
        executed_leaves = [
            item
            for item in self.client.get(f"/api/projects/{self.project_id}/matcher-leaves").json()
            if item["provenance_marker"] == "QUALSIGHT_EXECUTED"
        ]
        self.assertEqual(len(items), len(executed_leaves))
        self.assertEqual({item["run_id"] for item in items}, {item["run_id"] for item in executed_leaves})
        for item in items:
            self.assertEqual(item["integrity"], "VERIFIED")
            self.assertEqual(item["dimension"], 512)
            self.assertGreater(item["rows"], 0)
            self.assertEqual(item["registered_sha256"], item["observed_sha256"])
            self.assertEqual(
                item["links"],
                {"run": f"/api/projects/{self.project_id}/runs/{item['run_id']}"},
            )
        first = items[0]
        run = self.client.get(first["links"]["run"]).json()["run"]
        self.assertTrue(all("uri" not in artifact for artifact in run["artifacts"]))
        embedding_path = Path(
            next(
                artifact.uri
                for artifact in self._private_run(first["run_id"]).artifacts
                if artifact.uri.endswith("embeddings.npy")
            )
        )
        real_sample = self.client.get(
            f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/samples",
            params={"limit": 1},
        ).json()[0]
        real_image = self.client.get(real_sample["links"]["image"])
        self.assertEqual(real_image.status_code, 200, real_image.text)
        self.assertEqual(
            hashlib.sha256(real_image.content).hexdigest(), real_sample["sha256"]
        )
        original = embedding_path.read_bytes()
        try:
            embedding_path.write_bytes(real_image.content)
            drifted = self.client.get(f"/api/projects/{self.project_id}/embeddings")
            self.assertEqual(drifted.status_code, 200, drifted.text)
            changed = next(item for item in drifted.json() if item["run_id"] == first["run_id"])
            self.assertEqual(changed["integrity"], "DRIFTED")
            self.assertIsNotNone(changed["integrity_reason"])
            self.assertIsNone(changed["rows"])
            self.assertIsNone(changed["dimension"])
        finally:
            embedding_path.write_bytes(original)

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
        audit_snapshot = self.data_dir / "local-data-audit.snapshot.sqlite3"
        self._run_store().snapshot_database(audit_snapshot)
        try:
            audit = subprocess.run([sys.executable, str(ROOT / "scripts" / "audit_local_data_controls.py"), "--data-dir", str(self.data_dir), "--database-snapshot", str(audit_snapshot)], capture_output=True, text=True, check=False)
        finally:
            audit_snapshot.unlink(missing_ok=True)
        self.assertEqual(audit.returncode, 0, audit.stdout[-800:] + audit.stderr[-400:])
        # Purge: scope is listed, confirmation is required, managed files and scientific metadata are removed and re-verified, and the audit tombstone remains.
        private_scope = purge_targets(self._run_store(), self.data_dir, self.project_id)
        scope = self.client.get(f"/api/projects/{self.project_id}/purge-scope").json()
        self.assertEqual(
            scope["target_counts"],
            {
                "files": len(private_scope["files"]),
                "directories": len(private_scope["directories"]),
            },
        )
        self.assertGreater(sum(scope["target_counts"].values()), 5)
        self.assertNotIn("targets", scope)
        self.assertIn("not scrubbed", scope["scope"])
        self.assertEqual(self.client.post(f"/api/projects/{self.project_id}/purge", json={"confirm": "wrong"}).status_code, 409)
        before = [path for path in private_scope["files"] + private_scope["directories"] if Path(path).exists()]
        self.assertTrue(before)
        purged = self.client.post(f"/api/projects/{self.project_id}/purge", json={"confirm": self.project_id})
        self.assertEqual(purged.status_code, 201, purged.text)
        record = purged.json()
        self.assertTrue(record["verified"], record)
        self.assertEqual(record["outcome_counts"]["remaining_after_purge"], 0)
        self.assertEqual(record["outcome_counts"]["failed"], 0)
        self.assertTrue(record["metadata"]["verified"], record["metadata"])
        self.assertFalse(any(record["metadata"]["remaining"].values()), record["metadata"])
        self.assertFalse(any(Path(path).exists() for path in before))
        self.assertEqual(self.client.get(f"/api/projects/{self.project_id}/runs/{self.status['leaves'][0]['run_id']}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/projects/{self.project_id}/datasets").json(), [])
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
        self.assertTrue(run["model_hashes"])
        self.assertTrue(all("path" not in identity for identity in run["model_hashes"]))
        private_run = self._run_store().run(leaf["run_id"])
        self.assertIsNotNone(private_run)
        self.assertTrue(
            any(identity.path.endswith("external_scores.csv") for identity in private_run[0].model_hashes)
        )
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
            status = self.client.get(f"/api/projects/{self.project_id}/identification-searches/{search_id}").json()
            if status["state"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(1)
        self.assertEqual(status["state"], "COMPLETED", status)
        metrics = status["metrics"]
        self.assertEqual(
            metrics["evaluation_scope"],
            "EXPLORATORY_CUSTOM_LFW_IDENTIFICATION_PROTOCOL",
        )
        self.assertEqual(
            metrics["scientific_support"],
            {
                "state": "NOT_SUPPORTED",
                "claim_scope": "OPERATIONAL_OPEN_SET_IDENTIFICATION_PERFORMANCE",
                "reason": "custom LFW gallery and probe construction is exploratory",
            },
        )
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
        run = self.client.get(f"/api/projects/{self.project_id}/runs/{status['run_id']}").json()["run"]
        self.assertEqual(run["matcher"]["component_id"], "adaface-ir101-local-candidate")
        self.assertEqual(run["pair_search_protocol"]["component_id"], protocol["protocol_id"])
        candidates = self.client.get(f"/api/projects/{self.project_id}/identification-searches/{search_id}/candidates", params={"limit": 1000}).json()
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
        listing_response = self.client.get(derived["links"]["detail"])
        self.assertEqual(listing_response.status_code, 200, listing_response.text)
        listing = listing_response.json()
        self.assertEqual(len(listing["samples"]), 96)
        first = listing["samples"][0]
        self.assertTrue(first["parent_sample_id"].startswith("sample:sha256:"))
        self.assertEqual(first["chain"][0]["root_resource_id"], "lfw-funneled-local-candidate")
        self.assertIn("kind", first["chain"][-1]); self.assertIn("parameters", first["chain"][-1]); self.assertIn("version", first["chain"][-1])
        derived_image = self.client.get(first["links"]["image"])
        self.assertEqual(derived_image.status_code, 200, derived_image.text)
        self.assertEqual(hashlib.sha256(derived_image.content).hexdigest(), first["sha256"])
        self.assertEqual(
            self.client.get(
                f"/api/projects/{self.project_id}/samples/{first['parent_sample_id']}"
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                f"/api/projects/{self.project_id}/samples/{first['derived_sample_id']}"
            ).status_code,
            200,
        )
        # OFIQ runs over the derived dataset through the registered engine (the batch engine admits derived samples whose chain roots at a registered input).
        started = self.client.post(f"/api/projects/{self.project_id}/datasets/{derived['derived_dataset_id']}/quality-batch", json={"selection": {"kind": "all"}, "chunks": 1})
        self.assertEqual(started.status_code, 202, started.text)
        job_id = started.json()["job_id"]
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            job = self.client.get(
                f"/api/projects/{self.project_id}/jobs/{job_id}"
            ).json()
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
            status = self.client.get(f"/api/projects/{self.project_id}/degradation-analyses/{analysis_id}").json()
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
        run = self.client.get(f"/api/projects/{self.project_id}/runs/{status['run_id']}").json()["run"]
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
            job = self.client.get(
                f"/api/projects/{self.project_id}/jobs/{job_id}"
            ).json()
            if job["ended_at"]:
                break
            time.sleep(0.5)
        self.assertEqual(job["state"], "COMPLETED", job)
        self.assertEqual(job["assessed"], 16)
        public_run = self.client.get(
            f"/api/projects/{self.project_id}/runs/{job['run_id']}"
        ).json()["run"]
        self.assertNotIn("text", public_run["configuration"])
        private_run = self._private_run(job["run_id"])
        self.assertEqual(
            public_run["configuration"]["sha256"], private_run.configuration.sha256
        )
        configuration = json.loads(private_run.configuration.text)
        environment = configuration.get("execution_environment") or {}
        self.assertEqual(environment.get("kind"), "CONTAINER")
        self.assertTrue(str(environment.get("image_id", "")).startswith("sha256:"))
        self.assertEqual(environment.get("network"), "none")
        self.assertTrue(environment.get("rootfs_manifest_sha256"))
        self.assertEqual(
            environment.get("resource_limits"),
            {"memory": "8g", "cpus": 4.0, "pids": 256, "tmpfs": "256m"},
        )
        launcher = environment.get("launcher_commands", [])[0]
        self.assertIn("--memory", launcher)
        self.assertIn("--cpus", launcher)
        self.assertIn("--pids-limit", launcher)
        # Same measures as the host adapter over the same samples.
        host = {r["sample_id"]: r for r in pq.read_table(self._private_job(host_job["job_id"])["parquet_path"]).to_pylist()}
        container = {r["sample_id"]: r for r in pq.read_table(self._private_job(job["job_id"])["parquet_path"]).to_pylist()}
        self.assertEqual(set(host), set(container))
        measure_columns = [c for c in next(iter(host.values())) if c.endswith(".native") or c.endswith(".scalar") or c.endswith(".failed")]
        for sample_id, row in host.items():
            for column in measure_columns:
                self.assertEqual(row[column], container[sample_id][column], (sample_id, column))


if __name__ == "__main__":
    unittest.main()
