"""Pair and sample visual evidence (Plan D QS-Y85/Y86/Y87; Plan E QS-Z91/Z92).

Everything served here is derived from recorded evidence or recomputed
deterministically from recorded bytes under a recorded frozen definition:

* Detection geometry (QS-Y85) runs the registered SCRFD-10G detector — the
  **matcher pipeline's** detector — on a recorded sample under a frozen
  condition's canvas rule. These are never presented as OFIQ's internal
  landmarks; the label travels in the payload.
* The aligned 112x112 crops (QS-Y86) are recomputed with the same frozen,
  versioned preprocessing code that produced the compared templates; the crop
  hash is reported so the reader can see exactly which bytes were compared.
* The match-evidence payload (QS-Y87) places the pair's recorded score on the
  Run's own genuine/impostor score distributions with the recorded thresholds
  and their kinds. No saliency or causal claim is ever generated (QS-Z92): the
  quality attribution (QS-Z91) is a ranked table of recorded measures, worded
  as association, never causation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from ..matchers.conditions import CONDITION_DEFINITIONS, ConditionId, preprocess
from ..matchers.detection import SCRFDDetector
from ..provenance import sha256_file

HONESTY_STATEMENT = (
    "Recorded evidence only: scores, thresholds, and quality measures come from immutable Runs; "
    "overlays show the matcher pipeline's SCRFD-10G detection, not OFIQ's internal landmarks; "
    "aligned crops are recomputed deterministically under the recorded frozen preprocessing "
    "condition; associations between quality and errors are not causal claims; nothing here is an "
    "identity decision or an ISO/IEC certification."
)


class EvidenceError(RuntimeError):
    """Raised when evidence cannot be derived from recorded data honestly."""


def detection_geometry(
    detector: SCRFDDetector, image_path: str, expected_sha256: str, condition_id: str | None
) -> dict[str, Any]:
    """QS-Y85: SCRFD-10G boxes + five landmarks on a recorded sample, hash-verified first."""

    path = Path(image_path)
    if sha256_file(path) != expected_sha256:
        raise EvidenceError(f"{image_path} drifted from its recorded hash")
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise EvidenceError(f"{image_path} does not decode as an image")
    try:
        condition = ConditionId(condition_id) if condition_id else ConditionId.ADAPTIVE_LARGEST_JPEG95
    except ValueError as error:
        raise EvidenceError(f"unknown preprocessing condition {condition_id}") from error
    result = preprocess(image, condition, detector)
    detections = detector.detect(image, result.canvas)
    chosen_index = None
    if result.detection is not None:
        for index, item in enumerate(detections):
            if np.array_equal(item.bbox, result.detection.bbox):
                chosen_index = index
                break
    return {
        "source": "MATCHER_PIPELINE_SCRFD10G",
        "label": "SCRFD-10G detection used by the frozen matcher pipeline — not OFIQ's internal landmarks",
        "condition_id": condition.value,
        "condition": CONDITION_DEFINITIONS[condition],
        "image": {"path": image_path, "sha256": expected_sha256, "width": image.shape[1], "height": image.shape[0]},
        "canvas": result.canvas,
        "face_count": result.face_count,
        "chosen_index": chosen_index,
        "selection_rule": str(CONDITION_DEFINITIONS[condition]["selection"]),
        "failure": result.failure,
        "detections": [
            {
                "bbox": [float(v) for v in item.bbox],
                "score": float(item.score),
                "landmarks": [[float(x), float(y)] for x, y in item.landmarks],
            }
            for item in detections
        ],
    }


def recompute_crop(detector: SCRFDDetector, image_path: str, expected_sha256: str, condition_id: str) -> tuple[np.ndarray, str]:
    """QS-Y86: the aligned 112x112 crop under the recorded frozen condition; returns (BGR crop, crop sha)."""

    path = Path(image_path)
    if sha256_file(path) != expected_sha256:
        raise EvidenceError(f"{image_path} drifted from its recorded hash")
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise EvidenceError(f"{image_path} does not decode as an image")
    try:
        condition = ConditionId(condition_id)
    except ValueError as error:
        raise EvidenceError(
            f"condition {condition_id} is not a frozen preprocessing condition (externally computed Runs carry no crops)"
        ) from error
    result = preprocess(image, condition, detector)
    if result.crop_bgr is None:
        raise EvidenceError(f"no face passes the detector under {condition_id}: {result.failure}")
    return result.crop_bgr, result.crop_sha256 or ""


def _comparison_rows(parquet_path: str) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    return pq.read_table(parquet_path).to_pylist()


def _histogram(scores: list[float], bins: int, lo: float, hi: float) -> dict[str, Any]:
    counts, edges = np.histogram(np.asarray(scores, dtype=np.float64), bins=bins, range=(lo, hi))
    return {"counts": [int(v) for v in counts], "edges": [float(v) for v in edges]}


def pair_evidence(store: Any, leaf_row: dict[str, Any], pair_id: str) -> dict[str, Any]:
    """QS-Y87: everything recorded about one compared pair, plus population context."""

    rows = _comparison_rows(leaf_row["comparisons_parquet"])
    target = next((row for row in rows if row["pair_id"] == pair_id), None)
    if target is None:
        raise EvidenceError(f"pair {pair_id} is not part of this Run's recorded comparisons")
    metrics = json.loads(leaf_row["metrics_json"])
    excl = metrics["by_policy"]["EXCLUDE_FAILED_TRIALS"]
    thresholds: list[dict[str, Any]] = []
    eer = excl.get("eer") or {}
    if eer.get("threshold") is not None:
        thresholds.append({"kind": "MATCHER_THRESHOLD", "name": "EER threshold", "threshold": eer["threshold"], "policy": "EXCLUDE_FAILED_TRIALS", "method": eer.get("method")})
    for point in excl.get("operating_points", []):
        if point.get("threshold") is not None:
            thresholds.append({"kind": "MATCHER_THRESHOLD", "name": f"FMR <= {point['target_fmr']:g}", "threshold": point["threshold"], "policy": "EXCLUDE_FAILED_TRIALS"})
    genuine = [row["score"] for row in rows if row["label"] == "MATED" and row["score"] is not None]
    impostor = [row["score"] for row in rows if row["label"] == "NON_MATED" and row["score"] is not None]
    scored = genuine + impostor
    if scored:
        lo, hi = float(min(scored)), float(max(scored))
        if lo == hi:
            lo, hi = lo - 0.5, hi + 0.5
    else:
        lo, hi = -1.0, 1.0
    sides: dict[str, Any] = {}
    for side in ("left", "right"):
        sample_id = target[f"{side}_sample_id"]
        sample = store.sample(sample_id)
        quality = None
        for record in store.quality_results_for_sample(sample_id):
            if record["dataset_id"] == leaf_row["dataset_id"] and record["outcome"] == "MEASURED":
                measures = record["measures"]
                quality = {
                    "job_id": record["job_id"],
                    "unified_quality_score": record["uqs_scalar"],
                    "failed_measures": sorted(name for name, value in measures.items() if value["failed"]),
                    "measures": measures,
                }
                break
        sides[side] = {
            "sample_id": sample_id,
            "subject_id": target[f"{side}_subject_id"],
            "path": sample.source_path if sample else None,
            "sha256": sample.sha256 if sample else None,
            "face_count": target.get(f"{side}_face_count"),
            "quality": quality,
        }
    quality_attribution = _quality_attribution(store, leaf_row, sides)
    return {
        "run_id": leaf_row["run_id"],
        "leaf_id": leaf_row["leaf_id"],
        "matcher_id": leaf_row["matcher_id"],
        "condition_id": leaf_row["condition_id"],
        "provenance_marker": "EXTERNALLY_COMPUTED" if leaf_row["condition_id"] == "EXTERNALLY_COMPUTED" else "QUALSIGHT_EXECUTED",
        "pair": {
            "pair_id": pair_id,
            "label": target["label"],
            "fold": target["fold"],
            "score": target["score"],
            "failure": target["failure"],
            "failure_side": target["failure_side"],
        },
        "sides": sides,
        "thresholds": thresholds,
        "decision_rule": "ACCEPT_WHEN_SCORE_GE_THRESHOLD",
        "decisions": [
            {
                "name": item["name"],
                "threshold": item["threshold"],
                "decision": (
                    "NO_SCORE" if target["score"] is None else ("ACCEPT" if target["score"] >= item["threshold"] else "REJECT")
                ),
            }
            for item in thresholds
        ],
        "distributions": {
            "bins": 48,
            "range": [lo, hi],
            "genuine": _histogram(genuine, 48, lo, hi),
            "impostor": _histogram(impostor, 48, lo, hi),
            "genuine_count": len(genuine),
            "impostor_count": len(impostor),
        },
        "quality_attribution": quality_attribution,
        "statement": HONESTY_STATEMENT,
    }


def _quality_attribution(store: Any, leaf_row: dict[str, Any], sides: dict[str, Any]) -> dict[str, Any]:
    """QS-Z91: per-side measures most below the job's median — recorded values, association wording only."""

    job_id = None
    for side in sides.values():
        if side["quality"]:
            job_id = side["quality"]["job_id"]
            break
    if job_id is None:
        return {"available": False, "reason": "no recorded quality batch covers this Run's dataset"}
    medians: dict[str, float] = {}
    if hasattr(store, "quality_measure_medians"):
        medians = store.quality_measure_medians(job_id)
    ranked: dict[str, list[dict[str, Any]]] = {}
    for name, side in sides.items():
        quality = side["quality"]
        if not quality:
            ranked[name] = []
            continue
        deltas = []
        for measure, value in quality["measures"].items():
            if value["failed"] or value["scalar"] is None:
                continue
            median = medians.get(measure)
            if median is None:
                continue
            deltas.append({"measure": measure, "scalar": value["scalar"], "population_median": median, "below_median_by": round(median - value["scalar"], 2)})
        deltas.sort(key=lambda item: -item["below_median_by"])
        ranked[name] = [item for item in deltas if item["below_median_by"] > 0][:5]
    return {
        "available": True,
        "job_id": job_id,
        "wording": "Measures furthest below the recorded batch median for each side — an association shown for inspection, not a causal explanation of the comparison outcome.",
        "sides": ranked,
    }


def ofiq_landmarks(registry: Any, image_path: str, expected_sha256: str) -> dict[str, Any]:
    """QS-Z90: OFIQ's own internal geometry (ADNet-98 landmarks + detected faces).

    Executes the registered AUXILIARY_BINARY (OFIQLandmarkApp, built from the
    registered v1.1.2 source against the registered ``libofiq_lib.so``) after
    verifying the binary, configuration, and image against their recorded
    hashes. The engine performs its full assessment internally; only its own
    preprocessing results are read back through its public API.
    """

    import subprocess

    runtime = registry.resource("ofiq-v1-1-2-linux-runtime")
    tool = runtime.artifact("AUXILIARY_BINARY").identity
    configuration = runtime.artifact("CONFIGURATION").identity
    for identity in (tool, configuration):
        if sha256_file(Path(identity.path)) != identity.sha256:
            raise EvidenceError(f"{identity.path} drifted from its registered hash")
    if sha256_file(Path(image_path)) != expected_sha256:
        raise EvidenceError(f"{image_path} drifted from its recorded hash")
    result = subprocess.run(
        [tool.path, "-c", configuration.path, "-i", image_path],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode != 0:
        raise EvidenceError(f"OFIQLandmarkApp failed ({result.returncode}): {result.stderr.strip()[:300]}")
    payload = json.loads(result.stdout)
    payload.update(
        {
            "source": "OFIQ_INTERNAL_ADNET98",
            "label": "OFIQ v1.1.2's own internal landmarks (ADNet-98) and detected faces — the geometry its 28 quality measures use; distinct from the matcher pipeline's SCRFD five points",
            "tool_sha256": tool.sha256,
            "image_sha256": expected_sha256,
        }
    )
    return payload
