"""Provenance-bearing figure artifacts (PRD §§35–36, 40; QS-F50).

A figure is built only from recorded Runs and analysis artifacts. Its
specification (kind + source identities + parameters) is content-addressed;
rendering writes ``figure.png``, ``figure.svg``, ``figure.pdf``, the plotted
values as ``figure.csv``, and ``figure.provenance.json`` linking every series
to its source Run, metric definition, thresholds, and configuration. No chart
without data lineage.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from ..engines.ofiq.adapter import content_id  # noqa: E402
from ..provenance import sha256_file  # noqa: E402

FIGURE_VERSION = "1.0.0"
FIGURE_KINDS = ("det_overlay", "roc_overlay", "score_distributions", "erc_overlay", "risk_coverage", "quality_conditioned_fnmr", "quality_histogram", "error_overlap", "operating_points")
COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#17becf", "#e377c2"]


class FigureError(RuntimeError):
    """Raised when a figure cannot be built from recorded evidence."""


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


@dataclass
class Series:
    label: str
    x: list[float]
    y: list[float]
    source_run_id: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class FigureSpec:
    kind: str
    title: str
    x_label: str
    y_label: str
    series: list[Series]
    project_id: str
    parameters: dict[str, Any]
    metric_definitions: list[list[str]]
    thresholds: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    plot: str = "line"  # line | bar | hist | step
    x_scale: str = "linear"
    y_scale: str = "linear"
    notes: list[str] = field(default_factory=list)

    @property
    def figure_id(self) -> str:
        payload = json.dumps({"kind": self.kind, "parameters": self.parameters, "sources": self.sources, "version": FIGURE_VERSION, "project_id": self.project_id}, sort_keys=True)
        return content_id("figure", payload)


# -- builders -------------------------------------------------------------------


def _leaf_rows(store: Any, matrix_id: str) -> list[dict[str, Any]]:
    rows = store.matcher_leaves(matrix_id=matrix_id)
    if not rows:
        raise FigureError("matrix is not recorded")
    return sorted(rows, key=lambda row: row["leaf_id"])


def det_overlay(store: Any, project_id: str, matrix_id: str, *, policy: str = "EXCLUDE_FAILED_TRIALS", kind: str = "det_overlay") -> FigureSpec:
    rows = _leaf_rows(store, matrix_id)
    series, sources, thresholds = [], [], []
    for row in rows:
        metrics = json.loads(row["metrics_json"])
        block = metrics["by_policy"][policy]
        points = block["det" if kind == "det_overlay" else "roc"]["points"]
        xs = [p["fmr"] for p in points]
        ys = [p["fnmr"] if kind == "det_overlay" else p["tmr"] for p in points]
        label = f"{row['leaf_id']} · {row['matcher_id'].split('-local')[0]} · {'native' if 'INMEMORY' in row['condition_id'] else 'shared'}"
        series.append(Series(label, xs, ys, row["run_id"], {"matcher_id": row["matcher_id"], "condition_id": row["condition_id"], "eer": block["eer"]}))
        sources.append({"run_id": row["run_id"], "leaf_id": row["leaf_id"], "matcher_id": row["matcher_id"], "condition_id": row["condition_id"], "metrics_sha256": row["metrics_sha256"], "comparisons_sha256": row["comparisons_sha256"]})
        for point in block["operating_points"]:
            if point.get("threshold") is not None:
                thresholds.append({"run_id": row["run_id"], "kind": "MATCHER_THRESHOLD", "target_fmr": point["target_fmr"], "threshold": point["threshold"], "fmr": point["fmr"], "fnmr": point["fnmr"]})
    definition = ["biometric.verification.det", "1.0.0"] if kind == "det_overlay" else ["biometric.verification.roc", "1.0.0"]
    return FigureSpec(kind=kind, title=("DET" if kind == "det_overlay" else "ROC") + f" · matrix {matrix_id[-12:]} · {policy}", x_label="FMR (log)", y_label="FNMR (log)" if kind == "det_overlay" else "TMR",
                      series=series, project_id=project_id, parameters={"matrix_id": matrix_id, "failure_policy": policy}, metric_definitions=[definition, ["biometric.verification.fmr", "1.0.0"], ["biometric.verification.fnmr", "1.0.0"]],
                      thresholds=thresholds, sources=sources, plot="line", x_scale="log", y_scale="log" if kind == "det_overlay" else "linear",
                      notes=["Six Runs are drawn separately; preprocessing conditions are never pooled."])


def score_distributions(store: Any, project_id: str, run_id: str, *, bins: int = 60) -> FigureSpec:
    row = store.matcher_leaf(run_id)
    if row is None:
        raise FigureError("matcher run is not recorded")
    table = pq.read_table(row["comparisons_parquet"], columns=["label", "score"]).to_pylist()
    mated = np.array([r["score"] for r in table if r["label"] == "MATED" and r["score"] is not None], dtype=np.float64)
    non_mated = np.array([r["score"] for r in table if r["label"] == "NON_MATED" and r["score"] is not None], dtype=np.float64)
    low = float(min(mated.min(), non_mated.min())) if mated.size and non_mated.size else -1.0
    high = float(max(mated.max(), non_mated.max())) if mated.size and non_mated.size else 1.0
    edges = np.linspace(low, high, bins + 1)
    series = []
    for label, values in (("mated", mated), ("non-mated", non_mated)):
        counts, _ = np.histogram(values, bins=edges)
        series.append(Series(f"{label} (n={values.size})", [float(e) for e in edges[:-1]], [float(c) for c in counts], run_id, {"bin_width": float(edges[1] - edges[0]), "count": int(values.size), "mean": float(values.mean()) if values.size else None, "median": float(np.median(values)) if values.size else None}))
    metrics = json.loads(row["metrics_json"])
    thresholds = [{"run_id": run_id, "kind": "MATCHER_THRESHOLD", "target_fmr": p["target_fmr"], "threshold": p["threshold"]} for p in metrics["by_policy"]["EXCLUDE_FAILED_TRIALS"]["operating_points"] if p.get("threshold") is not None]
    return FigureSpec(kind="score_distributions", title=f"Score distributions · {row['leaf_id']}", x_label="cosine similarity", y_label="comparisons", series=series, project_id=project_id,
                      parameters={"run_id": run_id, "bins": bins}, metric_definitions=[], thresholds=thresholds,
                      sources=[{"run_id": run_id, "leaf_id": row["leaf_id"], "matcher_id": row["matcher_id"], "condition_id": row["condition_id"], "comparisons_sha256": row["comparisons_sha256"]}], plot="bar")


def _utility_document(store: Any, analysis_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    row = store.utility_analysis(analysis_id)
    if row is None:
        raise FigureError("utility analysis is not recorded")
    document = json.loads((Path(row["directory"]) / "utility.json").read_text(encoding="utf-8"))
    return row, document


def erc_overlay(store: Any, project_id: str, analysis_ids: list[str], *, curve: str = "FNMR") -> FigureSpec:
    series, sources, thresholds, definitions = [], [], [], []
    for analysis_id in analysis_ids:
        row, document = _utility_document(store, analysis_id)
        points = document["utility"]["erc"]["curves"][curve]["points"]
        xs = [p["rejected_fraction"] for p in points if p["error_rate"] is not None]
        ys = [p["error_rate"] for p in points if p["error_rate"] is not None]
        summary = json.loads(row["summary_json"])
        series.append(Series(f"{summary['leaf_id']} · {summary['quality_measure']} · {summary['pair_quality_policy'].replace('pair.quality.', '')}", xs, ys, row["run_id"], {"matcher_run_id": summary["matcher_run_id"], "au_erc": document["utility"]["areas"][curve]["au_erc"]["value"]}))
        sources.append({"run_id": row["run_id"], "analysis_id": analysis_id, "matcher_run_id": summary["matcher_run_id"], "quality_job_id": summary["quality_job_id"], "utility_sha256": row["utility_sha256"]})
        thresholds.append({"run_id": row["run_id"], "kind": "MATCHER_THRESHOLD", "threshold": document["matcher_threshold"]["value"], "source": document["matcher_threshold"].get("source")})
        definitions = [list(document["utility"]["erc"]["definition"]), list(document["utility"]["areas"][curve]["au_erc"]["definition"])]
    return FigureSpec(kind="erc_overlay", title=f"{curve}-ERC overlay", x_label="rejected fraction (worst pair quality first)", y_label=f"{curve} of retained units", series=series, project_id=project_id,
                      parameters={"analysis_ids": list(analysis_ids), "curve": curve}, metric_definitions=definitions, thresholds=thresholds, sources=sources, plot="step",
                      notes=["Matcher thresholds are fixed per analysis and never re-estimated along the curve."])


def risk_coverage(store: Any, project_id: str, analysis_id: str, *, curve: str = "FNMR") -> FigureSpec:
    row, document = _utility_document(store, analysis_id)
    points = document["utility"]["risk_coverage"]["curves"][curve]["points"]
    xs = [p["coverage"] for p in points if p["risk"] is not None]
    ys = [p["risk"] for p in points if p["risk"] is not None]
    summary = json.loads(row["summary_json"])
    return FigureSpec(kind="risk_coverage", title=f"Risk–coverage · {summary['leaf_id']} · {summary['quality_measure']}", x_label="retained coverage", y_label=f"{curve} of retained units",
                      series=[Series(summary["leaf_id"], xs, ys, row["run_id"], {"matcher_run_id": summary["matcher_run_id"]})], project_id=project_id, parameters={"analysis_id": analysis_id, "curve": curve},
                      metric_definitions=[list(document["utility"]["risk_coverage"]["definition"])], thresholds=[{"run_id": row["run_id"], "kind": "MATCHER_THRESHOLD", "threshold": document["matcher_threshold"]["value"]}],
                      sources=[{"run_id": row["run_id"], "analysis_id": analysis_id, "utility_sha256": row["utility_sha256"]}], plot="step")


def quality_conditioned_fnmr(store: Any, project_id: str, analysis_id: str) -> FigureSpec:
    row, document = _utility_document(store, analysis_id)
    bins = document["utility"]["quality_conditioned"]["FNMR"]["bins"]
    xs = [(b["quality_low"] + b["quality_high"]) / 2 for b in bins]
    ys = [b["rate"] if b["rate"] is not None else 0.0 for b in bins]
    summary = json.loads(row["summary_json"])
    extra = {"ci_lower": [b["ci_lower"] for b in bins], "ci_upper": [b["ci_upper"] for b in bins], "trials": [b["trials"] for b in bins], "supported": [b["supported"] for b in bins], "bin_edges": document["utility"]["quality_conditioned"]["bin_edges"]}
    return FigureSpec(kind="quality_conditioned_fnmr", title=f"FNMR | pair-quality bin · {summary['leaf_id']} · {summary['quality_measure']}", x_label="pair quality bin (midpoint)", y_label="FNMR",
                      series=[Series(summary["leaf_id"], xs, ys, row["run_id"], extra)], project_id=project_id, parameters={"analysis_id": analysis_id},
                      metric_definitions=[list(document["utility"]["quality_conditioned"]["FNMR"]["definition"])], thresholds=[{"run_id": row["run_id"], "kind": "MATCHER_THRESHOLD", "threshold": document["matcher_threshold"]["value"]}],
                      sources=[{"run_id": row["run_id"], "analysis_id": analysis_id, "utility_sha256": row["utility_sha256"]}], plot="bar_ci", notes=["Bins with fewer than 30 units are drawn hatched (unsupported precision)."])


def quality_histogram(store: Any, project_id: str, job_id: str, *, measure: str = "UnifiedQualityScore", bins: int = 20) -> FigureSpec:
    job = store.job(job_id)
    if job is None or not job.get("parquet_path"):
        raise FigureError("quality job is not recorded")
    table = pq.read_table(job["parquet_path"], columns=["outcome", f"{measure}.scalar", f"{measure}.failed"]).to_pylist()
    values = np.array([r[f"{measure}.scalar"] for r in table if r["outcome"] == "MEASURED" and not r[f"{measure}.failed"] and r[f"{measure}.scalar"] is not None], dtype=np.float64)
    edges = np.linspace(0, 100, bins + 1)
    counts, _ = np.histogram(values, bins=edges)
    return FigureSpec(kind="quality_histogram", title=f"{measure} · job {job_id[-12:]}", x_label=measure, y_label="samples",
                      series=[Series(f"{measure} (n={values.size})", [float(e) for e in edges[:-1]], [float(c) for c in counts], job["run_id"] or job_id, {"bin_width": float(edges[1] - edges[0]), "mean": float(values.mean()) if values.size else None})],
                      project_id=project_id, parameters={"job_id": job_id, "measure": measure, "bins": bins}, metric_definitions=[], thresholds=[],
                      sources=[{"run_id": job["run_id"], "job_id": job_id, "parquet_sha256": job["parquet_sha256"], "engine_version": job["engine_version"]}], plot="bar")


def error_overlap(store: Any, project_id: str, analysis_id: str) -> FigureSpec:
    row = store.cross_matcher_analysis(analysis_id)
    if row is None:
        raise FigureError("cross-matcher analysis is not recorded")
    document = json.loads((Path(row["directory"]) / "crossmatcher.json").read_text(encoding="utf-8"))
    contrasts = document["contrasts"]
    xs = list(range(len(contrasts)))
    series = []
    for key in ("A_ONLY", "B_ONLY", "BOTH"):
        series.append(Series(key, [float(x) for x in xs], [float(c["error_overlap"]["mated_false_non_match"][key]) for c in contrasts], row["run_id"], {"contrast_ids": [c["contrast_id"] for c in contrasts]}))
    return FigureSpec(kind="error_overlap", title="False non-match overlap per contrast (mated units scored by both)", x_label="contrast", y_label="pairs", series=series, project_id=project_id,
                      parameters={"analysis_id": analysis_id}, metric_definitions=[["biometric.verification.fnmr", "1.0.0"]], thresholds=[{"run_id": c["a"]["run_id"], "kind": "MATCHER_THRESHOLD", "threshold": c["a"]["threshold"]} for c in contrasts] + [{"run_id": c["b"]["run_id"], "kind": "MATCHER_THRESHOLD", "threshold": c["b"]["threshold"]} for c in contrasts],
                      sources=[{"run_id": row["run_id"], "analysis_id": analysis_id, "document_sha256": row["document_sha256"]}] + [{"run_id": c["a"]["run_id"], "contrast_id": c["contrast_id"], "role": "A"} for c in contrasts] + [{"run_id": c["b"]["run_id"], "contrast_id": c["contrast_id"], "role": "B"} for c in contrasts],
                      plot="grouped_bar", notes=[c["contrast_id"] for c in contrasts])


def operating_points(store: Any, project_id: str, matrix_id: str, *, policy: str = "EXCLUDE_FAILED_TRIALS") -> FigureSpec:
    rows = _leaf_rows(store, matrix_id)
    series, sources, thresholds = [], [], []
    for row in rows:
        block = json.loads(row["metrics_json"])["by_policy"][policy]
        points = [p for p in block["operating_points"] if p.get("threshold") is not None]
        series.append(Series(row["leaf_id"], [p["target_fmr"] for p in points], [p["fnmr"] for p in points], row["run_id"], {"fnmr_ci95": [p["fnmr_ci95"] for p in points], "thresholds": [p["threshold"] for p in points]}))
        sources.append({"run_id": row["run_id"], "leaf_id": row["leaf_id"], "metrics_sha256": row["metrics_sha256"]})
        thresholds.extend({"run_id": row["run_id"], "kind": "MATCHER_THRESHOLD", "target_fmr": p["target_fmr"], "threshold": p["threshold"]} for p in points)
    return FigureSpec(kind="operating_points", title=f"FNMR at target FMR · matrix {matrix_id[-12:]}", x_label="target FMR", y_label="FNMR", series=series, project_id=project_id, parameters={"matrix_id": matrix_id, "failure_policy": policy},
                      metric_definitions=[["biometric.verification.fnmr", "1.0.0"], ["biometric.verification.fmr", "1.0.0"]], thresholds=thresholds, sources=sources, plot="line_ci", x_scale="log")


BUILDERS = {
    "det_overlay": lambda store, project_id, params: det_overlay(store, project_id, params["matrix_id"], policy=params.get("failure_policy", "EXCLUDE_FAILED_TRIALS")),
    "roc_overlay": lambda store, project_id, params: det_overlay(store, project_id, params["matrix_id"], policy=params.get("failure_policy", "EXCLUDE_FAILED_TRIALS"), kind="roc_overlay"),
    "score_distributions": lambda store, project_id, params: score_distributions(store, project_id, params["run_id"], bins=int(params.get("bins", 60))),
    "erc_overlay": lambda store, project_id, params: erc_overlay(store, project_id, list(params["analysis_ids"]), curve=params.get("curve", "FNMR")),
    "risk_coverage": lambda store, project_id, params: risk_coverage(store, project_id, params["analysis_id"], curve=params.get("curve", "FNMR")),
    "quality_conditioned_fnmr": lambda store, project_id, params: quality_conditioned_fnmr(store, project_id, params["analysis_id"]),
    "quality_histogram": lambda store, project_id, params: quality_histogram(store, project_id, params["job_id"], measure=params.get("measure", "UnifiedQualityScore"), bins=int(params.get("bins", 20))),
    "error_overlap": lambda store, project_id, params: error_overlap(store, project_id, params["analysis_id"]),
    "operating_points": lambda store, project_id, params: operating_points(store, project_id, params["matrix_id"], policy=params.get("failure_policy", "EXCLUDE_FAILED_TRIALS")),
}


def build(store: Any, project_id: str, kind: str, params: dict[str, Any]) -> FigureSpec:
    builder = BUILDERS.get(kind)
    if builder is None:
        raise FigureError(f"unknown figure kind {kind}")
    try:
        return builder(store, project_id, params)
    except KeyError as error:
        raise FigureError(f"figure {kind} needs parameter {error}") from error


# -- rendering ------------------------------------------------------------------


def _draw(spec: FigureSpec, ax: Any) -> None:
    for index, series in enumerate(spec.series):
        color = COLORS[index % len(COLORS)]
        if spec.plot == "line":
            ax.plot(series.x, series.y, label=series.label, color=color, linewidth=1.4)
        elif spec.plot == "step":
            ax.step(series.x, series.y, where="post", label=series.label, color=color, linewidth=1.4)
        elif spec.plot == "line_ci":
            ax.plot(series.x, series.y, marker="o", label=series.label, color=color, linewidth=1.2)
            ci = series.extra.get("fnmr_ci95")
            if ci:
                ax.fill_between(series.x, [c[0] for c in ci], [c[1] for c in ci], color=color, alpha=0.12)
        elif spec.plot == "bar":
            width = series.extra.get("bin_width", 1.0)
            ax.bar(series.x, series.y, width=width, align="edge", alpha=0.55, label=series.label, color=color)
        elif spec.plot == "bar_ci":
            edges = series.extra["bin_edges"]
            widths = [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]
            lower = [(y - (lo if lo is not None else y)) for y, lo in zip(series.y, series.extra["ci_lower"])]
            upper = [((hi if hi is not None else y) - y) for y, hi in zip(series.y, series.extra["ci_upper"])]
            bars = ax.bar(series.x, series.y, width=[w * 0.9 for w in widths], yerr=[lower, upper], capsize=3, color=color, alpha=0.7, label=series.label)
            for bar, supported in zip(bars, series.extra["supported"]):
                if not supported:
                    bar.set_hatch("//")
                    bar.set_alpha(0.35)
        elif spec.plot == "grouped_bar":
            width = 0.8 / max(1, len(spec.series))
            ax.bar([x + index * width for x in series.x], series.y, width=width, label=series.label, color=color)
    if spec.plot == "grouped_bar" and spec.notes:
        ax.set_xticks([x + 0.4 - 0.4 / max(1, len(spec.series)) for x in spec.series[0].x])
        ax.set_xticklabels(spec.notes, rotation=35, ha="right", fontsize=7)
    ax.set_xscale(spec.x_scale)
    ax.set_yscale(spec.y_scale)
    ax.set_xlabel(spec.x_label)
    ax.set_ylabel(spec.y_label)
    ax.set_title(spec.title, fontsize=10)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7, loc="best")


def render(spec: FigureSpec, root: Path) -> dict[str, Any]:
    """Render PNG/SVG/PDF + CSV + provenance sidecar into root/<figure_id>/; returns the figure record."""

    figure_id = spec.figure_id
    directory = root / figure_id.replace(":", "_")
    if directory.exists():
        provenance_path = directory / "figure.provenance.json"
        if provenance_path.exists():
            return json.loads(provenance_path.read_text(encoding="utf-8"))
    directory.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(6.4, 4.2), dpi=150)
    _draw(spec, ax)
    fig.tight_layout()
    files = {}
    for extension in ("png", "svg", "pdf"):
        path = directory / f"figure.{extension}"
        fig.savefig(path, format=extension, metadata={"Title": spec.title, "Creator": "QualSight"} if extension != "png" else {"Title": spec.title, "Software": "QualSight"})
        files[extension] = path
    plt.close(fig)
    csv_path = directory / "figure.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["series", "source_run_id", "x", "y"])
        for series in spec.series:
            for x, y in zip(series.x, series.y):
                writer.writerow([series.label, series.source_run_id, repr(x), repr(y)])
    files["csv"] = csv_path
    record = {
        "figure_id": figure_id, "version": FIGURE_VERSION, "kind": spec.kind, "title": spec.title, "project_id": spec.project_id, "created_at": _now().isoformat(),
        "parameters": spec.parameters, "sources": spec.sources, "source_run_ids": sorted({s["run_id"] for s in spec.sources if s.get("run_id")}),
        "metric_definitions": spec.metric_definitions, "thresholds": spec.thresholds, "notes": spec.notes,
        "axes": {"x": spec.x_label, "y": spec.y_label, "x_scale": spec.x_scale, "y_scale": spec.y_scale, "plot": spec.plot},
        "series": [{"label": s.label, "source_run_id": s.source_run_id, "points": len(s.x), "extra": s.extra} for s in spec.series],
        "generator": {"matplotlib": matplotlib.__version__, "backend": "Agg"},
        "files": {name: {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size} for name, path in files.items()},
        "directory": str(directory),
    }
    provenance_path = directory / "figure.provenance.json"
    provenance_path.write_text(json.dumps(record, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    record["provenance_sha256"] = hashlib.sha256(provenance_path.read_bytes()).hexdigest()
    return record
