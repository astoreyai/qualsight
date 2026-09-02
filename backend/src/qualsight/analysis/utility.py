"""Quality-utility analysis Runs (QS-D30–QS-D33): one matcher Run × one quality job × one measure.

The analysis reads the recorded comparisons Parquet of a matcher leaf Run and
the recorded quality Parquet of a batch job, joins them per protocol pair
under an explicit versioned pair-quality policy, and writes an immutable
analysis Run whose artifacts (utility.json, policy.json, pair_quality.parquet)
carry every number the UI and reports show. Nothing is re-scored: the source
Runs are referenced by ID and by artifact hash.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ..domain.entities import Artifact, ArtifactKind, DataClassification, Experiment, Project, ProtocolKind, StorageBackend, VerificationProtocol
from ..domain.primitives import CanonicalJson, ComponentBinding, ComponentState
from ..domain.run import CompletedRun, LogRecord, MetricObservation, RunStatus, ScientificSupportState
from ..engines.ofiq.adapter import content_id
from ..execution.prepared_output import (
    PreparedOutputError,
    create_staging_directory,
    discard_staging_directory,
    load_prepared_output,
    prepare_and_publish,
)
from ..metrics.policy import THRESHOLD_POLICY_DEFINITION, analyze_policy, retention_matched_cutoff
from ..metrics.utility import (
    PAIR_QUALITY_POLICY_VERSION,
    POLICY_DEFINITIONS,
    PairQualityPolicy,
    UtilityInputs,
    compute_utility_analysis,
    pair_quality,
)
from ..metrics.verification import operating_point_with_support
from ..matchers.conditions import ConditionId, SCRFD_CONDITIONS
from ..provenance import application_version, environment_identity, hardware_identity, repository_identity, sha256_bytes, sha256_file

ANALYSIS_KIND = "QUALITY_UTILITY"
ANALYSIS_VERSION = "1.2.0"
OVERLAY_IDENTITY_VERSION = "1.0.0"
DEFAULT_MEASURE = "UnifiedQualityScore"
DEFAULT_QUALITY_RANGE = (0.0, 100.0)  # OFIQ scalar quality components are 0–100 integers

_OVERLAY_LOCKED_FIELDS = (
    ("project_id", "PROJECT_MISMATCH"),
    ("dataset_id", "DATASET_MISMATCH"),
    ("protocol_id", "PROTOCOL_MISMATCH"),
    ("pair_universe_sha256", "PAIR_UNIVERSE_MISMATCH"),
    ("quality_parquet_sha256", "QUALITY_PARQUET_MISMATCH"),
    ("quality_measure", "QUALITY_MEASURE_MISMATCH"),
    ("pair_quality_policy", "PAIR_QUALITY_POLICY_MISMATCH"),
    ("metric_registry", "METRIC_REGISTRY_MISMATCH"),
    ("threshold_score_semantics", "THRESHOLD_SCORE_SEMANTICS_MISMATCH"),
    ("matcher_threshold_value", "MATCHER_THRESHOLD_MISMATCH"),
    ("quality_policy_threshold", "QUALITY_POLICY_THRESHOLD_MISMATCH"),
    ("partial_interval", "PARTIAL_INTERVAL_MISMATCH"),
    ("bootstrap_replicates", "BOOTSTRAP_CONFIGURATION_MISMATCH"),
    ("seed", "BOOTSTRAP_CONFIGURATION_MISMATCH"),
)


def _canonical_sha256(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _metric_registry_identity(repository_root: Path) -> dict[str, str]:
    path = repository_root / "metrics" / "metric-registry.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UtilityAnalysisError("the metric registry definition cannot be read") from error
    registry_id = value.get("registry_id")
    version = value.get("registry_version")
    if not isinstance(registry_id, str) or not registry_id or not isinstance(version, str) or not version:
        raise UtilityAnalysisError("the metric registry identity is incomplete")
    return {
        "id": registry_id,
        "version": version,
        "definition_sha256": sha256_file(path),
    }


def classify_utility_overlay(
    baseline_summary: Mapping[str, Any],
    candidate_summary: Mapping[str, Any],
    *,
    comparison_axes: Sequence[str],
) -> dict[str, Any]:
    """Classify one recorded analysis for an explicit, evidence-compatible overlay."""

    axes = tuple(comparison_axes)
    if not axes or len(set(axes)) != len(axes) or set(axes) - {"MATCHER", "PREPROCESSING"}:
        raise ValueError("comparison_axes must be a unique non-empty subset of MATCHER and PREPROCESSING")
    baseline = baseline_summary.get("overlay_identity")
    candidate = candidate_summary.get("overlay_identity")
    required = {
        "schema_version",
        "project_id",
        "dataset_id",
        "protocol_id",
        "pair_universe_sha256",
        "comparisons_sha256",
        "quality_parquet_sha256",
        "quality_measure",
        "pair_quality_policy",
        "metric_registry",
        "threshold_score_semantics",
        "matcher_threshold_value",
        "quality_policy_threshold",
        "partial_interval",
        "bootstrap_replicates",
        "seed",
        "matcher_id",
        "preprocessing_id",
    }
    if (
        not isinstance(baseline, Mapping)
        or not isinstance(candidate, Mapping)
        or set(baseline) != required
        or set(candidate) != required
        or baseline.get("schema_version") != OVERLAY_IDENTITY_VERSION
        or candidate.get("schema_version") != OVERLAY_IDENTITY_VERSION
    ):
        return {
            "eligible": False,
            "comparison_axis": "UNKNOWN",
            "reasons": ["INCOMPLETE_OVERLAY_IDENTITY"],
        }

    matcher_changed = baseline["matcher_id"] != candidate["matcher_id"]
    preprocessing_changed = baseline["preprocessing_id"] != candidate["preprocessing_id"]
    comparison_axis = (
        "COMPOSITE"
        if matcher_changed and preprocessing_changed
        else "MATCHER"
        if matcher_changed
        else "PREPROCESSING"
        if preprocessing_changed
        else "IDENTICAL_CONFIGURATION"
    )
    reasons = [
        reason
        for field, reason in _OVERLAY_LOCKED_FIELDS
        if baseline[field] != candidate[field]
    ]
    if matcher_changed and "MATCHER" not in axes:
        reasons.append("MATCHER_AXIS_NOT_SELECTED")
    if preprocessing_changed and "PREPROCESSING" not in axes:
        reasons.append("PREPROCESSING_AXIS_NOT_SELECTED")
    if (
        not matcher_changed
        and not preprocessing_changed
        and baseline["comparisons_sha256"] != candidate["comparisons_sha256"]
    ):
        reasons.append("COMPARISONS_ARTIFACT_MISMATCH")
    return {
        "eligible": not reasons,
        "comparison_axis": comparison_axis,
        "reasons": reasons,
    }


def quality_input_stage(condition_ids: list[str] | tuple[str, ...]) -> dict[str, object]:
    """Record where OFIQ source bytes sit relative to each matcher preprocessing condition."""

    relationships = []
    for condition_id in sorted(set(condition_ids)):
        if condition_id == ConditionId.PREALIGNED_ARCFACE112.value:
            relationship = "SAME_PUBLISHER_PREALIGNED_IMAGE_DECODED_WITHOUT_FACE_DETECTION_REALIGNMENT_OR_CROP"
        elif condition_id in {condition.value for condition in SCRFD_CONDITIONS}:
            relationship = "PRECEDES_QUALSIGHT_EXECUTED_FACE_DETECTION_ALIGNMENT_AND_CROP"
        else:
            relationship = "MATCHER_INPUT_ARTIFACT_AND_PREPROCESSING_RELATIONSHIP_NOT_RECORDED"
        relationships.append(
            {"condition_id": condition_id, "relationship": relationship}
        )
    return {
        "stage_id": "REGISTERED_DATASET_SAMPLE_SOURCE_ARTIFACT_AS_QUALITY_ENGINE_INPUT",
        "artifact_kind": "DATASET_SAMPLE_SOURCE_BYTES",
        "matcher_preprocessing_relationships": relationships,
    }


class UtilityAnalysisError(RuntimeError):
    """Raised when the analysis cannot be executed faithfully on the recorded inputs."""


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def pair_quality_policies() -> list[dict[str, str]]:
    """The versioned pair-quality policy registry (PRD §21; QS-D30)."""

    return [{"id": policy.value, "version": PAIR_QUALITY_POLICY_VERSION, "definition": POLICY_DEFINITIONS[policy]} for policy in PairQualityPolicy]


@dataclass(frozen=True, slots=True)
class UtilityRequest:
    matcher_run_id: str
    quality_job_id: str
    quality_measure: str = DEFAULT_MEASURE
    pair_quality_policy: str = PairQualityPolicy.MIN.value
    target_fmr: float = 0.001
    matcher_threshold: float | None = None
    quality_threshold: float | None = None
    partial_interval: float = 0.2
    bootstrap_replicates: int = 1000
    seed: int = 20260817


@dataclass(frozen=True, slots=True)
class UtilityResult:
    analysis_id: str
    run: CompletedRun
    experiment: Experiment
    utility: dict[str, Any]
    policy: dict[str, Any]
    directory: Path
    pair_table: Path
    utility_path: Path
    policy_path: Path
    summary: dict[str, Any]


UTILITY_ARTIFACT_NAMES = ("pair_quality.parquet", "policy.json", "utility.json")


def recover_prepared_utility_analysis(
    directory: Path,
    *,
    analysis_id: str,
    project_id: str,
    request_sha256: str,
) -> UtilityResult:
    """Reconstruct a typed utility result from its validated final receipt."""

    try:
        receipt = load_prepared_output(
            directory,
            operation_kind="UTILITY_ANALYSIS",
            result_id=analysis_id,
            project_id=project_id,
            request_sha256=request_sha256,
            artifact_names=UTILITY_ARTIFACT_NAMES,
        )
        record = receipt["record"]
        if set(record) != {"experiment", "run", "summary"}:
            raise UtilityAnalysisError("prepared utility result contract differs")
        run = CompletedRun.model_validate_json(
            json.dumps(record["run"], separators=(",", ":"), ensure_ascii=False)
        )
        experiment = Experiment.model_validate_json(
            json.dumps(record["experiment"], separators=(",", ":"), ensure_ascii=False)
        )
        summary = record["summary"]
        pair_table = directory / "pair_quality.parquet"
        utility_path = directory / "utility.json"
        policy_path = directory / "policy.json"
        utility = json.loads(utility_path.read_text(encoding="utf-8"))
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        expected_paths = {str(pair_table), str(utility_path), str(policy_path)}
        if (
            run.project_id != project_id
            or experiment.project_id != project_id
            or summary.get("analysis_id") != analysis_id
            or summary.get("run_id") != run.run_id
            or {artifact.uri for artifact in run.artifacts} != expected_paths
            or any(
                sha256_file(Path(artifact.uri)) != artifact.sha256
                for artifact in run.artifacts
            )
        ):
            raise UtilityAnalysisError("prepared utility result identity differs")
        return UtilityResult(
            analysis_id,
            run,
            experiment,
            utility,
            policy,
            directory,
            pair_table,
            utility_path,
            policy_path,
            summary,
        )
    except (PreparedOutputError, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        if isinstance(error, UtilityAnalysisError):
            raise
        raise UtilityAnalysisError(
            "prepared utility result failed integrity verification"
        ) from error


def quality_columns(schema: pa.Schema) -> list[str]:
    """Return every scalar quality measure recorded by a quality Parquet schema."""

    return sorted({name[: -len(".scalar")] for name in schema.names if name.endswith(".scalar")})


def _quality_values(parquet_path: str, measure: str) -> tuple[dict[str, float], int]:
    columns = ["sample_id", "outcome", f"{measure}.scalar", f"{measure}.failed"]
    table = pq.read_table(parquet_path, columns=columns)
    values: dict[str, float] = {}
    measured = 0
    for record in table.to_pylist():
        if record["outcome"] != "MEASURED":
            continue
        measured += 1
        if record[f"{measure}.failed"] or record[f"{measure}.scalar"] is None:
            continue
        values[record["sample_id"]] = float(record[f"{measure}.scalar"])
    return values, measured


class UtilityAnalysisJob:
    def __init__(
        self,
        *,
        store: Any,
        repository_root: Path,
        project: Project,
        actor: str,
        request: UtilityRequest,
        job_root: Path,
    ) -> None:
        self.store = store
        self.repository_root = repository_root
        self.project = project
        self.actor = actor
        self.request = request
        self.job_root = job_root
        leaf = store.matcher_leaf(request.matcher_run_id)
        if leaf is None:
            raise UtilityAnalysisError("matcher run is not recorded")
        if leaf["project_id"] != project.project_id:
            raise UtilityAnalysisError("matcher run belongs to another project")
        job = store.job(request.quality_job_id)
        if job is None or not job.get("parquet_path") or job.get("state") != "COMPLETED":
            raise UtilityAnalysisError("quality job is not a completed recorded job")
        if job["project_id"] != project.project_id:
            raise UtilityAnalysisError("quality job belongs to another project")
        if job["dataset_id"] != leaf["dataset_id"]:
            raise UtilityAnalysisError("quality job and matcher run cover different datasets")
        quality_parquet = Path(job["parquet_path"])
        self.quality_parquet_sha256 = sha256_file(quality_parquet)
        if (
            not isinstance(job.get("parquet_sha256"), str)
            or job["parquet_sha256"] != self.quality_parquet_sha256
        ):
            raise UtilityAnalysisError("quality job Parquet failed recorded hash verification")
        comparisons_parquet = Path(leaf["comparisons_parquet"])
        if sha256_file(comparisons_parquet) != leaf["comparisons_sha256"]:
            raise UtilityAnalysisError("matcher comparisons Parquet failed recorded hash verification")
        matcher_run = store.run(request.matcher_run_id)
        quality_run = store.run(job["run_id"]) if job.get("run_id") else None
        if matcher_run is None or quality_run is None:
            raise UtilityAnalysisError("source Runs are not recorded")
        try:
            self.policy = PairQualityPolicy(request.pair_quality_policy)
        except ValueError as error:
            raise UtilityAnalysisError(f"unknown pair-quality policy {request.pair_quality_policy}") from error
        schema = pq.read_schema(job["parquet_path"])
        self.available_measures = quality_columns(schema)
        if request.quality_measure not in self.available_measures:
            raise UtilityAnalysisError(f"quality measure {request.quality_measure} is not a scalar column of the quality job")
        if not (0 < request.partial_interval <= 1):
            raise UtilityAnalysisError("partial interval must lie in (0, 1]")
        if request.bootstrap_replicates < 0 or request.bootstrap_replicates > 100_000:
            raise UtilityAnalysisError("bootstrap replicates out of range")
        protocol = store.protocol(leaf["protocol_id"])
        if protocol is None:
            raise UtilityAnalysisError("protocol of the matcher run is not recorded")
        self.protocol = protocol
        self.metric_registry = _metric_registry_identity(repository_root)
        self.leaf = leaf
        self.quality_job = job
        self.matcher_run: CompletedRun = matcher_run[0]
        self.quality_run: CompletedRun = quality_run[0]
        self.metrics = json.loads(leaf["metrics_json"])
        self.analysis_id = content_id(
            "analysis", ANALYSIS_KIND, ANALYSIS_VERSION, request.matcher_run_id, request.quality_job_id, request.quality_measure, self.policy.value,
            json.dumps({"target_fmr": request.target_fmr, "matcher_threshold": request.matcher_threshold, "quality_threshold": request.quality_threshold, "partial_interval": request.partial_interval, "replicates": request.bootstrap_replicates, "seed": request.seed}, sort_keys=True),
        )
        self.directory = job_root / self.analysis_id.replace(":", "_")

    # -- execution ------------------------------------------------------------

    def run(self, *, request_sha256: str | None = None) -> UtilityResult:
        started = _now()
        clock = time.monotonic()
        request = self.request
        rows = pq.read_table(self.leaf["comparisons_parquet"]).to_pylist()
        quality, measured = _quality_values(self.quality_job["parquet_path"], request.quality_measure)
        scores = np.array([r["score"] if r["score"] is not None else np.nan for r in rows], dtype=np.float64)
        mated = np.array([r["label"] == "MATED" for r in rows])
        failed = np.array([r["failure"] is not None for r in rows])
        q_left = np.array([quality.get(r["left_sample_id"], np.nan) for r in rows], dtype=np.float64)
        q_right = np.array([quality.get(r["right_sample_id"], np.nan) for r in rows], dtype=np.float64)
        subjects_recorded = all(
            r["left_subject_id"] is not None and r["right_subject_id"] is not None
            for r in rows
        )
        subject_left = np.array([r["left_subject_id"] for r in rows], dtype=object)
        subject_right = np.array([r["right_subject_id"] for r in rows], dtype=object)
        uncertainty_sampling_unit = (
            "SUBJECT_ID_IN_EITHER_PAIR_POSITION"
            if subjects_recorded
            else None
        )
        if request.matcher_threshold is None:
            point = next((p for p in self.metrics["by_policy"]["EXCLUDE_FAILED_TRIALS"]["operating_points"] if p["target_fmr"] == request.target_fmr and p.get("threshold") is not None), None)
            if point is None:
                raise UtilityAnalysisError(f"no recorded operating point reaches target FMR {request.target_fmr}")
            point = operating_point_with_support(request.target_fmr, point)
            threshold = float(point["threshold"])
            provenance = {
                "source": "RECORDED_OPERATING_POINT",
                "kind": "MATCHER_THRESHOLD",
                "target_fmr": request.target_fmr,
                "failure_policy": "EXCLUDE_FAILED_TRIALS",
                "fmr_at_threshold": point["fmr"],
                "fnmr_at_threshold": point["fnmr"],
                "matcher_run_id": request.matcher_run_id,
                "evaluation_scope": point["evaluation_scope"],
                "threshold_selection": point["threshold_selection"],
                "event_support": point["event_support"],
                "scientific_support": point["scientific_support"],
            }
        else:
            threshold = float(request.matcher_threshold)
            usable_nonmated = (~mated) & (~failed) & np.isfinite(scores)
            provenance = {
                "source": "CALLER_SUPPLIED",
                "kind": "MATCHER_THRESHOLD",
                "evaluation_scope": "DESCRIPTIVE_APPLICATION_TO_RECORDED_TRIALS",
                "threshold_selection": "CALLER_SUPPLIED_SELECTION_EVIDENCE_NOT_RECORDED",
                "event_support": {
                    "rule": "NO_TARGET_FMR_SELECTION_EVIDENCE_RECORDED",
                    "nonmated_trials": int(np.sum(usable_nonmated)),
                    "observed_false_matches": int(
                        np.sum(scores[usable_nonmated] >= threshold)
                    ),
                    "supported": False,
                },
                "scientific_support": {
                    "state": "NOT_SUPPORTED",
                    "claim_scope": "PROSPECTIVE_OR_OPERATIONAL_THRESHOLD_PERFORMANCE",
                    "reasons": [
                        "CALLER_SUPPLIED_THRESHOLD_SELECTION_EVIDENCE_NOT_RECORDED",
                        "DESCRIPTIVE_APPLICATION_TO_RECORDED_TRIALS_ONLY",
                    ],
                },
            }
        pq_values = pair_quality(q_left, q_right, self.policy)
        usable_mated = (~failed) & np.isfinite(scores) & np.isfinite(pq_values) & mated
        if request.quality_threshold is None:
            # Default: the pair-quality cutoff at the partial-interval rejection fraction of the mated units.
            if not np.any(usable_mated):
                raise UtilityAnalysisError("no usable mated comparison units")
            ordered = np.sort(pq_values[usable_mated])
            index = min(int(np.floor(request.partial_interval * ordered.shape[0])), ordered.shape[0] - 1)
            quality_threshold = float(ordered[index])
            quality_provenance = {"source": "DERIVED_PAIR_QUALITY_AT_PARTIAL_INTERVAL_REJECTION", "rejection_fraction": request.partial_interval, "kind": "QUALITY_POLICY_THRESHOLD"}
        else:
            quality_threshold = float(request.quality_threshold)
            quality_provenance = {"source": "CALLER_SUPPLIED", "kind": "QUALITY_POLICY_THRESHOLD"}
        overlay_identity = {
            "schema_version": OVERLAY_IDENTITY_VERSION,
            "project_id": self.project.project_id,
            "dataset_id": self.leaf["dataset_id"],
            "protocol_id": self.leaf["protocol_id"],
            "pair_universe_sha256": self.protocol.pairs_sha256,
            "comparisons_sha256": self.leaf["comparisons_sha256"],
            "quality_parquet_sha256": self.quality_parquet_sha256,
            "quality_measure": request.quality_measure,
            "pair_quality_policy": {
                "id": self.policy.value,
                "version": PAIR_QUALITY_POLICY_VERSION,
                "definition_sha256": _canonical_sha256(
                    {
                        "id": self.policy.value,
                        "version": PAIR_QUALITY_POLICY_VERSION,
                        "definition": POLICY_DEFINITIONS[self.policy],
                    }
                ),
            },
            "metric_registry": self.metric_registry,
            "threshold_score_semantics": {
                "threshold_kind": provenance["kind"],
                "threshold_source": provenance["source"],
                "threshold_selection": provenance["threshold_selection"],
                "target_fmr": provenance.get("target_fmr"),
                "failure_policy": provenance.get("failure_policy"),
                "evaluation_scope": provenance["evaluation_scope"],
                "score_direction": "HIGHER_IS_MORE_SIMILAR",
                "equality_rule": "ACCEPT_WHEN_SCORE_GE_THRESHOLD",
            },
            "matcher_threshold_value": threshold,
            "quality_policy_threshold": {
                "value": quality_threshold,
                **quality_provenance,
            },
            "partial_interval": request.partial_interval,
            "bootstrap_replicates": request.bootstrap_replicates,
            "seed": request.seed,
            "matcher_id": self.leaf["matcher_id"],
            "preprocessing_id": self.leaf["condition_id"],
        }
        quality_range = DEFAULT_QUALITY_RANGE if request.quality_measure in self.available_measures else None
        utility = compute_utility_analysis(
            UtilityInputs(scores, mated, failed, q_left, q_right, subject_left, subject_right),
            policy=self.policy, threshold=threshold, threshold_provenance=provenance, partial_interval=request.partial_interval,
            quality_range=quality_range, bootstrap_replicates=request.bootstrap_replicates, seed=request.seed, quality_cutoff_for_prediction=quality_threshold,
            uncertainty_sampling_unit=uncertainty_sampling_unit,
        )
        policy_block = analyze_policy(scores, mated, failed, pq_values, quality_threshold=quality_threshold, matcher_threshold=threshold, quality_measure=request.quality_measure, pair_quality_policy=self.policy.value)
        policy_block["thresholds"]["quality_policy"]["provenance"] = quality_provenance
        policy_block["thresholds"]["matcher"]["provenance"] = provenance
        # Per-quality-measure impact at the same retention (PRD §24), for every scalar measure of the job.
        retention = policy_block["retention_rate"]
        per_measure = []
        if retention is not None:
            for measure in self.available_measures:
                values, _ = _quality_values(self.quality_job["parquet_path"], measure)
                m_left = np.array([values.get(r["left_sample_id"], np.nan) for r in rows], dtype=np.float64)
                m_right = np.array([values.get(r["right_sample_id"], np.nan) for r in rows], dtype=np.float64)
                m_pair = pair_quality(m_left, m_right, self.policy)
                usable = (~failed) & np.isfinite(scores) & np.isfinite(m_pair)
                cutoff = retention_matched_cutoff(m_pair[usable], retention) if np.any(usable) else None
                if cutoff is None:
                    per_measure.append({"quality_measure": measure, "status": "NO_USABLE_UNITS"})
                    continue
                block = analyze_policy(scores, mated, failed, m_pair, quality_threshold=cutoff, matcher_threshold=threshold, quality_measure=measure, pair_quality_policy=self.policy.value)
                per_measure.append({
                    "quality_measure": measure, "quality_cutoff": cutoff, "cutoff_rule": "RETENTION_MATCHED_TO_POLICY", "usable_units": block["counts"]["usable_units"],
                    "retained": block["counts"]["retained"], "retention_rate": block["retention_rate"], "errors_prevented": block["impact"]["errors_prevented"],
                    "false_non_matches_prevented": block["impact"]["false_non_matches_prevented"], "false_matches_prevented": block["impact"]["false_matches_prevented"],
                    "successful_comparisons_removed": block["impact"]["successful_comparisons_removed"],
                    "fnmr_after": block["after_policy"]["fnmr"]["rate"], "fmr_after": block["after_policy"]["fmr"]["rate"], "evidence_sha256": block["evidence_sha256"],
                })
        policy_block["per_quality_measure_impact"] = {"rule": "each measure is cut at the smallest cutoff whose retention does not exceed the policy retention; the matcher threshold is unchanged", "measures": per_measure}
        # Persist artifacts.
        effective_request_sha256 = request_sha256 or CanonicalJson.from_value(
            asdict(self.request)
        ).sha256
        stage = create_staging_directory(
            self.directory,
            operation_kind="UTILITY_ANALYSIS",
            result_id=self.analysis_id,
            project_id=self.project.project_id,
            request_sha256=effective_request_sha256,
        )
        pair_table = stage / "pair_quality.parquet"
        accept = np.where(np.isfinite(scores), scores >= threshold, False)
        table = pa.table(
            {
                "pair_id": [r["pair_id"] for r in rows], "fold": [r["fold"] for r in rows], "label": [r["label"] for r in rows],
                "left_sample_id": [r["left_sample_id"] for r in rows], "right_sample_id": [r["right_sample_id"] for r in rows],
                "left_subject_id": [r["left_subject_id"] for r in rows], "right_subject_id": [r["right_subject_id"] for r in rows],
                "score": [None if not np.isfinite(v) else float(v) for v in scores], "failure": [r["failure"] for r in rows],
                "quality_left": [None if not np.isfinite(v) else float(v) for v in q_left], "quality_right": [None if not np.isfinite(v) else float(v) for v in q_right],
                "pair_quality": [None if not np.isfinite(v) else float(v) for v in pq_values],
                "accepted_at_matcher_threshold": [None if not np.isfinite(s) else bool(a) for s, a in zip(scores, accept)],
                "retained_by_quality_policy": [None if not np.isfinite(v) else bool(v >= quality_threshold) for v in pq_values],
                "decision_error": [None if not np.isfinite(s) else bool((m and not a) or ((not m) and a)) for s, m, a in zip(scores, mated, accept)],
            }
        )
        pq.write_table(table, pair_table, compression="zstd")
        document = {
            "analysis_id": self.analysis_id, "kind": ANALYSIS_KIND, "version": ANALYSIS_VERSION,
            "overlay_identity": overlay_identity,
            "sources": {
                "matcher_run_id": request.matcher_run_id, "matcher_leaf_id": self.leaf["leaf_id"], "matcher_id": self.leaf["matcher_id"], "condition_id": self.leaf["condition_id"], "comparisons_sha256": self.leaf["comparisons_sha256"],
                "quality_job_id": request.quality_job_id, "quality_run_id": self.quality_job["run_id"], "quality_parquet_sha256": self.quality_parquet_sha256, "quality_measure": request.quality_measure, "quality_measure_column": f"{request.quality_measure}.scalar",
                "quality_input_stage": quality_input_stage([self.leaf["condition_id"]]),
                "samples_measured": measured, "samples_with_measure_value": len(quality),
            },
            "pair_quality_policy": {"id": self.policy.value, "version": PAIR_QUALITY_POLICY_VERSION, "definition": POLICY_DEFINITIONS[self.policy]},
            "quality_direction": "HIGHER_IS_BETTER",
            "matcher_threshold": {"value": threshold, **provenance},
            "quality_policy_threshold": {"value": quality_threshold, **quality_provenance},
            "utility": utility,
            "counts": {"comparison_units": len(rows), "processing_failures": int(np.sum(failed)), "units_missing_quality": int(np.sum(~failed & ~np.isfinite(pq_values)))},
        }
        utility_path = stage / "utility.json"
        utility_path.write_text(json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        policy_path = stage / "policy.json"
        policy_path.write_text(json.dumps(policy_block, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        seconds = time.monotonic() - clock
        run, experiment = self._run(started, seconds, document, policy_block, utility_path, policy_path, pair_table, threshold, quality_threshold, artifact_directory=self.directory)
        summary = {
            "analysis_id": self.analysis_id, "run_id": run.run_id, "matcher_run_id": request.matcher_run_id, "leaf_id": self.leaf["leaf_id"], "matcher_id": self.leaf["matcher_id"], "condition_id": self.leaf["condition_id"],
            "overlay_identity": overlay_identity,
            "quality_job_id": request.quality_job_id, "quality_measure": request.quality_measure, "pair_quality_policy": self.policy.value, "matcher_threshold": threshold, "quality_threshold": quality_threshold,
            "au_erc_fnmr": utility["areas"]["FNMR"]["au_erc"]["value"], "partial_au_erc_fnmr": utility["areas"]["FNMR"]["partial_au_erc"]["value"],
            "au_erc_fmr": utility["areas"]["FMR"]["au_erc"]["value"], "fnmr_before": policy_block["before_policy"]["fnmr"]["rate"], "fnmr_after": policy_block["after_policy"]["fnmr"]["rate"],
            "retention_rate": policy_block["retention_rate"], "errors_prevented": policy_block["impact"]["errors_prevented"], "successful_comparisons_removed": policy_block["impact"]["successful_comparisons_removed"],
        }
        result = UtilityResult(
            self.analysis_id,
            run,
            experiment,
            document,
            policy_block,
            self.directory,
            self.directory / pair_table.name,
            self.directory / utility_path.name,
            self.directory / policy_path.name,
            summary,
        )
        try:
            prepare_and_publish(
                stage,
                self.directory,
                operation_kind="UTILITY_ANALYSIS",
                result_id=self.analysis_id,
                project_id=self.project.project_id,
                request_sha256=effective_request_sha256,
                artifact_names=UTILITY_ARTIFACT_NAMES,
                record={
                    "run": json.loads(run.model_dump_json()),
                    "experiment": json.loads(experiment.model_dump_json()),
                    "summary": summary,
                },
            )
        except BaseException:
            discard_staging_directory(stage)
            raise
        self.store.record_utility_analysis(result)
        return result

    def _run(self, started: datetime, seconds: float, document: dict[str, Any], policy_block: dict[str, Any], utility_path: Path, policy_path: Path, pair_table: Path, threshold: float, quality_threshold: float, *, artifact_directory: Path | None = None) -> tuple[CompletedRun, Experiment]:
        request = self.request
        source = self.matcher_run
        configuration = CanonicalJson.from_value({
            "analysis_id": self.analysis_id, "kind": ANALYSIS_KIND, "version": ANALYSIS_VERSION,
            "sources": document["sources"], "pair_quality_policy": document["pair_quality_policy"],
            "matcher_threshold": document["matcher_threshold"], "quality_policy_threshold": document["quality_policy_threshold"],
            "rejection_domain": document["utility"]["rejection_domain"], "uncertainty": document["utility"]["uncertainty"],
            "quality_bins": document["utility"]["quality_conditioned"]["bin_edges"], "definitions": {
                "erc": list(document["utility"]["erc"]["definition"]), "au_erc": list(document["utility"]["areas"]["FNMR"]["au_erc"]["definition"]),
                "partial_au_erc": list(document["utility"]["areas"]["FNMR"]["partial_au_erc"]["definition"]), "risk_coverage": list(document["utility"]["risk_coverage"]["definition"]),
                "threshold_policy": list(THRESHOLD_POLICY_DEFINITION)},
        })
        experiment_id = content_id("experiment", self.project.project_id, self.leaf["dataset_id"], ANALYSIS_KIND, request.matcher_run_id, request.quality_job_id, request.quality_measure, self.policy.value)
        run_id = content_id("run", experiment_id, started.isoformat(), sha256_file(utility_path), sha256_file(policy_path), sha256_file(pair_table))
        utility = document["utility"]
        uncertainty_estimated = utility["uncertainty"].get("status") == "ESTIMATED"
        observations = [
            MetricObservation(metric_id="comparisons.total", value=document["counts"]["comparison_units"], unit="count"),
            MetricObservation(metric_id="comparisons.failed", value=document["counts"]["processing_failures"], unit="count"),
            MetricObservation(metric_id="policy.retained", value=policy_block["counts"]["retained"], unit="count"),
            MetricObservation(metric_id="policy.errors_prevented", value=policy_block["impact"]["errors_prevented"], unit="count"),
            MetricObservation(metric_id="policy.successful_comparisons_removed", value=policy_block["impact"]["successful_comparisons_removed"], unit="count"),
            MetricObservation(metric_id="timing.analysis_seconds", value=round(seconds, 3), unit="observation"),
        ]
        for kind in ("FNMR", "FMR"):
            for name in ("au_erc", "partial_au_erc"):
                value = utility["areas"][kind][name]["value"]
                if value is not None:
                    metric_id, version = utility["areas"][kind][name]["definition"]
                    observations.append(MetricObservation(metric_id=f"{metric_id}@{version}[{kind}]", value=float(value), unit="area"))
            rate = policy_block["after_policy"]["fnmr" if kind == "FNMR" else "fmr"]["rate"]
            if rate is not None:
                observations.append(MetricObservation(metric_id=f"policy.{kind.lower()}_after", value=float(rate), unit="rate"))
        artifacts = []
        for path, kind, media, backend in ((utility_path, ArtifactKind.METRICS, "application/json", StorageBackend.FILESYSTEM), (policy_path, ArtifactKind.METRICS, "application/json", StorageBackend.FILESYSTEM), (pair_table, ArtifactKind.TABLE, "application/vnd.apache.parquet", StorageBackend.PARQUET)):
            digest = sha256_file(path)
            artifact_path = (artifact_directory / path.name) if artifact_directory is not None else path
            artifacts.append(Artifact(artifact_id=content_id("artifact", run_id, digest, path.name), run_id=run_id, kind=kind, storage_backend=backend, uri=str(artifact_path), sha256=digest, size_bytes=path.stat().st_size, media_type=media, classification=DataClassification.SENSITIVE_BIOMETRIC_DERIVED, created_at=_now()))
        run = CompletedRun(
            run_id=run_id, project_id=self.project.project_id, experiment_id=experiment_id, user_id=self.actor,
            dataset_manifest=source.dataset_manifest, dataset_hash=source.dataset_hash,
            subject_protocol=source.subject_protocol, pair_search_protocol=source.pair_search_protocol,
            quality_engine=self.quality_run.quality_engine, quality_engine_configuration=self.quality_run.quality_engine_configuration,
            model_hashes=source.model_hashes, matcher=source.matcher, matcher_preprocessing=source.matcher_preprocessing,
            operating_thresholds=ComponentBinding(state=ComponentState.CONFIGURED, component_id="qualsight.metrics.policy", version="1.0.0", configuration_sha256=sha256_bytes(json.dumps({"matcher_threshold": threshold, "quality_threshold": quality_threshold, "pair_quality_policy": self.policy.value}, sort_keys=True).encode("utf-8"))),
            statistical_methods=ComponentBinding(state=ComponentState.CONFIGURED, component_id="qualsight.metrics.utility+statistics", version="1.0.0", configuration_sha256=sha256_bytes(json.dumps({"partial_interval": request.partial_interval, "replicates": request.bootstrap_replicates, "seed": request.seed}, sort_keys=True).encode("utf-8"))),
            confidence_interval_method=(
                ComponentBinding(state=ComponentState.CONFIGURED, component_id="PAIR_SUBJECT_MULTI_MEMBERSHIP_BOOTSTRAP_PERCENTILE_95+CLOPPER_PEARSON_95", version="1.0.0")
                if uncertainty_estimated
                else ComponentBinding(state=ComponentState.NOT_APPLICABLE, reason="Subject-cluster intervals were not assessed because the protocol records no subject identities; point rates retain exact binomial intervals where applicable.")
            ),
            randomness=(
                ComponentBinding(state=ComponentState.CONFIGURED, component_id="numpy.random.default_rng", version="PCG64", configuration_sha256=sha256_bytes(str(request.seed).encode("utf-8")))
                if uncertainty_estimated
                else ComponentBinding(state=ComponentState.NOT_APPLICABLE, reason="No bootstrap resampling was executed because subject identities are unavailable.")
            ),
            random_seeds=(request.seed,) if uncertainty_estimated else (),
            application_version=application_version(self.repository_root / "backend" / "pyproject.toml"), repository=repository_identity(self.repository_root),
            environment=environment_identity(self.repository_root / "uv.lock", {}), hardware=hardware_identity("numpy on CPU"),
            start_time=started, end_time=_now(), runtime_seconds=round(seconds, 3), initialization_ms=0, assessment_ms=int(seconds * 1000),
            configuration=configuration, metrics=tuple(observations), artifacts=tuple(artifacts),
            logs=(LogRecord(stream="stdout", sha256=sha256_bytes(b""), text=""), LogRecord(stream="stderr", sha256=sha256_bytes(b""), text="")),
            warnings=tuple(w for w in (
                f"{document['counts']['units_missing_quality']} comparison units lack a {request.quality_measure} value on one side and are excluded from utility curves" if document["counts"]["units_missing_quality"] else None,
                "quality-conditioned bins with fewer than 30 units are flagged as unsupported precision",
                "subject-cluster uncertainty is not assessed because the protocol authority records no subject identities" if not uncertainty_estimated else None,
            ) if w),
            provenance_limits=tuple(source.provenance_limits) + ("Utility results are QualSight-executed on the recorded matcher Run and quality job; they are not published-figure reproductions.",),
            exit_code=0, status=RunStatus.COMPLETED, scientific_support=ScientificSupportState.NOT_ASSESSED,
        )
        experiment = Experiment(
            experiment_id=experiment_id, project_id=self.project.project_id, dataset_id=self.leaf["dataset_id"],
            name=f"utility: {self.leaf['leaf_id']} × {request.quality_measure} ({self.policy.value})", created_by=self.actor, created_at=started,
            protocol=VerificationProtocol(kind=ProtocolKind.VERIFICATION, protocol_id=self.leaf["protocol_id"], pair_manifest=self.protocol.pair_manifest, identity_schema=source.subject_protocol),
            configuration=configuration,
        )
        return run, experiment
