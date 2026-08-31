"""QS-C22/QS-C23 on real data: matcher × preprocessing leaves over frozen protocol pairs.

Uses the ten fixture pairs of the frozen LFW View 2 protocol so the twelve real
SCRFD-source Runs execute in seconds; the full-protocol matrix is run by the CLI and
recorded in the ledger. Checks that every pair is retained with a score or an
explicit failure, that matcher and preprocessing identities are recorded
separately, and that the metric engine reconciles to an independent
recomputation from the Parquet comparisons.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

import numpy as np
import pyarrow.parquet as pq
from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.domain.comparison import ProtocolPair
from qualsight.execution.registry_binding import OFIQ_INPUT_RESOURCE_ID, OFIQ_V112_RUNTIME_RESOURCE_ID
from qualsight.matchers.conditions import SCRFD_CONDITIONS, ConditionId
from qualsight.matchers.matrix import LEAF_IDS, MatcherMatrixJob, MatrixError
from qualsight.matchers.runtime import MatcherId
from qualsight.matchers.upstream import UpstreamSources
from qualsight.metrics.verification import ComparisonInputs, compute_verification_metrics


ROOT = Path(__file__).resolve().parents[2]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class RealMatcherMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-matrix-"))
        settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=cls.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        cls.client = TestClient(create_app(settings), base_url="http://127.0.0.1")
        cls.client.__enter__()
        project = cls.client.post("/api/projects", json={"name": "matrix gate", "description": "QS-C22 real.", "purpose_scope": RESEARCH_SCOPE}).json()
        cls.project_id = project["project_id"]
        cls.client.post(f"/api/projects/{cls.project_id}/open")
        imported = cls.client.post(f"/api/projects/{cls.project_id}/datasets/import-registered", json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False})
        cls.dataset_id = imported.json()["dataset_id"]
        frozen = cls.client.post(f"/api/projects/{cls.project_id}/datasets/{cls.dataset_id}/protocols/freeze-lfw-view2").json()
        cls.protocol_id = frozen["protocol_id"]
        # Reach the live store/engine of the app to build the job in-process.
        from qualsight.api.store import RunStore
        from qualsight.execution.registry_binding import load_resource_registry

        cls.registry = load_resource_registry(ROOT / "resources" / "resource-registry.json")
        cls.run_store = RunStore(cls.data_dir / "qualsight.sqlite3")
        cls.project = cls.run_store.project_head(cls.project_id)[0]
        cls.dataset = cls.run_store.dataset(cls.dataset_id)
        cls.protocol = cls.run_store.protocol(cls.protocol_id)
        fixture = json.loads((ROOT / "resources" / "manifests" / "lfw-subset-fixture-v1.json").read_text(encoding="utf-8"))
        wanted = {p["pair_id"] for p in fixture["pairs"]}
        cls.pairs = [pair for pair in cls.run_store.protocol_pairs(cls.protocol_id, limit=10000) if pair.pair_id in wanted]
        assert len(cls.pairs) == 10
        upstream = UpstreamSources(ROOT / "resources" / "manifests" / "matcher-upstream-sources-v1.json", ROOT / "var")
        upstream.acquire()
        cls.job = MatcherMatrixJob(
            registry=cls.registry, registry_path=ROOT / "resources" / "resource-registry.json", repository_root=ROOT, upstream=upstream,
            store=cls.run_store, project=cls.project, dataset=cls.dataset, protocol=cls.protocol, pairs=cls.pairs,
            job_root=cls.data_dir / "matrix", selection_rule="the ten registered subset-fixture pairs of the frozen protocol (test gate)",
        )
        cls.leaves = cls.job.run()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.run_store.close()
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def test_twelve_source_image_leaves_keep_every_pair_with_score_or_explicit_failure(self) -> None:
        self.assertEqual(len(self.leaves), 12)
        expected = {
            leaf_id
            for (matcher, condition), leaf_id in LEAF_IDS.items()
            if matcher in MatcherId and condition in SCRFD_CONDITIONS
        }
        self.assertEqual({leaf.leaf_id for leaf in self.leaves}, expected)
        self.assertIsNotNone(self.job.template_set_sha256)
        self.assertEqual(list(self.job.job_dir.glob(".*-crops.npy")), [])
        for leaf in self.leaves:
            with self.subTest(leaf=leaf.leaf_id):
                table = pq.read_table(leaf.comparisons_parquet).to_pylist()
                self.assertEqual(len(table), 10)
                self.assertEqual({row["pair_id"] for row in table}, {p.pair_id for p in self.pairs})
                for row in table:
                    self.assertEqual((row["score"] is None), (row["failure"] is not None), row)
                self.assertEqual(leaf.scored_count + leaf.failed_count, 10)
                run = leaf.run
                self.assertIsNone(run.quality_engine)
                self.assertEqual(run.matcher.component_id, leaf.matcher_id.value)
                self.assertEqual(run.matcher_preprocessing.component_id, leaf.condition_id.value)
                self.assertEqual(len(run.model_hashes), 2)
                configuration = json.loads(run.configuration.text)
                self.assertEqual(
                    configuration["template_storage"]["kind"],
                    "NUMPY_DISK_BACKED_MEMMAP_V1",
                )
                self.assertEqual(
                    configuration["template_storage"]["write_verification"],
                    "SHA256_MATCHES_PREPROCESS_OUTPUT",
                )
                self.assertEqual(configuration["pair_selection"]["pair_count"], 10)
                self.assertFalse(configuration["pair_selection"]["complete_protocol"])
                self.assertEqual(configuration["preprocessing"]["condition_id"], leaf.condition_id.value)
                self.assertEqual(configuration["matcher"]["resource_id"], leaf.matcher_id.value)
                # Persisted and re-readable as an immutable Run with its artifacts.
                stored = self.run_store.run(run.run_id)
                self.assertIsNotNone(stored)
                self.assertEqual({a.uri for a in self.run_store.artifacts_for_run(run.run_id)}, {a.uri for a in run.artifacts})
                recorded = self.run_store.matcher_leaf(run.run_id)
                self.assertEqual(recorded["leaf_id"], leaf.leaf_id)
                self.assertEqual(recorded["comparison_count"], 10)

    def test_matrix_axes_reject_empty_and_duplicate_catalog_selections(self) -> None:
        invalid_axes = (
            ("empty matchers", (), tuple(ConditionId)),
            ("empty preprocessing", tuple(MatcherId), ()),
            ("duplicate matcher", (MatcherId.ADAFACE_IR101, MatcherId.ADAFACE_IR101), (ConditionId.ADAPTIVE_LARGEST_JPEG95,)),
            ("duplicate preprocessing", (MatcherId.ADAFACE_IR101,), (ConditionId.ADAPTIVE_LARGEST_JPEG95, ConditionId.ADAPTIVE_LARGEST_JPEG95)),
        )
        for index, (label, matchers, conditions) in enumerate(invalid_axes):
            with self.subTest(case=label), self.assertRaises(MatrixError):
                MatcherMatrixJob(
                    registry=self.registry,
                    registry_path=ROOT / "resources" / "resource-registry.json",
                    repository_root=ROOT,
                    upstream=self.job.upstream,
                    store=self.run_store,
                    project=self.project,
                    dataset=self.dataset,
                    protocol=self.protocol,
                    pairs=self.pairs,
                    job_root=self.data_dir / f"invalid-matrix-{index}",
                    matchers=matchers,
                    conditions=conditions,
                    selection_rule="the real registered invalid-axis contract gate",
                )

    def test_conditions_and_matchers_are_separate_identities_never_pooled(self) -> None:
        by_key = {(leaf.matcher_id, leaf.condition_id): leaf for leaf in self.leaves}
        for matcher_id in MatcherId:
            adaptive = pq.read_table(by_key[(matcher_id, ConditionId.ADAPTIVE_LARGEST_JPEG95)].comparisons_parquet).to_pylist()
            fixed = pq.read_table(by_key[(matcher_id, ConditionId.FIXED640_HIGHEST_INMEMORY)].comparisons_parquet).to_pylist()
            scores_a = [row["score"] for row in adaptive]
            scores_f = [row["score"] for row in fixed]
            # Same pairs, different preprocessing → different (but close) scores; never identical arrays.
            self.assertEqual([r["pair_id"] for r in adaptive], [r["pair_id"] for r in fixed])
            self.assertNotEqual(scores_a, scores_f)
            self.assertNotEqual(by_key[(matcher_id, ConditionId.ADAPTIVE_LARGEST_JPEG95)].run.experiment_id, by_key[(matcher_id, ConditionId.FIXED640_HIGHEST_INMEMORY)].run.experiment_id)
            self.assertNotEqual(by_key[(matcher_id, ConditionId.ADAPTIVE_LARGEST_JPEG95)].run.run_id, by_key[(matcher_id, ConditionId.FIXED640_HIGHEST_INMEMORY)].run.run_id)
        # Same condition, different matchers → different Runs.
        run_ids = {leaf.run.run_id for leaf in self.leaves}
        self.assertEqual(len(run_ids), 12)

    def test_metrics_reconcile_to_an_independent_recomputation(self) -> None:
        for leaf in self.leaves:
            with self.subTest(leaf=leaf.leaf_id):
                rows = pq.read_table(leaf.comparisons_parquet).to_pylist()
                scores = np.array([r["score"] if r["score"] is not None else np.nan for r in rows])
                mated = np.array([r["label"] == "MATED" for r in rows])
                failed = np.array([r["failure"] is not None for r in rows])
                folds = np.array([r["fold"] for r in rows])
                # Independent EER on the ten pairs: sweep every recorded score as threshold.
                completed = ~failed
                best = None
                for threshold in np.unique(scores[completed]):
                    accept = np.zeros_like(failed, dtype=bool)
                    accept[completed] = scores[completed] >= threshold
                    fmr = np.sum(accept & ~mated & completed) / max(1, np.sum(~mated & completed))
                    fnmr = np.sum(~accept & mated & completed) / max(1, np.sum(mated & completed))
                    if best is None or abs(fmr - fnmr) < best[0]:
                        best = (abs(fmr - fnmr), fmr, fnmr, threshold)
                metrics = leaf.metrics
                policy = metrics["by_policy"]["EXCLUDE_FAILED_TRIALS"]
                self.assertEqual(metrics["counts"]["trials"], 10)
                self.assertEqual(metrics["counts"]["mated_trials"], 5)
                self.assertEqual(metrics["counts"]["failed_trials"], int(failed.sum()))
                # On the fixture the mated scores exceed every non-mated score, so EER is 0 under both computations.
                self.assertEqual(policy["eer"]["eer"], 0.0)
                self.assertEqual(best[1], 0.0)
                self.assertEqual(best[2], 0.0)
                recomputed = compute_verification_metrics(ComparisonInputs(scores, mated, failed, folds))
                self.assertEqual(recomputed["evidence_sha256"], metrics["evidence_sha256"])
                self.assertEqual(policy["operating_points"][0]["target_fmr"], 0.1)
                for point in policy["operating_points"]:
                    self.assertEqual(point["evaluation_scope"], "DESCRIPTIVE_IN_SAMPLE")
                    self.assertEqual(
                        point["threshold_selection"],
                        "SELECTED_AND_EVALUATED_ON_THE_SAME_RECORDED_TRIALS",
                    )
                    self.assertEqual(
                        point["scientific_support"]["state"], "NOT_SUPPORTED"
                    )
                    self.assertEqual(
                        point["scientific_support"]["claim_scope"],
                        "PROSPECTIVE_OR_OPERATIONAL_THRESHOLD_PERFORMANCE",
                    )
                    self.assertEqual(
                        point["event_support"]["nonmated_trials"],
                        metrics["counts"]["nonmated_trials"],
                    )
                    self.assertEqual(
                        point["event_support"]["observed_false_matches"],
                        point["false_matches"],
                    )
                    self.assertEqual(
                        point["event_support"]["rule"],
                        "THRESHOLD_PRESENT_AND_AT_LEAST_FIVE_OBSERVED_FALSE_MATCHES",
                    )
                    self.assertEqual(
                        point["event_support"]["minimum_observed_false_matches"],
                        5,
                    )
                    self.assertEqual(
                        point["event_support"]["supported"],
                        point["threshold"] is not None
                        and point["false_matches"] >= 5,
                    )
                    self.assertFalse(point["event_support"]["supported"])
                    self.assertIn(
                        "FEWER_THAN_FIVE_OBSERVED_FALSE_MATCH_EVENTS_AT_RECORDED_THRESHOLD",
                        point["scientific_support"]["reasons"],
                    )
                # ROC/DET points carry the recorded thresholds and monotone error trade-off.
                det = policy["det"]["points"]
                self.assertTrue(all(0.0 <= p["fmr"] <= 1.0 and 0.0 <= p["fnmr"] <= 1.0 for p in det))
                self.assertGreaterEqual(policy["recorded_thresholds"], 1)
                run_metrics = {m.metric_id: m.value for m in leaf.run.metrics}
                self.assertEqual(run_metrics["biometric.verification.eer@1.0.0[EXCLUDE_FAILED_TRIALS]"], 0.0)
                self.assertEqual(run_metrics["comparisons.total"], 10)


if __name__ == "__main__":
    unittest.main()
