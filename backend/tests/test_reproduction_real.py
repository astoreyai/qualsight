"""Phase-0 reproducibility gate on real bytes.

Records a real OFIQ v1.1.2 Run of the registered r-01 image, reproduces it in a
clean re-execution, verifies the reproduction is EXACT on every non-timing
metric, and exports the immutable Run manifest.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from qualsight.api.store import RunStore
from qualsight.domain.run import ReproductionState, RunAttestation, RunAttestationKind
from qualsight.engines.ofiq import OFIQQualityEngine
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)
from qualsight.reproduction import export_run_manifest, reproduce_run
from qualsight import cli


ROOT = Path(__file__).resolve().parents[2]
R01 = "/mnt/projects/02_perception_biometrics/OFIQ-Project/data/tests/images/r-01-frontal.png"
SCOPE = "REPRODUCE_OFIQ_CONFORMANCE_TESTS"


class RealReproductionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-repro-"))
        cls.store = RunStore(cls.data_dir / "qualsight.sqlite3")
        cls.engine = OFIQQualityEngine(
            registry_path=ROOT / "resources" / "resource-registry.json",
            repository_root=ROOT,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID,),
        )
        bundle = cls.engine.build_run(cls.engine.assess(R01, SCOPE), purpose_scope=SCOPE)
        cls.store.record_bundle(bundle)
        cls.run_id = bundle.run.run_id

    @classmethod
    def tearDownClass(cls) -> None:
        cls.store.close()
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def test_clean_reexecution_reproduces_the_recorded_run_exactly(self) -> None:
        report = reproduce_run(store=self.store, engine=self.engine, run_id=self.run_id)
        self.assertEqual(report.state, ReproductionState.EXACT, report.differing_metrics)
        self.assertEqual(report.compared_metrics, 56)
        self.assertEqual(report.differing_metrics, ())
        self.assertIsNotNone(report.reproduction_run_id)
        self.assertNotEqual(report.reproduction_run_id, self.run_id)
        attestation = report.attestation
        self.assertIsInstance(attestation, RunAttestation)
        self.assertEqual(attestation.kind, RunAttestationKind.REPRODUCED)
        self.assertEqual(attestation.run_id, self.run_id)
        evidence_text = json.dumps(report.evidence, sort_keys=True, separators=(",", ":"))
        self.assertEqual(attestation.evidence_sha256, hashlib.sha256(evidence_text.encode("utf-8")).hexdigest())
        self.assertEqual(report.evidence["original_model_hashes"], report.evidence["reproduction_model_hashes"])
        # The reproduction Run itself is persisted immutably.
        stored = self.store.run(report.reproduction_run_id)
        self.assertIsNotNone(stored)

    def test_exported_manifest_is_complete_and_self_consistent(self) -> None:
        out = self.data_dir / "exports" / "run-manifest.json"
        manifest = export_run_manifest(self.store, self.run_id, out)
        self.assertTrue(out.is_file())
        text = out.read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), manifest["manifest_sha256"])
        loaded = json.loads(text)
        self.assertEqual(loaded["kind"], "QUALSIGHT_IMMUTABLE_RUN_MANIFEST")
        self.assertEqual(loaded["run"]["run_id"], self.run_id)
        stored_run, stored_evidence = self.store.run(self.run_id)
        self.assertEqual(loaded["run_evidence_sha256"], stored_evidence)
        self.assertEqual(loaded["run"], json.loads(stored_run.model_dump_json()))
        self.assertEqual(loaded["project"]["project_id"], loaded["run"]["project_id"])
        self.assertEqual(loaded["experiment"]["experiment_id"], loaded["run"]["experiment_id"])
        self.assertEqual(loaded["sample"]["source_path"], R01)
        self.assertEqual(loaded["audit_events"][0]["subject_id"], self.run_id)
        self.assertEqual(len(loaded["run"]["metrics"]), 56)
        self.assertEqual(len(loaded["run"]["model_hashes"]), 12)

    def test_cli_reproduce_and_export_use_the_same_real_store(self) -> None:
        import contextlib
        import io
        import os

        env = {
            "QUALSIGHT_REPOSITORY_ROOT": str(ROOT),
            "QUALSIGHT_DATA_DIR": str(self.data_dir),
            "QUALSIGHT_INPUT_RESOURCE_IDS": OFIQ_INPUT_RESOURCE_ID,
        }
        previous = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        try:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli.main(["reproduce", "--run-id", self.run_id, "--no-record"])
            self.assertEqual(code, 0, buffer.getvalue())
            report = json.loads(buffer.getvalue())
            self.assertEqual(report["state"], "EXACT")
            self.assertEqual(report["differing_metrics"], [])
            out = self.data_dir / "exports" / "cli-manifest.json"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli.main(["export-run", "--run-id", self.run_id, "--out", str(out)])
            self.assertEqual(code, 0, buffer.getvalue())
            self.assertTrue(out.is_file())
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
