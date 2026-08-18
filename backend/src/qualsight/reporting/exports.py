"""Tabular data exports with source identifiers and classification enforcement (PRD §39; QS-F52).

Every export table carries the identifiers that trace each row to its immutable
Run, and a ``.provenance.json`` sidecar records the source, classification,
format, and file hash. Image bytes are never exported here; RESTRICTED sources
are refused for external formats.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ..engines.ofiq.adapter import content_id
from ..provenance import sha256_file

EXPORT_VERSION = "1.0.0"
CATEGORIES = ("quality_results", "matcher_scores", "comparison_records", "embeddings", "metric_tables", "statistical_results", "annotations")
FORMATS = ("csv", "json", "parquet")


class ExportError(RuntimeError):
    """Raised when an export cannot be produced from recorded evidence or is refused by classification."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _classification_of_run(store: Any, run_id: str) -> str:
    found = store.run(run_id)
    if found is None:
        raise ExportError("run is not recorded")
    run = found[0]
    classes = {artifact.classification.value for artifact in run.artifacts}
    return "RESTRICTED" if "RESTRICTED" in classes else (classes.pop() if len(classes) == 1 else "SENSITIVE_BIOMETRIC_DERIVED")


def _rows_quality_results(store: Any, job_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    job = store.job(job_id)
    if job is None or not job.get("parquet_path"):
        raise ExportError("quality job is not recorded")
    rows = pq.read_table(job["parquet_path"]).to_pylist()
    for row in rows:
        row["job_id"] = job_id
        row["run_id"] = job["run_id"]
    return rows, {"job_id": job_id, "run_id": job["run_id"], "parquet_sha256": job["parquet_sha256"], "engine_version": job["engine_version"]}


def _rows_comparisons(store: Any, run_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    row = store.matcher_leaf(run_id)
    if row is None:
        raise ExportError("matcher run is not recorded")
    rows = pq.read_table(row["comparisons_parquet"]).to_pylist()
    for item in rows:
        item["run_id"] = run_id
        item["matcher_id"] = row["matcher_id"]
        item["condition_id"] = row["condition_id"]
        item["protocol_id"] = row["protocol_id"]
    return rows, {"run_id": run_id, "leaf_id": row["leaf_id"], "comparisons_sha256": row["comparisons_sha256"], "matcher_id": row["matcher_id"], "condition_id": row["condition_id"]}


def _rows_embeddings(store: Any, run_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    row = store.matcher_leaf(run_id)
    if row is None:
        raise ExportError("matcher run is not recorded")
    found = store.run(run_id)
    embeddings_artifact = next((a for a in found[0].artifacts if a.uri.endswith("embeddings.npy")), None)
    if embeddings_artifact is None:
        raise ExportError("run has no embeddings artifact")
    if sha256_file(Path(embeddings_artifact.uri)) != embeddings_artifact.sha256:
        raise ExportError("embeddings artifact bytes drifted from the recorded hash")
    vectors = np.load(embeddings_artifact.uri)
    comparisons = pq.read_table(row["comparisons_parquet"], columns=["fold", "source_order", "left_sample_id", "right_sample_id", "failure", "failure_side"]).to_pylist()
    comparisons.sort(key=lambda r: (r["fold"], r["source_order"]))
    ordered: list[str] = []
    failed: set[str] = set()
    for item in comparisons:
        for side in ("left", "right"):
            sample_id = item[f"{side}_sample_id"]
            if sample_id not in ordered:
                ordered.append(sample_id)
            if item["failure"] is not None and item["failure_side"] and side in item["failure_side"].split("+"):
                failed.add(sample_id)
    successful = [sample_id for sample_id in ordered if sample_id not in failed]
    if len(successful) != vectors.shape[0]:
        raise ExportError(f"embedding rows ({vectors.shape[0]}) do not reconcile to the successful sample count ({len(successful)}) reconstructed from the comparison records")
    rows = [{"run_id": run_id, "row_index": index, "sample_id": sample_id, "embedding": [float(v) for v in vectors[index]]} for index, sample_id in enumerate(successful)]
    return rows, {"run_id": run_id, "leaf_id": row["leaf_id"], "embeddings_sha256": embeddings_artifact.sha256, "dimension": int(vectors.shape[1]) if vectors.ndim == 2 else None, "row_order": "successful samples in first-appearance protocol order (matrix.py success_index)"}


def _rows_metric_tables(store: Any, run_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    row = store.matcher_leaf(run_id)
    if row is None:
        raise ExportError("matcher run is not recorded")
    metrics = json.loads(row["metrics_json"])
    rows = []
    for policy, block in metrics["by_policy"].items():
        for point in block["operating_points"]:
            rows.append({"run_id": run_id, "leaf_id": row["leaf_id"], "failure_policy": policy, "metric": "fnmr_at_target_fmr", "definition": "biometric.verification.fnmr@1.0.0", **{k: (json.dumps(v) if isinstance(v, list) else v) for k, v in point.items()}})
        rows.append({"run_id": run_id, "leaf_id": row["leaf_id"], "failure_policy": policy, "metric": "eer", "definition": "biometric.verification.eer@1.0.0", "value": block["eer"]["eer"], "threshold": block["eer"]["threshold"], "method": block["eer"]["method"]})
    return rows, {"run_id": run_id, "leaf_id": row["leaf_id"], "metrics_sha256": row["metrics_sha256"]}


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), item, out)
    elif isinstance(value, list) and value and all(isinstance(v, (int, float, str, bool)) or v is None for v in value):
        out[prefix] = json.dumps(value)
    elif isinstance(value, list):
        out[prefix] = json.dumps(value)
    else:
        out[prefix] = value


def _rows_statistical_results(store: Any, analysis_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    row = store.utility_analysis(analysis_id)
    if row is None:
        raise ExportError("utility analysis is not recorded")
    document = json.loads((Path(row["directory"]) / "utility.json").read_text(encoding="utf-8"))
    rows = []
    for kind in ("FNMR", "FMR"):
        for section in ("areas", "association", "failure_prediction"):
            flat: dict[str, Any] = {}
            _flatten("", document["utility"][section][kind], flat)
            for key, value in flat.items():
                rows.append({"run_id": row["run_id"], "analysis_id": analysis_id, "matcher_run_id": document["sources"]["matcher_run_id"], "quality_job_id": document["sources"]["quality_job_id"], "curve": kind, "section": section, "statistic": key, "value_json": json.dumps(value)})
    return rows, {"run_id": row["run_id"], "analysis_id": analysis_id, "utility_sha256": row["utility_sha256"]}


def _rows_annotations(store: Any, project_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = store.annotations(project_id=project_id)
    for item in rows:
        item["context"] = json.dumps(item["context"], sort_keys=True)
    return rows, {"project_id": project_id}


def export_table(store: Any, *, category: str, ref: str, fmt: str, root: Path) -> dict[str, Any]:
    if category not in CATEGORIES:
        raise ExportError(f"unknown export category {category}")
    if fmt not in FORMATS:
        raise ExportError(f"unknown export format {fmt}")
    if category == "quality_results":
        rows, source = _rows_quality_results(store, ref)
        classification = "SENSITIVE_BIOMETRIC_DERIVED"
    elif category in ("matcher_scores", "comparison_records"):
        rows, source = _rows_comparisons(store, ref)
        classification = _classification_of_run(store, ref)
    elif category == "embeddings":
        rows, source = _rows_embeddings(store, ref)
        classification = "SENSITIVE_BIOMETRIC_DERIVED"
    elif category == "metric_tables":
        rows, source = _rows_metric_tables(store, ref)
        classification = "SENSITIVE_BIOMETRIC_DERIVED"
    elif category == "statistical_results":
        rows, source = _rows_statistical_results(store, ref)
        classification = "SENSITIVE_BIOMETRIC_DERIVED"
    else:
        rows, source = _rows_annotations(store, ref)
        classification = "PRIVATE"
    if classification == "RESTRICTED":
        raise ExportError("RESTRICTED source records are not exportable")
    export_id = content_id("export", category, ref, fmt, EXPORT_VERSION)
    directory = root / export_id.replace(":", "_")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{category}.{fmt}"
    if fmt == "csv":
        columns: list[str] = []
        for item in rows:
            for key in item:
                if key not in columns:
                    columns.append(key)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for item in rows:
                writer.writerow({key: (json.dumps(value) if isinstance(value, (list, dict)) else value) for key, value in item.items()})
    elif fmt == "json":
        path.write_text(json.dumps({"category": category, "source": source, "rows": rows}, indent=1, sort_keys=True, allow_nan=False, default=str) + "\n", encoding="utf-8")
    else:
        if rows:
            pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")
        else:
            pq.write_table(pa.table({"run_id": pa.array([], pa.string())}), path)
    record = {"export_id": export_id, "version": EXPORT_VERSION, "category": category, "ref": ref, "format": fmt, "rows": len(rows), "classification": classification, "source": source,
              "path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size, "created_at": _now(), "note": "rows carry run_id / analysis identifiers that trace to the immutable source Run; image bytes are never exported"}
    (directory / f"{category}.{fmt}.provenance.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record
