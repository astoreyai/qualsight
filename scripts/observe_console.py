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
            page.keyboard.press("Tab")
            first_focus = page.evaluate("() => ({text: document.activeElement?.textContent?.trim(), href: document.activeElement?.getAttribute('href')})")
            if first_focus != {"text": "Skip to main content", "href": "#main-content"}:
                raise AssertionError(f"first keyboard focus is not the main-content skip link: {first_focus}")

            def goto(view: str) -> None:
                """QS-Y89: navigate the routed console; exactly one view is visible."""
                page.evaluate(f"() => {{ window.location.hash = '#/{view}' }}")
                page.wait_for_function(
                    "(v) => { const shown = Array.from(document.querySelectorAll('main > div[data-view]')).filter(d => getComputedStyle(d).display !== 'none'); return shown.length >= 1 && shown.every(d => (d.getAttribute('data-view') || '').startsWith(v) || (v === 'projects' && (d.getAttribute('data-view') || '').startsWith('projects'))); }",
                    arg=view,
                    timeout=30_000,
                )

            # QS-Y89: routed navigation — every route shows exactly one view; empty states honest on a fresh store.
            nav_checks = []
            for target in ("system", "utility", "cross", "reports", "advanced", "matchers", "data", "assess", "projects"):
                goto(target)
                nav_checks.append({"view": target, "visible_views": page.evaluate("() => Array.from(document.querySelectorAll('main > div[data-view]')).filter(d => getComputedStyle(d).display !== 'none').map(d => d.getAttribute('data-view'))"), "current_links": page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=primary-nav] [aria-current=page]')).map(d => ({label: d.textContent?.trim(), view: d.getAttribute('data-view')}))")})
            nav_views = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=primary-nav] [data-testid=nav-link]')).map(d => d.getAttribute('data-view'))")
            if len(nav_views) != 9 or len(set(nav_views)) != 9:
                raise AssertionError(f"primary navigation must expose nine unique destinations: {nav_views}")
            if any(check["current_links"] != [{"label": next(label for view, label in {
                "projects": "Projects", "data": "Datasets & batches", "assess": "Sample quality", "matchers": "Verification",
                "advanced": "Identification & robustness", "utility": "Quality utility", "cross": "Cross-matcher",
                "reports": "Reports & reproduction", "system": "Add-ons & system",
            }.items() if view == check["view"]), "view": check["view"]}] for check in nav_checks):
                raise AssertionError(f"navigation current state diverged from the routed view: {nav_checks}")
            goto("utility")
            empty_state = page.inner_text("main div[data-view=utility]")
            empty_recovery = page.get_attribute("main div[data-view=utility] .empty-state a", "href")
            if empty_recovery != "#/projects":
                raise AssertionError(f"utility empty state has no direct project recovery action: {empty_recovery}")
            goto("system")
            page.wait_for_selector("[data-testid=matcher-assets] .asset-card", timeout=60_000)
            system_assets = page.evaluate("() => document.querySelectorAll('[data-testid=matcher-assets] .asset-card').length")
            dataset_addons = page.evaluate("() => document.querySelectorAll('[data-testid=dataset-addons] .asset-card').length")
            preprocessing_addons = page.evaluate("() => document.querySelectorAll('[data-testid=preprocessing-addons] .asset-card').length")
            transform_addons = page.evaluate("() => document.querySelectorAll('[data-testid=transform-addons] .resource-card').length")
            addon_catalog_api = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/addons"))  # noqa: S310
            dom_addon_ids = {
                "matchers": sorted(page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=matcher-assets] [data-matcher-id]')).map(e => e.getAttribute('data-matcher-id'))")),
                "datasets": sorted(page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=dataset-addons] [data-addon-id]')).map(e => e.getAttribute('data-addon-id'))")),
                "preprocessing": sorted(page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=preprocessing-addons] [data-addon-id]')).map(e => e.getAttribute('data-addon-id'))")),
                "transforms": sorted(page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=transform-addons] [data-addon-id]')).map(e => e.getAttribute('data-addon-id'))")),
            }
            api_addon_ids = {
                "matchers": sorted(item["matcher_id"] for item in addon_catalog_api["matchers"]),
                "datasets": sorted(item["addon_id"] for item in addon_catalog_api["datasets"]),
                "preprocessing": sorted(item["addon_id"] for item in addon_catalog_api["preprocessing"]),
                "transforms": sorted(item["addon_id"] for item in addon_catalog_api["transforms"]),
            }
            system_tabs = page.get_by_role("tab").all_text_contents()
            page.get_by_role("tab", name="Connections", exact=True).click()
            connection_text = page.inner_text("[data-testid=connection-addons]")
            if (system_assets, dataset_addons, preprocessing_addons, transform_addons) != (6, 4, 3, 7):
                raise AssertionError(
                    f"unexpected closed add-on inventory: matchers={system_assets}, datasets={dataset_addons}, "
                    f"preprocessing={preprocessing_addons}, transforms={transform_addons}"
                )
            if dom_addon_ids != api_addon_ids:
                raise AssertionError(f"console add-on IDs diverge from the API catalog: dom={dom_addon_ids}, api={api_addon_ids}")
            if addon_catalog_api["connections"] != [] or addon_catalog_api["connection_policy"]["state"] != "NOT_ADMITTED_IN_BETA" or "No API connections are admitted in this beta" not in connection_text:
                raise AssertionError(f"connection policy was not visible: {connection_text}")
            system_resources = page.evaluate("() => document.querySelectorAll('[data-testid=resource-table] .resource-card').length")
            page.get_by_role("tab", name="Add-ons", exact=True).click()
            observation["steps"].append({"step": "load", "title": page.title(), "manifest": manifest, "primary_nav_links": len(nav_views), "primary_nav_unique_views": len(set(nav_views)), "first_keyboard_focus": first_focus, "system_tabs": system_tabs, "system_panel_matcher_assets": system_assets, "system_panel_dataset_addons": dataset_addons, "system_panel_preprocessing_addons": preprocessing_addons, "system_panel_transform_addons": transform_addons, "addon_ids_match_api": dom_addon_ids == api_addon_ids, "system_panel_connection_policy": connection_text, "system_panel_resources": system_resources, "routes": nav_checks, "fresh_store_empty_state": empty_state[:200], "fresh_store_empty_recovery": empty_recovery})
            page.wait_for_selector("[data-testid=engine-badge] .value", timeout=30_000)
            badge = page.inner_text("[data-testid=engine-badge]")
            observation["steps"].append({"step": "engine-badge", "text": badge})
            # Matcher sources are an explicit operator action. Scientific jobs
            # must never acquire them as an implicit side effect.
            asset_cards = page.locator("[data-testid=matcher-assets] .asset-card")
            elasticface_card = page.locator(
                "[data-testid=matcher-assets] [data-matcher-id='elasticface-arc-r100-local-candidate']"
            )
            managed_checkpoint = elasticface_card.get_by_role(
                "button", name="Install managed checkpoint"
            )
            if managed_checkpoint.count() != 1 or not managed_checkpoint.is_enabled():
                raise AssertionError("ElasticFace managed-checkpoint action is not available")
            managed_checkpoint.click()
            elasticface_card.get_by_text("Managed checkpoint verified", exact=True).wait_for(
                timeout=120_000
            )
            acquisition_status = page.locator(
                "[data-testid=system-panel] > p.status"
            ).filter(has_text="matcher checkpoint acquisition completed")
            if acquisition_status.count() != 1:
                raise AssertionError(
                    f"checkpoint action exposed {acquisition_status.count()} live status banners"
                )
            acquisition_message = acquisition_status.inner_text()
            if "evidence" not in acquisition_message:
                raise AssertionError(
                    f"checkpoint action did not expose its durable event evidence: {acquisition_message}"
                )
            for index in range(asset_cards.count()):
                card = asset_cards.nth(index)
                pull = card.get_by_role("button", name="Pull pinned sources")
                if pull.count() == 1 and pull.is_enabled():
                    pull.click()
                    card.get_by_text("Sources verified", exact=True).wait_for(timeout=120_000)
            first_asset = asset_cards.first
            first_asset.get_by_role("button", name="Load matcher").click()
            page.wait_for_function(
                """() => {
                    const buttons = Array.from(document.querySelectorAll(
                        '[data-testid=matcher-assets] .asset-actions button'
                    ));
                    return buttons.length > 1 && buttons.every(button => button.disabled);
                }""",
                timeout=30_000,
            )
            lifecycle_buttons_while_loading = page.evaluate(
                """() => Array.from(document.querySelectorAll(
                    '[data-testid=matcher-assets] .asset-actions button'
                )).map(button => ({label: button.textContent?.trim(), disabled: button.disabled}))"""
            )
            if not lifecycle_buttons_while_loading or any(
                not button["disabled"] for button in lifecycle_buttons_while_loading
            ):
                raise AssertionError(
                    "matcher lifecycle controls were not globally disabled during a real load: "
                    f"{lifecycle_buttons_while_loading}"
                )
            page.wait_for_function(
                "card => card.querySelector('.asset-title .state-pill')?.textContent?.trim() === 'LOADED'",
                arg=first_asset.element_handle(),
                timeout=180_000,
            )
            loaded_runtime = first_asset.locator("dt", has_text="Runtime").locator("xpath=following-sibling::dd[1]").inner_text()
            real_load_observed = "parameters" in loaded_runtime and "not resident" not in loaded_runtime
            if not real_load_observed:
                raise AssertionError(f"matcher reported LOADED without a resident runtime: {loaded_runtime}")
            first_asset.get_by_role("button", name="Unload").click()
            page.wait_for_function(
                "card => card.querySelector('.asset-title .state-pill')?.textContent?.trim() === 'UNLOADED'",
                arg=first_asset.element_handle(),
                timeout=120_000,
            )
            unloaded_runtime = first_asset.locator("dt", has_text="Runtime").locator("xpath=following-sibling::dd[1]").inner_text()
            unload_observed = "not resident" in unloaded_runtime
            if not unload_observed:
                raise AssertionError(f"matcher reported UNLOADED while runtime appeared resident: {unloaded_runtime}")
            observation["steps"].append(
                {
                    "step": "matcher-lifecycle",
                    "assets": system_assets,
                    "elasticface_managed_checkpoint": True,
                    "checkpoint_event_message": acquisition_message,
                    "sources_verified": page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=matcher-assets] .asset-card')).every(card => card.innerText.includes('Sources verified'))"),
                    "all_lifecycle_buttons_disabled_during_load": all(
                        button["disabled"] for button in lifecycle_buttons_while_loading
                    ),
                    "lifecycle_buttons_during_load": lifecycle_buttons_while_loading,
                    "loaded_runtime": loaded_runtime,
                    "real_load_observed": real_load_observed,
                    "unloaded_runtime": unloaded_runtime,
                    "unload_observed": unload_observed,
                }
            )
            goto("assess")
            page.wait_for_selector("img[alt^='Registered sample']", timeout=30_000)
            image_ok = page.evaluate(
                "() => { const i = document.querySelector('img[alt^=\"Registered sample\"]'); return i && i.complete && i.naturalWidth > 0; }"
            )
            observation["steps"].append({"step": "image-rendered", "natural_size_ok": bool(image_ok)})
            # QS-Y85: matcher-pipeline detection overlay on the registered sample.
            page.click("[data-testid=detection-toggle]")
            page.wait_for_selector("[data-testid=detection-overlay]", state="attached", timeout=60_000)
            overlay_counts = page.evaluate(
                "() => ({ boxes: document.querySelectorAll('[data-testid=detection-overlay] rect').length, points: document.querySelectorAll('[data-testid=detection-overlay] circle').length })"
            )
            detection_label = page.inner_text("[data-testid=detection-label]")
            observation["steps"].append(
                {
                    "step": "detection-overlay",
                    "boxes": overlay_counts["boxes"],
                    "landmark_points": overlay_counts["points"],
                    "labelled_as_matcher_pipeline": "not OFIQ's internal landmarks" in detection_label,
                }
            )
            page.click("[data-testid=detection-toggle]")
            # QS-Z90: OFIQ's own internal ADNet-98 landmarks through the registered auxiliary binary.
            page.click("[data-testid=ofiq-landmark-toggle]")
            page.wait_for_selector("[data-testid=ofiq-landmark-overlay]", state="attached", timeout=120_000)
            ofiq_counts = page.evaluate(
                "() => ({ faces: document.querySelectorAll('[data-testid=ofiq-landmark-overlay] rect').length, points: document.querySelectorAll('[data-testid=ofiq-landmark-overlay] circle').length })"
            )
            ofiq_label = page.inner_text("[data-testid=ofiq-landmark-label]")
            observation["steps"].append(
                {
                    "step": "ofiq-landmarks",
                    "faces": ofiq_counts["faces"],
                    "landmark_points": ofiq_counts["points"],
                    "labelled_as_ofiq_internal": "distinct from the matcher pipeline" in ofiq_label,
                }
            )
            page.click("[data-testid=ofiq-landmark-toggle]")
            # QS-B10: create a project from the console and record the run into it.
            goto("projects")
            observation_project_name = f"Console lifecycle observation — {args.observation.stem}"
            page.fill("[data-testid=project-name-input]", observation_project_name)
            page.click("[data-testid=project-create]")
            page.wait_for_selector("[data-testid=project-actions]", timeout=30_000)
            created_message = page.inner_text("[data-testid=project-message]")
            project_context = page.inner_text("[data-testid=experiment-context]")
            if observation_project_name not in project_context or "No dataset selected" not in project_context:
                raise AssertionError(f"project context did not update after project creation: {project_context}")
            observation["steps"].append({"step": "project-created", "message": created_message, "context": project_context})
            console_project_id_holder = [next(p["project_id"] for p in json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects")) if p["name"] == observation_project_name)]  # noqa: S310

            def recorded_run_count() -> int:
                projects = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects"))  # noqa: S310
                project = next(item for item in projects if item["project_id"] == console_project_id_holder[0])
                return len(project["run_ids"])

            goto("assess")
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
            goto("projects")
            page.wait_for_selector("[data-testid=run-table]", timeout=30_000)
            observation["steps"].append({"step": "run-list", "rows": page.evaluate("() => document.querySelectorAll('[data-testid=run-table] tbody tr').length")})
            # QS-Y81 (decision 30): upload an operator-picked real photo through the
            # console file picker and observe the recorded owner-supplied Run.
            upload_source = Path("/mnt/projects/datasets/lfw/lfw_funneled/Aaron_Peirsol/Aaron_Peirsol_0001.jpg")
            upload_copy = Path(tempfile.mkdtemp(prefix="qualsight-upload-")) / "evaluator_photo.jpg"
            shutil.copyfile(upload_source, upload_copy)
            goto("assess")
            page.set_input_files("[data-testid=upload-input]", str(upload_copy))
            page.wait_for_selector("[data-testid=upload-result]", timeout=180_000)
            uqs_text = page.inner_text("[data-testid=upload-uqs]")
            upload_text = page.inner_text("[data-testid=upload-result]")
            observation["steps"].append(
                {
                    "step": "owner-supplied-upload",
                    "file": upload_copy.name,
                    "unified_quality_score": uqs_text,
                    "source_kind_shown": "OWNER_SUPPLIED_LOCAL_INPUT" in upload_text,
                    "run_link_present": bool(page.get_attribute("[data-testid=upload-result] a", "href")),
                }
            )
            shutil.rmtree(upload_copy.parent, ignore_errors=True)
            upload_run_count = recorded_run_count()
            upload_run_label = f"{upload_run_count} run{'s' if upload_run_count != 1 else ''}"
            page.wait_for_function(
                "expected => (document.querySelector('[data-testid=experiment-context]')?.innerText || '').includes(expected)",
                arg=upload_run_label,
                timeout=30_000,
            )
            upload_context = page.inner_text("[data-testid=experiment-context]")
            goto("projects")
            page.wait_for_function(
                "expected => document.querySelectorAll('[data-testid=run-table] tbody tr').length === expected",
                arg=upload_run_count,
                timeout=30_000,
            )
            observation["steps"].append(
                {
                    "step": "owner-upload-global-refresh",
                    "api_run_count": upload_run_count,
                    "context": upload_context,
                    "run_table_rows": page.evaluate("() => document.querySelectorAll('[data-testid=run-table] tbody tr').length"),
                }
            )
            # A fresh observation store cannot prove project-scoped Run switching
            # unless it creates a second real project and executes a real Run in it.
            goto("projects")
            switch_project_name = f"Project switch isolation — {args.observation.stem}"
            page.fill("[data-testid=project-name-input]", switch_project_name)
            page.click("[data-testid=project-create]")
            page.wait_for_function(
                "name => (document.querySelector('[data-testid=experiment-context]')?.innerText || '').includes(name)",
                arg=switch_project_name,
                timeout=30_000,
            )
            switch_project_id = next(
                project["project_id"]
                for project in json.load(
                    urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects")  # noqa: S310
                )
                if project["name"] == switch_project_name
            )
            goto("assess")
            page.click("[data-testid=assess-button]")
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                projects_after_assessment = json.load(
                    urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects")  # noqa: S310
                )
                switch_project = next(
                    project for project in projects_after_assessment if project["project_id"] == switch_project_id
                )
                if switch_project["run_ids"]:
                    break
                page.wait_for_timeout(250)
            else:
                raise AssertionError("the second real project assessment did not record a Run")
            observation["steps"].append(
                {
                    "step": "second-project-real-run",
                    "project_id": switch_project_id,
                    "run_count": len(switch_project["run_ids"]),
                }
            )
            goto("projects")
            page.select_option("[data-testid=project-select]", console_project_id_holder[0])
            project_inventory = json.load(
                urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects")  # noqa: S310
            )
            switch_target = next(
                (
                    item
                    for item in sorted(project_inventory, key=lambda value: (-len(value["run_ids"]), value["project_id"]))
                    if item["project_id"] != console_project_id_holder[0] and item["run_ids"]
                ),
                None,
            )
            if switch_target is None:
                raise AssertionError("the populated-store project-switch gate has no second project with recorded Runs")

            def project_run_ids(project_id: str) -> list[str]:
                query = urllib.parse.urlencode({"project_id": project_id, "limit": 10_000})
                rows = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/runs?{query}"))  # noqa: S310
                return [row["run_id"] for row in rows]

            guarded_projects = {
                console_project_id_holder[0]: project_run_ids(console_project_id_holder[0]),
                switch_target["project_id"]: project_run_ids(switch_target["project_id"]),
            }
            page.evaluate(
                """guarded => {
                    window.__runScopeViolations = [];
                    window.__runScopeObserver?.disconnect();
                    const inspect = () => {
                        const selected = document.querySelector('[data-testid=project-select]')?.value || '';
                        if (!(selected in guarded)) return;
                        const allowed = new Set(guarded[selected]);
                        const observed = Array.from(document.querySelectorAll('[data-testid=run-table] tbody a'))
                            .map(link => decodeURIComponent(new URL(link.href).pathname.split('/').pop() || ''));
                        const foreign = observed.filter(runId => !allowed.has(runId));
                        if (foreign.length) window.__runScopeViolations.push({selected, foreign});
                    };
                    window.__runScopeObserver = new MutationObserver(inspect);
                    window.__runScopeObserver.observe(document.querySelector('main'), {subtree: true, childList: true, attributes: true});
                    inspect();
                }""",
                guarded_projects,
            )
            page.select_option("[data-testid=project-select]", switch_target["project_id"])
            page.wait_for_function(
                "expected => document.querySelector('[data-testid=project-select]')?.value === expected.projectId && document.querySelectorAll('[data-testid=run-table] tbody tr').length === expected.count",
                arg={"projectId": switch_target["project_id"], "count": len(guarded_projects[switch_target["project_id"]])},
                timeout=30_000,
            )
            page.select_option("[data-testid=project-select]", console_project_id_holder[0])
            page.wait_for_function(
                "expected => document.querySelector('[data-testid=project-select]')?.value === expected.projectId && document.querySelectorAll('[data-testid=run-table] tbody tr').length === expected.count",
                arg={"projectId": console_project_id_holder[0], "count": len(guarded_projects[console_project_id_holder[0]])},
                timeout=30_000,
            )
            run_scope_violations = page.evaluate(
                "() => { window.__runScopeObserver?.disconnect(); return window.__runScopeViolations; }"
            )
            if run_scope_violations:
                raise AssertionError(f"project switching exposed Runs from the prior project: {run_scope_violations}")
            observation["steps"].append(
                {
                    "step": "project-run-switch-isolation",
                    "switched_to_project_id": switch_target["project_id"],
                    "switched_to_run_count": len(guarded_projects[switch_target["project_id"]]),
                    "restored_project_id": console_project_id_holder[0],
                    "restored_run_count": len(guarded_projects[console_project_id_holder[0]]),
                    "scope_violations": run_scope_violations,
                }
            )
            # Save, close, reopen, clone, archive from the console; each transition is a new version.
            goto("projects")
            transitions = []
            for label in ("Save", "Close", "Open", "Archive"):
                page.click(f"[data-testid=project-actions] button:has-text('{label}')")
                if label == "Archive":
                    page.get_by_role("button", name="Confirm archive", exact=True).click()
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
            goto("projects")
            page.click("[data-testid=project-actions] button:has-text('Open')")
            page.wait_for_function(
                "() => (document.querySelector('[data-testid=project-message]')?.innerText || '').toLowerCase().startsWith('open')",
                timeout=30_000,
            )
            goto("data")
            page.click("[data-testid=import-lfw]")
            page.wait_for_selector("[data-testid=dataset-facts]", timeout=300_000)
            dataset_context = page.inner_text("[data-testid=experiment-context]")
            if "Local LFW funneled image collection" not in dataset_context or "13,233 samples" not in dataset_context:
                raise AssertionError(f"dataset context did not update after import: {dataset_context}")
            observation["steps"].append({"step": "dataset-imported", "facts": page.inner_text("[data-testid=dataset-facts]"), "context": dataset_context})
            page.select_option("[data-testid=batch-selection]", "subset_fixture")
            page.click("[data-testid=batch-start]")
            page.wait_for_selector("[data-testid=job-progress]", timeout=30_000)
            page.wait_for_function(
                "() => /^(COMPLETED|CANCELLED|FAILED)/.test(document.querySelector('[data-testid=job-progress] p')?.innerText || '')",
                timeout=600_000,
            )
            observation["steps"].append({"step": "batch", "progress": page.inner_text("[data-testid=job-progress]")})
            batch_run_count = recorded_run_count()
            batch_run_label = f"{batch_run_count} run{'s' if batch_run_count != 1 else ''}"
            page.wait_for_function(
                "expected => (document.querySelector('[data-testid=experiment-context]')?.innerText || '').includes(expected)",
                arg=batch_run_label,
                timeout=30_000,
            )
            batch_context = page.inner_text("[data-testid=experiment-context]")
            goto("projects")
            page.wait_for_function(
                "expected => document.querySelectorAll('[data-testid=run-table] tbody tr').length === expected",
                arg=batch_run_count,
                timeout=30_000,
            )
            observation["steps"].append(
                {
                    "step": "batch-terminal-global-refresh",
                    "api_run_count": batch_run_count,
                    "context": batch_context,
                    "run_table_rows": page.evaluate("() => document.querySelectorAll('[data-testid=run-table] tbody tr').length"),
                }
            )
            goto("data")
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
            # QS-C20..C24: freeze the protocol, run one catalog-selected
            # matcher/preprocessing leaf over the real fixture pairs, and inspect
            # its error list. Keep this reduced selection for every later view.
            goto("matchers")
            page.wait_for_selector("[data-testid=addon-selection] input", timeout=60_000)
            matcher_checkboxes = page.locator("[data-testid=addon-selection] > div").nth(0).locator("input")
            profile_checkboxes = page.locator("[data-testid=addon-selection] > div").nth(1).locator("input")
            if matcher_checkboxes.count() != 6 or profile_checkboxes.count() != 2:
                raise AssertionError("the matcher page did not expose all six matchers and both preprocessing profiles")
            if any(not matcher_checkboxes.nth(index).is_checked() for index in range(matcher_checkboxes.count())) or any(not profile_checkboxes.nth(index).is_checked() for index in range(profile_checkboxes.count())):
                raise AssertionError("the matcher page did not initialize all admitted matcher and preprocessing add-ons")
            for index in range(1, matcher_checkboxes.count()):
                matcher_checkboxes.nth(index).uncheck()
            profile_checkboxes.nth(1).uncheck()
            page.wait_for_function(
                "() => (document.querySelector('[data-testid=addon-selection]')?.innerText || '').includes('1 immutable Run')",
                timeout=30_000,
            )
            selected_one = "1 immutable Run" in page.inner_text("[data-testid=addon-selection]")
            selected_count = sum(matcher_checkboxes.nth(index).is_checked() for index in range(matcher_checkboxes.count())) + sum(profile_checkboxes.nth(index).is_checked() for index in range(profile_checkboxes.count()))
            if not selected_one or selected_count != 2:
                raise AssertionError("the catalog filters did not retain exactly one matcher and one preprocessing profile")
            page.click("[data-testid=freeze-protocol]")
            page.wait_for_selector("[data-testid=protocol-summary]", timeout=120_000)
            page.click("[data-testid=matrix-fixture]")
            page.wait_for_function(
                "() => /completed|failed/.test(document.querySelector('[data-testid=matrix-progress]')?.innerText || '')",
                timeout=900_000,
            )
            page.wait_for_selector("[data-testid=matrix-table]", timeout=60_000)
            matrix_progress = page.inner_text("[data-testid=matrix-progress]")
            if "completed" not in matrix_progress.lower() or "1/1 leaves" not in matrix_progress:
                raise AssertionError(f"the reduced matrix did not complete exactly one leaf: {matrix_progress}")
            cells = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=matrix-table] button.cell')).map(b => b.innerText)")
            if len(cells) != 1 or not page.query_selector("[data-testid=leaf-QS-C22\\.AS]"):
                raise AssertionError(f"the reduced matrix did not expose only QS-C22.AS: {cells}")
            det_polylines = page.evaluate("() => document.querySelectorAll('[data-testid=det-plot] polyline').length")
            if det_polylines < 1:
                raise AssertionError("the reduced real matrix rendered no DET curve")
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
                    "catalog_selection": {
                        "controls": matcher_checkboxes.count() + profile_checkboxes.count(),
                        "selected_matchers": 1,
                        "selected_preprocessing_profiles": 1,
                        "one_leaf_selection_observed": selected_one,
                    },
                    "matrix_progress": matrix_progress,
                    "leaf_cells": cells,
                    "det_polylines": det_polylines,
                    "error_pairs_at_0_9": error_pairs[:6],
                }
            )
            # QS-Y86/Y87/Z91/Z92: open the recorded match evidence for the first error pair.
            page.click("[data-testid=evidence-open]")
            page.wait_for_selector("[data-testid=pair-evidence]", timeout=120_000)
            page.wait_for_selector("[data-testid=evidence-crop-left]", timeout=120_000)
            page.wait_for_selector("[data-testid=score-distribution]", state="attached", timeout=60_000)
            crops_ok = page.evaluate(
                "() => ['left','right'].map(s => { const i = document.querySelector(`[data-testid=evidence-crop-${s}]`); return i && i.naturalWidth === 112 && i.naturalHeight === 112; })"
            )
            evidence_statement = page.inner_text("[data-testid=evidence-statement]")
            decisions = page.evaluate(
                "() => Array.from(document.querySelectorAll('[data-testid=evidence-decisions] tbody tr')).map(r => r.innerText)"
            )
            observation["steps"].append(
                {
                    "step": "match-evidence",
                    "compared_crops_112": crops_ok,
                    "score_marker": bool(page.query_selector("[data-testid=pair-score-marker]")),
                    "decisions": decisions,
                    "attribution_shown": bool(page.query_selector("[data-testid=evidence-attribution]")),
                    "statement_present": "not OFIQ's internal landmarks" in evidence_statement and "not causal" in evidence_statement,
                }
            )
            matcher_run_count = recorded_run_count()
            matcher_run_label = f"{matcher_run_count} run{'s' if matcher_run_count != 1 else ''}"
            page.wait_for_function(
                "expected => (document.querySelector('[data-testid=experiment-context]')?.innerText || '').includes(expected)",
                arg=matcher_run_label,
                timeout=30_000,
            )
            matcher_context = page.inner_text("[data-testid=experiment-context]")
            goto("projects")
            page.wait_for_function(
                "expected => document.querySelectorAll('[data-testid=run-table] tbody tr').length === expected",
                arg=matcher_run_count,
                timeout=30_000,
            )
            observation["steps"].append(
                {
                    "step": "matcher-terminal-global-refresh",
                    "api_run_count": matcher_run_count,
                    "context": matcher_context,
                    "run_table_rows": page.evaluate("() => document.querySelectorAll('[data-testid=run-table] tbody tr').length"),
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
            goto("utility")
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
            utility_run_count = recorded_run_count()
            utility_run_label = f"{utility_run_count} run{'s' if utility_run_count != 1 else ''}"
            page.wait_for_function(
                "expected => (document.querySelector('[data-testid=experiment-context]')?.innerText || '').includes(expected)",
                arg=utility_run_label,
                timeout=30_000,
            )
            utility_context = page.inner_text("[data-testid=experiment-context]")
            goto("projects")
            page.wait_for_function(
                "expected => document.querySelectorAll('[data-testid=run-table] tbody tr').length === expected",
                arg=utility_run_count,
                timeout=30_000,
            )
            observation["steps"].append(
                {
                    "step": "utility-terminal-global-refresh",
                    "api_run_count": utility_run_count,
                    "context": utility_context,
                    "run_table_rows": page.evaluate("() => document.querySelectorAll('[data-testid=run-table] tbody tr').length"),
                }
            )
            # QS-E40..E42: a deliberately one-leaf observation cannot create a
            # cross-matcher contrast. The UI must block the invalid request and
            # expose the exact prerequisite before any API call is possible.
            goto("cross")
            page.wait_for_function("() => document.querySelectorAll('[data-testid=cross-matrix] option').length > 0 && document.querySelectorAll('[data-testid=cross-job] option').length > 0", timeout=60_000)
            cross_matrix_label = page.locator("[data-testid=cross-matrix] option:checked").inner_text()
            if "1 recorded leaf" not in cross_matrix_label:
                raise AssertionError(f"the cross-matcher view did not retain the reduced matrix: {cross_matrix_label}")
            page.fill("[data-testid=cross-replicates]", "100")
            cross_run = page.locator("[data-testid=cross-run]")
            if cross_run.is_enabled():
                raise AssertionError("the one-leaf cross-matcher action was enabled")
            cross_refusal = page.inner_text("#cross-run-help")
            if "at least two" not in cross_refusal:
                raise AssertionError(f"the one-leaf disabled reason was not visible: {cross_refusal}")
            observation["steps"].append(
                {
                    "step": "cross-matcher-reduced-matrix-disabled",
                    "matrix": cross_matrix_label,
                    "message": cross_refusal,
                    "contrast_rows": page.evaluate("() => document.querySelectorAll('[data-testid=contrast-row]').length"),
                }
            )
            # QS-F50..F53: figures, dashboard save/reload, report build, bundle export, reproduction.
            goto("reports")
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
            page.wait_for_function("() => document.querySelectorAll('[data-testid=report-table] tbody tr').length >= 1", timeout=300_000)
            report_rows = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=report-table] tbody tr')).map(r => r.innerText)")
            project_api_id = urllib.parse.quote(console_project_id_holder[0], safe="")
            project_datasets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects/{project_api_id}/datasets"))  # noqa: S310
            active_lfw_dataset_id = next(item["dataset_id"] for item in project_datasets if item["manifest"].get("dataset_resource_id") == "lfw-funneled-local-candidate")
            dashboard_catalog = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects/{project_api_id}/dashboards"))  # noqa: S310
            report_catalog = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects/{project_api_id}/reports"))  # noqa: S310
            if not dashboard_catalog or not dashboard_catalog[-1].get("panel_run_ids"):
                raise AssertionError("the dashboard list omitted its recorded panel Run lineage")
            if not report_catalog or report_catalog[-1].get("dataset_id") != active_lfw_dataset_id:
                raise AssertionError("the report list omitted or mismatched its recorded dataset lineage")
            page.click("[data-testid=bundle-export-analysis]")
            page.wait_for_function("() => document.querySelectorAll('[data-testid=bundle-table] tbody tr').length >= 1", timeout=120_000)
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
                    "dashboard_panel_run_ids": dashboard_catalog[-1]["panel_run_ids"],
                    "report_rows": report_rows,
                    "report_dataset_id": report_catalog[-1]["dataset_id"],
                    "reproduction_states": reproduction_states,
                }
            )
            # QS-X60/QS-X61: 1:N identification over the fixture subjects and a controlled-degradation response through the console.
            goto("advanced")
            page.wait_for_function("() => !document.querySelector('[data-testid=ident-freeze]')?.disabled", timeout=60_000)
            page.click("[data-testid=ident-freeze]")
            page.wait_for_selector("[data-testid=ident-search]", timeout=60_000)
            page.click("[data-testid=ident-search]")
            page.wait_for_selector("[data-testid=ident-detail]", timeout=600_000)
            page.wait_for_selector("[data-testid=cmc-plot] polyline", state="attached", timeout=60_000)
            page.click("[data-testid=ident-candidates]")
            page.wait_for_selector("[data-testid=ident-candidate-table] tbody tr", timeout=60_000)
            ident_rows = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=ident-searches] tbody tr')).map(r => r.innerText)")
            page.wait_for_selector("[data-testid=transform-select-jpeg]", timeout=60_000)
            transform_toggles = page.locator("[data-testid^=transform-select-]")
            for index in range(transform_toggles.count()):
                toggle = transform_toggles.nth(index)
                if toggle.get_attribute("data-testid") == "transform-select-jpeg":
                    toggle.check()
                elif toggle.is_checked():
                    toggle.uncheck()
            page.fill("[data-testid=transform-params-jpeg]", "80,50,20")
            selected_transforms = [
                transform_toggles.nth(index).get_attribute("data-testid")
                for index in range(transform_toggles.count())
                if transform_toggles.nth(index).is_checked()
            ]
            jpeg_parameters = page.input_value("[data-testid=transform-params-jpeg]")
            if selected_transforms != ["transform-select-jpeg"] or jpeg_parameters != "80,50,20":
                raise AssertionError(f"the derived-set controls did not retain only catalog JPEG 80,50,20: {selected_transforms}, {jpeg_parameters}")
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
            observation["steps"].append(
                {
                    "step": "advanced",
                    "identification_rows": ident_rows,
                    "derived_set_transform": {"selected": selected_transforms, "jpeg_parameters": jpeg_parameters},
                    "degradation_tables": degradation_tables,
                }
            )
            advanced_run_count = recorded_run_count()
            advanced_run_label = f"{advanced_run_count} run{'s' if advanced_run_count != 1 else ''}"
            page.wait_for_function(
                "expected => (document.querySelector('[data-testid=experiment-context]')?.innerText || '').includes(expected)",
                arg=advanced_run_label,
                timeout=30_000,
            )
            advanced_context = page.inner_text("[data-testid=experiment-context]")
            goto("projects")
            page.wait_for_function(
                "expected => document.querySelectorAll('[data-testid=run-table] tbody tr').length === expected",
                arg=advanced_run_count,
                timeout=30_000,
            )
            observation["steps"].append(
                {
                    "step": "advanced-terminal-global-refresh",
                    "api_run_count": advanced_run_count,
                    "context": advanced_context,
                    "run_table_rows": page.evaluate("() => document.querySelectorAll('[data-testid=run-table] tbody tr').length"),
                }
            )

            # Repeatable semantic accessibility gate over the populated real-data
            # console. The mobile pass below covers target sizing and overflow;
            # this pass covers names, labels, semantics, headings, contrast, and
            # disabled-control explanations in the rendered browser state.
            def accessibility_route(view: str) -> dict[str, object]:
                goto(view)
                result = page.evaluate(
                    """view => {
                        const root = document.querySelector(`main > div[data-view="${view}"]`);
                        const visible = element => {
                            const style = getComputedStyle(element);
                            const box = element.getBoundingClientRect();
                            return style.display !== 'none' && style.visibility !== 'hidden' &&
                                Number(style.opacity) !== 0 && box.width > 0 && box.height > 0;
                        };
                        const describedText = element => (element.getAttribute('aria-describedby') || '')
                            .split(/\s+/)
                            .filter(Boolean)
                            .map(id => document.getElementById(id)?.textContent?.trim() || '')
                            .filter(Boolean)
                            .join(' ');
                        const labelledText = element => (element.getAttribute('aria-labelledby') || '')
                            .split(/\s+/)
                            .filter(Boolean)
                            .map(id => document.getElementById(id)?.textContent?.trim() || '')
                            .filter(Boolean)
                            .join(' ');
                        const accessibleName = element => {
                            const aria = element.getAttribute('aria-label')?.trim() || labelledText(element);
                            if (aria) return aria;
                            const labels = Array.from(element.labels || []).map(label => label.textContent?.trim() || '').filter(Boolean);
                            if (labels.length) return labels.join(' ');
                            if (element.tagName === 'IMG') return element.getAttribute('alt')?.trim() || '';
                            if (element.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes(element.type)) return element.value?.trim() || '';
                            return element.textContent?.trim() || element.getAttribute('title')?.trim() || '';
                        };
                        const interactive = Array.from(root.querySelectorAll('button,a[href],select,input:not([type=hidden]),textarea,summary,[role=button]')).filter(visible);
                        const unnamed = interactive.filter(element => !accessibleName(element)).map(element => element.outerHTML.slice(0, 180));
                        const unlabeled = Array.from(root.querySelectorAll('select,input:not([type=hidden]):not([type=button]):not([type=submit]):not([type=reset]),textarea'))
                            .filter(visible)
                            .filter(element => !(element.labels?.length) && !element.getAttribute('aria-label') && !labelledText(element))
                            .map(element => element.outerHTML.slice(0, 180));
                        const unfocusable = interactive
                            .filter(element => !element.matches(':disabled') && element.getAttribute('role') !== 'tab' && element.tabIndex < 0)
                            .map(element => element.outerHTML.slice(0, 180));
                        const nonsemanticButtons = Array.from(root.querySelectorAll('[role=button]'))
                            .filter(visible)
                            .filter(element => !['BUTTON', 'A', 'INPUT', 'SUMMARY'].includes(element.tagName) && element.tabIndex < 0)
                            .map(element => element.outerHTML.slice(0, 180));
                        const disabledUnexplained = Array.from(root.querySelectorAll('[disabled]'))
                            .filter(visible)
                            .filter(element => !element.getAttribute('title')?.trim() && !describedText(element))
                            .map(element => element.outerHTML.slice(0, 180));
                        const imagesWithoutAlt = Array.from(root.querySelectorAll('img'))
                            .filter(visible)
                            .filter(image => !image.hasAttribute('alt'))
                            .map(image => image.outerHTML.slice(0, 180));
                        const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))
                            .filter(visible)
                            .map(element => ({level: Number(element.tagName.slice(1)), text: element.textContent?.trim() || ''}));
                        const headingJumps = headings.slice(1)
                            .filter((heading, index) => heading.level > headings[index].level + 1)
                            .map((heading, index) => `${headings[index].text} -> ${heading.text}`);
                        const idCounts = Array.from(document.querySelectorAll('[id]')).reduce((counts, element) => {
                            counts[element.id] = (counts[element.id] || 0) + 1;
                            return counts;
                        }, {});
                        const duplicateIds = Object.entries(idCounts).filter(([, count]) => count > 1).map(([id]) => id);
                        const dialogs = Array.from(document.querySelectorAll('[role=dialog]')).filter(visible);
                        const invalidDialogs = dialogs
                            .filter(dialog => dialog.getAttribute('aria-modal') !== 'true' || !accessibleName(dialog))
                            .map(dialog => dialog.outerHTML.slice(0, 180));
                        const invalidTables = Array.from(root.querySelectorAll('table'))
                            .filter(visible)
                            .flatMap(table => {
                                const caption = table.querySelector(':scope > caption');
                                const headerCells = Array.from(table.querySelectorAll(':scope > thead > tr > th'));
                                const columnHeaders = headerCells.filter(header => header.getAttribute('scope') === 'col');
                                const columnCount = columnHeaders.reduce((count, header) => count + header.colSpan, 0);
                                const dataRows = Array.from(table.querySelectorAll(':scope > tbody > tr'));
                                const problems = [];
                                if (!caption?.textContent?.trim()) problems.push('missing non-empty caption');
                                if (!headerCells.length || columnHeaders.length !== headerCells.length || columnHeaders.some(header => !header.textContent?.trim())) {
                                    problems.push('missing named column headers with scope=col');
                                }
                                const unassociatedRows = dataRows.filter(row => {
                                    const cells = Array.from(row.querySelectorAll(':scope > th, :scope > td'));
                                    if (!cells.length) return false;
                                    const logicalCellCount = cells.reduce((count, cell) => count + cell.colSpan, 0);
                                    const spanningStatusRow = cells.length === 1 && cells[0].tagName === 'TD' && cells[0].colSpan === columnCount;
                                    if (spanningStatusRow) return false;
                                    const rowHeader = cells[0];
                                    return logicalCellCount !== columnCount || rowHeader.tagName !== 'TH' || rowHeader.getAttribute('scope') !== 'row';
                                });
                                if (unassociatedRows.length) problems.push(`${unassociatedRows.length} data row(s) lack complete scope=col/scope=row associations`);
                                return problems.length ? [{
                                    testid: table.getAttribute('data-testid') || '',
                                    problems,
                                    html: table.outerHTML.slice(0, 220),
                                }] : [];
                            });

                        const parseColor = value => {
                            const match = value.match(/^rgba?\(\s*([\d.]+)[, ]+\s*([\d.]+)[, ]+\s*([\d.]+)(?:\s*[,/]\s*([\d.]+))?\s*\)$/);
                            return match ? [Number(match[1]), Number(match[2]), Number(match[3]), match[4] === undefined ? 1 : Number(match[4])] : null;
                        };
                        const blend = (foreground, background) => {
                            const alpha = foreground[3];
                            return [
                                foreground[0] * alpha + background[0] * (1 - alpha),
                                foreground[1] * alpha + background[1] * (1 - alpha),
                                foreground[2] * alpha + background[2] * (1 - alpha),
                                1,
                            ];
                        };
                        const backgroundFor = element => {
                            const lineage = [];
                            for (let node = element; node instanceof Element; node = node.parentElement) lineage.unshift(node);
                            let color = [255, 255, 255, 1];
                            let complex = false;
                            for (const node of lineage) {
                                const style = getComputedStyle(node);
                                if (style.backgroundImage !== 'none') complex = true;
                                const layer = parseColor(style.backgroundColor);
                                if (layer && layer[3] > 0) color = blend(layer, color);
                            }
                            return complex ? null : color;
                        };
                        const luminance = color => {
                            const channel = value => {
                                const normalized = value / 255;
                                return normalized <= 0.03928 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
                            };
                            return 0.2126 * channel(color[0]) + 0.7152 * channel(color[1]) + 0.0722 * channel(color[2]);
                        };
                        const contrastRatio = (a, b) => {
                            const first = luminance(a);
                            const second = luminance(b);
                            return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
                        };
                        const contrastFailures = [];
                        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                        while (walker.nextNode()) {
                            const text = walker.currentNode.textContent?.trim() || '';
                            const element = walker.currentNode.parentElement;
                            if (!text || !element || !visible(element) || element.closest(':disabled')) continue;
                            const style = getComputedStyle(element);
                            const foreground = parseColor(style.color);
                            const background = backgroundFor(element);
                            if (!foreground || !background) continue;
                            const renderedForeground = blend(foreground, background);
                            const ratio = contrastRatio(renderedForeground, background);
                            const fontSize = Number.parseFloat(style.fontSize);
                            const fontWeight = Number.parseInt(style.fontWeight, 10) || 400;
                            const large = fontSize >= 24 || (fontSize >= 18.66 && fontWeight >= 700);
                            const minimum = large ? 3 : 4.5;
                            if (ratio + 0.01 < minimum && contrastFailures.length < 8) {
                                contrastFailures.push({text: text.slice(0, 80), ratio: Number(ratio.toFixed(2)), minimum});
                            }
                        }
                        return {
                            view,
                            interactive_controls: interactive.length,
                            headings,
                            dialogs: dialogs.length,
                            unnamed,
                            unlabeled,
                            unfocusable,
                            nonsemantic_buttons: nonsemanticButtons,
                            disabled_unexplained: disabledUnexplained,
                            images_without_alt: imagesWithoutAlt,
                            heading_jumps: headingJumps,
                            duplicate_ids: duplicateIds,
                            invalid_dialogs: invalidDialogs,
                            invalid_tables: invalidTables,
                            contrast_failures: contrastFailures,
                        };
                    }""",
                    view,
                )
                failures = {
                    key: result[key]
                    for key in (
                        "unnamed", "unlabeled", "unfocusable", "nonsemantic_buttons",
                        "disabled_unexplained", "images_without_alt", "heading_jumps",
                        "duplicate_ids", "invalid_dialogs", "invalid_tables", "contrast_failures",
                    )
                    if result[key]
                }
                if failures:
                    raise AssertionError(f"{view} accessibility failures: {failures}")
                return result

            accessibility_routes = [
                accessibility_route(view)
                for view in ("projects", "data", "assess", "matchers", "advanced", "utility", "cross", "reports", "system")
            ]
            goto("system")
            selected_tab = page.locator("[role=tab][aria-selected=true]")
            selected_tab.focus()
            selected_tab_id = selected_tab.get_attribute("id")
            page.keyboard.press("ArrowRight")
            next_tab = page.locator("[role=tab][aria-selected=true]")
            next_tab_id = next_tab.get_attribute("id")
            next_tab_focused = next_tab.evaluate("element => document.activeElement === element")
            page.keyboard.press("ArrowLeft")
            restored_tab = page.locator("[role=tab][aria-selected=true]")
            restored_tab_id = restored_tab.get_attribute("id")
            restored_tab_focused = restored_tab.evaluate("element => document.activeElement === element")
            if (
                not selected_tab_id
                or next_tab_id == selected_tab_id
                or restored_tab_id != selected_tab_id
                or not next_tab_focused
                or not restored_tab_focused
            ):
                raise AssertionError(
                    "system tablist did not support focus-preserving ArrowRight/ArrowLeft navigation: "
                    f"selected={selected_tab_id}, next={next_tab_id}, restored={restored_tab_id}, "
                    f"next_focused={next_tab_focused}, restored_focused={restored_tab_focused}"
                )
            observation["steps"].append(
                {
                    "step": "semantic-accessibility",
                    "routes": accessibility_routes,
                    "system_tab_keyboard": {
                        "selected": selected_tab_id,
                        "next": next_tab_id,
                        "restored": restored_tab_id,
                        "next_focused": next_tab_focused,
                        "restored_focused": restored_tab_focused,
                    },
                }
            )

            # Real 390px browser pass: audit every route for document overflow,
            # undersized controls, and checkbox-label hit targets. Operate both
            # scientific catalog surfaces from the keyboard while preserving state.
            page.set_viewport_size({"width": 390, "height": 844})

            def mobile_surface(view: str, selector: str) -> dict[str, object]:
                goto(view)
                target = page.locator(selector).first
                target.wait_for(state="visible", timeout=60_000)
                layout = page.evaluate(
                    """() => ({
                        viewport: window.innerWidth,
                        document_client_width: document.documentElement.clientWidth,
                        document_scroll_width: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)
                    })"""
                )
                if layout["viewport"] != 390 or layout["document_scroll_width"] > layout["document_client_width"]:
                    raise AssertionError(f"{view} overflows horizontally at 390px: {layout}")
                target_size = target.evaluate(
                    """element => {
                        const hitTarget = element.closest('label') || element;
                        const box = hitTarget.getBoundingClientRect();
                        return {width: box.width, height: box.height, tag: hitTarget.tagName};
                    }"""
                )
                if target_size["width"] < 44 or target_size["height"] < 44:
                    raise AssertionError(f"{view} mobile target is smaller than 44px: {target_size}")
                checked_before = target.is_checked()
                target.focus()
                page.keyboard.press("Tab")
                page.keyboard.press("Shift+Tab")
                focused = target.evaluate("element => document.activeElement === element")
                focus_visible = target.evaluate("element => element.matches(':focus-visible')")
                page.keyboard.press("Space")
                toggled = target.is_checked() != checked_before
                page.keyboard.press("Space")
                restored = target.is_checked() == checked_before
                if not (focused and focus_visible and toggled and restored):
                    raise AssertionError(
                        f"{view} checkbox was not keyboard/focus usable: "
                        f"focused={focused}, focus_visible={focus_visible}, toggled={toggled}, restored={restored}"
                    )
                return {
                    "view": view,
                    "layout": layout,
                    "target": target_size,
                    "keyboard_focus": focused,
                    "focus_visible": focus_visible,
                    "space_toggled": toggled,
                    "state_restored": restored,
                }

            mobile_matcher_filter = mobile_surface("matchers", "[data-testid=addon-selection] input[type=checkbox]")
            mobile_transform_filter = mobile_surface("advanced", "[data-testid=transform-select-jpeg]")

            def mobile_route(view: str) -> dict[str, object]:
                goto(view)
                result = page.evaluate(
                    """view => {
                        const root = document.querySelector(`main > div[data-view="${view}"]`);
                        const visible = element => {
                            const style = getComputedStyle(element);
                            const box = element.getBoundingClientRect();
                            return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0;
                        };
                        const ordinary = Array.from(root.querySelectorAll('button,a[href],select,input:not([type=checkbox]):not([type=radio]),textarea,summary'))
                            .filter(visible)
                            .map(element => {
                                const box = element.getBoundingClientRect();
                                return {tag: element.tagName, text: (element.textContent || element.getAttribute('aria-label') || '').trim().slice(0, 80), width: box.width, height: box.height};
                            });
                        const checkboxTargets = Array.from(root.querySelectorAll('input[type=checkbox],input[type=radio]'))
                            .filter(visible)
                            .map(element => {
                                const target = element.closest('label') || element;
                                const box = target.getBoundingClientRect();
                                return {tag: target.tagName, text: target.textContent.trim().slice(0, 80), width: box.width, height: box.height};
                            });
                        return {
                            viewport: window.innerWidth,
                            document_client_width: document.documentElement.clientWidth,
                            document_scroll_width: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
                            ordinary_controls: ordinary.length,
                            checkbox_controls: checkboxTargets.length,
                            undersized: [...ordinary, ...checkboxTargets].filter(item => item.width < 44 || item.height < 44)
                        };
                    }""",
                    view,
                )
                if result["viewport"] != 390 or result["document_scroll_width"] > result["document_client_width"]:
                    raise AssertionError(f"{view} overflows horizontally at 390px: {result}")
                if result["undersized"]:
                    raise AssertionError(f"{view} has mobile targets smaller than 44px: {result['undersized'][:5]}")
                return {"view": view, **result}

            mobile_routes = [mobile_route(view) for view in ("projects", "data", "assess", "matchers", "advanced", "utility", "cross", "reports", "system")]
            observation["steps"].append(
                {
                    "step": "mobile-390-accessibility",
                    "viewport_width": 390,
                    "surfaces": [mobile_matcher_filter, mobile_transform_filter],
                    "routes": mobile_routes,
                }
            )
            page.set_viewport_size({"width": 1500, "height": 1000})
            goto("advanced")
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
                "note": "restricted-image evidence in an untracked project directory; never commit or redistribute",
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
