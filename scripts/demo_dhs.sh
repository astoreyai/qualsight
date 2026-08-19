#!/usr/bin/env bash
# QS-Y88: DFAB/DHS demonstration runner.
#
#   bash scripts/demo_dhs.sh            start the console on the recorded evidence and verify the demo state
#   bash scripts/demo_dhs.sh --verify   verify only (no server left running; used by the demo dry-run gate)
#
# The demo runs on the PERSISTENT store (var/): the recorded LFW project with
# its six matcher Runs, seven utility analyses, cross-matcher contrasts, nine
# figures, and two reports. Nothing is fabricated at demo time — the live parts
# of the flow (single-photo upload, evaluator-directory import) execute the
# real registered engine in the room. The timed operator flow is
# docs/DEMO_SCRIPT.md.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"
PORT=8480
VERIFY_ONLY="${1:-}"
STARTED_BY_SCRIPT=0

if ! curl -sf "http://127.0.0.1:${PORT}/api/health" > /dev/null 2>&1; then
    echo "[demo] starting qualsight serve on 127.0.0.1:${PORT} (persistent store var/)"
    PYTHONPATH=src:.. QUALSIGHT_INPUT_RESOURCE_IDS="ofiq-r-01-conformance-input,lfw-funneled-local-candidate" \
        nohup ~/.local/bin/uv run --directory backend --frozen --offline python -m qualsight.cli serve \
        > var/serve.log 2>&1 &
    STARTED_BY_SCRIPT=1
    for _ in $(seq 1 90); do
        curl -sf "http://127.0.0.1:${PORT}/api/health" > /dev/null 2>&1 && break
        sleep 2
    done
fi

python3 - "$PORT" <<'PY'
import json
import sys
import urllib.request

port = sys.argv[1]


def get(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as response:  # noqa: S310 - loopback only
        return json.load(response)


def post(path, body):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310 - loopback only
        return json.load(response)


health = get("/api/health")
assert health["engine"]["state"] == "APPROVED_LOCAL_RESEARCH_USE", health["engine"]
projects = get("/api/projects")
demo = next(p for p in projects if p["name"].startswith("LFW quality workbench"))
pid = demo["project_id"]
if demo["lifecycle"] != "OPEN":
    post(f"/api/projects/{pid}/open", {})
    print(f"[demo] opened project ({demo['lifecycle']} -> OPEN)")
checks = {
    "engine": f"{health['engine']['name']} v{health['engine']['version']}",
    "project": demo["name"],
    "matcher_leaves": len(get(f"/api/projects/{pid}/matcher-leaves")),
    "utility_analyses": len(get(f"/api/projects/{pid}/utility-analyses")),
    "cross_matcher_analyses": len(get(f"/api/projects/{pid}/cross-matcher")),
    "figures": len(get(f"/api/projects/{pid}/figures")),
    "reports": len(get(f"/api/projects/{pid}/reports")),
}
minimums = {"matcher_leaves": 7, "utility_analyses": 6, "cross_matcher_analyses": 1, "figures": 9, "reports": 2}
for key, minimum in minimums.items():
    assert checks[key] >= minimum, f"demo evidence missing: {key}={checks[key]} < {minimum}"

# The Stakeholder Demo dashboard (PRD section 37 template), built from recorded figures; content-addressed, so idempotent.
figures = {f["kind"]: f["figure_id"] for f in get(f"/api/projects/{pid}/figures")}
wanted = [figures[k] for k in ("det_overlay", "erc_overlay", "error_overlap", "quality_histogram") if k in figures]
dashboards = get(f"/api/projects/{pid}/dashboards")
existing = [d for d in dashboards if d.get("template") == "Stakeholder Demo"]
if not existing:
    saved = post(
        f"/api/projects/{pid}/dashboards",
        {"name": "DFAB/DHS stakeholder demo", "template": "Stakeholder Demo", "columns": 2,
         "panels": [{"kind": "figure", "ref": ref} for ref in wanted]},
    )
    print(f"[demo] created Stakeholder Demo dashboard {saved['dashboard_id'][:24]}…")
else:
    print(f"[demo] Stakeholder Demo dashboard present: {existing[0]['dashboard_id'][:24]}…")
print("[demo] evidence checks:", json.dumps(checks, indent=2))
PY

if [ "${VERIFY_ONLY}" = "--verify" ]; then
    if [ "${STARTED_BY_SCRIPT}" = "1" ]; then
        pkill -f "qualsight.cli serve" || true
    fi
    echo "[demo] verify complete"
    exit 0
fi

echo
echo "[demo] console ready: http://127.0.0.1:${PORT}  (routes: #/projects #/assess #/data #/matchers #/utility #/cross #/reports #/advanced #/system)"
echo "[demo] operator flow: docs/DEMO_SCRIPT.md (15 minutes, timed)"
