"""Validate owner-biometric purge and post-purge UI isolation with real inputs.

The gate uses a disposable QualSight data directory, the real local backend,
the built console, and hash-pinned LFW photographs. It records a working
sample, dataset, overlays, quality evidence, and a direct-pair comparison
before exercising the existing purge-scope and exact-confirmation endpoints
through the browser UI. A real non-image repository file exercises the
bounded invalid-media failure and retry path; no response is fabricated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import traceback
import urllib.parse

from controlled_inputs import lfw_tree_root
from validate_comparison_studio import (
    _assert_direct_evidence,
    _assert_pair_record,
    _assert_side,
    _browser_upload_record,
    _json_request,
    _port_is_free,
    _stop,
    _wait_http,
)


ROOT = Path(__file__).resolve().parents[1]
API_PORT = 8480
PREVIEW_PORT = 5197
NODE_EXECUTABLE = shutil.which("node")
if NODE_EXECUTABLE is None:
    raise RuntimeError("the reviewed Node executable is not available on PATH")
NODE_BIN = str(Path(NODE_EXECUTABLE).parent)
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"
LFW_ROOT = lfw_tree_root(ROOT)
LEFT_IMAGE = LFW_ROOT / "Aaron_Peirsol/Aaron_Peirsol_0001.jpg"
RIGHT_IMAGE = LFW_ROOT / "Aaron_Peirsol/Aaron_Peirsol_0002.jpg"
INVALID_MEDIA = ROOT / "resources" / "manifests" / "lfw-subset-fixture-v1.json"


def _uv_binary() -> str:
    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv is unavailable on PATH")
    return executable
EXPECTED_INPUTS = {
    LEFT_IMAGE: (
        "afb428f9939e4b545fc9982488afec825d262a4510b791f565bf3c4237605679",
        16_869,
    ),
    RIGHT_IMAGE: (
        "1b60c628765d7d681e2f1669b02be0f19476910b496d7768898ae4fd3720181d",
        20_589,
    ),
    INVALID_MEDIA: (
        "6d35c169199d83c12e0c76c90a0b1e67c432d0cf78df3aea66df293f65b8b6fe",
        6_917,
    ),
}
MATCHER_ID = "adaface-ir101-local-candidate"


def _assert_project_purge_frontend_contract() -> None:
    source = (ROOT / "frontend" / "src" / "ProjectPurge.tsx").read_text(
        encoding="utf-8"
    )
    assert "[project.project_id]" in source, (
        "ProjectPurge must invalidate its scope and pending action when the active "
        "project changes"
    )
    api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    purge_contract = api_source.split("export interface ProjectPurgeOut {", 1)[1].split("\n}", 1)[0]
    assert "reconciled_after_deletion_intent:" in purge_contract, (
        "ProjectPurgeOut omits the public crash-reconciliation outcome count"
    )


def _visible_text_has_path(page, *paths: Path) -> bool:
    text = page.locator("body").inner_text()
    return any(str(path) in text for path in paths)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    _assert_project_purge_frontend_contract()

    for path, (expected_hash, expected_size) in EXPECTED_INPUTS.items():
        if not path.is_file():
            raise FileNotFoundError(f"required real input is unavailable: {path}")
        payload = path.read_bytes()
        actual_hash = hashlib.sha256(payload).hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError(f"real input hash drifted: {path}: {actual_hash}")
        if len(payload) != expected_size:
            raise RuntimeError(
                f"real input size drifted: {path}: {len(payload)} != {expected_size}"
            )
    if not _port_is_free(API_PORT) or not _port_is_free(PREVIEW_PORT):
        raise RuntimeError(f"ports {API_PORT}/{PREVIEW_PORT} must be free")

    args.observation.parent.mkdir(parents=True, exist_ok=True)
    if args.screenshot is not None:
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)

    data_dir = Path(tempfile.mkdtemp(prefix="qualsight-owner-purge-ui-"))
    environment = {
        **os.environ,
        "PATH": f"{NODE_BIN}:{os.environ.get('PATH', '')}",
        "QUALSIGHT_REPOSITORY_ROOT": os.environ.get(
            "QUALSIGHT_REPOSITORY_ROOT", str(ROOT)
        ),
        "QUALSIGHT_DATA_DIR": str(data_dir),
    }
    api_process = subprocess.Popen(
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
    preview_process = subprocess.Popen(
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
        "kind": "QUALSIGHT_OWNER_PURGE_UI_VALIDATION",
        "real_inputs": [
            {
                "path": str(path),
                "sha256": expected_hash,
                "size_bytes": expected_size,
                "mtime_ns": path.stat().st_mtime_ns,
                "role": "invalid-media" if path == INVALID_MEDIA else "biometric-image",
            }
            for path, (expected_hash, expected_size) in EXPECTED_INPUTS.items()
        ],
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
                "name": "Owner biometric purge browser validation",
                "description": "Real LFW state-isolation and purge confirmation validation.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        assert isinstance(project, dict)
        project_id = str(project["project_id"])
        alternate_project = _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects",
            method="POST",
            body={
                "name": "Purge context-switch validation",
                "description": "Real project-context invalidation boundary.",
                "purpose_scope": RESEARCH_SCOPE,
            },
        )
        assert isinstance(alternate_project, dict)
        alternate_project_id = str(alternate_project["project_id"])
        _json_request(
            f"http://127.0.0.1:{API_PORT}/api/projects/{urllib.parse.quote(project_id, safe='')}/open",
            method="POST",
        )
        acquisition = _json_request(
            f"http://127.0.0.1:{API_PORT}/api/matcher-assets/{MATCHER_ID}/pull-sources",
            method="POST",
        )
        assert isinstance(acquisition, dict)
        assert acquisition["asset"]["matcher_id"] == MATCHER_ID
        assert acquisition["asset"]["ready_to_load"] is True

        from playwright.sync_api import sync_playwright

        purge_requests: list[dict] = []
        stale_geometry_responses: list[dict] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 1050})
            page.add_init_script(
                "window.localStorage.setItem('qualsight.activeProjectId', "
                f"{json.dumps(project_id)})"
            )

            def record_request(request) -> None:
                if request.method == "POST" and request.url.endswith("/purge"):
                    purge_requests.append(request.post_data_json)

            def record_response(response) -> None:
                response_path = urllib.parse.urlparse(response.url).path
                if response_path.startswith("/api/projects/") and response_path.endswith(
                    "/ofiq-landmarks"
                ):
                    stale_geometry_responses.append(
                        {"url": response.url, "status": response.status, "recorded_at": time.monotonic()}
                    )

            page.on("request", record_request)
            page.on("response", record_response)
            page.goto(
                f"http://127.0.0.1:{PREVIEW_PORT}/#/data",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            page.wait_for_selector("[data-testid=import-lfw]", timeout=30_000)
            page.click("[data-testid=import-lfw]")
            page.wait_for_selector("[data-testid=dataset-facts]", timeout=300_000)
            dataset_id = page.get_attribute("[data-context-key=dataset]", "data-dataset-id")
            assert dataset_id

            page.evaluate("() => { window.location.hash = '#/assess'; }")
            page.wait_for_selector("[data-testid=upload-input]", timeout=30_000)
            with page.expect_response(
                lambda response: "/api/quality/assess-upload" in response.url,
                timeout=300_000,
            ) as working_response:
                page.set_input_files("[data-testid=upload-input]", str(LEFT_IMAGE))
            assert working_response.value.status == 201
            page.wait_for_selector("[data-testid=working-sample-image]", timeout=30_000)
            page.wait_for_selector("[data-testid=measure-table]", timeout=60_000)
            assert page.locator("[data-testid=measure-table] tbody tr").count() == 28
            project_datasets = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/"
                f"{urllib.parse.quote(project_id, safe='')}/datasets"
            )
            assert isinstance(project_datasets, list)
            working_sample: dict | None = None
            for recorded_dataset in project_datasets:
                recorded_samples = _json_request(
                    f"http://127.0.0.1:{API_PORT}/api/projects/"
                    f"{urllib.parse.quote(project_id, safe='')}/datasets/"
                    f"{urllib.parse.quote(recorded_dataset['dataset_id'], safe='')}/samples"
                )
                assert isinstance(recorded_samples, list)
                working_sample = next(
                    (
                        sample
                        for sample in recorded_samples
                        if sample["sha256"] == EXPECTED_INPUTS[LEFT_IMAGE][0]
                        and sample["metadata"].get("source_kind")
                        == "OWNER_SUPPLIED_LOCAL_INPUT"
                    ),
                    working_sample,
                )
            assert working_sample is not None
            page.click("[data-testid=detection-toggle]")
            page.wait_for_selector("[data-testid=detection-overlay]", timeout=240_000)
            page.click("[data-testid=ofiq-landmark-toggle]")
            page.wait_for_selector("[data-testid=ofiq-landmark-overlay]", timeout=240_000)

            page.evaluate("() => { window.location.hash = '#/matchers'; }")
            page.wait_for_selector("[data-testid=comparison-studio]", timeout=30_000)
            pair_uploads: dict[str, dict] = {}
            for side, path in (("a", LEFT_IMAGE), ("b", RIGHT_IMAGE)):
                with page.expect_response(
                    lambda response: "/api/quality/assess-upload" in response.url,
                    timeout=300_000,
                ) as upload_response:
                    page.set_input_files(f"[data-testid=comparison-upload-{side}]", str(path))
                assert upload_response.value.status == 201
                pair_uploads[side] = _browser_upload_record(page, side, path)
                _assert_side(page, side, pair_uploads[side], path)
            page.check("input[name=comparison-relationship][value=SAME_PERSON]")
            with page.expect_response(
                lambda response: "/owner-pair-admissions" in response.url,
                timeout=60_000,
            ) as pair_response:
                page.click("[data-testid=comparison-finalize]")
            assert pair_response.value.status in {200, 201}
            pair = pair_response.value.json()
            _assert_pair_record(page, pair, "SAME_PERSON")
            page.select_option("[data-testid=comparison-matcher]", MATCHER_ID)
            page.wait_for_function(
                "() => document.querySelectorAll('[data-testid=comparison-preprocessing] option').length > 0",
                timeout=30_000,
            )
            page.click("[data-testid=comparison-run]")
            page.wait_for_function(
                "() => document.querySelector('[data-testid=direct-pair-evidence]')?.getAttribute('data-state') === 'ready'",
                timeout=300_000,
            )
            comparison_id = page.get_attribute(
                "[data-testid=direct-pair-evidence]", "data-comparison-id"
            )
            assert comparison_id
            evidence = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/"
                f"{urllib.parse.quote(project_id, safe='')}/owner-pair-comparisons/"
                f"{urllib.parse.quote(comparison_id, safe='')}/evidence"
            )
            assert isinstance(evidence, dict)
            _assert_direct_evidence(page, evidence, "SAME_PERSON")

            page.evaluate("() => { window.location.hash = '#/assess'; }")
            page.wait_for_selector("[data-testid=working-sample-image]", timeout=30_000)
            with page.expect_response(
                lambda response: "/api/quality/assess-upload" in response.url,
                timeout=60_000,
            ) as invalid_response:
                page.set_input_files("[data-testid=upload-input]", str(INVALID_MEDIA))
            assert invalid_response.value.status in {400, 415, 422}
            page.wait_for_selector("[data-testid=upload-error]", timeout=30_000)
            invalid_error = page.inner_text("[data-testid=upload-error]")

            page.evaluate("() => { window.location.hash = '#/projects'; }")
            page.wait_for_selector("[data-testid=projects-panel]", timeout=30_000)
            purge_open = page.locator("[data-testid=project-purge-open]")
            assert purge_open.count() == 1, (
                "active project does not expose the scoped owner-data purge action"
            )

            page.evaluate("() => { window.location.hash = '#/assess'; }")
            page.wait_for_selector("[data-testid=upload-error]", timeout=30_000)
            retry = page.locator("[data-testid=upload-retry]")
            assert retry.count() == 1 and retry.is_visible(), (
                "invalid-media failure has no visible safe retry"
            )
            assert not _visible_text_has_path(page, data_dir, LEFT_IMAGE, RIGHT_IMAGE, INVALID_MEDIA), (
                "owner-image failure/inspector exposed an absolute filesystem path"
            )
            assert str(data_dir) not in invalid_error and str(INVALID_MEDIA) not in invalid_error
            with page.expect_response(
                lambda response: "/api/quality/assess-upload" in response.url,
                timeout=300_000,
            ) as retry_response:
                with page.expect_file_chooser(timeout=30_000) as chooser:
                    retry.click()
                chooser.value.set_files(str(LEFT_IMAGE))
            assert retry_response.value.status in {200, 201}
            page.wait_for_selector("[data-testid=upload-result]", timeout=30_000)

            # Start one genuine geometry request immediately before opening the
            # destructive flow. The post-purge assertions below wait long enough
            # for any real late response and refuse state resurrection.
            if page.locator("[data-testid=ofiq-landmark-overlay]").count() == 1:
                page.click("[data-testid=ofiq-landmark-toggle]")
            expected_landmark_path = (
                f"/api/projects/{urllib.parse.quote(project_id, safe='')}/samples/"
                f"{urllib.parse.quote(str(working_sample['sample_id']), safe='')}/ofiq-landmarks"
            )
            page.evaluate(
                """expectedPath => {
                    const originalFetch = window.fetch.bind(window);
                    window.__qualsightDelayedGeometry = { received: false, released: false };
                    window.fetch = async (input, init) => {
                        const rawUrl = typeof input === 'string' ? input : input.url;
                        const response = await originalFetch(input, init);
                        if (new URL(rawUrl, window.location.href).pathname !== expectedPath) return response;
                        window.__qualsightDelayedGeometry.received = true;
                        return await new Promise(resolve => {
                            window.__qualsightReleaseGeometry = () => {
                                window.__qualsightDelayedGeometry.released = true;
                                resolve(response);
                            };
                        });
                    };
                }""",
                expected_landmark_path,
            )
            with page.expect_request(
                lambda request: urllib.parse.urlparse(request.url).path
                == expected_landmark_path,
                timeout=30_000,
            ):
                page.click("[data-testid=ofiq-landmark-toggle]")
            page.wait_for_function(
                "() => window.__qualsightDelayedGeometry?.received === true",
                timeout=30_000,
            )

            page.evaluate("() => { window.location.hash = '#/projects'; }")
            with page.expect_response(
                lambda response: response.url.endswith("/purge-scope"),
                timeout=30_000,
            ) as scope_response:
                page.click("[data-testid=project-purge-open]")
            assert scope_response.value.status == 200
            scope_record = scope_response.value.json()
            assert scope_record["project_id"] == project_id
            dialog = page.locator("[data-testid=project-purge-dialog]")
            dialog.wait_for(state="visible", timeout=30_000)
            scope_text = page.inner_text("[data-testid=project-purge-scope]")
            assert scope_record["scope"] in scope_text
            assert str(data_dir) not in scope_text
            assert "targets" not in scope_record
            assert scope_record["target_counts"]["files"] > 0

            page.select_option("[data-testid=project-select]", alternate_project_id)
            dialog.wait_for(state="hidden", timeout=30_000)
            assert page.locator("[data-testid=project-purge-scope]").count() == 0, (
                "purge scope from the previous project remained mounted after context change"
            )
            page.select_option("[data-testid=project-select]", project_id)
            page.wait_for_selector("[data-testid=project-purge-open]", timeout=30_000)
            with page.expect_response(
                lambda response: response.url.endswith("/purge-scope"),
                timeout=30_000,
            ) as reopened_scope_response:
                page.click("[data-testid=project-purge-open]")
            assert reopened_scope_response.value.status == 200
            scope_record = reopened_scope_response.value.json()
            assert scope_record["project_id"] == project_id
            dialog.wait_for(state="visible", timeout=30_000)

            confirmation = page.locator("[data-testid=project-purge-confirmation]")
            confirm_button = page.locator("[data-testid=project-purge-confirm]")
            confirmation.fill("wrong-project")
            assert confirm_button.is_disabled()
            assert purge_requests == []
            confirmation.fill(project_id)
            assert not confirm_button.is_disabled()
            with page.expect_response(
                lambda response: response.url.endswith("/purge"),
                timeout=120_000,
            ) as purge_response:
                confirm_button.click()
            assert purge_response.value.status == 201
            purge_record = purge_response.value.json()
            assert purge_record["verified"] is True
            assert purge_record["project_id"] == project_id
            assert purge_requests == [{"confirm": project_id}]
            purge_completed_at = time.monotonic()
            page.evaluate("() => window.__qualsightReleaseGeometry()")
            geometry_released_at = time.monotonic()
            assert geometry_released_at > purge_completed_at
            page.wait_for_selector(
                "[data-testid=project-purge-result][data-verified=true]", timeout=30_000
            )
            recorded_purges = _json_request(
                f"http://127.0.0.1:{API_PORT}/api/projects/"
                f"{urllib.parse.quote(project_id, safe='')}/purges"
            )
            assert isinstance(recorded_purges, list) and recorded_purges
            assert recorded_purges[-1]["verified"] is True
            assert recorded_purges[-1]["scope"] == scope_record["scope"]
            assert page.evaluate(
                "() => window.localStorage.getItem('qualsight.activeProjectId')"
            ) is None

            page.evaluate("() => { window.location.hash = '#/assess'; }")
            page.wait_for_timeout(3_000)
            assert page.locator("[data-testid=working-sample-image]").count() == 0
            assert page.locator("[data-testid=detection-overlay]").count() == 0
            assert page.locator("[data-testid=ofiq-landmark-overlay]").count() == 0
            assert page.locator("[data-testid=measure-table]").count() == 0
            assert page.locator("[data-testid=upload-result]").count() == 0
            assert page.locator("[data-testid=dataset-facts]").count() == 0

            page.evaluate("() => { window.location.hash = '#/matchers'; }")
            page.wait_for_timeout(2_000)
            for side in ("a", "b"):
                assert not page.get_attribute(
                    f"[data-testid=comparison-side-{side}]", "data-sample-id"
                )
            assert page.locator("[data-testid=comparison-pair-record]").count() == 0
            assert page.locator("[data-testid=direct-pair-evidence]").count() == 0
            history = page.locator("[data-testid=comparison-history]")
            assert history.count() == 1
            assert history.locator("[data-testid=comparison-history-item]").count() == 0
            assert "no direct comparisons" in history.inner_text().lower()
            delayed_geometry_state = page.evaluate(
                "() => window.__qualsightDelayedGeometry"
            )
            assert delayed_geometry_state == {"received": True, "released": True}

            if args.screenshot is not None:
                page.screenshot(path=str(args.screenshot), full_page=True)
            browser.close()

        observed.update(
            {
                "passed": True,
                "project_id": project_id,
                "dataset_id": dataset_id,
                "working_sample_id": working_sample["sample_id"],
                "pair_id": pair["pair_id"],
                "comparison_id": comparison_id,
                "purge_id": purge_record["purge_id"],
                "purge_verified": purge_record["verified"],
                "purge_request": purge_requests[0],
                "invalid_media_status": invalid_response.value.status,
                "geometry_responses": stale_geometry_responses,
                "purge_completed_at": purge_completed_at,
                "geometry_released_at": geometry_released_at,
                "delayed_geometry_state": delayed_geometry_state,
            }
        )
        exit_code = 0
    except Exception as error:
        observed["failure"] = {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exc(),
        }
    finally:
        _stop(preview_process)
        _stop(api_process)
        if api_process.stdout is not None:
            observed["api_tail"] = api_process.stdout.read().decode(
                "utf-8", errors="replace"
            )[-12_000:]
        if preview_process.stdout is not None:
            observed["preview_tail"] = preview_process.stdout.read().decode(
                "utf-8", errors="replace"
            )[-4_000:]
        args.observation.write_text(
            json.dumps(observed, indent=2) + "\n", encoding="utf-8"
        )
        shutil.rmtree(data_dir, ignore_errors=True)

    if exit_code == 0:
        print(json.dumps(observed, indent=2))
    else:
        print(json.dumps(observed.get("failure", observed), indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
