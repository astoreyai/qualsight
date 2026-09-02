"""Validate the owner-upload working-sample flow with real biometric input.

The gate starts the real local control plane and built console, records one
owner-supplied LFW image through the browser, and verifies that the returned
record becomes the inspector's working sample. It also verifies that the 28
recorded measures are reused without a second quality assessment and that both
SCRFD and OFIQ overlays execute against the recorded sample identifier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
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

from controlled_inputs import lfw_tree_root

ROOT = Path(__file__).resolve().parents[1]
API_PORT = 8480
PREVIEW_PORT = 5193
NODE_EXECUTABLE = shutil.which("node")
if NODE_EXECUTABLE is None:
    raise RuntimeError("the reviewed Node executable is not available on PATH")
NODE_BIN = str(Path(NODE_EXECUTABLE).parent)
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"
LFW_ROOT = lfw_tree_root(ROOT)
REAL_UPLOAD = LFW_ROOT / "Aaron_Peirsol/Aaron_Peirsol_0001.jpg"
REAL_FAILURE_UPLOAD = LFW_ROOT / "Abdoulaye_Wade/Abdoulaye_Wade_0003.jpg"


def _uv_binary() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv is unavailable on PATH")
    return executable
QUALITY_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("overall", ("UnifiedQualityScore",)),
    (
        "capture-illumination",
        (
            "BackgroundUniformity",
            "IlluminationUniformity",
            "LuminanceMean",
            "LuminanceVariance",
            "UnderExposurePrevention",
            "OverExposurePrevention",
            "DynamicRange",
            "Sharpness",
            "CompressionArtifacts",
            "NaturalColour",
        ),
    ),
    (
        "face-geometry-crop-pose",
        (
            "SingleFacePresent",
            "InterEyeDistance",
            "HeadSize",
            "LeftwardCropOfTheFaceImage",
            "RightwardCropOfTheFaceImage",
            "MarginAboveOfTheFaceImage",
            "MarginBelowOfTheFaceImage",
            "HeadPoseYaw",
            "HeadPosePitch",
            "HeadPoseRoll",
        ),
    ),
    (
        "visibility-occlusion-expression",
        (
            "EyesOpen",
            "MouthClosed",
            "EyesVisible",
            "MouthOcclusionPrevention",
            "FaceOcclusionPrevention",
            "ExpressionNeutrality",
            "NoHeadCoverings",
        ),
    ),
)
REAL_FAILURE_MEASURES = ("UnderExposurePrevention", "OverExposurePrevention")
CONTEXT_LABELS = (
    "Project",
    "Dataset",
    "Working sample",
    "Protocol",
    "Matcher leaf",
    "Evidence / Run",
)


def _assert_quality_results_frontend_contract() -> None:
    source = (ROOT / "frontend" / "src" / "QualityResults.tsx").read_text(
        encoding="utf-8"
    )
    required = {
        "Run evidence identity does not match the selected Run": (
            "run-envelope identity binding"
        ),
        "Recorded sample evidence identity does not match the selected sample": (
            "sample-envelope identity binding"
        ),
        "Configuration file identity": "path-free configuration evidence",
        "Recorded artifact identity": "path-free artifact evidence",
        "Repository identity": "path-free repository evidence",
    }
    missing = [description for marker, description in required.items() if marker not in source]
    assert not missing, f"QualityResults frontend contract is missing: {missing!r}"
    api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    registered_contract = api_source.split("export interface RegisteredSample {", 1)[1].split("\n}", 1)[0]
    if "registered_sample_id:" not in registered_contract or "path:" in registered_contract:
        raise AssertionError("RegisteredSample must expose only an opaque registered_sample_id")
    failure_contract = api_source.split("export interface AssessmentFailure {", 1)[1].split("\n}", 1)[0]
    required_failure_fields = (
        "registered_sample_id:",
        "exit_code:",
        "reason_code:",
        "message:",
        "failure_event_id:",
        "failure_evidence_sha256:",
    )
    if any(field not in failure_contract for field in required_failure_fields):
        raise AssertionError("AssessmentFailure omits its safe recorded failure identity")
    if any(field in failure_contract for field in ("stderr:", "image_path:", "reason:")):
        raise AssertionError("AssessmentFailure still exposes retired diagnostic fields")
    retired_path_markers = (
        "selected.path",
        "sample.path",
        "item.path",
        "?path=",
        "image_path",
    )
    leaks = [marker for marker in retired_path_markers if marker in app_source]
    if leaks:
        raise AssertionError(f"working-sample frontend still uses retired private path identity: {leaks!r}")
    for test_id in ("detection-error", "ofiq-landmark-error"):
        error_markup = re.search(
            rf'<p[^>]*role="alert"[^>]*data-testid="{test_id}"|'
            rf'<p[^>]*data-testid="{test_id}"[^>]*role="alert"',
            app_source,
        )
        if error_markup is None:
            raise AssertionError(f"{test_id} is not exposed as an assertive async failure")


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


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _json_request(url: str, *, method: str = "GET", body: dict | None = None) -> dict | list:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers={"content-type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - loopback only
        return json.load(response)


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def _context_snapshot(page) -> list[dict[str, str]]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll(
            '[data-testid=experiment-context] [data-context-key]'
        )).map((item) => ({
            key: item.getAttribute('data-context-key') || '',
            label: item.querySelector('.context-label')?.textContent?.trim() || '',
            value: item.querySelector('strong')?.textContent?.trim() || '',
            meta: item.querySelector('.context-meta')?.textContent?.trim() || '',
        }))"""
    )


def _assert_context_contract(page) -> list[dict[str, str]]:
    snapshot = _context_snapshot(page)
    labels = tuple(item["label"] for item in snapshot)
    if labels != CONTEXT_LABELS:
        raise AssertionError(
            "compact context header labels diverge from the six-field workflow contract: "
            f"observed={labels!r}, expected={CONTEXT_LABELS!r}"
        )
    if any(not item["value"] for item in snapshot):
        raise AssertionError(f"context header has an implicit blank state: {snapshot!r}")
    return snapshot


def _layout_snapshot(page, *, mode: str, width: int, height: int, equivalent_zoom: int) -> dict:
    page.set_viewport_size({"width": width, "height": height})
    page.wait_for_timeout(100)
    result = page.evaluate(
        """([mode, equivalentZoom]) => {
            const root = document.documentElement;
            const context = document.querySelector('[data-testid=experiment-context]');
            const rect = context?.getBoundingClientRect();
            return {
                mode,
                equivalent_zoom_percent: equivalentZoom,
                viewport_width: window.innerWidth,
                viewport_height: window.innerHeight,
                document_client_width: root.clientWidth,
                document_scroll_width: root.scrollWidth,
                document_scroll_height: root.scrollHeight,
                context_left: rect?.left ?? null,
                context_right: rect?.right ?? null,
                context_height: rect?.height ?? null,
                context_scroll_width: context?.scrollWidth ?? null,
                context_client_width: context?.clientWidth ?? null,
            };
        }""",
        [mode, equivalent_zoom],
    )
    if result["document_scroll_width"] > result["document_client_width"]:
        raise AssertionError(f"{mode} has horizontal document overflow: {result!r}")
    if result["context_scroll_width"] > result["context_client_width"]:
        raise AssertionError(f"{mode} context header overflows horizontally: {result!r}")
    if result["context_left"] < 0 or result["context_right"] > result["viewport_width"] + 1:
        raise AssertionError(f"{mode} context header leaves the viewport: {result!r}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    _assert_quality_results_frontend_contract()

    for real_input in (REAL_UPLOAD, REAL_FAILURE_UPLOAD):
        if not real_input.is_file():
            raise FileNotFoundError(f"registered real LFW input is unavailable: {real_input}")
    if not _port_is_free(API_PORT) or not _port_is_free(PREVIEW_PORT):
        raise RuntimeError(f"ports {API_PORT}/{PREVIEW_PORT} must be free")

    args.observation.parent.mkdir(parents=True, exist_ok=True)
    if args.screenshot is not None:
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)

    data_dir = Path(tempfile.mkdtemp(prefix="qualsight-upload-working-sample-"))
    environment = {
        **os.environ,
        "PATH": f"{NODE_BIN}:{os.environ.get('PATH', '')}",
        "QUALSIGHT_REPOSITORY_ROOT": str(ROOT),
        "QUALSIGHT_DATA_DIR": str(data_dir),
    }
    api = subprocess.Popen(
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
    )
    preview = subprocess.Popen(
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
    )

    observed: dict[str, object] = {
        "kind": "QUALSIGHT_UPLOAD_WORKING_SAMPLE_VALIDATION",
        "real_input": {
            "path": str(REAL_UPLOAD),
            "sha256": hashlib.sha256(REAL_UPLOAD.read_bytes()).hexdigest(),
            "size_bytes": REAL_UPLOAD.stat().st_size,
            "mtime_ns": REAL_UPLOAD.stat().st_mtime_ns,
        },
        "real_failure_input": {
            "path": str(REAL_FAILURE_UPLOAD),
            "sha256": hashlib.sha256(REAL_FAILURE_UPLOAD.read_bytes()).hexdigest(),
            "size_bytes": REAL_FAILURE_UPLOAD.stat().st_size,
            "mtime_ns": REAL_FAILURE_UPLOAD.stat().st_mtime_ns,
        },
        "passed": False,
    }
    exit_code = 1
    try:
        _wait_http(f"http://127.0.0.1:{API_PORT}/api/health", 90)
        _wait_http(f"http://127.0.0.1:{PREVIEW_PORT}/", 60)
        project = _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects",
            method="POST",
            body={
                "name": "Owner upload working-sample validation",
                "description": "Real browser validation of recorded upload evidence reuse.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        assert isinstance(project, dict)
        project_id = str(project["project_id"])
        _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/open",
            method="POST",
        )
        switch_project = _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects",
            method="POST",
            body={
                "name": "Context switch validation",
                "description": "Real browser validation of context invalidation.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        assert isinstance(switch_project, dict)
        switch_project_id = str(switch_project["project_id"])

        from playwright.sync_api import sync_playwright

        quality_posts: list[str] = []
        overlay_requests: list[str] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            page.add_init_script(
                "window.localStorage.setItem('qualsight.activeProjectId', "
                f"{json.dumps(project_id)})"
            )

            def record_request(request) -> None:
                if request.method == "POST" and "/api/quality/assess" in request.url:
                    quality_posts.append(request.url)
                request_path = urllib.parse.urlparse(request.url).path
                if request_path.startswith("/api/projects/") and request_path.endswith(
                    ("/detection", "/ofiq-landmarks")
                ):
                    overlay_requests.append(request.url)

            page.on("request", record_request)
            page.goto(
                f"http://127.0.0.1:{PREVIEW_PORT}/#/assess",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.wait_for_selector("[data-testid=upload-input]", timeout=30_000)

            context_before_upload = _assert_context_contract(page)
            responsive_context = [
                _layout_snapshot(page, mode="desktop", width=1440, height=1050, equivalent_zoom=100),
                _layout_snapshot(page, mode="tablet", width=1024, height=768, equivalent_zoom=100),
                _layout_snapshot(page, mode="narrow-mobile", width=390, height=844, equivalent_zoom=100),
                _layout_snapshot(page, mode="200%-reflow", width=720, height=525, equivalent_zoom=200),
                _layout_snapshot(page, mode="400%-reflow", width=360, height=263, equivalent_zoom=400),
            ]
            mobile_screenshot = None
            if args.screenshot is not None:
                page.set_viewport_size({"width": 390, "height": 844})
                mobile_screenshot = args.screenshot.with_name(
                    f"{args.screenshot.stem}-390{args.screenshot.suffix}"
                )
                page.screenshot(path=str(mobile_screenshot), full_page=False)
            page.set_viewport_size({"width": 1440, "height": 1050})

            page.keyboard.press("Tab")
            first_focus = page.evaluate(
                "() => ({text: document.activeElement?.textContent?.trim() || '', "
                "href: document.activeElement?.getAttribute('href') || ''})"
            )
            if first_focus != {"text": "Skip to main content", "href": "#main-content"}:
                raise AssertionError(f"keyboard navigation does not begin at the main-content skip link: {first_focus!r}")
            page.keyboard.press("Enter")
            if page.evaluate("() => document.activeElement?.id") != "main-content":
                raise AssertionError("skip link did not move keyboard focus to main content")
            visible_views_after_skip = page.evaluate(
                "() => Array.from(document.querySelectorAll('main > div[data-view]'))"
                ".filter(item => getComputedStyle(item).display !== 'none')"
                ".map(item => item.getAttribute('data-view'))"
            )
            if visible_views_after_skip != ["assess"]:
                raise AssertionError(
                    "skip link changed the route or hid the active work surface: "
                    f"{visible_views_after_skip!r}"
                )

            with page.expect_response(
                lambda response: "/api/quality/assess-upload" in response.url,
                timeout=300_000,
            ) as upload_response:
                page.set_input_files("[data-testid=upload-input]", str(REAL_UPLOAD))
            response = upload_response.value
            assert response.status == 201, f"owner upload returned HTTP {response.status}"
            page.wait_for_selector("[data-testid=upload-result]", timeout=30_000)

            datasets = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/datasets"
            )
            assert isinstance(datasets, list) and len(datasets) == 1, {
                "datasets": datasets,
                "quality_posts": quality_posts,
                "expected_project_id": project_id,
            }
            dataset_id = str(datasets[0]["dataset_id"])
            samples = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/"
                f"{urllib.parse.quote(project_id, safe='')}/datasets/"
                f"{urllib.parse.quote(dataset_id, safe='')}/samples"
            )
            assert isinstance(samples, list) and len(samples) == 1, samples
            recorded_sample = samples[0]
            projects = _json_request(f"http://127.0.0.1:{API_PORT}/api/projects")
            assert isinstance(projects, list)
            recorded_project = next(item for item in projects if item["project_id"] == project_id)
            assert len(recorded_project["run_ids"]) == 1, recorded_project["run_ids"]
            run_id = str(recorded_project["run_ids"][0])
            run = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/"
                f"{urllib.parse.quote(project_id, safe='')}/runs/{urllib.parse.quote(run_id, safe='')}"
            )
            assert isinstance(run, dict)

            context_after_upload = _assert_context_contract(page)
            context_values = {item["key"]: item["value"] for item in context_after_upload}
            expected_context_values = {
                "project": str(project["name"]),
                "dataset": "No dataset selected",
                "sample": REAL_UPLOAD.name,
                "protocol": "No protocol selected",
                "matcher": "No matcher leaf selected",
            }
            for key, expected in expected_context_values.items():
                if context_values.get(key) != expected:
                    raise AssertionError(
                        f"recorded upload context field {key!r} diverged: "
                        f"{context_values.get(key)!r} != {expected!r}"
                    )
            evidence_run_id = page.get_attribute("[data-context-key=evidence]", "data-run-id")
            if evidence_run_id != run_id or not context_values.get("evidence", "").startswith("Run "):
                raise AssertionError(
                    "recorded upload Run is absent or misbound in compact context: "
                    f"DOM identity={evidence_run_id!r}, API identity={run_id!r}, "
                    f"visible={context_after_upload!r}"
                )

            expected_image = str(recorded_sample["links"]["image"])
            inspector_image = page.get_attribute(".inspector img", "src")
            assert inspector_image == expected_image, (
                "uploaded photo did not become the working sample: "
                f"inspector src {inspector_image!r}, expected {expected_image!r}"
            )
            page.wait_for_function(
                "() => { const image = document.querySelector('.inspector img'); "
                "return !!image && image.complete && image.naturalWidth > 0 && image.naturalHeight > 0; }",
                timeout=30_000,
            )

            measure_names = page.evaluate(
                "() => Array.from(document.querySelectorAll('[data-testid=measure-table] tbody th'))"
                ".map(cell => cell.textContent?.trim())"
            )
            assert len(measure_names) == 28, measure_names
            assert "UnifiedQualityScore" in measure_names, measure_names

            assess_button = page.locator("[data-testid=assess-button]")
            assert assess_button.is_disabled(), "recorded upload exposed a duplicate quality action"
            assert "already recorded" in assess_button.inner_text().lower()

            sample_route = expected_image.rsplit("/image", 1)[0]
            detection_path = f"{sample_route}/detection"
            with page.expect_response(
                lambda response: urllib.parse.unquote(
                    urllib.parse.urlparse(response.url).path
                )
                == detection_path,
                timeout=120_000,
            ) as detection_response:
                page.click("[data-testid=detection-toggle]")
            assert detection_response.value.status == 200, (
                f"SCRFD returned HTTP {detection_response.value.status}: "
                f"{detection_response.value.text()}"
            )
            page.wait_for_selector("[data-testid=detection-overlay]", state="attached", timeout=30_000)
            scrfd_points = page.evaluate(
                "() => document.querySelectorAll('[data-testid=detection-overlay] circle').length"
            )
            assert scrfd_points == 5, f"expected 5 SCRFD landmarks, observed {scrfd_points}"

            landmark_path = f"{sample_route}/ofiq-landmarks"
            with page.expect_response(
                lambda response: urllib.parse.unquote(
                    urllib.parse.urlparse(response.url).path
                )
                == landmark_path,
                timeout=180_000,
            ) as landmark_response:
                page.click("[data-testid=ofiq-landmark-toggle]")
            assert landmark_response.value.status == 200, (
                f"OFIQ landmarks returned HTTP {landmark_response.value.status}: "
                f"{landmark_response.value.text()}"
            )
            page.wait_for_selector(
                "[data-testid=ofiq-landmark-overlay]", state="attached", timeout=30_000
            )
            ofiq_points = page.evaluate(
                "() => document.querySelectorAll('[data-testid=ofiq-landmark-overlay] circle').length"
            )
            assert ofiq_points == 98, f"expected 98 OFIQ landmarks, observed {ofiq_points}"

            image_response = page.request.get(
                f"http://127.0.0.1:{PREVIEW_PORT}{expected_image}"
            )
            assert image_response.ok
            assert image_response.headers["x-qualsight-sha256"] == recorded_sample["sha256"]
            assert len(quality_posts) == 1, quality_posts
            assert "/api/quality/assess-upload" in quality_posts[0], quality_posts
            upload_query = urllib.parse.parse_qs(
                urllib.parse.urlparse(quality_posts[0]).query
            )
            observed_scope = upload_query.get("purpose_scope")
            assert observed_scope == [RESEARCH_SCOPE], (
                "owner upload used the selected registered sample's scope instead of the "
                f"admitted owner-input scope: observed {observed_scope!r}, "
                f"expected {[RESEARCH_SCOPE]!r}"
            )

            b40_failures: list[str] = []

            def b40_require(condition: bool, message: str) -> None:
                if not condition:
                    b40_failures.append(message)

            run_record = run["run"]
            jobs = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/jobs"
            )
            assert isinstance(jobs, list)
            job = next(item for item in jobs if item["run_id"] == run_id)
            measure_catalog = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/jobs/"
                f"{job['job_id']}/quality-measures"
            )
            assert isinstance(measure_catalog, dict)
            job_results = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/jobs/"
                f"{job['job_id']}/results"
            )
            assert isinstance(job_results, dict) and job_results["total"] == 1
            recorded_row = job_results["rows"][0]
            expected_measure_names = [
                name for _, names in QUALITY_GROUPS for name in names
            ]
            assert len(measure_catalog["measures"]) == 28, measure_catalog
            assert set(measure_catalog["measures"]) == set(expected_measure_names), measure_catalog
            metrics = {
                f"{name}.{component}": recorded_row[f"{name}.{component}"]
                for name in expected_measure_names
                for component in ("native", "scalar")
            }

            rendered_groups = page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-testid=quality-group]')).map(group => ({
                    id: group.getAttribute('data-quality-group'),
                    measures: Array.from(group.querySelectorAll('[data-testid=quality-scalar-row]')).map(row => row.getAttribute('data-measure')),
                }))"""
            )
            expected_groups = [
                {"id": group_id, "measures": list(names)}
                for group_id, names in QUALITY_GROUPS
            ]
            b40_require(
                rendered_groups == expected_groups,
                f"grouped scalar profile missing or divergent: {rendered_groups!r} != {expected_groups!r}",
            )

            scalar_rows = page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-testid=quality-scalar-row]')).map(row => ({
                    measure: row.getAttribute('data-measure'),
                    scalar: row.getAttribute('data-scalar'),
                    status: row.getAttribute('data-status'),
                }))"""
            )
            b40_require(
                [row["measure"] for row in scalar_rows] == expected_measure_names,
                f"scalar view did not expose all 28 recorded measures once: {scalar_rows!r}",
            )
            progressbars = page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-testid=quality-scalar-row] [role=progressbar]')).map(bar => ({
                    minimum: bar.getAttribute('aria-valuemin'),
                    maximum: bar.getAttribute('aria-valuemax'),
                    value: bar.getAttribute('aria-valuenow'),
                    measure: bar.closest('[data-measure]')?.getAttribute('data-measure'),
                }))"""
            )
            b40_require(
                len(progressbars) == 28
                and all(bar["minimum"] == "0" and bar["maximum"] == "100" for bar in progressbars),
                f"scalar view lacked 28 accessible 0-100 bars: {progressbars!r}",
            )
            if len(progressbars) == 28:
                for bar in progressbars:
                    expected_value = metrics[f"{bar['measure']}.scalar"]
                    b40_require(
                        float(bar["value"]) == float(expected_value),
                        f"{bar['measure']} scalar bar {bar['value']} != recorded {expected_value}",
                    )
            fill_styles = page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-testid=quality-scalar-fill]')).map(fill => {
                    const style = getComputedStyle(fill);
                    return `${style.backgroundColor}|${style.backgroundImage}`;
                })"""
            )
            b40_require(
                len(fill_styles) == 28 and len(set(fill_styles)) == 1,
                f"scalar bars were missing or used score-dependent colors: {fill_styles!r}",
            )
            policy_note = page.inner_text("[data-testid=quality-policy-note]") if page.query_selector("[data-testid=quality-policy-note]") else ""
            b40_require(
                "no pass/fail" in policy_note.lower() and "policy" in policy_note.lower(),
                f"non-policy scalar semantics were not explicit: {policy_note!r}",
            )

            native_button = page.query_selector("[data-testid=quality-view-native]")
            b40_require(native_button is not None, "Native values view control is missing")
            if native_button is not None:
                native_button.focus()
                page.keyboard.press("Enter")
                native_rows = page.evaluate(
                    """() => Array.from(document.querySelectorAll('[data-testid=quality-native-row]')).map(row => ({
                        measure: row.getAttribute('data-measure'),
                        native: row.getAttribute('data-native'),
                        unit: row.getAttribute('data-native-unit'),
                        semantics: row.querySelector('.quality-native-semantics')?.textContent?.trim() ?? '',
                        status: row.getAttribute('data-status'),
                    }))"""
                )
                b40_require(
                    [row["measure"] for row in native_rows] == expected_measure_names,
                    f"native view did not preserve all 28 recorded measures: {native_rows!r}",
                )
                if len(native_rows) == 28:
                    for row in native_rows:
                        expected_value = metrics[f"{row['measure']}.native"]
                        b40_require(
                            float(row["native"]) == float(expected_value),
                            f"{row['measure']} native value {row['native']} != recorded {expected_value}",
                        )
                        b40_require(
                            bool(row["unit"]) and bool(row["semantics"]),
                            f"{row['measure']} native unit or semantics is absent: {row!r}",
                        )
                    native_units = {row["measure"]: row["unit"] for row in native_rows}
                    b40_require(
                        native_units.get("InterEyeDistance") == "pixel"
                        and native_units.get("HeadPoseYaw") == "degree"
                        and native_units.get("DynamicRange") == "bit",
                        f"authoritative OFIQ native units are not rendered: {native_units!r}",
                    )
                native_progressbars = page.evaluate(
                    "() => document.querySelectorAll('[data-testid=quality-native-view] [role=progressbar]').length"
                )
                b40_require(
                    native_progressbars == 0,
                    f"heterogeneous native values were plotted on a shared axis ({native_progressbars} bars)",
                )
                native_note = page.inner_text("[data-testid=quality-native-note]") if page.query_selector("[data-testid=quality-native-note]") else ""
                b40_require(
                    "not" in native_note.lower() and "shared" in native_note.lower(),
                    f"native-scale limitation was not explicit: {native_note!r}",
                )

            panel_geometry = page.evaluate(
                """() => {
                    const panel = document.querySelector('.results');
                    const heading = document.querySelector('[data-testid=quality-status-heading]');
                    const panelRect = panel?.getBoundingClientRect();
                    const headingRect = heading?.getBoundingClientRect();
                    return {
                        panelClientWidth: panel?.clientWidth ?? -1,
                        panelScrollWidth: panel?.scrollWidth ?? -1,
                        panelRight: panelRect?.right ?? -1,
                        headingRight: headingRect?.right ?? -1,
                        headingClientWidth: heading?.clientWidth ?? -1,
                        headingScrollWidth: heading?.scrollWidth ?? -1,
                    };
                }"""
            )
            b40_require(
                panel_geometry["panelScrollWidth"] <= panel_geometry["panelClientWidth"] + 1
                and 0 < panel_geometry["headingRight"] <= panel_geometry["panelRight"]
                and panel_geometry["headingScrollWidth"] <= panel_geometry["headingClientWidth"],
                f"Status remained clipped at 1500px: {panel_geometry!r}",
            )

            failure_button = page.query_selector("[data-testid=quality-view-failures]")
            b40_require(failure_button is not None, "Failures view control is missing")
            if failure_button is not None:
                failure_button.focus()
                page.keyboard.press("Enter")
                empty_failure_text = page.inner_text("[data-testid=quality-failure-empty]") if page.query_selector("[data-testid=quality-failure-empty]") else ""
                b40_require(
                    "0 failed measures" in empty_failure_text.lower(),
                    f"zero-failure state was not explicit for the Aaron Peirsol input: {empty_failure_text!r}",
                )

            structured = page.query_selector("[data-testid=structured-evidence]")
            b40_require(structured is not None, "structured Run evidence is not the primary in-app view")
            if structured is not None:
                try:
                    page.wait_for_function(
                        "() => ['ready', 'error'].includes(document.querySelector('[data-testid=structured-evidence]')?.getAttribute('data-state') || '')",
                        timeout=60_000,
                    )
                except Exception as error:
                    b40_failures.append(f"structured Run evidence did not load: {error}")
                evidence_state = page.get_attribute("[data-testid=structured-evidence]", "data-state")
                evidence_text = page.inner_text("[data-testid=structured-evidence]")
                b40_require(
                    evidence_state == "ready",
                    f"structured Run evidence entered {evidence_state!r}: {evidence_text!r}",
                )
                evidence_disclosures = page.evaluate(
                    """() => Array.from(document.querySelectorAll(
                        '[data-testid=structured-evidence] details[data-testid^=evidence-]'
                    )).map((item) => ({
                        testid: item.getAttribute('data-testid'),
                        open: item.hasAttribute('open'),
                        summary: item.querySelector('summary')?.textContent?.trim() ?? '',
                    }))"""
                )
                expected_disclosures = {
                    "evidence-configuration",
                    "evidence-models",
                    "evidence-artifacts",
                    "evidence-warnings",
                    "evidence-limits",
                    "evidence-repository",
                    "evidence-audit",
                }
                b40_require(
                    {item["testid"] for item in evidence_disclosures} == expected_disclosures
                    and all(not item["open"] and item["summary"] for item in evidence_disclosures),
                    "routine quality review did not keep complete provenance in seven named, "
                    f"keyboard-expandable closed disclosures: {evidence_disclosures!r}",
                )
                page.evaluate(
                    """() => document.querySelectorAll(
                        '[data-testid=structured-evidence] details[data-testid^=evidence-]'
                    ).forEach((item) => { item.open = true; })"""
                )
                evidence_text = page.inner_text("[data-testid=structured-evidence]")
                required_evidence = {
                    "project": run_record["project_id"],
                    "dataset": dataset_id,
                    "sample": str(recorded_sample["sample_id"]),
                    "engine": run_record["quality_engine"]["resource_id"],
                    "configuration": run_record["quality_engine_configuration"]["sha256"],
                    "timing": str(run_record["assessment_ms"]),
                    "repository": run_record["repository"]["status_sha256"],
                }
                for label, value in required_evidence.items():
                    b40_require(
                        value in evidence_text,
                        f"structured evidence omitted recorded {label} value {value}",
                    )
                for section in (
                    "project",
                    "dataset",
                    "sample",
                    "engine",
                    "configuration",
                    "models",
                    "timing",
                    "artifacts",
                    "warnings",
                    "limits",
                    "repository",
                    "audit",
                ):
                    b40_require(
                        page.query_selector(f"[data-testid=evidence-{section}]") is not None,
                        f"structured evidence section {section!r} is missing",
                    )
                artifact_text = page.inner_text("[data-testid=evidence-artifacts]") if page.query_selector("[data-testid=evidence-artifacts]") else ""
                for artifact in run_record["artifacts"]:
                    for artifact_value in (artifact["kind"], artifact["sha256"]):
                        b40_require(
                            artifact_value in artifact_text,
                            f"structured evidence omitted recorded artifact value {artifact_value}",
                        )
                public_run_text = json.dumps(run, sort_keys=True)
                for private_value in (str(ROOT), str(data_dir)):
                    b40_require(
                        private_value not in public_run_text and private_value not in evidence_text,
                        f"public Run or primary structured evidence exposed {private_value!r}",
                    )

                chart_download = page.query_selector(
                    "[data-testid=quality-chart-data-download]"
                )
                b40_require(
                    chart_download is not None,
                    "downloadable quality chart data is missing",
                )
                if chart_download is not None:
                    chart_payload = page.evaluate(
                        """async () => {
                            const link = document.querySelector('[data-testid=quality-chart-data-download]');
                            const response = await fetch(link.href);
                            return await response.json();
                        }"""
                    )
                    b40_require(
                        chart_payload.get("schema_version")
                        == "QUALSIGHT_QUALITY_CHART_DATA_V1",
                        f"quality chart data schema is not versioned: {chart_payload!r}",
                    )
                    expected_chart_context = {
                        "project_id": run_record["project_id"],
                        "dataset_id": dataset_id,
                        "sample_id": str(recorded_sample["sample_id"]),
                        "run_id": run_id,
                    }
                    b40_require(
                        chart_payload.get("context") == expected_chart_context,
                        "quality chart data does not bind the active recorded context: "
                        f"{chart_payload.get('context')!r} != {expected_chart_context!r}",
                    )
                    chart_measures = chart_payload.get("measures", [])
                    b40_require(
                        len(chart_measures) == 28
                        and [item.get("metric_id") for item in chart_measures]
                        == [f"{name}.scalar" for name in expected_measure_names],
                        f"quality chart data lacks the 28 exact metric identifiers: {chart_measures!r}",
                    )
                    if len(chart_measures) == 28:
                        for item in chart_measures:
                            b40_require(
                                item.get("value") == metrics[item["metric_id"]]
                                and item.get("unit") == "OFIQ scalar [0,100]"
                                and item.get("source_run_id") == run_id,
                                "quality chart datum is not bound to its recorded value, unit, "
                                f"and source Run: {item!r}",
                            )

            primary_raw_run = page.query_selector(
                "[data-testid=upload-result] a[href^='/api/projects/'][href*='/runs/']"
            )
            b40_require(
                primary_raw_run is None,
                "upload summary still makes public Run JSON a primary action",
            )
            raw_actions = page.query_selector("[data-testid=raw-evidence-actions]")
            b40_require(raw_actions is not None, "secondary public Run JSON actions are missing")
            if raw_actions is not None:
                raw_state = page.evaluate(
                    """() => {
                        const structured = document.querySelector('[data-testid=structured-evidence]');
                        const raw = document.querySelector('[data-testid=raw-evidence-actions]');
                        return {
                            open: raw?.hasAttribute('open') ?? true,
                            afterStructured: !!(structured && raw && (structured.compareDocumentPosition(raw) & Node.DOCUMENT_POSITION_FOLLOWING)),
                            viewHref: raw?.querySelector('[data-testid=raw-json-view]')?.getAttribute('href') ?? '',
                            downloadName: raw?.querySelector('[data-testid=raw-json-download]')?.getAttribute('download') ?? '',
                            viewText: raw?.querySelector('[data-testid=raw-json-view]')?.textContent?.trim() ?? '',
                            downloadText: raw?.querySelector('[data-testid=raw-json-download]')?.textContent?.trim() ?? '',
                        };
                    }"""
                )
                b40_require(
                    not raw_state["open"]
                    and raw_state["afterStructured"]
                    and urllib.parse.unquote(raw_state["viewHref"])
                    == f"/api/projects/{project_id}/runs/{run_id}"
                    and raw_state["downloadName"].endswith(".json")
                    and raw_state["viewText"] == "View public Run JSON"
                    and raw_state["downloadText"] == "Download public Run JSON",
                    f"public Run JSON actions were not secondary, collapsed, viewable, and downloadable: {raw_state!r}",
                )

            with page.expect_response(
                lambda response: "/api/quality/assess-upload" in response.url,
                timeout=300_000,
            ) as failure_upload_response:
                page.set_input_files(
                    "[data-testid=upload-input]", str(REAL_FAILURE_UPLOAD)
                )
            assert failure_upload_response.value.status == 201, (
                f"real failure input upload returned HTTP {failure_upload_response.value.status}"
            )
            page.wait_for_function(
                "name => (document.querySelector('[data-testid=upload-result]')?.textContent || '').includes(name)",
                arg=REAL_FAILURE_UPLOAD.name,
                timeout=30_000,
            )
            datasets_after_failure = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/datasets"
            )
            assert isinstance(datasets_after_failure, list) and len(datasets_after_failure) == 2
            failure_sha = hashlib.sha256(REAL_FAILURE_UPLOAD.read_bytes()).hexdigest()
            failure_sample: dict | None = None
            for dataset in datasets_after_failure:
                candidates = _json_request(
                    f"http://127.0.0.1:{API_PORT}/api/projects/"
                    f"{urllib.parse.quote(project_id, safe='')}/datasets/"
                    f"{urllib.parse.quote(str(dataset['dataset_id']), safe='')}/samples"
                )
                assert isinstance(candidates, list)
                failure_sample = next(
                    (item for item in candidates if item["sha256"] == failure_sha),
                    failure_sample,
                )
            assert failure_sample is not None
            expected_failure_image = str(failure_sample["links"]["image"])
            page.wait_for_function(
                "expected => document.querySelector('[data-testid=working-sample-image]')?.getAttribute('src') === expected",
                arg=expected_failure_image,
                timeout=30_000,
            )
            projects_after_failure = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects"
            )
            assert isinstance(projects_after_failure, list)
            project_after_failure = next(
                item for item in projects_after_failure if item["project_id"] == project_id
            )
            assert len(project_after_failure["run_ids"]) == 2, project_after_failure["run_ids"]
            assert len(quality_posts) == 2, quality_posts

            failure_view_button = page.query_selector("[data-testid=quality-view-failures]")
            b40_require(
                failure_view_button is not None,
                "Failures view is missing for the documented real failure input",
            )
            failure_rows: list[dict] = []
            if failure_view_button is not None:
                failure_view_button.focus()
                page.keyboard.press("Enter")
                failure_rows = page.evaluate(
                    """() => Array.from(document.querySelectorAll('[data-testid=quality-failure-row]')).map(row => ({
                        measure: row.getAttribute('data-measure'),
                        text: row.textContent || '',
                        native: row.getAttribute('data-native'),
                        scalar: row.getAttribute('data-scalar'),
                    }))"""
                )
                b40_require(
                    [row["measure"] for row in failure_rows] == list(REAL_FAILURE_MEASURES),
                    f"real failure view did not list exactly {REAL_FAILURE_MEASURES!r}: {failure_rows!r}",
                )
                for row in failure_rows:
                    expected_reason = (
                        "OFIQ returned no finite native value or scalar quality component "
                        f"for {row['measure']}."
                    )
                    b40_require(
                        row["native"] == "unavailable"
                        and row["scalar"] == "unavailable"
                        and expected_reason in row["text"]
                        and "0" not in row["text"],
                        f"failed measure substituted a number or diverged from the recorded reason: {row!r}",
                    )

            if b40_failures:
                observed["b40_failures"] = b40_failures
                raise AssertionError(
                    "B40 RED assertions failed:\n- " + "\n- ".join(b40_failures)
                )

            if args.screenshot is not None:
                page.screenshot(path=str(args.screenshot), full_page=True)

            page.evaluate("() => { window.location.hash = '#/projects'; }")
            page.wait_for_selector("[data-testid=project-select]", state="visible", timeout=30_000)
            page.select_option("[data-testid=project-select]", switch_project_id)
            page.wait_for_function(
                "expected => document.querySelector('[data-context-key=project] strong')?.textContent?.trim() === expected",
                arg=str(switch_project["name"]),
                timeout=30_000,
            )
            stale_cleared_context = _assert_context_contract(page)
            stale_values = {item["key"]: item["value"] for item in stale_cleared_context}
            stale_text = " ".join(item["value"] + " " + item["meta"] for item in stale_cleared_context)
            if stale_values.get("dataset") != "No dataset selected":
                raise AssertionError(f"project switch retained stale dataset context: {stale_cleared_context!r}")
            if stale_values.get("protocol") != "No protocol selected" or stale_values.get("matcher") != "No matcher leaf selected":
                raise AssertionError(f"project switch retained stale matcher context: {stale_cleared_context!r}")
            if REAL_UPLOAD.name in stale_text or page.get_attribute("[data-context-key=evidence]", "data-run-id") == run_id:
                raise AssertionError(f"project switch exposed the prior upload or Run: {stale_cleared_context!r}")
            observed.update(
                {
                    "project_id": project_id,
                    "run_id": run_id,
                    "sample_id": recorded_sample["sample_id"],
                    "image_sha256": recorded_sample["sha256"],
                    "measure_count": len(measure_names),
                    "scrfd_landmarks": scrfd_points,
                    "ofiq_landmarks": ofiq_points,
                    "quality_posts": quality_posts,
                    "overlay_requests": overlay_requests,
                    "quality_groups": expected_groups,
                    "failure_measures": [row["measure"] for row in failure_rows],
                    "context_before_upload": context_before_upload,
                    "context_after_upload": context_after_upload,
                    "context_after_project_switch": stale_cleared_context,
                    "responsive_context": responsive_context,
                    "first_keyboard_focus": first_focus,
                    "mobile_screenshot": str(mobile_screenshot) if mobile_screenshot else None,
                    "passed": True,
                }
            )
            browser.close()
        exit_code = 0
    except Exception as error:  # the observation records the exact red or green outcome
        observed["failure"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exc(),
        }
    finally:
        _stop(preview)
        _stop(api)
        if exit_code != 0:
            observed["server_output"] = {
                "api": api.stdout.read().decode("utf-8", errors="replace")[-12_000:]
                if api.stdout is not None
                else "",
                "preview": preview.stdout.read().decode("utf-8", errors="replace")[-4_000:]
                if preview.stdout is not None
                else "",
            }
        shutil.rmtree(data_dir, ignore_errors=True)
        args.observation.write_text(json.dumps(observed, indent=2) + "\n", encoding="utf-8")

    if exit_code == 0:
        print(json.dumps(observed, indent=2))
    else:
        print(json.dumps(observed["failure"], indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
