"""Pair-quality policies and the Error-versus-Reject Curve (PRD §§20–21; QS-D30/QS-D31 evidence).

The ERC follows the registry definition ``biometric.quality.utility.erc@1.0.0``:
the retained-set error rate as progressively worse-quality comparison units are
rejected under a recorded pair-quality policy while the matcher threshold stays
fixed across the curve. Everything that definition leaves to the caller is
made explicit here:

* pair-quality policy (PRD §21): MIN, MAX, MEAN, GEOMETRIC_MEAN of the two
  sample qualities, or the ENROLLMENT (left) / PROBE (right) sample's quality;
* fixed matcher threshold: the recorded threshold at a target FMR on the full
  comparison set (failure policy EXCLUDE_FAILED_TRIALS), or a caller value;
* comparison units: mated comparisons for the FNMR-ERC and non-mated
  comparisons for the FMR-ERC — reported separately, never pooled;
* rejection order: worst pair quality first; ties are rejected together at
  the same rejection fraction so the curve is a deterministic function of the
  data.

Areas under the ERC (AU-ERC, partial AU-ERC), risk-coverage, quality-conditioned
rates, association statistics, and the failure predictor are registered in
metric registry v1.1.0 (``biometric.quality.utility.*@1.0.0``) and computed by
``compute_utility_analysis`` below; ``compute_erc`` keeps its compact derived
summaries for the lightweight ERC endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import math

import numpy as np

ERC_DEFINITION = ("biometric.quality.utility.erc", "1.0.0")


class PairQualityPolicy(StrEnum):
    MIN = "pair.quality.min"
    MAX = "pair.quality.max"
    MEAN = "pair.quality.mean"
    GEOMETRIC_MEAN = "pair.quality.geometric_mean"
    ENROLLMENT = "pair.quality.enrollment"
    PROBE = "pair.quality.probe"


PAIR_QUALITY_POLICY_VERSION = "1.0.0"
POLICY_DEFINITIONS: dict[PairQualityPolicy, str] = {
    PairQualityPolicy.MIN: "min(q_left, q_right)",
    PairQualityPolicy.MAX: "max(q_left, q_right)",
    PairQualityPolicy.MEAN: "(q_left + q_right) / 2",
    PairQualityPolicy.GEOMETRIC_MEAN: "sqrt(q_left * q_right) for non-negative qualities",
    PairQualityPolicy.ENROLLMENT: "q_left (the first sample of the protocol pair is the enrolment sample)",
    PairQualityPolicy.PROBE: "q_right (the second sample of the protocol pair is the probe)",
}


def pair_quality(left: np.ndarray, right: np.ndarray, policy: PairQualityPolicy) -> np.ndarray:
    if policy is PairQualityPolicy.MIN:
        return np.minimum(left, right)
    if policy is PairQualityPolicy.MAX:
        return np.maximum(left, right)
    if policy is PairQualityPolicy.MEAN:
        return (left + right) / 2.0
    if policy is PairQualityPolicy.GEOMETRIC_MEAN:
        return np.sqrt(np.clip(left, 0, None) * np.clip(right, 0, None))
    if policy is PairQualityPolicy.ENROLLMENT:
        return left.copy()
    return right.copy()


@dataclass(frozen=True, slots=True)
class ErcInputs:
    scores: np.ndarray  # float64, NaN when the comparison failed
    mated: np.ndarray  # bool
    failed: np.ndarray  # bool (comparison processing failure)
    quality_left: np.ndarray  # float64, NaN when the sample has no quality value
    quality_right: np.ndarray


def _curve(scores: np.ndarray, quality: np.ndarray, threshold: float, error_kind: str) -> list[dict[str, float | int]]:
    """Retained-set error at each distinct rejection step (worst quality first)."""

    order = np.argsort(quality, kind="stable")  # ascending: worst first
    quality_sorted = quality[order]
    scores_sorted = scores[order]
    n = scores_sorted.shape[0]
    if error_kind == "FNMR":
        errors_sorted = (scores_sorted < threshold).astype(np.int64)
    else:
        errors_sorted = (scores_sorted >= threshold).astype(np.int64)
    # cumulative errors of the retained suffix
    suffix_errors = np.concatenate((np.cumsum(errors_sorted[::-1])[::-1], [0]))
    points: list[dict[str, float | int]] = []
    # Reject in blocks of equal quality so ties leave together.
    boundaries = [0]
    for index in range(1, n):
        if quality_sorted[index] != quality_sorted[index - 1]:
            boundaries.append(index)
    boundaries.append(n)
    for start in boundaries:
        retained = n - start
        if retained == 0:
            points.append({"rejected_fraction": 1.0, "retained": 0, "error_rate": None, "quality_cutoff": float(quality_sorted[-1]) if n else None})
            break
        points.append(
            {
                "rejected_fraction": start / n,
                "retained": int(retained),
                "errors": int(suffix_errors[start]),
                "error_rate": float(suffix_errors[start] / retained),
                "quality_cutoff": float(quality_sorted[start]),
            }
        )
    return points


def _area(points: list[dict[str, float | int]], max_fraction: float) -> float | None:
    xs, ys = [], []
    for point in points:
        if point["error_rate"] is None or point["rejected_fraction"] > max_fraction:
            continue
        xs.append(float(point["rejected_fraction"]))
        ys.append(float(point["error_rate"]))
    if len(xs) < 2:
        return None
    if xs[-1] < max_fraction:
        xs.append(max_fraction)
        ys.append(ys[-1])
    return float(np.trapezoid(ys, xs)) if hasattr(np, "trapezoid") else float(np.trapz(ys, xs))


def compute_erc(inputs: ErcInputs, *, policy: PairQualityPolicy, threshold: float, threshold_provenance: dict[str, object]) -> dict[str, object]:
    quality = pair_quality(inputs.quality_left, inputs.quality_right, policy)
    usable = (~inputs.failed) & np.isfinite(inputs.scores) & np.isfinite(quality)
    excluded = {
        "comparison_failures": int(np.sum(inputs.failed)),
        "missing_quality": int(np.sum(~np.isfinite(quality) & ~inputs.failed)),
    }
    result: dict[str, object] = {
        "definition": ERC_DEFINITION,
        "pair_quality_policy": {"id": policy.value, "version": PAIR_QUALITY_POLICY_VERSION, "definition": POLICY_DEFINITIONS[policy]},
        "fixed_matcher_threshold": {"value": float(threshold), **threshold_provenance},
        "rejection_order": "WORST_PAIR_QUALITY_FIRST_TIES_TOGETHER",
        "excluded_units": excluded,
        "curves": {},
        "derived_summaries": {},
    }
    for kind, mask in (("FNMR", inputs.mated), ("FMR", ~inputs.mated)):
        selection = usable & mask
        points = _curve(inputs.scores[selection], quality[selection], threshold, kind)
        result["curves"][kind] = {"comparison_units": int(np.sum(selection)), "points": points}
        result["derived_summaries"][kind] = {
            "note": "Compact summaries derived from the registered ERC curve; the registry metrics biometric.quality.utility.au-erc@1.0.0 and partial-au-erc@1.0.0 (metric registry v1.1.0) with recorded integration domains and uncertainty are reported by compute_utility_analysis.",
            "au_erc_0_to_1": _area(points, 1.0),
            "partial_au_erc_0_to_0_2": _area(points, 0.2),
            "error_at_0_rejection": points[0]["error_rate"] if points else None,
            "error_at_0_1_rejection": _error_at(points, 0.1),
            "error_at_0_2_rejection": _error_at(points, 0.2),
        }
    text = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    result["evidence_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return result


def _error_at(points: list[dict[str, float | int]], fraction: float) -> float | None:
    value = None
    for point in points:
        if point["rejected_fraction"] <= fraction and point["error_rate"] is not None:
            value = float(point["error_rate"])
        else:
            break
    return value


# ---------------------------------------------------------------------------
# Full quality-utility analysis (QS-D31/QS-D32): AU-ERC, partial AU-ERC,
# risk-coverage, quality-conditioned error rates, association, prediction.
# ---------------------------------------------------------------------------

from .statistics import (  # noqa: E402  (kept after the ERC core so the module reads top-down)
    CONFIDENCE_LEVEL,
    average_precision,
    classifier_summary,
    clopper_pearson,
    cluster_bootstrap,
    kendall_tau_b,
    logistic_regression,
    roc_auc,
    spearman,
)

AU_ERC_DEFINITION = ("biometric.quality.utility.au-erc", "1.0.0")
PARTIAL_AU_ERC_DEFINITION = ("biometric.quality.utility.partial-au-erc", "1.0.0")
RISK_COVERAGE_DEFINITION = ("biometric.quality.utility.risk-coverage", "1.0.0")
QUALITY_CONDITIONED_FNMR_DEFINITION = ("biometric.quality.utility.quality-conditioned-fnmr", "1.0.0")
QUALITY_CONDITIONED_FMR_DEFINITION = ("biometric.quality.utility.quality-conditioned-fmr", "1.0.0")
QUALITY_ERROR_ASSOCIATION_DEFINITION = ("biometric.quality.utility.quality-error-association", "1.0.0")
FAILURE_PREDICTOR_DEFINITION = ("biometric.quality.utility.failure-predictor", "1.0.0")
UNSUPPORTED_BIN_TRIALS = 30


@dataclass(frozen=True, slots=True)
class UtilityInputs(ErcInputs):
    subject_left: np.ndarray  # str
    subject_right: np.ndarray


def _errors_at_threshold(scores: np.ndarray, threshold: float, error_kind: str) -> np.ndarray:
    return (scores < threshold) if error_kind == "FNMR" else (scores >= threshold)


def _area_from_units(quality: np.ndarray, errors: np.ndarray, max_fraction: float) -> float | None:
    """AU-ERC directly from units (used inside bootstrap replicates)."""

    n = quality.shape[0]
    if n < 2:
        return None
    order = np.argsort(quality, kind="stable")
    q = quality[order]
    e = errors[order].astype(np.int64)
    suffix = np.concatenate((np.cumsum(e[::-1])[::-1], [0]))
    starts = np.concatenate(([0], np.flatnonzero(q[1:] != q[:-1]) + 1))
    xs = starts / n
    ys = suffix[starts] / (n - starts)
    keep = xs <= max_fraction
    xs, ys = xs[keep], ys[keep]
    if xs.shape[0] < 2:
        return None
    if xs[-1] < max_fraction:
        xs = np.append(xs, max_fraction)
        ys = np.append(ys, ys[-1])
    return float(np.trapezoid(ys, xs)) if hasattr(np, "trapezoid") else float(np.trapz(ys, xs))


def _default_bin_edges(quality: np.ndarray, quality_range: tuple[float, float] | None) -> list[float]:
    if quality_range is not None:
        low, high = quality_range
    else:
        low, high = float(np.floor(np.min(quality))), float(np.ceil(np.max(quality)))
        if high <= low:
            high = low + 1.0
    return [low + (high - low) * k / 10.0 for k in range(11)]


def _conditioned_rates(quality: np.ndarray, errors: np.ndarray, edges: list[float]) -> list[dict[str, object]]:
    bins: list[dict[str, object]] = []
    for index in range(len(edges) - 1):
        low, high = edges[index], edges[index + 1]
        last = index == len(edges) - 2
        mask = (quality >= low) & ((quality <= high) if last else (quality < high))
        trials = int(np.sum(mask))
        events = int(np.sum(errors[mask]))
        rate = clopper_pearson(events, trials)
        rate.update({"bin_index": index, "quality_low": low, "quality_high": high, "closed_high": last, "supported": trials >= UNSUPPORTED_BIN_TRIALS})
        if trials < UNSUPPORTED_BIN_TRIALS:
            rate["warning"] = f"fewer than {UNSUPPORTED_BIN_TRIALS} comparison units; the interval does not support a credible rate"
        bins.append(rate)
    return bins


def compute_utility_analysis(
    inputs: UtilityInputs,
    *,
    policy: PairQualityPolicy,
    threshold: float,
    threshold_provenance: dict[str, object],
    partial_interval: float = 0.2,
    quality_range: tuple[float, float] | None = None,
    bin_edges: list[float] | None = None,
    bootstrap_replicates: int = 1000,
    seed: int = 20260817,
    quality_cutoff_for_prediction: float | None = None,
) -> dict[str, object]:
    """ERC plus the PRD §20 family over one matcher Run and one quality measure.

    Rejection domain, retained coverage, threshold policy, pair aggregation,
    counts, and uncertainty are all recorded in the returned document.
    """

    erc = compute_erc(inputs, policy=policy, threshold=threshold, threshold_provenance=threshold_provenance)
    quality = pair_quality(inputs.quality_left, inputs.quality_right, policy)
    usable = (~inputs.failed) & np.isfinite(inputs.scores) & np.isfinite(quality)
    result: dict[str, object] = {
        "erc": erc,
        "rejection_domain": {"variable": "REJECTED_FRACTION_OF_COMPARISON_UNITS", "interval": [0.0, 1.0], "partial_interval": [0.0, float(partial_interval)]},
        "areas": {},
        "risk_coverage": {"definition": RISK_COVERAGE_DEFINITION, "curves": {}},
        "quality_conditioned": {},
        "association": {},
        "failure_prediction": {},
        "uncertainty": {"method": "CLUSTER_BOOTSTRAP_PERCENTILE", "sampling_unit": "LEFT_SUBJECT_ID_OF_THE_COMPARISON_UNIT", "replicates": int(bootstrap_replicates), "seed": int(seed), "confidence_level": CONFIDENCE_LEVEL},
    }
    edges = bin_edges if bin_edges is not None else _default_bin_edges(quality[usable] if np.any(usable) else np.array([0.0, 1.0]), quality_range)
    result["quality_conditioned"]["bin_edges"] = edges
    for kind, mask in (("FNMR", inputs.mated), ("FMR", ~inputs.mated)):
        selection = usable & mask
        q = quality[selection]
        s = inputs.scores[selection]
        errors = _errors_at_threshold(s, threshold, kind)
        clusters = inputs.subject_left[selection]
        n = int(q.shape[0])
        # Areas under the recorded ERC (interval recorded), with cluster-bootstrap CIs.
        full = _area_from_units(q, errors, 1.0)
        partial = _area_from_units(q, errors, partial_interval)
        boot_full = cluster_bootstrap(clusters, lambda idx: _area_from_units(q[idx], errors[idx], 1.0), replicates=bootstrap_replicates, seed=seed) if n else None
        boot_partial = cluster_bootstrap(clusters, lambda idx: _area_from_units(q[idx], errors[idx], partial_interval), replicates=bootstrap_replicates, seed=seed + 1) if n else None
        result["areas"][kind] = {
            "au_erc": {"definition": AU_ERC_DEFINITION, "integration_domain": [0.0, 1.0], "integration_method": "TRAPEZOID_OVER_RECORDED_STEP_POINTS", "value": full, "comparison_units": n, "uncertainty": boot_full},
            "partial_au_erc": {"definition": PARTIAL_AU_ERC_DEFINITION, "integration_domain": [0.0, float(partial_interval)], "integration_method": "TRAPEZOID_OVER_RECORDED_STEP_POINTS", "value": partial, "comparison_units": n, "uncertainty": boot_partial},
        }
        # Risk-coverage: same points re-parameterized by retained coverage.
        points = erc["curves"][kind]["points"]
        result["risk_coverage"]["curves"][kind] = {
            "comparison_units": n,
            "points": [{"coverage": 1.0 - float(p["rejected_fraction"]), "retained": p["retained"], "risk": p["error_rate"], "quality_cutoff": p["quality_cutoff"]} for p in points],
        }
        # Quality-conditioned error rates per recorded bin.
        result["quality_conditioned"][kind] = {"definition": QUALITY_CONDITIONED_FNMR_DEFINITION if kind == "FNMR" else QUALITY_CONDITIONED_FMR_DEFINITION, "bins": _conditioned_rates(q, errors, edges) if n else []}
        # Association between pair quality and the error outcome / the score.
        association: dict[str, object] = {"definition": QUALITY_ERROR_ASSOCIATION_DEFINITION, "comparison_units": n}
        if n >= 3:
            association["spearman_quality_vs_score"] = spearman(q, s)
            association["kendall_quality_vs_score"] = kendall_tau_b(q, s)
            association["logistic_error_on_quality"] = logistic_regression(q, errors.astype(np.float64))
        else:
            association["status"] = "UNSUPPORTED_BY_SAMPLE"
        result["association"][kind] = association
        # Quality as a predictor of the error outcome (low quality → error).
        prediction: dict[str, object] = {"definition": FAILURE_PREDICTOR_DEFINITION, "outcome": "FALSE_NON_MATCH_AT_FIXED_THRESHOLD" if kind == "FNMR" else "FALSE_MATCH_AT_FIXED_THRESHOLD", "predictor": "NEGATIVE_PAIR_QUALITY", "comparison_units": n, "outcome_events": int(np.sum(errors))}
        if n and 0 < int(np.sum(errors)) < n:
            prediction["roc_auc"] = roc_auc(-q, errors)
            prediction["pr_auc"] = average_precision(-q, errors)
            prediction["prevalence"] = float(np.mean(errors))
            if quality_cutoff_for_prediction is not None:
                prediction["at_quality_cutoff"] = classifier_summary(q, errors, cutoff=float(quality_cutoff_for_prediction), positive_when="SCORE_LE_CUTOFF")
        else:
            prediction["status"] = "UNSUPPORTED_BY_SAMPLE"
            prediction["reason"] = "both outcome classes are required"
        result["failure_prediction"][kind] = prediction
    # Processing failures versus quality (when the failed side has a quality value).
    failed_with_quality = inputs.failed & np.isfinite(quality)
    if int(np.sum(failed_with_quality)) and int(np.sum(usable)):
        pool = failed_with_quality | usable
        result["failure_prediction"]["PROCESSING_FAILURE"] = {"definition": FAILURE_PREDICTOR_DEFINITION, "outcome": "COMPARISON_PROCESSING_FAILURE", "predictor": "NEGATIVE_PAIR_QUALITY", "comparison_units": int(np.sum(pool)), "outcome_events": int(np.sum(failed_with_quality)), "roc_auc": roc_auc(-quality[pool], inputs.failed[pool]), "pr_auc": average_precision(-quality[pool], inputs.failed[pool])}
    text = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_json_default)
    result["evidence_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return result


def _json_default(value: object) -> object:
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"not JSON serializable: {type(value)!r}")
