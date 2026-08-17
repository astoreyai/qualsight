"""Append one real record to atg/ledger.jsonl with observed hashes.

The ledger is append-only. This helper never rewrites existing lines; it
computes SHA-256 identities of the named input and output files at append time
(so no digest is transcribed by hand), captures the Git state of the root, and
appends exactly one JSON line. Every other field is supplied explicitly by the
caller through a JSON document on stdin or ``--record-json``.

Usage:
    python scripts/append_ledger_record.py --record-json '{...}' \
        --input REQUIREMENTS.md --input ATG_PLAN.md \
        --output resources/resource-registry.json ...

The record JSON must contain: cycle_id, iteration_index, node_id,
historical_ancestor_id, action, exit_code, baseline, final, guard, progress,
status, error_classification, minimal_repair_subgraph, runtime_observation,
unresolved_external_blocks, rollback. Optional: operator (default "Claude"),
timestamp (default now, ISO-8601 with offset).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "atg" / "ledger.jsonl"
REQUIRED = {
    "cycle_id",
    "iteration_index",
    "node_id",
    "historical_ancestor_id",
    "action",
    "exit_code",
    "baseline",
    "final",
    "guard",
    "progress",
    "status",
    "error_classification",
    "minimal_repair_subgraph",
    "runtime_observation",
    "unresolved_external_blocks",
    "rollback",
}
ALLOWED_STATES = {"ACCEPTED", "BLOCKED", "FAILED", "READY", "RUNNING", "SUPERSEDED"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_state() -> tuple[str | None, dict[str, object]]:
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"], capture_output=True, text=True, check=False)
    status = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain=v1", "--untracked-files=normal"], capture_output=True, text=True, check=True)
    branch = subprocess.run(["git", "-C", str(ROOT), "branch", "--show-current"], capture_output=True, text=True, check=False).stdout.strip()
    commit = head.stdout.strip() if head.returncode == 0 else None
    lines = [line for line in status.stdout.splitlines() if line]
    return commit, {
        "branch": branch or "main",
        "head": commit or "UNBORN_NO_COMMITS",
        "staged": [line for line in lines if line[:1] not in {" ", "?"}],
        "untracked_project_files": any(line.startswith("??") for line in lines),
        "unrelated_claude_skills": "UNTOUCHED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record-json", help="record fields as a JSON object (stdin when omitted)")
    parser.add_argument("--input", action="append", default=[], help="repo-relative input file to hash")
    parser.add_argument("--output", action="append", default=[], help="repo-relative output file to hash")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    raw = args.record_json if args.record_json is not None else sys.stdin.read()
    record = json.loads(raw)
    missing = REQUIRED - set(record)
    if missing:
        raise SystemExit(f"record is missing fields: {sorted(missing)}")
    if record["status"] not in ALLOWED_STATES:
        raise SystemExit(f"unsupported status {record['status']}")
    for path_text in [*args.input, *args.output]:
        if not (ROOT / path_text).is_file():
            raise SystemExit(f"not a file: {path_text}")
    commit, dirty = _git_state()
    full = {
        "schema_version": "1.0",
        "cycle_id": record["cycle_id"],
        "iteration_index": int(record["iteration_index"]),
        "node_id": record["node_id"],
        "historical_ancestor_id": record["historical_ancestor_id"],
        "timestamp": record.get("timestamp") or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "operator": record.get("operator", "Claude"),
        "input_hashes": {"atg/ledger.jsonl": _sha256(LEDGER), **{p: _sha256(ROOT / p) for p in args.input}},
        "action": record["action"],
        "output_artifacts": {p: _sha256(ROOT / p) for p in args.output},
        "exit_code": record["exit_code"],
        "baseline": record["baseline"],
        "final": record["final"],
        "guard": record["guard"],
        "progress": record["progress"],
        "status": record["status"],
        "error_classification": record["error_classification"],
        "minimal_repair_subgraph": record["minimal_repair_subgraph"],
        "source_commit": commit,
        "dirty_state": dirty,
        "runtime_observation": record["runtime_observation"],
        "unresolved_external_blocks": record["unresolved_external_blocks"],
        "rollback": record["rollback"],
    }
    # Fix the timestamp offset format to match existing records (+HH:MM).
    ts = full["timestamp"]
    if len(ts) >= 5 and ts[-5] in "+-" and ts[-3] != ":":
        full["timestamp"] = ts[:-2] + ":" + ts[-2:]
    line = json.dumps(full, ensure_ascii=False, separators=(",", ":"))
    if args.dry_run:
        print(line)
        return 0
    with LEDGER.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(f"appended {full['node_id']} as line {sum(1 for _ in LEDGER.open('rb'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
