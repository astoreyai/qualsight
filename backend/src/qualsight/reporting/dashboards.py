"""Saved dashboards (PRD §37; QS-F51): project artifacts whose layout reloads exactly.

A dashboard is a named, versioned layout of panels. Each panel references a
recorded figure or a recorded table source by identity; the saved layout is
content-addressed and stored append-only, and reloading resolves every panel
to its current data together with the underlying Run references.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from ..engines.ofiq.adapter import content_id

DASHBOARD_VERSION = "1.0.0"
TEMPLATES = ("Dataset Overview", "Matcher Evaluation", "FIQA Evaluation", "Cross-Matcher Evaluation", "Stakeholder Demo", "Publication Workspace", "Custom")
PANEL_KINDS = ("figure", "operating_points", "utility_summary", "cross_contrasts", "dataset_statistics", "policy_table")


class DashboardError(RuntimeError):
    """Raised when a dashboard cannot be saved or reloaded from recorded evidence."""


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _resolve_panel(store: Any, project_id: str, panel: dict[str, Any]) -> dict[str, Any]:
    kind = panel.get("kind")
    ref = panel.get("ref")
    if kind == "figure":
        figure = store.figure(ref)
        if figure is None or figure["project_id"] != project_id:
            raise DashboardError(f"figure {ref} is not recorded")
        provenance = json.loads(figure["provenance_json"])
        return {"kind": kind, "ref": ref, "title": figure["title"], "run_ids": provenance["source_run_ids"], "data": {"figure": {"kind": figure["kind"], "files": {name: f"/api/projects/{project_id}/figures/{ref}/file?format={name}" for name in ("png", "svg", "pdf", "csv", "provenance")}, "thresholds": provenance["thresholds"], "metric_definitions": provenance["metric_definitions"]}}}
    if kind == "operating_points":
        row = store.matcher_leaf(ref)
        if row is None or row["project_id"] != project_id:
            raise DashboardError(f"matcher run {ref} is not recorded")
        metrics = json.loads(row["metrics_json"])
        return {"kind": kind, "ref": ref, "title": f"Operating points · {row['leaf_id']}", "run_ids": [ref], "data": {"leaf_id": row["leaf_id"], "matcher_id": row["matcher_id"], "condition_id": row["condition_id"], "by_policy": {policy: {"operating_points": block["operating_points"], "eer": block["eer"]} for policy, block in metrics["by_policy"].items()}}}
    if kind == "utility_summary" or kind == "policy_table":
        row = store.utility_analysis(ref)
        if row is None or row["project_id"] != project_id:
            raise DashboardError(f"utility analysis {ref} is not recorded")
        summary = json.loads(row["summary_json"])
        data: dict[str, Any] = {"summary": summary}
        if kind == "policy_table":
            data["policy"] = json.loads((Path(row["directory"]) / "policy.json").read_text(encoding="utf-8"))
        return {"kind": kind, "ref": ref, "title": f"{'Policy' if kind == 'policy_table' else 'Utility'} · {summary['leaf_id']} · {summary['quality_measure']}", "run_ids": [row["run_id"], summary["matcher_run_id"]], "data": data}
    if kind == "cross_contrasts":
        row = store.cross_matcher_analysis(ref)
        if row is None or row["project_id"] != project_id:
            raise DashboardError(f"cross-matcher analysis {ref} is not recorded")
        summary = json.loads(row["summary_json"])
        return {"kind": kind, "ref": ref, "title": f"Cross-matcher contrasts · {summary['matrix_id'][-12:]}", "run_ids": [row["run_id"]], "data": {"contrasts": summary["contrasts"]}}
    if kind == "dataset_statistics":
        dataset = store.dataset(ref)
        found = store.dataset_statistics(ref)
        if dataset is None or dataset.project_id != project_id or found is None:
            raise DashboardError(f"dataset {ref} has no recorded statistics")
        statistics, evidence = found
        return {"kind": kind, "ref": ref, "title": f"Dataset statistics · {ref[-12:]}", "run_ids": [], "data": {"statistics": statistics, "evidence_sha256": evidence}}
    raise DashboardError(f"unknown panel kind {kind}")


def save_dashboard(store: Any, *, project_id: str, name: str, template: str, panels: list[dict[str, Any]], columns: int, actor: str, supersedes: str | None = None) -> dict[str, Any]:
    if template not in TEMPLATES:
        raise DashboardError(f"unknown template {template}")
    if not panels:
        raise DashboardError("a dashboard needs at least one panel")
    if columns < 1 or columns > 4:
        raise DashboardError("columns must be 1–4")
    resolved = [_resolve_panel(store, project_id, panel) for panel in panels]  # validates every reference before saving
    layout = {"version": DASHBOARD_VERSION, "name": name, "template": template, "columns": columns, "panels": [{"kind": p["kind"], "ref": p["ref"], "title": p.get("title")} for p in panels]}
    layout_text = json.dumps(layout, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    version = 1
    if supersedes is not None:
        prior = store.dashboard(supersedes)
        if prior is None or prior["project_id"] != project_id:
            raise DashboardError("superseded dashboard is not recorded")
        version = int(prior["version"]) + 1
    dashboard_id = content_id("dashboard", project_id, layout_text, str(version), supersedes or "")
    record = {"dashboard_id": dashboard_id, "project_id": project_id, "name": name, "template": template, "version": version, "supersedes": supersedes, "layout": layout, "layout_text": layout_text, "layout_sha256": hashlib.sha256(layout_text.encode("utf-8")).hexdigest(),
              "panel_run_ids": sorted({run_id for panel in resolved for run_id in panel["run_ids"]}), "created_by": actor, "created_at": _now().isoformat()}
    store.record_dashboard(record)
    return record


def load_dashboard(store: Any, project_id: str, dashboard_id: str) -> dict[str, Any]:
    row = store.dashboard(dashboard_id)
    if row is None or row["project_id"] != project_id:
        raise DashboardError("dashboard is not recorded")
    layout = json.loads(row["layout_json"])
    return {"dashboard_id": row["dashboard_id"], "project_id": row["project_id"], "name": row["name"], "template": row["template"], "version": row["version"], "supersedes": row["supersedes"], "created_by": row["created_by"], "created_at": row["created_at"],
            "layout": layout, "layout_sha256": row["evidence_sha256"], "panels": [_resolve_panel(store, project_id, panel) for panel in layout["panels"]]}
