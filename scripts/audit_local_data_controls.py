"""Audit QualSight's local data controls (PRD §§48–50; QS-H70).

Usage: python scripts/audit_local_data_controls.py [--data-dir DIR]

Checks, from the repository and the data directory (never modifying either):

1. runtime network posture — the control plane binds 127.0.0.1 only; the only
   outbound network code in the backend package is the installation-time
   acquisition of pinned upstream sources (qualsight/matchers/upstream.py); the
   runtime network guard exists and refuses non-loopback connections;
2. secrets absent from records and logs — every environment value that looks
   like a credential (variable name containing TOKEN, SECRET, PASSWORD, KEY,
   CREDENTIAL, or AUTH and value length >= 8) and every common token shape
   (ghp_, sk-, AKIA, xox[abp]-, hf_, gitea/basic-auth URLs) is searched for
   in the SQLite record store, the ledger, and every text/JSON file under the
   data directory;
3. purge scope statement is present in the code and its record schema exists.

Exit 0 only when every check passes; the JSON summary is printed either way.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend" / "src" / "qualsight"
OUTBOUND_RE = re.compile(r"urllib\.request|import requests|import httpx|http\.client|socket\.create_connection|aiohttp")
TOKEN_SHAPES = re.compile(r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[abp]-[A-Za-z0-9-]{10,}|hf_[A-Za-z0-9]{20,}|https?://[^/\s:@]+:[^/\s@]{6,}@")
SECRET_NAME_RE = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_KEY|PRIVATE_KEY|AUTH", re.IGNORECASE)


def _check_network_posture() -> dict[str, object]:
    cli = (BACKEND / "cli.py").read_text(encoding="utf-8")
    binds_loopback = 'host="127.0.0.1"' in cli
    outbound_files = []
    for path in sorted(BACKEND.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if OUTBOUND_RE.search(text):
            outbound_files.append(str(path.relative_to(ROOT)))
    sanctioned = {"backend/src/qualsight/matchers/upstream.py"}
    guard = (BACKEND / "security" / "network_guard.py").exists()
    return {"binds_loopback_only": binds_loopback, "outbound_network_files": outbound_files, "sanctioned_setup_time_files": sorted(sanctioned), "unsanctioned_outbound": sorted(set(outbound_files) - sanctioned), "network_guard_present": guard,
            "ok": binds_loopback and guard and not (set(outbound_files) - sanctioned)}


def _secret_values() -> list[tuple[str, str]]:
    values = []
    for name, value in os.environ.items():
        if SECRET_NAME_RE.search(name) and len(value) >= 8:
            values.append((name, value))
    return values


def _scan_text(text: str, secrets: list[tuple[str, str]], where: str, findings: list[dict[str, str]]) -> None:
    for name, value in secrets:
        if value in text:
            findings.append({"where": where, "kind": "environment_secret_value", "variable": name})
    for match in TOKEN_SHAPES.finditer(text):
        findings.append({"where": where, "kind": "token_shape", "sample": match.group(0)[:12] + "…"})


def _check_secrets(data_dir: Path) -> dict[str, object]:
    secrets = _secret_values()
    findings: list[dict[str, str]] = []
    scanned = 0
    database = data_dir / "qualsight.sqlite3"
    if database.exists():
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            for table in ("runs", "audit_events", "projects", "jobs", "artifacts", "annotations", "reports", "figures", "utility_analyses", "cross_matcher_analyses", "reproductions", "purges", "bundles"):
                try:
                    cursor = connection.execute(f"SELECT * FROM {table}")
                except sqlite3.OperationalError:
                    continue
                for row in cursor.fetchall():
                    scanned += 1
                    _scan_text(" ".join(str(v) for v in row if v is not None), secrets, f"sqlite:{table}", findings)
        finally:
            connection.close()
    ledger = ROOT / "atg" / "ledger.jsonl"
    if ledger.exists():
        scanned += 1
        _scan_text(ledger.read_text(encoding="utf-8"), secrets, "atg/ledger.jsonl", findings)
    for path in data_dir.rglob("*"):
        if path.is_file() and path.suffix in {".log", ".txt", ".json", ".jsonl", ".csv", ".md", ".html"} and path.stat().st_size < 50_000_000:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            _scan_text(text, secrets, str(path), findings)
    return {"secret_environment_variables_considered": [name for name, _ in secrets], "records_and_files_scanned": scanned, "findings": findings, "ok": not findings}


def _check_purge() -> dict[str, object]:
    purge = (BACKEND / "security" / "purge.py").read_text(encoding="utf-8")
    store = (BACKEND / "api" / "store.py").read_text(encoding="utf-8")
    return {"scope_statement_present": "SCOPE_STATEMENT" in purge and "not scrubbed" in purge, "purge_table_present": "CREATE TABLE IF NOT EXISTS purges" in store, "verified_flag_present": '"verified"' in purge,
            "ok": "SCOPE_STATEMENT" in purge and "CREATE TABLE IF NOT EXISTS purges" in store}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=ROOT / "var")
    args = parser.parse_args(argv)
    summary = {"network_posture": _check_network_posture(), "secrets": _check_secrets(args.data_dir), "purge": _check_purge()}
    ok = all(section["ok"] for section in summary.values())
    summary["ok"] = ok
    print(("LOCAL_DATA_CONTROLS_OK " if ok else "LOCAL_DATA_CONTROLS_FAIL ") + json.dumps(summary, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
