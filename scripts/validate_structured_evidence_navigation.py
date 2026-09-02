"""Validate structured in-app evidence navigation against recorded real evidence.

The validator takes a SQLite online backup of the workstation's populated
QualSight repository, serves that isolated copy through the real backend, and
drives the built console with Chromium.  It never creates or substitutes
biometric records.  The gate covers every converted primary evidence action;
intentional file downloads and collapsed raw-record actions remain distinct.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import traceback
import urllib.parse

from validate_comparison_studio import _json_request, _port_is_free, _stop, _wait_http


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DATABASE = ROOT / "var" / "qualsight.sqlite3"
API_PORT = 8480
PREVIEW_PORT = 5198
NODE_EXECUTABLE = shutil.which("node")
if NODE_EXECUTABLE is None:
    raise RuntimeError("the reviewed Node executable is not available on PATH")
NODE_BIN = str(Path(NODE_EXECUTABLE).parent)


def _uv_binary() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv is unavailable on PATH")
    return executable

# Primary API anchors must never take the operator out of the application.
# Intentional file exports are excluded: they remain explicit download actions.
LEGACY_PRIMARY_RAW_ANCHORS: dict[str, tuple[str, ...]] = {
    "Workbench.tsx": (
        'href={`/api/runs/${activeJob.run_id}`}',
        'href={`/api/runs/${distribution.run_id}`}',
        "href={currentResult.links.run}",
        "href={currentResult.links.audit}",
    ),
    "Matchers.tsx": (
        'href={`/api/runs/${errors.run_id}`}',
        "href={row.links.left_sample}",
        "href={row.links.right_sample}",
    ),
    "Advanced.tsx": (
        'href={`/api/runs/${searchDetail.run_id}`}',
        'href={`/api/runs/${degradationDetail.run_id}`}',
    ),
    "CrossMatcher.tsx": (
        "href={item.links.run}",
        'href={`/api/runs/${fullContrast.a.run_id}`}',
        'href={`/api/runs/${fullContrast.b.run_id}`}',
        "href={detail.summary.links.run}",
        "href={row.links.left_quality}",
        "href={row.links.right_quality}",
        "href={row.links.run_a}",
        "href={row.links.run_b}",
    ),
    "Utility.tsx": (
        "href={detail.summary.links.run}",
        "href={row.links.left_quality}",
        "href={row.links.right_quality}",
        "href={row.links.run}",
    ),
    "Reports.tsx": ("href={selectedFigure.files.provenance}",),
    "SystemPanel.tsx": ("href={item.links.run}",),
    "ComparisonStudio.tsx": (
        "href={evidence.links.run}",
        "href={evidence.links.status}",
    ),
}

WORKFLOW_EVIDENCE_SITES: dict[str, tuple[str, ...]] = {
    "Workbench": (
        "workbench-active-run-evidence",
        "workbench-distribution-run-evidence",
        "workbench-sample-run-evidence",
        "workbench-run-audit-evidence",
    ),
    "Matchers": (
        "matcher-error-run-evidence",
        "matcher-left-sample-evidence",
        "matcher-right-sample-evidence",
    ),
    "Advanced": (
        "advanced-search-run-evidence",
        "advanced-degradation-run-evidence",
    ),
    "CrossMatcher": (
        "cross-analysis-run-evidence",
        "cross-contrast-run-a-evidence",
        "cross-contrast-run-b-evidence",
        "cross-summary-run-evidence",
        "cross-left-quality-evidence",
        "cross-right-quality-evidence",
        "cross-pair-run-a-evidence",
        "cross-pair-run-b-evidence",
    ),
    "Utility": (
        "utility-detail-run-evidence",
        "utility-left-quality-evidence",
        "utility-right-quality-evidence",
        "utility-pair-run-evidence",
    ),
    "Reports": ("report-figure-provenance-evidence",),
    "SystemPanel": ("system-embedding-run-evidence",),
}


def _legacy_primary_raw_anchors() -> list[str]:
    found: list[str] = []
    source_root = ROOT / "frontend" / "src"
    for filename, anchors in LEGACY_PRIMARY_RAW_ANCHORS.items():
        source = (source_root / filename).read_text(encoding="utf-8")
        for anchor in anchors:
            if anchor in source:
                found.append(f"{filename}: {anchor}")
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _snapshot_database(source: Path, target: Path) -> None:
    """Use SQLite's online-backup API so committed real records stay coherent."""

    source_uri = f"file:{urllib.parse.quote(str(source))}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_connection:
        with sqlite3.connect(target) as target_connection:
            source_connection.backup(target_connection)


def _environment(data_dir: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{NODE_BIN}:{os.environ.get('PATH', '')}",
        "QUALSIGHT_REPOSITORY_ROOT": str(ROOT),
        "QUALSIGHT_DATA_DIR": str(data_dir),
    }


def _start_api(data_dir: Path) -> subprocess.Popen[str]:
    environment = _environment(data_dir)
    return subprocess.Popen(
        [
            _uv_binary(),
            "run",
            "--frozen",
            "--offline",
            "--directory",
            "backend",
            "python",
            "-m",
            "uvicorn",
            "qualsight.api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(API_PORT),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env={**environment, "PYTHONPATH": "src:.."},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )


def _start_preview(data_dir: Path) -> subprocess.Popen[str]:
    environment = _environment(data_dir)
    return subprocess.Popen(
        [
            "pnpm",
            "--filter",
            "@qualsight/frontend",
            "exec",
            "vite",
            "preview",
            "--host",
            "127.0.0.1",
            "--port",
            str(PREVIEW_PORT),
            "--strictPort",
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )


def _processes(data_dir: Path) -> tuple[subprocess.Popen[str], subprocess.Popen[str]]:
    return _start_api(data_dir), _start_preview(data_dir)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()

    legacy_anchors = _legacy_primary_raw_anchors()
    if legacy_anchors:
        raise AssertionError(
            f"{len(legacy_anchors)} primary raw API anchors remain:\n" + "\n".join(legacy_anchors)
        )

    if not SOURCE_DATABASE.is_file():
        raise FileNotFoundError(f"populated QualSight repository is unavailable: {SOURCE_DATABASE}")
    if not _port_is_free(API_PORT) or not _port_is_free(PREVIEW_PORT):
        raise RuntimeError(f"ports {API_PORT}/{PREVIEW_PORT} must be free")

    args.observation.parent.mkdir(parents=True, exist_ok=True)
    if args.screenshot is not None:
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)

    source_stat = SOURCE_DATABASE.stat()
    data_dir = Path(tempfile.mkdtemp(prefix="qualsight-structured-evidence-"))
    snapshot = data_dir / "qualsight.sqlite3"
    _snapshot_database(SOURCE_DATABASE, snapshot)

    with sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True) as connection:
        project_count = int(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0])
        run_count = int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        sensitive_source_paths = [
            str(row[0])
            for row in connection.execute(
                "SELECT json_extract(record_json, '$.source_path') FROM samples "
                "WHERE json_extract(record_json, '$.source_path') IS NOT NULL LIMIT 10"
            ).fetchall()
        ]
    if project_count < 1 or run_count < 1:
        raise RuntimeError(
            "structured-evidence validation requires the real repository to contain a project and a Run"
        )

    observed: dict[str, object] = {
        "kind": "QUALSIGHT_STRUCTURED_EVIDENCE_NAVIGATION_VALIDATION",
        "source_repository": {
            "path": str(SOURCE_DATABASE),
            "sha256": _sha256(SOURCE_DATABASE),
            "size_bytes": source_stat.st_size,
            "mtime_ns": source_stat.st_mtime_ns,
            "project_count": project_count,
            "run_count": run_count,
            "sensitive_source_path_hashes": [
                hashlib.sha256(path.encode("utf-8")).hexdigest() for path in sensitive_source_paths
            ],
        },
        "isolated_snapshot": str(snapshot),
        "passed": False,
    }
    api_process: subprocess.Popen[str] | None = None
    preview_process: subprocess.Popen[str] | None = None
    exit_code = 1
    try:
        api_process, preview_process = _processes(data_dir)
        _wait_http(f"http://127.0.0.1:{API_PORT}/api/health", 90)
        _wait_http(f"http://127.0.0.1:{PREVIEW_PORT}/", 60)

        projects = _json_request(f"http://127.0.0.1:{API_PORT}/api/projects")
        if not isinstance(projects, list):
            raise AssertionError("GET /api/projects did not return a project list")
        populated_projects = [
            project
            for project in projects
            if isinstance(project, dict) and isinstance(project.get("run_ids"), list) and project["run_ids"]
        ]
        if not populated_projects:
            raise AssertionError("the isolated real repository exposed no populated project")
        populated_projects.sort(key=lambda project: len(project["run_ids"]), reverse=True)
        if len(populated_projects) < 2:
            raise AssertionError("stale-context validation requires two real populated projects")
        project = populated_projects[0]
        project_id = str(project["project_id"])
        encoded_project_id = urllib.parse.quote(project_id, safe="")
        runs = _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects/{encoded_project_id}/runs?limit=10000"
        )
        if not isinstance(runs, list) or not runs:
            raise AssertionError("the selected real project exposed no recorded Runs")
        expected_run_ids = {str(run["run_id"]) for run in runs if isinstance(run, dict) and run.get("run_id")}
        if len(expected_run_ids) < 3:
            raise AssertionError("error/retry and stale-context validation require three real Runs in one project")
        other_project = populated_projects[1]
        other_project_id = str(other_project["project_id"])

        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            console_errors: list[str] = []
            request_failures: list[dict[str, str]] = []
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.on(
                "requestfailed",
                lambda request: request_failures.append(
                    {
                        "method": request.method,
                        "url": request.url,
                        "failure": request.failure or "unknown request failure",
                    }
                ),
            )
            page.goto(f"http://127.0.0.1:{PREVIEW_PORT}/#/projects", wait_until="networkidle")
            page.locator("[data-testid=project-select]").select_option(project_id)
            run_table = page.locator("[data-testid=run-table]")
            run_table.wait_for(state="visible")
            page.goto(f"http://127.0.0.1:{PREVIEW_PORT}/#/home")
            page.locator("[data-testid=home-panel][data-state=ready]").wait_for(state="visible")

            def home_task(label: str):
                return page.locator("[data-testid=home-task]").filter(has_text=label)

            expected_destinations = {
                "Compare images": "#/matchers",
                "Stress-test an image": "#/advanced",
                "Analyze a dataset": "#/data",
                "Compare matchers": "#/matchers",
            }
            for label, destination in expected_destinations.items():
                task = home_task(label)
                if task.count() != 1 or task.get_attribute("data-destination") != destination:
                    raise AssertionError(f"Home task {label!r} does not route to {destination}")
            stress_task = home_task("Stress-test an image")
            stress_reason = stress_task.locator("[data-testid=home-task-reason]")
            if stress_task.get_attribute("aria-disabled") != "true" or stress_reason.count() != 1:
                raise AssertionError("Home Stress task does not expose its working-sample prerequisite")
            if "working image" not in stress_reason.inner_text().lower():
                raise AssertionError(f"Home Stress prerequisite is not actionable: {stress_reason.inner_text()}")
            observed["home_workflow_contract"] = {
                "destinations": expected_destinations,
                "stress_prerequisite": stress_reason.inner_text(),
            }

            workflow_evidence: list[dict[str, str]] = []

            def open_workflow_evidence(test_id: str, workflow: str) -> None:
                action = page.locator(f"[data-testid='{test_id}']").first
                action.wait_for(state="visible", timeout=120_000)
                action.scroll_into_view_if_needed()
                evidence_id = action.get_attribute("data-evidence-id")
                evidence_kind = action.get_attribute("data-evidence-kind")
                evidence_project_id = action.get_attribute("data-project-id")
                evidence_href = action.get_attribute("data-evidence-href")
                if not evidence_id or not evidence_kind:
                    raise AssertionError(f"{workflow} {test_id} does not publish its evidence identity and kind")
                if evidence_project_id != project_id:
                    raise AssertionError(
                        f"{workflow} {test_id} is not bound to the active project: {evidence_project_id!r}"
                    )
                if not evidence_href or not evidence_href.startswith("/api/"):
                    raise AssertionError(f"{workflow} {test_id} does not retain its real API record")
                route_before = page.url
                action.focus()
                page.keyboard.press("Enter")
                detail = page.locator(
                    f"[data-testid=evidence-detail][data-evidence-id='{evidence_id}']"
                    f"[data-project-id='{project_id}']"
                )
                page.wait_for_function(
                    "([evidenceId, projectId]) => Boolean("
                    "document.querySelector(`[data-testid='evidence-detail'][data-evidence-id='${evidenceId}'][data-project-id='${projectId}']`)"
                    " || document.querySelector(`[data-testid='evidence-error'][data-project-id='${projectId}']`)"
                    ")",
                    arg=[evidence_id, project_id],
                    timeout=180_000,
                )
                error = page.locator(
                    f"[data-testid=evidence-error][data-project-id='{project_id}']"
                )
                if error.count():
                    raise AssertionError(
                        f"{workflow} {test_id} failed to load its structured record: {error.inner_text()}"
                    )
                detail.wait_for(state="visible")
                if detail.get_attribute("data-evidence-kind") != evidence_kind:
                    raise AssertionError(f"{workflow} {test_id} opened the wrong structured evidence kind")
                if page.url != route_before:
                    raise AssertionError(f"{workflow} {test_id} left its in-app route: {page.url}")
                if not page.evaluate(
                    "document.querySelector('[data-testid=evidence-detail]').contains(document.activeElement)"
                ):
                    raise AssertionError(f"{workflow} {test_id} did not move keyboard focus into Evidence detail")
                if detail.locator("h4").count() < 1 or detail.locator("dl, table, ol").count() < 1:
                    raise AssertionError(f"{workflow} {test_id} has no readable structured sections")
                if detail.locator("pre").count() != 0:
                    raise AssertionError(f"{workflow} {test_id} rendered raw JSON as primary evidence")
                primary_text = detail.inner_text()
                if any(path in primary_text for path in sensitive_source_paths):
                    raise AssertionError(f"{workflow} {test_id} exposed a recorded filesystem source path")
                actions = detail.locator("details[data-testid=evidence-actions]")
                if actions.count() != 1 or actions.evaluate("element => element.open"):
                    raise AssertionError(f"{workflow} {test_id} did not keep raw actions secondary and collapsed")
                if actions.locator("a[href^='/api/']").count() != 2:
                    raise AssertionError(f"{workflow} {test_id} did not retain View and Download raw actions")
                if detail.locator("a[href^='/api/']").count() != actions.locator("a[href^='/api/']").count():
                    raise AssertionError(f"{workflow} {test_id} left a raw API anchor outside Evidence actions")
                close = detail.get_by_role("button", name="Close")
                close.focus()
                page.keyboard.press("Enter")
                detail.wait_for(state="detached", timeout=30_000)
                page.wait_for_function(
                    "([testId, evidenceId]) => Array.from(document.querySelectorAll(`[data-testid='${testId}']`))"
                    ".some(element => element.getAttribute('data-evidence-id') === evidenceId && document.activeElement === element)",
                    arg=[test_id, evidence_id],
                    timeout=30_000,
                )
                workflow_evidence.append(
                    {
                        "workflow": workflow,
                        "test_id": test_id,
                        "kind": evidence_kind,
                        "evidence_id": evidence_id,
                        "project_id": evidence_project_id,
                        "href": evidence_href,
                    }
                )

            page.goto(f"http://127.0.0.1:{PREVIEW_PORT}/#/projects")
            run_table.wait_for(state="visible")
            run_actions = run_table.locator("tbody [data-testid=evidence-open]")
            run_action = run_actions.first
            run_href = run_action.get_attribute("data-evidence-href") or run_action.get_attribute("href")
            run_prefix = f"/api/projects/{encoded_project_id}/runs/"
            if not run_href or not run_href.startswith(run_prefix):
                raise AssertionError(f"recorded Run action did not expose its real API identity: {run_href!r}")
            selected_run_id = urllib.parse.unquote(run_href.removeprefix(run_prefix))
            if selected_run_id not in expected_run_ids:
                raise AssertionError("the rendered Run action is not bound to the active project's API records")

            page.evaluate(
                """
                window.__qualsightEvidenceStates = [];
                new MutationObserver(() => {
                  for (const name of ["evidence-loading", "evidence-empty", "evidence-error", "evidence-detail"]) {
                    if (document.querySelector(`[data-testid="${name}"]`)) {
                      window.__qualsightEvidenceStates.push(name);
                    }
                  }
                }).observe(document.body, { childList: true, subtree: true });
                """
            )
            run_action.focus()
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)
            if args.screenshot is not None:
                page.screenshot(path=str(args.screenshot), full_page=True)

            evidence = page.locator("[data-testid=evidence-detail]")
            current_url = page.url
            if evidence.count() == 0:
                raise AssertionError(
                    "recorded Run action left the in-app Projects workflow instead of opening structured Evidence detail; "
                    f"browser URL is {current_url}"
                )
            evidence.wait_for(state="visible")
            focus_moved_to_evidence = page.evaluate(
                "document.querySelector('[data-testid=evidence-detail]').contains(document.activeElement)"
            )
            if not focus_moved_to_evidence:
                raise AssertionError("keyboard activation did not move focus to the opened Evidence detail")
            if page.url != f"http://127.0.0.1:{PREVIEW_PORT}/#/projects":
                raise AssertionError(f"Evidence detail did not retain the Projects route: {page.url}")
            if evidence.get_attribute("data-project-id") != project_id:
                raise AssertionError("Evidence detail is not bound to the active project")
            if evidence.get_attribute("data-evidence-id") != selected_run_id:
                raise AssertionError("Evidence detail is not bound to the selected Run")
            if evidence.locator("h2, h3").count() == 0:
                raise AssertionError("Evidence detail has no readable heading")
            if evidence.locator("dl, table").count() == 0:
                raise AssertionError("Evidence detail has no readable structured sections")
            if evidence.locator("pre").count() != 0:
                raise AssertionError("raw JSON is rendered as primary evidence")
            primary_evidence_text = evidence.inner_text()
            if any(path in primary_evidence_text for path in sensitive_source_paths):
                raise AssertionError("primary structured Evidence exposes a recorded filesystem source path")
            structured_sections = evidence.locator("dl, table").count()

            actions = evidence.locator("details[data-testid=evidence-actions]")
            if actions.count() != 1:
                raise AssertionError("Evidence detail does not provide one explicit secondary Evidence actions disclosure")
            if actions.evaluate("element => element.open"):
                raise AssertionError("secondary Evidence actions are expanded by default")
            if actions.locator("a[href^='/api/']").count() < 1:
                raise AssertionError("Evidence actions do not retain access to the byte-equivalent API record")
            if evidence.locator("a[href^='/api/']").count() != actions.locator("a[href^='/api/']").count():
                raise AssertionError("raw API navigation remains outside the secondary Evidence actions disclosure")
            raw_actions_open_by_default = actions.evaluate("element => element.open")
            observed_states = page.evaluate("window.__qualsightEvidenceStates")
            if "evidence-loading" not in observed_states:
                raise AssertionError("opening recorded evidence did not expose a loading state")

            if console_errors:
                raise AssertionError(f"browser console errors before outage validation: {console_errors}")

            outage_action = run_actions.nth(1)
            outage_href = outage_action.get_attribute("data-evidence-href") or outage_action.get_attribute("href")
            if not outage_href or not outage_href.startswith(run_prefix):
                raise AssertionError("second recorded Run action did not retain its API identity")
            outage_run_id = urllib.parse.unquote(outage_href.removeprefix(run_prefix))
            if outage_run_id not in expected_run_ids:
                raise AssertionError("outage validation Run is not bound to the active project")
            assert api_process is not None
            _stop(api_process)
            observed["intentional_outage_api_returncode"] = api_process.returncode
            observed["intentional_outage_api_output"] = api_process.stdout.read()[-4000:] if api_process.stdout else ""
            api_process = None
            outage_action.focus()
            page.keyboard.press("Enter")
            error_panel = page.locator("[data-testid=evidence-error]")
            error_panel.wait_for(state="visible")
            if error_panel.get_attribute("data-project-id") != project_id:
                raise AssertionError("evidence error state lost its active-project binding")
            if error_panel.get_by_role("button", name="Retry evidence").count() != 1:
                raise AssertionError("evidence error state has no accessible Retry action")
            outage_console_errors = list(console_errors)
            console_errors.clear()

            api_process = _start_api(data_dir)
            _wait_http(f"http://127.0.0.1:{API_PORT}/api/health", 90)
            page.wait_for_timeout(500)
            console_errors.clear()
            error_panel.get_by_role("button", name="Retry evidence").click()
            recovered = page.locator(
                f"[data-testid=evidence-detail][data-evidence-id='{outage_run_id}'][data-project-id='{project_id}']"
            )
            recovered.wait_for(state="visible")

            stale_action = run_actions.nth(2)
            stale_href = stale_action.get_attribute("data-evidence-href") or stale_action.get_attribute("href")
            if not stale_href or not stale_href.startswith(run_prefix):
                raise AssertionError("third recorded Run action did not retain its API identity")
            stale_run_id = urllib.parse.unquote(stale_href.removeprefix(run_prefix))
            if stale_run_id not in expected_run_ids:
                raise AssertionError("stale-response validation Run is not bound to the active project")
            page.evaluate(
                """
                href => {
                  window.__qualsightOriginalFetch = window.fetch.bind(window);
                  window.__qualsightHeldResponseReady = false;
                  window.__qualsightReleaseHeldResponse = null;
                  window.fetch = (input, init) => {
                    const url = typeof input === "string" ? input : input.url;
                    const actual = window.__qualsightOriginalFetch(input, init);
                    if (url !== href) return actual;
                    return actual.then(response => new Promise(resolve => {
                      window.__qualsightHeldResponseReady = true;
                      window.__qualsightReleaseHeldResponse = () => resolve(response);
                    }));
                  };
                }
                """,
                stale_href,
            )
            stale_action.focus()
            page.keyboard.press("Enter")
            page.wait_for_function("window.__qualsightHeldResponseReady === true")
            page.locator("[data-testid=project-select]").select_option(other_project_id)
            cleared = page.locator("[data-testid=evidence-empty]")
            cleared.wait_for(state="visible")
            if "project" not in cleared.inner_text().lower():
                raise AssertionError("context-cleared Evidence state does not explain the project boundary")
            page.evaluate("window.__qualsightReleaseHeldResponse()")
            page.wait_for_timeout(500)
            if page.locator(f"[data-testid=evidence-detail][data-evidence-id='{stale_run_id}']").count() != 0:
                raise AssertionError("a late real Run response reappeared after the active project changed")
            page.evaluate(
                """
                window.fetch = window.__qualsightOriginalFetch;
                delete window.__qualsightOriginalFetch;
                delete window.__qualsightHeldResponseReady;
                delete window.__qualsightReleaseHeldResponse;
                """
            )

            page.locator("[data-testid=project-select]").select_option(project_id)
            run_table.wait_for(state="visible")
            run_actions.first.focus()
            page.keyboard.press("Enter")
            responsive_evidence = page.locator(
                f"[data-testid=evidence-detail][data-evidence-id='{selected_run_id}'][data-project-id='{project_id}']"
            )
            responsive_evidence.wait_for(state="visible")
            if not page.evaluate(
                "document.querySelector('[data-testid=evidence-detail]').contains(document.activeElement)"
            ):
                raise AssertionError("reopened Evidence detail did not retain keyboard focus management")

            responsive_results: list[dict[str, object]] = []
            for label, width, height in [
                ("desktop", 1440, 1050),
                ("tablet", 900, 1000),
                ("mobile", 390, 844),
                ("effective-200-percent", 720, 900),
                ("effective-400-percent", 360, 800),
            ]:
                page.set_viewport_size({"width": width, "height": height})
                layout = page.evaluate(
                    """
                    ({label, width}) => {
                      const evidence = document.querySelector('[data-testid="evidence-detail"]');
                      const visible = element => {
                        const style = getComputedStyle(element);
                        const box = element.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0;
                      };
                      const controls = Array.from(evidence.querySelectorAll('button, summary, a[href]'))
                        .filter(visible)
                        .map(element => {
                          const box = element.getBoundingClientRect();
                          return {tag: element.tagName, text: (element.textContent || '').trim().slice(0, 80), width: box.width, height: box.height};
                        });
                      const evidenceBox = evidence.getBoundingClientRect();
                      return {
                        label,
                        viewport_width: width,
                        document_client_width: document.documentElement.clientWidth,
                        document_scroll_width: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
                        evidence_left: evidenceBox.left,
                        evidence_right: evidenceBox.right,
                        controls,
                        undersized: controls.filter(item => item.width < 44 || item.height < 44),
                      };
                    }
                    """,
                    {"label": label, "width": width},
                )
                if layout["document_scroll_width"] > layout["document_client_width"]:
                    raise AssertionError(f"Evidence workflow overflows horizontally at {label}: {layout}")
                if layout["evidence_left"] < -1 or layout["evidence_right"] > width + 1:
                    raise AssertionError(f"Evidence detail escapes the viewport at {label}: {layout}")
                if layout["undersized"]:
                    raise AssertionError(f"Evidence controls are smaller than 44px at {label}: {layout['undersized']}")
                responsive_results.append(layout)

            responsive_evidence.get_by_role("button", name="Close").click()
            page.locator("[data-testid=evidence-empty]").wait_for(state="visible")
            page.wait_for_function(
                "document.activeElement === document.querySelector('[data-testid=run-table] tbody [data-testid=evidence-open]')",
                timeout=30_000,
            )
            focus_returned_to_trigger = True
            if not focus_returned_to_trigger:
                raise AssertionError("closing structured Evidence did not return keyboard focus to its opening action")

            page.set_viewport_size({"width": 1440, "height": 1050})

            page.goto(f"http://127.0.0.1:{PREVIEW_PORT}/#/matchers", wait_until="networkidle")
            page.locator("[data-testid=comparison-mode-dataset]").click()
            page.locator("[data-testid=matrix-table] button.cell").first.wait_for(state="visible", timeout=60_000)
            if page.locator("#matcher-feed-error, #matcher-dataset-error").count() != 0:
                raise AssertionError("Compare retained a hidden-panel feed error after route entry and API recovery")
            observed["route_entry_recovery"] = {
                "workflow": "Matchers",
                "active_project_id": project_id,
                "recovered_without_page_reload": True,
            }
            console_errors.clear()

            page.goto(f"http://127.0.0.1:{PREVIEW_PORT}/#/data", wait_until="networkidle")
            page.locator("[data-testid=dataset-facts]").wait_for(state="visible", timeout=60_000)
            page.locator("[data-testid=job-list] button:not([disabled])").first.click()
            page.locator("[data-testid=histogram]").wait_for(state="visible", timeout=120_000)
            open_workflow_evidence("workbench-active-run-evidence", "Workbench")
            open_workflow_evidence("workbench-distribution-run-evidence", "Workbench")
            populated_bin = page.evaluate(
                """() => {
                  const bins = Array.from(document.querySelectorAll('[data-testid^=bin-]'));
                  return bins.sort((left, right) =>
                    Number(right.querySelector('rect')?.getAttribute('height') || 0)
                    - Number(left.querySelector('rect')?.getAttribute('height') || 0)
                  )[0]?.getAttribute('data-testid') || null;
                }"""
            )
            if not populated_bin:
                raise AssertionError("Workbench real quality distribution has no populated bin")
            selected_bin = page.locator(f"[data-testid='{populated_bin}']")
            selected_bin.focus()
            with page.expect_response(
                lambda response: (
                    f"/api/projects/{encoded_project_id}/jobs/" in response.url
                    and "/drilldown?" in response.url
                ),
                timeout=60_000,
            ) as drill_response:
                page.keyboard.press("Enter")
            if drill_response.value.status != 200:
                raise AssertionError(
                    f"Workbench real quality drilldown returned HTTP {drill_response.value.status}"
                )
            drill_record = drill_response.value.json()
            if not isinstance(drill_record.get("rows"), list) or not drill_record["rows"]:
                raise AssertionError("Workbench largest recorded histogram bin returned no sample rows")
            drill_sample = page.locator("[data-testid=drill-sample]").first
            drill_sample.wait_for(state="visible", timeout=60_000)
            drill_sample.click()
            page.locator("[data-testid=sample-quality] dl").wait_for(state="visible", timeout=60_000)
            open_workflow_evidence("workbench-sample-run-evidence", "Workbench")
            open_workflow_evidence("workbench-run-audit-evidence", "Workbench")

            page.goto(f"http://127.0.0.1:{PREVIEW_PORT}/#/matchers", wait_until="networkidle")
            page.locator("[data-testid=comparison-mode-dataset]").click()
            page.locator("[data-testid=matrix-table] button.cell").first.wait_for(state="visible", timeout=60_000)
            page.locator("[data-testid=matrix-table] button.cell").first.click()
            page.locator("[data-testid=matcher-errors]").wait_for(state="visible", timeout=60_000)
            open_workflow_evidence("matcher-error-run-evidence", "Matchers")
            page.locator("[data-testid=matcher-errors] input").fill("0.9")
            page.locator("[data-testid=matcher-errors]").get_by_role("button", name="Recompute").click()
            page.locator("[data-testid=error-pair]").first.wait_for(state="visible", timeout=120_000)
            open_workflow_evidence("matcher-left-sample-evidence", "Matchers")
            open_workflow_evidence("matcher-right-sample-evidence", "Matchers")

            page.goto(f"http://127.0.0.1:{PREVIEW_PORT}/#/advanced", wait_until="networkidle")
            page.locator("[data-testid=ident-open]").first.wait_for(state="visible", timeout=60_000)
            page.locator("[data-testid=ident-open]").first.click()
            page.locator("[data-testid=ident-detail]").wait_for(state="visible", timeout=120_000)
            open_workflow_evidence("advanced-search-run-evidence", "Advanced")
            page.locator("[data-testid=degradation-open]").first.wait_for(state="visible", timeout=60_000)
            page.locator("[data-testid=degradation-open]").first.click()
            page.locator("[data-testid=degradation-detail]").wait_for(state="visible", timeout=120_000)
            open_workflow_evidence("advanced-degradation-run-evidence", "Advanced")

            page.goto(f"http://127.0.0.1:{PREVIEW_PORT}/#/cross", wait_until="networkidle")
            page.locator("[data-testid=cross-analysis-run-evidence]").first.wait_for(state="visible", timeout=60_000)
            open_workflow_evidence("cross-analysis-run-evidence", "CrossMatcher")
            page.locator("[data-testid=cross-analysis]").first.click()
            page.locator("[data-testid=contrast-row]").first.wait_for(state="visible", timeout=120_000)
            page.locator("[data-testid=contrast-row]").first.get_by_role("button").first.click()
            page.locator("[data-testid=cross-contrast-run-a-evidence]").wait_for(state="visible", timeout=60_000)
            open_workflow_evidence("cross-contrast-run-a-evidence", "CrossMatcher")
            open_workflow_evidence("cross-contrast-run-b-evidence", "CrossMatcher")
            open_workflow_evidence("cross-summary-run-evidence", "CrossMatcher")
            if page.locator("[data-testid=cross-pair]").count() == 0:
                for contrast_button in page.locator("[data-testid=contrast-row] button").all():
                    contrast_button.click()
                    page.wait_for_timeout(500)
                    if page.locator("[data-testid=cross-pair]").count() > 0:
                        break
            page.locator("[data-testid=cross-pair]").first.wait_for(state="visible", timeout=60_000)
            open_workflow_evidence("cross-left-quality-evidence", "CrossMatcher")
            open_workflow_evidence("cross-right-quality-evidence", "CrossMatcher")
            open_workflow_evidence("cross-pair-run-a-evidence", "CrossMatcher")
            open_workflow_evidence("cross-pair-run-b-evidence", "CrossMatcher")

            page.goto(f"http://127.0.0.1:{PREVIEW_PORT}/#/utility", wait_until="networkidle")
            page.locator("[data-testid=utility-table] tbody button.cell").first.wait_for(state="visible", timeout=60_000)
            page.locator("[data-testid=utility-table] tbody button.cell").first.click()
            page.locator("[data-testid=utility-detail]").wait_for(state="visible", timeout=120_000)
            open_workflow_evidence("utility-detail-run-evidence", "Utility")
            for filter_id in ("pairs-rejected", "pairs-prevented", "pairs-removed"):
                page.locator(f"[data-testid='{filter_id}']").click()
                page.locator("[data-testid=utility-pairs]").wait_for(state="visible", timeout=60_000)
                page.wait_for_timeout(400)
                if page.locator("[data-testid=utility-pair]").count() > 0:
                    break
            page.locator("[data-testid=utility-pair]").first.wait_for(state="visible", timeout=60_000)
            open_workflow_evidence("utility-left-quality-evidence", "Utility")
            open_workflow_evidence("utility-right-quality-evidence", "Utility")
            open_workflow_evidence("utility-pair-run-evidence", "Utility")

            page.goto(f"http://127.0.0.1:{PREVIEW_PORT}/#/reports", wait_until="networkidle")
            page.locator("[data-testid=figure-thumb]").first.wait_for(state="visible", timeout=60_000)
            page.locator("[data-testid=figure-thumb]").first.click()
            page.locator("[data-testid=figure-detail]").wait_for(state="visible", timeout=60_000)
            open_workflow_evidence("report-figure-provenance-evidence", "Reports")

            page.goto(f"http://127.0.0.1:{PREVIEW_PORT}/#/system", wait_until="networkidle")
            page.locator("[data-testid=system-panel]").wait_for(state="visible", timeout=60_000)
            page.get_by_role("tab", name="Runtime & storage", exact=True).click()
            page.locator("[data-testid=embedding-assets] [data-testid=system-embedding-run-evidence]").first.wait_for(state="visible", timeout=60_000)
            open_workflow_evidence("system-embedding-run-evidence", "SystemPanel")

            expected_workflow_sites = {
                test_id
                for test_ids in WORKFLOW_EVIDENCE_SITES.values()
                for test_id in test_ids
            }
            observed_workflow_sites = {item["test_id"] for item in workflow_evidence}
            if observed_workflow_sites != expected_workflow_sites:
                raise AssertionError(
                    f"structured Evidence workflow coverage differs: missing "
                    f"{sorted(expected_workflow_sites - observed_workflow_sites)}, extra "
                    f"{sorted(observed_workflow_sites - expected_workflow_sites)}"
                )

            observed["projects_contract"] = {
                "project_id": project_id,
                "project_name": project.get("name"),
                "run_id": selected_run_id,
                "source_href": run_href,
                "route_after_open": page.url,
                "observed_states": page.evaluate("window.__qualsightEvidenceStates"),
                "structured_sections": structured_sections,
                "raw_actions_open_by_default": raw_actions_open_by_default,
                "keyboard_focus_moved_to_detail": focus_moved_to_evidence,
                "outage_run_id": outage_run_id,
                "outage_console_errors": outage_console_errors,
                "retry_recovered": True,
                "stale_run_id": stale_run_id,
                "switched_to_project_id": other_project_id,
                "held_real_response_rejected_after_switch": True,
                "responsive_reflow": responsive_results,
                "keyboard_focus_returned_to_trigger": focus_returned_to_trigger,
            }
            observed["workflow_evidence_contract"] = {
                "expected_sites": len(expected_workflow_sites),
                "observed_sites": len(observed_workflow_sites),
                "sites": workflow_evidence,
                "comparison_sites": ["comparison-run-link", "comparison-status-link"],
                "total_primary_sites_across_real_gates": len(observed_workflow_sites) + 2,
            }
            if console_errors:
                raise AssertionError(f"browser console errors: {console_errors}")
            browser.close()

        observed["passed"] = True
        exit_code = 0
    except Exception as error:
        observed["failure"] = {
            "type": type(error).__name__,
            "detail": str(error),
            "traceback": traceback.format_exc(),
        }
    finally:
        api_returncode_before_cleanup = api_process.poll() if api_process is not None else None
        if "request_failures" in locals():
            observed["browser_request_failures"] = request_failures[-20:]
        if preview_process is not None:
            _stop(preview_process)
        if api_process is not None:
            _stop(api_process)
        observed["api_process_returncode_before_cleanup"] = api_returncode_before_cleanup
        observed["api_process_returncode_after_cleanup"] = (
            api_process.returncode if api_process is not None else None
        )
        observed["api_output"] = (
            api_process.stdout.read()[-4000:] if api_process is not None and api_process.stdout else ""
        )
        observed["preview_output"] = (
            preview_process.stdout.read()[-4000:] if preview_process is not None and preview_process.stdout else ""
        )
        observed["finished_at_unix"] = time.time()
        args.observation.write_text(json.dumps(observed, indent=2) + "\n", encoding="utf-8")
        shutil.rmtree(data_dir, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
