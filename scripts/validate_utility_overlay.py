"""Validate explicit ERC overlays against real recorded LFW utility analyses."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
API_PORT = 8480
PREVIEW_PORT = 5200
NODE_EXECUTABLE = shutil.which("node")
if NODE_EXECUTABLE is None:
    raise RuntimeError("the reviewed Node executable is not available on PATH")
NODE_BIN = str(Path(NODE_EXECUTABLE).parent)
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
MATCHERS = (
    "adaface-ir101-local-candidate",
    "elasticface-arc-r100-local-candidate",
)
LEAVES = ("QS-C22.AS", "QS-C22.AN", "QS-C22.ES", "QS-C22.EN")


def _uv_binary() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv is unavailable on PATH")
    return executable


def _assert_frontend_job_privacy_contract() -> None:
    api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    home_source = (ROOT / "frontend" / "src" / "Home.tsx").read_text(encoding="utf-8")
    offenders = []
    if "parquet_path" in api_source:
        offenders.append("frontend/src/api.ts")
    if "parquet_path" in home_source:
        offenders.append("frontend/src/Home.tsx")
    if offenders:
        raise AssertionError(
            "public quality-job workflow still depends on private parquet_path: "
            + ", ".join(offenders)
        )


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _wait_http(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - loopback only
                if response.status < 500:
                    return
        except (urllib.error.URLError, ConnectionError, socket.timeout) as error:
            last_error = error
        time.sleep(0.25)
    raise RuntimeError(f"{url} did not become ready: {last_error}")


def _json(url: str, *, method: str = "GET", body: dict | None = None) -> dict | list:
    data = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers={"content-type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(request, timeout=90) as response:  # noqa: S310 - loopback only
        return json.load(response)


def _wait_matrix(project_id: str, matrix_id: str) -> dict:
    deadline = time.monotonic() + 360
    latest: dict = {}
    while time.monotonic() < deadline:
        value = _json(
            f"http://127.0.0.1:{API_PORT}/api/projects/{urllib.parse.quote(project_id, safe='')}"
            f"/matrices/{urllib.parse.quote(matrix_id, safe='')}"
        )
        assert isinstance(value, dict)
        latest = value
        if value["progress"]["phase"] in {"completed", "failed"}:
            return value
        time.sleep(0.5)
    raise AssertionError(f"matcher matrix did not terminate: {latest}")


def _wait_job(project_id: str, job_id: str) -> dict:
    deadline = time.monotonic() + 360
    latest: dict = {}
    while time.monotonic() < deadline:
        value = _json(
            f"http://127.0.0.1:{API_PORT}/api/projects/"
            f"{urllib.parse.quote(project_id, safe='')}/jobs/"
            f"{urllib.parse.quote(job_id, safe='')}"
        )
        assert isinstance(value, dict)
        latest = value
        if value["state"] in {"COMPLETED", "FAILED", "CANCELLED"} and value.get("ended_at"):
            return value
        time.sleep(0.5)
    raise AssertionError(f"quality job did not terminate: {latest}")


def _wait_analysis(project_id: str, analysis_id: str) -> dict:
    deadline = time.monotonic() + 180
    latest: dict = {}
    while time.monotonic() < deadline:
        value = _json(
            f"http://127.0.0.1:{API_PORT}/api/projects/{urllib.parse.quote(project_id, safe='')}"
            f"/utility-analyses/{urllib.parse.quote(analysis_id, safe='')}"
        )
        assert isinstance(value, dict)
        latest = value
        if value["state"] in {"COMPLETED", "FAILED", "INTERRUPTED"}:
            return value
        time.sleep(0.2)
    raise AssertionError(f"utility analysis did not terminate: {latest}")


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    _assert_frontend_job_privacy_contract()
    if not _port_is_free(API_PORT) or not _port_is_free(PREVIEW_PORT):
        raise RuntimeError(f"ports {API_PORT}/{PREVIEW_PORT} must be free")
    args.observation.parent.mkdir(parents=True, exist_ok=True)
    if args.screenshot is not None:
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)

    data_dir = Path(tempfile.mkdtemp(prefix="qualsight-utility-overlay-"))
    environment = {
        **os.environ,
        "PATH": f"{NODE_BIN}:{os.environ.get('PATH', '')}",
        "QUALSIGHT_REPOSITORY_ROOT": str(ROOT),
        "QUALSIGHT_DATA_DIR": str(data_dir),
    }
    api = subprocess.Popen(
        [
            _uv_binary(), "run", "--frozen", "--offline", "--directory", "backend",
            "python", "-m", "uvicorn", "qualsight.api.app:create_app", "--factory",
            "--host", "127.0.0.1", "--port", str(API_PORT), "--log-level", "warning",
        ],
        cwd=ROOT,
        env={**environment, "PYTHONPATH": "src:.."},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    preview = subprocess.Popen(
        [
            "pnpm", "--filter", "@qualsight/frontend", "exec", "vite", "preview",
            "--host", "127.0.0.1", "--port", str(PREVIEW_PORT), "--strictPort",
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    observation: dict[str, object] = {
        "kind": "QUALSIGHT_UTILITY_OVERLAY_REAL_VALIDATION",
        "data_dir": str(data_dir),
        "metric_registry": {
            "path": str(ROOT / "metrics" / "metric-registry.json"),
            "sha256": hashlib.sha256((ROOT / "metrics" / "metric-registry.json").read_bytes()).hexdigest(),
        },
        "passed": False,
    }
    exit_code = 1
    try:
        _wait_http(f"http://127.0.0.1:{API_PORT}/api/health", 90)
        _wait_http(f"http://127.0.0.1:{PREVIEW_PORT}/", 60)
        project = _json(
            f"http://127.0.0.1:{API_PORT}/api/projects",
            method="POST",
            body={
                "name": "Utility overlay real browser gate",
                "description": "Explicit compatible ERC overlays over the recorded LFW fixture pairs.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        assert isinstance(project, dict)
        project_id = str(project["project_id"])
        _json(f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/open", method="POST")
        dataset = _json(
            f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/datasets/import-registered",
            method="POST",
            body={"resource_id": LFW_RESOURCE_ID, "verify_hashes": False},
        )
        assert isinstance(dataset, dict)
        dataset_id = str(dataset["dataset_id"])
        protocol = _json(
            f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/datasets/{dataset_id}/protocols/freeze-lfw-view2",
            method="POST",
        )
        assert isinstance(protocol, dict)
        protocol_id = str(protocol["protocol_id"])
        for matcher_id in MATCHERS:
            pulled = _json(
                f"http://127.0.0.1:{API_PORT}/api/matcher-assets/{matcher_id}/pull-sources",
                method="POST",
            )
            assert isinstance(pulled, dict)

        matrix_query = urllib.parse.urlencode(
            {"fixture_pairs_only": "true", "leaves": ",".join(LEAVES)}
        )
        started_matrix = _json(
            f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/protocols/{protocol_id}/matrix?{matrix_query}",
            method="POST",
        )
        assert isinstance(started_matrix, dict)
        matrix = _wait_matrix(project_id, str(started_matrix["matrix_id"]))
        assert matrix["progress"]["phase"] == "completed", matrix
        leaves = {item["leaf_id"]: item for item in matrix["leaves"]}
        assert set(leaves) == set(LEAVES), leaves

        started_job = _json(
            f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/datasets/{dataset_id}/quality-batch",
            method="POST",
            body={"selection": {"kind": "subset_fixture"}, "chunks": 1},
        )
        assert isinstance(started_job, dict)
        quality_job = _wait_job(project_id, str(started_job["job_id"]))
        assert quality_job["state"] == "COMPLETED", quality_job
        assert "parquet_path" not in quality_job, quality_job
        quality_results = _json(
            f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/jobs/{quality_job['job_id']}/results?limit=1"
        )
        assert isinstance(quality_results, dict), quality_results
        assert quality_results["total"] > 0 and len(quality_results["rows"]) == 1, quality_results
        quality_sha256 = str(quality_results["parquet_sha256"])
        assert len(quality_sha256) == 64 and all(character in "0123456789abcdef" for character in quality_sha256), quality_sha256

        def execute(leaf_id: str, measure: str = "UnifiedQualityScore") -> dict:
            started = _json(
                f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/utility-analyses",
                method="POST",
                body={
                    "matcher_run_id": leaves[leaf_id]["run_id"],
                    "quality_job_id": quality_job["job_id"],
                    "quality_measure": measure,
                    "pair_quality_policy": "pair.quality.min",
                    "matcher_threshold": 0.8,
                    "quality_threshold": 40.0,
                    "bootstrap_replicates": 0,
                    "seed": 29,
                },
            )
            assert isinstance(started, dict)
            completed = _wait_analysis(project_id, str(started["analysis_id"]))
            assert completed["state"] == "COMPLETED", completed
            return completed

        completed = {
            "baseline": execute("QS-C22.AS"),
            "matcher": execute("QS-C22.ES"),
            "preprocessing": execute("QS-C22.AN"),
            "composite": execute("QS-C22.EN"),
            "measure": execute("QS-C22.AS", "Sharpness"),
        }
        summaries = {name: value["summary"] for name, value in completed.items()}
        baseline_identity = summaries["baseline"]["overlay_identity"]
        assert completed["baseline"]["utility"]["overlay_identity"] == baseline_identity
        assert baseline_identity["project_id"] == project_id
        assert baseline_identity["dataset_id"] == dataset_id
        assert baseline_identity["protocol_id"] == protocol_id
        assert baseline_identity["quality_parquet_sha256"] == quality_sha256
        assert baseline_identity["metric_registry"]["definition_sha256"] == observation["metric_registry"]["sha256"]
        assert baseline_identity["pair_universe_sha256"] == summaries["matcher"]["overlay_identity"]["pair_universe_sha256"]
        assert baseline_identity["comparisons_sha256"] != summaries["matcher"]["overlay_identity"]["comparisons_sha256"]

        from playwright.sync_api import sync_playwright

        detail_requests: list[str] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            page.add_init_script(
                "window.localStorage.setItem('qualsight.activeProjectId', "
                f"{json.dumps(project_id)})"
            )
            page.on(
                "response",
                lambda response: detail_requests.append(response.url)
                if f"/api/projects/{urllib.parse.quote(project_id, safe='')}/utility-analyses/analysis%3A" in response.url
                else None,
            )
            page.goto(
                f"http://127.0.0.1:{PREVIEW_PORT}/#/data",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.wait_for_function(
                "datasetId => document.querySelector('[data-context-key=dataset]')?.getAttribute('data-dataset-id') === datasetId",
                arg=dataset_id,
                timeout=60_000,
            )
            page.evaluate("window.location.hash = '#/matchers'")
            page.wait_for_selector("[data-testid=comparison-mode-dataset]", timeout=60_000)
            page.click("[data-testid=comparison-mode-dataset]")
            page.wait_for_selector("[data-testid='leaf-QS-C22.AS']", timeout=60_000)
            page.click("[data-testid='leaf-QS-C22.AS']")
            page.wait_for_function(
                "() => document.querySelector('[data-context-key=matcher]')?.getAttribute('data-leaf-id') "
                "&& document.querySelector('[data-context-key=evidence]')?.getAttribute('data-run-id')",
                timeout=60_000,
            )
            matcher_context_before_utility = page.evaluate(
                """() => ({
                  protocolId: document.querySelector('[data-context-key=protocol]')?.getAttribute('data-protocol-id'),
                  leafId: document.querySelector('[data-context-key=matcher]')?.getAttribute('data-leaf-id'),
                  runId: document.querySelector('[data-context-key=evidence]')?.getAttribute('data-run-id'),
                })"""
            )
            page.evaluate("window.location.hash = '#/utility'")
            page.wait_for_function(
                "count => document.querySelectorAll('[data-testid=utility-overlay-candidate]').length === count",
                arg=len(summaries),
                timeout=30_000,
            )

            def candidate(name: str):
                analysis_id = summaries[name]["analysis_id"]
                return page.locator(
                    f'[data-testid="utility-overlay-candidate"][data-analysis-id="{analysis_id}"]'
                )

            slow_analysis_id = summaries["matcher"]["analysis_id"]
            baseline_analysis_id = summaries["baseline"]["analysis_id"]
            page.evaluate(
                """
                ({ projectId, slowId }) => {
                  const originalFetch = window.fetch.bind(window);
                  const slowPath = `/api/projects/${encodeURIComponent(projectId)}/utility-analyses/${encodeURIComponent(slowId)}`;
                  window.__utilityOriginalFetch = originalFetch;
                  window.__utilityHeldDetailReady = false;
                  window.__utilityReleaseDetail = null;
                  window.fetch = (input, init) => {
                    const raw = typeof input === 'string' ? input : input.url;
                    const url = new URL(raw, window.location.href);
                    const actual = originalFetch(input, init);
                    if (url.pathname !== slowPath || url.search) return actual;
                    return actual.then(response => new Promise(resolve => {
                      window.__utilityHeldDetailReady = true;
                      window.__utilityReleaseDetail = () => resolve(response);
                    }));
                  };
                }
                """,
                {"projectId": project_id, "slowId": slow_analysis_id},
            )
            slow_digest = slow_analysis_id.split("sha256:", 1)[-1][:10]
            baseline_digest = baseline_analysis_id.split("sha256:", 1)[-1][:10]
            utility_table = page.get_by_test_id("utility-table")
            utility_table.get_by_role("button", name=f"{slow_digest}…", exact=True).click()
            page.wait_for_function("window.__utilityHeldDetailReady === true")
            utility_table.get_by_role("button", name=f"{baseline_digest}…", exact=True).click()
            page.wait_for_function(
                "digest => (document.querySelector('[data-testid=utility-detail] h3')?.textContent || '').includes(digest)",
                arg=baseline_analysis_id.split("sha256:", 1)[-1][:14],
                timeout=30_000,
            )
            page.evaluate("window.__utilityReleaseDetail()")
            page.wait_for_timeout(300)
            detail_text_after_release = page.locator("[data-testid=utility-detail] h3").inner_text()
            assert baseline_analysis_id.split("sha256:", 1)[-1][:14] in detail_text_after_release, (
                "late same-project detail response replaced the newer selection",
                detail_text_after_release,
            )
            page.evaluate(
                """
                window.fetch = window.__utilityOriginalFetch;
                delete window.__utilityOriginalFetch;
                delete window.__utilityHeldDetailReady;
                delete window.__utilityReleaseDetail;
                """
            )

            # A repeated Run returns an already-recorded analysis. Hold that
            # real detail response, open a different recorded analysis, then
            # release it: the later operator selection must remain authoritative.
            page.select_option("[data-testid=utility-leaf]", leaves["QS-C22.AS"]["run_id"])
            page.select_option("[data-testid=utility-job]", str(quality_job["job_id"]))
            page.wait_for_function(
                "() => !document.querySelector('[data-testid=utility-measure]')?.disabled",
                timeout=30_000,
            )
            page.select_option("[data-testid=utility-measure]", "UnifiedQualityScore")
            page.select_option("[data-testid=utility-policy]", "pair.quality.min")
            page.fill("[data-testid=utility-fmr]", "0.001")
            page.fill("[data-testid=utility-q]", "40")
            page.fill("[data-testid=utility-replicates]", "0")
            page.click("[data-testid=utility-run]")
            page.wait_for_function(
                "() => { const button = document.querySelector('[data-testid=utility-run]'); "
                "return button && !button.disabled && document.querySelector('[data-testid=utility-detail]'); }",
                timeout=180_000,
            )
            page.evaluate(
                """
                () => {
                  const originalFetch = window.fetch.bind(window);
                  window.__utilityOriginalFetch = originalFetch;
                  window.__utilityStartedAnalysisId = null;
                  window.__utilityHeldStartedDetailReady = false;
                  window.__utilityReleaseStartedDetail = null;
                  window.fetch = async (input, init) => {
                    const raw = typeof input === 'string' ? input : input.url;
                    const url = new URL(raw, window.location.href);
                    const method = (init?.method || (typeof input === 'string' ? 'GET' : input.method) || 'GET').toUpperCase();
                    const response = await originalFetch(input, init);
                    if (method === 'POST' && /^\/api\/projects\/[^/]+\/utility-analyses$/.test(url.pathname)) {
                      const body = await response.clone().json();
                      if (body.already_recorded !== true) throw new Error('expected a real already-recorded utility response');
                      window.__utilityStartedAnalysisId = body.analysis_id;
                      return response;
                    }
                    const startedPath = window.__utilityStartedAnalysisId
                      ? `/api/projects/${encodeURIComponent(%s)}/utility-analyses/${encodeURIComponent(window.__utilityStartedAnalysisId)}`
                      : null;
                    if (method !== 'GET' || url.pathname !== startedPath || url.search) return response;
                    return new Promise(resolve => {
                      window.__utilityHeldStartedDetailReady = true;
                      window.__utilityReleaseStartedDetail = () => resolve(response);
                    });
                  };
                }
                """ % json.dumps(project_id)
            )
            page.click("[data-testid=utility-run]")
            page.wait_for_function("window.__utilityHeldStartedDetailReady === true", timeout=30_000)
            composite_analysis_id = summaries["composite"]["analysis_id"]
            composite_digest = composite_analysis_id.split("sha256:", 1)[-1][:10]
            utility_table.get_by_role("button", name=f"{composite_digest}…", exact=True).click()
            page.wait_for_function(
                "digest => (document.querySelector('[data-testid=utility-detail] h3')?.textContent || '').includes(digest)",
                arg=composite_analysis_id.split("sha256:", 1)[-1][:14],
                timeout=30_000,
            )
            page.evaluate("window.__utilityReleaseStartedDetail()")
            page.wait_for_timeout(300)
            detail_text_after_started_release = page.locator("[data-testid=utility-detail] h3").inner_text()
            assert composite_analysis_id.split("sha256:", 1)[-1][:14] in detail_text_after_started_release, (
                "late already-recorded start detail replaced the newer operator selection",
                detail_text_after_started_release,
            )
            page.evaluate(
                """
                window.fetch = window.__utilityOriginalFetch;
                delete window.__utilityOriginalFetch;
                delete window.__utilityStartedAnalysisId;
                delete window.__utilityHeldStartedDetailReady;
                delete window.__utilityReleaseStartedDetail;
                """
            )

            composite_summary = summaries["composite"]
            page.wait_for_function(
                "expected => { const protocol = document.querySelector('[data-context-key=protocol]'); "
                "const matcher = document.querySelector('[data-context-key=matcher]'); "
                "const evidence = document.querySelector('[data-context-key=evidence]'); "
                "return protocol?.getAttribute('data-protocol-id') === expected.protocolId "
                "&& matcher?.getAttribute('data-leaf-id') === expected.leafId "
                "&& evidence?.getAttribute('data-run-id') === expected.runId; }",
                arg={
                    "protocolId": composite_summary["overlay_identity"]["protocol_id"],
                    "leafId": composite_summary["leaf_id"],
                    "runId": composite_summary["run_id"],
                },
                timeout=30_000,
            )
            coherent_utility_context = page.evaluate(
                """() => ({
                  protocolId: document.querySelector('[data-context-key=protocol]')?.getAttribute('data-protocol-id'),
                  leafId: document.querySelector('[data-context-key=matcher]')?.getAttribute('data-leaf-id'),
                  runId: document.querySelector('[data-context-key=evidence]')?.getAttribute('data-run-id'),
                })"""
            )
            utility_table.get_by_role("button", name=f"{baseline_digest}…", exact=True).click()
            page.wait_for_function(
                "digest => (document.querySelector('[data-testid=utility-detail] h3')?.textContent || '').includes(digest)",
                arg=baseline_analysis_id.split("sha256:", 1)[-1][:14],
                timeout=30_000,
            )

            expected_prevented = page.evaluate(
                """
                async ({ projectId, analysisId }) => {
                  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/utility-analyses/${encodeURIComponent(analysisId)}/pairs?retained=false&decision_error=true&limit=60`);
                  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
                  return response.json();
                }
                """,
                {"projectId": project_id, "analysisId": baseline_analysis_id},
            )
            page.evaluate(
                """
                ({ projectId, analysisId }) => {
                  const originalFetch = window.fetch.bind(window);
                  const heldPath = `/api/projects/${encodeURIComponent(projectId)}/utility-analyses/${encodeURIComponent(analysisId)}/pairs`;
                  window.__utilityOriginalFetch = originalFetch;
                  window.__utilityHeldPairsReady = false;
                  window.__utilityReleasePairs = null;
                  window.fetch = (input, init) => {
                    const raw = typeof input === 'string' ? input : input.url;
                    const url = new URL(raw, window.location.href);
                    const actual = originalFetch(input, init);
                    if (url.pathname !== heldPath || url.searchParams.get('decision_error') !== null) return actual;
                    return actual.then(response => new Promise(resolve => {
                      window.__utilityHeldPairsReady = true;
                      window.__utilityReleasePairs = () => resolve(response);
                    }));
                  };
                }
                """,
                {"projectId": project_id, "analysisId": baseline_analysis_id},
            )
            page.click("[data-testid=pairs-rejected]")
            page.wait_for_function("window.__utilityHeldPairsReady === true")
            page.click("[data-testid=pairs-prevented]")
            page.wait_for_selector("[data-testid=utility-pairs]")
            page.wait_for_function(
                "() => (document.querySelector('[data-testid=utility-pairs] h4')?.textContent || '').includes('Errors prevented')"
            )

            def rendered_pair_ids() -> list[str]:
                return page.locator("[data-testid=utility-pair] .pair-facts > span.mono:first-child").evaluate_all(
                    "items => items.map(item => (item.textContent || '').split('·', 1)[0].trim())"
                )

            expected_prevented_ids = [row["pair_id"] for row in expected_prevented["rows"]]
            assert rendered_pair_ids() == expected_prevented_ids
            page.evaluate("window.__utilityReleasePairs()")
            page.wait_for_timeout(300)
            assert rendered_pair_ids() == expected_prevented_ids, (
                "late same-project pair response replaced the newer filter",
                rendered_pair_ids(),
                expected_prevented_ids,
            )
            page.evaluate(
                """
                window.fetch = window.__utilityOriginalFetch;
                delete window.__utilityOriginalFetch;
                delete window.__utilityHeldPairsReady;
                delete window.__utilityReleasePairs;
                """
            )

            baseline_input = candidate("baseline")
            page.wait_for_function(
                "() => document.querySelectorAll('[data-testid=erc-plot] polyline').length === 1",
                timeout=30_000,
            )
            assert baseline_input.is_checked() and baseline_input.is_disabled()

            matcher_input = candidate("matcher")
            preprocessing_input = candidate("preprocessing")
            composite_input = candidate("composite")
            measure_input = candidate("measure")
            assert matcher_input.get_attribute("data-eligible") == "true"
            assert matcher_input.get_attribute("data-comparison-axis") == "MATCHER"
            assert preprocessing_input.get_attribute("data-eligible") == "false"
            assert "PREPROCESSING_AXIS_NOT_SELECTED" in (preprocessing_input.get_attribute("data-reasons") or "")
            assert "QUALITY_MEASURE_MISMATCH" in (measure_input.get_attribute("data-reasons") or "")
            assert preprocessing_input.is_disabled() and measure_input.is_disabled()

            matcher_input.check()
            page.wait_for_function(
                "() => document.querySelectorAll('[data-testid=erc-plot] polyline').length === 2",
                timeout=30_000,
            )
            page.select_option("[data-testid=utility-overlay-axis]", "COMPOSITE")
            assert not matcher_input.is_checked()
            assert composite_input.get_attribute("data-eligible") == "true"
            assert composite_input.get_attribute("data-comparison-axis") == "COMPOSITE"
            composite_input.focus()
            page.keyboard.press("Space")
            page.wait_for_function(
                "() => document.querySelectorAll('[data-testid=erc-plot] polyline').length === 2",
                timeout=30_000,
            )
            assert "does not isolate matcher from preprocessing" in page.locator("[data-testid=utility-overlay-claim]").inner_text()

            desktop_layout = page.evaluate(
                """() => {
                  const plot = document.querySelector('[data-testid=erc-plot]')?.getBoundingClientRect();
                  const candidates = document.querySelector('[data-testid=utility-overlay-candidates]')?.getBoundingClientRect();
                  return {
                    viewport: window.innerWidth,
                    document_client: document.documentElement.clientWidth,
                    document_scroll: document.documentElement.scrollWidth,
                    plot_left: plot?.left ?? null,
                    plot_right: plot?.right ?? null,
                    plot_width: plot?.width ?? null,
                    candidates_left: candidates?.left ?? null,
                    candidates_right: candidates?.right ?? null,
                  };
                }"""
            )
            assert desktop_layout["document_scroll"] <= desktop_layout["document_client"], desktop_layout
            assert desktop_layout["plot_width"] is not None and desktop_layout["plot_width"] >= 250, desktop_layout
            assert desktop_layout["plot_left"] >= 0 and desktop_layout["plot_right"] <= desktop_layout["viewport"] + 1, desktop_layout
            assert desktop_layout["candidates_left"] >= 0 and desktop_layout["candidates_right"] <= desktop_layout["viewport"] + 1, desktop_layout

            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(100)
            overflow = page.evaluate(
                "() => ({client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth})"
            )
            assert overflow["scroll"] <= overflow["client"], overflow
            page.set_viewport_size({"width": 1440, "height": 1050})
            if args.screenshot is not None:
                page.screenshot(path=str(args.screenshot), full_page=True)
            browser.close()

        requested_ids = {
            urllib.parse.unquote(url.rsplit("/", 1)[-1])
            for url in detail_requests
        }
        assert summaries["baseline"]["analysis_id"] in requested_ids
        assert summaries["composite"]["analysis_id"] in requested_ids
        assert summaries["preprocessing"]["analysis_id"] not in requested_ids
        assert summaries["measure"]["analysis_id"] not in requested_ids

        purged = _json(
            f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/purge",
            method="POST",
            body={"confirm": project_id},
        )
        assert isinstance(purged, dict) and purged["verified"] is True, purged
        observation.update(
            {
                "passed": True,
                "project_id": project_id,
                "dataset_id": dataset_id,
                "protocol_id": protocol_id,
                "pair_universe_sha256": baseline_identity["pair_universe_sha256"],
                "quality_parquet_sha256": quality_sha256,
                "comparison_sha256": {
                    name: summary["overlay_identity"]["comparisons_sha256"]
                    for name, summary in summaries.items()
                },
                "analysis_ids": {
                    name: summary["analysis_id"] for name, summary in summaries.items()
                },
                "browser_detail_requests": sorted(requested_ids),
                "desktop_layout": desktop_layout,
                "mobile_overflow": overflow,
                "matcher_context_before_utility": matcher_context_before_utility,
                "coherent_utility_context": coherent_utility_context,
                "purge_id": purged["purge_id"],
            }
        )
        exit_code = 0
    except Exception as error:  # noqa: BLE001 - gate must serialize exact failure
        observation["failure"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exc(),
        }
    finally:
        _stop(preview)
        _stop(api)
        shutil.rmtree(data_dir, ignore_errors=True)
        args.observation.write_text(
            json.dumps(observation, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(observation, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
