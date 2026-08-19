"""Drive the real QualSight console end to end and record what was observed.

Starts the FastAPI control plane (real registered OFIQ v1.1.2 runtime, SQLite
store), serves the built PWA through ``vite preview`` (which proxies ``/api``),
opens it in headless Chromium, runs one real assessment from the browser, and
records the DOM-observed values next to the persisted Run. The screenshot is
written outside the repository because it shows a restricted conformance image.

Usage:
    python scripts/observe_console.py --screenshot-dir /path/outside/repo \
        --observation atg/evidence/<name>.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
API_PORT = 8480
PREVIEW_PORT = 5181
NODE_BIN = "/usr/bin"  # the accepted Node 20.20.2; ~/.local/bin/node shadows it with v26


def _wait_http(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - local only
                if response.status < 500:
                    return
        except (urllib.error.URLError, ConnectionError, socket.timeout) as error:
            last_error = error
        time.sleep(0.25)
    raise RuntimeError(f"{url} did not become ready: {last_error}")


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot-dir", type=Path, required=True)
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=None, help="SQLite data dir (temporary when omitted)")
    args = parser.parse_args()
    if not _port_free(API_PORT) or not _port_free(PREVIEW_PORT):
        print(f"ports {API_PORT}/{PREVIEW_PORT} are in use; refusing to observe", file=sys.stderr)
        return 2
    args.screenshot_dir.mkdir(parents=True, exist_ok=True)
    args.observation.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = None
    data_dir = args.data_dir
    if data_dir is None:
        temp_dir = tempfile.mkdtemp(prefix="qualsight-observe-")
        data_dir = Path(temp_dir)

    env = dict(os.environ)
    env["QUALSIGHT_REPOSITORY_ROOT"] = str(ROOT)
    env["QUALSIGHT_DATA_DIR"] = str(data_dir)
    env["PATH"] = f"{NODE_BIN}:{env.get('PATH', '')}"
    uv = "/home/aaron/.local/bin/uv"
    api = subprocess.Popen(
        [
            uv, "run", "--frozen", "--offline", "--directory", "backend", "python", "-m", "uvicorn",
            "qualsight.api.app:create_app", "--factory", "--host", "127.0.0.1", "--port", str(API_PORT),
            "--log-level", "warning",
        ],
        cwd=ROOT,
        env={**env, "PYTHONPATH": "src:.."},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    preview = subprocess.Popen(
        ["pnpm", "--filter", "@qualsight/frontend", "exec", "vite", "preview", "--host", "127.0.0.1", "--port", str(PREVIEW_PORT), "--strictPort"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    observation: dict[str, object] = {
        "schema_version": "1.0.0",
        "kind": "QUALSIGHT_CONSOLE_RUNTIME_OBSERVATION",
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "api_url": f"http://127.0.0.1:{API_PORT}",
        "console_url": f"http://127.0.0.1:{PREVIEW_PORT}",
        "browser": None,
        "steps": [],
    }
    exit_code = 1
    try:
        _wait_http(f"http://127.0.0.1:{API_PORT}/api/health", 90)
        _wait_http(f"http://127.0.0.1:{PREVIEW_PORT}/", 60)
        health = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/health"))  # noqa: S310
        observation["health"] = health

        from playwright.sync_api import sync_playwright  # real headless Chromium

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            observation["browser"] = f"chromium {browser.version}"
            page = browser.new_page(viewport={"width": 1500, "height": 1000})
            page.goto(f"http://127.0.0.1:{PREVIEW_PORT}/", wait_until="networkidle")
            manifest = page.evaluate("() => document.querySelector('link[rel=manifest]')?.getAttribute('href')")
            observation["steps"].append({"step": "load", "title": page.title(), "manifest": manifest, "primary_nav_links": page.evaluate("() => document.querySelectorAll('[data-testid=primary-nav] [data-testid=nav-link]').length"), "system_panel_resources": (page.wait_for_selector("[data-testid=resource-table] tbody tr", timeout=60_000) and page.evaluate("() => document.querySelectorAll('[data-testid=resource-table] tbody tr').length"))})
            page.wait_for_selector("[data-testid=engine-badge] .value", timeout=30_000)
            badge = page.inner_text("[data-testid=engine-badge]")
            observation["steps"].append({"step": "engine-badge", "text": badge})
            page.wait_for_selector("img[alt^='Registered sample']", timeout=30_000)
            image_ok = page.evaluate(
                "() => { const i = document.querySelector('img[alt^=\"Registered sample\"]'); return i && i.complete && i.naturalWidth > 0; }"
            )
            observation["steps"].append({"step": "image-rendered", "natural_size_ok": bool(image_ok)})
            # QS-B10: create a project from the console and record the run into it.
            page.fill("[data-testid=project-name-input]", "Console lifecycle observation")
            page.click("[data-testid=project-create]")
            page.wait_for_selector("[data-testid=project-actions]", timeout=30_000)
            created_message = page.inner_text("[data-testid=project-message]")
            observation["steps"].append({"step": "project-created", "message": created_message})
            console_project_id_holder = [next(p["project_id"] for p in json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects")) if p["name"] == "Console lifecycle observation")]  # noqa: S310
            page.click("[data-testid=assess-button]")
            page.wait_for_selector("[data-testid=measure-table]", timeout=180_000)
            banner = page.inner_text("[data-testid=result-banner]")
            rows = page.evaluate(
                "() => Array.from(document.querySelectorAll('[data-testid=measure-table] tbody tr')).map(r => Array.from(r.children).map(c => c.innerText))"
            )
            provenance = page.inner_text("[data-testid=provenance]")
            run_link = page.get_attribute("[data-testid=provenance] a", "href")
            observation["steps"].append(
                {
                    "step": "assessment",
                    "banner": banner,
                    "measure_rows": len(rows),
                    "unified_quality_score_row": next((r for r in rows if r and r[0] == "UnifiedQualityScore"), None),
                    "provenance_text": provenance,
                    "run_link": run_link,
                }
            )
            page.wait_for_selector("[data-testid=run-table]", timeout=30_000)
            observation["steps"].append({"step": "run-list", "rows": page.evaluate("() => document.querySelectorAll('[data-testid=run-table] tbody tr').length")})
            # Save, close, reopen, clone, archive from the console; each transition is a new version.
            transitions = []
            for label in ("Save", "Close", "Open", "Archive"):
                page.click(f"[data-testid=project-actions] button:has-text('{label}')")
                page.wait_for_function(
                    "(label) => (document.querySelector('[data-testid=project-message]')?.innerText || '').toLowerCase().startsWith(label)",
                    arg=label.lower(),
                    timeout=30_000,
                )
                transitions.append(page.inner_text("[data-testid=project-message]"))
            observation["steps"].append({"step": "project-transitions", "messages": transitions})
            facts = page.inner_text("[data-testid=project-facts]")
            observation["steps"].append({"step": "project-facts", "text": facts})
            # QS-B11..B13: reopen, import the registered LFW dataset, run the
            # subset-fixture batch, and explore the distribution down to a sample.
            page.click("[data-testid=project-actions] button:has-text('Open')")
            page.wait_for_function(
                "() => (document.querySelector('[data-testid=project-message]')?.innerText || '').toLowerCase().startsWith('open')",
                timeout=30_000,
            )
            page.click("[data-testid=import-lfw]")
            page.wait_for_selector("[data-testid=dataset-facts]", timeout=300_000)
            observation["steps"].append({"step": "dataset-imported", "facts": page.inner_text("[data-testid=dataset-facts]")})
            page.select_option("[data-testid=batch-selection]", "subset_fixture")
            page.click("[data-testid=batch-start]")
            page.wait_for_selector("[data-testid=job-progress]", timeout=30_000)
            page.wait_for_function(
                "() => /^(COMPLETED|CANCELLED|FAILED)/.test(document.querySelector('[data-testid=job-progress] p')?.innerText || '')",
                timeout=600_000,
            )
            observation["steps"].append({"step": "batch", "progress": page.inner_text("[data-testid=job-progress]")})
            page.wait_for_selector("[data-testid=job-list] button:not([disabled])", timeout=30_000)
            page.click("[data-testid=job-list] button:not([disabled])")
            page.wait_for_selector("[data-testid=histogram]", timeout=60_000)
            bins = page.evaluate(
                "() => Array.from(document.querySelectorAll('[data-testid=histogram] rect')).map(r => Number(r.getAttribute('height')))"
            )
            top_bin = max(range(len(bins)), key=lambda i: bins[i])
            page.click(f"[data-testid=bin-{top_bin}]")
            page.wait_for_selector("[data-testid=drilldown] [data-testid=drill-sample]", timeout=60_000)
            drill_count = page.evaluate("() => document.querySelectorAll('[data-testid=drill-sample]').length")
            page.wait_for_function(
                "() => { const i = document.querySelector('[data-testid=drill-sample] img'); return !!i && i.complete && i.naturalWidth > 0; }",
                timeout=30_000,
            )
            thumb_ok = page.evaluate(
                "() => { const i = document.querySelector('[data-testid=drill-sample] img'); return !!i && i.complete && i.naturalWidth > 0; }"
            )
            page.click("[data-testid=drill-sample]")
            page.wait_for_selector("[data-testid=sample-measure-table]", timeout=60_000)
            sample_rows = page.evaluate("() => document.querySelectorAll('[data-testid=sample-measure-table] tbody tr').length")
            sample_facts = page.inner_text("[data-testid=sample-quality] dl")
            # QS-C20..C24: freeze the protocol, run the fixture-pair matrix, inspect the six leaves and an error list.
            page.click("[data-testid=freeze-protocol]")
            page.wait_for_selector("[data-testid=protocol-summary]", timeout=120_000)
            page.click("[data-testid=matrix-fixture]")
            page.wait_for_function(
                "() => /completed|failed/.test(document.querySelector('[data-testid=matrix-progress]')?.innerText || '')",
                timeout=900_000,
            )
            page.wait_for_selector("[data-testid=matrix-table]", timeout=60_000)
            matrix_progress = page.inner_text("[data-testid=matrix-progress]")
            cells = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=matrix-table] button.cell')).map(b => b.innerText)")
            det_polylines = page.evaluate("() => document.querySelectorAll('[data-testid=det-plot] polyline').length")
            page.click("[data-testid=leaf-QS-C22\\.AS]")
            page.wait_for_selector("[data-testid=matcher-errors]", timeout=60_000)
            page.fill("[data-testid=matcher-errors] input", "0.9")
            page.click("[data-testid=matcher-errors] button:has-text('Recompute')")
            page.wait_for_selector("[data-testid=error-pair]", timeout=60_000)
            error_pairs = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=error-pair]')).map(e => e.innerText)")
            observation["steps"].append(
                {
                    "step": "matchers",
                    "protocol": page.inner_text("[data-testid=protocol-summary]"),
                    "matrix_progress": matrix_progress,
                    "leaf_cells": cells,
                    "det_polylines": det_polylines,
                    "error_pairs_at_0_9": error_pairs[:6],
                }
            )
            observation["steps"].append(
                {
                    "step": "explorer",
                    "bins": bins,
                    "selected_bin": top_bin,
                    "drilldown_samples_shown": drill_count,
                    "thumbnail_rendered": bool(thumb_ok),
                    "sample_measure_rows": sample_rows,
                    "sample_facts": sample_facts,
                }
            )
            # QS-D30..D33: run a utility analysis over the fixture matcher Run and the fixture quality job, inspect the results.
            page.wait_for_function("() => document.querySelectorAll('[data-testid=utility-leaf] option').length > 0 && document.querySelectorAll('[data-testid=utility-job] option').length > 0", timeout=60_000)
            page.fill("[data-testid=utility-q]", "40")
            page.fill("[data-testid=utility-replicates]", "200")
            page.select_option("[data-testid=utility-policy]", "pair.quality.min")
            page.click("[data-testid=utility-run]")
            page.wait_for_selector("[data-testid=utility-detail]", timeout=300_000)
            page.wait_for_selector("[data-testid=erc-plot] polyline", state="attached", timeout=60_000)
            utility_rows = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=utility-table] tbody tr')).map(r => r.innerText)")
            policy_table = page.inner_text("[data-testid=policy-table]")
            conditioned_rows = page.evaluate("() => document.querySelectorAll('[data-testid=conditioned-table] tbody tr').length")
            measure_rows = page.evaluate("() => document.querySelectorAll('[data-testid=measure-impact] tbody tr').length")
            page.click("[data-testid=pairs-rejected]")
            page.wait_for_selector("[data-testid=utility-pairs]", timeout=60_000)
            rejected_pairs = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=utility-pair]')).map(e => e.innerText)")
            observation["steps"].append(
                {
                    "step": "utility",
                    "analysis_rows": utility_rows,
                    "erc_polylines": page.evaluate("() => document.querySelectorAll('[data-testid=erc-plot] polyline').length"),
                    "policy_table": policy_table,
                    "conditioned_bins": conditioned_rows,
                    "per_measure_rows": measure_rows,
                    "rejected_pairs": rejected_pairs[:6],
                    "detail_heading": page.inner_text("[data-testid=utility-detail] h3"),
                }
            )
            # QS-E40..E42: run the cross-matcher contrasts over the fixture matrix, open a contrast's disagreement list, annotate a pair.
            page.wait_for_function("() => document.querySelectorAll('[data-testid=cross-matrix] option').length > 0 && document.querySelectorAll('[data-testid=cross-job] option').length > 0", timeout=60_000)
            page.fill("[data-testid=cross-replicates]", "100")
            page.click("[data-testid=cross-run]")
            page.wait_for_selector("[data-testid=cross-detail]", timeout=600_000)
            contrast_rows = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=contrast-row]')).map(r => r.innerText)")
            page.click("[data-testid=contrast-row] [data-testid=pairs-BOTH]")
            page.wait_for_selector("[data-testid=cross-pairs]", timeout=60_000)
            cross_pairs = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=cross-pair]')).map(e => e.innerText)")
            shown_category = "BOTH"
            if not cross_pairs:
                # The fixture pairs carry no shared false non-matches at these thresholds; review the agreed-correct cell instead.
                page.click("[data-testid=contrast-row] [data-testid=pairs-NEITHER]")
                page.wait_for_selector("[data-testid=cross-pair]", timeout=60_000)
                cross_pairs = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=cross-pair]')).map(e => e.innerText)")
                shown_category = "NEITHER"
            annotation_saved = None
            if cross_pairs:
                page.fill("[data-testid=cross-pair] [data-testid=annotation-input]", "console observation 07: reviewed")
                page.click("[data-testid=cross-pair] [data-testid=annotation-save]")
                page.wait_for_selector("[data-testid=cross-pair] .annotations li", timeout=60_000)
                annotation_saved = page.inner_text("[data-testid=cross-pair] .annotations li")
            observation["steps"].append(
                {
                    "step": "cross-matcher",
                    "contrast_rows": contrast_rows,
                    "shown_category": shown_category,
                    "pairs_shown": cross_pairs[:6],
                    "annotation_saved": annotation_saved,
                    "detail_heading": page.inner_text("[data-testid=cross-detail] h3"),
                }
            )
            # QS-F50..F53: figures, dashboard save/reload, report build, bundle export, reproduction.
            page.wait_for_function("() => !document.querySelector('[data-testid=figure-det]')?.disabled", timeout=60_000)
            page.click("[data-testid=figure-det]")
            page.wait_for_selector("[data-testid=figure-detail] img", timeout=120_000)
            page.click("[data-testid=figure-erc]")
            page.wait_for_function("() => document.querySelectorAll('[data-testid=figure-thumb]').length >= 2", timeout=120_000)
            page.wait_for_function("() => { const i = document.querySelector('[data-testid=figure-detail] img'); return !!i && i.complete && i.naturalWidth > 0; }", timeout=60_000)
            figure_facts = page.inner_text("[data-testid=figure-detail] dl")
            page.click("[data-testid=dashboard-save]")
            page.wait_for_selector("[data-testid=dashboard-panel]", timeout=120_000)
            dashboard_panels = page.evaluate("() => document.querySelectorAll('[data-testid=dashboard-panel]').length")
            dashboard_message = page.inner_text("[data-testid=project-message]") if page.query_selector("[data-testid=project-message]") else None
            page.select_option("[data-testid=report-image-policy]", "INCLUDE_PERMITTED")
            page.click("[data-testid=report-build]")
            page.wait_for_function("() => document.querySelectorAll('[data-testid=report-table] tr').length >= 1", timeout=300_000)
            report_rows = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=report-table] tr')).map(r => r.innerText)")
            page.click("[data-testid=bundle-export-analysis]")
            page.wait_for_function("() => document.querySelectorAll('[data-testid=bundle-table] tr').length >= 1", timeout=120_000)
            page.click("[data-testid=reproduce]")
            page.wait_for_selector("[data-testid=reproduction-state]", timeout=300_000)
            reproduction_states = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=reproduction-state]')).map(e => e.innerText)")
            observation["steps"].append(
                {
                    "step": "reports",
                    "figure_facts": figure_facts,
                    "figures": page.evaluate("() => document.querySelectorAll('[data-testid=figure-thumb]').length"),
                    "dashboard_panels": dashboard_panels,
                    "dashboard_message": dashboard_message,
                    "report_rows": report_rows,
                    "reproduction_states": reproduction_states,
                }
            )
            # QS-X60/QS-X61: 1:N identification over the fixture subjects and a controlled-degradation response through the console.
            page.wait_for_function("() => !document.querySelector('[data-testid=ident-freeze]')?.disabled", timeout=60_000)
            page.click("[data-testid=ident-freeze]")
            page.wait_for_selector("[data-testid=ident-search]", timeout=60_000)
            page.click("[data-testid=ident-search]")
            page.wait_for_selector("[data-testid=ident-detail]", timeout=600_000)
            page.wait_for_selector("[data-testid=cmc-plot] polyline", state="attached", timeout=60_000)
            page.click("[data-testid=ident-candidates]")
            page.wait_for_selector("[data-testid=ident-candidate-table] tbody tr", timeout=60_000)
            ident_rows = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=ident-searches] tbody tr')).map(r => r.innerText)")
            page.click("[data-testid=transform-run]")
            page.wait_for_selector("[data-testid=derived-batch]", timeout=120_000)
            page.click("[data-testid=derived-batch]")
            page.wait_for_function("() => (document.querySelector('[data-testid=project-message]')?.innerText || '').includes('OFIQ batch')", timeout=60_000)
            # wait until the derived batch is complete by polling the API for a job over the derived dataset
            derived_sets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects/" + urllib.parse.quote(console_project_id_holder[0], safe='') + "/derived-sets"))  # noqa: S310
            derived_dataset = derived_sets[-1]["derived_dataset_id"]
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                jobs_api = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects/" + urllib.parse.quote(console_project_id_holder[0], safe='') + "/jobs"))  # noqa: S310
                if any(j["dataset_id"] == derived_dataset and j["state"] == "COMPLETED" for j in jobs_api):
                    break
                time.sleep(2)
            page.wait_for_function("() => !document.querySelector('[data-testid=degradation-run]')?.disabled", timeout=60_000)
            page.click("[data-testid=degradation-run]")
            page.wait_for_selector("[data-testid=degradation-detail]", timeout=600_000)
            degradation_tables = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=degradation-table]')).map(t => t.innerText.slice(0, 400))")
            observation["steps"].append({"step": "advanced", "identification_rows": ident_rows, "degradation_tables": degradation_tables})
            projects_api = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects"))  # noqa: S310
            console_project = next((p for p in projects_api if p["name"] == "Console lifecycle observation"), None)
            observation["console_project"] = (
                {"project_id": console_project["project_id"], "lifecycle": console_project["lifecycle"], "version": console_project["version"], "run_ids": console_project["run_ids"]}
                if console_project else None
            )
            shot = args.screenshot_dir / "qualsight-console-observation.png"
            page.screenshot(path=str(shot), full_page=True)
            observation["screenshot"] = {
                "path": str(shot),
                "sha256": hashlib.sha256(shot.read_bytes()).hexdigest(),
                "size_bytes": shot.stat().st_size,
                "note": "kept outside the repository: it shows the restricted OFIQ conformance image",
            }
            # Cross-check the browser-observed run against the persisted API record.
            if run_link:
                stored = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}{run_link}"))  # noqa: S310
                metrics = {m["metric_id"]: m["value"] for m in stored["run"]["metrics"]}
                observation["persisted_run"] = {
                    "run_id": stored["run"]["run_id"],
                    "evidence_sha256": stored["evidence_sha256"],
                    "UnifiedQualityScore.native": metrics.get("UnifiedQualityScore.native"),
                    "UnifiedQualityScore.scalar": metrics.get("UnifiedQualityScore.scalar"),
                    "metric_count": len(metrics),
                }
            browser.close()
        exit_code = 0
    finally:
        for proc in (preview, api):
            try:
                os.killpg(proc.pid, 15)
            except ProcessLookupError:
                pass
        for proc in (preview, api):
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, 9)
        observation["api_log_tail"] = (api.stdout.read().decode("utf-8", "replace")[-2000:] if api.stdout else "")
        observation["exit_code"] = exit_code
        args.observation.write_text(json.dumps(observation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        print(f"observation written to {args.observation} exit={exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
