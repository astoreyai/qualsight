"""Plan F QS-S99 — dependency and supply-chain audit (offline).

Inventories the real installed environments (the backend virtualenv resolved
from uv.lock and the console's node_modules resolved from pnpm-lock.yaml),
records name, version, and declared license from each package's own installed
metadata, verifies every Python distribution is pinned in uv.lock, and writes
docs/DEPENDENCY_AUDIT.md. No network access; everything read from disk.

Usage: python3 scripts/audit_dependencies.py [--check]
    --check   exit non-zero if the committed audit differs from the live tree
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import tomllib

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "DEPENDENCY_AUDIT.md"


def _normalise_python_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def python_inventory() -> list[dict[str, str]]:
    site = ROOT / ".venv" / "lib"
    infos = sorted(site.glob("python*/site-packages/*.dist-info"))
    if not infos:
        raise SystemExit("no installed backend environment (.venv) found; run scripts/install.sh first")
    with (ROOT / "uv.lock").open("rb") as handle:
        lock = tomllib.load(handle)
    locked = {_normalise_python_name(package["name"]): package["version"] for package in lock["package"]}
    rows: list[dict[str, str]] = []
    for info in infos:
        metadata = (info / "METADATA").read_text(encoding="utf-8", errors="replace")
        name = re.search(r"^Name: (.+)$", metadata, re.M)
        version = re.search(r"^Version: (.+)$", metadata, re.M)
        license_field = re.search(r"^License-Expression: (.+)$", metadata, re.M) or re.search(r"^License: (.+)$", metadata, re.M)
        classifiers = re.findall(r"^Classifier: License :: OSI Approved :: (.+)$", metadata, re.M)
        license_text = (license_field.group(1).strip() if license_field else "") or "; ".join(classifiers) or "NOT DECLARED"
        if license_text and len(license_text) > 80:
            license_text = "; ".join(classifiers) or license_text[:77] + "..."
        package = name.group(1).strip() if name else info.name.split("-")[0]
        key = _normalise_python_name(package)
        pinned = locked.get(key)
        rows.append({
            "name": package,
            "version": version.group(1).strip() if version else "?",
            "license": license_text,
            "pinned": "yes" if pinned == (version.group(1).strip() if version else None) else f"LOCK MISMATCH ({pinned})",
        })
    return rows


def node_inventory() -> list[dict[str, str]]:
    modules = ROOT / "node_modules" / ".pnpm"
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    if not modules.is_dir():
        raise SystemExit("no installed console environment (node_modules/.pnpm) found; run scripts/install.sh first")
    lock = yaml.safe_load((ROOT / "pnpm-lock.yaml").read_text(encoding="utf-8"))
    locked: set[tuple[str, str]] = set()
    for key in lock["packages"]:
        identity = key.split("(", 1)[0]
        boundary = identity.rfind("@")
        if boundary <= 0 or boundary == len(identity) - 1:
            raise SystemExit(f"unsupported pnpm lock identity: {key}")
        locked.add((identity[:boundary], identity[boundary + 1 :]))
    manifests = sorted(modules.glob("*/node_modules/*/package.json"))
    manifests += sorted(modules.glob("*/node_modules/@*/*/package.json"))
    for manifest in manifests:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SystemExit(f"cannot read installed package manifest {manifest}: {error}") from error
        name = data.get("name")
        version = data.get("version")
        identity = (name, version)
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise SystemExit(f"installed package manifest lacks a string name/version: {manifest}")
        if identity in seen:
            continue
        seen.add(identity)
        license_value = data.get("license")
        if isinstance(license_value, dict):
            license_value = license_value.get("type")
        rows.append(
            {
                "name": name,
                "version": version,
                "license": str(license_value or "NOT DECLARED"),
                "pinned": "yes" if identity in locked else "LOCK MISMATCH (not in pnpm-lock.yaml)",
            }
        )
    return rows


def render(python_rows: list[dict[str, str]], node_rows: list[dict[str, str]]) -> str:
    def table(rows: list[dict[str, str]]) -> str:
        lines = ["| Package | Version | Declared license | Pinned |", "|---|---|---|---|"]
        for row in rows:
            lines.append(f"| {row['name']} | {row['version']} | {row['license']} | {row['pinned']} |")
        return "\n".join(lines)

    undeclared = [r["name"] for r in python_rows + node_rows if r["license"] == "NOT DECLARED"]
    python_mismatched = [r["name"] for r in python_rows if r["pinned"] != "yes"]
    node_mismatched = [r["name"] for r in node_rows if r["pinned"] != "yes"]
    return f"""# QualSight dependency audit (QS-S99)

Generated by `python3 scripts/audit_dependencies.py` from the installed environments —
`.venv` (uv.lock, hash-pinned wheels) and every installed transitive package under
`node_modules/.pnpm` (pnpm-lock.yaml). Regenerate after any dependency change; `--check` fails
when this file is stale or an installed package identity is absent from its lock.
Licenses are each package's own declared metadata, recorded verbatim — not legal review.

* Backend distributions: **{len(python_rows)}** · console packages: **{len(node_rows)}**
* Undeclared licenses: **{len(undeclared)}**{" — " + ", ".join(undeclared) if undeclared else ""}
* uv.lock mismatches: **{len(python_mismatched)}**{" — " + ", ".join(python_mismatched) if python_mismatched else ""}
* pnpm-lock.yaml mismatches: **{len(node_mismatched)}**{" — " + ", ".join(node_mismatched) if node_mismatched else ""}
* Acquisition policy: wheels only via `uv sync --frozen` (sha256 in uv.lock); npm packages only via
  `pnpm install --frozen-lockfile`; models/runtimes only via the resource registry's pinned SHA-256
  identities (decision 12). Runtime makes no network call (THREAT_MODEL.md).

## Vulnerability gate

`python3 scripts/audit_vulnerabilities.py --output <new-path>` audits the complete real locks with
`pip-audit` and `pnpm audit`, binds the result to both lock hashes, and refuses to overwrite evidence.
The most recent release-candidate gate was rerun on 2026-09-02 after the lock repair described below;
its local report SHA-256 was `5859ec907c4f57641f948db5c56b3f5bf6f58aa9130a5af98fcaf14630e01dd1`:

* Frontend: **0** advisories. Four high-severity advisories published on 2026-09-02 against transitive
  `fast-uri` 3.1.5 were first observed as a release-stopping audit failure. The root pnpm policy now
  forces patched 3.1.7 through the existing `vite-plugin-pwa` → `workbox-build` → `ajv` path; no
  vulnerability-policy exception was added.
* Backend: **2 accepted local-only PyTorch findings**, `PYSEC-2025-194` and `PYSEC-2026-139`.
  The project-pinned isolated auditor covers every current-platform runtime and development requirement
  exported from `uv.lock` (87 reported dependency records). AST analysis of 73 repository Python
  sources plus all 9 byte-pinned setup-time upstream sources finds zero direct or imported-alias `torch.jit.script` and
  `torch.compile` references. It rejects direct `eval`, `exec`, `__import__`, `importlib.import_module`, and
  torch namespace-reflection patterns, and constrains every current deployment-source `load_module` call to a
  literal manifest-governed key whose byte-pinned source is included in the scan. The only checkpoint loader
  uses exact registered hashes, `weights_only=True`, and `strict=True`. This static gate is not a formal proof
  that no equivalent dynamic-execution path exists. The acceptance policy
  is byte-bound in `resources/security/vulnerability-policy-v1.json` and refuses every new finding.
* This is a constrained beta risk acceptance, not a claim that PyTorch 2.10.0 is vulnerability-free.
  A tensor-runtime change requires real six-matcher numerical-equivalence and performance gates.

## Backend (Python)

{table(python_rows)}

## Console (npm)

{table(node_rows)}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    python_rows = python_inventory()
    node_rows = node_inventory()
    mismatched = [
        f"{row['name']}=={row['version']} ({row['pinned']})"
        for row in python_rows + node_rows
        if row["pinned"] != "yes"
    ]
    if mismatched:
        print("installed dependencies differ from the frozen locks: " + "; ".join(mismatched), file=sys.stderr)
        return 1
    text = render(python_rows, node_rows)
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != text:
            print("DEPENDENCY_AUDIT.md is stale; regenerate with scripts/audit_dependencies.py", file=sys.stderr)
            return 1
        print(f"dependency audit is current ({OUTPUT})")
        return 0
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT} at {datetime.now(timezone.utc).astimezone().isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
