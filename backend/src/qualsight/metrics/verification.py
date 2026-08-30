"""1:1 verification metrics over recorded comparisons (QS-C23).

Implements the registry definitions ``biometric.verification.{fmr,fnmr,roc,det,eer}``
(definition version 1.0.0) with every input those definitions require made
explicit: score direction, threshold equality rule (accept when
``score >= threshold`` for higher-is-more-similar scores), and the processing
failure policy. Two failure policies are computed and reported side by side:

* ``EXCLUDE_FAILED_TRIALS`` — ISO/IEC 19795-1 style: FMR/FNMR over completed
  comparisons only; failed trials are counted and reported as FTA;
* ``FAILURES_AS_REJECT`` — generalized rates: a failed trial is a reject, so it
  counts as a false non-match for mated pairs and never as a false match.

Uncertainty: exact Clopper–Pearson binomial intervals per rate (the registry's
BINOMIAL_WHEN_INDEPENDENCE_IS_JUSTIFIED method, reported with the caveat that
LFW reuses subjects) plus the official View 2 ten-fold accuracy (threshold
chosen on the nine held-in folds, evaluated on the held-out fold).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

import numpy as np

REGISTRY_METRICS = {
    "fmr": ("biometric.verification.fmr", "1.0.0"),
    "fnmr": ("biometric.verification.fnmr", "1.0.0"),
    "roc": ("biometric.verification.roc", "1.0.0"),
    "det": ("biometric.verification.det", "1.0.0"),
    "eer": ("biometric.verification.eer", "1.0.0"),
}
THRESHOLD_EQUALITY_RULE = "ACCEPT_WHEN_SCORE_GE_THRESHOLD"
SCORE_DIRECTION = "HIGHER_IS_MORE_SIMILAR"
FAILURE_POLICIES = ("EXCLUDE_FAILED_TRIALS", "FAILURES_AS_REJECT")
TARGET_FMRS = (0.1, 0.01, 0.001)


def _binom_interval(successes: int, trials: int, alpha: float = 0.05) -> tuple[float | None, float | None]:
    """Exact Clopper–Pearson interval via the beta quantile (regularized incomplete beta inversion)."""

    if trials <= 0:
        return None, None
    lower = 0.0 if successes == 0 else _beta_ppf(alpha / 2, successes, trials - successes + 1)
    upper = 1.0 if successes == trials else _beta_ppf(1 - alpha / 2, successes + 1, trials - successes)
    return lower, upper


def _beta_ppf(q: float, a: float, b: float) -> float:
    """Inverse regularized incomplete beta by bisection on a monotone CDF (adequate for CI reporting)."""

    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _betainc(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b) via Lentz continued fraction (Numerical Recipes)."""

    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x)
    front = math.exp(lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return h


@dataclass(frozen=True, slots=True)
class ComparisonInputs:
    """Vectorized comparison inputs; failed trials have NaN score and a failure flag."""

    scores: np.ndarray  # float64, NaN when failed
    mated: np.ndarray  # bool
    failed: np.ndarray  # bool
    folds: np.ndarray  # positive int when the protocol publishes fold authority; otherwise None


def _rates_at(inputs: ComparisonInputs, threshold: float, policy: str) -> dict[str, float | int | None]:
    mated, failed, scores = inputs.mated, inputs.failed, inputs.scores
    accept = np.zeros_like(failed, dtype=bool)
    ok = ~failed
    accept[ok] = scores[ok] >= threshold
    if policy == "EXCLUDE_FAILED_TRIALS":
        nm_included = (~mated) & ok
        m_included = mated & ok
        false_matches = int(np.sum(nm_included & accept))
        false_non_matches = int(np.sum(m_included & ~accept))
    else:  # FAILURES_AS_REJECT
        nm_included = ~mated
        m_included = mated
        false_matches = int(np.sum(nm_included & accept))  # failed → reject → never a false match
        false_non_matches = int(np.sum(m_included & (~accept)))  # failed mated trials count as rejects
    nm_n = int(np.sum(nm_included))
    m_n = int(np.sum(m_included))
    fmr = false_matches / nm_n if nm_n else None
    fnmr = false_non_matches / m_n if m_n else None
    fmr_ci = _binom_interval(false_matches, nm_n)
    fnmr_ci = _binom_interval(false_non_matches, m_n)
    return {
        "threshold": float(threshold),
        "fmr": fmr,
        "fnmr": fnmr,
        "false_matches": false_matches,
        "nonmated_trials_included": nm_n,
        "false_non_matches": false_non_matches,
        "mated_trials_included": m_n,
        "fmr_ci95": [fmr_ci[0], fmr_ci[1]],
        "fnmr_ci95": [fnmr_ci[0], fnmr_ci[1]],
    }


def _curve(inputs: ComparisonInputs, policy: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """FMR and FNMR at every distinct completed score (plus ±inf), ascending threshold."""

    ok = ~inputs.failed
    thresholds = np.unique(inputs.scores[ok])
    thresholds = np.concatenate(([-np.inf], thresholds, [np.inf]))
    nonmated_scores = np.sort(inputs.scores[ok & ~inputs.mated])
    mated_scores = np.sort(inputs.scores[ok & inputs.mated])
    false_matches = nonmated_scores.size - np.searchsorted(nonmated_scores, thresholds, side="left")
    false_non_matches = np.searchsorted(mated_scores, thresholds, side="left")
    if policy == "EXCLUDE_FAILED_TRIALS":
        nonmated_trials = nonmated_scores.size
        mated_trials = mated_scores.size
    else:
        nonmated_trials = int((~inputs.mated).sum())
        mated_trials = int(inputs.mated.sum())
        false_non_matches = false_non_matches + int((inputs.failed & inputs.mated).sum())
    fmr = false_matches.astype(np.float64) / nonmated_trials if nonmated_trials else np.full(thresholds.shape, np.nan)
    fnmr = false_non_matches.astype(np.float64) / mated_trials if mated_trials else np.full(thresholds.shape, np.nan)
    return thresholds, fmr, fnmr


def _best_accuracy_threshold(inputs: ComparisonInputs, policy: str) -> tuple[float | None, float]:
    ok = ~inputs.failed
    candidates = np.unique(inputs.scores[ok])
    if candidates.size == 0:
        return None, -1.0
    nonmated_scores = np.sort(inputs.scores[ok & ~inputs.mated])
    mated_scores = np.sort(inputs.scores[ok & inputs.mated])
    correct_nonmated = np.searchsorted(nonmated_scores, candidates, side="left")
    correct_mated = mated_scores.size - np.searchsorted(mated_scores, candidates, side="left")
    correct = correct_nonmated + correct_mated
    if policy == "EXCLUDE_FAILED_TRIALS":
        included = int(ok.sum())
    else:
        included = int(inputs.scores.shape[0])
        correct = correct + int((inputs.failed & ~inputs.mated).sum())
    accuracy = correct.astype(np.float64) / included if included else np.full(candidates.shape, np.nan)
    best = int(np.nanargmax(accuracy))
    return float(candidates[best]), float(accuracy[best])


def _eer(thresholds: np.ndarray, fmr: np.ndarray, fnmr: np.ndarray) -> dict[str, float | str]:
    """Locate the operating point where FMR and FNMR cross (linear interpolation between adjacent thresholds)."""

    diff = fmr - fnmr
    finite = np.isfinite(thresholds)
    indices = np.where(finite)[0]
    for a, b in zip(indices[:-1], indices[1:], strict=False):
        if diff[a] == 0:
            return {"eer": float(fmr[a]), "threshold": float(thresholds[a]), "method": "EXACT_CROSSING_AT_RECORDED_THRESHOLD"}
        if diff[a] > 0 > diff[b] or diff[a] < 0 < diff[b]:
            weight = abs(diff[a]) / (abs(diff[a]) + abs(diff[b]))
            eer = float((fmr[a] + fnmr[a]) / 2 * (1 - weight) + (fmr[b] + fnmr[b]) / 2 * weight)
            threshold = float(thresholds[a] + (thresholds[b] - thresholds[a]) * weight)
            return {"eer": eer, "threshold": threshold, "method": "LINEAR_INTERPOLATION_BETWEEN_ADJACENT_RECORDED_THRESHOLDS"}
    best = int(np.nanargmin(np.abs(diff[finite])))
    return {"eer": float((fmr[finite][best] + fnmr[finite][best]) / 2), "threshold": float(thresholds[finite][best]), "method": "NEAREST_RECORDED_THRESHOLD_NO_CROSSING"}


def _threshold_at_target_fmr(thresholds: np.ndarray, fmr: np.ndarray, target: float) -> float | None:
    """Smallest recorded threshold whose FMR is <= target (conservative)."""

    candidates = np.where(fmr <= target)[0]
    if candidates.size == 0:
        return None
    return float(thresholds[candidates[0]])


def _fold_accuracy(inputs: ComparisonInputs, policy: str) -> dict[str, object]:
    """Official View 2 protocol: choose the threshold on nine folds, test on the held-out fold."""

    if any(value is None for value in inputs.folds.tolist()):
        return {
            "status": "NOT_ASSESSED",
            "reason": "The protocol authority publishes no fold assignments.",
            "method": "NOT_APPLICABLE_WITHOUT_RECORDED_FOLDS",
            "folds": [],
            "mean_accuracy": None,
            "std_accuracy": None,
        }
    folds = sorted(int(f) for f in np.unique(inputs.folds))
    per_fold: list[dict[str, float | int]] = []
    for held_out in folds:
        train = inputs.folds != held_out
        test = inputs.folds == held_out
        train_inputs = ComparisonInputs(inputs.scores[train], inputs.mated[train], inputs.failed[train], inputs.folds[train])
        test_inputs = ComparisonInputs(inputs.scores[test], inputs.mated[test], inputs.failed[test], inputs.folds[test])
        best_threshold, best_accuracy = _best_accuracy_threshold(train_inputs, policy)
        test_accuracy = _accuracy(test_inputs, best_threshold, policy) if best_threshold is not None else None
        per_fold.append({"fold": held_out, "threshold": best_threshold, "train_accuracy": best_accuracy, "test_accuracy": test_accuracy, "test_trials": int(test.sum())})
    accuracies = [item["test_accuracy"] for item in per_fold if item["test_accuracy"] is not None]
    return {
        "method": "VIEW2_TENFOLD_THRESHOLD_ON_HELD_IN_FOLDS",
        "folds": per_fold,
        "mean_accuracy": float(np.mean(accuracies)) if accuracies else None,
        "std_accuracy": float(np.std(accuracies, ddof=1)) if len(accuracies) > 1 else None,
    }


def _accuracy(inputs: ComparisonInputs, threshold: float, policy: str) -> float:
    ok = ~inputs.failed
    accept = np.zeros_like(inputs.failed, dtype=bool)
    accept[ok] = inputs.scores[ok] >= threshold
    if policy == "EXCLUDE_FAILED_TRIALS":
        included = ok
    else:
        included = np.ones_like(ok, dtype=bool)
    correct = ((accept & inputs.mated) | (~accept & ~inputs.mated)) & included
    return float(correct.sum() / included.sum()) if included.sum() else float("nan")


def compute_verification_metrics(inputs: ComparisonInputs, *, curve_points: int = 200) -> dict[str, object]:
    """Compute the registry-defined 1:1 metrics with explicit semantics; deterministic and JSON-serialisable."""

    counts = {
        "trials": int(inputs.scores.shape[0]),
        "mated_trials": int(inputs.mated.sum()),
        "nonmated_trials": int((~inputs.mated).sum()),
        "failed_trials": int(inputs.failed.sum()),
        "failed_mated_trials": int((inputs.failed & inputs.mated).sum()),
        "failed_nonmated_trials": int((inputs.failed & ~inputs.mated).sum()),
        "completed_trials": int((~inputs.failed).sum()),
    }
    ok = ~inputs.failed
    completed_scores = inputs.scores[ok]
    score_distribution = {
        "mated": _summary(inputs.scores[ok & inputs.mated]),
        "nonmated": _summary(inputs.scores[ok & ~inputs.mated]),
    }
    per_policy: dict[str, object] = {}
    for policy in FAILURE_POLICIES:
        thresholds, fmr, fnmr = _curve(inputs, policy)
        eer = _eer(thresholds, fmr, fnmr)
        operating_points = []
        for target in TARGET_FMRS:
            threshold = _threshold_at_target_fmr(thresholds, fmr, target)
            if threshold is None:
                operating_points.append({"target_fmr": target, "threshold": None, "reason": "no recorded threshold reaches the target FMR"})
            else:
                operating_points.append({"target_fmr": target, **_rates_at(inputs, threshold, policy)})
        # Down-sample the full curve for storage: keep first/last and evenly spaced points, always retaining every distinct FMR step near the low-FMR end.
        finite = np.where(np.isfinite(thresholds))[0]
        if finite.size > curve_points:
            picks = np.unique(np.concatenate((finite[np.linspace(0, finite.size - 1, curve_points).astype(int)], finite[:20], finite[-20:])))
        else:
            picks = finite
        curve = [{"threshold": float(thresholds[i]), "fmr": float(fmr[i]), "fnmr": float(fnmr[i])} for i in picks]
        per_policy[policy] = {
            "eer": eer,
            "operating_points": operating_points,
            "roc": {"definition": REGISTRY_METRICS["roc"], "x": "fmr", "y": "one_minus_fnmr", "points": [{"fmr": p["fmr"], "tmr": 1.0 - p["fnmr"], "threshold": p["threshold"]} for p in curve]},
            "det": {"definition": REGISTRY_METRICS["det"], "x": "fmr", "y": "fnmr", "points": curve},
            "recorded_thresholds": int(finite.size),
            "view2_tenfold": _fold_accuracy(inputs, policy),
        }
    result = {
        "definitions": REGISTRY_METRICS,
        "score_direction": SCORE_DIRECTION,
        "threshold_equality_rule": THRESHOLD_EQUALITY_RULE,
        "failure_policies": list(FAILURE_POLICIES),
        "confidence_interval_method": "CLOPPER_PEARSON_EXACT_BINOMIAL_95 (independence among LFW trials is not established; subject reuse is real)",
        "counts": counts,
        "score_distribution": score_distribution,
        "completed_score_count": int(completed_scores.shape[0]),
        "by_policy": per_policy,
    }
    text = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    result["evidence_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return result


def _summary(values: np.ndarray) -> dict[str, float | int | None]:
    if values.size == 0:
        return {"count": 0, "min": None, "max": None, "mean": None, "median": None, "std": None}
    return {
        "count": int(values.size),
        "min": float(values.min()),
        "max": float(values.max()),
        "mean": float(values.mean()),
        "median": float(np.median(values)),
        "std": float(values.std(ddof=1)) if values.size > 1 else 0.0,
    }
