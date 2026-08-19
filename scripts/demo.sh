#!/usr/bin/env bash
# QualSight demonstration script (PRD §56 Phase 7): runs the shipped user guide verbatim on the
# registered subset fixture (fresh data directory, GPU for the matchers), prints what each step
# produced, then starts the console + API on http://127.0.0.1:8480 against the installed data
# directory so the same workflow can be shown interactively. Nothing here is a demo mode: every
# command is the product's own CLI on real registered inputs.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="/usr/bin:$PATH"
UV="${UV:-$HOME/.local/bin/uv}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OBS="var/demo/demo-${STAMP}.json"
mkdir -p var/demo
echo "== QualSight demonstration — executing docs/USER_GUIDE.md verbatim on the subset fixture"
python3 scripts/docs_workflow_gate.py --observation "$OBS" --keep-data-dir
python3 - "$OBS" <<'PY'
import json, sys
o = json.load(open(sys.argv[1]))
print(f"\n== {o['blocks']} guide blocks executed in {o['seconds']} s, exit {o['exit_code']}; data directory kept at {o['data_dir']}")
for line in o["stdout_tail"].splitlines():
    if any(key in line for key in ("project:sha256", "dataset:sha256", "job:sha256", "matrix:sha256", "QS-C21.EXTERNAL", "VERIFIED", "identification-search", "EXACT", "figure_id")):
        print("   ", line[:160])
PY
echo
echo "== Starting the console + API (Ctrl-C to stop): http://127.0.0.1:8480"
env PYTHONPATH=src:.. QUALSIGHT_INPUT_RESOURCE_IDS="ofiq-r-01-conformance-input,lfw-funneled-local-candidate" "$UV" run --directory backend --frozen --offline python -m qualsight.cli serve
