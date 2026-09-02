"""Validate Stress Studio with one rights-recorded real LFW working sample."""

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

from controlled_inputs import lfw_tree_root

ROOT = Path(__file__).resolve().parents[1]
API_PORT = 8480
PREVIEW_PORT = 5193
NODE_EXECUTABLE = shutil.which("node")
if NODE_EXECUTABLE is None:
    raise RuntimeError("the reviewed Node executable is not available on PATH")
NODE_BIN = str(Path(NODE_EXECUTABLE).parent)
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"
REAL_UPLOAD = lfw_tree_root(ROOT) / "Aaron_Peirsol/Aaron_Peirsol_0001.jpg"
REAL_RELATIVE_PATH = "Aaron_Peirsol/Aaron_Peirsol_0001.jpg"
IMAGE_MANIFEST = ROOT / "resources" / "manifests" / "lfw-funneled-images-v1.jsonl"
PARAMETERS: dict[str, int | float] = {
    "jpeg": 20,
    "gaussian_blur": 5.0,
    "downscale": 0.25,
    "gamma": 0.3,
    "contrast": 0.3,
    "rotation": 45.0,
    "occlusion": 0.35,
}


def _uv_binary() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv is unavailable on PATH")
    return executable


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
    data = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers={"content-type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - loopback only
        return json.load(response)


def _sample_api_url(project_id: str, sample_id: str, resource: str) -> str:
    if resource not in {"detection", "ofiq-landmarks"}:
        raise ValueError(f"unsupported sample resource: {resource}")
    return (
        f"http://127.0.0.1:{API_PORT}/api/projects/"
        f"{urllib.parse.quote(project_id, safe='')}/samples/"
        f"{urllib.parse.quote(sample_id, safe='')}/{resource}"
    )


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def _manifest_record() -> dict:
    for line in IMAGE_MANIFEST.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record["path"] == REAL_RELATIVE_PATH:
            return record
    raise RuntimeError(f"real input is absent from the registered manifest: {REAL_RELATIVE_PATH}")


def _operator_contract_accepts(contract: dict, value: int | float) -> bool:
    if contract["value_type"] == "integer" and type(value) is not int:
        return False
    lower = value > contract["minimum"] if contract["minimum_exclusive"] else value >= contract["minimum"]
    upper = value < contract["maximum"] if contract["maximum_exclusive"] else value <= contract["maximum"]
    return lower and upper


def _wait_job(project_id: str, job_id: str, timeout: float = 360.0) -> dict:
    deadline = time.monotonic() + timeout
    current: dict = {}
    while time.monotonic() < deadline:
        value = _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects/"
            f"{urllib.parse.quote(project_id, safe='')}/jobs/"
            f"{urllib.parse.quote(job_id, safe='')}"
        )
        assert isinstance(value, dict)
        current = value
        if value["state"] in {"COMPLETED", "FAILED", "CANCELLED"} and value.get("ended_at"):
            return value
        time.sleep(0.5)
    raise AssertionError(f"quality job did not terminate: {current}")


def _layout_snapshot(page, *, mode: str, width: int, height: int, equivalent_zoom: int) -> dict:
    page.set_viewport_size({"width": width, "height": height})
    page.wait_for_timeout(100)
    result = page.evaluate(
        """([mode, equivalentZoom]) => {
            const root = document.documentElement;
            const studio = document.querySelector('[data-testid=stress-studio]');
            const rect = studio?.getBoundingClientRect();
            const smallTargets = Array.from(studio?.querySelectorAll('button, input, select, a') || [])
              .filter((item) => {
                const box = item.getBoundingClientRect();
                return box.width > 0 && box.height > 0 && (box.width < 24 || box.height < 24);
              }).length;
            return {
                mode,
                equivalent_zoom_percent: equivalentZoom,
                viewport_width: window.innerWidth,
                viewport_height: window.innerHeight,
                document_client_width: root.clientWidth,
                document_scroll_width: root.scrollWidth,
                studio_left: rect?.left ?? null,
                studio_right: rect?.right ?? null,
                studio_scroll_width: studio?.scrollWidth ?? null,
                studio_client_width: studio?.clientWidth ?? null,
                undersized_controls: smallTargets,
            };
        }""",
        [mode, equivalent_zoom],
    )
    if result["document_scroll_width"] > result["document_client_width"]:
        raise AssertionError(f"{mode} has horizontal document overflow: {result!r}")
    if result["studio_scroll_width"] > result["studio_client_width"]:
        raise AssertionError(f"{mode} Stress Studio overflows horizontally: {result!r}")
    if result["studio_left"] < 0 or result["studio_right"] > result["viewport_width"] + 1:
        raise AssertionError(f"{mode} Stress Studio leaves the viewport: {result!r}")
    if result["undersized_controls"]:
        raise AssertionError(f"{mode} has undersized Stress Studio controls: {result!r}")
    return result


def _chart_points(page, series: str) -> list[dict]:
    return page.evaluate(
        """series => Array.from(document.querySelectorAll(
            `[data-testid=stress-trajectory-point][data-series="${series}"]`
        )).map((point) => ({
            sample_id: point.getAttribute('data-sample-id'),
            operator: point.getAttribute('data-operator'),
            parameter: point.getAttribute('data-parameter'),
            scalar: point.getAttribute('data-scalar'),
            native: point.getAttribute('data-native'),
            status: point.getAttribute('data-status'),
            failure_reason: point.getAttribute('data-failure-reason'),
        }))""",
        series,
    )


def _geometry_overlay(page, side: str) -> dict:
    return page.evaluate(
        """side => {
            const root = document.querySelector(`[data-testid=stress-geometry-side][data-side="${side}"]`);
            const scrfd = root?.querySelector('[data-testid=stress-scrfd-overlay]');
            const ofiq = root?.querySelector('[data-testid=stress-ofiq-overlay]');
            const numbers = (items, names) => Array.from(items).map(item =>
              Object.fromEntries(names.map(name => [name, Number(item.getAttribute(name))]))
            );
            return {
              sample_id: root?.getAttribute('data-sample-id'),
              scrfd_label: root?.querySelector('[data-testid=stress-scrfd-label]')?.textContent?.trim(),
              ofiq_label: root?.querySelector('[data-testid=stress-ofiq-label]')?.textContent?.trim(),
              scrfd_boxes: numbers(scrfd?.querySelectorAll('rect') || [], ['x', 'y', 'width', 'height']),
              scrfd_points: numbers(scrfd?.querySelectorAll('circle') || [], ['cx', 'cy']),
              ofiq_faces: numbers(ofiq?.querySelectorAll('rect') || [], ['x', 'y', 'width', 'height']),
              ofiq_points: numbers(ofiq?.querySelectorAll('circle') || [], ['cx', 'cy']),
              scrfd_stroke: scrfd?.querySelector('circle, rect')
                ? getComputedStyle(scrfd.querySelector('circle, rect')).stroke : null,
              ofiq_stroke: ofiq?.querySelector('circle, rect')
                ? getComputedStyle(ofiq.querySelector('circle, rect')).stroke : null,
            };
        }""",
        side,
    )


def _expected_geometry(detection: dict, ofiq: dict) -> dict:
    return {
        "scrfd_label": detection["label"],
        "ofiq_label": f"{ofiq['label']} ({ofiq['landmark_type']})",
        "scrfd_boxes": [
            {
                "x": item["bbox"][0],
                "y": item["bbox"][1],
                "width": item["bbox"][2] - item["bbox"][0],
                "height": item["bbox"][3] - item["bbox"][1],
            }
            for item in detection["detections"]
        ],
        "scrfd_points": [
            {"cx": point[0], "cy": point[1]}
            for item in detection["detections"]
            for point in item["landmarks"]
        ],
        "ofiq_faces": [
            {"x": face["x"], "y": face["y"], "width": face["width"], "height": face["height"]}
            for face in ofiq["faces"]
        ],
        "ofiq_points": [{"cx": point[0], "cy": point[1]} for point in ofiq["landmarks"]],
    }


def _expected_point(row: dict, measure: str, operator: str) -> dict:
    failed = bool(row[f"{measure}.failed"])
    scalar = row[f"{measure}.scalar"]
    native = row[f"{measure}.native"]
    return {
        "sample_id": row["sample_id"],
        "operator": operator,
        "scalar": "unavailable" if scalar is None else str(scalar),
        "native": "unavailable" if native is None else str(native),
        "status": "failed" if failed else "measured",
    }


def _assert_point_matches_recorded(point: dict, expected: dict) -> None:
    for key, value in expected.items():
        if key in {"scalar", "native"} and value != "unavailable":
            assert point[key] != "unavailable", (point, expected)
            assert float(point[key]) == float(value), (point, expected)
        else:
            assert point[key] == value, (point, expected)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    if not REAL_UPLOAD.is_file():
        raise FileNotFoundError(f"registered real LFW input is unavailable: {REAL_UPLOAD}")
    manifest_record = _manifest_record()
    source_sha256 = hashlib.sha256(REAL_UPLOAD.read_bytes()).hexdigest()
    if source_sha256 != manifest_record["sha256"] or REAL_UPLOAD.stat().st_size != manifest_record["size_bytes"]:
        raise RuntimeError("registered real LFW input drifted from its recorded manifest identity")
    if not _port_is_free(API_PORT) or not _port_is_free(PREVIEW_PORT):
        raise RuntimeError(f"ports {API_PORT}/{PREVIEW_PORT} must be free")

    args.observation.parent.mkdir(parents=True, exist_ok=True)
    if args.screenshot is not None:
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="qualsight-stress-studio-"))
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
    observed: dict[str, object] = {
        "kind": "QUALSIGHT_STRESS_STUDIO_REAL_VALIDATION",
        "real_input": {
            "path": str(REAL_UPLOAD),
            "manifest_path": str(IMAGE_MANIFEST),
            "sha256": source_sha256,
            "size_bytes": REAL_UPLOAD.stat().st_size,
            "mtime_ns": REAL_UPLOAD.stat().st_mtime_ns,
        },
        "data_dir": str(data_dir),
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
                "name": "Stress Studio real browser gate",
                "description": "One rights-recorded real image through all admitted controlled transformations.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        assert isinstance(project, dict)
        project_id = str(project["project_id"])
        _json_request(f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/open", method="POST")

        catalog = _json_request(f"http://127.0.0.1:{API_PORT}/api/transforms")
        assert isinstance(catalog, dict)
        operators = catalog["operators"]
        assert [item["kind"] for item in operators] == list(PARAMETERS), operators
        assert all(_operator_contract_accepts(item["parameter_contract"], PARAMETERS[item["kind"]]) for item in operators)

        from playwright.sync_api import sync_playwright

        transformation_requests: list[dict] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            page.add_init_script(
                "window.localStorage.setItem('qualsight.activeProjectId', "
                f"{json.dumps(project_id)})"
            )

            def record_request(request) -> None:
                if request.method == "POST" and request.url.endswith("/transformations"):
                    transformation_requests.append(request.post_data_json)

            page.on("request", record_request)
            page.goto(f"http://127.0.0.1:{PREVIEW_PORT}/#/assess", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("[data-testid=upload-input]", timeout=30_000)
            with page.expect_response(lambda response: "/api/quality/assess-upload" in response.url, timeout=300_000) as upload_response:
                page.set_input_files("[data-testid=upload-input]", str(REAL_UPLOAD))
            assert upload_response.value.status == 201, upload_response.value.status
            page.wait_for_selector("[data-testid=upload-result]", timeout=30_000)

            datasets = _json_request(f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/datasets")
            assert isinstance(datasets, list) and len(datasets) == 1, datasets
            dataset_id = str(datasets[0]["dataset_id"])
            samples = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/"
                f"{urllib.parse.quote(project_id, safe='')}/datasets/"
                f"{urllib.parse.quote(dataset_id, safe='')}/samples"
            )
            assert isinstance(samples, list) and len(samples) == 1, samples
            sample = samples[0]
            sample_id = str(sample["sample_id"])
            assert sample["dataset_id"] == dataset_id and sample["sha256"] == source_sha256
            original_jobs = _json_request(f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/jobs")
            assert isinstance(original_jobs, list) and len(original_jobs) == 1, original_jobs
            original_job = original_jobs[0]
            original_results = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/jobs/"
                f"{original_job['job_id']}/results"
            )
            assert isinstance(original_results, dict) and original_results["total"] == 1
            original_row = original_results["rows"][0]
            expected_original_image = str(sample["links"]["image"])
            assert page.get_attribute(".inspector img", "src") == expected_original_image
            original_response = page.request.get(f"http://127.0.0.1:{PREVIEW_PORT}{expected_original_image}")
            assert original_response.ok
            assert hashlib.sha256(original_response.body()).hexdigest() == source_sha256
            assert original_response.headers["x-qualsight-sha256"] == source_sha256

            page.click('[data-testid=primary-nav] [data-testid=nav-link][data-view="advanced"]')
            page.wait_for_function("() => window.location.hash === '#/advanced'")
            page.wait_for_selector(
                '[data-view="advanced"] [data-testid=stress-studio]',
                state="visible",
                timeout=10_000,
            )
            bound_sample_id = page.get_attribute(
                '[data-view="advanced"] [data-testid=stress-studio]',
                "data-source-sample-id",
            )
            assert bound_sample_id == sample_id, (bound_sample_id, sample_id)

            page.click('[data-testid=primary-nav] [data-testid=nav-link][data-view="assess"]')
            page.wait_for_selector("[data-testid=stress-open]", state="visible", timeout=10_000)
            page.click("[data-testid=stress-open]")
            page.wait_for_selector(
                '[data-view="assess"] [data-testid=stress-studio]',
                state="visible",
                timeout=30_000,
            )
            assert page.get_attribute(
                '[data-view="assess"] [data-testid=stress-studio]',
                "data-source-sample-id",
            ) == sample_id
            page.click("[data-testid=stress-open]")
            page.click('[data-testid=primary-nav] [data-testid=nav-link][data-view="advanced"]')
            page.wait_for_selector(
                '[data-view="advanced"] [data-testid=stress-studio]',
                state="visible",
                timeout=30_000,
            )

            operator_rows = page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-testid=stress-operator]')).map(row => ({
                    kind: row.getAttribute('data-operator'),
                    parameter: row.getAttribute('data-parameter-name'),
                    version: row.getAttribute('data-version'),
                    contract: JSON.parse(row.getAttribute('data-parameter-contract') || '{}'),
                }))"""
            )
            assert operator_rows == [
                {
                    "kind": item["kind"],
                    "parameter": item["parameter"],
                    "version": item["version"],
                    "contract": item["parameter_contract"],
                }
                for item in operators
            ], operator_rows
            for kind, parameter in PARAMETERS.items():
                toggle = page.locator(f"[data-testid=stress-operator-toggle-{kind}]")
                if not toggle.is_checked():
                    toggle.check()
                page.fill(f"[data-testid=stress-parameter-{kind}]", str(parameter))

            page.locator("[data-testid=stress-run]").focus()
            assert page.evaluate("() => document.activeElement?.getAttribute('data-testid')") == "stress-run"
            with page.expect_response(lambda response: response.url.endswith("/transformations"), timeout=120_000) as created_response:
                page.keyboard.press("Enter")
            assert created_response.value.status == 201, created_response.value.text()
            derived = created_response.value.json()
            assert transformation_requests == [
                {
                    "selection": "sample_ids",
                    "sample_ids": [sample_id],
                    "transforms": [
                        {"kind": kind, "parameters": [parameter]}
                        for kind, parameter in PARAMETERS.items()
                    ],
                }
            ], transformation_requests
            assert derived["source_dataset_id"] == dataset_id
            assert derived["source_count"] == 1 and derived["levels"] == 7 and derived["derived_count"] == 7
            detail = _json_request(
                f"http://127.0.0.1:{API_PORT}{derived['links']['detail']}"
            )
            assert isinstance(detail, dict) and len(detail["samples"]) == 7
            derived_rows = detail["samples"]
            rows_by_sample = {row["derived_sample_id"]: row for row in derived_rows}

            page.wait_for_function(
                "() => document.querySelectorAll('[data-testid=stress-gallery-item]').length === 8",
                timeout=60_000,
            )
            gallery = page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-testid=stress-gallery-item]')).map(item => ({
                    kind: item.getAttribute('data-kind'),
                    sample_id: item.getAttribute('data-sample-id'),
                    sha256: item.getAttribute('data-sha256'),
                    image: item.querySelector('img')?.getAttribute('src'),
                }))"""
            )
            assert len(gallery) == 8, gallery
            assert {
                key: gallery[0][key] for key in ("kind", "sample_id", "sha256")
            } == {"kind": "original", "sample_id": sample_id, "sha256": source_sha256}
            assert gallery[0]["image"] == sample["links"]["image"], gallery[0]
            for item in gallery[1:]:
                row = rows_by_sample[item["sample_id"]]
                assert item["kind"] == row["transform"] and item["sha256"] == row["sha256"]
                response = page.request.get(f"http://127.0.0.1:{PREVIEW_PORT}{item['image']}")
                assert response.ok
                assert hashlib.sha256(response.body()).hexdigest() == row["sha256"]
                assert response.headers["x-qualsight-sha256"] == row["sha256"]

            geometry_options = page.locator("[data-testid=stress-geometry-select]").locator("option").evaluate_all(
                "options => options.map(option => option.value)"
            )
            assert geometry_options == [row["derived_sample_id"] for row in derived_rows], geometry_options
            stale_sample_id = derived_rows[-1]["derived_sample_id"]
            selected_geometry_row = derived_rows[0]
            selected_geometry_id = selected_geometry_row["derived_sample_id"]
            page.select_option("[data-testid=stress-geometry-select]", stale_sample_id)
            page.select_option("[data-testid=stress-geometry-select]", selected_geometry_id)
            page.wait_for_function(
                "sampleId => { const status = document.querySelector('[data-testid=stress-geometry-status]');"
                " return status?.getAttribute('data-selected-sample-id') === sampleId"
                " && status?.getAttribute('data-state') === 'READY'; }",
                arg=selected_geometry_id,
                timeout=240_000,
            )
            geometry_api = {
                "original": {
                    "detection": _json_request(
                        _sample_api_url(project_id, sample_id, "detection")
                    ),
                    "ofiq": _json_request(
                        _sample_api_url(project_id, sample_id, "ofiq-landmarks")
                    ),
                },
                "derived": {
                    "detection": _json_request(
                        _sample_api_url(project_id, selected_geometry_id, "detection")
                    ),
                    "ofiq": _json_request(
                        _sample_api_url(project_id, selected_geometry_id, "ofiq-landmarks")
                    ),
                },
            }
            for side, expected_sample_id in (("original", sample_id), ("derived", selected_geometry_id)):
                observed_geometry = _geometry_overlay(page, side)
                expected_geometry = _expected_geometry(
                    geometry_api[side]["detection"],
                    geometry_api[side]["ofiq"],
                )
                assert observed_geometry["sample_id"] == expected_sample_id, observed_geometry
                for key, value in expected_geometry.items():
                    assert observed_geometry[key] == value, (side, key, observed_geometry[key], value)
                if observed_geometry["scrfd_stroke"] and observed_geometry["ofiq_stroke"]:
                    assert observed_geometry["scrfd_stroke"] != observed_geometry["ofiq_stroke"], observed_geometry

            # Start a real quality Run over the first set, then record a new,
            # smaller set before the old terminal response reaches React. The
            # old Run may finish in storage, but it must not repopulate the new
            # derived-set workspace.
            page.evaluate(
                """
                () => {
                  const originalFetch = window.fetch.bind(window);
                  window.__stressOriginalFetch = originalFetch;
                  window.__stressHeldJobId = null;
                  window.__stressHeldJobReady = false;
                  window.__stressReleaseJob = null;
                  window.fetch = (input, init) => {
                    const raw = typeof input === 'string' ? input : input.url;
                    const url = new URL(raw, window.location.href);
                    const actual = originalFetch(input, init);
                    const jobId = window.__stressHeldJobId;
                    if (!jobId || !url.pathname.endsWith(`/jobs/${encodeURIComponent(jobId)}`)) return actual;
                    return actual.then(async response => {
                      const body = await response.clone().json();
                      if (!body.ended_at || !['COMPLETED', 'FAILED', 'CANCELLED'].includes(body.state)) return response;
                      return new Promise(resolve => {
                        window.__stressHeldJobReady = true;
                        window.__stressReleaseJob = () => resolve(response);
                      });
                    });
                  };
                }
                """
            )
            with page.expect_response(
                lambda response: response.request.method == "POST" and response.url.endswith("/quality-batch"),
                timeout=120_000,
            ) as stale_batch_response:
                page.click("[data-testid=stress-run-ofiq]")
            assert stale_batch_response.value.status == 202, stale_batch_response.value.text()
            stale_job_id = str(stale_batch_response.value.json()["job_id"])
            page.evaluate("jobId => { window.__stressHeldJobId = jobId; }", stale_job_id)

            for kind in PARAMETERS:
                toggle = page.locator(f"[data-testid=stress-operator-toggle-{kind}]")
                if kind == "jpeg":
                    if not toggle.is_checked():
                        toggle.check()
                elif toggle.is_checked():
                    toggle.uncheck()
            next_jpeg_parameter = PARAMETERS["jpeg"] + 1
            page.fill("[data-testid=stress-parameter-jpeg]", str(next_jpeg_parameter))
            with page.expect_response(lambda response: response.url.endswith("/transformations"), timeout=120_000) as next_created_response:
                page.click("[data-testid=stress-run]")
            assert next_created_response.value.status == 201, next_created_response.value.text()
            initial_derived = derived
            derived = next_created_response.value.json()
            assert derived["source_count"] == 1 and derived["levels"] == 1 and derived["derived_count"] == 1
            assert transformation_requests[-1] == {
                "selection": "sample_ids",
                "sample_ids": [sample_id],
                "transforms": [{"kind": "jpeg", "parameters": [next_jpeg_parameter]}],
            }
            detail = _json_request(f"http://127.0.0.1:{API_PORT}{derived['links']['detail']}")
            assert isinstance(detail, dict) and len(detail["samples"]) == 1
            derived_rows = detail["samples"]
            rows_by_sample = {row["derived_sample_id"]: row for row in derived_rows}
            selected_geometry_id = derived_rows[0]["derived_sample_id"]
            page.wait_for_function(
                "() => document.querySelectorAll('[data-testid=stress-gallery-item]').length === 2",
                timeout=60_000,
            )
            stale_job = _wait_job(project_id, stale_job_id)
            assert stale_job["state"] == "COMPLETED", stale_job
            page.wait_for_timeout(1_000)
            if page.evaluate("window.__stressHeldJobReady === true"):
                page.evaluate("window.__stressReleaseJob()")
            page.wait_for_timeout(500)
            assert page.locator(f'[data-testid=stress-quality-status][data-job-id="{stale_job_id}"]').count() == 0, (
                "the old quality Run repopulated the newly recorded derived set",
                stale_job_id,
            )
            page.evaluate(
                """
                window.fetch = window.__stressOriginalFetch;
                delete window.__stressOriginalFetch;
                delete window.__stressHeldJobId;
                delete window.__stressHeldJobReady;
                delete window.__stressReleaseJob;
                """
            )

            with page.expect_response(
                lambda response: response.request.method == "POST" and response.url.endswith("/quality-batch"),
                timeout=120_000,
            ) as batch_response:
                page.click("[data-testid=stress-run-ofiq]")
            assert batch_response.value.status == 202, batch_response.value.text()
            admitted_job = batch_response.value.json()
            page.wait_for_selector('[data-testid=stress-quality-status][data-state="RUNNING"]')
            running_copy = page.inner_text('[data-testid=stress-quality-status][data-state="RUNNING"]')
            assert "1 recorded derived image" in running_copy, running_copy
            job = _wait_job(project_id, str(admitted_job["job_id"]))
            assert job["state"] == "COMPLETED", job
            page.wait_for_function(
                "jobId => document.querySelector('[data-testid=stress-quality-status]')?.getAttribute('data-job-id') === jobId"
                " && document.querySelector('[data-testid=stress-quality-status]')?.getAttribute('data-state') === 'COMPLETED'",
                arg=job["job_id"],
                timeout=60_000,
            )
            job_results = _json_request(f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/jobs/{job['job_id']}/results")
            measure_catalog = _json_request(f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/jobs/{job['job_id']}/quality-measures")
            assert isinstance(job_results, dict) and job_results["total"] == derived["derived_count"]
            assert isinstance(measure_catalog, dict) and measure_catalog["default_measure"] == "UnifiedQualityScore"
            result_rows = {row["sample_id"]: row for row in job_results["rows"]}
            assert set(result_rows) == set(rows_by_sample)

            descriptive = page.inner_text("[data-testid=stress-claim-scope]").lower()
            assert "descriptive" in descriptive and "single-sample" in descriptive
            studio_text = page.inner_text("[data-testid=stress-studio]").lower()
            assert "fmr" not in studio_text and "fnmr" not in studio_text

            uqs_points = _chart_points(page, "UnifiedQualityScore")
            assert len(uqs_points) == derived["derived_count"] + 1, uqs_points
            assert page.locator(
                'svg[aria-label="UnifiedQualityScore scalar response for the original and 1 derived image"]'
            ).count() == 1
            assert uqs_points[0]["sample_id"] == sample_id
            assert uqs_points[0]["scalar"] == (
                "unavailable"
                if original_row["UnifiedQualityScore.scalar"] is None
                else str(original_row["UnifiedQualityScore.scalar"])
            )
            for point in uqs_points[1:]:
                row = result_rows[point["sample_id"]]
                expected = _expected_point(row, "UnifiedQualityScore", rows_by_sample[point["sample_id"]]["transform"])
                _assert_point_matches_recorded(point, expected)

            component_names = [name for name in measure_catalog["measures"] if name != "UnifiedQualityScore"]
            failing_component = next(
                (
                    name
                    for name in component_names
                    if any(bool(row[f"{name}.failed"]) for row in result_rows.values())
                ),
                "Sharpness",
            )
            selector_values = page.locator("[data-testid=stress-measure]").locator("option").evaluate_all(
                "options => options.map(option => option.value)"
            )
            assert selector_values == component_names, (selector_values, component_names)
            page.select_option("[data-testid=stress-measure]", failing_component)
            component_points = _chart_points(page, failing_component)
            assert len(component_points) == derived["derived_count"] + 1, component_points
            original_component_native = original_row[f"{failing_component}.native"]
            if original_component_native is None:
                assert component_points[0]["native"] == "unavailable", component_points[0]
            else:
                assert float(component_points[0]["native"]) == float(original_component_native), component_points[0]
            for point in component_points[1:]:
                row = result_rows[point["sample_id"]]
                expected = _expected_point(row, failing_component, rows_by_sample[point["sample_id"]]["transform"])
                _assert_point_matches_recorded(point, expected)
            selected_component_point = next(
                point for point in component_points if point["sample_id"] == selected_geometry_id
            )
            selected_component_row = result_rows[selected_geometry_id]
            selected_component_native = selected_component_row[f"{failing_component}.native"]
            if selected_component_native is None:
                assert selected_component_point["native"] == "unavailable", selected_component_point
            else:
                assert float(selected_component_point["native"]) == float(selected_component_native), selected_component_point
            assert selected_component_point["failure_reason"] == (
                selected_component_row["failure_reason"]
                if selected_component_row[f"{failing_component}.failed"]
                else ""
            )

            responsive = [
                _layout_snapshot(page, mode="desktop", width=1440, height=1050, equivalent_zoom=100),
                _layout_snapshot(page, mode="mobile", width=390, height=844, equivalent_zoom=100),
                _layout_snapshot(page, mode="200%-reflow", width=720, height=525, equivalent_zoom=200),
                _layout_snapshot(page, mode="400%-reflow", width=360, height=263, equivalent_zoom=400),
            ]
            page.set_viewport_size({"width": 1440, "height": 1050})
            if args.screenshot is not None:
                page.screenshot(path=str(args.screenshot), full_page=True)

            page.click("[data-testid=stress-clear]")
            page.wait_for_selector("[data-testid=stress-gallery-item]", state="detached", timeout=30_000)
            assert len(_json_request(f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/derived-sets")) == 2
            derived_directories = [
                data_dir / "derived" / str(item["derived_set_id"]).replace(":", "_")
                for item in (initial_derived, derived)
            ]
            assert all(path.is_dir() for path in derived_directories), derived_directories
            purged = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/purge",
                method="POST",
                body={"confirm": project_id},
            )
            assert isinstance(purged, dict) and purged["verified"]
            assert all(not path.exists() for path in derived_directories), derived_directories
            assert _json_request(f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/derived-sets") == []

            observed.update(
                {
                    "project_id": project_id,
                    "source_dataset_id": dataset_id,
                    "source_sample_id": sample_id,
                    "derived_set_id": derived["derived_set_id"],
                    "derived_dataset_id": derived["derived_dataset_id"],
                    "derived_hashes": {row["transform"]: row["sha256"] for row in derived_rows},
                    "quality_job_id": job["job_id"],
                    "quality_run_id": job["run_id"],
                    "parquet_sha256": measure_catalog["parquet_sha256"],
                    "selected_component": failing_component,
                    "component_has_recorded_failure": any(
                        bool(row[f"{failing_component}.failed"]) for row in result_rows.values()
                    ),
                    "responsive": responsive,
                    "purge_id": purged["purge_id"],
                    "passed": True,
                }
            )
            browser.close()
        exit_code = 0
    except Exception as error:
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
                "api": api.stdout.read().decode("utf-8", errors="replace")[-12_000:] if api.stdout else "",
                "preview": preview.stdout.read().decode("utf-8", errors="replace")[-4_000:] if preview.stdout else "",
            }
        shutil.rmtree(data_dir, ignore_errors=True)
        args.observation.write_text(json.dumps(observed, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(observed if exit_code == 0 else observed["failure"], indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
