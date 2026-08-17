"""Independently re-derive a recorded cross-matcher analysis from its contrast pair table and the source Runs.

Usage: python scripts/verify_cross_matcher.py <analysis directory> [<database path>]

For every contrast, plain-Python loops recompute from ``contrast_pairs.parquet``
the error-overlap counts (A only / B only / both / neither) for mated and
non-mated units scored by both Runs, the processing-failure overlap, the
failure-set Jaccard values, the transferred-threshold FMR/FNMR, and the AU-ERC
of each Run's FNMR-ERC over the shared units; when a database path is given the
per-pair scores are also cross-checked against the leaf comparison Parquets
recorded in the store. Exit 0 only when every number matches.
"""

from __future__ import annotations

from itertools import groupby
import json
from pathlib import Path
import sqlite3
import sys

import pyarrow.parquet as pq

TOLERANCE = 1e-9


def _fail(message: str) -> None:
    print(f"CROSS_MATCHER_MISMATCH: {message}")
    raise SystemExit(1)


def _close(a: float | None, b: float | None, what: str) -> None:
    if a is None and b is None:
        return
    if a is None or b is None or abs(a - b) > TOLERANCE:
        _fail(f"{what}: recorded {a} vs recomputed {b}")


def _au_erc(units: list[tuple[float, bool]], upper: float) -> float | None:
    ordered = sorted(units, key=lambda item: item[0])
    n = len(ordered)
    if n < 2:
        return None
    xs, ys = [], []
    start = 0
    for _, group in groupby(ordered, key=lambda item: item[0]):
        block = list(group)
        fraction = start / n
        if fraction <= upper:
            retained = ordered[start:]
            xs.append(fraction)
            ys.append(sum(1 for _, e in retained if e) / len(retained))
        start += len(block)
    if len(xs) < 2:
        return None
    if xs[-1] < upper:
        xs.append(upper)
        ys.append(ys[-1])
    return sum((xs[i + 1] - xs[i]) * (ys[i] + ys[i + 1]) / 2 for i in range(len(xs) - 1))


def verify(directory: Path, database: Path | None) -> dict[str, object]:
    document = json.loads((directory / "crossmatcher.json").read_text(encoding="utf-8"))
    rows = pq.read_table(directory / "contrast_pairs.parquet").to_pylist()
    by_contrast: dict[str, list[dict]] = {}
    for row in rows:
        by_contrast.setdefault(row["contrast_id"], []).append(row)
    checks = 0
    leaf_scores: dict[str, dict[str, float | None]] = {}
    if database is not None:
        connection = sqlite3.connect(str(database))
        for run_id, parquet in connection.execute("SELECT run_id, comparisons_parquet FROM matcher_leaves"):
            leaf_scores[run_id] = {r["pair_id"]: r["score"] for r in pq.read_table(parquet).to_pylist()}
        connection.close()
    for contrast in document["contrasts"]:
        pairs = by_contrast.get(contrast["contrast_id"], [])
        if len(pairs) != contrast["error_overlap"]["processing_failures"]["units"]:
            _fail(f"{contrast['contrast_id']}: pair rows {len(pairs)} vs units {contrast['error_overlap']['processing_failures']['units']}")
        ta, tb = contrast["a"]["threshold"], contrast["b"]["threshold"]
        if leaf_scores:
            sa, sb = leaf_scores[contrast["a"]["run_id"]], leaf_scores[contrast["b"]["run_id"]]
            for row in pairs:
                if sa[row["pair_id"]] != row["score_a"] or sb[row["pair_id"]] != row["score_b"]:
                    _fail(f"{contrast['contrast_id']}: pair {row['pair_id']} scores differ from the leaf Runs")
            checks += 1
        both = [r for r in pairs if r["failure_a"] is None and r["failure_b"] is None and r["score_a"] is not None and r["score_b"] is not None]
        for label, key in (("MATED", "mated_false_non_match"), ("NON_MATED", "non_mated_false_match")):
            units = [r for r in both if r["label"] == label]
            def err(r: dict, s: str, t: float) -> bool:
                return (r[s] < t) if label == "MATED" else (r[s] >= t)
            counts = {"units": len(units), "A_ONLY": sum(1 for r in units if err(r, "score_a", ta) and not err(r, "score_b", tb)), "B_ONLY": sum(1 for r in units if not err(r, "score_a", ta) and err(r, "score_b", tb)),
                      "BOTH": sum(1 for r in units if err(r, "score_a", ta) and err(r, "score_b", tb)), "NEITHER": sum(1 for r in units if not err(r, "score_a", ta) and not err(r, "score_b", tb))}
            recorded = contrast["error_overlap"][key]
            for name, value in counts.items():
                if recorded[name] != value:
                    _fail(f"{contrast['contrast_id']} {key} {name}: {recorded[name]} vs {value}")
            union = counts["A_ONLY"] + counts["B_ONLY"] + counts["BOTH"]
            _close(contrast["failure_set_jaccard"][key], (counts["BOTH"] / union) if union else None, f"{contrast['contrast_id']} {key} jaccard")
            checks += 6
            # Category column agrees with the recomputed decision.
            for r in units:
                expected = "BOTH" if err(r, "score_a", ta) and err(r, "score_b", tb) else "A_ONLY" if err(r, "score_a", ta) else "B_ONLY" if err(r, "score_b", tb) else "NEITHER"
                if r["category"] != expected:
                    _fail(f"{contrast['contrast_id']} pair {r['pair_id']} category {r['category']} vs {expected}")
        failures = {"A_ONLY": sum(1 for r in pairs if r["failure_a"] is not None and r["failure_b"] is None), "B_ONLY": sum(1 for r in pairs if r["failure_a"] is None and r["failure_b"] is not None),
                    "BOTH": sum(1 for r in pairs if r["failure_a"] is not None and r["failure_b"] is not None), "NEITHER": sum(1 for r in pairs if r["failure_a"] is None and r["failure_b"] is None)}
        for name, value in failures.items():
            if contrast["error_overlap"]["processing_failures"][name] != value:
                _fail(f"{contrast['contrast_id']} processing failures {name}: {contrast['error_overlap']['processing_failures'][name]} vs {value}")
        checks += 4
        # Transferred thresholds (own units, EXCLUDE_FAILED_TRIALS).
        for direction, side, other_threshold in (("a_threshold_on_b", "b", ta), ("b_threshold_on_a", "a", tb)):
            scored = [r for r in pairs if r[f"failure_{side}"] is None and r[f"score_{side}"] is not None]
            mated = [r for r in scored if r["label"] == "MATED"]
            non_mated = [r for r in scored if r["label"] == "NON_MATED"]
            block = contrast["threshold_transferability"][direction]
            _close(block["fnmr"]["rate"], (sum(1 for r in mated if r[f"score_{side}"] < other_threshold) / len(mated)) if mated else None, f"{contrast['contrast_id']} {direction} FNMR")
            _close(block["fmr"]["rate"], (sum(1 for r in non_mated if r[f"score_{side}"] >= other_threshold) / len(non_mated)) if non_mated else None, f"{contrast['contrast_id']} {direction} FMR")
            checks += 2
        # AU-ERC of each Run over the shared units with quality.
        shared = [r for r in both if r["pair_quality"] is not None and r["label"] == "MATED"]
        recorded = contrast["au_erc_differences"]["FNMR"]
        for side, threshold, key in (("a", ta, "a"), ("b", tb, "b")):
            units = [(r["pair_quality"], r[f"score_{side}"] < threshold) for r in shared]
            _close(recorded["au_erc"][key], _au_erc(units, 1.0), f"{contrast['contrast_id']} AU-ERC {side}")
            _close(recorded["partial_au_erc"][key], _au_erc(units, recorded["partial_au_erc"]["integration_domain"][1]), f"{contrast['contrast_id']} partial AU-ERC {side}")
            checks += 2
        _close(recorded["au_erc"]["difference_a_minus_b"], None if recorded["au_erc"]["a"] is None or recorded["au_erc"]["b"] is None else recorded["au_erc"]["a"] - recorded["au_erc"]["b"], f"{contrast['contrast_id']} AU-ERC difference")
        checks += 1
    return {"directory": str(directory), "contrasts": len(document["contrasts"]), "pair_rows": len(rows), "checks": checks, "scores_cross_checked_against_store": bool(leaf_scores)}


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    database = Path(argv[1]) if len(argv) > 1 else None
    summary = verify(Path(argv[0]), database)
    print("CROSS_MATCHER_VERIFIED " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
