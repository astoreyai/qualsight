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

from controlled_inputs import lfw_tree_root

ROOT = Path(__file__).resolve().parents[1]
API_PORT = 8480
PREVIEW_PORT = 5181
NODE_EXECUTABLE = shutil.which("node")
if NODE_EXECUTABLE is None:
    raise RuntimeError("the reviewed Node executable is not available on PATH")
NODE_BIN = str(Path(NODE_EXECUTABLE).parent)


def _assert_beta_ui_boundaries() -> None:
    app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    reports_source = (ROOT / "frontend" / "src" / "Reports.tsx").read_text(encoding="utf-8")
    required = {
        'data-testid="advanced-beta-boundary"': "the visible Advanced research-extension boundary",
        "Outside beta acceptance": "the explicit Advanced beta-scope statement",
        'data-testid="report-disclosure-warning"': "the governed-download disclosure warning",
        'data-testid="dashboard-operator-summary"': "typed primary dashboard summaries",
        'data-testid="dashboard-raw-data"': "collapsed secondary raw panel evidence",
        'data-governed-download="qsexp"': "bundle-download disclosure binding",
    }
    combined = f"{app_source}\n{reports_source}"
    missing = [description for marker, description in required.items() if marker not in combined]
    if missing:
        raise AssertionError(f"beta UI boundary contract is missing: {missing!r}")
    if "panel.kind !== \"figure\" && <pre" in reports_source:
        raise AssertionError("dashboard panel JSON remains a primary presentation")


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
    _assert_beta_ui_boundaries()
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
    env["QUALSIGHT_REPOSITORY_ROOT"] = os.environ.get(
        "QUALSIGHT_REPOSITORY_ROOT", str(ROOT)
    )
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
            expected_nav = [
                {"view": "home", "label": "Home"},
                {"view": "projects", "label": "Projects"},
                {"view": "data", "label": "Inputs"},
                {"view": "assess", "label": "Inspect quality"},
                {"view": "matchers", "label": "Compare"},
                {"view": "advanced", "label": "Stress test"},
                {"view": "utility", "label": "Analyze utility"},
                {"view": "cross", "label": "Results"},
                {"view": "reports", "label": "Evidence & reports"},
                {"view": "system", "label": "Manage system"},
            ]
            nav_items = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=primary-nav] [data-testid=nav-link]')).map(d => ({view: d.getAttribute('data-view'), label: d.textContent?.trim()}))")
            nav_views = [item["view"] for item in nav_items]
            if nav_items != expected_nav:
                raise AssertionError(f"primary navigation diverged from the ten-destination workflow: {nav_items}")
            desktop_nav = page.evaluate(
                """() => {
                    const rect = (selector) => {
                        const value = document.querySelector(selector)?.getBoundingClientRect();
                        if (!value) throw new Error(`missing ${selector}`);
                        return {left: value.left, right: value.right, top: value.top, height: value.height, width: value.width};
                    };
                    return {
                        viewport: {width: window.innerWidth, height: window.innerHeight},
                        nav: rect('[data-testid=primary-nav]'),
                        topbar: rect('.topbar'),
                        context: rect('[data-testid=experiment-context]'),
                        main: rect('#main-content'),
                    };
                }"""
            )
            nav_rect = desktop_nav["nav"]
            if not (
                abs(nav_rect["left"]) <= 1
                and abs(nav_rect["top"]) <= 1
                and 200 <= nav_rect["width"] <= 280
                and nav_rect["height"] >= desktop_nav["viewport"]["height"] - 1
                and min(desktop_nav[name]["left"] for name in ("topbar", "context", "main")) >= nav_rect["right"] - 1
            ):
                raise AssertionError(f"primary navigation is not a full-height left rail: {desktop_nav}")

            page.locator('[data-testid=primary-nav] [data-view="assess"]').focus()
            page.keyboard.press("Enter")
            page.wait_for_function("() => window.location.hash === '#/assess' && document.querySelector('[data-testid=primary-nav] [data-view=assess]')?.getAttribute('aria-current') === 'page'")
            keyboard_current = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=primary-nav] [aria-current=page]')).map(d => d.getAttribute('data-view'))")
            if keyboard_current != ["assess"]:
                raise AssertionError(f"keyboard navigation did not update the current route: {keyboard_current}")

            page.set_viewport_size({"width": 390, "height": 844})
            page.evaluate("() => window.scrollTo(0, 0)")
            drawer_toggle = page.locator('[data-testid=nav-drawer-toggle]')
            if drawer_toggle.count() != 1 or not drawer_toggle.is_visible():
                raise AssertionError("390px navigation has no visible collapsible drawer control")
            drawer_initial = page.evaluate(
                """() => ({
                    expanded: document.querySelector('[data-testid=nav-drawer-toggle]')?.getAttribute('aria-expanded'),
                    visibleLinks: Array.from(document.querySelectorAll('[data-testid=primary-nav] [data-testid=nav-link]'))
                        .filter(link => { const box = link.getBoundingClientRect(); return box.width > 0 && box.height > 0; }).length,
                    documentWidth: document.documentElement.scrollWidth,
                    viewportWidth: window.innerWidth,
                })"""
            )
            if drawer_initial != {"expanded": "false", "visibleLinks": 0, "documentWidth": 390, "viewportWidth": 390}:
                raise AssertionError(f"390px navigation drawer is not initially collapsed without overflow: {drawer_initial}")
            drawer_toggle.focus()
            page.keyboard.press("Enter")
            page.wait_for_function(
                "() => document.querySelector('[data-testid=nav-drawer-toggle]')?.getAttribute('aria-expanded') === 'true' && document.activeElement?.getAttribute('data-testid') === 'nav-drawer-close'"
            )
            drawer_open = page.evaluate(
                """() => {
                    const links = Array.from(document.querySelectorAll('[data-testid=primary-nav] [data-testid=nav-link]'));
                    return {
                        visibleLinks: links.filter(link => { const box = link.getBoundingClientRect(); return box.width > 0 && box.height > 0; }).length,
                        undersizedLinks: links.filter(link => { const box = link.getBoundingClientRect(); return box.width > 0 && box.height > 0 && (box.width < 44 || box.height < 44); }).length,
                        closeFocused: document.activeElement?.getAttribute('data-testid') === 'nav-drawer-close',
                        documentWidth: document.documentElement.scrollWidth,
                        viewportWidth: window.innerWidth,
                    };
                }"""
            )
            if drawer_open != {"visibleLinks": 10, "undersizedLinks": 0, "closeFocused": True, "documentWidth": 390, "viewportWidth": 390}:
                raise AssertionError(f"opened mobile drawer is not a keyboard-sized ten-route surface: {drawer_open}")
            mobile_drawer_shot = args.screenshot_dir / "qualsight-mobile-drawer.png"
            page.screenshot(path=str(mobile_drawer_shot), full_page=False)
            page.locator("[data-testid=nav-drawer-content] .nav-brand").focus()
            page.keyboard.press("Shift+Tab")
            wrapped_to_last = page.evaluate("() => document.activeElement?.getAttribute('data-view') === 'system'")
            page.keyboard.press("Tab")
            wrapped_to_first = page.evaluate("() => document.activeElement?.classList.contains('nav-brand') === true")
            page.keyboard.press("Escape")
            page.wait_for_function(
                "() => document.querySelector('[data-testid=nav-drawer-toggle]')?.getAttribute('aria-expanded') === 'false' && document.activeElement?.getAttribute('data-testid') === 'nav-drawer-toggle'"
            )
            drawer_closed_focus = page.evaluate("() => document.activeElement?.getAttribute('data-testid') === 'nav-drawer-toggle'")
            if not (wrapped_to_last and wrapped_to_first and drawer_closed_focus):
                raise AssertionError(
                    "mobile drawer did not trap focus and return it after Escape: "
                    f"wrapped_to_last={wrapped_to_last}, wrapped_to_first={wrapped_to_first}, returned={drawer_closed_focus}"
                )
            observation["steps"].append(
                {
                    "step": "mobile-navigation-drawer",
                    "initial": drawer_initial,
                    "open": drawer_open,
                    "focus_wrapped_to_last": wrapped_to_last,
                    "focus_wrapped_to_first": wrapped_to_first,
                    "escape_returned_focus": drawer_closed_focus,
                    "screenshot": str(mobile_drawer_shot),
                }
            )
            page.set_viewport_size({"width": 1500, "height": 1000})
            goto("projects")
            page.evaluate("() => { window.location.hash = '#/home' }")
            page.wait_for_timeout(300)
            home_view = page.query_selector('main > div[data-view="home"]:not(.hidden-view)')
            if home_view is None:
                raise AssertionError("guided Home route is absent: #/home exposes no visible workflow launcher")
            page.wait_for_selector('[data-testid=home-panel][data-state="empty"], [data-testid=home-panel][data-state="ready"], [data-testid=home-panel][data-state="error"]', timeout=30_000)
            expected_home_tasks = [
                {"label": "Inspect one image", "href": "#/assess"},
                {"label": "Compare images", "href": "#/matchers"},
                {"label": "Stress-test an image", "href": "#/advanced"},
                {"label": "Analyze a dataset", "href": "#/data"},
                {"label": "Compare matchers", "href": "#/matchers"},
                {"label": "Quality utility", "href": "#/utility"},
                {"label": "Build a report", "href": "#/reports"},
            ]
            home_tasks = page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-testid=home-task]')).map(item => ({
                    label: item.querySelector('strong')?.textContent?.trim(),
                    href: item.getAttribute('href'),
                    destination: item.getAttribute('data-destination'),
                    disabled: item.getAttribute('aria-disabled'),
                    reason: item.querySelector('[data-testid=home-task-reason]')?.textContent?.trim() || null,
                }))"""
            )
            expected_disabled_tasks = [
                {
                    **task,
                    "href": None,
                    "destination": task["href"],
                    "disabled": "true",
                    "reason": "Select or create a project before starting this workflow.",
                }
                for task in expected_home_tasks
            ]
            if home_tasks != expected_disabled_tasks:
                raise AssertionError(f"fresh Home tasks do not expose their exact project prerequisite: {home_tasks}")
            home_state = page.get_attribute("[data-testid=home-panel]", "data-state")
            projects_api = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects"))  # noqa: S310
            addons_api = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/addons"))  # noqa: S310
            home_counts = page.evaluate(
                """() => ({
                    projects: Number(document.querySelector('[data-testid=home-project-count]')?.textContent),
                    matchers: Number(document.querySelector('[data-testid=home-matcher-count]')?.textContent),
                    transforms: Number(document.querySelector('[data-testid=home-transform-count]')?.textContent),
                })"""
            )
            expected_home_counts = {
                "projects": len(projects_api),
                "matchers": len(addons_api["matchers"]),
                "transforms": len(addons_api["transforms"]),
            }
            if home_counts != expected_home_counts:
                raise AssertionError(f"guided Home counts are not sourced from the live APIs: observed={home_counts} expected={expected_home_counts}")
            home_measure = page.evaluate(
                """() => ({
                    value: document.querySelector('[data-testid=home-measure-count]')?.textContent?.trim() || null,
                    state: document.querySelector('[data-testid=home-measure-count]')?.getAttribute('data-state') || null,
                    reason: document.querySelector('[data-testid=home-measure-reason]')?.textContent?.trim() || null,
                })"""
            )
            if home_measure != {
                "value": "—",
                "state": "unavailable",
                "reason": "Complete a quality batch to record the available OFIQ measures.",
            }:
                raise AssertionError(f"fresh Home invented or omitted the unavailable measure-catalog state: {home_measure}")
            home_run_count = page.evaluate(
                """() => ({
                    value: document.querySelector('[data-testid=home-run-count]')?.textContent?.trim() || null,
                    state: document.querySelector('[data-testid=home-run-count]')?.getAttribute('data-state') || null,
                    reason: document.querySelector('[data-testid=home-run-reason]')?.textContent?.trim() || null,
                })"""
            )
            if home_run_count != {
                "value": "—",
                "state": "unavailable",
                "reason": "Select an active project to count its recorded Runs.",
            }:
                raise AssertionError(f"fresh Home invented or omitted the unavailable Run-count state: {home_run_count}")
            home_recent_state = page.evaluate(
                """() => ({
                    state: document.querySelector('[data-testid=home-recent-feed-state]')?.getAttribute('data-state') || null,
                    text: document.querySelector('[data-testid=home-recent-feed-state]')?.textContent?.trim() || null,
                })"""
            )
            if home_recent_state != {"state": "empty", "text": "Select an active project to review its recorded work."}:
                raise AssertionError(f"fresh Home recent-work feed is not an explicit empty state: {home_recent_state}")
            expected_home_state = "empty" if not projects_api else "ready"
            if home_state != expected_home_state:
                raise AssertionError(f"guided Home state is not truthful for the live project feed: observed={home_state} expected={expected_home_state}")
            engine_text = page.inner_text("[data-testid=home-engine]")
            if health["engine"]["name"] not in engine_text or health["engine"]["version"] not in engine_text:
                raise AssertionError(f"guided Home engine status diverged from live health: {engine_text}")
            goto("projects")
            if any(check["current_links"] != [{"label": next(label for view, label in {
                "projects": "Projects", "data": "Inputs", "assess": "Inspect quality", "matchers": "Compare",
                "advanced": "Stress test", "utility": "Analyze utility", "cross": "Results",
                "reports": "Evidence & reports", "system": "Manage system",
            }.items() if view == check["view"]), "view": check["view"]}] for check in nav_checks):
                raise AssertionError(f"navigation current state diverged from the routed view: {nav_checks}")
            goto("utility")
            empty_state = page.inner_text("main div[data-view=utility]")
            empty_recovery = page.get_attribute("main div[data-view=utility] .empty-state a", "href")
            if empty_recovery != "#/projects":
                raise AssertionError(f"utility empty state has no direct project recovery action: {empty_recovery}")
            goto("system")
            page.wait_for_selector("[data-testid=matcher-assets] tbody tr", timeout=60_000)
            system_assets = page.evaluate("() => document.querySelectorAll('[data-testid=matcher-assets] tbody tr').length")
            dataset_addons = page.evaluate("() => document.querySelectorAll('[data-testid=dataset-addons] [data-addon-id]').length")
            addon_catalog_api = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/addons"))  # noqa: S310
            dom_addon_ids = {
                "matchers": sorted(page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=matcher-assets] [data-matcher-id]')).map(e => e.getAttribute('data-matcher-id'))")),
                "datasets": sorted(page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=dataset-addons] [data-addon-id]')).map(e => e.getAttribute('data-addon-id'))")),
            }
            api_addon_ids = {
                "matchers": sorted(item["matcher_id"] for item in addon_catalog_api["matchers"]),
                "datasets": sorted(item["addon_id"] for item in addon_catalog_api["datasets"]),
                "preprocessing": sorted(item["addon_id"] for item in addon_catalog_api["preprocessing"]),
                "transforms": sorted(item["addon_id"] for item in addon_catalog_api["transforms"]),
            }
            system_tabs = page.get_by_role("tab").all_text_contents()
            page.get_by_role("tab", name="Processing profiles", exact=True).click()
            preprocessing_addons = page.evaluate("() => document.querySelectorAll('[data-testid=preprocessing-addons] [data-addon-id]').length")
            transform_addons = page.evaluate("() => document.querySelectorAll('[data-testid=transform-addons] [data-addon-id]').length")
            dom_addon_ids["preprocessing"] = sorted(page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=preprocessing-addons] [data-addon-id]')).map(e => e.getAttribute('data-addon-id'))"))
            dom_addon_ids["transforms"] = sorted(page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=transform-addons] [data-addon-id]')).map(e => e.getAttribute('data-addon-id'))"))
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
            page.get_by_role("tab", name="Audit", exact=True).click()
            system_resources = page.evaluate("() => document.querySelectorAll('[data-testid=resource-table] .resource-card').length")
            page.get_by_role("tab", name="Add-ons", exact=True).click()
            observation["steps"].append({"step": "load", "title": page.title(), "manifest": manifest, "primary_nav_links": len(nav_views), "primary_nav_unique_views": len(set(nav_views)), "first_keyboard_focus": first_focus, "system_tabs": system_tabs, "system_panel_matcher_assets": system_assets, "system_panel_dataset_addons": dataset_addons, "system_panel_preprocessing_addons": preprocessing_addons, "system_panel_transform_addons": transform_addons, "addon_ids_match_api": dom_addon_ids == api_addon_ids, "system_panel_connection_policy": connection_text, "system_panel_resources": system_resources, "routes": nav_checks, "fresh_store_empty_state": empty_state[:200], "fresh_store_empty_recovery": empty_recovery})
            page.wait_for_selector("[data-testid=engine-badge] .value", timeout=30_000)
            badge = page.inner_text("[data-testid=engine-badge]")
            observation["steps"].append({"step": "engine-badge", "text": badge})
            # Matcher sources are an explicit operator action. Scientific jobs
            # must never acquire them as an implicit side effect.
            asset_rows = page.locator("[data-testid=matcher-assets] tbody tr")
            elasticface_row = page.locator(
                "[data-testid=matcher-assets] [data-matcher-id='elasticface-arc-r100-local-candidate']"
            )
            elasticface_row.get_by_role("button").click()
            matcher_detail = page.locator("[data-testid=matcher-detail]")
            managed_checkpoint = matcher_detail.get_by_role(
                "button", name="Download verified checkpoint"
            )
            if managed_checkpoint.count() != 1 or not managed_checkpoint.is_enabled():
                raise AssertionError("ElasticFace managed-checkpoint action is not available")
            managed_checkpoint.click()
            page.get_by_role("dialog").get_by_role("button", name="Confirm exact target", exact=True).click()
            matcher_detail.get_by_text("Managed checkpoint verified", exact=True).wait_for(
                timeout=120_000
            )
            acquisition_status = page.locator(
                "[data-testid=system-action-message]"
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
            sources_verified: list[str] = []
            for matcher in addon_catalog_api["matchers"]:
                row = page.locator(f"[data-testid=matcher-assets] [data-matcher-id='{matcher['matcher_id']}']")
                row.get_by_role("button").click()
                pull = matcher_detail.get_by_role("button", name="Pull pinned sources")
                if pull.count() == 1 and pull.is_enabled():
                    pull.click()
                    page.get_by_role("dialog").get_by_role("button", name="Confirm exact target", exact=True).click()
                    matcher_detail.get_by_text("Sources verified", exact=True).wait_for(timeout=120_000)
                if matcher_detail.get_by_text("Sources verified", exact=True).count() == 1:
                    sources_verified.append(matcher["matcher_id"])
            first_asset = addon_catalog_api["matchers"][0]
            page.locator(f"[data-testid=matcher-assets] [data-matcher-id='{first_asset['matcher_id']}']").get_by_role("button").click()
            matcher_detail.get_by_role("button", name="Load matcher", exact=True).click()
            page.get_by_role("dialog").get_by_role("button", name="Confirm exact target", exact=True).click()
            page.wait_for_function(
                """() => {
                    const buttons = Array.from(document.querySelectorAll(
                        '[data-testid=matcher-lifecycle-actions] button'
                    ));
                    return buttons.length > 1 && buttons.every(button => button.disabled);
                }""",
                timeout=30_000,
            )
            lifecycle_buttons_while_loading = page.evaluate(
                """() => Array.from(document.querySelectorAll(
                    '[data-testid=matcher-lifecycle-actions] button'
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
                "() => document.querySelector('[data-testid=matcher-detail] > header .state-pill')?.textContent?.trim() === 'LOADED'",
                timeout=180_000,
            )
            loaded_runtime = matcher_detail.locator("dt", has_text="Runtime").locator("xpath=following-sibling::dd[1]").inner_text()
            real_load_observed = "parameters" in loaded_runtime and "not resident" not in loaded_runtime
            if not real_load_observed:
                raise AssertionError(f"matcher reported LOADED without a resident runtime: {loaded_runtime}")
            matcher_detail.get_by_role("button", name="Unload matcher", exact=True).click()
            page.get_by_role("dialog").get_by_role("button", name="Confirm exact target", exact=True).click()
            page.wait_for_function(
                "() => document.querySelector('[data-testid=matcher-detail] > header .state-pill')?.textContent?.trim() === 'UNLOADED'",
                timeout=120_000,
            )
            unloaded_runtime = matcher_detail.locator("dt", has_text="Runtime").locator("xpath=following-sibling::dd[1]").inner_text()
            unload_observed = "not resident" in unloaded_runtime
            if not unload_observed:
                raise AssertionError(f"matcher reported UNLOADED while runtime appeared resident: {unloaded_runtime}")
            observation["steps"].append(
                {
                    "step": "matcher-lifecycle",
                    "assets": system_assets,
                    "elasticface_managed_checkpoint": True,
                    "checkpoint_event_message": acquisition_message,
                    "sources_verified": len(sources_verified) == system_assets,
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
            page.wait_for_selector("[data-testid=working-sample-image]", timeout=30_000)
            image_ok = page.evaluate(
                "() => { const i = document.querySelector('[data-testid=working-sample-image]'); return i && i.complete && i.naturalWidth > 0; }"
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
            goto("home")
            active_created_project = page.evaluate("() => window.localStorage.getItem('qualsight.activeProjectId')")
            page.wait_for_function(
                "projectId => document.querySelector('[data-testid=home-panel]')?.getAttribute('data-project-id') === projectId && document.querySelector('[data-testid=home-panel]')?.getAttribute('data-state') === 'ready'",
                arg=active_created_project,
                timeout=60_000,
            )
            compare_images = page.locator(
                '[data-testid="home-task"][data-destination="#/matchers"]',
                has_text="Compare images",
            )
            if compare_images.count() != 1 or compare_images.evaluate("node => node.tagName") != "A":
                raise AssertionError(
                    "Home did not enable direct two-image comparison for an active project without a dataset"
                )
            goto("projects")
            console_project_id_holder = [next(p["project_id"] for p in json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects")) if p["name"] == observation_project_name)]  # noqa: S310

            def recorded_run_count() -> int:
                projects = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects"))  # noqa: S310
                project = next(item for item in projects if item["project_id"] == console_project_id_holder[0])
                return len(project["run_ids"])

            def latest_recorded_run_id() -> str:
                encoded_project = urllib.parse.quote(console_project_id_holder[0], safe="")
                rows = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects/{encoded_project}/runs?limit=10000"))  # noqa: S310
                if not rows:
                    raise AssertionError("active project has no recorded Run")
                return max(rows, key=lambda row: (row["start_time"], row["run_id"]))["run_id"]

            goto("assess")
            page.click("[data-testid=assess-button]")
            page.wait_for_selector("[data-testid=measure-table]", timeout=180_000)
            banner = page.inner_text("[data-testid=result-banner]")
            rows = page.evaluate(
                "() => Array.from(document.querySelectorAll('[data-testid=measure-table] tbody tr')).map(r => Array.from(r.children).map(c => c.innerText))"
            )
            page.wait_for_function(
                "() => document.querySelector('[data-testid=structured-evidence]')?.getAttribute('data-state') === 'ready'",
                timeout=60_000,
            )
            provenance = page.inner_text("[data-testid=structured-evidence]")
            run_link = page.get_attribute("[data-testid=raw-json-view]", "href")
            if not run_link:
                raise AssertionError("structured evidence did not expose its public Run JSON link")
            recorded_run = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}{run_link}"))  # noqa: S310
            recorded_project_id = recorded_run["run"]["project_id"]
            recorded_sample = json.load(urllib.request.urlopen(  # noqa: S310
                f"http://127.0.0.1:{API_PORT}/api/projects/{urllib.parse.quote(recorded_project_id, safe='')}/samples/{urllib.parse.quote(recorded_run['sample_id'], safe='')}"
            ))
            if recorded_sample["dataset_id"] not in provenance:
                raise AssertionError(
                    "structured evidence omitted the persisted single-assessment dataset: "
                    f"expected {recorded_sample['dataset_id']} in {provenance!r}"
                )
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
            upload_source = lfw_tree_root(ROOT) / "Aaron_Peirsol/Aaron_Peirsol_0001.jpg"
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
            upload_run_id = latest_recorded_run_id()
            page.wait_for_function(
                "expected => document.querySelector('[data-context-key=evidence]')?.getAttribute('data-run-id') === expected",
                arg=upload_run_id,
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
                encoded_project = urllib.parse.quote(project_id, safe="")
                rows = json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/api/projects/{encoded_project}/runs?limit=10000"))  # noqa: S310
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
                        const observed = Array.from(document.querySelectorAll('[data-testid=run-table] tbody button[data-evidence-href]'))
                            .map(button => decodeURIComponent((button.getAttribute('data-evidence-href') || '').split('/').pop() || ''));
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
            batch_run_id = latest_recorded_run_id()
            page.wait_for_function(
                "expected => document.querySelector('[data-context-key=evidence]')?.getAttribute('data-run-id') === expected",
                arg=batch_run_id,
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
            page.wait_for_selector("[data-testid=comparison-studio]", timeout=60_000)
            if page.get_attribute("[data-testid=comparison-mode-pair]", "aria-pressed") != "true":
                raise AssertionError("Compare did not open in the image-pair workflow")
            if page.locator("[data-testid=comparison-side-a]").count() != 1 or page.locator("[data-testid=comparison-side-b]").count() != 1:
                raise AssertionError("the image-pair workflow did not expose independent Image A and Image B slots")
            page.click("[data-testid=comparison-mode-dataset]")
            page.wait_for_selector("[data-testid=matchers]", timeout=60_000)
            if page.get_attribute("[data-testid=comparison-mode-dataset]", "aria-pressed") != "true":
                raise AssertionError("Compare did not enter the dataset-study workflow")
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
            active_dataset_id = page.get_attribute("[data-context-key=dataset]", "data-dataset-id")
            if not active_dataset_id:
                raise AssertionError("dataset-study context has no exact active dataset identity")
            protocol_api = json.load(urllib.request.urlopen(  # noqa: S310
                f"http://127.0.0.1:{API_PORT}/api/projects/"
                f"{urllib.parse.quote(console_project_id_holder[0], safe='')}/protocols"
            ))
            expected_protocol = [item for item in protocol_api if item["dataset_id"] == active_dataset_id][-1]
            page.wait_for_function(
                "expected => document.querySelector('[data-context-key=protocol]')?.getAttribute('data-protocol-id') === expected",
                arg=expected_protocol["protocol_id"],
                timeout=30_000,
            )
            if page.get_attribute("[data-context-key=matcher]", "data-leaf-id") is not None:
                raise AssertionError("protocol freeze invented a selected matcher leaf before operator selection")
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
            matcher_leaves_api = json.load(urllib.request.urlopen(  # noqa: S310
                f"http://127.0.0.1:{API_PORT}/api/projects/"
                f"{urllib.parse.quote(console_project_id_holder[0], safe='')}/matcher-leaves"
            ))
            expected_leaf = next(
                item for item in matcher_leaves_api
                if item["dataset_id"] == active_dataset_id and item["leaf_id"] == "QS-C22.AS"
            )
            expected_matcher_name = next(
                item["name"] for item in addon_catalog_api["matchers"]
                if item["matcher_id"] == expected_leaf["matcher_id"]
            )
            expected_profile_name = next(
                item["display_name"] for item in addon_catalog_api["preprocessing"]
                if item["addon_id"] == expected_leaf["condition_id"]
            )
            page.wait_for_function(
                "expected => { const protocol = document.querySelector('[data-context-key=protocol]'); "
                "const matcher = document.querySelector('[data-context-key=matcher]'); "
                "const evidence = document.querySelector('[data-context-key=evidence]'); "
                "return protocol?.getAttribute('data-protocol-id') === expected.protocolId "
                "&& matcher?.getAttribute('data-leaf-id') === expected.leafId "
                "&& evidence?.getAttribute('data-run-id') === expected.runId "
                "&& matcher?.querySelector('strong')?.textContent?.trim() === expected.leafLabel; }",
                arg={
                    "protocolId": expected_protocol["protocol_id"],
                    "leafId": expected_leaf["leaf_id"],
                    "runId": expected_leaf["run_id"],
                    "leafLabel": f"{expected_matcher_name} · {expected_profile_name}",
                },
                timeout=30_000,
            )
            populated_matcher_context = page.inner_text("[data-testid=experiment-context]")
            for raw_identity in (expected_protocol["protocol_id"], expected_leaf["leaf_id"], expected_leaf["run_id"]):
                if raw_identity in populated_matcher_context:
                    raise AssertionError(f"compact context exposed a raw workflow identity: {raw_identity}")
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
                    "context": {
                        "protocol_id": expected_protocol["protocol_id"],
                        "leaf_id": expected_leaf["leaf_id"],
                        "run_id": expected_leaf["run_id"],
                        "visible": populated_matcher_context,
                    },
                    "det_polylines": det_polylines,
                    "error_pairs_at_0_9": error_pairs[:6],
                }
            )
            # QS-Y86/Y87/Z91/Z92: open the recorded match evidence for the first error pair.
            page.locator(
                'main > div[data-view="matchers"]:not(.hidden-view) [data-testid=evidence-open]'
            ).first.click()
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
            page.wait_for_function(
                "expected => document.querySelector('[data-context-key=evidence]')?.getAttribute('data-run-id') === expected",
                arg=expected_leaf["run_id"],
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
            page.wait_for_function(
                "() => { const control = document.querySelector('[data-testid=utility-measure]'); return control?.tagName === 'SELECT' && !control.disabled && control.options.length > 0; }",
                timeout=60_000,
            )
            selected_quality_job = page.input_value("[data-testid=utility-job]")
            measure_catalog = page.evaluate(
                "async ([projectId, jobId]) => { const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(jobId)}/quality-measures`); if (!response.ok) throw new Error(`${response.status} ${await response.text()}`); return response.json(); }",
                [console_project_id_holder[0], selected_quality_job],
            )
            measure_options = page.evaluate(
                "() => Array.from(document.querySelectorAll('[data-testid=utility-measure] option')).map(option => option.value)"
            )
            if set(measure_options) != set(measure_catalog["measures"]) or len(measure_options) != len(measure_catalog["measures"]):
                raise AssertionError(
                    f"utility selector diverged from selected job catalog: {measure_options} vs {measure_catalog['measures']}"
                )
            expected_measure_groups = [
                {
                    "id": "overall",
                    "label": "Overall quality",
                    "measures": ["UnifiedQualityScore"],
                },
                {
                    "id": "capture-illumination",
                    "label": "Capture & illumination",
                    "measures": [
                        "BackgroundUniformity", "IlluminationUniformity", "LuminanceMean",
                        "LuminanceVariance", "UnderExposurePrevention", "OverExposurePrevention",
                        "DynamicRange", "Sharpness", "CompressionArtifacts", "NaturalColour",
                    ],
                },
                {
                    "id": "face-geometry-crop-pose",
                    "label": "Face geometry, crop & pose",
                    "measures": [
                        "SingleFacePresent", "InterEyeDistance", "HeadSize",
                        "LeftwardCropOfTheFaceImage", "RightwardCropOfTheFaceImage",
                        "MarginAboveOfTheFaceImage", "MarginBelowOfTheFaceImage",
                        "HeadPoseYaw", "HeadPosePitch", "HeadPoseRoll",
                    ],
                },
                {
                    "id": "visibility-occlusion-expression",
                    "label": "Visibility, occlusion & expression",
                    "measures": [
                        "EyesOpen", "MouthClosed", "EyesVisible", "MouthOcclusionPrevention",
                        "FaceOcclusionPrevention", "ExpressionNeutrality", "NoHeadCoverings",
                    ],
                },
            ]
            rendered_measure_groups = page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-testid=utility-measure] optgroup')).map(group => ({
                    id: group.getAttribute('data-quality-group'),
                    label: group.getAttribute('label'),
                    measures: Array.from(group.querySelectorAll('option')).map(option => option.value),
                }))"""
            )
            if rendered_measure_groups != expected_measure_groups:
                raise AssertionError(
                    f"utility selector quality groups diverged from the inspector profile: {rendered_measure_groups}"
                )
            if page.input_value("[data-testid=utility-measure]") != measure_catalog["default_measure"]:
                raise AssertionError(
                    f"utility selector did not choose the recorded default measure: {measure_catalog}"
                )
            page.select_option("[data-testid=utility-measure]", "Sharpness")
            page.fill("[data-testid=utility-q]", "40")
            page.fill("[data-testid=utility-replicates]", "200")
            page.select_option("[data-testid=utility-policy]", "pair.quality.min")
            page.click("[data-testid=utility-run]")
            page.wait_for_selector("[data-testid=utility-detail]", timeout=300_000)
            page.wait_for_function(
                "() => (document.querySelector('[data-testid=utility-detail] h3')?.innerText || '').includes('Sharpness')",
                timeout=60_000,
            )
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
                    "measure_catalog_count": len(measure_catalog["measures"]),
                    "selected_quality_measure": "Sharpness",
                    "rejected_pairs": rejected_pairs[:6],
                    "detail_heading": page.inner_text("[data-testid=utility-detail] h3"),
                }
            )
            utility_run_count = recorded_run_count()
            utility_run_id = latest_recorded_run_id()
            utility_summaries = json.load(urllib.request.urlopen(  # noqa: S310
                f"http://127.0.0.1:{API_PORT}/api/projects/"
                f"{urllib.parse.quote(console_project_id_holder[0], safe='')}/utility-analyses"
            ))
            utility_summary = next(item for item in utility_summaries if item["run_id"] == utility_run_id)
            utility_identity = utility_summary["overlay_identity"]
            page.wait_for_function(
                "expected => document.querySelector('[data-context-key=protocol]')?.getAttribute('data-protocol-id') === expected.protocolId "
                "&& document.querySelector('[data-context-key=matcher]')?.getAttribute('data-leaf-id') === expected.leafId "
                "&& document.querySelector('[data-context-key=evidence]')?.getAttribute('data-run-id') === expected.runId",
                arg={
                    "protocolId": utility_identity["protocol_id"],
                    "leafId": utility_summary["leaf_id"],
                    "runId": utility_run_id,
                },
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
                    "active_workflow_context": {
                        "protocol_id": utility_identity["protocol_id"],
                        "leaf_id": utility_summary["leaf_id"],
                        "run_id": utility_run_id,
                    },
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
            operator_summaries = page.evaluate("() => document.querySelectorAll('[data-testid=dashboard-operator-summary]').length")
            non_figure_panels = page.evaluate("() => document.querySelectorAll('[data-testid=dashboard-panel]:not([data-panel-kind=figure])').length")
            if operator_summaries != non_figure_panels or operator_summaries < 1:
                raise AssertionError(f"non-figure dashboard panels lack typed operator summaries: {operator_summaries}/{non_figure_panels}")
            if page.locator("[data-testid=dashboard-panel] > pre").count() != 0:
                raise AssertionError("a dashboard panel still presents raw JSON as primary content")
            raw_panels = page.locator("[data-testid=dashboard-raw-data]")
            if raw_panels.count() != non_figure_panels:
                raise AssertionError(f"secondary raw panel disclosures are incomplete: {raw_panels.count()}/{non_figure_panels}")
            if any(raw_panels.nth(index).get_attribute("open") is not None for index in range(raw_panels.count())):
                raise AssertionError("secondary raw dashboard data is expanded by default")
            first_raw_summary = raw_panels.first.locator("summary")
            first_raw_summary.focus()
            page.keyboard.press("Enter")
            if raw_panels.first.get_attribute("open") is None:
                raise AssertionError("secondary raw dashboard disclosure is not keyboard operable")
            page.keyboard.press("Enter")
            if raw_panels.first.get_attribute("open") is not None:
                raise AssertionError("secondary raw dashboard disclosure did not collapse from the keyboard")
            dashboard_message = page.inner_text("[data-testid=project-message]") if page.query_selector("[data-testid=project-message]") else None
            disclosure = page.locator("[data-testid=report-disclosure-warning]")
            disclosure_text = (disclosure.text_content() or "").lower()
            if not disclosure.is_visible() or disclosure.get_attribute("role") != "note" or "disclosure review" not in disclosure_text or "deployment-local provenance" not in disclosure_text:
                raise AssertionError("governed report/export downloads lack an adjacent accessible disclosure-review warning")
            quality_export = page.locator('[data-governed-download="quality-csv"]')
            if quality_export.count() != 1 or not quality_export.is_enabled():
                raise AssertionError("the real quality export action is unavailable")
            with page.expect_download(timeout=60_000) as export_download_info:
                quality_export.click()
            export_download = export_download_info.value
            export_path = export_download.path()
            if not export_download.suggested_filename.endswith(".csv"):
                raise AssertionError(
                    f"quality export filename is not CSV: {export_download.suggested_filename}"
                )
            if export_path is None or not export_path.is_file():
                raise AssertionError("the browser did not persist the quality export response")
            export_bytes = export_path.read_bytes()
            if not export_bytes:
                raise AssertionError("the governed quality export is empty")
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
            bundle_download = page.locator('[data-governed-download="qsexp"]')
            if bundle_download.count() != 1 or bundle_download.get_attribute("aria-describedby") != "report-disclosure-warning-text":
                raise AssertionError("the .qsexp download is not bound to the disclosure-review warning")
            page.click("[data-testid=reproduce]")
            page.wait_for_selector("[data-testid=reproduction-state]", timeout=300_000)
            reproduction_states = page.evaluate("() => Array.from(document.querySelectorAll('[data-testid=reproduction-state]')).map(e => e.innerText)")
            observation["steps"].append(
                {
                    "step": "reports",
                    "figure_facts": figure_facts,
                    "figures": page.evaluate("() => document.querySelectorAll('[data-testid=figure-thumb]').length"),
                    "dashboard_panels": dashboard_panels,
                    "dashboard_operator_summaries": operator_summaries,
                    "dashboard_raw_disclosures_collapsed": raw_panels.count(),
                    "dashboard_message": dashboard_message,
                    "download_disclosure": (disclosure.text_content() or "").strip(),
                    "export_filename": export_download.suggested_filename,
                    "export_size_bytes": len(export_bytes),
                    "export_sha256": hashlib.sha256(export_bytes).hexdigest(),
                    "qsexp_disclosure_bound": True,
                    "dashboard_panel_run_ids": dashboard_catalog[-1]["panel_run_ids"],
                    "report_rows": report_rows,
                    "report_dataset_id": report_catalog[-1]["dataset_id"],
                    "reproduction_states": reproduction_states,
                }
            )
            # QS-X60/QS-X61: 1:N identification over the fixture subjects and a controlled-degradation response through the console.
            goto("advanced")
            beta_boundary = page.locator("[data-testid=advanced-beta-boundary]")
            beta_boundary_text = (beta_boundary.text_content() or "").lower()
            for phrase in ("research extension", "outside beta acceptance", "single-image stress test", "1:n", "container"):
                if phrase not in beta_boundary_text:
                    raise AssertionError(f"Advanced beta boundary omits {phrase!r}: {beta_boundary_text!r}")
            if not beta_boundary.is_visible() or beta_boundary.get_attribute("role") != "note":
                raise AssertionError("Advanced beta boundary is not a visible accessible note")
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
                    "beta_boundary": (beta_boundary.text_content() or "").strip(),
                    "identification_rows": ident_rows,
                    "derived_set_transform": {"selected": selected_transforms, "jpeg_parameters": jpeg_parameters},
                    "degradation_tables": degradation_tables,
                }
            )
            advanced_run_count = recorded_run_count()
            advanced_run_id = latest_recorded_run_id()
            page.wait_for_function(
                "expected => document.querySelector('[data-context-key=evidence]')?.getAttribute('data-run-id') === expected",
                arg=advanced_run_id,
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

            # B20: revisit Home after the real recorded workflows and verify that
            # prerequisites, measure count, and the merged recent-work feed are
            # all derived from the active project's APIs.
            active_home_project_id = page.evaluate("() => window.localStorage.getItem('qualsight.activeProjectId')")
            if not active_home_project_id:
                raise AssertionError("populated Home validation has no active project context")
            encoded_home_project = urllib.parse.quote(active_home_project_id, safe="")

            def home_feed(path: str):
                return json.load(urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}{path}"))  # noqa: S310

            home_runs_api = home_feed(f"/api/projects/{encoded_home_project}/runs?limit=10000")
            home_jobs_api = home_feed(f"/api/projects/{encoded_home_project}/jobs")
            home_utility_api = home_feed(f"/api/projects/{encoded_home_project}/utility-analyses")
            home_degradation_api = home_feed(f"/api/projects/{encoded_home_project}/degradation-analyses")
            home_figures_api = home_feed(f"/api/projects/{encoded_home_project}/figures")
            home_reports_api = home_feed(f"/api/projects/{encoded_home_project}/reports")
            home_bundles_api = home_feed(f"/api/projects/{encoded_home_project}/bundles")
            completed_home_jobs = sorted(
                (job for job in home_jobs_api if job["state"] == "COMPLETED" and job.get("ended_at")),
                key=lambda job: job["ended_at"],
                reverse=True,
            )
            if not completed_home_jobs:
                raise AssertionError("populated Home validation has no completed quality job")
            home_measure_catalog_api = home_feed(
                f"/api/projects/{encoded_home_project}/jobs/"
                f"{urllib.parse.quote(completed_home_jobs[0]['job_id'], safe='')}/quality-measures"
            )

            expected_recent_records = []
            for kind, items, timestamp in (
                ("run", home_runs_api, "end_time"),
                ("utility", home_utility_api, "created_at"),
                ("degradation", home_degradation_api, "created_at"),
                ("figure", home_figures_api, "created_at"),
                ("report", home_reports_api, "created_at"),
                ("bundle", home_bundles_api, "created_at"),
            ):
                expected_recent_records.extend(
                    {"kind": kind, "recordedAt": item[timestamp]}
                    for item in items
                    if item.get(timestamp)
                )
            expected_recent_records = sorted(
                expected_recent_records,
                key=lambda item: item["recordedAt"],
                reverse=True,
            )[:6]

            goto("home")
            page.wait_for_function(
                "projectId => document.querySelector('[data-testid=home-panel]')?.getAttribute('data-project-id') === projectId && document.querySelector('[data-testid=home-panel]')?.getAttribute('data-state') === 'ready'",
                arg=active_home_project_id,
                timeout=60_000,
            )
            populated_home_tasks = page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-testid=home-task]')).map(item => ({
                    label: item.querySelector('strong')?.textContent?.trim(),
                    href: item.getAttribute('href'),
                    disabled: item.getAttribute('aria-disabled'),
                    reason: item.querySelector('[data-testid=home-task-reason]')?.textContent?.trim() || null,
                }))"""
            )
            expected_enabled_home_tasks = [
                (
                    {
                        **task,
                        "href": None,
                        "disabled": "true",
                        "reason": "Upload and assess a working image before stress-testing it.",
                    }
                    if task["label"] == "Stress-test an image"
                    else {**task, "disabled": None, "reason": None}
                )
                for task in expected_home_tasks
            ]
            if populated_home_tasks != expected_enabled_home_tasks:
                raise AssertionError(f"populated Home task prerequisites diverged from real project evidence: {populated_home_tasks}")
            populated_measure_count = page.evaluate(
                """() => ({
                    value: Number(document.querySelector('[data-testid=home-measure-count]')?.textContent),
                    state: document.querySelector('[data-testid=home-measure-count]')?.getAttribute('data-state'),
                })"""
            )
            expected_measure_count = len(home_measure_catalog_api["measures"])
            if populated_measure_count != {"value": expected_measure_count, "state": "recorded"}:
                raise AssertionError(
                    "Home OFIQ measure count diverged from the selected completed job catalog: "
                    f"observed={populated_measure_count} expected={expected_measure_count}"
                )
            populated_run_count = page.evaluate(
                """() => ({
                    value: Number(document.querySelector('[data-testid=home-run-count]')?.textContent),
                    state: document.querySelector('[data-testid=home-run-count]')?.getAttribute('data-state'),
                })"""
            )
            if populated_run_count != {"value": len(home_runs_api), "state": "recorded"}:
                raise AssertionError(
                    "Home Run count diverged from the active project feed: "
                    f"observed={populated_run_count} expected={len(home_runs_api)}"
                )
            populated_recent_records = page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-testid=home-recent-item]')).map(item => ({
                    kind: item.getAttribute('data-kind'),
                    recordedAt: item.getAttribute('data-recorded-at'),
                }))"""
            )
            if populated_recent_records != expected_recent_records:
                raise AssertionError(
                    "Home recent work does not merge the real project analysis feeds in recorded-time order: "
                    f"observed={populated_recent_records} expected={expected_recent_records}"
                )
            if page.locator("[data-testid=home-feed-errors]").count() != 0:
                raise AssertionError(f"Home reported feed errors on healthy real APIs: {page.inner_text('[data-testid=home-feed-errors]')}")
            home_populated_shot = args.screenshot_dir / "qualsight-home-populated.png"
            page.screenshot(path=str(home_populated_shot), full_page=True)
            observation["steps"].append(
                {
                    "step": "guided-home-populated",
                    "project_id": active_home_project_id,
                    "task_count": len(populated_home_tasks),
                    "measure_count": populated_measure_count["value"],
                    "run_count": populated_run_count["value"],
                    "recent_records": populated_recent_records,
                    "screenshot": str(home_populated_shot),
                }
            )
            goto("projects")

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
                # Route activation refreshes the admitted catalog. Exercise the
                # first rendered control, and wait for that exact control to become
                # enabled instead of silently selecting a later enabled sibling.
                target = page.locator(selector).first
                target.wait_for(state="visible", timeout=60_000)
                page.wait_for_function(
                    "selector => { const target = document.querySelector(selector); "
                    "return target !== null && target.matches(':disabled') === false; }",
                    arg=selector,
                    timeout=60_000,
                )
                if not target.evaluate("(element, selector) => element === document.querySelector(selector)", selector):
                    raise AssertionError(f"{view} responsive gate did not retain the first rendered control")
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
