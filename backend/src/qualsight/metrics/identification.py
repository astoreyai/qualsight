"""1:N identification metrics (PRD §17; QS-X60): FPIR, FNIR, TPIR, rank-k, CMC, true-mate rank, gallery effects.

Registered definitions ``biometric.identification.*@1.0.0`` (metric registry
v1.2.0). Inputs are the per-search candidate lists (already ordered by
descending score, ties broken by the recorded gallery order) rather than the
full similarity matrix, so the same function evaluates a stored candidate
Parquet exactly as it evaluated the live search. Closed-set and open-set
quantities are separate blocks; two failure policies are reported.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

import numpy as np

from .statistics import clopper_pearson

DEFINITIONS = {
    "fpir": ("biometric.identification.fpir", "1.0.0"),
    "fnir": ("biometric.identification.fnir", "1.0.0"),
    "tpir": ("biometric.identification.tpir", "1.0.0"),
    "rank_k": ("biometric.identification.rank-k", "1.0.0"),
    "cmc": ("biometric.identification.cmc", "1.0.0"),
}
RANK_VALUES = (1, 5, 10, 20, 100)
TARGET_FPIRS = (0.1, 0.01, 0.001)
FAILURE_POLICIES = ("EXCLUDE_FAILED_TRIALS", "FAILURES_AS_MISS")
SCORE_DIRECTION = "HIGHER_IS_MORE_SIMILAR"
THRESHOLD_EQUALITY_RULE = "RETURN_WHEN_SCORE_GE_THRESHOLD"


@dataclass(frozen=True, slots=True)
class SearchRecord:
    probe_sample_id: str
    probe_subject_id: str
    kind: str  # MATED | NON_MATED
    failed: bool  # probe template failure
    top_score: float | None  # best candidate score (None when no candidates)
    true_mate_rank: int | None  # rank within the candidate list, None if not present within the list
    true_mate_score: float | None
    true_mate_gallery_failed: bool  # the mate's gallery template failed → cannot be returned


def _rate(events: int, trials: int) -> dict[str, Any]:
    return clopper_pearson(events, trials)


def compute_identification_metrics(records: list[SearchRecord], *, gallery_size: int, gallery_template_failures: int, rank_depth: int, gallery_effect_sizes: list[int] | None = None) -> dict[str, Any]:
    mated = [r for r in records if r.kind == "MATED"]
    non_mated = [r for r in records if r.kind == "NON_MATED"]
    result: dict[str, Any] = {
        "definitions": {name: list(value) for name, value in DEFINITIONS.items()},
        "score_direction": SCORE_DIRECTION, "threshold_equality_rule": THRESHOLD_EQUALITY_RULE, "candidate_list_length": rank_depth,
        "counts": {"gallery_size": gallery_size, "gallery_templates": gallery_size - gallery_template_failures, "gallery_template_failures": gallery_template_failures,
                   "mated_searches": len(mated), "nonmated_searches": len(non_mated), "mated_probe_template_failures": sum(1 for r in mated if r.failed), "nonmated_probe_template_failures": sum(1 for r in non_mated if r.failed),
                   "mated_searches_with_failed_gallery_mate": sum(1 for r in mated if r.true_mate_gallery_failed)},
        "failure_policies": list(FAILURE_POLICIES), "closed_set": {}, "open_set": {},
    }
    ranks_available = [k for k in RANK_VALUES if k <= min(rank_depth, max(gallery_size, 1))]
    for policy in FAILURE_POLICIES:
        if policy == "EXCLUDE_FAILED_TRIALS":
            m_used = [r for r in mated if not r.failed and not r.true_mate_gallery_failed]
            n_used = [r for r in non_mated if not r.failed]
        else:
            m_used = list(mated)
            n_used = list(non_mated)
        # Closed set: rank-k, CMC, true-mate rank distribution over the mated searches.
        def hit_at(k: int) -> int:
            return sum(1 for r in m_used if (not r.failed) and (r.true_mate_rank is not None) and r.true_mate_rank <= k)
        rank_k = {str(k): {**_rate(hit_at(k), len(m_used)), "k": k} for k in ranks_available}
        cmc_points = [{"rank": k, "rate": (hit_at(k) / len(m_used)) if m_used else None} for k in range(1, min(rank_depth, max(gallery_size, 1)) + 1)]
        distribution: dict[str, int] = {}
        for r in m_used:
            if r.failed:
                key = "PROBE_TEMPLATE_FAILED"
            elif r.true_mate_gallery_failed:
                key = "GALLERY_TEMPLATE_FAILED"
            elif r.true_mate_rank is None:
                key = f"BEYOND_RANK_{rank_depth}"
            else:
                key = str(r.true_mate_rank)
            distribution[key] = distribution.get(key, 0) + 1
        result["closed_set"][policy] = {"definition": list(DEFINITIONS["rank_k"]), "mated_searches_included": len(m_used), "rank_k": rank_k, "cmc": {"definition": list(DEFINITIONS["cmc"]), "points": cmc_points}, "true_mate_rank_distribution": dict(sorted(distribution.items(), key=lambda item: (not item[0].isdigit(), int(item[0]) if item[0].isdigit() else 0, item[0])))}
        # Open set: FPIR from the non-mated top scores; FNIR/TPIR at thresholds reaching target FPIR.
        top = np.array([r.top_score if (r.top_score is not None and not r.failed) else -np.inf for r in n_used], dtype=np.float64)
        candidates = np.unique(top[np.isfinite(top)])
        curve = []
        for threshold in sorted(candidates, reverse=True):
            fp = int(np.sum(top >= threshold))
            misses = sum(1 for r in m_used if r.failed or r.true_mate_gallery_failed or r.true_mate_rank is None or r.true_mate_score is None or r.true_mate_score < threshold)
            curve.append({"threshold": float(threshold), "fpir": fp / len(n_used) if n_used else None, "fnir": misses / len(m_used) if m_used else None})
        operating_points = []
        for target in TARGET_FPIRS:
            reachable = [p for p in curve if p["fpir"] is not None and p["fpir"] <= target]
            if not reachable or not n_used:
                operating_points.append({"target_fpir": target, "threshold": None, "reason": "no non-mated search count supports this FPIR" if not n_used else "no threshold reaches the target FPIR", "supported": False})
                continue
            point = max(reachable, key=lambda p: p["fpir"])  # loosest threshold still at or under target
            threshold = point["threshold"]
            fp = int(np.sum(top >= threshold))
            misses = sum(1 for r in m_used if r.failed or r.true_mate_gallery_failed or r.true_mate_rank is None or r.true_mate_score is None or r.true_mate_score < threshold)
            fpir = _rate(fp, len(n_used))
            fnir = _rate(misses, len(m_used))
            expected_events = target * len(n_used)
            operating_points.append({"target_fpir": target, "threshold": float(threshold), "fpir": fpir["rate"], "fpir_ci95": [fpir["ci_lower"], fpir["ci_upper"]], "false_positive_searches": fp,
                                     "fnir": fnir["rate"], "fnir_ci95": [fnir["ci_lower"], fnir["ci_upper"]], "missed_searches": misses, "tpir": (1 - fnir["rate"]) if fnir["rate"] is not None else None,
                                     "mated_included": len(m_used), "nonmated_included": len(n_used), "supported": expected_events >= 5,
                                     "warning": None if expected_events >= 5 else f"fewer than five expected false-positive searches at FPIR {target} ({len(n_used)} non-mated searches); precision unsupported"})
        result["open_set"][policy] = {"definitions": {"fpir": list(DEFINITIONS["fpir"]), "fnir": list(DEFINITIONS["fnir"]), "tpir": list(DEFINITIONS["tpir"])}, "mated_included": len(m_used), "nonmated_included": len(n_used), "operating_points": operating_points, "fpir_fnir_curve": curve[:: max(1, len(curve) // 200)] if len(curve) > 400 else curve}
    result["gallery_effects"] = {"gallery_sizes": [], "note": "populated by the search job by re-evaluating candidate lists restricted to deterministic gallery subsets"}
    text = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    result["evidence_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return result
