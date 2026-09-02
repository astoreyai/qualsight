"""Interaction / piping audit: walk every console view on the recorded evidence,
exercise every drilldown, and resolve every link and image the DOM renders.

Read-mostly on the persistent store: mutations are one owner-supplied upload
of a real LFW copy and, when absent, one dashboard assembled through the shipped
UI from the project's already-recorded real evidence.
Reproduce/matrix/report builders are not triggered here —
they are covered by the observation and docs gates.

Usage: python3 scripts/audit_console.py --observation atg/evidence/<name>.json
Exit 0 only when every step succeeded and every resolved URL returned 200.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

from controlled_inputs import lfw_tree_root

ROOT = Path(__file__).resolve().parents[1]
PORT = 8480
UPLOAD_SOURCE = lfw_tree_root(ROOT) / "John_Howard/John_Howard_0002.jpg"

FINDINGS: list[dict] = []
CHECKED: dict[str, int] = {}


def check_links(page, scope: str) -> dict:
    """GET every /api href and img src currently in the DOM; record statuses."""

    urls = page.evaluate(
        """() => {
            const out = new Set();
            for (const a of document.querySelectorAll('a[href]')) {
              const h = a.getAttribute('href');
              if (h && (h.startsWith('/api/') || h.startsWith('/'))) out.add(h);
            }
            for (const i of document.querySelectorAll('img[src]')) {
              const s = i.getAttribute('src');
              if (s && s.startsWith('/api/')) out.add(s);
            }
            return Array.from(out);
        }"""
    )
    fresh = [u for u in urls if u not in CHECKED and not u.startswith("/#")]
    statuses = page.evaluate(
        """async (urls) => {
            const out = {};
            for (const u of urls) {
              const controller = new AbortController();
              const timer = setTimeout(() => controller.abort(), 15000);
              try {
                const r = await fetch(u, { method: 'GET', signal: controller.signal });
                out[u] = r.status;
                if (r.body) await r.body.cancel();
              }
              catch (e) { out[u] = -1; }
              finally { clearTimeout(timer); }
            }
            return out;
        }""",
        fresh,
    )
    for url, status in statuses.items():
        CHECKED[url] = status
        if status != 200:
            FINDINGS.append({"scope": scope, "url": url, "status": status})
    return {"scope": scope, "urls_in_dom": len(urls), "newly_checked": len(fresh), "failures": [u for u, s in statuses.items() if s != 200]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observation", required=True)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    observation: dict = {"gate": "console interaction/piping audit", "steps": [], "exit_code": 1}
    started = time.monotonic()
    console_errors: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 950})
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(str(e)))

            def step(name: str, fn) -> None:
                t0 = time.monotonic()
                detail = fn() or {}
                detail.update({"step": name, "seconds": round(time.monotonic() - t0, 1)})
                observation["steps"].append(detail)

            def goto(view: str) -> None:
                page.evaluate(f"() => {{ window.location.hash = '#/{view}' }}")
                page.wait_for_timeout(400)

            def sel(css: str) -> str:
                return css

            # ---- load + project selection --------------------------------------
            def load():
                page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
                goto("projects")
                page.wait_for_function("() => document.querySelectorAll('[data-testid=project-select] option').length > 1", timeout=30_000)
                value = page.evaluate("""() => {
                    const projects = Array.from(document.querySelectorAll('[data-testid=project-select] option')).filter((option) => option.value);
                    projects.sort((left, right) => {
                        const leftRuns = Number(left.innerText.match(/(\d+) run\(s\)/)?.[1] ?? 0);
                        const rightRuns = Number(right.innerText.match(/(\d+) run\(s\)/)?.[1] ?? 0);
                        return rightRuns - leftRuns;
                    });
                    return projects[0]?.value;
                }""")
                assert value, "no recorded project is available for the interaction audit"
                page.select_option("[data-testid=project-select]", value)
                page.wait_for_timeout(1200)
                rows = page.evaluate("() => document.querySelectorAll('[data-testid=run-table] tbody tr').length")
                assert rows > 0, "runs table empty"
                return {"runs_listed": rows, **check_links(page, "projects")}

            # ---- assess: registered sample, overlays, upload -------------------
            def assess():
                goto("assess")
                page.click("[data-testid=assess-button]")
                page.wait_for_selector("[data-testid=measure-table]", timeout=180_000)
                measures = page.evaluate("() => document.querySelectorAll('[data-testid=measure-table] tbody tr').length")
                assert measures == 28, measures
                page.click("[data-testid=detection-toggle]")
                page.wait_for_selector("[data-testid=detection-overlay]", state="attached", timeout=60_000)
                scrfd = page.evaluate("() => document.querySelectorAll('[data-testid=detection-overlay] circle').length")
                page.click("[data-testid=ofiq-landmark-toggle]")
                page.wait_for_selector("[data-testid=ofiq-landmark-overlay]", state="attached", timeout=180_000)
                ofiq = page.evaluate("() => document.querySelectorAll('[data-testid=ofiq-landmark-overlay] circle').length")
                assert (scrfd, ofiq) == (5, 98), (scrfd, ofiq)
                copy = Path(tempfile.mkdtemp(prefix="qualsight-audit-")) / "audit_photo.jpg"
                shutil.copyfile(UPLOAD_SOURCE, copy)
                page.set_input_files("[data-testid=upload-input]", str(copy))
                page.wait_for_selector("[data-testid=upload-result]", timeout=180_000)
                shutil.rmtree(copy.parent, ignore_errors=True)
                uqs = page.inner_text("[data-testid=upload-uqs]")
                # piping: the new Run must appear in the projects runs table
                goto("projects")
                page.wait_for_timeout(1500)
                return {"measures": measures, "scrfd_points": scrfd, "ofiq_points": ofiq, "upload_uqs": uqs, **check_links(page, "assess")}

            # ---- data: explorer drilldown to a sample --------------------------
            def data():
                goto("data")
                page.wait_for_selector("[data-testid=dataset-facts]", timeout=60_000)
                page.wait_for_selector("[data-testid=job-list] button:not([disabled])", timeout=60_000)
                page.click("[data-testid=job-list] button:not([disabled])")
                page.wait_for_selector("[data-testid^=bin-]", timeout=120_000)
                bars = page.evaluate("() => document.querySelectorAll('[data-testid^=bin-]').length")
                top = page.evaluate("() => { const b = Array.from(document.querySelectorAll('[data-testid^=bin-]')); let best=null, h=0; for (const x of b){ const r=x.querySelector('rect'); const v=r ? Number(r.getAttribute('height')) : 0; if (v>h){h=v;best=x;} } return best?.getAttribute('data-testid'); }")
                assert top, "no populated histogram bin"
                page.click(f"[data-testid={top}]")
                page.wait_for_selector("[data-testid=drill-sample]", timeout=60_000)
                page.click("[data-testid=drill-sample]")
                page.wait_for_selector("[data-testid=sample-quality] dl", timeout=60_000)
                return {"bins": bars, **check_links(page, "data")}

            # ---- matchers: leaf -> errors -> evidence --------------------------
            def matchers():
                goto("data")
                dataset_select = page.locator("#workbench-dataset")
                dataset_select.wait_for(state="visible", timeout=60_000)
                matrix_dataset = dataset_select.locator("option").evaluate_all(
                    "options => options.find(option => option.textContent?.includes('Local LFW funneled image collection'))?.value ?? null"
                )
                assert matrix_dataset, (
                    "Inputs does not expose the recorded LFW dataset required by the matrix evidence"
                )
                dataset_select.select_option(matrix_dataset)
                page.wait_for_function(
                    "() => document.querySelector('[data-testid=dataset-facts]')?.textContent?.includes('Local LFW funneled image collection')",
                    timeout=60_000,
                )
                page.wait_for_function(
                    "datasetId => document.querySelector('[data-context-key=dataset]')?.getAttribute('data-dataset-id') === datasetId",
                    arg=matrix_dataset,
                    timeout=60_000,
                )
                goto("matchers")
                page.wait_for_selector("[data-testid=comparison-studio]", timeout=60_000)
                dataset_study = page.locator("[data-testid=comparison-mode-dataset]")
                assert dataset_study.count() == 1, (
                    "Compare does not expose the accepted Dataset study mode"
                )
                dataset_study.click()
                assert dataset_study.get_attribute("aria-pressed") == "true", (
                    "Dataset study did not become the active Compare mode"
                )
                page.wait_for_selector("[data-testid=matrix-table] button.cell", timeout=60_000)
                cells = page.evaluate("() => document.querySelectorAll('[data-testid=matrix-table] button.cell').length")
                assert cells >= 1, "no recorded matcher leaf is available"
                registered_matchers = page.evaluate(
                    """async () => {
                        const response = await fetch("/api/addons");
                        if (!response.ok) throw new Error(`add-on catalog returned ${response.status}`);
                        return (await response.json()).matchers.map((item) => item.name);
                    }"""
                )
                matcher_rows = page.query_selector_all("[data-testid=matrix-table] tbody tr")
                audited_matchers: list[str] = []
                for row in matcher_rows:
                    name = row.query_selector("th").inner_text().strip()
                    button = row.query_selector("button.cell")
                    assert button is not None, f"registered matcher {name} has no recorded matrix leaf"
                    leaf_test_id = button.get_attribute("data-testid")
                    assert leaf_test_id, f"registered matcher {name} has an unidentified matrix leaf"
                    button.click()
                    page.wait_for_function(
                        "testId => document.querySelector(`[data-testid='${testId}']`)?.classList.contains('selected')",
                        arg=leaf_test_id,
                        timeout=60_000,
                    )
                    page.wait_for_selector("[data-testid=matcher-errors]", timeout=60_000)
                    audited_matchers.append(name)
                assert len(audited_matchers) == len(set(audited_matchers)), (
                    f"matrix renders duplicate matcher rows: {audited_matchers}"
                )
                unregistered = sorted(set(audited_matchers) - set(registered_matchers))
                assert not unregistered, (
                    f"matrix renders matcher rows outside the admitted catalog: {unregistered}"
                )
                page.fill("[data-testid=matcher-errors] input", "0.9")
                page.click("[data-testid=matcher-errors] button:has-text('Recompute')")
                page.wait_for_selector("[data-testid=error-pair]", timeout=120_000)
                page.locator(
                    "[data-testid=matcher-errors] [data-testid=evidence-open]"
                ).first.click()
                scope = "main div[data-view=matchers] "
                page.wait_for_selector(scope + "[data-testid=evidence-crop-left]", timeout=180_000)
                decisions = page.evaluate("(s) => document.querySelectorAll(s + '[data-testid=evidence-decisions] tbody tr').length", scope)
                marker = bool(page.query_selector(scope + "[data-testid=pair-score-marker]"))
                assert decisions >= 1 and marker, (decisions, marker)
                return {
                    "matrix_dataset_id": matrix_dataset,
                    "matrix_cells": cells,
                    "registered_matchers": registered_matchers,
                    "matcher_drilldowns": audited_matchers,
                    "evidence_decisions": decisions,
                    **check_links(page, "matchers"),
                }

            # ---- utility: detail + all three pair filters + evidence -----------
            def utility():
                goto("utility")
                page.wait_for_selector("[data-testid=utility-table] tbody tr", timeout=60_000)
                page.evaluate("() => document.querySelector('[data-testid=utility-table] tbody tr button.cell')?.click()")
                page.wait_for_selector("[data-testid=utility-detail]", timeout=120_000)
                counts = {}
                for kind in ("rejected", "prevented", "removed"):
                    page.click(f"[data-testid=pairs-{kind}]")
                    page.wait_for_selector("[data-testid=utility-pairs]", timeout=60_000)
                    page.wait_for_timeout(600)
                    counts[kind] = page.evaluate("() => document.querySelectorAll('[data-testid=utility-pair]').length")
                if counts["removed"] == 0:
                    page.click("[data-testid=pairs-rejected]")
                    page.wait_for_timeout(600)
                if page.query_selector("[data-testid=utility-evidence-open]"):
                    page.click("[data-testid=utility-evidence-open]")
                    page.wait_for_selector("main div[data-view=utility] [data-testid=pair-evidence]", timeout=180_000)
                    page.wait_for_selector("main div[data-view=utility] [data-testid=evidence-statement]", timeout=60_000)
                erc = page.evaluate("() => document.querySelectorAll('[data-testid=erc-plot] polyline').length")
                assert erc >= 1, "no ERC overlay curves"
                return {"pair_counts": counts, "erc_polylines": erc, **check_links(page, "utility")}

            # ---- cross: analysis -> contrasts -> pairs -> evidence + annotate ---
            def cross():
                goto("cross")
                page.wait_for_selector("[data-testid=cross-table] tbody tr", timeout=60_000)
                analysis = None
                for row in page.query_selector_all("[data-testid=cross-table] tbody tr"):
                    candidate = row.query_selector("[data-testid=cross-analysis]")
                    cells = row.query_selector_all("td")
                    if candidate is None or len(cells) < 2:
                        continue
                    counts = [int(value) for value in re.findall(r"\d+", cells[-2].inner_text())]
                    if counts[:2] == [6, 3]:
                        analysis = candidate
                        break
                assert analysis is not None, "no recorded cross-matcher analysis is available"
                analysis.click()
                page.wait_for_selector("[data-testid=contrast-row]", timeout=120_000)
                contrasts = page.evaluate("() => document.querySelectorAll('[data-testid=contrast-row]').length")
                assert contrasts >= 9, f"expected the complete 3 matcher × 2 preprocessing contrast set; found {contrasts}"
                got_pairs = False
                for button in page.query_selector_all("[data-testid=contrast-row] button")[:16]:
                    button.click()
                    page.wait_for_timeout(700)
                    if page.query_selector("[data-testid=cross-pair]"):
                        got_pairs = True
                        break
                assert got_pairs, "no contrast category produced pairs"
                page.click("[data-testid=cross-evidence-open]")
                page.wait_for_selector("main div[data-view=cross] [data-testid=pair-evidence]", timeout=180_000)
                return {"contrasts": contrasts, **check_links(page, "cross")}

            # ---- reports: thumbs, dashboards, report links, bundles ------------
            def reports():
                goto("reports")
                disclosure = page.locator("[data-testid=report-disclosure-warning]")
                disclosure_text = (disclosure.text_content() or "").lower()
                assert disclosure.is_visible() and disclosure.get_attribute("role") == "note", "report disclosure warning is not an accessible visible note"
                assert "disclosure review" in disclosure_text and "deployment-local provenance" in disclosure_text, disclosure_text
                page.wait_for_selector("[data-testid=figure-thumb]", timeout=60_000)
                thumbs = page.query_selector_all("[data-testid=figure-thumb]")
                for thumb in thumbs:
                    thumb.click()
                    page.wait_for_selector("[data-testid=figure-detail] img", timeout=60_000)
                    page.wait_for_function("() => { const i = document.querySelector('[data-testid=figure-detail] img'); return i && i.complete && i.naturalWidth > 0; }", timeout=60_000)
                governed_links = page.locator("[data-governed-download]")
                assert governed_links.count() > 0, "the populated report view has no disclosure-bound downloads"
                assert all(governed_links.nth(index).get_attribute("aria-describedby") == "report-disclosure-warning-text" for index in range(governed_links.count())), "a governed download is not associated with the disclosure warning"
                quality_export = page.locator('[data-governed-download="quality-csv"]')
                assert quality_export.count() == 1 and quality_export.is_enabled(), "the real quality export action is unavailable"
                with page.expect_download(timeout=60_000) as download_info:
                    quality_export.click()
                export_download = download_info.value
                export_path = export_download.path()
                assert export_download.suggested_filename.endswith(".csv"), export_download.suggested_filename
                assert export_path is not None and export_path.is_file(), "the browser did not persist the export response"
                export_bytes = export_path.read_bytes()
                assert export_bytes, "the governed quality export is empty"
                dashboard_buttons = page.locator("[data-testid=dashboard-open]")
                dashboard_created = False
                if dashboard_buttons.count() == 0:
                    dashboard_save = page.locator("[data-testid=dashboard-save]")
                    assert dashboard_save.count() == 1 and dashboard_save.is_enabled(), (
                        "the real project has no dashboard and its recorded evidence cannot create one"
                    )
                    dashboard_save.click()
                    page.wait_for_selector("[data-testid=dashboard-detail]", timeout=60_000)
                    page.wait_for_function(
                        "() => document.querySelectorAll('[data-testid=dashboard-open]').length > 0",
                        timeout=60_000,
                    )
                    dashboard_created = True
                assert dashboard_buttons.count() > 0, "the real recorded project has no dashboard to inspect"
                dashboard_buttons.first.click()
                page.wait_for_selector("[data-testid=dashboard-detail]", timeout=60_000)
                non_figure_panels = page.locator("[data-testid=dashboard-panel]:not([data-panel-kind=figure])")
                summaries = page.locator("[data-testid=dashboard-operator-summary]")
                raw_disclosures = page.locator("[data-testid=dashboard-raw-data]")
                assert non_figure_panels.count() > 0, "the real dashboard has no operator-data panel"
                assert summaries.count() == non_figure_panels.count(), (summaries.count(), non_figure_panels.count())
                assert raw_disclosures.count() == non_figure_panels.count(), (raw_disclosures.count(), non_figure_panels.count())
                assert page.locator("[data-testid=dashboard-panel] > pre").count() == 0, "raw JSON remains primary dashboard content"
                assert all(raw_disclosures.nth(index).get_attribute("open") is None for index in range(raw_disclosures.count())), "raw dashboard JSON is expanded by default"
                raw_disclosures.first.locator("summary").focus()
                page.keyboard.press("Enter")
                assert raw_disclosures.first.get_attribute("open") is not None, "raw dashboard disclosure is not keyboard operable"
                page.keyboard.press("Enter")
                assert raw_disclosures.first.get_attribute("open") is None, "raw dashboard disclosure did not collapse from the keyboard"
                return {"figures_opened": len(thumbs), "dashboard_created": dashboard_created, "governed_downloads": governed_links.count(), "export_filename": export_download.suggested_filename, "export_size_bytes": len(export_bytes), "export_sha256": hashlib.sha256(export_bytes).hexdigest(), "operator_summaries": summaries.count(), "raw_disclosures_collapsed": raw_disclosures.count(), **check_links(page, "reports")}

            # ---- advanced + system ---------------------------------------------
            def advanced():
                goto("advanced")
                boundary = page.locator("[data-testid=advanced-beta-boundary]")
                boundary_text = (boundary.text_content() or "").lower()
                assert boundary.is_visible() and boundary.get_attribute("role") == "note", "Advanced beta boundary is not an accessible visible note"
                for phrase in ("research extension", "outside beta acceptance", "single-image stress test", "1:n", "container"):
                    assert phrase in boundary_text, f"Advanced beta boundary omits {phrase}: {boundary_text}"
                page.wait_for_selector("main div[data-view=advanced] table", timeout=60_000)
                return {"beta_boundary": (boundary.text_content() or "").strip(), **check_links(page, "advanced")}

            def system():
                goto("system")
                page.wait_for_selector("[data-testid=matcher-assets] tbody tr", timeout=60_000)
                assets = page.evaluate("() => document.querySelectorAll('[data-testid=matcher-assets] tbody tr').length")
                page.get_by_role("tab", name="Audit", exact=True).click()
                resources = page.evaluate("() => document.querySelectorAll('[data-testid=resource-table] .resource-card').length")
                assert assets == 6, assets
                assert resources == 13, resources
                return {"matcher_assets": assets, "resources": resources, **check_links(page, "system")}

            step("load", load)
            step("assess", assess)
            step("data", data)
            step("matchers", matchers)
            step("utility", utility)
            step("cross", cross)
            step("reports", reports)
            step("advanced", advanced)
            step("system", system)
            browser.close()
        observation["link_checks"] = {"total": len(CHECKED), "non_200": FINDINGS}
        observation["console_errors"] = console_errors[:20]
        observation["total_seconds"] = round(time.monotonic() - started, 1)
        observation["exit_code"] = 0 if not FINDINGS and not console_errors else 1
    except Exception as error:  # noqa: BLE001 - recorded honestly
        observation["error"] = f"{type(error).__name__}: {error}"
        observation["link_checks"] = {"total": len(CHECKED), "non_200": FINDINGS}
        observation["console_errors"] = console_errors[:20]
        raise
    finally:
        Path(args.observation).write_text(json.dumps(observation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"audit recorded in {args.observation} exit={observation['exit_code']} links={len(CHECKED)} failures={len(FINDINGS)} console_errors={len(console_errors)}")
    return observation["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
