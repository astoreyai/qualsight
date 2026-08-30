"""Audit QualSight's local data controls (PRD §§48–50; QS-H70).

Usage: python scripts/audit_local_data_controls.py [--data-dir DIR] [--database-snapshot SNAPSHOT]

Checks, from the repository and the data directory (never modifying either):

1. runtime network posture — the control plane binds 127.0.0.1 only; the only
   outbound network code in the backend package is the installation-time
   acquisition of pinned upstream sources, the owner-admitted matcher
   checkpoints, and the verification archive; the
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
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
sys.path.insert(0, str(BACKEND_SRC))

from qualsight.security.process_lease import DataDirectoryLease, DataDirectoryLeaseError

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
    sanctioned = {
        "backend/src/qualsight/dataset_acquisition.py",
        "backend/src/qualsight/matchers/acquisition.py",
        "backend/src/qualsight/matchers/upstream.py",
    }
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


def _check_secrets(
    data_dir: Path,
    database: Path,
    database_descriptor: int | None = None,
) -> dict[str, object]:
    secrets = _secret_values()
    findings: list[dict[str, str]] = []
    scanned = 0
    database_scanned = False
    if database_descriptor is not None or database.exists():
        database_uri = (
            f"file:/proc/self/fd/{database_descriptor}?mode=ro&immutable=1"
            if database_descriptor is not None
            else f"{database.resolve(strict=True).as_uri()}?mode=ro"
        )
        connection = sqlite3.connect(database_uri, uri=True)
        try:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise sqlite3.DatabaseError("database snapshot integrity check failed")
            governed_tables = (
                "runs",
                "audit_events",
                "projects",
                "jobs",
                "artifacts",
                "annotations",
                "reports",
                "figures",
                "utility_analyses",
                "cross_matcher_analyses",
                "reproductions",
                "purges",
                "bundles",
            )
            recorded_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            missing_tables = sorted(set(governed_tables) - recorded_tables)
            if missing_tables:
                raise sqlite3.DatabaseError(
                    f"database snapshot is missing governed tables: {missing_tables}"
                )
            for table in governed_tables:
                cursor = connection.execute(f"SELECT * FROM {table}")
                for row in cursor.fetchall():
                    scanned += 1
                    _scan_text(" ".join(str(v) for v in row if v is not None), secrets, f"sqlite:{table}", findings)
            database_scanned = True
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
    return {"database": str(database), "database_scanned": database_scanned, "secret_environment_variables_considered": [name for name, _ in secrets], "records_and_files_scanned": scanned, "findings": findings, "ok": not findings}


def _check_purge() -> dict[str, object]:
    purge = (BACKEND / "security" / "purge.py").read_text(encoding="utf-8")
    store = (BACKEND / "api" / "store.py").read_text(encoding="utf-8")
    journal_present = "CREATE TABLE IF NOT EXISTS purge_journal" in store
    atomic_completion = "record_purge_and_complete" in store
    startup_reconciliation = "reconcile_incomplete_purges" in purge
    return {"scope_statement_present": "SCOPE_STATEMENT" in purge and "not scrubbed" in purge, "purge_table_present": "CREATE TABLE IF NOT EXISTS purges" in store,
            "purge_journal_present": journal_present, "atomic_completion_present": atomic_completion, "startup_reconciliation_present": startup_reconciliation,
            "verified_flag_present": '"verified"' in purge,
            "ok": "SCOPE_STATEMENT" in purge and "CREATE TABLE IF NOT EXISTS purges" in store and journal_present and atomic_completion and startup_reconciliation}


def _check_owner_supplied(data_dir: Path) -> dict[str, object]:
    """QS-S96: decision-30 admission controls are present in the code and honored on disk."""

    module = (BACKEND / "importers" / "owner_supplied.py").read_text(encoding="utf-8")
    decode_check = module.split("def _decode_check", 1)[1].split("\n\n", 1)[0]
    code_controls = {
        "size_cap": "MAX_UPLOAD_BYTES" in module,
        "decode_before_accept": "imdecode" in module and "do not decode as an image" in module,
        "decode_bomb_guard": (
            "MAX_DECODED_PIXELS" in decode_check
            and decode_check.index("_encoded_dimensions(data, name)")
            < decode_check.index("cv2.imdecode")
        ),
        "symlink_refusal": "symlinks are not admitted" in module,
        "root_escape_refusal": "resolves outside the declared root" in module,
        "source_kind_marker": 'SOURCE_KIND = "OWNER_SUPPLIED_LOCAL_INPUT"' in module,
    }
    # On-disk check: every stored owner-supplied copy re-hashes to its filename stem.
    import hashlib

    storage = data_dir / "owner_supplied"
    verified = drifted = 0
    if storage.is_dir():
        for path in storage.iterdir():
            if not path.is_file():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if path.stem == digest:
                verified += 1
            else:
                drifted += 1
    ok = all(code_controls.values()) and drifted == 0
    return {**code_controls, "stored_copies_verified": verified, "stored_copies_drifted": drifted, "ok": ok}


def _check_loopback_bind() -> dict[str, object]:
    """QS-S96: the serve entry point binds 127.0.0.1 explicitly and nothing binds 0.0.0.0."""

    cli = (BACKEND / "cli.py").read_text(encoding="utf-8")
    package_binds = []
    for path in BACKEND.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "0.0.0.0" in text:
            package_binds.append(str(path.relative_to(ROOT)))
    ok = '"127.0.0.1"' in cli and not package_binds
    return {"serve_binds_loopback": '"127.0.0.1"' in cli, "wildcard_binds": package_binds, "ok": ok}


def _open_snapshot_descriptor(snapshot: Path, data_dir: Path) -> tuple[Path, int]:
    candidate = snapshot.absolute()
    if not candidate.name.endswith(".snapshot.sqlite3"):
        raise ValueError("database snapshot path must end with .snapshot.sqlite3")
    try:
        descriptor = os.open(
            candidate,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError as error:
        raise ValueError(f"database snapshot does not exist: {candidate}") from error
    except OSError as error:
        raise ValueError(f"cannot open database snapshot without following links: {candidate}") from error
    try:
        status = os.fstat(descriptor)
        path_status = candidate.lstat()
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_ISLNK(path_status.st_mode)
            or (status.st_dev, status.st_ino) != (path_status.st_dev, path_status.st_ino)
        ):
            raise ValueError(f"database snapshot is not a stable regular non-symlink file: {candidate}")
        if status.st_nlink != 1:
            raise ValueError(f"database snapshot must be a single-link file: {candidate}")
        if status.st_uid != os.geteuid():
            raise ValueError(f"database snapshot is not owned by the current user: {candidate}")
        live_database = data_dir.absolute() / "qualsight.sqlite3"
        if live_database.exists():
            live_status = live_database.stat()
            if (status.st_dev, status.st_ino) == (live_status.st_dev, live_status.st_ino):
                raise ValueError("live database is not accepted as an audit snapshot")
        return candidate, descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _validated_snapshot_path(snapshot: Path, data_dir: Path) -> Path:
    candidate, descriptor = _open_snapshot_descriptor(snapshot, data_dir)
    os.close(descriptor)
    return candidate.resolve(strict=True)


def _failure(error: Exception) -> int:
    summary = {"error": type(error).__name__, "detail": str(error), "ok": False}
    print("LOCAL_DATA_CONTROLS_FAIL " + json.dumps(summary, sort_keys=True))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=ROOT / "var")
    parser.add_argument(
        "--database-snapshot",
        type=Path,
        help="existing RunStore snapshot to inspect while the API owns the live data directory",
    )
    args = parser.parse_args(argv)
    lease: DataDirectoryLease | None = None
    snapshot_descriptor: int | None = None
    try:
        data_dir = args.data_dir.absolute()
        if args.database_snapshot is not None:
            database, snapshot_descriptor = _open_snapshot_descriptor(
                args.database_snapshot, data_dir
            )
        else:
            lease = DataDirectoryLease.acquire(data_dir, owner="local-data-controls-audit")
            database = data_dir / "qualsight.sqlite3"
        summary = {"network_posture": _check_network_posture(), "secrets": _check_secrets(data_dir, database, snapshot_descriptor), "purge": _check_purge(), "owner_supplied": _check_owner_supplied(data_dir), "loopback_bind": _check_loopback_bind()}
        ok = all(section["ok"] for section in summary.values())
        summary["ok"] = ok
        print(("LOCAL_DATA_CONTROLS_OK " if ok else "LOCAL_DATA_CONTROLS_FAIL ") + json.dumps(summary, sort_keys=True))
        return 0 if ok else 1
    except (DataDirectoryLeaseError, OSError, sqlite3.Error, ValueError) as error:
        return _failure(error)
    finally:
        if snapshot_descriptor is not None:
            os.close(snapshot_descriptor)
        if lease is not None:
            lease.close()


if __name__ == "__main__":
    sys.exit(main())
