"""Execute the shipped user guide verbatim (PRD §38; QS-H73).

Extracts every fenced ``sh gate`` block from docs/USER_GUIDE.md in order, joins
them into one bash script under ``set -euo pipefail``, and runs it against a
fresh data directory with the environment the guide names (QUALSIGHT_ROOT, UV,
WORK, OFIQ_R01_IMAGE, QUALSIGHT_DATA_DIR). The commands a researcher reads are
the commands that run; nothing is paraphrased. Output and exit code are written
to the evidence file.

Usage: python3 scripts/docs_workflow_gate.py --observation atg/evidence/<file>.json [--keep-data-dir]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "USER_GUIDE.md"
UV = "/home/aaron/.local/bin/uv"
BLOCK_RE = re.compile(r"```sh gate\n(.*?)```", re.DOTALL)


def _controlled_repository_root() -> Path:
    configured = os.environ.get("QUALSIGHT_REPOSITORY_ROOT")
    if configured is None:
        return ROOT
    repository_root = Path(configured).resolve(strict=True)
    if not repository_root.is_dir():
        raise ValueError(
            "QUALSIGHT_REPOSITORY_ROOT must resolve to a directory"
        )
    return repository_root


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--keep-data-dir", action="store_true")
    args = parser.parse_args()
    guide_text = GUIDE.read_text(encoding="utf-8")
    blocks = BLOCK_RE.findall(guide_text)
    if not blocks:
        print("no `sh gate` blocks in the user guide", file=sys.stderr)
        return 2
    repository_root = _controlled_repository_root()
    registry = json.loads((ROOT / "resources" / "resource-registry.json").read_text(encoding="utf-8"))
    r01 = next(a["path"] for e in registry["resources"] if e["resource_id"] == "ofiq-r-01-conformance-input" for a in e["artifacts"] if a["role"] == "DATA")
    data_dir = Path(tempfile.mkdtemp(prefix="qualsight-docs-gate-"))
    work = data_dir / "work"
    work.mkdir()
    script = "set -euo pipefail\n" + "\n".join(f"# --- USER_GUIDE.md block {index + 1}\n{block}" for index, block in enumerate(blocks)) + "\n"
    script_path = work / "user_guide_gate.sh"
    script_path.write_text(script, encoding="utf-8")
    env = {**os.environ, "QUALSIGHT_ROOT": str(ROOT), "UV": UV, "WORK": str(work), "OFIQ_R01_IMAGE": r01, "QUALSIGHT_DATA_DIR": str(data_dir), "QUALSIGHT_REPOSITORY_ROOT": str(repository_root)}
    started = time.time()
    result = subprocess.run(["bash", str(script_path)], cwd=str(ROOT), env=env, capture_output=True, text=True, check=False)
    observation = {
        "kind": "QUALSIGHT_DOCS_WORKFLOW_GATE", "guide": str(GUIDE.relative_to(ROOT)), "guide_sha256": hashlib.sha256(guide_text.encode("utf-8")).hexdigest(), "blocks": len(blocks), "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        "data_dir": str(data_dir), "exit_code": result.returncode, "seconds": round(time.time() - started, 1), "stdout_tail": result.stdout[-6000:], "stderr_tail": result.stderr[-4000:],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started)),
    }
    args.observation.parent.mkdir(parents=True, exist_ok=True)
    args.observation.write_text(json.dumps(observation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not args.keep_data_dir:
        shutil.rmtree(data_dir, ignore_errors=True)
    print(f"docs workflow gate written to {args.observation} exit={result.returncode} blocks={len(blocks)} seconds={observation['seconds']}")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
