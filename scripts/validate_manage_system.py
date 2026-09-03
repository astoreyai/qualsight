"""Validate the Manage console against the live local control plane.

The gate uses the production frontend build and a temporary private QualSight
data directory. It first observes the real unavailable-control-plane state,
then starts the API, retries through the UI, and validates the catalog and
lifecycle surface against live API responses.
"""

from __future__ import annotations

import argparse
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


ROOT = Path(__file__).resolve().parents[1]
API_PORT = 8480
PREVIEW_PORT = 5196
NODE_EXECUTABLE = shutil.which("node")
if NODE_EXECUTABLE is None:
    raise RuntimeError("the reviewed Node executable is not available on PATH")
NODE_BIN = str(Path(NODE_EXECUTABLE).parent)
SYSTEM_AREAS = [
    "Add-ons",
    "Processing profiles",
    "Connections",
    "Runtime & storage",
    "Audit",
]


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


def _json(url: str) -> dict | list:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - loopback only
        return json.load(response)


def _stop(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def _assert_frontend_privacy_contract() -> None:
    api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    system_source = (ROOT / "frontend" / "src" / "SystemPanel.tsx").read_text(encoding="utf-8")
    bundle_contract = api_source.split("export interface BundleOut {", 1)[1].split("\n}", 1)[0]
    if "path:" in bundle_contract:
        raise AssertionError("public BundleOut still exposes a private filesystem path")
    matcher_contract = api_source.split("export interface MatcherAssetOut {", 1)[1].split("export interface MatcherAddonOut", 1)[0]
    dataset_contract = api_source.split("export interface DatasetAddonOut {", 1)[1].split("export interface PreprocessingAddonOut", 1)[0]
    receipt_contract = api_source.split("pullVerificationDataset:", 1)[1].split(
        "importVerificationDataset:", 1
    )[0]
    forbidden = {
        "MatcherAssetOut": [marker for marker in ("path:", "managed_path:") if marker in matcher_contract],
        "DatasetAddonOut": [marker for marker in ("local_root:", "path:") if marker in dataset_contract],
        "acquisition receipts": [marker for marker in ("path:",) if marker in receipt_contract],
    }
    exposed = {contract: markers for contract, markers in forbidden.items() if markers}
    if exposed:
        raise AssertionError(f"frontend add-on contracts still expose private path fields: {exposed!r}")
    if "location_state:" not in matcher_contract:
        raise AssertionError("MatcherAssetOut omits the public checkpoint location state")
    if 'location_state: "MANAGED" | "REGISTERED_EXTERNAL" | "LEGACY_EXTERNAL";' not in matcher_contract:
        raise AssertionError(
            "MatcherAssetOut does not model the real LEGACY_EXTERNAL checkpoint state"
        )
    for marker in (
        'selectedAsset.checkpoint.location_state === "LEGACY_EXTERNAL"',
        "Download verified checkpoint",
    ):
        if marker not in system_source:
            raise AssertionError(
                "Manage does not preserve the admitted legacy checkpoint acquisition: "
                f"missing {marker!r}"
            )
    readiness_contract = api_source.split("type ReadyDatabaseCheck", 1)[1].split(
        "export interface ReadyOut", 1
    )[0]
    if (
        'error_code: "DATABASE_UNAVAILABLE"' not in readiness_contract
        or 'error_code: "STORAGE_UNAVAILABLE"' not in readiness_contract
        or "error: string" in readiness_contract
    ):
        raise AssertionError("readiness failure types do not model the backend error_code contract")
    for marker in ("ready.checks.storage.error_code", "ready.checks.database.error_code"):
        if marker not in system_source:
            raise AssertionError(f"Runtime readiness does not render stable failure identity {marker}")
    sequencing_markers = (
        "projectActionSequence",
        "requestedProjectId",
        "requestSequence !== projectActionSequence.current",
    )
    missing_sequencing = [marker for marker in sequencing_markers if marker not in system_source]
    if missing_sequencing:
        raise AssertionError(
            "dataset import completion is not bound to the initiating project epoch: "
            f"{missing_sequencing!r}"
        )


def _assert_receipt_scan_contract() -> None:
    api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    receipt_contract = api_source.split("pullVerificationDataset:", 1)[1].split(
        "importVerificationDataset:", 1
    )[0]
    required_fields = (
        "receipt:",
        "receipt_id:",
        "evidence_sha256:",
        "archive:",
        "sha256:",
        "size_bytes:",
    )
    missing = [field for field in required_fields if field not in receipt_contract]
    if missing:
        raise AssertionError(
            "dataset receipt privacy scan is not bound to its full response contract: "
            f"{missing!r}"
        )


def _assert_path_free_public_payload(value: object, location: str = "response") -> None:
    path_key = re.compile(r"(?:^path$|_path$|^local_root$|^repository_root$|^data_dir$|^directory$)")
    path_value = re.compile(
        r"(?:"
        r"(?:^|[\s\"'`([{=:])/(?!/|api(?:/|$))[^\s\"'`)}>\]]+"
        r"|(?:^|[\s\"'`([{=])[A-Za-z]:[\\/]"
        r"|(?:^|[\s\"'`([{=])(?:\\\\|(?<!:)//)[^\s\"'`)}>\]]+"
        r"|(?:^|[\s\"'`([{=])file:/{1,3}"
        r"|(?:^|[\s\"'`([{=])~(?:[\\/]|$)"
        r")",
        re.IGNORECASE,
    )
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{location}.{key}"
            if path_key.search(str(key)):
                raise AssertionError(f"public payload exposes a private path key at {child}")
            _assert_path_free_public_payload(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_path_free_public_payload(item, f"{location}[{index}]")
    elif isinstance(value, str) and path_value.search(value.strip()):
        raise AssertionError(f"public payload exposes a private path value at {location}")


def _assert_embedded_path_detector_contract() -> None:
    real_path = ROOT / "frontend" / "src" / "SystemPanel.tsx"
    for message in (
        f"runtime failed while reading {real_path}",
        f"runtime failure:{real_path}",
    ):
        try:
            _assert_path_free_public_payload(
                {"message": message},
                "embedded-path-probe",
            )
        except AssertionError:
            continue
        raise AssertionError("recursive privacy check accepts an embedded absolute filesystem path")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()

    _assert_frontend_privacy_contract()
    _assert_receipt_scan_contract()
    _assert_embedded_path_detector_contract()
    if not _port_is_free(API_PORT) or not _port_is_free(PREVIEW_PORT):
        raise RuntimeError(f"ports {API_PORT}/{PREVIEW_PORT} must be free")
    args.observation.parent.mkdir(parents=True, exist_ok=True)
    if args.screenshot is not None:
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)

    data_dir = Path(tempfile.mkdtemp(prefix="qualsight-manage-system-"))
    environment = {
        **os.environ,
        "PATH": f"{NODE_BIN}:{os.environ.get('PATH', '')}",
        "QUALSIGHT_REPOSITORY_ROOT": os.environ.get(
            "QUALSIGHT_REPOSITORY_ROOT", str(ROOT)
        ),
        "QUALSIGHT_DATA_DIR": str(data_dir),
    }
    api_process: subprocess.Popen[bytes] | None = None
    preview_process: subprocess.Popen[bytes] | None = None
    observation: dict[str, object] = {
        "kind": "QUALSIGHT_MANAGE_SYSTEM_VALIDATION",
        "data_source": "live local control plane",
        "steps": [],
        "passed": False,
    }
    exit_code = 1
    try:
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
        _wait_http(f"http://127.0.0.1:{PREVIEW_PORT}/", 60)

        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            observation["browser"] = f"chromium {browser.version}"
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            rendered_api: dict[str, dict] = {}
            system_payloads: list[tuple[str, object]] = []

            def capture_rendered_api(response) -> None:
                route_path = urllib.parse.urlparse(response.url).path
                rendered_route = next(
                    (route for route in ("/api/ready", "/api/metrics") if route_path == route),
                    None,
                )
                system_route = (
                    route_path in {
                        "/api/health",
                        "/api/ready",
                        "/api/metrics",
                        "/api/resources",
                        "/api/addons",
                        "/api/matcher-assets",
                        "/api/system-events",
                    }
                    or route_path.startswith("/api/matcher-assets/")
                    or "/dataset-assets/" in route_path
                )
                if not system_route or response.status < 200 or response.status >= 600:
                    return
                try:
                    body = response.json()
                except Exception:  # noqa: BLE001 - non-JSON error responses are not rendered raw
                    return
                system_payloads.append((f"{response.request.method} {route_path}", body))
                if rendered_route is not None and isinstance(body, dict):
                    rendered_api[rendered_route] = body

            page.on("response", capture_rendered_api)
            page.add_init_script(
                """(() => {
                    window.__manageLoadingObserved = false;
                    const mark = () => {
                        if (document.querySelector('[data-testid=system-loading]')) {
                            window.__manageLoadingObserved = true;
                        }
                    };
                    new MutationObserver(mark).observe(document, {subtree: true, childList: true});
                    document.addEventListener('DOMContentLoaded', mark);
                })()"""
            )
            page.goto(f"http://127.0.0.1:{PREVIEW_PORT}/#/system", wait_until="domcontentloaded")
            page.locator("[data-testid=system-panel]").wait_for(state="visible", timeout=30_000)
            area_labels = page.get_by_role("tab").all_text_contents()
            if area_labels != SYSTEM_AREAS:
                raise AssertionError(
                    f"Manage areas differ from the accepted beta structure: {area_labels}"
                )
            page.locator("[data-testid=system-error-state]").wait_for(state="visible", timeout=30_000)
            if not page.evaluate("() => window.__manageLoadingObserved === true"):
                raise AssertionError("Manage loading evidence was not observed before the real API failure")
            error_text = page.locator("[data-testid=system-error-state]").inner_text()
            if "Manage data is unavailable" not in error_text or page.get_by_role("button", name="Retry", exact=True).count() != 1:
                raise AssertionError(f"Manage error/retry state is incomplete: {error_text}")
            observation["steps"].append({"step": "loading-error", "loading_observed": True, "error": error_text})

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
            _wait_http(f"http://127.0.0.1:{API_PORT}/api/health", 90)
            page.get_by_role("button", name="Retry", exact=True).click()
            page.wait_for_function(
                "() => document.querySelector('[data-testid=system-panel]')?.getAttribute('aria-busy') === 'false'",
                timeout=60_000,
            )
            page.locator("[data-testid=matcher-assets] tbody tr").first.wait_for(state="visible", timeout=60_000)

            health = _json(f"http://127.0.0.1:{API_PORT}/api/health")
            if not isinstance(health, dict):
                raise TypeError("live /api/health did not return an object")
            engine_badge = page.locator("[data-testid=engine-badge]").inner_text()
            if "Control plane unavailable" in engine_badge or health["engine"]["version"] not in engine_badge:
                raise AssertionError(f"App control-plane banner did not recover with the successful Manage retry: {engine_badge}")

            catalog = _json(f"http://127.0.0.1:{API_PORT}/api/addons")
            matcher_assets = _json(f"http://127.0.0.1:{API_PORT}/api/matcher-assets")
            readiness = rendered_api.get("/api/ready")
            metrics = rendered_api.get("/api/metrics")
            resources = _json(f"http://127.0.0.1:{API_PORT}/api/resources")
            if not isinstance(catalog, dict) or not isinstance(health, dict) or not isinstance(readiness, dict) or not isinstance(metrics, dict) or not isinstance(resources, list):
                raise TypeError("live Manage APIs returned an unexpected top-level shape")
            _assert_path_free_public_payload(catalog, "/api/addons")
            _assert_path_free_public_payload(matcher_assets, "/api/matcher-assets")
            _assert_path_free_public_payload(health, "/api/health")
            _assert_path_free_public_payload(readiness, "/api/ready")
            _assert_path_free_public_payload(metrics, "/api/metrics")
            _assert_path_free_public_payload(resources, "/api/resources")

            matcher_rows = page.locator("[data-testid=matcher-assets] tbody tr")
            matcher_caption = page.locator("[data-testid=matcher-assets] > caption").inner_text().strip()
            if not matcher_caption:
                raise AssertionError("matcher add-on table is missing a meaningful caption")
            if matcher_rows.count() != len(catalog["matchers"]):
                raise AssertionError(f"matcher list count differs from catalog: DOM={matcher_rows.count()} API={len(catalog['matchers'])}")
            legacy_candidates = [
                matcher
                for matcher in catalog["matchers"]
                if matcher["checkpoint"]["state"] == "VERIFIED"
                and matcher["checkpoint"]["location_state"] == "LEGACY_EXTERNAL"
                and matcher["checkpoint_acquisition"]["available"]
            ]
            if not legacy_candidates:
                raise AssertionError(
                    "fresh real catalog exposes no verified LEGACY_EXTERNAL checkpoint with admitted acquisition"
                )
            legacy_matcher = legacy_candidates[0]
            legacy_search = page.get_by_label("Search matchers")
            legacy_search.fill(legacy_matcher["matcher_id"])
            matcher_rows.first.get_by_role("button").click()
            legacy_detail = page.locator("[data-testid=matcher-detail]")
            if "Legacy external checkpoint" not in legacy_detail.inner_text():
                raise AssertionError("Manage does not render the LEGACY_EXTERNAL location truthfully")
            checkpoint_download = legacy_detail.get_by_role(
                "button", name="Download verified checkpoint", exact=True
            )
            if checkpoint_download.count() != 1 or not checkpoint_download.is_enabled():
                raise AssertionError(
                    "verified LEGACY_EXTERNAL checkpoint does not expose its admitted managed acquisition"
                )
            checkpoint_download.click()
            checkpoint_dialog = page.get_by_role("dialog")
            checkpoint_dialog.wait_for(state="visible", timeout=10_000)
            checkpoint_dialog.get_by_role(
                "button", name="Confirm exact target", exact=True
            ).click()
            legacy_detail.get_by_text("Managed checkpoint verified", exact=True).wait_for(
                timeout=180_000
            )
            refreshed_assets = _json(
                f"http://127.0.0.1:{API_PORT}/api/matcher-assets"
            )
            refreshed_legacy = next(
                asset
                for asset in refreshed_assets
                if asset["matcher_id"] == legacy_matcher["matcher_id"]
            )
            if (
                refreshed_legacy["checkpoint"]["state"] != "VERIFIED"
                or refreshed_legacy["checkpoint"]["location_state"] != "MANAGED"
            ):
                raise AssertionError(
                    "managed checkpoint acquisition did not preserve verified identity and reach MANAGED"
                )
            observation["steps"].append(
                {
                    "step": "legacy-external-checkpoint-acquisition",
                    "matcher_id": legacy_matcher["matcher_id"],
                    "initial_location_state": "LEGACY_EXTERNAL",
                    "final_location_state": "MANAGED",
                    "registered_sha256": legacy_matcher["checkpoint"]["registered_sha256"],
                    "registered_size_bytes": legacy_matcher["checkpoint"]["registered_size_bytes"],
                }
            )
            catalog = _json(f"http://127.0.0.1:{API_PORT}/api/addons")
            if not isinstance(catalog, dict):
                raise TypeError("refreshed /api/addons did not return an object")
            _assert_path_free_public_payload(catalog, "/api/addons:after-checkpoint-acquisition")
            first_matcher = catalog["matchers"][0]
            search = legacy_search
            search.fill(first_matcher["matcher_id"])
            if matcher_rows.count() != 1:
                raise AssertionError(f"exact matcher search returned {matcher_rows.count()} rows")
            matcher_rows.first.get_by_role("button").click()
            detail = page.locator("[data-testid=matcher-detail]")
            if detail.get_attribute("data-matcher-id") != first_matcher["matcher_id"]:
                raise AssertionError("matcher selection did not bind the detail panel to the exact row")
            if detail.locator("[data-field=checkpoint-sha256]").inner_text().strip() != first_matcher["checkpoint"]["registered_sha256"]:
                raise AssertionError("selected matcher checkpoint SHA-256 was shortened or changed")
            expected_checkpoint_size = f"{first_matcher['checkpoint']['registered_size_bytes']:,} bytes"
            if detail.locator("[data-field=checkpoint-size]").inner_text().strip() != expected_checkpoint_size:
                raise AssertionError("selected matcher checkpoint size differs from the catalog")
            location_state = first_matcher["checkpoint"]["location_state"]
            expected_location = (
                "Managed by QualSight"
                if location_state == "MANAGED"
                else "Legacy external checkpoint"
                if location_state == "LEGACY_EXTERNAL"
                else "Registered external checkpoint"
            )
            if expected_location not in detail.inner_text():
                raise AssertionError(
                    f"checkpoint location state {location_state} is not rendered truthfully"
                )
            source_cards = detail.locator("[data-testid=matcher-sources] > article")
            if source_cards.count() != len(first_matcher["architecture_sources"]):
                raise AssertionError("selected matcher source count differs from the catalog")
            for source in first_matcher["architecture_sources"]:
                source_text = detail.locator(f"[data-source-key='{source['key']}']").inner_text()
                if source["sha256"] not in source_text or f"{source['size_bytes']:,} bytes" not in source_text or source["commit"] not in source_text:
                    raise AssertionError(f"source identity is incomplete for {source['key']}: {source_text}")
            detail_text = detail.inner_text()
            for exact_value in (
                first_matcher["third_party_rights_status"],
                first_matcher["redistribution_status"],
                first_matcher["checkpoint_acquisition"]["reason"],
                first_matcher["driver_id"],
            ):
                if exact_value not in detail_text:
                    raise AssertionError(f"selected matcher detail omitted catalog evidence: {exact_value}")
            if detail.locator("[data-field=active-leases]").inner_text().strip() != str(first_matcher["runtime"]["active_leases"]):
                raise AssertionError("selected matcher lease count differs from the catalog")
            search.fill("")
            available_datasets = [item for item in catalog["datasets"] if item["acquisition"]["available"]]
            if not available_datasets:
                raise AssertionError("the real catalog did not expose an admitted downloadable dataset")
            published_dataset = available_datasets[0]
            published_text = page.locator(
                f"[data-testid=dataset-addons] [data-addon-id='{published_dataset['addon_id']}']"
            ).inner_text()
            published_uri = published_dataset["acquisition"].get("source_uri")
            if not published_uri or published_uri not in published_text:
                raise AssertionError(
                    "downloadable dataset omitted its exact registered publisher URL: "
                    f"addon={published_dataset['addon_id']!r} uri={published_uri!r} card={published_text!r}"
                )
            observation["steps"].append(
                {
                    "step": "compact-matchers",
                    "count": matcher_rows.count(),
                    "selected": first_matcher["matcher_id"],
                    "sources": source_cards.count(),
                    "dataset_publisher": {
                        "addon_id": published_dataset["addon_id"],
                        "source_uri": published_uri,
                    },
                }
            )

            add_ons_tab = page.get_by_role("tab", name="Add-ons", exact=True)
            add_ons_tab.focus()
            add_ons_tab.press("End")
            if page.evaluate("() => document.activeElement?.textContent?.trim()") != "Audit":
                raise AssertionError("End did not move the tab focus and selection to Audit")
            page.get_by_role("tab", name="Audit", exact=True).press("Home")
            if page.evaluate("() => document.activeElement?.textContent?.trim()") != "Add-ons":
                raise AssertionError("Home did not return the tab focus and selection to Add-ons")
            page.get_by_role("tab", name="Add-ons", exact=True).press("ArrowRight")
            if page.evaluate("() => document.activeElement?.textContent?.trim()") != "Processing profiles":
                raise AssertionError("ArrowRight did not enter Processing profiles")

            profile_rows = page.locator("[data-testid=preprocessing-addons] > article")
            transform_rows = page.locator("[data-testid=transform-addons] tbody tr")
            if profile_rows.count() != len(catalog["preprocessing"]):
                raise AssertionError("preprocessing profile count differs from the API catalog")
            if transform_rows.count() != len(catalog["transforms"]) or transform_rows.count() != 7:
                raise AssertionError(f"controlled transform count differs from the API: {transform_rows.count()}")
            for profile in catalog["preprocessing"]:
                text = page.locator(f"[data-testid=preprocessing-addons] [data-addon-id='{profile['addon_id']}']").inner_text()
                if "PER RUN" not in text or profile["definition"]["alignment"] not in text or profile["driver_id"] not in text:
                    raise AssertionError(f"preprocessing contract is incomplete for {profile['addon_id']}")
            for transform in catalog["transforms"]:
                text = page.locator(f"[data-testid=transform-addons] [data-addon-id='{transform['addon_id']}']").inner_text()
                contract = transform["parameter_contract"]
                for exact_value in (transform["parameter"], contract["guidance"], str(contract["minimum"]), str(contract["maximum"])):
                    if exact_value not in text:
                        raise AssertionError(f"transform contract is incomplete for {transform['addon_id']}: {exact_value}")
            observation["steps"].append({"step": "processing", "profiles": profile_rows.count(), "transforms": transform_rows.count()})

            page.get_by_role("tab", name="Connections", exact=True).click()
            connection = page.locator("[data-testid=connection-addons]")
            if connection.get_attribute("data-provider-count") != "0" or catalog["connections"] != []:
                raise AssertionError("Connections invented an admitted provider")
            if page.get_by_role("button", name="Add connection", exact=True).count() != 0:
                raise AssertionError("Connections exposes an actionable Add connection control")
            requirements = connection.locator("[data-testid=connection-requirements] li").all_text_contents()
            if requirements != catalog["connection_policy"]["admission_requires"]:
                raise AssertionError("connection requirements differ from the backend policy")

            page.get_by_role("tab", name="Runtime & storage", exact=True).click()
            runtime_text = page.locator("[data-testid=runtime-health]").inner_text()
            for forbidden_key in ("database_path", "repository_root", "data_dir"):
                if forbidden_key in health:
                    raise AssertionError(f"public health evidence exposed private host field {forbidden_key}")
            for exact_value in (health["application_version"], health["engine"]["version"], health["registry_sha256"]):
                if exact_value not in runtime_text:
                    raise AssertionError(f"runtime identity omitted live health evidence: {exact_value}")
            readiness_text = page.locator("[data-testid=runtime-readiness]").inner_text()
            storage = readiness["checks"]["storage"]
            if storage["ok"]:
                if "path" in storage:
                    raise AssertionError("public readiness storage evidence exposed its private host path")
                for exact_value in (storage["mode"], f"{storage['available_bytes']:,} bytes"):
                    if exact_value not in readiness_text:
                        raise AssertionError(f"runtime storage omitted readiness evidence: {exact_value}")
            runtime_artifacts = readiness["checks"]["core_runtime"]["artifacts"]
            if any("path" in artifact for artifact in runtime_artifacts):
                raise AssertionError("public readiness runtime evidence exposed a private artifact path")
            for artifact in runtime_artifacts:
                role = artifact["role"]
                if role not in readiness_text:
                    raise AssertionError(f"runtime readiness omitted artifact role {role}")
                for public_identity in (artifact["resource_id"], artifact["sha256"][:12]):
                    if public_identity not in readiness_text:
                        raise AssertionError(
                            f"runtime readiness omitted public artifact identity {public_identity}"
                        )
            metrics_text = page.locator("[data-testid=runtime-metrics]").inner_text()
            if str(metrics["reconciled_at_startup"]["operations"]) not in metrics_text or str(metrics["reconciled_at_startup"]["purges"]) not in metrics_text:
                raise AssertionError("runtime recovery summary differs from operational metrics")
            lease = readiness["checks"]["data_directory_lease"]
            expected_lease = {
                "state": "HELD",
                "owner": "api-server",
                "expires_at": None,
                "release_condition": "PROCESS_SHUTDOWN",
            }
            if any(lease.get(key) != value for key, value in expected_lease.items()):
                raise AssertionError(f"readiness returned an unexpected data-directory lease contract: {lease!r}")
            if not isinstance(lease.get("pid"), int) or lease["pid"] <= 0 or not isinstance(lease.get("acquired_at"), str) or not lease["acquired_at"]:
                raise AssertionError(f"readiness omitted live data-directory lease identity: {lease!r}")
            lease_card = page.locator("[data-testid=runtime-data-directory-lease]")
            if lease_card.count() != 1 or lease_card.get_attribute("data-state") != lease["state"]:
                raise AssertionError("Runtime & storage did not render the live lease state")
            lease_text = lease_card.inner_text()
            for exact_value in (lease["owner"], str(lease["pid"]), lease["acquired_at"], lease["release_condition"]):
                if exact_value not in lease_text:
                    raise AssertionError(f"runtime lease card omitted readiness evidence: {exact_value}")
            if page.get_by_text("Select a project to inventory embeddings.", exact=True).count() != 1:
                raise AssertionError("Runtime & storage has no honest no-project embedding state")
            if page.locator("[data-testid=runtime-json-evidence]").evaluate("element => element.open"):
                raise AssertionError("raw runtime JSON is primary instead of secondary collapsed evidence")

            page.get_by_role("tab", name="Audit", exact=True).click()
            initial_events = _json(f"http://127.0.0.1:{API_PORT}/api/system-events")
            if not isinstance(initial_events, list):
                raise TypeError("live /api/system-events did not return a list")
            _assert_path_free_public_payload(initial_events, "/api/system-events:initial")
            if not initial_events and page.get_by_text("No lifecycle action is recorded.", exact=True).count() != 1:
                raise AssertionError("Audit has no honest first-run lifecycle state")
            if page.locator("[data-testid=resource-table] .resource-card").count() != len(resources):
                raise AssertionError("typed resource evidence count differs from /api/resources")
            if page.locator("[data-testid=audit-json-evidence]").evaluate("element => element.open"):
                raise AssertionError("raw audit JSON is primary instead of secondary collapsed evidence")
            observation["steps"].append({"step": "connections-runtime-audit", "providers": 0, "resources": len(resources), "ready_status": readiness["status"], "lease_contract": lease})

            page.get_by_role("tab", name="Add-ons", exact=True).click()
            page.get_by_label("Search matchers").fill(first_matcher["matcher_id"])
            detail = page.locator("[data-testid=matcher-detail]")
            pull = detail.get_by_role("button", name="Pull pinned sources", exact=True)
            if pull.count() != 1 or not pull.is_enabled():
                raise AssertionError("selected real matcher source pull is not available")
            pull.click()
            dialog = page.get_by_role("dialog")
            dialog.wait_for(state="visible", timeout=10_000)
            expected_target = f"{first_matcher['name']} ({first_matcher['matcher_id']})"
            if page.locator("[data-testid=system-confirm-target]").inner_text().strip() != expected_target:
                raise AssertionError("lifecycle confirmation does not identify the exact matcher target")
            for _ in range(4):
                page.keyboard.press("Tab")
                if not page.evaluate("() => document.activeElement?.closest('dialog') !== null"):
                    raise AssertionError("lifecycle confirmation did not retain keyboard focus")
            page.keyboard.press("Escape")
            dialog.wait_for(state="hidden", timeout=10_000)
            if page.evaluate("() => document.activeElement?.textContent?.trim()") != "Pull pinned sources":
                raise AssertionError("Escape did not cancel the action and restore focus to its trigger")
            pull.click()
            dialog.get_by_role("button", name="Confirm exact target", exact=True).click()
            detail.get_by_text("Sources verified", exact=True).wait_for(timeout=120_000)
            load = detail.get_by_role("button", name="Load matcher", exact=True)
            if not load.is_enabled():
                raise AssertionError("verified registered matcher did not become loadable")
            load.click()
            dialog.get_by_role("button", name="Confirm exact target", exact=True).click()
            page.locator("[data-testid=system-lifecycle-progress]").wait_for(state="visible", timeout=30_000)
            lifecycle_controls = detail.locator("[data-testid=matcher-lifecycle-actions] button")
            if lifecycle_controls.count() == 0 or any(lifecycle_controls.nth(index).is_enabled() for index in range(lifecycle_controls.count())):
                raise AssertionError("concurrent lifecycle controls remained available during a real matcher load")
            page.wait_for_function(
                "() => document.querySelector('[data-testid=matcher-detail] > header .state-pill')?.textContent?.trim() === 'LOADED'",
                timeout=180_000,
            )
            loaded_runtime = detail.locator(".system-detail-section").filter(has_text="Rights, acquisition, and runtime").inner_text()
            if "not resident" in loaded_runtime:
                raise AssertionError("matcher reported LOADED without resident runtime evidence")
            unload = detail.get_by_role("button", name="Unload matcher", exact=True)
            unload.click()
            dialog.get_by_role("button", name="Confirm exact target", exact=True).click()
            page.wait_for_function(
                "() => document.querySelector('[data-testid=matcher-detail] > header .state-pill')?.textContent?.trim() === 'UNLOADED'",
                timeout=120_000,
            )
            lifecycle_events = _json(f"http://127.0.0.1:{API_PORT}/api/system-events")
            if not isinstance(lifecycle_events, list) or not any(event["event_type"] == "MATCHER_LOADED" for event in lifecycle_events) or not any(event["event_type"] == "MATCHER_UNLOADED" for event in lifecycle_events):
                raise AssertionError("real load/unload did not produce durable terminal events")
            _assert_path_free_public_payload(lifecycle_events, "/api/system-events:lifecycle")
            required_rendered_routes = {
                "/api/health",
                "/api/ready",
                "/api/metrics",
                "/api/resources",
                "/api/addons",
                "/api/system-events",
            }
            observed_rendered_routes = {
                route.split(" ", 1)[1]
                for route, _payload in system_payloads
                if route.startswith("GET ")
            }
            missing_rendered_routes = sorted(required_rendered_routes - observed_rendered_routes)
            if missing_rendered_routes:
                raise AssertionError(
                    f"Manage privacy gate did not observe every raw payload: {missing_rendered_routes!r}"
                )
            if not any(route.startswith("POST /api/matcher-assets/") for route, _payload in system_payloads):
                raise AssertionError("Manage privacy gate observed no matcher lifecycle response payload")
            for route, payload in system_payloads:
                _assert_path_free_public_payload(payload, route)
            blocked_datasets = [item for item in catalog["datasets"] if not item["acquisition"]["available"]]
            if not blocked_datasets:
                raise AssertionError("the real catalog did not expose the expected acquisition refusal")
            blocked = blocked_datasets[0]
            blocker_text = page.locator(f"[data-testid=dataset-addons] [data-addon-id='{blocked['addon_id']}']").inner_text()
            if blocked["acquisition"]["reason"] not in blocker_text or blocked["acquisition"]["refusal_code"] not in blocker_text:
                raise AssertionError("dataset acquisition refusal omitted its real code or reason")
            observation["steps"].append({"step": "real-lifecycle", "matcher_id": first_matcher["matcher_id"], "source_pull": "completed", "load": "completed", "unload": "completed", "events": len(lifecycle_events), "refusal": blocked["addon_id"]})

            page.get_by_label("Search matchers").fill("")
            if args.screenshot is not None:
                page.screenshot(path=str(args.screenshot), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            page.get_by_role("tab", name="Processing profiles", exact=True).click()
            overflow = page.evaluate("() => ({viewport: window.innerWidth, document: document.documentElement.scrollWidth})")
            if overflow["document"] > overflow["viewport"]:
                raise AssertionError(f"390px Manage surface overflows horizontally: {overflow}")
            touch_targets = page.locator(".system-area-tabs button:visible, .system-profile-list .button:visible").evaluate_all(
                "elements => elements.map(element => ({label: element.textContent?.trim(), width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height}))"
            )
            undersized = [target for target in touch_targets if target["height"] < 44 or target["width"] < 44]
            if undersized:
                raise AssertionError(f"390px Manage has undersized interactive targets: {undersized}")
            mobile_screenshot = None
            if args.screenshot is not None:
                mobile_screenshot = args.screenshot.with_name(f"{args.screenshot.stem}-390{args.screenshot.suffix}")
                page.screenshot(path=str(mobile_screenshot), full_page=True)
            observation["steps"].append({"step": "mobile-keyboard-accessibility", "viewport": 390, "overflow": overflow, "touch_targets": len(touch_targets), "undersized": 0, "mobile_screenshot": str(mobile_screenshot) if mobile_screenshot else None})
            browser.close()

        observation["passed"] = True
        args.observation.write_text(json.dumps(observation, indent=2) + "\n", encoding="utf-8")
        exit_code = 0
    except Exception as error:  # noqa: BLE001 - evidence gate records the exact failure
        observation["error"] = f"{type(error).__name__}: {error}"
        observation["traceback"] = traceback.format_exc()
        args.observation.write_text(json.dumps(observation, indent=2) + "\n", encoding="utf-8")
        print(observation["traceback"], end="")
    finally:
        _stop(api_process)
        _stop(preview_process)
        shutil.rmtree(data_dir, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
