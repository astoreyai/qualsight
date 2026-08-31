"""Report builder: HTML (print-ready) and PDF from recorded Runs, figures, and tables (PRD §38; QS-F52).

A report is assembled only from recorded evidence: dataset and protocol records,
the quality job, matcher leaves, utility and cross-matcher analyses, recorded
figures (embedded as their SVG bytes), failure examples, statistical methods,
limitations, and a provenance section with software versions, model hashes,
and dataset hashes. Data classification is enforced: images are embedded only
under an explicit policy and never for RESTRICTED sources.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import textwrap
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
import matplotlib.image as mpimg  # noqa: E402

from ..engines.ofiq.adapter import content_id  # noqa: E402
from ..metrics.verification import operating_point_with_support  # noqa: E402
from ..provenance import application_version, sha256_file  # noqa: E402

REPORT_VERSION = "1.0.0"
METRIC_REGISTRY_ID = "qualsight-core-metrics"
METRIC_REGISTRY_VERSION = "1.2.0"
IMAGE_POLICIES = ("EXCLUDE", "INCLUDE_PERMITTED")


class ReportError(RuntimeError):
    """Raised when a report cannot be assembled from recorded evidence."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _pct(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f} %"


def _num(value: float | None, digits: int = 4) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def aggregate_scientific_support(states: set[str]) -> str:
    """Return the most restrictive recorded support state without auto-approval."""

    precedence = ("BLOCKED", "NOT_SUPPORTED", "NOT_ASSESSED", "SUPPORTED")
    unknown = states - set(precedence)
    if unknown:
        raise ReportError(f"unknown scientific support states: {sorted(unknown)}")
    return next((state for state in precedence if state in states), "NOT_ASSESSED")


def quality_input_stage_summary(sources: dict[str, Any]) -> tuple[str, str]:
    """Summarize recorded stage evidence, conservatively labeling real legacy records."""

    quality_stage = sources.get("quality_input_stage", {})
    quality_stage_id = quality_stage.get(
        "stage_id", "NOT_RECORDED_LEGACY_ARTIFACT"
    )
    relationships = quality_stage.get("matcher_preprocessing_relationships")
    relationship = (
        "; ".join(
            f"{item['condition_id']}: {item['relationship']}"
            for item in relationships
        )
        if relationships
        else "NOT_RECORDED_LEGACY_ARTIFACT"
    )
    return str(quality_stage_id), relationship


class ReportBuilder:
    def __init__(self, *, store: Any, registry: Any, repository_root: Path, project_id: str, spec: dict[str, Any], actor: str, root: Path) -> None:
        self.store = store
        self.registry = registry
        self.repository_root = repository_root
        self.project = store.project(project_id)
        if self.project is None:
            raise ReportError("project is not recorded")
        self.spec = spec
        self.actor = actor
        self.root = root
        self.image_policy = spec.get("image_policy", "EXCLUDE")
        if self.image_policy not in IMAGE_POLICIES:
            raise ReportError(f"unknown image policy {self.image_policy}")
        self.sections: list[dict[str, Any]] = []
        self.run_ids: set[str] = set()
        self.model_hashes: dict[str, str] = {}
        self.dataset_hashes: dict[str, str] = {}
        self.claim_support_records: dict[tuple[str, str, str], dict[str, str]] = {}
        self.figures: list[dict[str, Any]] = []
        self.images_embedded = 0
        self.images_withheld = 0

    # -- assembly -----------------------------------------------------------------

    def _add(self, title: str, kind: str, content: Any) -> None:
        self.sections.append({"title": title, "kind": kind, "content": content})

    def _record_claim_support(self, support: object, *, basis: str) -> None:
        if not isinstance(support, dict):
            return
        state = str(support.get("state", "NOT_ASSESSED"))
        aggregate_scientific_support({state})
        claim_scope = str(support.get("claim_scope", "CLAIM_SCOPE_NOT_RECORDED"))
        key = (state, claim_scope, basis)
        self.claim_support_records[key] = {
            "state": state,
            "claim_scope": claim_scope,
            "basis": basis,
        }

    def _dataset(self, dataset_id: str) -> None:
        dataset = self.store.dataset(dataset_id)
        if dataset is None:
            raise ReportError(f"dataset {dataset_id} is not recorded")
        self.dataset_hashes[dataset_id] = dataset.manifest.sha256
        statistics = self.store.dataset_statistics(dataset_id)
        rows = [["Dataset", dataset.name], ["Dataset ID", dataset_id], ["Manifest SHA-256", dataset.manifest.sha256], ["Classification", dataset.classification.value if hasattr(dataset, "classification") else "PRIVATE"], ["Rights status", dataset.rights_status], ["Samples recorded", str(self.store.sample_count(dataset_id))]]
        if statistics is not None:
            for key, value in sorted(statistics[0].items()):
                if isinstance(value, (int, float, str)):
                    rows.append([key, str(value)])
        self._add("Dataset", "table", {"columns": ["Field", "Value"], "rows": rows})

    def _protocol(self, protocol_id: str) -> None:
        protocol = self.store.protocol(protocol_id)
        if protocol is None:
            raise ReportError(f"protocol {protocol_id} is not recorded")
        rows = [["Protocol", protocol.name], ["Protocol ID", protocol_id], ["Category", protocol.category.value], ["Pair policy", protocol.pair_policy.value], ["Folds", str(protocol.fold_count) if protocol.fold_count is not None else "Not recorded"], ["Subjects", str(protocol.unique_subject_count) if protocol.unique_subject_count is not None else "Not recorded"], ["Pairs", str(protocol.pair_count)], ["Mated", str(protocol.mated_count)], ["Non-mated", str(protocol.nonmated_count)], ["Pairs SHA-256", protocol.pairs_sha256]]
        for limitation in protocol.limitations:
            rows.append(["Limitation", limitation])
        self._add("Protocol", "table", {"columns": ["Field", "Value"], "rows": rows})

    def _engine(self, job_id: str) -> None:
        job = self.store.job(job_id)
        if job is None:
            raise ReportError(f"quality job {job_id} is not recorded")
        if job["run_id"]:
            self.run_ids.add(job["run_id"])
            found = self.store.run(job["run_id"])
            if found:
                run = found[0]
                if run.quality_engine:
                    for artifact in run.quality_engine.artifacts:
                        self.model_hashes[artifact.path] = artifact.sha256
        rows = [["Job", job_id], ["Run", job["run_id"] or "—"], ["Engine version", job["engine_version"] or "—"], ["State", job["state"]], ["Total samples", str(job["total"])], ["Measured", str(job["measured"])], ["Engine row failures", str(job["engine_row_failures"])], ["Integrity failures", str(job["integrity_failures"])], ["Not assessed", str(job["not_assessed"])], ["Results SHA-256", job["parquet_sha256"] or "—"]]
        self._add("Quality engine", "table", {"columns": ["Field", "Value"], "rows": rows})

    def _matchers(self, matrix_id: str) -> None:
        leaves = self.store.matcher_leaves(matrix_id=matrix_id)
        if not leaves:
            raise ReportError(f"matrix {matrix_id} is not recorded")
        rows = []
        points_rows = []
        for leaf in sorted(leaves, key=lambda item: item["leaf_id"]):
            self.run_ids.add(leaf["run_id"])
            found = self.store.run(leaf["run_id"])
            if found:
                for identity in found[0].model_hashes:
                    self.model_hashes[identity.path] = identity.sha256
            metrics = json.loads(leaf["metrics_json"])
            block = metrics["by_policy"]["EXCLUDE_FAILED_TRIALS"]
            eer_scope = block["eer"].get("evaluation_scope", "DESCRIPTIVE_IN_SAMPLE")
            threshold_selection = block.get(
                "threshold_selection",
                "SELECTED_AND_EVALUATED_ON_THE_SAME_RECORDED_TRIALS",
            )
            support = block.get(
                "scientific_support",
                {
                    "state": "NOT_SUPPORTED",
                    "claim_scope": "PROSPECTIVE_OR_OPERATIONAL_THRESHOLD_PERFORMANCE",
                },
            )
            self._record_claim_support(
                support,
                basis=f"matcher-run:{leaf['run_id']}:aggregate-verification-metrics",
            )
            rows.append([leaf["leaf_id"], leaf["matcher_id"], leaf["condition_id"], str(leaf["comparison_count"]), str(leaf["scored_count"]), str(leaf["failed_count"]), _pct(block["eer"]["eer"]), _num(block["eer"]["threshold"], 6), eer_scope, threshold_selection, support["state"], support["claim_scope"], leaf["run_id"]])
            for point in block["operating_points"]:
                if point.get("threshold") is not None:
                    point = operating_point_with_support(float(point["target_fmr"]), point)
                    event_support = point["event_support"]
                    self._record_claim_support(
                        point["scientific_support"],
                        basis=(
                            f"matcher-run:{leaf['run_id']}:target-fmr:"
                            f"{point['target_fmr']}"
                        ),
                    )
                    points_rows.append([
                        leaf["leaf_id"],
                        str(point["target_fmr"]),
                        _num(point["threshold"], 6),
                        _pct(point["fmr"], 3),
                        _pct(point["fnmr"]),
                        f"[{_pct(point['fnmr_ci95'][0])}, {_pct(point['fnmr_ci95'][1])}]",
                        str(point["mated_trials_included"]),
                        str(point["nonmated_trials_included"]),
                        point["evaluation_scope"],
                        point["scientific_support"]["state"],
                        point["scientific_support"]["claim_scope"],
                        f"{'SUPPORTED' if event_support['supported'] else 'NOT_SUPPORTED'}; observed {event_support['observed_false_matches']}; minimum observed {event_support['minimum_observed_false_matches']}",
                    ])
        self._add("Matcher Runs (each preprocessing condition reported separately)", "table", {"columns": ["Leaf", "Matcher", "Preprocessing condition", "Comparisons", "Scored", "Failed", "EER", "EER threshold", "EER evaluation scope", "Threshold selection", "Prospective / operational support", "Claim scope", "Run"], "rows": rows})
        self._add("Operating points (descriptive in-sample MATCHER THRESHOLD at target FMR; EXCLUDE_FAILED_TRIALS; Clopper–Pearson 95 %)", "table", {"columns": ["Leaf", "Target FMR", "Threshold", "FMR", "FNMR", "FNMR 95 % CI", "Mated trials", "Non-mated trials", "Evaluation scope", "Prospective / operational support", "Claim scope", "False-match event support"], "rows": points_rows})

    def _utility(self, analysis_ids: list[str]) -> None:
        rows = []
        stats_rows = []
        for analysis_id in analysis_ids:
            row = self.store.utility_analysis(analysis_id)
            if row is None:
                raise ReportError(f"utility analysis {analysis_id} is not recorded")
            self.run_ids.add(row["run_id"])
            summary = json.loads(row["summary_json"])
            document = json.loads((Path(row["directory"]) / "utility.json").read_text(encoding="utf-8"))
            matcher_threshold = (
                document.get("policy", {})
                .get("thresholds", {})
                .get("matcher", {})
                .get("provenance", {})
            )
            self._record_claim_support(
                matcher_threshold.get("scientific_support"),
                basis=f"utility-analysis:{analysis_id}:matcher-threshold",
            )
            areas = document["utility"]["areas"]["FNMR"]
            quality_stage_id, quality_stage_relationship = (
                quality_input_stage_summary(document["sources"])
            )
            rows.append([summary["leaf_id"], summary["quality_measure"], quality_stage_id, quality_stage_relationship, summary["pair_quality_policy"], _num(summary["matcher_threshold"], 6), str(summary["quality_threshold"]), _num(areas["au_erc"]["value"], 5), f"[{_num(areas['au_erc']['uncertainty'].get('ci_lower'), 5)}, {_num(areas['au_erc']['uncertainty'].get('ci_upper'), 5)}]", _num(areas["partial_au_erc"]["value"], 5), _pct(summary["fnmr_before"]), _pct(summary["fnmr_after"]), _pct(summary["retention_rate"], 1), str(summary["errors_prevented"]), str(summary["successful_comparisons_removed"]), row["run_id"]])
            association = document["utility"]["association"]["FNMR"]
            prediction = document["utility"]["failure_prediction"]["FNMR"]
            logistic = association.get("logistic_error_on_quality", {})
            stats_rows.append([summary["leaf_id"], _num(association.get("spearman_quality_vs_score", {}).get("rho"), 3), _num(association.get("kendall_quality_vs_score", {}).get("tau"), 3), (f"{_num(logistic.get('odds_ratio_per_10_units'), 3)} [{_num(logistic.get('odds_ratio_per_10_units_ci', [None, None])[0], 3)}, {_num(logistic.get('odds_ratio_per_10_units_ci', [None, None])[1], 3)}]" if logistic.get("status") == "ESTIMATED" else logistic.get("status", "—")), _num(prediction.get("roc_auc"), 3), _num(prediction.get("pr_auc"), 3), f"{document['utility']['uncertainty']['method']} × {document['utility']['uncertainty']['replicates']} (seed {document['utility']['uncertainty']['seed']})"])
        self._add(f"Quality → utility (metric registry v{METRIC_REGISTRY_VERSION}; MATCHER THRESHOLD fixed; QUALITY POLICY THRESHOLD separate)", "table", {"columns": ["Leaf", "Measure", "Quality input stage", "Relationship to matcher preprocessing", "Pair policy", "Matcher T", "Quality Q", "AU-ERC (FNMR)", "AU-ERC 95 % CI", "pAU-ERC 0–20 %", "FNMR before", "FNMR after", "Retained", "Errors prevented", "Good removed", "Analysis Run"], "rows": rows})
        self._add("Statistical methods and confidence intervals (utility)", "table", {"columns": ["Leaf", "Spearman ρ", "Kendall τ-b", "OR per 10 units [95 % Wald]", "ROC-AUC", "PR-AUC", "Uncertainty method"], "rows": stats_rows})

    def _cross(self, analysis_id: str) -> None:
        row = self.store.cross_matcher_analysis(analysis_id)
        if row is None:
            raise ReportError(f"cross-matcher analysis {analysis_id} is not recorded")
        self.run_ids.add(row["run_id"])
        summary = json.loads(row["summary_json"])
        document = json.loads(
            (Path(row["directory"]) / "crossmatcher.json").read_text(encoding="utf-8")
        )
        for contrast in document.get("contrasts", []):
            for side_name in ("a", "b"):
                provenance = contrast.get(side_name, {}).get(
                    "threshold_provenance", {}
                )
                self._record_claim_support(
                    provenance.get("scientific_support"),
                    basis=(
                        f"cross-matcher:{analysis_id}:"
                        f"{contrast.get('contrast_id', 'unidentified-contrast')}:"
                        f"side-{side_name}"
                    ),
                )
        rows = []
        for contrast in summary["contrasts"]:
            ci = contrast.get("au_erc_diff_fnmr_ci") or [None, None]
            overlap = contrast["fnm_overlap"]
            rows.append([contrast["contrast_id"], contrast["kind"], json.dumps(contrast["held_fixed"]), f"{overlap['A_ONLY']} / {overlap['B_ONLY']} / {overlap['BOTH']} / {overlap['NEITHER']}", _num(contrast["fnm_jaccard"], 3), _num(contrast["au_erc_diff_fnmr"], 5), f"[{_num(ci[0], 5)}, {_num(ci[1], 5)}]", contrast["interaction_status"] or "—"])
        self._add("Cross-matcher contrasts (matcher contrasts hold the crop fixed; preprocessing contrasts hold the matcher fixed)", "table", {"columns": ["Contrast", "Kind", "Held fixed", "FNM A only / B only / both / neither", "Jaccard", "AU-ERC A − B", "95 % paired CI", "Interaction"], "rows": rows})

    def _figures(self, figure_ids: list[str]) -> None:
        for figure_id in figure_ids:
            figure = self.store.figure(figure_id)
            if figure is None:
                raise ReportError(f"figure {figure_id} is not recorded")
            provenance = json.loads(figure["provenance_json"])
            for run_id in provenance["source_run_ids"]:
                self.run_ids.add(run_id)
            svg = Path(provenance["files"]["svg"]["path"]).read_text(encoding="utf-8")
            png = Path(provenance["files"]["png"]["path"])
            self.figures.append({"figure_id": figure_id, "title": figure["title"], "svg": svg, "png_path": str(png), "provenance": provenance})
            self._add(f"Figure · {figure['title']}", "figure", {"figure_id": figure_id, "source_run_ids": provenance["source_run_ids"], "metric_definitions": provenance["metric_definitions"], "thresholds": provenance["thresholds"], "sha256": {name: item["sha256"] for name, item in provenance["files"].items()}})

    def _failure_examples(self, block: dict[str, Any]) -> None:
        analysis_id = block["analysis_id"]
        limit = int(block.get("limit", 6))
        rows_out = []
        if "contrast_id" in block:
            row = self.store.cross_matcher_analysis(analysis_id)
            if row is None:
                raise ReportError("cross-matcher analysis is not recorded")
            import duckdb

            parquet = str(Path(row["directory"]) / "contrast_pairs.parquet").replace("'", "''")
            connection = duckdb.connect()
            try:
                cursor = connection.execute(f"SELECT pair_id, label, left_sample_id, right_sample_id, score_a, score_b, failure_a, failure_b, category, pair_quality FROM read_parquet('{parquet}') WHERE contrast_id = ? AND category = ? ORDER BY pair_quality NULLS LAST, pair_id LIMIT ?", [block["contrast_id"], block.get("category", "BOTH"), limit])
                names = [item[0] for item in cursor.description]
                pairs = [dict(zip(names, values)) for values in cursor.fetchall()]
            finally:
                connection.close()
            self.run_ids.add(row["run_id"])
            source = {"analysis_id": analysis_id, "contrast_id": block["contrast_id"], "category": block.get("category", "BOTH")}
        else:
            row = self.store.utility_analysis(analysis_id)
            if row is None:
                raise ReportError("utility analysis is not recorded")
            import duckdb

            parquet = str(Path(row["directory"]) / "pair_quality.parquet").replace("'", "''")
            connection = duckdb.connect()
            try:
                cursor = connection.execute(f"SELECT pair_id, label, left_sample_id, right_sample_id, score, failure, decision_error, pair_quality, retained_by_quality_policy FROM read_parquet('{parquet}') WHERE decision_error = true ORDER BY pair_quality NULLS LAST, pair_id LIMIT ?", [limit])
                names = [item[0] for item in cursor.description]
                pairs = [dict(zip(names, values)) for values in cursor.fetchall()]
            finally:
                connection.close()
            self.run_ids.add(row["run_id"])
            source = {"analysis_id": analysis_id, "selection": "decision_error = true"}
        for pair in pairs:
            images = []
            for side in ("left", "right"):
                sample = self.store.sample(pair[f"{side}_sample_id"])
                if sample is None:
                    images.append(None)
                    continue
                dataset = self.store.dataset(sample.dataset_id)
                classification = dataset.classification.value if dataset else "RESTRICTED"
                if self.image_policy == "INCLUDE_PERMITTED" and classification in ("PUBLIC", "PRIVATE"):
                    path = Path(sample.source_path)
                    if path.exists() and sha256_file(path) == sample.sha256:
                        images.append({"data_uri": "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode("ascii"), "sample_id": sample.sample_id, "sha256": sample.sha256, "classification": classification})
                        self.images_embedded += 1
                        continue
                self.images_withheld += 1
                images.append({"withheld": True, "sample_id": sample.sample_id, "classification": classification, "reason": "image policy EXCLUDE" if self.image_policy == "EXCLUDE" else f"classification {classification} not permitted"})
            rows_out.append({**{k: (v if not isinstance(v, float) else round(v, 6)) for k, v in pair.items()}, "images": images})
        self._add("Failure examples", "failures", {"source": source, "image_policy": self.image_policy, "pairs": rows_out})

    def _scientific_support(self) -> None:
        rows: list[list[str]] = []
        for run_id in sorted(self.run_ids):
            found = self.store.run(run_id)
            if found is None:
                continue
            run, _ = found
            rows.append(
                [
                    run_id,
                    run.scientific_support.value,
                    "RECORDED_COMPLETED_RUN_STATE",
                ]
            )
        for record in self.claim_support_records.values():
            rows.append(
                [
                    record["claim_scope"],
                    record["state"],
                    record["basis"],
                ]
            )
        self._add(
            "Scientific support",
            "table",
            {
                "columns": ["Run or claim scope", "State", "Basis"],
                "rows": rows,
                "note": "QualSight reports recorded support states and does not infer or auto-approve scientific support.",
            },
        )

    def build(self) -> dict[str, Any]:
        spec = self.spec
        title = spec.get("title") or f"QualSight report · {self.project.name}"
        if spec.get("executive_summary"):
            self._add("Executive summary", "text", spec["executive_summary"])
        if spec.get("dataset_id"):
            self._dataset(spec["dataset_id"])
        if spec.get("protocol_id"):
            self._protocol(spec["protocol_id"])
        if spec.get("quality_job_id"):
            self._engine(spec["quality_job_id"])
        if spec.get("matrix_id"):
            self._matchers(spec["matrix_id"])
        if spec.get("utility_analysis_ids"):
            self._utility(list(spec["utility_analysis_ids"]))
        if spec.get("cross_matcher_analysis_id"):
            self._cross(spec["cross_matcher_analysis_id"])
        if spec.get("figure_ids"):
            self._figures(list(spec["figure_ids"]))
        if spec.get("failure_examples"):
            self._failure_examples(spec["failure_examples"])
        limitations = list(spec.get("limitations", []))
        limitations.extend([
            "LFW results are QualSight-executed on the funneled release with SCRFD alignment under two frozen preprocessing conditions; they are not reproductions of the matchers' published LFW figures.",
            "Matcher training-set identity exclusion is recorded as each matcher's published claim (owner decision 19); QualSight does not verify it.",
            "Comparison units that share subjects are not independent; subject-cluster bootstrap intervals are reported where computed, and Wald intervals are approximations.",
        ])
        self._add("Limitations", "list", limitations)
        self._scientific_support()
        provenance = self._provenance(title)
        self._add("Provenance", "provenance", provenance)
        report_id = content_id("report", self.project.project_id, json.dumps(spec, sort_keys=True), REPORT_VERSION, _now())
        directory = self.root / report_id.replace(":", "_")
        directory.mkdir(parents=True, exist_ok=False)
        html_path = directory / "report.html"
        html_path.write_text(self._html(title, report_id), encoding="utf-8")
        pdf_path = directory / "report.pdf"
        self._pdf(title, report_id, pdf_path)
        content_path = directory / "report.json"
        content_path.write_text(json.dumps({"report_id": report_id, "title": title, "sections": [s if s["kind"] != "failures" else {**s, "content": {**s["content"], "pairs": [{**p, "images": [({k: v for k, v in i.items() if k != "data_uri"} if i else None) for i in p["images"]]} for p in s["content"]["pairs"]]}} for s in self.sections]}, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
        record = {"report_id": report_id, "version": REPORT_VERSION, "project_id": self.project.project_id, "title": title, "spec": spec, "directory": str(directory), "created_by": self.actor, "created_at": _now(),
                  "files": {"html": {"path": str(html_path), "sha256": sha256_file(html_path)}, "pdf": {"path": str(pdf_path), "sha256": sha256_file(pdf_path)}, "json": {"path": str(content_path), "sha256": sha256_file(content_path)}},
                  "provenance": provenance, "image_policy": self.image_policy, "images_embedded": self.images_embedded, "images_withheld": self.images_withheld}
        (directory / "report.provenance.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.store.record_report(record)
        return record

    # -- provenance -----------------------------------------------------------------

    def _provenance(self, title: str) -> dict[str, Any]:
        runs = []
        for run_id in sorted(self.run_ids):
            found = self.store.run(run_id)
            if found is None:
                continue
            run, evidence = found
            runs.append({"run_id": run_id, "evidence_sha256": evidence, "experiment_id": run.experiment_id, "start_time": run.start_time.isoformat(), "application_version": run.application_version, "repository": run.repository.model_dump(mode="json"), "environment_manifest_sha256": run.environment.dependency_manifest.sha256, "matcher": run.matcher.component_id, "quality_engine": run.quality_engine.resource_id if run.quality_engine else None, "scientific_support": run.scientific_support.value})
            for identity in run.model_hashes:
                self.model_hashes[identity.path] = identity.sha256
            self.dataset_hashes.setdefault("run:" + run_id[-12:], run.dataset_hash)
        support_states = {run["scientific_support"] for run in runs}
        support_states.update(
            record["state"] for record in self.claim_support_records.values()
        )
        overall_support = aggregate_scientific_support(support_states)
        return {"title": title, "project_id": self.project.project_id, "project_name": self.project.name, "generated_by": self.actor, "generated_at": _now(), "application_version": application_version(self.repository_root / "backend" / "pyproject.toml"),
                "metric_registry": {"id": METRIC_REGISTRY_ID, "version": METRIC_REGISTRY_VERSION},
                "scientific_support": {"overall_state": overall_support, "auto_approved": False, "basis": "RECORDED_RUN_AND_CLAIM_SPECIFIC_SCIENTIFIC_SUPPORT_STATES"},
                "software_versions": {"matplotlib": matplotlib.__version__}, "runs": runs, "model_hashes": self.model_hashes, "dataset_hashes": self.dataset_hashes, "figures": [{"figure_id": f["figure_id"], "sha256": {n: i["sha256"] for n, i in f["provenance"]["files"].items()}} for f in self.figures],
                "image_policy": self.image_policy, "images_embedded": self.images_embedded, "images_withheld": self.images_withheld}

    # -- renderers -------------------------------------------------------------------

    def _html(self, title: str, report_id: str) -> str:
        parts = ["<!doctype html><html lang='en'><head><meta charset='utf-8'><title>", html.escape(title), "</title><style>",
                 "body{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#111}table{border-collapse:collapse;width:100%;font-size:0.8rem;margin:0.5rem 0}th,td{border:1px solid #bbb;padding:0.25rem 0.4rem;text-align:left;vertical-align:top}th{background:#eee}h2{margin-top:1.6rem;border-bottom:1px solid #999}code{font-size:0.75rem;word-break:break-all}.fig svg{max-width:100%;height:auto}.pair{display:flex;gap:0.5rem;align-items:center;border:1px solid #ccc;padding:0.3rem;margin:0.3rem 0}.pair img{width:96px;height:96px;object-fit:cover}.withheld{width:96px;height:96px;background:#ddd;display:flex;align-items:center;justify-content:center;font-size:0.6rem;text-align:center}@media print{body{max-width:none;margin:0}h2{page-break-before:auto}.fig,table{page-break-inside:avoid}}",
                 "</style></head><body>", f"<h1>{html.escape(title)}</h1><p><code>{html.escape(report_id)}</code> · generated {html.escape(_now())} by {html.escape(self.actor)} · QualSight {html.escape(application_version(self.repository_root / 'backend' / 'pyproject.toml'))}</p>"]
        for section in self.sections:
            parts.append(f"<h2>{html.escape(section['title'])}</h2>")
            kind, content = section["kind"], section["content"]
            if kind == "text":
                parts.append(f"<p>{html.escape(str(content))}</p>")
            elif kind == "list":
                parts.append("<ul>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in content) + "</ul>")
            elif kind == "table":
                parts.append("<table><thead><tr>" + "".join(f"<th>{html.escape(str(c))}</th>" for c in content["columns"]) + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>" for row in content["rows"]) + "</tbody></table>")
            elif kind == "figure":
                figure = next(f for f in self.figures if f["figure_id"] == content["figure_id"])
                parts.append(f"<div class='fig'>{figure['svg']}</div><p><code>figure {html.escape(content['figure_id'])}</code> · sources: {html.escape(', '.join(content['source_run_ids']))} · definitions: {html.escape(json.dumps(content['metric_definitions']))} · svg sha256 {html.escape(content['sha256']['svg'])}</p>")
            elif kind == "failures":
                parts.append(f"<p>Source: <code>{html.escape(json.dumps(content['source']))}</code> · image policy {html.escape(content['image_policy'])}</p>")
                for pair in content["pairs"]:
                    cells = []
                    for image in pair["images"]:
                        if image and image.get("data_uri"):
                            cells.append(f"<img src='{image['data_uri']}' alt='' />")
                        else:
                            cells.append(f"<div class='withheld'>image withheld<br/>{html.escape((image or {}).get('classification', ''))}</div>")
                    facts = {k: v for k, v in pair.items() if k != "images"}
                    parts.append("<div class='pair'>" + "".join(cells) + f"<div><code>{html.escape(json.dumps(facts, default=str))}</code></div></div>")
            elif kind == "provenance":
                parts.append("<pre><code>" + html.escape(json.dumps(content, indent=1, sort_keys=True)) + "</code></pre>")
        parts.append("</body></html>")
        return "".join(parts)

    def _pdf(self, title: str, report_id: str, path: Path) -> None:
        with PdfPages(str(path)) as pdf:
            def text_page(heading: str, lines: list[str]) -> None:
                fig = plt.figure(figsize=(8.27, 11.69))
                fig.text(0.06, 0.95, heading, fontsize=13, weight="bold", va="top")
                y = 0.91
                for line in lines:
                    for wrapped in textwrap.wrap(line, 110) or [""]:
                        if y < 0.04:
                            pdf.savefig(fig)
                            plt.close(fig)
                            fig = plt.figure(figsize=(8.27, 11.69))
                            y = 0.95
                        fig.text(0.06, y, wrapped, fontsize=7.2, family="monospace", va="top")
                        y -= 0.014
                pdf.savefig(fig)
                plt.close(fig)

            text_page(title, [f"report {report_id}", f"generated {_now()} by {self.actor}", ""])
            for section in self.sections:
                kind, content = section["kind"], section["content"]
                if kind == "text":
                    text_page(section["title"], [str(content)])
                elif kind == "list":
                    text_page(section["title"], [f"- {item}" for item in content])
                elif kind == "table":
                    lines = [" | ".join(str(c) for c in content["columns"])] + [" | ".join(str(v) for v in row) for row in content["rows"]]
                    text_page(section["title"], lines)
                elif kind == "figure":
                    figure = next(f for f in self.figures if f["figure_id"] == content["figure_id"])
                    fig = plt.figure(figsize=(8.27, 11.69))
                    fig.text(0.06, 0.95, section["title"], fontsize=11, weight="bold", va="top")
                    ax = fig.add_axes([0.06, 0.35, 0.88, 0.56])
                    ax.imshow(mpimg.imread(figure["png_path"]))
                    ax.axis("off")
                    fig.text(0.06, 0.30, f"figure {content['figure_id']}", fontsize=6.5, family="monospace")
                    fig.text(0.06, 0.28, "sources: " + ", ".join(content["source_run_ids"])[:180], fontsize=6.5, family="monospace")
                    fig.text(0.06, 0.26, "definitions: " + json.dumps(content["metric_definitions"])[:180], fontsize=6.5, family="monospace")
                    pdf.savefig(fig)
                    plt.close(fig)
                elif kind == "failures":
                    lines = [f"source {json.dumps(content['source'])} · image policy {content['image_policy']}"]
                    for pair in content["pairs"]:
                        lines.append(json.dumps({k: v for k, v in pair.items() if k != "images"}, default=str))
                        lines.append("  images: " + "; ".join(("embedded " + i["sample_id"]) if i and i.get("data_uri") else ("withheld " + (i or {}).get("sample_id", "?")) for i in pair["images"]))
                    text_page(section["title"], lines)
                    for pair in content["pairs"]:
                        embedded = [i for i in pair["images"] if i and i.get("data_uri")]
                        if not embedded:
                            continue
                        fig = plt.figure(figsize=(8.27, 4))
                        for index, image in enumerate(embedded):
                            ax = fig.add_axes([0.05 + index * 0.45, 0.1, 0.4, 0.8])
                            ax.imshow(mpimg.imread(Path(self.store.sample(image["sample_id"]).source_path)))
                            ax.set_title(image["sample_id"][-16:], fontsize=7)
                            ax.axis("off")
                        pdf.savefig(fig)
                        plt.close(fig)
                elif kind == "provenance":
                    text_page(section["title"], json.dumps(content, indent=1, sort_keys=True).splitlines())
