"""QS-Z93: demo dry-run gate — drive docs/DEMO_SCRIPT.md's flow on the recorded evidence.

Starts the product on the PERSISTENT store (the same evidence the room will
see), walks the timed demo routes in headless Chromium, executes the live
steps for real (single-photo upload through the registered engine), and
records what was observed with per-step wall-clock timings. Exit 0 only when
every step succeeded. The screenshot directory must lie outside the repository.

Usage:
    python3 scripts/observe_demo.py --screenshot-dir /path/outside/repo \
        --observation atg/evidence/<name>.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from controlled_inputs import lfw_tree_root

ROOT = Path(__file__).resolve().parents[1]
PORT = 8480
UPLOAD_SOURCE = lfw_tree_root(ROOT) / "Vladimir_Putin/Vladimir_Putin_0001.jpg"


def _wait_http(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):  # noqa: S310 - loopback only
                return
        except OSError:
            time.sleep(1)
    raise RuntimeError(f"{url} did not come up in {timeout}s")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot-dir", required=True)
    parser.add_argument("--observation", required=True)
    args = parser.parse_args()
    shots = Path(args.screenshot_dir).resolve()
    if str(shots).startswith(str(ROOT)):
        print("screenshot directory must be outside the repository", file=sys.stderr)
        return 2
    shots.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["PYTHONPATH"] = "src:.."
    env["QUALSIGHT_INPUT_RESOURCE_IDS"] = "ofiq-r-01-conformance-input,lfw-funneled-local-candidate"
    server = subprocess.Popen(
        [str(Path.home() / ".local/bin/uv"), "run", "--directory", "backend", "--frozen", "--offline",
         "python", "-m", "qualsight.cli", "serve"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    observation: dict = {"gate": "QS-Z93 demo dry-run", "demo_script": "docs/DEMO_SCRIPT.md", "steps": [], "exit_code": 1}
    started = time.monotonic()
    try:
        _wait_http(f"http://127.0.0.1:{PORT}/api/health", 180)
        # The demo prep (evidence verification + Stakeholder Demo dashboard) must be green first.
        prep = subprocess.run(["bash", "scripts/demo_dhs.sh", "--verify"], cwd=str(ROOT), capture_output=True, text=True, check=False)
        if prep.returncode != 0:
            raise RuntimeError(f"demo prep failed: {prep.stderr[-400:]}")
        observation["steps"].append({"step": "prep", "seconds": round(time.monotonic() - started, 1), "output": prep.stdout.strip().splitlines()[-8:]})
        # demo_dhs --verify kills servers it started; ours predates it, but re-check.
        _wait_http(f"http://127.0.0.1:{PORT}/api/health", 60)

        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1500, "height": 950})

            def timed(name: str, fn) -> None:
                t0 = time.monotonic()
                detail = fn()
                entry = {"step": name, "seconds": round(time.monotonic() - t0, 1)}
                if isinstance(detail, dict):
                    entry.update(detail)
                observation["steps"].append(entry)
                page.screenshot(path=str(shots / f"{len(observation['steps']):02d}-{name}.png"))

            def goto(view: str) -> None:
                page.evaluate(f"() => {{ window.location.hash = '#/{view}' }}")
                page.wait_for_timeout(300)

            def minute_0_system():
                page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
                goto("system")
                page.wait_for_selector("[data-testid=matcher-assets] .asset-card", timeout=60_000)
                assets = page.evaluate("() => document.querySelectorAll('[data-testid=matcher-assets] .asset-card').length")
                resources = page.evaluate("() => document.querySelectorAll('[data-testid=resource-table] .resource-card').length")
                badge = page.inner_text("[data-testid=engine-badge]")
                assert assets == 3, assets
                assert resources == 10, resources
                assert "OFIQ" in badge and "1.1.2" in badge, badge
                return {"matcher_assets": assets, "resources": resources}

            def select_demo_project():
                goto("projects")
                page.wait_for_function("() => document.querySelectorAll('[data-testid=project-select] option').length > 1", timeout=30_000)
                value = page.evaluate(
                    "() => Array.from(document.querySelectorAll('[data-testid=project-select] option')).find(o => o.innerText.includes('LFW quality workbench'))?.value"
                )
                assert value, "demo project is not in the selector"
                page.select_option("[data-testid=project-select]", value)
                return {"project": "LFW quality workbench"}

            def minute_1_upload():
                goto("assess")
                copy = Path(tempfile.mkdtemp(prefix="qualsight-demo-")) / "evaluator_photo.jpg"
                shutil.copyfile(UPLOAD_SOURCE, copy)
                page.set_input_files("[data-testid=upload-input]", str(copy))
                page.wait_for_selector("[data-testid=upload-result]", timeout=180_000)
                text = page.inner_text("[data-testid=upload-result]")
                shutil.rmtree(copy.parent, ignore_errors=True)
                assert "OWNER_SUPPLIED_LOCAL_INPUT" in text, text
                return {"uqs": page.inner_text("[data-testid=upload-uqs]")}

            def minute_3_overlays():
                page.click("[data-testid=detection-toggle]")
                page.wait_for_selector("[data-testid=detection-overlay]", state="attached", timeout=60_000)
                scrfd_points = page.evaluate("() => document.querySelectorAll('[data-testid=detection-overlay] circle').length")
                page.click("[data-testid=ofiq-landmark-toggle]")
                page.wait_for_selector("[data-testid=ofiq-landmark-overlay]", state="attached", timeout=180_000)
                ofiq_points = page.evaluate("() => document.querySelectorAll('[data-testid=ofiq-landmark-overlay] circle').length")
                assert ofiq_points == 98, ofiq_points
                return {"scrfd_points": scrfd_points, "ofiq_points": ofiq_points}

            def minute_5_data():
                goto("data")
                facts = page.wait_for_selector("[data-testid=dataset-facts]", timeout=60_000) and page.inner_text("[data-testid=dataset-facts]")
                assert "13,233" in facts, facts[:200]
                return {"dataset": "LFW 13,233 recorded"}

            def minute_7_matchers_evidence():
                goto("matchers")
                page.wait_for_selector("[data-testid=matrix-table]", timeout=60_000)
                cells = page.evaluate("() => document.querySelectorAll('[data-testid=matrix-table] button.cell').length")
                assert cells >= 6, cells
                page.evaluate("() => document.querySelector('[data-testid=matrix-table] button.cell').click()")
                page.wait_for_selector("[data-testid=matcher-errors]", timeout=120_000)
                page.wait_for_selector("[data-testid=error-pair]", timeout=120_000)
                page.click("[data-testid=evidence-open]")
                page.wait_for_selector("[data-testid=pair-evidence]", timeout=180_000)
                page.wait_for_selector("[data-testid=evidence-crop-left]", timeout=180_000)
                statement = page.inner_text("[data-testid=evidence-statement]")
                assert "not OFIQ's internal landmarks" in statement
                decisions = page.evaluate("() => document.querySelectorAll('[data-testid=evidence-decisions] tbody tr').length")
                return {"matrix_cells": cells, "evidence_decisions": decisions}

            def minute_9_utility():
                goto("utility")
                rows = page.wait_for_selector("[data-testid=utility-table] tbody tr", timeout=60_000) and page.evaluate(
                    "() => document.querySelectorAll('[data-testid=utility-table] tbody tr').length"
                )
                assert rows >= 6, rows
                return {"recorded_analyses": rows}

            def minute_11_cross():
                goto("cross")
                rows = page.wait_for_selector("[data-testid=cross-table] tbody tr", timeout=60_000) and page.evaluate(
                    "() => document.querySelectorAll('[data-testid=cross-table] tbody tr').length"
                )
                assert rows >= 1, rows
                # Open the recorded analysis -> a contrast -> a disagreement list -> match evidence (Loop E cycle 2).
                page.evaluate("() => document.querySelector('[data-testid=cross-table] tbody tr button')?.click()")
                page.wait_for_selector("[data-testid=contrast-row]", timeout=120_000)
                evidence = {"cross_evidence": False}
                for button in page.query_selector_all("[data-testid=contrast-row] button")[:12]:
                    button.click()
                    page.wait_for_timeout(700)
                    if page.query_selector("[data-testid=cross-pair]"):
                        page.click("[data-testid=cross-evidence-open]")
                        scope = "main div[data-view=cross] "
                        page.wait_for_selector(scope + "[data-testid=pair-evidence]", timeout=120_000)
                        statement = page.inner_text(scope + "[data-testid=evidence-statement]")
                        assert "not causal" in statement
                        evidence["cross_evidence"] = True
                        break
                assert evidence["cross_evidence"], "no contrast category produced a pair list with evidence"
                return {"recorded_cross_analyses": rows, **evidence}

            def minute_12_reports():
                goto("reports")
                thumbs = page.wait_for_selector("[data-testid=figure-thumb]", timeout=60_000) and page.evaluate(
                    "() => document.querySelectorAll('[data-testid=figure-thumb]').length"
                )
                assert thumbs >= 9, thumbs
                dashboards = page.inner_text("main div[data-view=reports]")
                assert "stakeholder demo" in dashboards.lower(), "Stakeholder Demo dashboard not visible"
                return {"figures": thumbs}

            def minute_13_advanced():
                goto("advanced")
                text = page.wait_for_selector("main div[data-view=advanced]", timeout=60_000) and page.inner_text("main div[data-view=advanced]")
                assert "rank" in text.lower() or "identification" in text.lower()
                return {"panel": "advanced shown"}

            timed("system", minute_0_system)
            timed("select-project", select_demo_project)
            timed("live-upload", minute_1_upload)
            timed("overlays", minute_3_overlays)
            timed("data", minute_5_data)
            timed("matchers-evidence", minute_7_matchers_evidence)
            timed("utility", minute_9_utility)
            timed("cross", minute_11_cross)
            timed("reports", minute_12_reports)
            timed("advanced", minute_13_advanced)
            browser.close()
        observation["total_seconds"] = round(time.monotonic() - started, 1)
        observation["exit_code"] = 0
    except Exception as error:  # noqa: BLE001 - recorded honestly
        observation["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        observation["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        Path(args.observation).write_text(json.dumps(observation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
    print(f"demo dry-run recorded in {args.observation} exit={observation['exit_code']}")
    return observation["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
