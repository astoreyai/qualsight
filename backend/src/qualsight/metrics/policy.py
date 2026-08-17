"""Threshold and policy analysis (PRD §24; QS-D33).

An operating policy is `pair quality >= Q` (QUALITY POLICY THRESHOLD) combined
with a fixed MATCHER THRESHOLD `T`. The analysis reports, exactly reconciled
to the source comparisons, what the policy retains and rejects and which
matcher decisions it prevents or removes. The matcher threshold is never
re-optimized here; both thresholds are reported under their own kind so they
cannot be conflated.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np

from .statistics import clopper_pearson

THRESHOLD_POLICY_DEFINITION = ("biometric.policy.threshold-analysis", "1.0.0")
THRESHOLD_KINDS = {"matcher": "MATCHER_THRESHOLD", "quality": "QUALITY_POLICY_THRESHOLD", "engine": "STANDARDS_OR_ENGINE_ASSESSMENT"}


def retention_matched_cutoff(quality: np.ndarray, target_retention: float) -> float | None:
    """Smallest cutoff whose retained fraction (quality >= cutoff) does not exceed the target retention."""

    finite = quality[np.isfinite(quality)]
    n = finite.shape[0]
    if n == 0:
        return None
    distinct = np.unique(finite)  # ascending candidate cutoffs
    # retained fraction at cutoff c is |{q >= c}| / n, non-increasing in c
    retained = (n - np.searchsorted(np.sort(finite), distinct, side="left")) / n
    admissible = np.flatnonzero(retained <= target_retention)
    if admissible.shape[0] == 0:
        return float(distinct[-1]) + 1.0  # no cutoff over the observed values retains few enough; reject all
    return float(distinct[admissible[0]])


def analyze_policy(
    scores: np.ndarray,
    mated: np.ndarray,
    failed: np.ndarray,
    pair_quality: np.ndarray,
    *,
    quality_threshold: float,
    matcher_threshold: float,
    quality_measure: str,
    pair_quality_policy: str,
) -> dict[str, object]:
    usable = (~failed) & np.isfinite(scores) & np.isfinite(pair_quality)
    retained = usable & (pair_quality >= quality_threshold)
    rejected = usable & ~(pair_quality >= quality_threshold)
    accept = scores >= matcher_threshold
    false_match = (~mated) & accept
    false_non_match = mated & ~accept
    correct = ((~mated) & ~accept) | (mated & accept)

    def block(mask: np.ndarray) -> dict[str, object]:
        m_units = int(np.sum(mask & mated))
        n_units = int(np.sum(mask & ~mated))
        return {
            "units": int(np.sum(mask)),
            "mated_units": m_units,
            "non_mated_units": n_units,
            "fnmr": clopper_pearson(int(np.sum(mask & false_non_match)), m_units),
            "fmr": clopper_pearson(int(np.sum(mask & false_match)), n_units),
        }

    total_usable = int(np.sum(usable))
    result: dict[str, object] = {
        "definition": THRESHOLD_POLICY_DEFINITION,
        "thresholds": {
            "matcher": {"kind": THRESHOLD_KINDS["matcher"], "value": float(matcher_threshold), "rule": "ACCEPT_WHEN_SCORE_GE_THRESHOLD", "reestimated_during_policy": False},
            "quality_policy": {"kind": THRESHOLD_KINDS["quality"], "value": float(quality_threshold), "rule": "RETAIN_WHEN_PAIR_QUALITY_GE_THRESHOLD", "quality_measure": quality_measure, "pair_quality_policy": pair_quality_policy},
        },
        "counts": {
            "comparison_units": int(scores.shape[0]),
            "processing_failures_excluded": int(np.sum(failed)),
            "missing_quality_excluded": int(np.sum(~failed & ~np.isfinite(pair_quality))),
            "usable_units": total_usable,
            "retained": int(np.sum(retained)),
            "rejected": int(np.sum(rejected)),
        },
        "retention_rate": (int(np.sum(retained)) / total_usable) if total_usable else None,
        "rejection_rate": (int(np.sum(rejected)) / total_usable) if total_usable else None,
        "before_policy": block(usable),
        "after_policy": block(retained),
        "rejected_units": block(rejected),
        "impact": {
            "false_non_matches_prevented": int(np.sum(rejected & false_non_match)),
            "false_matches_prevented": int(np.sum(rejected & false_match)),
            "errors_prevented": int(np.sum(rejected & (false_match | false_non_match))),
            "successful_comparisons_removed": int(np.sum(rejected & correct)),
            "successful_mated_removed": int(np.sum(rejected & correct & mated)),
            "successful_non_mated_removed": int(np.sum(rejected & correct & ~mated)),
        },
    }
    text = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    result["evidence_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return result
