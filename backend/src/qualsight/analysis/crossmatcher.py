"""Cross-matcher and preprocessing contrasts over one recorded matrix (QS-E40–QS-E42; PRD §22).

Matcher contrasts hold the preprocessing condition fixed; preprocessing
contrasts hold the matcher and the protocol fixed. Both identities travel with
every number. The job writes an immutable analysis Run whose artifacts
(crossmatcher.json, contrast_pairs.parquet) carry every overlap count, curve,
difference, interaction coefficient, and transferred-threshold rate that the
workbench shows; nothing is re-scored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from itertools import combinations
import json
from pathlib import Path
import time
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ..domain.entities import Artifact, ArtifactKind, DataClassification, Experiment, Project, ProtocolKind, StorageBackend, VerificationProtocol
from ..domain.primitives import ArtifactIdentity as DomainArtifactIdentity, CanonicalJson, ComponentBinding, ComponentState
from ..domain.run import CompletedRun, LogRecord, MetricObservation, RunStatus, ScientificSupportState
from ..engines.ofiq.adapter import content_id
from ..execution.cli_runtime import USABLE_RESOURCE_STATES
from ..metrics.statistics import CONFIDENCE_LEVEL, clopper_pearson, cluster_bootstrap, logistic_regression_multi, roc_auc, spearman
from ..metrics.utility import ErcInputs, PAIR_QUALITY_POLICY_VERSION, POLICY_DEFINITIONS, PairQualityPolicy, _area_from_units, compute_erc, pair_quality
from ..provenance import application_version, environment_identity, hardware_identity, repository_identity, sha256_bytes, sha256_file
from .utility import _quality_values

ANALYSIS_KIND = "CROSS_MATCHER"
ANALYSIS_VERSION = "1.0.0"
CONTRAST_KINDS = {"matcher": "MATCHER_CONTRAST_FIXED_CONDITION", "preprocessing": "PREPROCESSING_CONTRAST_FIXED_MATCHER"}
CATEGORIES = ("NEITHER", "A_ONLY", "B_ONLY", "BOTH")


class CrossMatcherError(RuntimeError):
    """Raised when the contrasts cannot be executed faithfully on the recorded inputs."""


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


@dataclass(frozen=True, slots=True)
class CrossMatcherRequest:
    matrix_id: str
    quality_job_id: str
    quality_measure: str = "UnifiedQualityScore"
    pair_quality_policy: str = PairQualityPolicy.MIN.value
    target_fmr: float = 0.001
    partial_interval: float = 0.2
    bootstrap_replicates: int = 1000
    seed: int = 20260817


@dataclass(frozen=True, slots=True)
class CrossMatcherResult:
    analysis_id: str
    run: CompletedRun
    experiment: Experiment
    document: dict[str, Any]
    directory: Path
    document_path: Path
    pairs_path: Path
    summary: dict[str, Any]


@dataclass
class LeafData:
    row: dict[str, Any]
    run: CompletedRun
    pair_ids: list[str]
    labels: np.ndarray  # bool mated
    scores: np.ndarray
    failed: np.ndarray
    failure_kind: list[str | None]
    left_ids: list[str]
    right_ids: list[str]
    left_subjects: np.ndarray
    threshold: float
    threshold_provenance: dict[str, Any]


def matcher_sets(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Group recorded leaves into fixed-condition and fixed-matcher sets (QS-E40)."""

    fixed_condition: dict[str, list[dict[str, Any]]] = {}
    fixed_matcher: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        fixed_condition.setdefault(row["condition_id"], []).append({"run_id": row["run_id"], "leaf_id": row["leaf_id"], "matcher_id": row["matcher_id"]})
        fixed_matcher.setdefault(row["matcher_id"], []).append({"run_id": row["run_id"], "leaf_id": row["leaf_id"], "condition_id": row["condition_id"]})
    protocols = {row["protocol_id"] for row in rows}
    return {
        "protocol_ids": sorted(protocols),
        "same_protocol": len(protocols) == 1,
        "fixed_condition": {condition: sorted(items, key=lambda item: item["leaf_id"]) for condition, items in fixed_condition.items()},
        "fixed_matcher": {matcher: sorted(items, key=lambda item: item["leaf_id"]) for matcher, items in fixed_matcher.items()},
        "matcher_contrasts_available": sum(len(items) * (len(items) - 1) // 2 for items in fixed_condition.values()),
        "preprocessing_contrasts_available": sum(len(items) * (len(items) - 1) // 2 for items in fixed_matcher.values()),
    }


class CrossMatcherJob:
    def __init__(self, *, store: Any, registry: Any, repository_root: Path, project: Project, actor: str, request: CrossMatcherRequest, job_root: Path) -> None:
        self.store = store
        self.registry = registry
        self.repository_root = repository_root
        self.project = project
        self.actor = actor
        self.request = request
        rows = store.matcher_leaves(matrix_id=request.matrix_id)
        if len(rows) < 2:
            raise CrossMatcherError("the matrix has fewer than two recorded leaves")
        if any(row["project_id"] != project.project_id for row in rows):
            raise CrossMatcherError("matrix belongs to another project")
        sets = matcher_sets(rows)
        if not sets["same_protocol"]:
            raise CrossMatcherError("leaves do not share one protocol")
        if sets["matcher_contrasts_available"] == 0:
            raise CrossMatcherError("no two matchers share a preprocessing condition")
        for row in rows:
            state = registry.raw_resource(row["matcher_id"])["state"]
            if state not in USABLE_RESOURCE_STATES:
                raise CrossMatcherError(f"matcher {row['matcher_id']} is in registry state {state}; not usable")
        job = store.job(request.quality_job_id)
        if job is None or not job.get("parquet_path") or job.get("state") != "COMPLETED" or job["project_id"] != project.project_id:
            raise CrossMatcherError("quality job is not a completed recorded job of this project")
        if any(job["dataset_id"] != row["dataset_id"] for row in rows):
            raise CrossMatcherError("quality job and matrix cover different datasets")
        try:
            self.policy = PairQualityPolicy(request.pair_quality_policy)
        except ValueError as error:
            raise CrossMatcherError(f"unknown pair-quality policy {request.pair_quality_policy}") from error
        protocol = store.protocol(rows[0]["protocol_id"])
        if protocol is None:
            raise CrossMatcherError("protocol is not recorded")
        self.protocol = protocol
        self.rows = sorted(rows, key=lambda row: row["leaf_id"])
        self.sets = sets
        self.quality_job = job
        quality_run = store.run(job["run_id"]) if job.get("run_id") else None
        if quality_run is None:
            raise CrossMatcherError("quality Run is not recorded")
        self.quality_run: CompletedRun = quality_run[0]
        self.analysis_id = content_id(
            "analysis", ANALYSIS_KIND, ANALYSIS_VERSION, request.matrix_id, request.quality_job_id, request.quality_measure, self.policy.value,
            json.dumps({"target_fmr": request.target_fmr, "partial_interval": request.partial_interval, "replicates": request.bootstrap_replicates, "seed": request.seed}, sort_keys=True),
        )
        self.directory = job_root / self.analysis_id.replace(":", "_")

    # -- loading ----------------------------------------------------------------

    def _load_leaf(self, row: dict[str, Any]) -> LeafData:
        run = self.store.run(row["run_id"])
        if run is None:
            raise CrossMatcherError(f"leaf Run {row['run_id']} is not recorded")
        records = pq.read_table(row["comparisons_parquet"]).to_pylist()
        metrics = json.loads(row["metrics_json"])
        point = next((p for p in metrics["by_policy"]["EXCLUDE_FAILED_TRIALS"]["operating_points"] if p["target_fmr"] == self.request.target_fmr and p.get("threshold") is not None), None)
        if point is None:
            raise CrossMatcherError(f"leaf {row['leaf_id']} has no recorded operating point at FMR {self.request.target_fmr}")
        return LeafData(
            row=row, run=run[0], pair_ids=[r["pair_id"] for r in records],
            labels=np.array([r["label"] == "MATED" for r in records]),
            scores=np.array([r["score"] if r["score"] is not None else np.nan for r in records], dtype=np.float64),
            failed=np.array([r["failure"] is not None for r in records]),
            failure_kind=[r["failure"] for r in records],
            left_ids=[r["left_sample_id"] for r in records], right_ids=[r["right_sample_id"] for r in records],
            left_subjects=np.array([r["left_subject_id"] for r in records]),
            threshold=float(point["threshold"]),
            threshold_provenance={"source": "RECORDED_OPERATING_POINT", "kind": "MATCHER_THRESHOLD", "target_fmr": self.request.target_fmr, "failure_policy": "EXCLUDE_FAILED_TRIALS", "fmr_at_threshold": point["fmr"], "fnmr_at_threshold": point["fnmr"], "matcher_run_id": row["run_id"]},
        )

    # -- contrasts --------------------------------------------------------------

    def _contrast(self, kind: str, a: LeafData, b: LeafData, quality: np.ndarray, quality_left: np.ndarray, quality_right: np.ndarray, pair_rows: list[dict[str, Any]]) -> dict[str, Any]:
        request = self.request
        contrast_id = f"{a.row['leaf_id']}~{b.row['leaf_id']}"
        both_scored = (~a.failed) & (~b.failed) & np.isfinite(a.scores) & np.isfinite(b.scores)
        mated = a.labels
        error_a = np.where(mated, a.scores < a.threshold, a.scores >= a.threshold)
        error_b = np.where(mated, b.scores < b.threshold, b.scores >= b.threshold)
        error_a = np.where(np.isfinite(a.scores), error_a, False)
        error_b = np.where(np.isfinite(b.scores), error_b, False)

        def overlap(mask: np.ndarray) -> dict[str, int]:
            return {
                "units": int(np.sum(mask)),
                "NEITHER": int(np.sum(mask & ~error_a & ~error_b)), "A_ONLY": int(np.sum(mask & error_a & ~error_b)),
                "B_ONLY": int(np.sum(mask & ~error_a & error_b)), "BOTH": int(np.sum(mask & error_a & error_b)),
            }

        def jaccard(mask: np.ndarray, x: np.ndarray, y: np.ndarray) -> float | None:
            union = int(np.sum(mask & (x | y)))
            return (int(np.sum(mask & x & y)) / union) if union else None

        failure_overlap = {
            "units": int(a.failed.shape[0]), "A_ONLY": int(np.sum(a.failed & ~b.failed)), "B_ONLY": int(np.sum(~a.failed & b.failed)), "BOTH": int(np.sum(a.failed & b.failed)), "NEITHER": int(np.sum(~a.failed & ~b.failed)),
        }
        # Category rows for the workbench.
        for index in range(len(a.pair_ids)):
            if a.failed[index] or b.failed[index]:
                category = "FAILURE_BOTH" if (a.failed[index] and b.failed[index]) else ("FAILURE_A" if a.failed[index] else "FAILURE_B")
            else:
                category = ("BOTH" if (error_a[index] and error_b[index]) else "A_ONLY" if error_a[index] else "B_ONLY" if error_b[index] else "NEITHER")
            pair_rows.append({
                "contrast_id": contrast_id, "contrast_kind": kind, "pair_id": a.pair_ids[index], "label": "MATED" if mated[index] else "NON_MATED",
                "left_sample_id": a.left_ids[index], "right_sample_id": a.right_ids[index], "left_subject_id": str(a.left_subjects[index]),
                "score_a": None if not np.isfinite(a.scores[index]) else float(a.scores[index]), "score_b": None if not np.isfinite(b.scores[index]) else float(b.scores[index]),
                "failure_a": a.failure_kind[index], "failure_b": b.failure_kind[index], "error_a": bool(error_a[index]), "error_b": bool(error_b[index]), "category": category,
                "quality_left": None if not np.isfinite(quality_left[index]) else float(quality_left[index]), "quality_right": None if not np.isfinite(quality_right[index]) else float(quality_right[index]),
                "pair_quality": None if not np.isfinite(quality[index]) else float(quality[index]),
            })
        # ERC overlay + AU-ERC difference (mated units scored by both, with quality).
        usable = both_scored & np.isfinite(quality)
        erc_a = compute_erc(ErcInputs(a.scores, mated, ~usable, quality_left, quality_right), policy=self.policy, threshold=a.threshold, threshold_provenance=a.threshold_provenance)
        erc_b = compute_erc(ErcInputs(b.scores, mated, ~usable, quality_left, quality_right), policy=self.policy, threshold=b.threshold, threshold_provenance=b.threshold_provenance)
        differences: dict[str, Any] = {}
        for label, error_kind in (("FNMR", "FNMR"), ("FMR", "FMR")):
            selection = usable & (mated if error_kind == "FNMR" else ~mated)
            q = quality[selection]
            ea = (a.scores[selection] < a.threshold) if error_kind == "FNMR" else (a.scores[selection] >= a.threshold)
            eb = (b.scores[selection] < b.threshold) if error_kind == "FNMR" else (b.scores[selection] >= b.threshold)
            clusters = a.left_subjects[selection]
            for name, upper in (("au_erc", 1.0), ("partial_au_erc", request.partial_interval)):
                value_a = _area_from_units(q, ea, upper)
                value_b = _area_from_units(q, eb, upper)
                boot = cluster_bootstrap(clusters, lambda idx, ea=ea, eb=eb, q=q, upper=upper: (lambda x, y: None if x is None or y is None else x - y)(_area_from_units(q[idx], ea[idx], upper), _area_from_units(q[idx], eb[idx], upper)), replicates=request.bootstrap_replicates, seed=request.seed) if q.shape[0] else None
                if boot is not None:
                    boot["method"] = "PAIRED_CLUSTER_BOOTSTRAP_PERCENTILE"
                    boot["sampling_unit"] = "LEFT_SUBJECT_ID_SHARED_BY_BOTH_RUNS"
                differences.setdefault(label, {})[name] = {"integration_domain": [0.0, upper], "a": value_a, "b": value_b, "difference_a_minus_b": None if value_a is None or value_b is None else value_a - value_b, "uncertainty": boot, "comparison_units": int(q.shape[0])}
        # Quality × matcher interaction on pooled mated units (both scored, with quality).
        selection = usable & mated
        q = quality[selection]
        design = np.column_stack((np.concatenate((q, q)), np.concatenate((np.zeros(q.shape[0]), np.ones(q.shape[0]))), np.concatenate((np.zeros(q.shape[0]), q))))
        outcome = np.concatenate(((a.scores[selection] < a.threshold).astype(np.float64), (b.scores[selection] < b.threshold).astype(np.float64)))
        interaction = logistic_regression_multi(design, outcome, ["pair_quality", "is_b", "pair_quality_x_is_b"]) if q.shape[0] else {"status": "UNSUPPORTED_BY_SAMPLE"}
        interaction["note"] = "Pooled mated units appear once per matcher; the two rows of a pair share a subject, so Wald intervals assume more independence than the design has and are labelled approximate."
        # Threshold transferability: A's threshold applied to B's scores and vice versa (own units, EXCLUDE_FAILED_TRIALS).
        def rates(leaf: LeafData, threshold: float) -> dict[str, Any]:
            scored = (~leaf.failed) & np.isfinite(leaf.scores)
            m = scored & mated
            n_ = scored & ~mated
            return {"threshold": threshold, "fnmr": clopper_pearson(int(np.sum(m & (leaf.scores < threshold))), int(np.sum(m))), "fmr": clopper_pearson(int(np.sum(n_ & (leaf.scores >= threshold))), int(np.sum(n_)))}

        transfer = {
            "a_threshold_on_b": {"kind": "MATCHER_THRESHOLD_TRANSFERRED", "learned_from": a.row["run_id"], "applied_to": b.row["run_id"], **rates(b, a.threshold), "b_own": rates(b, b.threshold)},
            "b_threshold_on_a": {"kind": "MATCHER_THRESHOLD_TRANSFERRED", "learned_from": b.row["run_id"], "applied_to": a.row["run_id"], **rates(a, b.threshold), "a_own": rates(a, a.threshold)},
        }
        # Score agreement and quality rank consistency.
        agreement = {
            "spearman_scores_mated": spearman(a.scores[both_scored & mated], b.scores[both_scored & mated]),
            "spearman_scores_non_mated": spearman(a.scores[both_scored & ~mated], b.scores[both_scored & ~mated]),
        }
        m_sel = usable & mated
        rank_consistency = {
            "quality_predicts_a_false_non_match_roc_auc": roc_auc(-quality[m_sel], (a.scores[m_sel] < a.threshold)) if np.any(m_sel) else None,
            "quality_predicts_b_false_non_match_roc_auc": roc_auc(-quality[m_sel], (b.scores[m_sel] < b.threshold)) if np.any(m_sel) else None,
            "median_pair_quality_a_false_non_matches": float(np.median(quality[m_sel & (a.scores < a.threshold)])) if np.any(m_sel & (a.scores < a.threshold)) else None,
            "median_pair_quality_b_false_non_matches": float(np.median(quality[m_sel & (b.scores < b.threshold)])) if np.any(m_sel & (b.scores < b.threshold)) else None,
            "median_pair_quality_all_mated": float(np.median(quality[m_sel])) if np.any(m_sel) else None,
        }
        return {
            "contrast_id": contrast_id, "kind": kind,
            "a": {"run_id": a.row["run_id"], "leaf_id": a.row["leaf_id"], "matcher_id": a.row["matcher_id"], "condition_id": a.row["condition_id"], "threshold": a.threshold, "threshold_provenance": a.threshold_provenance},
            "b": {"run_id": b.row["run_id"], "leaf_id": b.row["leaf_id"], "matcher_id": b.row["matcher_id"], "condition_id": b.row["condition_id"], "threshold": b.threshold, "threshold_provenance": b.threshold_provenance},
            "held_fixed": {"condition_id": a.row["condition_id"]} if kind == CONTRAST_KINDS["matcher"] else {"matcher_id": a.row["matcher_id"]},
            "protocol_id": a.row["protocol_id"],
            "error_overlap": {"mated_false_non_match": overlap(both_scored & mated), "non_mated_false_match": overlap(both_scored & ~mated), "processing_failures": failure_overlap, "note": "decision errors are counted at each Run's own recorded threshold over units scored by both Runs"},
            "failure_set_jaccard": {"mated_false_non_match": jaccard(both_scored & mated, error_a, error_b), "non_mated_false_match": jaccard(both_scored & ~mated, error_a, error_b), "processing_failures": jaccard(np.ones_like(a.failed), a.failed, b.failed)},
            "erc_overlay": {"a": erc_a, "b": erc_b, "units_note": "both curves use the mated/non-mated units scored by both Runs and carrying a pair-quality value"},
            "au_erc_differences": differences,
            "quality_matcher_interaction": interaction,
            "threshold_transferability": transfer,
            "score_agreement": agreement,
            "quality_rank_consistency": rank_consistency,
        }

    # -- run --------------------------------------------------------------------

    def run(self) -> CrossMatcherResult:
        started = _now()
        clock = time.monotonic()
        request = self.request
        leaves = [self._load_leaf(row) for row in self.rows]
        first = leaves[0]
        for leaf in leaves[1:]:
            if leaf.pair_ids != first.pair_ids or leaf.left_ids != first.left_ids or leaf.right_ids != first.right_ids:
                raise CrossMatcherError(f"leaf {leaf.row['leaf_id']} does not share the ordered pair manifest of {first.row['leaf_id']}")
        quality_values, measured = _quality_values(self.quality_job["parquet_path"], request.quality_measure)
        quality_left = np.array([quality_values.get(s, np.nan) for s in first.left_ids], dtype=np.float64)
        quality_right = np.array([quality_values.get(s, np.nan) for s in first.right_ids], dtype=np.float64)
        quality = pair_quality(quality_left, quality_right, self.policy)
        by_id = {leaf.row["run_id"]: leaf for leaf in leaves}
        pair_rows: list[dict[str, Any]] = []
        contrasts: list[dict[str, Any]] = []
        for condition, items in sorted(self.sets["fixed_condition"].items()):
            for x, y in combinations(items, 2):
                contrasts.append(self._contrast(CONTRAST_KINDS["matcher"], by_id[x["run_id"]], by_id[y["run_id"]], quality, quality_left, quality_right, pair_rows))
        for matcher, items in sorted(self.sets["fixed_matcher"].items()):
            for x, y in combinations(items, 2):
                contrasts.append(self._contrast(CONTRAST_KINDS["preprocessing"], by_id[x["run_id"]], by_id[y["run_id"]], quality, quality_left, quality_right, pair_rows))
        self.directory.mkdir(parents=True, exist_ok=False)
        pairs_path = self.directory / "contrast_pairs.parquet"
        pq.write_table(pa.Table.from_pylist(pair_rows), pairs_path, compression="zstd")
        document = {
            "analysis_id": self.analysis_id, "kind": ANALYSIS_KIND, "version": ANALYSIS_VERSION,
            "sources": {"matrix_id": request.matrix_id, "protocol_id": self.rows[0]["protocol_id"], "leaves": [{"run_id": r["run_id"], "leaf_id": r["leaf_id"], "matcher_id": r["matcher_id"], "condition_id": r["condition_id"], "comparisons_sha256": r["comparisons_sha256"], "matcher_registry_state": self.registry.raw_resource(r["matcher_id"])["state"]} for r in self.rows],
                        "quality_job_id": request.quality_job_id, "quality_run_id": self.quality_job["run_id"], "quality_parquet_sha256": sha256_file(Path(self.quality_job["parquet_path"])), "quality_measure": request.quality_measure, "samples_measured": measured},
            "matcher_sets": self.sets,
            "pair_quality_policy": {"id": self.policy.value, "version": PAIR_QUALITY_POLICY_VERSION, "definition": POLICY_DEFINITIONS[self.policy]},
            "target_fmr": request.target_fmr, "partial_interval": request.partial_interval,
            "uncertainty": {"method": "PAIRED_CLUSTER_BOOTSTRAP_PERCENTILE", "sampling_unit": "LEFT_SUBJECT_ID_SHARED_BY_BOTH_RUNS", "replicates": request.bootstrap_replicates, "seed": request.seed, "confidence_level": CONFIDENCE_LEVEL},
            "contrasts": contrasts,
            "rules": ["matcher contrasts hold the preprocessing condition fixed", "preprocessing contrasts hold the matcher and the protocol fixed", "no pooled cross-condition number is reported", "no single matcher-independence score is defined"],
        }
        document_path = self.directory / "crossmatcher.json"
        document_path.write_text(json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        seconds = time.monotonic() - clock
        run, experiment = self._run(started, seconds, document, document_path, pairs_path, leaves)
        summary = {
            "analysis_id": self.analysis_id, "run_id": run.run_id, "matrix_id": request.matrix_id, "quality_job_id": request.quality_job_id, "quality_measure": request.quality_measure,
            "pair_quality_policy": self.policy.value, "target_fmr": request.target_fmr, "matcher_contrasts": sum(1 for c in contrasts if c["kind"] == CONTRAST_KINDS["matcher"]),
            "preprocessing_contrasts": sum(1 for c in contrasts if c["kind"] == CONTRAST_KINDS["preprocessing"]), "leaves": len(leaves),
            "contrasts": [{"contrast_id": c["contrast_id"], "kind": c["kind"], "a": c["a"]["leaf_id"], "b": c["b"]["leaf_id"], "held_fixed": c["held_fixed"],
                           "fnm_overlap": c["error_overlap"]["mated_false_non_match"], "fnm_jaccard": c["failure_set_jaccard"]["mated_false_non_match"],
                           "au_erc_diff_fnmr": c["au_erc_differences"]["FNMR"]["au_erc"]["difference_a_minus_b"],
                           "au_erc_diff_fnmr_ci": [c["au_erc_differences"]["FNMR"]["au_erc"]["uncertainty"].get("ci_lower"), c["au_erc_differences"]["FNMR"]["au_erc"]["uncertainty"].get("ci_upper")] if c["au_erc_differences"]["FNMR"]["au_erc"]["uncertainty"] else None,
                           "interaction_status": c["quality_matcher_interaction"].get("status")} for c in contrasts],
        }
        result = CrossMatcherResult(self.analysis_id, run, experiment, document, self.directory, document_path, pairs_path, summary)
        self.store.record_cross_matcher_analysis(result)
        return result

    def _run(self, started: datetime, seconds: float, document: dict[str, Any], document_path: Path, pairs_path: Path, leaves: list[LeafData]) -> tuple[CompletedRun, Experiment]:
        request = self.request
        source = leaves[0].run
        model_hashes: list[DomainArtifactIdentity] = []
        seen: set[str] = set()
        for leaf in leaves:
            for identity in leaf.run.model_hashes:
                if identity.sha256 not in seen:
                    seen.add(identity.sha256)
                    model_hashes.append(identity)
        run_ids = sorted(leaf.row["run_id"] for leaf in leaves)
        configuration = CanonicalJson.from_value({
            "analysis_id": self.analysis_id, "kind": ANALYSIS_KIND, "version": ANALYSIS_VERSION, "sources": document["sources"], "matcher_sets": document["matcher_sets"],
            "pair_quality_policy": document["pair_quality_policy"], "target_fmr": request.target_fmr, "partial_interval": request.partial_interval, "uncertainty": document["uncertainty"], "rules": document["rules"],
        })
        experiment_id = content_id("experiment", self.project.project_id, self.rows[0]["dataset_id"], ANALYSIS_KIND, request.matrix_id, request.quality_job_id, request.quality_measure, self.policy.value)
        run_id = content_id("run", experiment_id, started.isoformat(), sha256_file(document_path), sha256_file(pairs_path))
        observations = [MetricObservation(metric_id="contrasts.matcher", value=sum(1 for c in document["contrasts"] if c["kind"] == CONTRAST_KINDS["matcher"]), unit="count"),
                        MetricObservation(metric_id="contrasts.preprocessing", value=sum(1 for c in document["contrasts"] if c["kind"] == CONTRAST_KINDS["preprocessing"]), unit="count"),
                        MetricObservation(metric_id="timing.analysis_seconds", value=round(seconds, 3), unit="observation")]
        for contrast in document["contrasts"]:
            diff = contrast["au_erc_differences"]["FNMR"]["au_erc"]["difference_a_minus_b"]
            if diff is not None:
                observations.append(MetricObservation(metric_id=f"biometric.quality.utility.au-erc@1.0.0[FNMR] difference {contrast['contrast_id']}", value=float(diff), unit="area"))
        artifacts = []
        for path, kind, media, backend in ((document_path, ArtifactKind.METRICS, "application/json", StorageBackend.FILESYSTEM), (pairs_path, ArtifactKind.TABLE, "application/vnd.apache.parquet", StorageBackend.PARQUET)):
            digest = sha256_file(path)
            artifacts.append(Artifact(artifact_id=content_id("artifact", run_id, digest, path.name), run_id=run_id, kind=kind, storage_backend=backend, uri=str(path), sha256=digest, size_bytes=path.stat().st_size, media_type=media, classification=DataClassification.SENSITIVE_BIOMETRIC_DERIVED, created_at=_now()))
        run = CompletedRun(
            run_id=run_id, project_id=self.project.project_id, experiment_id=experiment_id, user_id=self.actor,
            dataset_manifest=source.dataset_manifest, dataset_hash=source.dataset_hash, subject_protocol=source.subject_protocol, pair_search_protocol=source.pair_search_protocol,
            quality_engine=self.quality_run.quality_engine, quality_engine_configuration=self.quality_run.quality_engine_configuration,
            model_hashes=tuple(model_hashes),
            matcher=ComponentBinding(state=ComponentState.CONFIGURED, component_id="cross-matcher-set", version=hashlib.sha256("\n".join(run_ids).encode("utf-8")).hexdigest()[:16], configuration_sha256=sha256_bytes("\n".join(run_ids).encode("utf-8")), artifact_hashes=tuple(identity.sha256 for identity in model_hashes)),
            matcher_preprocessing=ComponentBinding(state=ComponentState.CONFIGURED, component_id="both-recorded-conditions", version="1.0.0", configuration_sha256=sha256_bytes(json.dumps(sorted(self.sets["fixed_condition"].keys())).encode("utf-8"))),
            operating_thresholds=ComponentBinding(state=ComponentState.CONFIGURED, component_id="per-run-recorded-operating-point", version="1.0.0", configuration_sha256=sha256_bytes(json.dumps({"target_fmr": request.target_fmr}, sort_keys=True).encode("utf-8"))),
            statistical_methods=ComponentBinding(state=ComponentState.CONFIGURED, component_id="qualsight.analysis.crossmatcher+metrics.statistics", version="1.0.0", configuration_sha256=sha256_bytes(json.dumps({"partial_interval": request.partial_interval, "replicates": request.bootstrap_replicates, "seed": request.seed}, sort_keys=True).encode("utf-8"))),
            confidence_interval_method=ComponentBinding(state=ComponentState.CONFIGURED, component_id="PAIRED_CLUSTER_BOOTSTRAP_PERCENTILE_95+CLOPPER_PEARSON_95+WALD", version="1.0.0"),
            randomness=ComponentBinding(state=ComponentState.CONFIGURED, component_id="numpy.random.default_rng", version="PCG64", configuration_sha256=sha256_bytes(str(request.seed).encode("utf-8"))),
            random_seeds=(request.seed,), application_version=application_version(self.repository_root / "backend" / "pyproject.toml"), repository=repository_identity(self.repository_root),
            environment=environment_identity(self.repository_root / "uv.lock", {}), hardware=hardware_identity("numpy on CPU"),
            start_time=started, end_time=_now(), runtime_seconds=round(seconds, 3), initialization_ms=0, assessment_ms=int(seconds * 1000),
            configuration=configuration, metrics=tuple(observations), artifacts=tuple(artifacts),
            logs=(LogRecord(stream="stdout", sha256=sha256_bytes(b""), text=""), LogRecord(stream="stderr", sha256=sha256_bytes(b""), text="")),
            warnings=("interaction models pool units across two Runs that share subjects; Wald intervals are approximate",),
            provenance_limits=tuple(sorted({limit for leaf in leaves for limit in leaf.run.provenance_limits})) + ("Cross-matcher results are QualSight-executed contrasts of recorded Runs; matcher contrasts never pool preprocessing conditions.",),
            exit_code=0, status=RunStatus.COMPLETED, scientific_support=ScientificSupportState.NOT_ASSESSED,
        )
        experiment = Experiment(
            experiment_id=experiment_id, project_id=self.project.project_id, dataset_id=self.rows[0]["dataset_id"], name=f"cross-matcher: matrix {request.matrix_id[-12:]} × {request.quality_measure} ({self.policy.value})",
            created_by=self.actor, created_at=started,
            protocol=VerificationProtocol(kind=ProtocolKind.VERIFICATION, protocol_id=self.rows[0]["protocol_id"], pair_manifest=self.protocol.pair_manifest, identity_schema=ComponentBinding(state=ComponentState.CONFIGURED, component_id="lfw-directory-identity", version="1.0.0")),
            configuration=configuration,
        )
        return run, experiment
