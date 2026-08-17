"""Independently re-derive a recorded quality-utility analysis from its own pair table.

Usage: python scripts/verify_utility_analysis.py <analysis directory> [...]

Reads pair_quality.parquet, utility.json, and policy.json and recomputes — with
plain Python loops, not the qualsight.metrics code — the ERC points, AU-ERC,
partial AU-ERC, the policy counts (retention, errors prevented, successful
comparisons removed, FNMR/FMR before and after), the quality-conditioned bin
counts, ROC-AUC by pair counting, and Spearman via ranks. Exit 0 only when every
recomputed number matches the recorded one.
"""

from __future__ import annotations

from itertools import groupby
import json
from pathlib import Path
import sys

import pyarrow.parquet as pq

TOLERANCE = 1e-9


def _fail(message: str) -> None:
    print(f"UTILITY_ANALYSIS_MISMATCH: {message}")
    raise SystemExit(1)


def _close(a: float | None, b: float | None, what: str) -> None:
    if a is None and b is None:
        return
    if a is None or b is None or abs(a - b) > TOLERANCE:
        _fail(f"{what}: recorded {a} vs recomputed {b}")


def _erc_points(units: list[tuple[float, bool]]) -> list[tuple[float, int, float | None]]:
    """(rejected_fraction, retained, error_rate) rejecting worst pair quality first, ties together."""

    ordered = sorted(units, key=lambda item: item[0])
    n = len(ordered)
    points = []
    start = 0
    for _, group in groupby(ordered, key=lambda item: item[0]):
        block = list(group)
        retained = ordered[start:]
        errors = sum(1 for _, error in retained if error)
        points.append((start / n, len(retained), errors / len(retained)))
        start += len(block)
    points.append((1.0, 0, None))
    return points


def _trapezoid(points: list[tuple[float, int, float | None]], max_fraction: float) -> float | None:
    xs, ys = [], []
    for fraction, _, rate in points:
        if rate is None or fraction > max_fraction:
            continue
        xs.append(fraction)
        ys.append(rate)
    if len(xs) < 2:
        return None
    if xs[-1] < max_fraction:
        xs.append(max_fraction)
        ys.append(ys[-1])
    return sum((xs[i + 1] - xs[i]) * (ys[i] + ys[i + 1]) / 2 for i in range(len(xs) - 1))


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    return sxy / (sxx * syy) ** 0.5


def verify(directory: Path) -> dict[str, object]:
    utility = json.loads((directory / "utility.json").read_text(encoding="utf-8"))
    policy = json.loads((directory / "policy.json").read_text(encoding="utf-8"))
    rows = pq.read_table(directory / "pair_quality.parquet").to_pylist()
    threshold = utility["matcher_threshold"]["value"]
    quality_threshold = utility["quality_policy_threshold"]["value"]
    checks = 0
    for kind, label in (("FNMR", "MATED"), ("FMR", "NON_MATED")):
        units = []
        for row in rows:
            if row["label"] != label or row["failure"] is not None or row["score"] is None or row["pair_quality"] is None:
                continue
            error = row["score"] < threshold if kind == "FNMR" else row["score"] >= threshold
            units.append((row["pair_quality"], error))
        recorded = utility["utility"]["erc"]["curves"][kind]
        if recorded["comparison_units"] != len(units):
            _fail(f"{kind} comparison units {recorded['comparison_units']} vs {len(units)}")
        points = _erc_points(units)
        if len(points) != len(recorded["points"]):
            _fail(f"{kind} ERC point count {len(recorded['points'])} vs {len(points)}")
        for (fraction, retained, rate), point in zip(points, recorded["points"]):
            _close(point["rejected_fraction"], fraction, f"{kind} rejected fraction")
            if point["retained"] != retained:
                _fail(f"{kind} retained {point['retained']} vs {retained}")
            _close(point["error_rate"], rate, f"{kind} error rate at {fraction}")
            checks += 1
        areas = utility["utility"]["areas"][kind]
        _close(areas["au_erc"]["value"], _trapezoid(points, 1.0), f"{kind} AU-ERC")
        _close(areas["partial_au_erc"]["value"], _trapezoid(points, areas["partial_au_erc"]["integration_domain"][1]), f"{kind} partial AU-ERC")
        checks += 2
        # Quality-conditioned bins.
        edges = utility["utility"]["quality_conditioned"]["bin_edges"]
        for bin_ in utility["utility"]["quality_conditioned"][kind]["bins"]:
            low, high, last = bin_["quality_low"], bin_["quality_high"], bin_["closed_high"]
            members = [e for q, e in units if q >= low and (q <= high if last else q < high)]
            if bin_["trials"] != len(members) or bin_["events"] != sum(members):
                _fail(f"{kind} bin [{low},{high}) trials/events {bin_['trials']}/{bin_['events']} vs {len(members)}/{sum(members)}")
            checks += 1
        assert edges[0] <= min((q for q, _ in units), default=edges[0])
        # ROC-AUC by pair counting (negative quality predicts the error).
        prediction = utility["utility"]["failure_prediction"][kind]
        if "roc_auc" in prediction:
            positives = [q for q, e in units if e]
            negatives = [q for q, e in units if not e]
            wins = 0.0
            for p in positives:
                for m in negatives:
                    if p < m:
                        wins += 1.0
                    elif p == m:
                        wins += 0.5
            _close(prediction["roc_auc"], wins / (len(positives) * len(negatives)), f"{kind} ROC-AUC")
            checks += 1
        association = utility["utility"]["association"][kind]
        if "spearman_quality_vs_score" in association and association["spearman_quality_vs_score"]["rho"] is not None:
            xs = [row["pair_quality"] for row in rows if row["label"] == label and row["failure"] is None and row["score"] is not None and row["pair_quality"] is not None]
            ys = [row["score"] for row in rows if row["label"] == label and row["failure"] is None and row["score"] is not None and row["pair_quality"] is not None]
            _close(association["spearman_quality_vs_score"]["rho"], _pearson(_ranks(xs), _ranks(ys)), f"{kind} Spearman")
            checks += 1
    # Policy block by brute force.
    usable = [r for r in rows if r["failure"] is None and r["score"] is not None and r["pair_quality"] is not None]
    retained = [r for r in usable if r["pair_quality"] >= quality_threshold]
    rejected = [r for r in usable if not (r["pair_quality"] >= quality_threshold)]

    def fnm(r: dict) -> bool:
        return r["label"] == "MATED" and r["score"] < threshold

    def fm(r: dict) -> bool:
        return r["label"] == "NON_MATED" and r["score"] >= threshold

    counts = policy["counts"]
    if (counts["usable_units"], counts["retained"], counts["rejected"]) != (len(usable), len(retained), len(rejected)):
        _fail("policy counts differ")
    impact = policy["impact"]
    if impact["false_non_matches_prevented"] != sum(1 for r in rejected if fnm(r)) or impact["false_matches_prevented"] != sum(1 for r in rejected if fm(r)):
        _fail("errors prevented differ")
    if impact["successful_comparisons_removed"] != sum(1 for r in rejected if not fnm(r) and not fm(r)):
        _fail("successful comparisons removed differ")
    mated_after = [r for r in retained if r["label"] == "MATED"]
    _close(policy["after_policy"]["fnmr"]["rate"], (sum(1 for r in mated_after if fnm(r)) / len(mated_after)) if mated_after else None, "FNMR after policy")
    mated_before = [r for r in usable if r["label"] == "MATED"]
    _close(policy["before_policy"]["fnmr"]["rate"], (sum(1 for r in mated_before if fnm(r)) / len(mated_before)) if mated_before else None, "FNMR before policy")
    for row in rows:
        if row["pair_quality"] is not None and row["retained_by_quality_policy"] != (row["pair_quality"] >= quality_threshold):
            _fail(f"pair {row['pair_id']} retention flag differs")
    checks += 6
    return {"directory": str(directory), "checks": checks, "comparison_units": len(rows), "usable_units": len(usable)}


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for item in argv:
        summary = verify(Path(item))
        print("UTILITY_ANALYSIS_VERIFIED " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
