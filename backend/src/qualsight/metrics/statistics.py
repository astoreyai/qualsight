"""Executable statistical components for quality-utility analysis (PRD §§16, 20.7–20.8, 28).

Everything here is implemented on numpy only so the numbers a Run records are
reproducible from the pinned lock file. Each function returns the method
metadata PRD §28 requires alongside the estimate (method, parameters,
confidence level, seed, sampling unit, replicates) and flags when the real
sample cannot support the estimate instead of returning a number that
implies unsupported precision.
"""

from __future__ import annotations

import math
from typing import Callable

import numpy as np

from .verification import _binom_interval

METHODS_VERSION = "1.0.0"
CONFIDENCE_LEVEL = 0.95


def clopper_pearson(events: int, trials: int) -> dict[str, float | int | None]:
    lower, upper = _binom_interval(events, trials)
    return {
        "events": int(events),
        "trials": int(trials),
        "rate": (events / trials) if trials else None,
        "ci_lower": lower,
        "ci_upper": upper,
        "ci_method": "CLOPPER_PEARSON_EXACT_BINOMIAL",
        "confidence_level": CONFIDENCE_LEVEL,
    }


def average_ranks(values: np.ndarray) -> np.ndarray:
    """Ranks starting at 1 with ties assigned the average rank."""

    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.shape[0], dtype=np.float64)
    sorted_values = values[order]
    index = 0
    n = values.shape[0]
    while index < n:
        end = index
        while end + 1 < n and sorted_values[end + 1] == sorted_values[index]:
            end += 1
        ranks[order[index : end + 1]] = (index + end) / 2.0 + 1.0
        index = end + 1
    return ranks


def pearson(x: np.ndarray, y: np.ndarray) -> float | None:
    if x.shape[0] < 3 or np.std(x) == 0 or np.std(y) == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def spearman(x: np.ndarray, y: np.ndarray) -> dict[str, object]:
    n = int(x.shape[0])
    rho = pearson(average_ranks(x), average_ranks(y)) if n >= 3 else None
    result: dict[str, object] = {"method": "SPEARMAN_RANK_CORRELATION", "version": METHODS_VERSION, "n": n, "rho": rho}
    if rho is not None and n > 3 and abs(rho) < 1:
        # Fisher z interval (large-sample approximation; recorded as such).
        z = math.atanh(rho)
        half = 1.959963984540054 / math.sqrt(n - 3)
        result["ci_lower"], result["ci_upper"] = math.tanh(z - half), math.tanh(z + half)
        result["ci_method"] = "FISHER_Z_LARGE_SAMPLE"
        result["confidence_level"] = CONFIDENCE_LEVEL
    else:
        result["ci_lower"] = result["ci_upper"] = None
        result["ci_method"] = "UNSUPPORTED"
    return result


def kendall_tau_b(x: np.ndarray, y: np.ndarray, *, chunk: int = 512) -> dict[str, object]:
    """Kendall tau-b with tie correction, evaluated over all pairs in blocks."""

    n = int(x.shape[0])
    if n < 3:
        return {"method": "KENDALL_TAU_B", "version": METHODS_VERSION, "n": n, "tau": None, "ci_method": "UNSUPPORTED"}
    concordant = discordant = 0
    ties_x = ties_y = 0
    for start in range(0, n, chunk):
        stop = min(start + chunk, n)
        dx = np.sign(x[start:stop, None] - x[None, :])
        dy = np.sign(y[start:stop, None] - y[None, :])
        # Only count each unordered pair once: j > i.
        j_index = np.arange(n)[None, :]
        i_index = np.arange(start, stop)[:, None]
        mask = j_index > i_index
        product = dx * dy
        concordant += int(np.sum((product > 0) & mask))
        discordant += int(np.sum((product < 0) & mask))
        ties_x += int(np.sum((dx == 0) & (dy != 0) & mask))
        ties_y += int(np.sum((dy == 0) & (dx != 0) & mask))
    denominator = math.sqrt((concordant + discordant + ties_x) * (concordant + discordant + ties_y))
    tau = (concordant - discordant) / denominator if denominator > 0 else None
    result: dict[str, object] = {"method": "KENDALL_TAU_B", "version": METHODS_VERSION, "n": n, "tau": tau, "concordant": concordant, "discordant": discordant, "ties_x_only": ties_x, "ties_y_only": ties_y}
    if tau is not None and n >= 10:
        # Normal approximation for tau under H0 with no ties, recorded as approximate.
        se = math.sqrt(2 * (2 * n + 5) / (9 * n * (n - 1)))
        result["z_under_null"] = tau / se
        result["ci_method"] = "NORMAL_APPROXIMATION_UNDER_NULL_VARIANCE"
    else:
        result["ci_method"] = "UNSUPPORTED"
    return result


def roc_auc(scores: np.ndarray, positives: np.ndarray) -> float | None:
    """ROC-AUC (probability a positive outranks a negative; ties count one half) via Mann–Whitney ranks."""

    n_pos = int(np.sum(positives))
    n_neg = int(positives.shape[0] - n_pos)
    if n_pos == 0 or n_neg == 0:
        return None
    ranks = average_ranks(scores)
    rank_sum = float(np.sum(ranks[positives]))
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def average_precision(scores: np.ndarray, positives: np.ndarray) -> float | None:
    """Area under the precision–recall curve as average precision (step-wise, ties grouped)."""

    n_pos = int(np.sum(positives))
    if n_pos == 0:
        return None
    order = np.argsort(-scores, kind="mergesort")
    sorted_scores = scores[order]
    sorted_pos = positives[order].astype(np.float64)
    tp = np.cumsum(sorted_pos)
    ranks = np.arange(1, scores.shape[0] + 1, dtype=np.float64)
    # Evaluate at the last index of each tie group so ties are grouped.
    last_of_group = np.concatenate((sorted_scores[1:] != sorted_scores[:-1], [True]))
    first_of_group = np.concatenate(([True], sorted_scores[1:] != sorted_scores[:-1]))
    tp_before = np.concatenate(([0.0], tp[:-1]))
    positives_in_group = tp[last_of_group] - tp_before[first_of_group]
    precision_at_group = tp[last_of_group] / ranks[last_of_group]
    return float(np.sum(precision_at_group * positives_in_group) / n_pos)


def classifier_summary(scores: np.ndarray, positives: np.ndarray, *, cutoff: float, positive_when: str) -> dict[str, object]:
    """Sensitivity, specificity, precision, recall of the rule `score <cmp> cutoff` for predicting positives."""

    if positive_when == "SCORE_LE_CUTOFF":
        predicted = scores <= cutoff
    elif positive_when == "SCORE_GE_CUTOFF":
        predicted = scores >= cutoff
    else:
        raise ValueError(positive_when)
    tp = int(np.sum(predicted & positives))
    fp = int(np.sum(predicted & ~positives))
    fn = int(np.sum(~predicted & positives))
    tn = int(np.sum(~predicted & ~positives))
    return {
        "cutoff": float(cutoff),
        "rule": positive_when,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "sensitivity": clopper_pearson(tp, tp + fn),
        "specificity": clopper_pearson(tn, tn + fp),
        "precision": clopper_pearson(tp, tp + fp),
        "recall": clopper_pearson(tp, tp + fn),
    }


def logistic_regression_multi(design: np.ndarray, y: np.ndarray, names: list[str], *, min_events_per_parameter: int = 10, max_iterations: int = 100) -> dict[str, object]:
    """Multivariable logistic regression y ~ design (an intercept column is prepended) by Newton–Raphson with Wald intervals.

    Predictor columns are standardized internally for the solve; coefficients are
    reported per original unit. Runs only when both outcome classes carry
    ``min_events_per_parameter`` events per estimated parameter.
    """

    n, p = design.shape
    events = int(np.sum(y))
    non_events = n - events
    parameters = p + 1
    result: dict[str, object] = {"method": "LOGISTIC_REGRESSION_NEWTON_RAPHSON", "version": METHODS_VERSION, "n": n, "events": events, "non_events": non_events, "parameters": parameters, "terms": ["intercept", *names], "support_rule": f"min {min_events_per_parameter} events and non-events per parameter"}
    if events < parameters * min_events_per_parameter or non_events < parameters * min_events_per_parameter:
        result["status"] = "UNSUPPORTED_BY_SAMPLE"
        return result
    means = design.mean(axis=0)
    sds = design.std(axis=0, ddof=1)
    if np.any(sds == 0):
        result["status"] = "UNSUPPORTED_BY_SAMPLE"
        result["reason"] = "a predictor has zero variance"
        return result
    z = (design - means) / sds
    x = np.column_stack((np.ones(n), z))
    beta = np.zeros(parameters)
    converged = False
    iterations = 0
    for iterations in range(1, max_iterations + 1):
        eta = x @ beta
        prob = 1.0 / (1.0 + np.exp(-eta))
        w = prob * (1 - prob)
        gradient = x.T @ (y - prob)
        hessian = x.T @ (x * w[:, None])
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            break
        beta = beta + step
        if np.max(np.abs(step)) < 1e-10:
            converged = True
            break
    eta = x @ beta
    prob = 1.0 / (1.0 + np.exp(-eta))
    w = prob * (1 - prob)
    hessian = x.T @ (x * w[:, None])
    try:
        covariance = np.linalg.inv(hessian)
    except np.linalg.LinAlgError:
        covariance = None
    log_likelihood = float(np.sum(y * np.log(np.clip(prob, 1e-300, 1)) + (1 - y) * np.log(np.clip(1 - prob, 1e-300, 1))))
    estimated = converged and covariance is not None and bool(np.all(np.abs(beta) < 25))
    result.update({"status": "ESTIMATED" if estimated else "NOT_CONVERGED_OR_SEPARATED", "converged": converged, "iterations": iterations, "log_likelihood": log_likelihood})
    if estimated:
        se_std = np.sqrt(np.diag(covariance))
        half = 1.959963984540054
        coefficients = []
        for index, name in enumerate(names):
            coefficient = float(beta[index + 1] / sds[index])
            se = float(se_std[index + 1] / sds[index])
            wald_z = coefficient / se if se > 0 else None
            coefficients.append({"term": name, "coefficient": coefficient, "se": se, "ci": [coefficient - half * se, coefficient + half * se], "z": wald_z, "p_two_sided": _normal_two_sided_p(wald_z) if wald_z is not None else None})
        result.update({"intercept": float(beta[0] - np.sum(beta[1:] * means / sds)), "coefficients": coefficients, "ci_method": "WALD_NORMAL", "confidence_level": CONFIDENCE_LEVEL})
    return result


def _normal_two_sided_p(z: float) -> float:
    return float(math.erfc(abs(z) / math.sqrt(2.0)))


def logistic_regression(x: np.ndarray, y: np.ndarray, *, min_events_per_parameter: int = 10, max_iterations: int = 50) -> dict[str, object]:
    """Univariate logistic regression y ~ intercept + slope·x by Newton–Raphson with Wald intervals.

    Runs only when both outcome classes carry at least ``min_events_per_parameter``
    events per estimated parameter (two parameters); otherwise the model is
    reported as UNSUPPORTED with the counts that failed the rule.
    """

    n = int(x.shape[0])
    events = int(np.sum(y))
    non_events = n - events
    result: dict[str, object] = {"method": "LOGISTIC_REGRESSION_NEWTON_RAPHSON", "version": METHODS_VERSION, "n": n, "events": events, "non_events": non_events, "parameters": 2, "support_rule": f"min {min_events_per_parameter} events and non-events per parameter"}
    if events < 2 * min_events_per_parameter or non_events < 2 * min_events_per_parameter:
        result["status"] = "UNSUPPORTED_BY_SAMPLE"
        return result
    x_mean, x_sd = float(np.mean(x)), float(np.std(x, ddof=1))
    if x_sd == 0:
        result["status"] = "UNSUPPORTED_BY_SAMPLE"
        result["reason"] = "predictor has zero variance"
        return result
    z = (x - x_mean) / x_sd
    design = np.column_stack((np.ones(n), z))
    beta = np.zeros(2)
    converged = False
    iterations = 0
    for iterations in range(1, max_iterations + 1):
        eta = design @ beta
        p = 1.0 / (1.0 + np.exp(-eta))
        w = p * (1 - p)
        gradient = design.T @ (y - p)
        hessian = design.T @ (design * w[:, None])
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            break
        beta = beta + step
        if np.max(np.abs(step)) < 1e-10:
            converged = True
            break
    eta = design @ beta
    p = 1.0 / (1.0 + np.exp(-eta))
    w = p * (1 - p)
    hessian = design.T @ (design * w[:, None])
    try:
        covariance = np.linalg.inv(hessian)
    except np.linalg.LinAlgError:
        covariance = None
    log_likelihood = float(np.sum(y * np.log(np.clip(p, 1e-300, 1)) + (1 - y) * np.log(np.clip(1 - p, 1e-300, 1))))
    result.update({"status": "ESTIMATED" if converged and covariance is not None and abs(beta[1]) < 25 else "NOT_CONVERGED_OR_SEPARATED", "converged": converged, "iterations": iterations, "log_likelihood": log_likelihood, "standardization": {"mean": x_mean, "sd": x_sd}})
    if result["status"] == "ESTIMATED":
        se = np.sqrt(np.diag(covariance))
        slope_raw = float(beta[1] / x_sd)
        se_raw = float(se[1] / x_sd)
        half = 1.959963984540054
        result.update({
            "intercept": float(beta[0] - beta[1] * x_mean / x_sd),
            "coefficient_per_unit": slope_raw,
            "coefficient_se": se_raw,
            "coefficient_ci": [slope_raw - half * se_raw, slope_raw + half * se_raw],
            "coefficient_per_sd": float(beta[1]),
            "odds_ratio_per_10_units": math.exp(10 * slope_raw),
            "odds_ratio_per_10_units_ci": [math.exp(10 * (slope_raw - half * se_raw)), math.exp(10 * (slope_raw + half * se_raw))],
            "ci_method": "WALD_NORMAL",
            "confidence_level": CONFIDENCE_LEVEL,
        })
    return result


def cluster_bootstrap(cluster_ids: np.ndarray, statistic: Callable[[np.ndarray], float | None], *, replicates: int, seed: int) -> dict[str, object]:
    """Resample clusters (subjects) with replacement and evaluate ``statistic`` on the pooled member indices.

    ``statistic`` receives an index array into the original units (with
    repeats) and returns a float or ``None`` when undefined for that replicate.
    """

    unique, inverse = np.unique(cluster_ids, return_inverse=True)
    members = [np.flatnonzero(inverse == k) for k in range(unique.shape[0])]
    generator = np.random.default_rng(seed)
    values: list[float] = []
    undefined = 0
    for _ in range(replicates):
        draw = generator.integers(0, unique.shape[0], size=unique.shape[0])
        indices = np.concatenate([members[k] for k in draw]) if unique.shape[0] else np.array([], dtype=np.int64)
        value = statistic(indices)
        if value is None or not math.isfinite(value):
            undefined += 1
        else:
            values.append(float(value))
    array = np.array(values, dtype=np.float64)
    result: dict[str, object] = {
        "method": "CLUSTER_BOOTSTRAP_PERCENTILE",
        "version": METHODS_VERSION,
        "sampling_unit": "CLUSTER_ID_SUPPLIED_BY_CALLER",
        "clusters": int(unique.shape[0]),
        "replicates": int(replicates),
        "undefined_replicates": undefined,
        "seed": int(seed),
        "confidence_level": CONFIDENCE_LEVEL,
    }
    if array.shape[0] >= max(20, replicates // 2):
        result.update({"ci_lower": float(np.percentile(array, 2.5)), "ci_upper": float(np.percentile(array, 97.5)), "bootstrap_mean": float(np.mean(array)), "bootstrap_sd": float(np.std(array, ddof=1)) if array.shape[0] > 1 else None, "status": "ESTIMATED"})
    else:
        result.update({"ci_lower": None, "ci_upper": None, "status": "UNSUPPORTED_BY_SAMPLE"})
    return result


def pair_subject_multi_membership_bootstrap(
    left_subject_ids: np.ndarray,
    right_subject_ids: np.ndarray,
    statistic: Callable[[np.ndarray], float | None],
    *,
    replicates: int,
    seed: int,
) -> dict[str, object]:
    """Bootstrap pair units by every recorded subject membership.

    Subjects are sampled with replacement. A same-subject pair receives that
    subject's sampled multiplicity. A pair with two different subjects receives
    the product of their sampled multiplicities, so either endpoint can remove
    or replicate the pair. ``statistic`` receives the resulting repeated row
    indices into the original pair-unit arrays.
    """

    if left_subject_ids.shape != right_subject_ids.shape:
        raise ValueError("left and right subject arrays must have the same shape")
    if left_subject_ids.ndim != 1:
        raise ValueError("pair subject arrays must be one-dimensional")
    if any(value is None for value in left_subject_ids.tolist() + right_subject_ids.tolist()):
        raise ValueError("pair subject bootstrap requires both subject identities for every pair")
    subjects, membership_indices = np.unique(
        np.concatenate((left_subject_ids, right_subject_ids)), return_inverse=True
    )
    unit_count = left_subject_ids.shape[0]
    left_index = membership_indices[:unit_count]
    right_index = membership_indices[unit_count:]
    same_subject = left_index == right_index
    generator = np.random.default_rng(seed)
    values: list[float] = []
    undefined = 0
    unit_indices = np.arange(unit_count, dtype=np.int64)
    for _ in range(replicates):
        draw = generator.integers(0, subjects.shape[0], size=subjects.shape[0])
        subject_multiplicity = np.bincount(draw, minlength=subjects.shape[0])
        multiplicity = subject_multiplicity[left_index] * subject_multiplicity[right_index]
        multiplicity[same_subject] = subject_multiplicity[left_index[same_subject]]
        indices = np.repeat(unit_indices, multiplicity)
        value = statistic(indices)
        if value is None or not math.isfinite(value):
            undefined += 1
        else:
            values.append(float(value))
    array = np.array(values, dtype=np.float64)
    result: dict[str, object] = {
        "method": "PAIR_SUBJECT_MULTI_MEMBERSHIP_BOOTSTRAP_PERCENTILE",
        "version": METHODS_VERSION,
        "sampling_unit": "SUBJECT_ID_IN_EITHER_PAIR_POSITION",
        "subjects": int(subjects.shape[0]),
        "pair_units": int(unit_count),
        "same_subject_pair_units": int(np.sum(same_subject)),
        "replicates": int(replicates),
        "undefined_replicates": undefined,
        "seed": int(seed),
        "confidence_level": CONFIDENCE_LEVEL,
    }
    if array.shape[0] >= max(20, replicates // 2):
        result.update(
            {
                "ci_lower": float(np.percentile(array, 2.5)),
                "ci_upper": float(np.percentile(array, 97.5)),
                "bootstrap_mean": float(np.mean(array)),
                "bootstrap_sd": float(np.std(array, ddof=1)) if array.shape[0] > 1 else None,
                "status": "ESTIMATED",
            }
        )
    else:
        result.update({"ci_lower": None, "ci_upper": None, "status": "UNSUPPORTED_BY_SAMPLE"})
    return result
