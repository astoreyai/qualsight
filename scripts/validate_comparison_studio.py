"""Validate the direct owner-pair Comparison Studio with real LFW images.

The gate starts the built console and real local control plane, admits three
documented LFW files through the browser, records all three closed relationship
states, and executes direct comparisons through the recorded comparison API.
It never substitutes browser routes, generated inputs, or hard-coded results.
"""

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
PREVIEW_PORT = 5195
NODE_EXECUTABLE = shutil.which("node")
if NODE_EXECUTABLE is None:
    raise RuntimeError("the reviewed Node executable is not available on PATH")
NODE_BIN = str(Path(NODE_EXECUTABLE).parent)
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"
LFW_ROOT = lfw_tree_root(ROOT)
LEFT_IMAGE = LFW_ROOT / "Aaron_Peirsol/Aaron_Peirsol_0001.jpg"
SAME_PERSON_RIGHT_IMAGE = LFW_ROOT / "Aaron_Peirsol/Aaron_Peirsol_0002.jpg"
DIFFERENT_PERSON_RIGHT_IMAGE = LFW_ROOT / "Abdoulaye_Wade/Abdoulaye_Wade_0003.jpg"
EXPECTED_HASHES = {
    LEFT_IMAGE: "afb428f9939e4b545fc9982488afec825d262a4510b791f565bf3c4237605679",
    SAME_PERSON_RIGHT_IMAGE: "1b60c628765d7d681e2f1669b02be0f19476910b496d7768898ae4fd3720181d",
    DIFFERENT_PERSON_RIGHT_IMAGE: "98b0e314593f91c2bba4983d312c60287bcd29499d1879262ca785009012fc66",
}
RELATIONSHIPS = ("SAME_PERSON", "DIFFERENT_PEOPLE", "NOT_RECORDED")
TERMINAL_STATES = ("COMPLETED", "FAILED", "INTERRUPTED")


def _uv_binary() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv is unavailable on PATH")
    return executable
SOURCE_ACQUISITIONS = {
    "adaface-ir101-local-candidate": {
        "adaface_net": {
            "sha256": "b4db4eb0174a385fd29e5f616391b50d443f455990c8b88dcab1f8021af8ba4c",
            "size_bytes": 12787,
        }
    },
    "magface-iresnet100-ms1mv2-local-candidate": {
        "insightface_arcface_torch_iresnet": {
            "sha256": "fb57d145c4c26d8e55680c24fa164541a7222ccdefa3a1716c054ff4c5859753",
            "size_bytes": 7431,
        }
    },
}
EXPECTED_READY_CHECKPOINTS = {
    "adaface-ir101-local-candidate": ("0a379dbaf7d79573d1ff63f317c0296f3adfb637b9c00b6234fdcc53a77a0f61", 683066959),
    "elasticface-arc-r100-local-candidate": ("8ea1772a9dc0e95b77c171a1713f667f353d86ad66f2e3d418e7973360d2fb28", 261216371),
    "magface-iresnet100-ms1mv2-local-candidate": ("cfeba792dada6f1f30d1e118aff077d493dd95dd76c77c30f57f90fd0164ad58", 283040192),
    "topofr-r100-glint360k-local-candidate": ("f92e5e61b326495d32803156ab60aa58abf7e17f84e83d38b5ff6bf1691bdbfe", 998958213),
}


def _assert_frontend_contract() -> None:
    source = (ROOT / "frontend" / "src" / "ComparisonStudio.tsx").read_text(
        encoding="utf-8"
    )
    required = {
        'phase: "starting"': "the synchronous pre-request execution lock",
        'data-testid="comparison-history-loading"': "visible history loading state",
        'data-testid="comparison-history-error"': "visible history failure state",
        'data-testid="comparison-history-retry"': "history retry action",
        'data-testid="comparison-side-evidence-actions"': (
            "collapsed secondary side-artifact evidence actions"
        ),
        'data-testid="comparison-project-prerequisite"': (
            "the visible no-project input prerequisite"
        ),
        "historyReadSequence": "completed-history request sequencing",
        "activeProjectId.current !== requestedProjectId": (
            "completed-history project binding"
        ),
        'disabled={status.state !== "COMPLETED" || configurationLocked}': (
            "history locking during current execution"
        ),
        'accept="image/jpeg,image/png,image/bmp"': (
            "the exact owner-admission media types"
        ),
        "JPEG, PNG, or BMP": "visible admitted-format guidance",
    }
    missing = [description for marker, description in required.items() if marker not in source]
    assert not missing, f"Comparison Studio frontend contract is missing: {missing!r}"


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


def _select_values(page, selector: str) -> list[str]:
    return page.locator(f"{selector} option").evaluate_all(
        "options => options.map(option => option.value).filter(Boolean)"
    )


def _assert_side(page, side: str, upload: dict, expected_path: Path) -> None:
    root = page.locator(f"[data-testid=comparison-side-{side}]")
    root.wait_for(state="visible", timeout=30_000)
    page.wait_for_function(
        "([selector, sampleId]) => document.querySelector(selector)?.getAttribute('data-sample-id') === sampleId",
        arg=[f"[data-testid=comparison-side-{side}]", upload["sample_id"]],
        timeout=30_000,
    )
    page.wait_for_function(
        "selector => { const root = document.querySelector(selector); return root && ['ready', 'error'].includes(root.getAttribute('data-scrfd-state')) && ['ready', 'error'].includes(root.getAttribute('data-ofiq-state')); }",
        arg=f"[data-testid=comparison-side-{side}]",
        timeout=240_000,
    )
    assert root.get_attribute("data-quality-state") == "ready"
    assert root.get_attribute("data-scrfd-state") == "ready"
    assert root.get_attribute("data-ofiq-state") == "ready"
    assert root.get_attribute("data-sample-id") == upload["sample_id"]
    assert root.get_attribute("data-image-sha256") == EXPECTED_HASHES[expected_path]

    image = page.locator(f"[data-testid=comparison-image-{side}]")
    assert image.get_attribute("src") == upload["provenance"]["sample_image"]
    page.wait_for_function(
        "selector => { const image = document.querySelector(selector); return image?.complete && image.naturalWidth > 0 && image.naturalHeight > 0; }",
        arg=f"[data-testid=comparison-image-{side}]",
        timeout=30_000,
    )
    quality = page.locator(f"[data-testid=comparison-quality-{side}]")
    assert int(quality.get_attribute("data-measure-count") or "0") == 28
    assert quality.get_attribute("data-run-id") == upload["run_id"]
    geometry = page.locator(f"[data-testid=comparison-geometry-{side}]")
    assert int(geometry.get_attribute("data-scrfd-landmarks") or "0") == 5
    assert int(geometry.get_attribute("data-ofiq-landmarks") or "0") == 98


def _browser_upload_record(page, side: str, expected_path: Path) -> dict:
    expected_hash = EXPECTED_HASHES[expected_path]
    page.wait_for_function(
        "([selector, sha256]) => document.querySelector(selector)?.getAttribute('data-image-sha256') === sha256",
        arg=[f"[data-testid=comparison-side-{side}]", expected_hash],
        timeout=30_000,
    )
    sample_id = page.get_attribute(f"[data-testid=comparison-side-{side}]", "data-sample-id")
    run_id = page.get_attribute(f"[data-testid=comparison-quality-{side}]", "data-run-id")
    sample_image = page.get_attribute(f"[data-testid=comparison-image-{side}]", "src")
    assert sample_id and run_id and sample_image
    return {
        "sample_id": sample_id,
        "run_id": run_id,
        "provenance": {"sample_image": sample_image},
    }


def _assert_pair_record(page, pair: dict, relationship: str) -> None:
    record = page.locator("[data-testid=comparison-pair-record]")
    record.wait_for(state="visible", timeout=30_000)
    assert record.get_attribute("data-pair-id") == pair["pair_id"]
    assert record.get_attribute("data-relationship") == relationship
    assert pair["relationship_assertion"]["state"] == "OPERATOR_ASSERTED_UNVERIFIED"
    text = record.inner_text().lower()
    assert "operator asserted" in text and "unverified" in text
    assert "ground truth" in text and ("not" in text or "no " in text)
    assert pair["population_error_claims_allowed"] is False


def _assert_direct_evidence(page, evidence: dict, relationship: str) -> None:
    root = page.locator("[data-testid=direct-pair-evidence]")
    root.wait_for(state="visible", timeout=30_000)
    assert root.get_attribute("data-state") == "ready"
    assert root.get_attribute("data-comparison-id") == evidence["comparison_id"]

    assertion = page.locator("[data-testid=comparison-assertion]")
    decision = page.locator("[data-testid=comparison-decision]")
    assert assertion.count() == 1 and decision.count() == 1
    assert assertion.evaluate("element => !!(element.compareDocumentPosition(document.querySelector('[data-testid=comparison-decision]')) & Node.DOCUMENT_POSITION_FOLLOWING)")
    assert relationship.replace("_", " ").lower() in assertion.inner_text().lower()
    rendered_decision = decision.get_attribute("data-decision")
    expected_decision = evidence["result"]["decision"]
    assert rendered_decision == (expected_decision or "NONE")

    agreement = page.locator("[data-testid=comparison-agreement]").inner_text().lower()
    expected_agreement = evidence["result"]["agreement_with_operator_assertion"]
    if relationship == "NOT_RECORDED":
        assert expected_agreement is None
        assert "no relationship was recorded" in agreement
        assert "no agreement or correctness claim" in agreement
    elif expected_decision is None:
        assert expected_agreement is None
        assert "no matcher decision" in agreement
        assert "no agreement or correctness claim" in agreement
    elif expected_agreement:
        assert "agrees with the operator assertion" in agreement
        assert "not a ground-truth correctness claim" in agreement
    else:
        assert "differs from the operator assertion" in agreement
        assert "not a ground-truth correctness claim" in agreement

    execution = page.locator("[data-testid=comparison-execution]")
    assert execution.get_attribute("data-matcher-id") == evidence["execution"]["matcher_id"]
    assert execution.get_attribute("data-preprocessing-id") == evidence["execution"]["preprocessing_id"]
    assert execution.get_attribute("data-run-id") == evidence["run_id"]
    assert evidence["claim_boundary"]["population_error_claims_allowed"] is False
    assert evidence["claim_boundary"]["ground_truth_authority"] is False
    claim_text = page.locator("[data-testid=comparison-claim-boundary]").inner_text().lower()
    assert "population error" in claim_text and "not" in claim_text
    assert "ground truth" in claim_text and ("not" in claim_text or "no " in claim_text)

    for side in ("left", "right"):
        expected = evidence["sides"][side]
        rendered = page.locator(f"[data-testid=comparison-evidence-{side}]")
        assert rendered.get_attribute("data-sample-id") == expected["sample_id"]
        assert rendered.get_attribute("data-sha256") == expected["sha256"]
        evidence_actions = rendered.locator(
            "details[data-testid=comparison-side-evidence-actions]"
        )
        assert evidence_actions.count() == 1
        assert evidence_actions.evaluate("element => !element.open")
        summary = evidence_actions.locator("summary")
        summary.focus()
        page.keyboard.press("Enter")
        assert evidence_actions.evaluate("element => element.open")
        for artifact in ("source", "crop", "detection", "embedding", "quality"):
            link = expected["links"][artifact]
            rendered_link = evidence_actions.locator(f"[data-artifact={artifact}]")
            if link is None:
                assert rendered_link.count() == 0
            else:
                assert rendered_link.get_attribute("href") == link
        page.keyboard.press("Enter")
        assert evidence_actions.evaluate("element => !element.open")

    raw = page.locator("[data-testid=comparison-raw-actions]")
    assert raw.evaluate("element => !element.open")
    assert raw.evaluate("element => !!(document.querySelector('[data-testid=direct-pair-evidence]').compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING)")
    assert raw.locator("[data-testid=comparison-raw-view]").get_attribute("href") == evidence["links"]["raw_manifest"]
    assert (raw.locator("[data-testid=comparison-raw-download]").get_attribute("download") or "").endswith(".json")
    run_action = page.locator("[data-testid=comparison-run-link]")
    status_action = page.locator("[data-testid=comparison-status-link]")
    assert run_action.get_attribute("data-evidence-href") == evidence["links"]["run"]
    assert run_action.get_attribute("data-evidence-id") == evidence["run_id"]
    assert run_action.get_attribute("data-project-id") == evidence["project_id"]
    assert status_action.get_attribute("data-evidence-href") == evidence["links"]["status"]
    assert status_action.get_attribute("data-evidence-id") == evidence["comparison_id"]
    assert status_action.get_attribute("data-project-id") == evidence["project_id"]


def _open_structured_evidence_action(
    page,
    *,
    test_id: str,
    expected_kind: str,
    expected_id: str,
    expected_project_id: str,
    expected_href: str,
) -> dict[str, str]:
    action = page.locator(f"[data-testid={test_id}]")
    action.wait_for(state="visible", timeout=30_000)
    assert action.get_attribute("data-evidence-kind") == expected_kind
    assert action.get_attribute("data-evidence-id") == expected_id
    assert action.get_attribute("data-project-id") == expected_project_id
    assert action.get_attribute("data-evidence-href") == expected_href
    route_before = page.url
    action.focus()
    page.keyboard.press("Enter")
    detail = page.locator(
        f"[data-testid=evidence-detail][data-evidence-kind='{expected_kind}']"
        f"[data-evidence-id='{expected_id}'][data-project-id='{expected_project_id}']"
    )
    detail.wait_for(state="visible", timeout=60_000)
    assert page.url == route_before
    assert page.evaluate(
        "document.querySelector('[data-testid=evidence-detail]').contains(document.activeElement)"
    )
    assert detail.locator("h4").count() >= 1
    assert detail.locator("dl, table, ol").count() >= 1
    assert detail.locator("pre").count() == 0
    raw = detail.locator("details[data-testid=evidence-actions]")
    assert raw.count() == 1 and not raw.evaluate("element => element.open")
    assert raw.locator("a[href^='/api/']").count() == 2
    assert detail.locator("a[href^='/api/']").count() == 2
    close = detail.get_by_role("button", name="Close")
    close.focus()
    page.keyboard.press("Enter")
    detail.wait_for(state="detached", timeout=30_000)
    page.wait_for_function(
        "testId => document.activeElement === document.querySelector(`[data-testid='${testId}']`)",
        arg=test_id,
        timeout=30_000,
    )
    return {
        "test_id": test_id,
        "kind": expected_kind,
        "evidence_id": expected_id,
        "project_id": expected_project_id,
        "href": expected_href,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    _assert_frontend_contract()

    for path, expected_hash in EXPECTED_HASHES.items():
        if not path.is_file():
            raise FileNotFoundError(f"documented real LFW input is unavailable: {path}")
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError(f"real LFW input hash drifted: {path}: {actual_hash}")
    if not _port_is_free(API_PORT) or not _port_is_free(PREVIEW_PORT):
        raise RuntimeError(f"ports {API_PORT}/{PREVIEW_PORT} must be free")

    args.observation.parent.mkdir(parents=True, exist_ok=True)
    if args.screenshot is not None:
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)

    data_dir = Path(tempfile.mkdtemp(prefix="qualsight-comparison-studio-"))
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
        "kind": "QUALSIGHT_COMPARISON_STUDIO_VALIDATION",
        "real_inputs": [
            {
                "path": str(path),
                "sha256": expected_hash,
                "size_bytes": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns,
            }
            for path, expected_hash in EXPECTED_HASHES.items()
        ],
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
                "name": "Direct owner-pair browser validation",
                "description": "Real LFW validation of direct comparison evidence.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        assert isinstance(project, dict)
        project_id = str(project["project_id"])
        _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/open",
            method="POST",
        )

        source_acquisitions: list[dict] = []
        acquired_ready: dict[str, dict] = {}

        from playwright.sync_api import sync_playwright

        upload_requests: list[dict] = []
        admission_requests: list[dict] = []
        comparison_requests: list[dict] = []
        geometry_urls: list[str] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            def record_request(request) -> None:
                if request.method == "POST" and "/api/quality/assess-upload" in request.url:
                    upload_requests.append({"url": request.url})
                elif request.method == "POST" and "/owner-pair-admissions" in request.url:
                    admission_requests.append(request.post_data_json)
                elif request.method == "POST" and "/owner-pair-comparisons" in request.url:
                    comparison_requests.append(request.post_data_json)
                else:
                    request_path = urllib.parse.unquote(urllib.parse.urlparse(request.url).path)
                    project_sample_prefix = f"/api/projects/{project_id}/samples/"
                    if request_path.startswith(project_sample_prefix) and request_path.endswith(("/detection", "/ofiq-landmarks")):
                        geometry_urls.append(request.url)

            page.on("request", record_request)
            page.goto(
                f"http://127.0.0.1:{PREVIEW_PORT}/#/matchers",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.wait_for_selector("[data-testid=comparison-studio]", timeout=30_000)
            prerequisite = page.locator("[data-testid=comparison-project-prerequisite]")
            if prerequisite.count() != 1 or not prerequisite.is_visible():
                raise AssertionError("Comparison Studio has no visible active-project prerequisite")
            for side in ("a", "b"):
                upload_input = page.locator(f"[data-testid=comparison-upload-{side}]")
                if not upload_input.is_disabled():
                    raise AssertionError(
                        f"comparison upload {side} accepts a file without an active project"
                    )
            page.evaluate(
                "projectId => window.localStorage.setItem('qualsight.activeProjectId', projectId)",
                project_id,
            )
            page.reload(wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("[data-testid=comparison-studio]", timeout=30_000)
            if page.locator("[data-testid=comparison-project-prerequisite]").count() != 0:
                raise AssertionError("project prerequisite remained after activating the real project")
            for side in ("a", "b"):
                upload_input = page.locator(f"[data-testid=comparison-upload-{side}]")
                if upload_input.is_disabled():
                    raise AssertionError(
                        f"comparison upload {side} remained disabled with an active project"
                    )
                if upload_input.get_attribute("accept") != "image/jpeg,image/png,image/bmp":
                    raise AssertionError(
                        f"comparison upload {side} advertises media outside the admitted JPEG/PNG/BMP set"
                    )
            format_guidance = page.locator("[data-testid=comparison-side-a] .comparison-file-label small")
            if not format_guidance.is_visible() or (format_guidance.text_content() or "").strip() != "JPEG, PNG, or BMP; recorded to the active project":
                raise AssertionError("Comparison Studio does not visibly state its exact admitted media types")
            assert page.locator("[data-testid=comparison-mode-pair]").get_attribute("aria-pressed") == "true"
            assert page.locator("[data-testid=comparison-mode-single]").get_attribute("href") == "#/assess"
            page.wait_for_selector("[data-testid=comparison-no-matchers]", timeout=30_000)
            assert _select_values(page, "[data-testid=comparison-matcher]") == []
            assert page.locator("[data-testid=comparison-no-matchers]").is_visible()
            assert page.locator("[data-testid=comparison-manage-addons]").get_attribute("href") == "#/system"

            for matcher_id, expected_sources in SOURCE_ACQUISITIONS.items():
                result = _json_request(
                    f"http://127.0.0.1:{API_PORT}/api/matcher-assets/{matcher_id}/pull-sources",
                    method="POST",
                )
                assert isinstance(result, dict)
                asset = result["asset"]
                assert asset["matcher_id"] == matcher_id
                by_key = {item["key"]: item for item in asset["architecture_sources"]}
                for key, expected in expected_sources.items():
                    source = by_key[key]
                    assert source["state"] == "VERIFIED", source
                    assert source["sha256"] == expected["sha256"], source
                    assert source["size_bytes"] == expected["size_bytes"], source
                source_acquisitions.append(
                    {
                        "matcher_id": matcher_id,
                        "event_id": result["event"]["system_event_id"],
                        "sources": [by_key[key] for key in expected_sources],
                    }
                )
            acquired_catalog = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/addons"
            )
            assert isinstance(acquired_catalog, dict)
            acquired_ready = {
                item["matcher_id"]: item
                for item in acquired_catalog["matchers"]
                if item["ready_to_load"] is True
            }
            assert set(acquired_ready) == set(EXPECTED_READY_CHECKPOINTS), acquired_ready
            for matcher_id, (sha256, size_bytes) in EXPECTED_READY_CHECKPOINTS.items():
                checkpoint = acquired_ready[matcher_id]["checkpoint"]
                assert checkpoint["state"] == "VERIFIED", checkpoint
                assert checkpoint["registered_sha256"] == sha256, checkpoint
                assert checkpoint["registered_size_bytes"] == size_bytes, checkpoint

            with page.expect_response(
                lambda response: response.url.endswith("/api/addons"),
                timeout=30_000,
            ) as refresh_response:
                page.locator("[data-testid=comparison-refresh-catalog]").click()
            assert refresh_response.value.status == 200
            page.wait_for_function(
                "expected => Array.from(document.querySelectorAll('[data-testid=comparison-matcher] option')).filter(option => option.value).length === expected",
                arg=len(acquired_ready),
                timeout=30_000,
            )
            page.locator("[data-testid=comparison-mode-dataset]").click()
            page.wait_for_selector("[data-testid=matchers]", timeout=30_000)
            page.locator("[data-testid=comparison-mode-pair]").click()

            relationship_inputs = page.locator("input[name=comparison-relationship]")
            assert relationship_inputs.count() == 3
            assert relationship_inputs.evaluate_all("inputs => inputs.map(input => input.value)") == list(RELATIONSHIPS)
            assert relationship_inputs.evaluate_all("inputs => inputs.every(input => !input.checked)")
            assert page.locator("[data-testid=comparison-finalize]").is_disabled()

            async_uploads: dict[str, dict] = {}
            for side, path in (("a", LEFT_IMAGE), ("b", SAME_PERSON_RIGHT_IMAGE)):
                with page.expect_response(
                    lambda response: "/api/quality/assess-upload" in response.url,
                    timeout=300_000,
                ) as upload_response:
                    page.set_input_files(f"[data-testid=comparison-upload-{side}]", str(path))
                response = upload_response.value
                assert response.status == 201, f"side {side} upload returned HTTP {response.status}"
                upload = _browser_upload_record(page, side, path)
                async_uploads[side] = upload
                _assert_side(page, side, upload, path)

            left_sample_id = async_uploads["a"]["sample_id"]
            first_right_sample_id = async_uploads["b"]["sample_id"]
            page.check("input[name=comparison-relationship][value=SAME_PERSON]")
            with page.expect_response(
                lambda response: "/owner-pair-admissions" in response.url,
                timeout=60_000,
            ) as pair_response:
                page.click("[data-testid=comparison-finalize]")
            assert pair_response.value.status == 201
            same_pair = pair_response.value.json()
            assert same_pair["left_sample_id"] == left_sample_id
            assert same_pair["right_sample_id"] == first_right_sample_id
            _assert_pair_record(page, same_pair, "SAME_PERSON")

            catalog = _json_request(f"http://127.0.0.1:{API_PORT}/api/addons")
            assert isinstance(catalog, dict)
            ready_matchers = [
                item for item in catalog["matchers"] if item["ready_to_load"] is True
            ]
            assert len(ready_matchers) >= 2, "real add-on catalog has fewer than two executable matchers"
            page.wait_for_function(
                "expected => Array.from(document.querySelectorAll('[data-testid=comparison-matcher] option')).filter(option => option.value).length === expected",
                arg=len(ready_matchers),
                timeout=30_000,
            )
            matcher_values = _select_values(page, "[data-testid=comparison-matcher]")
            assert matcher_values == [item["matcher_id"] for item in ready_matchers], (
                matcher_values,
                [item["matcher_id"] for item in ready_matchers],
            )
            selected_matcher = page.locator("[data-testid=comparison-matcher]").input_value()
            ready_profiles = [
                item
                for item in catalog["preprocessing"]
                if item["runtime_state"] == "READY"
                and item["input_representation"] == "SOURCE_IMAGE_REQUIRING_DETECTION"
                and selected_matcher in item["compatible_matcher_ids"]
            ]
            page.wait_for_function(
                "expected => Array.from(document.querySelectorAll('[data-testid=comparison-preprocessing] option')).filter(option => option.value).length === expected",
                arg=len(ready_profiles),
                timeout=30_000,
            )
            profile_values = _select_values(page, "[data-testid=comparison-preprocessing]")
            assert profile_values == [item["addon_id"] for item in ready_profiles]
            assert ready_profiles, "real add-on catalog has no compatible direct-pair preprocessing"
            assert page.locator("[data-testid=comparison-threshold]").input_value() == "0.8"

            comparison_records: list[dict] = []

            def run_comparison(
                pair: dict, relationship: str, *, hold_start: bool = False
            ) -> dict:
                matcher_id = page.locator("[data-testid=comparison-matcher]").input_value()
                preprocessing_id = page.locator("[data-testid=comparison-preprocessing]").input_value()
                requests_before = len(comparison_requests)
                if hold_start:
                    page.evaluate(
                        """() => {
                            const originalFetch = window.fetch.bind(window);
                            window.__qualsightHeldComparisonStart = { received: false, released: false };
                            window.fetch = async (input, init) => {
                                const url = typeof input === 'string' ? input : input.url;
                                const method = (init?.method || (typeof input === 'string' ? 'GET' : input.method) || 'GET').toUpperCase();
                                const response = await originalFetch(input, init);
                                if (method === 'POST' && url.includes('/owner-pair-comparisons')) {
                                    window.__qualsightHeldComparisonStart.received = true;
                                    return await new Promise((resolve) => {
                                        window.__qualsightReleaseComparisonStart = () => {
                                            window.__qualsightHeldComparisonStart.released = true;
                                            window.fetch = originalFetch;
                                            resolve(response);
                                        };
                                    });
                                }
                                return response;
                            };
                        }"""
                    )
                with page.expect_response(
                    lambda response: "/owner-pair-comparisons" in response.url,
                    timeout=60_000,
                ) as comparison_response:
                    page.click("[data-testid=comparison-run]")
                if hold_start:
                    page.wait_for_function(
                        "() => window.__qualsightHeldComparisonStart?.received === true",
                        timeout=60_000,
                    )
                    locked_controls = (
                        "[data-testid=comparison-run]",
                        "[data-testid=comparison-matcher]",
                        "[data-testid=comparison-preprocessing]",
                        "[data-testid=comparison-threshold]",
                        "[data-testid=comparison-upload-a]",
                        "[data-testid=comparison-upload-b]",
                        "input[name=comparison-relationship][value=SAME_PERSON]",
                    )
                    for selector in locked_controls:
                        assert page.locator(selector).is_disabled(), (
                            f"pre-response direct comparison left {selector} editable"
                        )
                    completed_history = page.locator(
                        "[data-testid=comparison-history-item]"
                    )
                    for index in range(completed_history.count()):
                        assert completed_history.nth(index).is_disabled(), (
                            "completed history remained interactive during a current "
                            "direct-comparison execution"
                        )
                    page.evaluate(
                        "() => document.querySelector('[data-testid=comparison-run]').click()"
                    )
                    page.wait_for_timeout(250)
                    assert len(comparison_requests) == requests_before + 1, (
                        "a duplicate direct-comparison POST escaped while the first real "
                        "response was held"
                    )
                    page.evaluate("() => window.__qualsightReleaseComparisonStart()")
                assert comparison_response.value.status in (200, 202)
                status = comparison_response.value.json()
                assert status["pair_id"] == pair["pair_id"]
                assert status["state"] in ("QUEUED", "RUNNING", *TERMINAL_STATES)
                page.wait_for_function(
                    "comparisonId => { const root = document.querySelector('[data-testid=direct-pair-evidence]'); return root?.getAttribute('data-comparison-id') === comparisonId && ['ready', 'failed'].includes(root.getAttribute('data-state')); }",
                    arg=status["comparison_id"],
                    timeout=600_000,
                )
                evidence_response = page.request.get(
                    f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/owner-pair-comparisons/{status['comparison_id']}/evidence"
                )
                assert evidence_response.status == 200
                evidence = evidence_response.json()
                assert evidence["pair"]["pair_id"] == pair["pair_id"]
                assert evidence["pair"]["relationship"] == relationship
                assert evidence["execution"]["matcher_id"] == matcher_id
                assert evidence["execution"]["preprocessing_id"] == preprocessing_id
                _assert_direct_evidence(page, evidence, relationship)
                comparison_records.append(evidence)
                return evidence

            first_same_evidence = run_comparison(
                same_pair, "SAME_PERSON", hold_start=True
            )
            second_matcher = ready_matchers[1]["matcher_id"]
            page.select_option("[data-testid=comparison-matcher]", second_matcher)
            assert page.locator("[data-testid=comparison-pair-record]").get_attribute("data-pair-id") == same_pair["pair_id"]
            second_same_evidence = run_comparison(
                same_pair, "SAME_PERSON", hold_start=True
            )
            assert second_same_evidence["comparison_id"] != first_same_evidence["comparison_id"]
            assert second_same_evidence["run_id"] != first_same_evidence["run_id"]
            history_ids = page.locator("[data-testid=comparison-history-item]").evaluate_all(
                "items => items.map(item => item.getAttribute('data-comparison-id'))"
            )
            assert first_same_evidence["comparison_id"] in history_ids
            assert second_same_evidence["comparison_id"] in history_ids

            page.evaluate(
                """comparisonId => {
                    const originalFetch = window.fetch.bind(window);
                    window.__qualsightHistoryRace = { received: false, released: false };
                    window.fetch = async (input, init) => {
                        const url = typeof input === 'string' ? input : input.url;
                        const method = (init?.method || (typeof input === 'string' ? 'GET' : input.method) || 'GET').toUpperCase();
                        const path = decodeURIComponent(new URL(url, window.location.href).pathname);
                        const target = `/owner-pair-comparisons/${comparisonId}/evidence`;
                        if (method === 'GET' && path.endsWith(target)) {
                            const response = await originalFetch(input, init);
                            window.__qualsightHistoryRace.received = true;
                            return await new Promise((resolve) => {
                                window.__qualsightReleaseHistory = () => {
                                    window.__qualsightHistoryRace.released = true;
                                    window.fetch = originalFetch;
                                    resolve(response);
                                };
                            });
                        }
                        return originalFetch(input, init);
                    };
                }""",
                first_same_evidence["comparison_id"],
            )
            page.evaluate(
                """([firstId, secondId]) => {
                    document.querySelector(`[data-testid=comparison-history-item][data-comparison-id="${firstId}"]`)?.click();
                    document.querySelector(`[data-testid=comparison-history-item][data-comparison-id="${secondId}"]`)?.click();
                }""",
                [
                    first_same_evidence["comparison_id"],
                    second_same_evidence["comparison_id"],
                ],
            )
            page.wait_for_function(
                "() => window.__qualsightHistoryRace?.received === true",
                timeout=60_000,
            )
            page.wait_for_function(
                "comparisonId => document.querySelector('[data-testid=direct-pair-evidence]')?.getAttribute('data-comparison-id') === comparisonId",
                arg=second_same_evidence["comparison_id"],
                timeout=60_000,
            )
            page.evaluate("() => window.__qualsightReleaseHistory()")
            page.wait_for_function(
                "() => window.__qualsightHistoryRace?.released === true",
                timeout=30_000,
            )
            page.wait_for_timeout(250)
            stale_history_id = page.locator(
                "[data-testid=direct-pair-evidence]"
            ).get_attribute("data-comparison-id")
            assert stale_history_id == second_same_evidence["comparison_id"], (
                "a delayed older history response overwrote the latest completed-history "
                f"selection: {stale_history_id!r}"
            )

            with page.expect_response(
                lambda response: "/api/quality/assess-upload" in response.url,
                timeout=300_000,
            ) as replacement_response:
                page.set_input_files(
                    "[data-testid=comparison-upload-b]", str(DIFFERENT_PERSON_RIGHT_IMAGE)
                )
            assert replacement_response.value.status == 201
            different_upload = _browser_upload_record(
                page, "b", DIFFERENT_PERSON_RIGHT_IMAGE
            )
            _assert_side(page, "b", different_upload, DIFFERENT_PERSON_RIGHT_IMAGE)
            assert page.locator("[data-testid=comparison-side-a]").get_attribute("data-sample-id") == left_sample_id
            assert page.locator("[data-testid=comparison-pair-record]").count() == 0
            assert page.locator("[data-testid=direct-pair-evidence]").count() == 0

            page.check("input[name=comparison-relationship][value=DIFFERENT_PEOPLE]")
            with page.expect_response(
                lambda response: "/owner-pair-admissions" in response.url,
                timeout=60_000,
            ) as different_pair_response:
                page.click("[data-testid=comparison-finalize]")
            assert different_pair_response.value.status == 201
            different_pair = different_pair_response.value.json()
            _assert_pair_record(page, different_pair, "DIFFERENT_PEOPLE")
            page.select_option("[data-testid=comparison-matcher]", ready_matchers[0]["matcher_id"])
            different_evidence = run_comparison(different_pair, "DIFFERENT_PEOPLE")

            page.check("input[name=comparison-relationship][value=NOT_RECORDED]")
            assert page.locator("[data-testid=comparison-pair-record]").count() == 0
            with page.expect_response(
                lambda response: "/owner-pair-admissions" in response.url,
                timeout=60_000,
            ) as unrecorded_pair_response:
                page.click("[data-testid=comparison-finalize]")
            assert unrecorded_pair_response.value.status == 201
            unrecorded_pair = unrecorded_pair_response.value.json()
            _assert_pair_record(page, unrecorded_pair, "NOT_RECORDED")
            unrecorded_matcher = page.locator("[data-testid=comparison-matcher]").input_value()
            unrecorded_preprocessing = page.locator(
                "[data-testid=comparison-preprocessing]"
            ).input_value()
            with page.expect_response(
                lambda response: "/owner-pair-comparisons" in response.url,
                timeout=60_000,
            ) as unrecorded_comparison_response:
                page.click("[data-testid=comparison-run]")
            assert unrecorded_comparison_response.value.status in (200, 202)
            unrecorded_status = unrecorded_comparison_response.value.json()
            assert unrecorded_status["pair_id"] == unrecorded_pair["pair_id"]
            page.wait_for_function(
                "() => document.querySelector('[data-testid=comparison-run]')?.disabled === true",
                timeout=30_000,
            )
            for locked_control in (
                "[data-testid=comparison-matcher]",
                "[data-testid=comparison-preprocessing]",
                "[data-testid=comparison-threshold]",
            ):
                assert page.locator(locked_control).is_disabled(), (
                    f"in-flight direct comparison left {locked_control} editable"
                )

            page.check("input[name=comparison-relationship][value=DIFFERENT_PEOPLE]")
            assert page.locator("[data-testid=comparison-pair-record]").count() == 0
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                polled = _json_request(
                    f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/owner-pair-comparisons/{unrecorded_status['comparison_id']}"
                )
                assert isinstance(polled, dict)
                if polled["state"] in TERMINAL_STATES:
                    unrecorded_status = polled
                    break
                time.sleep(0.25)
            assert unrecorded_status["state"] == "COMPLETED", unrecorded_status
            page.wait_for_timeout(1_000)
            stale_evidence = page.locator(
                f"[data-testid=direct-pair-evidence][data-comparison-id='{unrecorded_status['comparison_id']}']"
            )
            assert stale_evidence.count() == 0, (
                "a completed comparison injected evidence after the active pair context changed"
            )

            history_item = page.locator(
                f"[data-testid=comparison-history-item][data-comparison-id='{unrecorded_status['comparison_id']}']"
            )
            history_item.wait_for(state="visible", timeout=30_000)
            assert not history_item.is_disabled()
            history_item.click()
            page.wait_for_function(
                "comparisonId => document.querySelector('[data-testid=direct-pair-evidence]')?.getAttribute('data-comparison-id') === comparisonId && document.querySelector('[data-testid=direct-pair-evidence]')?.getAttribute('data-state') === 'ready'",
                arg=unrecorded_status["comparison_id"],
                timeout=60_000,
            )
            unrecorded_evidence_response = page.request.get(
                f"http://127.0.0.1:{API_PORT}/api/projects/{project_id}/owner-pair-comparisons/{unrecorded_status['comparison_id']}/evidence"
            )
            assert unrecorded_evidence_response.status == 200
            unrecorded_evidence = unrecorded_evidence_response.json()
            assert unrecorded_evidence["execution"]["matcher_id"] == unrecorded_matcher
            assert unrecorded_evidence["execution"]["preprocessing_id"] == unrecorded_preprocessing
            _assert_direct_evidence(page, unrecorded_evidence, "NOT_RECORDED")
            comparison_records.append(unrecorded_evidence)
            assert unrecorded_evidence["result"]["agreement_with_operator_assertion"] is None
            structured_evidence_actions = [
                _open_structured_evidence_action(
                    page,
                    test_id="comparison-run-link",
                    expected_kind="direct-run",
                    expected_id=unrecorded_evidence["run_id"],
                    expected_project_id=project_id,
                    expected_href=unrecorded_evidence["links"]["run"],
                ),
                _open_structured_evidence_action(
                    page,
                    test_id="comparison-status-link",
                    expected_kind="direct-comparison",
                    expected_id=unrecorded_evidence["comparison_id"],
                    expected_project_id=project_id,
                    expected_href=unrecorded_evidence["links"]["status"],
                ),
            ]

            expected_admissions = [
                {
                    "left_sample_id": left_sample_id,
                    "right_sample_id": first_right_sample_id,
                    "relationship": "SAME_PERSON",
                },
                {
                    "left_sample_id": left_sample_id,
                    "right_sample_id": different_upload["sample_id"],
                    "relationship": "DIFFERENT_PEOPLE",
                },
                {
                    "left_sample_id": left_sample_id,
                    "right_sample_id": different_upload["sample_id"],
                    "relationship": "NOT_RECORDED",
                },
            ]
            assert len(upload_requests) == 3, upload_requests
            assert admission_requests == expected_admissions
            assert len(comparison_requests) == 4, comparison_requests
            assert all(
                request["threshold"]
                == {
                    "contract_id": "matcher_threshold",
                    "kind": "FIXED_VALUE",
                    "value": 0.8,
                }
                for request in comparison_requests
            )
            geometry_paths = [
                urllib.parse.unquote(urllib.parse.urlparse(url).path)
                for url in geometry_urls
            ]
            expected_geometry_paths = [
                f"/api/projects/{project_id}/samples/{sample_id}/{kind}"
                for sample_id in (left_sample_id, first_right_sample_id, different_upload["sample_id"])
                for kind in ("detection", "ofiq-landmarks")
            ]
            assert sorted(geometry_paths) == sorted(expected_geometry_paths), {
                "expected": expected_geometry_paths,
                "observed": geometry_paths,
            }

            page.set_viewport_size({"width": 720, "height": 1000})
            overflow = page.evaluate(
                "() => ({ viewport: window.innerWidth, scroll: document.documentElement.scrollWidth })"
            )
            assert overflow["scroll"] <= overflow["viewport"] + 1, overflow
            target_sizes = page.locator(
                "[data-testid=comparison-mode-pair], [data-testid=comparison-mode-dataset], "
                "[data-testid=comparison-upload-a], [data-testid=comparison-upload-b], "
                "[data-testid=comparison-finalize], [data-testid=comparison-run]"
            ).evaluate_all(
                "items => items.filter(item => getComputedStyle(item).display !== 'none').map(item => ({ testid: item.getAttribute('data-testid'), width: item.getBoundingClientRect().width, height: item.getBoundingClientRect().height }))"
            )
            assert all(item["width"] >= 44 and item["height"] >= 44 for item in target_sizes), target_sizes

            if args.screenshot is not None:
                page.set_viewport_size({"width": 1500, "height": 1050})
                page.screenshot(path=str(args.screenshot), full_page=True)
            observed.update(
                {
                    "project_id": project_id,
                    "pair_ids": [
                        same_pair["pair_id"],
                        different_pair["pair_id"],
                        unrecorded_pair["pair_id"],
                    ],
                    "comparison_ids": [item["comparison_id"] for item in comparison_records],
                    "run_ids": [item["run_id"] for item in comparison_records],
                    "relationships": list(RELATIONSHIPS),
                    "upload_count": len(upload_requests),
                    "comparison_count": len(comparison_requests),
                    "no_project_inputs_disabled": ["a", "b"],
                    "history_locked_during_execution": True,
                    "history_sequence": {
                        "delayed_comparison_id": first_same_evidence["comparison_id"],
                        "latest_comparison_id": second_same_evidence["comparison_id"],
                        "retained_comparison_id": stale_history_id,
                    },
                    "structured_evidence_actions": structured_evidence_actions,
                    "ready_matcher_count": len(ready_matchers),
                    "source_acquisitions": source_acquisitions,
                    "ready_matcher_checkpoint_ids": sorted(acquired_ready),
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
