"""QS-C20: comparison/protocol schemas and freezing the real LFW View 2 protocol.

Freezes the registered 6,000-pair LFW View 2 list over the imported dataset,
checks that every pair resolves to real recorded samples with the manifest's
digests and that labels agree with subject identity, and validates the schema
guards (label/subject consistency, score-xor-failure, provenance marker,
threshold policy) using real pair identities.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient
from pydantic import ValidationError

from qualsight.api import Settings, create_app
from qualsight.domain.comparison import (
    CandidateListEntry,
    Comparison,
    ComparisonLabel,
    ProcessingFailureKind,
    ProtocolPair,
    ProvenanceMarker,
    ScoreDirection,
    ThresholdPolicy,
    ThresholdPolicyKind,
)
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)


ROOT = Path(__file__).resolve().parents[3]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"
PAIR_MANIFEST = ROOT / "resources" / "manifests" / "lfw-local-pairs-v1.jsonl"


class RealProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-protocol-"))
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
        project = cls.client.post(
            "/api/projects", json={"name": "LFW protocol gate", "description": "QS-C20 real protocol.", "purpose_scope": RESEARCH_SCOPE}
        ).json()
        cls.project_id = project["project_id"]
        cls.client.post(f"/api/projects/{cls.project_id}/open")
        imported = cls.client.post(
            f"/api/projects/{cls.project_id}/datasets/import-registered",
            json={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
        )
        assert imported.status_code == 201, imported.text
        cls.dataset_id = imported.json()["dataset_id"]
        cls.frozen = cls.client.post(f"/api/projects/{cls.project_id}/datasets/{cls.dataset_id}/protocols/freeze-lfw-view2")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def test_frozen_protocol_binds_every_official_pair_to_recorded_samples(self) -> None:
        self.assertEqual(self.frozen.status_code, 201, self.frozen.text)
        record = self.frozen.json()
        self.assertEqual(record["pair_count"], 6000)
        self.assertEqual(record["mated_count"], 3000)
        self.assertEqual(record["nonmated_count"], 3000)
        self.assertEqual(record["fold_count"], 10)
        self.assertEqual(record["unique_sample_count"], 7701)
        self.assertEqual(record["unique_subject_count"], 4281)
        self.assertEqual(record["pair_policy"], "OFFICIAL_PROTOCOL_LIST")
        self.assertEqual(record["category"], "UNRESTRICTED_WITH_LABELED_OUTSIDE_DATA")
        self.assertEqual(record["pair_manifest"]["sha256"], hashlib.sha256(PAIR_MANIFEST.read_bytes()).hexdigest())
        self.assertNotIn("path", record["pair_manifest"])
        self.assertEqual(len(record["protocol_authority"]), 3)
        self.assertTrue(all("path" not in item for item in record["protocol_authority"]))
        self.assertTrue(all(len(item["sha256"]) == 64 for item in record["protocol_authority"]))
        head = self.client.get(f"/api/projects/{self.project_id}/protocols/{record['protocol_id']}").json()
        self.assertEqual(head["recorded_pair_count"], 6000)

        # Every pair resolves to real samples whose digests match the registered manifest.
        manifest_pairs = {}
        for line in PAIR_MANIFEST.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("record_type") == "PAIR":
                manifest_pairs[row["pair_id"]] = row
        pairs = self.client.get(f"/api/projects/{self.project_id}/protocols/{record['protocol_id']}/pairs", params={"limit": 10000}).json()
        self.assertEqual(len(pairs), 6000)
        digest = hashlib.sha256()
        for pair in pairs:
            source = manifest_pairs[pair["pair_id"]]
            self.assertEqual(pair["left_sha256"], source["left_sha256"])
            self.assertEqual(pair["right_sha256"], source["right_sha256"])
            self.assertEqual(pair["label"], source["label"])
            self.assertEqual(pair["fold"], source["fold"])
            same = pair["left_subject_id"] == pair["right_subject_id"]
            self.assertEqual(same, pair["label"] == "MATED", pair["pair_id"])
            self.assertEqual(pair["left_subject_id"], Path(source["left_path"]).parent.name)
            digest.update(ProtocolPair.model_validate_json(json.dumps(pair)).model_dump_json().encode("utf-8"))
            digest.update(b"\n")
        self.assertEqual(digest.hexdigest(), record["pairs_sha256"])
        # Samples resolve through the dataset sample endpoint.
        first = pairs[0]
        sample = self.client.get(
            f"/api/projects/{self.project_id}/samples/{first['left_sample_id']}"
        ).json()
        self.assertEqual(sample["sha256"], first["left_sha256"])
        fold_one = self.client.get(f"/api/projects/{self.project_id}/protocols/{record['protocol_id']}/pairs", params={"fold": 1, "limit": 10000}).json()
        self.assertEqual(len(fold_one), 600)
        self.assertEqual(sum(1 for p in fold_one if p["label"] == "MATED"), 300)
        # Refreezing is idempotent (same protocol id, same pairs).
        again = self.client.post(f"/api/projects/{self.project_id}/datasets/{self.dataset_id}/protocols/freeze-lfw-view2")
        self.assertEqual(again.status_code, 201, again.text)
        self.assertEqual(again.json()["protocol_id"], record["protocol_id"])
        self.assertEqual(len(self.client.get(f"/api/projects/{self.project_id}/protocols").json()), 1)

    def test_schema_guards_keep_label_score_failure_threshold_and_provenance_apart(self) -> None:
        record = self.frozen.json()
        pairs = self.client.get(f"/api/projects/{self.project_id}/protocols/{record['protocol_id']}/pairs", params={"limit": 2}).json()
        real_mated = next(p for p in pairs if p["label"] == "MATED")
        # A mated pair cannot be re-labelled non-mated (subjects agree).
        with self.assertRaises(ValidationError):
            ProtocolPair.model_validate_json(json.dumps({**real_mated, "label": "NON_MATED"}))
        # Comparisons: a not-yet-executed comparison carries a failure and no score...
        base = {
            "comparison_id": "comparison:test",
            "run_id": "run:none",
            "protocol_id": record["protocol_id"],
            "pair_id": real_mated["pair_id"],
            "label": ComparisonLabel.MATED,
            "left_sample_id": real_mated["left_sample_id"],
            "right_sample_id": real_mated["right_sample_id"],
            "score_direction": ScoreDirection.HIGHER_IS_MORE_SIMILAR,
            "provenance": ProvenanceMarker.QUALSIGHT_EXECUTED,
            "matcher_component_id": "none",
            "preprocessing_condition_id": "none",
        }
        not_executed = Comparison(score=None, failure=ProcessingFailureKind.NOT_EXECUTED, **base)
        self.assertIsNone(not_executed.score)
        # ...and can never carry both a score and a failure, or neither.
        with self.assertRaises(ValidationError):
            Comparison(score=0.5, failure=ProcessingFailureKind.FAILURE_TO_DETECT, **base)
        with self.assertRaises(ValidationError):
            Comparison(score=None, failure=None, **base)
        with self.assertRaises(ValidationError):
            Comparison(score=float("nan"), failure=None, **base)
        # Threshold policies live apart from comparisons and validate their own domain.
        ThresholdPolicy(policy_id="fmr-1e-3", kind=ThresholdPolicyKind.TARGET_FMR, value=0.001, score_direction=ScoreDirection.HIGHER_IS_MORE_SIMILAR, description="threshold at FMR 0.1 %")
        with self.assertRaises(ValidationError):
            ThresholdPolicy(policy_id="bad", kind=ThresholdPolicyKind.TARGET_FMR, value=1.5, score_direction=ScoreDirection.HIGHER_IS_MORE_SIMILAR, description="impossible target")
        # 1:N-compatible candidate entries share the sample identity model.
        with self.assertRaises(ValidationError):
            CandidateListEntry(run_id="run:none", probe_sample_id=real_mated["left_sample_id"], gallery_sample_id=real_mated["left_sample_id"], rank=1, score=1.0, score_direction=ScoreDirection.HIGHER_IS_MORE_SIMILAR, mated=True, provenance=ProvenanceMarker.QUALSIGHT_EXECUTED)

    def test_freeze_refuses_a_non_lfw_dataset(self) -> None:
        # The r-01 single-sample dataset created by an ad-hoc assessment is not the LFW resource.
        assessed = self.client.post(
            "/api/quality/assess",
            json={"registered_sample_id": self.registered_sample_id, "purpose_scope": "REPRODUCE_OFIQ_CONFORMANCE_TESTS", "project_id": self.project_id},
        )
        self.assertEqual(assessed.status_code, 200, assessed.text)
        datasets = self.client.get(f"/api/projects/{self.project_id}/datasets").json()
        other = next(d for d in datasets if d["dataset_id"] != self.dataset_id)
        refused = self.client.post(f"/api/projects/{self.project_id}/datasets/{other['dataset_id']}/protocols/freeze-lfw-view2")
        self.assertEqual(refused.status_code, 409, refused.text)


if __name__ == "__main__":
    unittest.main()
