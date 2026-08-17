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

Areas under the ERC (AU-ERC, partial AU-ERC) are reported as *derived
summaries* of the registered curve; the metric registry does not yet carry
their definitions, so they are labelled as such and never surfaced as
registry metrics.
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
            "note": "AU-ERC and partial AU-ERC are derived from the registered ERC curve; their definitions are not yet registered in the metric registry (v1.0.1) and they are not registry metrics.",
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
