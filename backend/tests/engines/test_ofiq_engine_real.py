"""Real OFIQ v1.1.2 engine tests: typed output, immutable Run, explicit failures.

Every assessment here launches the registered installed OFIQSampleApp on
registered bytes. The unreadable-input case uses the registered LFW archive
(a real DATA artifact that is not an image) so that the engine's own row-level
error path is observed rather than simulated.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import unittest

from qualsight.domain.run import CompletedRun
from qualsight.engines.ofiq import OFIQQualityEngine
from qualsight.engines.ofiq.adapter import OFIQEngineError
from qualsight.execution.cli_runtime import RunningExecution
from qualsight.execution.contracts import (
    ContractValidationError,
    ExecutionFailed,
    LifecycleEvent,
    LifecycleState,
)
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)


ROOT = Path(__file__).resolve().parents[3]
REGISTRY_PATH = ROOT / "resources" / "resource-registry.json"
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
CONFORMANCE_SCOPE = "REPRODUCE_OFIQ_CONFORMANCE_TESTS"
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"
R01 = "/mnt/projects/02_perception_biometrics/OFIQ-Project/data/tests/images/r-01-frontal.png"
LFW_ARCHIVE = "/mnt/projects/datasets/lfw/lfw-funneled.tgz"
ISO_MEASURES = (
    "UnifiedQualityScore",
    "BackgroundUniformity",
    "IlluminationUniformity",
    "LuminanceMean",
    "LuminanceVariance",
    "UnderExposurePrevention",
    "OverExposurePrevention",
    "DynamicRange",
    "Sharpness",
    "CompressionArtifacts",
    "NaturalColour",
    "SingleFacePresent",
    "EyesOpen",
    "MouthClosed",
    "EyesVisible",
    "MouthOcclusionPrevention",
    "FaceOcclusionPrevention",
    "InterEyeDistance",
    "HeadSize",
    "LeftwardCropOfTheFaceImage",
    "RightwardCropOfTheFaceImage",
    "MarginAboveOfTheFaceImage",
    "MarginBelowOfTheFaceImage",
    "HeadPoseYaw",
    "HeadPosePitch",
    "HeadPoseRoll",
    "ExpressionNeutrality",
    "NoHeadCoverings",
)


class RealOFIQEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = OFIQQualityEngine(
            registry_path=REGISTRY_PATH,
            repository_root=ROOT,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )
        cls.result = cls.engine.assess(R01, CONFORMANCE_SCOPE)

    def test_engine_reports_all_28_iso_measures_for_the_conformance_image(self) -> None:
        result = self.result
        self.assertTrue(result.succeeded)
        self.assertEqual(result.assessment.engine_version, "1.1.2")
        self.assertEqual(result.assessment.measure_names, ISO_MEASURES)
        self.assertEqual(len(result.row.measures), 28)
        self.assertEqual(result.row.failed_measure_names, ())
        uqs = result.row.measure("UnifiedQualityScore")
        self.assertEqual(uqs.native, 26.390884)
        self.assertEqual(uqs.scalar, 79)
        for measure in result.row.measures:
            self.assertTrue(measure.native_is_finite, measure)
            self.assertIsNotNone(measure.scalar, measure)
            self.assertGreaterEqual(measure.scalar, 0)
            self.assertLessEqual(measure.scalar, 100)
        self.assertGreater(result.assessment.initialization_ms, 0)
        self.assertGreater(result.row.assessment_ms, 0)

    def test_repeated_assessment_reproduces_every_non_timing_value(self) -> None:
        again = self.engine.assess(R01, CONFORMANCE_SCOPE)
        first = [(m.name, m.native, m.scalar, m.failed) for m in self.result.row.measures]
        second = [(m.name, m.native, m.scalar, m.failed) for m in again.row.measures]
        self.assertEqual(first, second)
        # Timing is an observation, not an equality gate.
        self.assertNotEqual(again.record.request.request_id, "")

    def test_immutable_run_binds_engine_input_models_and_provenance(self) -> None:
        bundle = self.engine.build_run(self.result, purpose_scope=CONFORMANCE_SCOPE)
        run = bundle.run
        self.assertIsInstance(run, CompletedRun)
        self.assertEqual(run.quality_engine.resource_id, OFIQ_V112_RUNTIME_RESOURCE_ID)
        self.assertEqual(run.quality_engine.version, "1.1.2")
        self.assertEqual(run.quality_engine.registry_state, "APPROVED_LOCAL_RESEARCH_USE")
        self.assertEqual(len(run.model_hashes), 12)
        self.assertEqual(len(run.metrics), 56)
        by_id = {metric.metric_id: metric.value for metric in run.metrics}
        self.assertEqual(by_id["UnifiedQualityScore.native"], 26.390884)
        self.assertEqual(by_id["UnifiedQualityScore.scalar"], 79)
        self.assertEqual(run.artifacts[0].uri, R01)
        self.assertEqual(run.artifacts[0].sha256, bundle.sample.sha256)
        self.assertEqual(run.dataset_hash, run.dataset_manifest.sha256)
        self.assertEqual(run.repository.root, str(ROOT))
        self.assertEqual(run.environment.dependency_manifest.path, str(ROOT / "uv.lock"))
        self.assertEqual(run.logs[0].stream, "stdout")
        self.assertIn("OFIQ library version: 1.1.2", run.logs[0].text)
        self.assertEqual(run.exit_code, 0)
        self.assertEqual(bundle.audit_event.subject_id, run.run_id)
        self.assertEqual(bundle.experiment.protocol.sample_ids, (bundle.sample.sample_id,))
        # Re-validating the JSON round trip proves the record is self-consistent.
        self.assertEqual(CompletedRun.model_validate_json(run.model_dump_json()), run)
        # A second capture of the same bytes yields the same content-addressed
        # project/dataset/sample/experiment identities and a distinct run_id.
        second = self.engine.build_run(
            self.engine.assess(R01, CONFORMANCE_SCOPE), purpose_scope=CONFORMANCE_SCOPE
        )
        self.assertEqual(second.project.project_id, bundle.project.project_id)
        self.assertEqual(second.experiment.experiment_id, bundle.experiment.experiment_id)
        self.assertEqual(second.sample.sample_id, bundle.sample.sample_id)
        self.assertNotEqual(second.run.run_id, run.run_id)
        self.assertEqual(
            {m.metric_id: m.value for m in second.run.metrics}, by_id
        )

    def test_unreadable_registered_input_is_an_explicit_row_failure(self) -> None:
        # The LFW archive is a registered DATA artifact sharing the research
        # scope with the runtime, but it is not an image: OFIQ reports an
        # [ERROR] on stderr and an empty result row while exiting 0.
        result = self.engine.assess(LFW_ARCHIVE, RESEARCH_SCOPE)
        self.assertEqual(result.record.state, LifecycleState.SUCCEEDED)
        self.assertEqual(result.record.exit_code, 0)
        self.assertFalse(result.succeeded)
        self.assertTrue(result.row.row_failed)
        self.assertEqual(result.row.measures, ())
        self.assertIsNotNone(result.row.failure_reason)
        self.assertIn("[ERROR]", result.row.failure_reason)
        with self.assertRaisesRegex(OFIQEngineError, r"(?i)failed"):
            self.engine.build_run(result, purpose_scope=RESEARCH_SCOPE)

    def test_conformance_image_is_refused_under_the_research_scope(self) -> None:
        with self.assertRaisesRegex(ContractValidationError, r"(?i)common approved scope"):
            self.engine.assess(R01, RESEARCH_SCOPE)

    def test_unregistered_path_is_refused_before_launch(self) -> None:
        with self.assertRaisesRegex(ContractValidationError, r"(?i)not a registered DATA artifact"):
            self.engine.assess(str(ROOT / "PRD.md"), CONFORMANCE_SCOPE)

    def test_nonzero_exit_of_the_real_engine_is_classified_as_failed(self) -> None:
        # The registry-bound adapter cannot be made to pass an invalid flag, so
        # the failure classification is exercised on a real process of the same
        # registered binary launched with an unrecognized flag (exit status 1),
        # collected through the same RunningExecution path.
        adapter = self.engine.adapter_for(R01)
        prepared = adapter.prepare(
            adapter.create_request(
                arguments=("-c", self.engine.configuration.path, "-i", R01),
                input_artifacts=(self.engine.registered_image(R01)[1],),
                purpose_scope=CONFORMANCE_SCOPE,
            )
        )
        command = (self.engine.executable.path, "--not-a-flag")
        process = subprocess.Popen(
            command,
            cwd=prepared.request.working_directory,
            env=dict(prepared.request.environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        running = RunningExecution(
            prepared=prepared,
            process=process,
            command=command,
            running_event=LifecycleEvent.observe(LifecycleState.RUNNING),
        )
        record = running.collect()
        self.assertEqual(record.state, LifecycleState.FAILED)
        self.assertEqual(record.exit_code, 1)
        self.assertIsNotNone(record.failure)
        self.assertEqual(record.failure.kind, "NONZERO_EXIT")
        self.assertIn("Unrecognized flag", record.stderr.content.decode("utf-8"))
        with self.assertRaises(ExecutionFailed):
            record.raise_for_status()


if __name__ == "__main__":
    unittest.main()
