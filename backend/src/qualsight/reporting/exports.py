"""Tabular data exports with source identifiers and classification enforcement (PRD §39; QS-F52).

Every export table carries the identifiers that trace each row to its immutable
Run, and a ``.provenance.json`` sidecar records the source, classification,
format, and file hash. Image bytes are never exported here; RESTRICTED sources
are refused for external formats.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ..engines.ofiq.adapter import content_id
from ..execution.prepared_output import (
    PreparedOutputError,
    create_staging_directory,
    discard_staging_directory,
    load_prepared_output,
    prepare_and_publish,
)
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


def _parquet_row_source(
    path: Path,
    additions: dict[str, Any],
) -> tuple[Callable[[], Iterator[dict[str, Any]]], int]:
    parquet = pq.ParquetFile(path)
    row_count = parquet.metadata.num_rows

    def rows() -> Iterator[dict[str, Any]]:
        for batch in parquet.iter_batches(batch_size=512):
            for row in batch.to_pylist():
                row.update(additions)
                yield row

    return rows, row_count


def _rows_quality_results(store: Any, job_id: str) -> tuple[Callable[[], Iterator[dict[str, Any]]], int, dict[str, Any]]:
    job = store.job(job_id)
    if job is None or not job.get("parquet_path"):
        raise ExportError("quality job is not recorded")
    rows, count = _parquet_row_source(
        Path(job["parquet_path"]),
        {"job_id": job_id, "run_id": job["run_id"]},
    )
    return rows, count, {"job_id": job_id, "run_id": job["run_id"], "parquet_sha256": job["parquet_sha256"], "engine_version": job["engine_version"], "read_strategy": "PARQUET_BATCH_STREAM_V1"}


def _rows_comparisons(store: Any, run_id: str) -> tuple[Callable[[], Iterator[dict[str, Any]]], int, dict[str, Any]]:
    row = store.matcher_leaf(run_id)
    if row is None:
        raise ExportError("matcher run is not recorded")
    rows, count = _parquet_row_source(
        Path(row["comparisons_parquet"]),
        {
            "run_id": run_id,
            "matcher_id": row["matcher_id"],
            "condition_id": row["condition_id"],
            "protocol_id": row["protocol_id"],
        },
    )
    return rows, count, {"run_id": run_id, "leaf_id": row["leaf_id"], "comparisons_sha256": row["comparisons_sha256"], "matcher_id": row["matcher_id"], "condition_id": row["condition_id"], "read_strategy": "PARQUET_BATCH_STREAM_V1"}


def _rows_embeddings(store: Any, run_id: str) -> tuple[Callable[[], Iterator[dict[str, Any]]], int, dict[str, Any]]:
    row = store.matcher_leaf(run_id)
    if row is None:
        raise ExportError("matcher run is not recorded")
    found = store.run(run_id)
    embeddings_artifact = next((a for a in found[0].artifacts if a.uri.endswith("embeddings.npy")), None)
    if embeddings_artifact is None:
        raise ExportError("run has no embeddings artifact")
    if sha256_file(Path(embeddings_artifact.uri)) != embeddings_artifact.sha256:
        raise ExportError("embeddings artifact bytes drifted from the recorded hash")
    vectors = np.load(embeddings_artifact.uri, mmap_mode="r")
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
    def rows() -> Iterator[dict[str, Any]]:
        for index, sample_id in enumerate(successful):
            yield {
                "run_id": run_id,
                "row_index": index,
                "sample_id": sample_id,
                "embedding": [float(value) for value in vectors[index]],
            }

    return rows, len(successful), {"run_id": run_id, "leaf_id": row["leaf_id"], "embeddings_sha256": embeddings_artifact.sha256, "dimension": int(vectors.shape[1]) if vectors.ndim == 2 else None, "row_order": "successful samples in first-appearance protocol order (matrix.py success_index)", "read_strategy": "NUMPY_READ_ONLY_MEMMAP_STREAM_V1"}


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


def _export_request_sha256(
    *, category: str, ref: str, fmt: str, source: dict[str, Any]
) -> str:
    encoded = json.dumps(
        {
            "version": EXPORT_VERSION,
            "category": category,
            "ref": ref,
            "format": fmt,
            "source": source,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def recover_prepared_export(
    directory: Path,
    *,
    export_id: str,
    project_id: str,
    category: str,
    ref: str,
    fmt: str,
    request_sha256: str,
) -> dict[str, Any]:
    """Validate and recover one fully promoted tabular export publication."""

    artifact_name = f"{category}.{fmt}"
    provenance_name = f"{artifact_name}.provenance.json"
    try:
        receipt = load_prepared_output(
            directory,
            operation_kind="TABULAR_EXPORT",
            result_id=export_id,
            project_id=project_id,
            request_sha256=request_sha256,
            artifact_names=(artifact_name, provenance_name),
        )
        record = receipt["record"]
        path = directory / artifact_name
        if (
            set(record)
            != {
                "export_id",
                "version",
                "category",
                "ref",
                "format",
                "project_id",
                "rows",
                "classification",
                "source",
                "path",
                "sha256",
                "size_bytes",
                "created_at",
                "note",
            }
            or record["export_id"] != export_id
            or record["version"] != EXPORT_VERSION
            or record["category"] != category
            or record["ref"] != ref
            or record["format"] != fmt
            or record["project_id"] != project_id
            or record["path"] != str(path)
            or record["sha256"] != sha256_file(path)
            or record["size_bytes"] != path.stat().st_size
        ):
            raise ExportError("prepared export record contract differs")
        provenance = json.loads(
            (directory / provenance_name).read_text(encoding="utf-8")
        )
        if provenance != record:
            raise ExportError("prepared export provenance differs")
        return record
    except (PreparedOutputError, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        if isinstance(error, ExportError):
            raise
        raise ExportError("prepared export failed integrity verification") from error


def export_table(store: Any, *, category: str, ref: str, fmt: str, root: Path) -> dict[str, Any]:
    if category not in CATEGORIES:
        raise ExportError(f"unknown export category {category}")
    if fmt not in FORMATS:
        raise ExportError(f"unknown export format {fmt}")
    row_factory: Callable[[], Iterator[dict[str, Any]]] | None = None
    row_count: int | None = None
    if category == "quality_results":
        row_factory, row_count, source = _rows_quality_results(store, ref)
        rows = []
        classification = "SENSITIVE_BIOMETRIC_DERIVED"
    elif category in ("matcher_scores", "comparison_records"):
        row_factory, row_count, source = _rows_comparisons(store, ref)
        rows = []
        classification = _classification_of_run(store, ref)
    elif category == "embeddings":
        row_factory, row_count, source = _rows_embeddings(store, ref)
        rows = []
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
    project_id = str(source.get("project_id") or source.get("run_id") or ref)
    if source.get("project_id") is None and source.get("run_id"):
        found = store.run(str(source["run_id"]))
        if found is None:
            raise ExportError("export source Run is not recorded")
        project_id = found[0].project_id
    request_sha256 = _export_request_sha256(
        category=category,
        ref=ref,
        fmt=fmt,
        source=source,
    )
    if directory.exists():
        return recover_prepared_export(
            directory,
            export_id=export_id,
            project_id=project_id,
            category=category,
            ref=ref,
            fmt=fmt,
            request_sha256=request_sha256,
        )
    stage = create_staging_directory(
        directory,
        operation_kind="TABULAR_EXPORT",
        result_id=export_id,
        project_id=project_id,
        request_sha256=request_sha256,
    )
    path = stage / f"{category}.{fmt}"
    def iter_rows() -> Iterator[dict[str, Any]]:
        return row_factory() if row_factory is not None else iter(rows)

    try:
        if fmt == "csv":
            columns: list[str] = []
            streamed_rows: Iterator[dict[str, Any]] | None = None
            if row_factory is not None:
                streamed_rows = iter_rows()
                first = next(streamed_rows, None)
                columns = list(first) if first is not None else []
            else:
                for item in rows:
                    for key in item:
                        if key not in columns:
                            columns.append(key)
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                if row_factory is not None and first is not None:
                    writer.writerow({key: (json.dumps(value) if isinstance(value, (list, dict)) else value) for key, value in first.items()})
                for item in streamed_rows if streamed_rows is not None else iter_rows():
                    writer.writerow({key: (json.dumps(value) if isinstance(value, (list, dict)) else value) for key, value in item.items()})
        elif fmt == "json":
            with path.open("w", encoding="utf-8") as handle:
                handle.write("{\n \"category\": ")
                handle.write(json.dumps(category, allow_nan=False))
                handle.write(",\n \"rows\": [")
                for index, item in enumerate(iter_rows()):
                    handle.write("," if index else "")
                    handle.write("\n  ")
                    handle.write(json.dumps(item, sort_keys=True, allow_nan=False, default=str))
                handle.write("\n ],\n \"source\": ")
                handle.write(json.dumps(source, sort_keys=True, allow_nan=False, default=str))
                handle.write("\n}\n")
        else:
            writer: pq.ParquetWriter | None = None
            batch: list[dict[str, Any]] = []
            try:
                for item in iter_rows():
                    batch.append(item)
                    if len(batch) == 512:
                        table = pa.Table.from_pylist(batch)
                        if writer is None:
                            writer = pq.ParquetWriter(path, table.schema, compression="zstd")
                        writer.write_table(table)
                        batch.clear()
                if batch:
                    table = pa.Table.from_pylist(batch)
                    if writer is None:
                        writer = pq.ParquetWriter(path, table.schema, compression="zstd")
                    writer.write_table(table)
            finally:
                if writer is not None:
                    writer.close()
            if writer is None:
                pq.write_table(pa.table({"run_id": pa.array([], pa.string())}), path)
        exported_rows = row_count if row_count is not None else len(rows)
        final_path = directory / path.name
        record = {"export_id": export_id, "version": EXPORT_VERSION, "category": category, "ref": ref, "format": fmt, "project_id": project_id, "rows": exported_rows, "classification": classification, "source": source,
                  "path": str(final_path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size, "created_at": _now(), "note": "rows carry run_id / analysis identifiers that trace to the immutable source Run; image bytes are never exported"}
        provenance_name = f"{category}.{fmt}.provenance.json"
        (stage / provenance_name).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        prepare_and_publish(
            stage,
            directory,
            operation_kind="TABULAR_EXPORT",
            result_id=export_id,
            project_id=project_id,
            request_sha256=request_sha256,
            artifact_names=(path.name, provenance_name),
            record=record,
        )
        return record
    except FileExistsError:
        return recover_prepared_export(
            directory,
            export_id=export_id,
            project_id=project_id,
            category=category,
            ref=ref,
            fmt=fmt,
            request_sha256=request_sha256,
        )
    except PreparedOutputError as error:
        raise ExportError(str(error)) from error
    finally:
        discard_staging_directory(stage)
