#!/usr/bin/env python3
"""Audit both dependency locks and fail on every unaccepted vulnerability."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import tomllib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tool_version(command: list[str], root: Path) -> str:
    return subprocess.run(
        command,
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _run_json(
    command: list[str], *, root: Path, accepted_codes: set[int]
) -> tuple[int, Any, str]:
    result = subprocess.run(command, cwd=root, capture_output=True, text=True)
    if result.returncode not in accepted_codes:
        raise RuntimeError(
            f"command failed with exit {result.returncode}: {' '.join(command)}\n{result.stderr}"
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"command did not emit JSON: {' '.join(command)}") from error
    return result.returncode, value, result.stderr


def _qualified_name(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value, aliases)
        return f"{owner}.{node.attr}" if owner else None
    return None


def _source_controls_text(
    text: str, display_path: str
) -> tuple[int, int, int, int, list[str]]:
    tree = ast.parse(text, filename=display_path)
    aliases: dict[str, str] = {}
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == "torch" or item.name.startswith("torch."):
                    local = item.asname or item.name.split(".", 1)[0]
                    aliases[local] = item.name if item.asname else local
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "torch" or node.module.startswith("torch."):
                for item in node.names:
                    if item.name == "*":
                        findings.append(f"{display_path}:{node.lineno}: wildcard torch import")
                    else:
                        imported = f"{node.module}.{item.name}"
                        aliases[item.asname or item.name] = imported
                        if imported in {"torch.jit.script", "torch.compile"}:
                            findings.append(
                                f"{display_path}:{node.lineno}: import of {imported}"
                            )
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            qualified = _qualified_name(value, aliases) if value is not None else None
            if qualified is None or not (
                qualified == "torch" or qualified.startswith("torch.")
            ):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases[target.id] = qualified
                    changed = True
    weights_only = 0
    strict_state = 0
    jit_script = 0
    torch_compile = 0
    for node in ast.walk(tree):
        qualified = _qualified_name(node, aliases)
        if isinstance(node, (ast.Name, ast.Attribute)) and qualified == "torch.jit.script":
            jit_script += 1
            findings.append(f"{display_path}:{node.lineno}: torch.jit.script reference")
        if isinstance(node, (ast.Name, ast.Attribute)) and qualified == "torch.compile":
            torch_compile += 1
            findings.append(f"{display_path}:{node.lineno}: torch.compile reference")
        if isinstance(node, ast.Attribute) and qualified in {
            "torch.__dict__",
            "torch.__getattr__",
            "torch.__getattribute__",
            "torch.jit.__dict__",
            "torch.jit.__getattr__",
            "torch.jit.__getattribute__",
        }:
            findings.append(f"{display_path}:{node.lineno}: reflective torch attribute access")
        if isinstance(node, ast.Subscript):
            owner = _qualified_name(node.value, aliases)
            if owner in {"torch.__dict__", "torch.jit.__dict__"}:
                findings.append(f"{display_path}:{node.lineno}: subscripted torch namespace access")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = _qualified_name(node.func, aliases)
        if called == "torch.load":
            guarded = any(
                keyword.arg == "weights_only"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )
            if not guarded:
                findings.append(f"{display_path}:{node.lineno}: torch.load without weights_only=True")
            else:
                weights_only += 1
        if called and called.endswith(".load_state_dict"):
            strict = any(
                keyword.arg == "strict"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )
            if not strict:
                findings.append(f"{display_path}:{node.lineno}: load_state_dict without strict=True")
            else:
                strict_state += 1
        if called in {"getattr", "builtins.getattr"} and len(node.args) >= 2:
            owner = _qualified_name(node.args[0], aliases)
            if owner in {"torch", "torch.jit"}:
                findings.append(f"{display_path}:{node.lineno}: dynamic access to {owner}")
        if called == "vars" and node.args and _qualified_name(node.args[0], aliases) in {"torch", "torch.jit"}:
            findings.append(f"{display_path}:{node.lineno}: vars() access to torch namespace")
        if called in {"eval", "builtins.eval", "exec", "builtins.exec"}:
            findings.append(f"{display_path}:{node.lineno}: dynamic code execution")
        if called in {"__import__", "builtins.__import__", "importlib.import_module"}:
            findings.append(f"{display_path}:{node.lineno}: dynamic module import")
        if any(value == "torch" or value.startswith("torch.") for value in aliases.values()) and called in {
            "globals",
            "builtins.globals",
            "locals",
            "builtins.locals",
        }:
            findings.append(f"{display_path}:{node.lineno}: dynamic namespace access in torch module")
    return weights_only, strict_state, jit_script, torch_compile, findings


def _source_controls(path: Path, root: Path) -> tuple[int, int, int, int, list[str]]:
    return _source_controls_text(
        path.read_text(encoding="utf-8"), path.relative_to(root).as_posix()
    )


def _governed_cache_path(root: Path, item: dict[str, Any]) -> Path:
    url = str(item["url"])
    filename = url.rsplit("/", 1)[-1]
    stem = filename.rsplit(".", 1)[0]
    repository = str(item["repository"])
    commit = str(item["commit"])
    return (
        root
        / "var"
        / "upstream"
        / repository.split("/", 1)[0].lower()
        / f"{stem}.{commit[:8]}.py"
    )


def _read_source_body(handle: Any, key: str, declared_size: int) -> bytes:
    remaining = declared_size + 1
    blocks: list[bytes] = []
    while remaining:
        block = handle.read(min(64 * 1024, remaining))
        if not block:
            break
        blocks.append(block)
        remaining -= len(block)
    data = b"".join(blocks)
    if len(data) > declared_size:
        raise RuntimeError(f"governed upstream source {key} exceeds its declared size")
    return data


def _read_governed_cache(cached: Path, key: str, declared_size: int) -> bytes:
    required_flags = ("O_PATH", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required_flags):
        raise RuntimeError("secure governed-source cache reads require Linux O_PATH and O_NOFOLLOW")
    anchor_flags = os.O_PATH | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        anchor = os.open(cached, anchor_flags)
    except OSError as error:
        raise RuntimeError(
            f"cannot securely open governed upstream cache {key}: {error}"
        ) from error
    try:
        anchor_stat = os.fstat(anchor)
        if not stat.S_ISREG(anchor_stat.st_mode):
            raise RuntimeError(f"governed upstream cache is not a regular file: {key}")
        reader_flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        try:
            reader = os.open(f"/proc/self/fd/{anchor}", reader_flags)
        except OSError as error:
            raise RuntimeError(
                f"cannot read pinned governed upstream cache {key}: {error}"
            ) from error
        try:
            reader_stat = os.fstat(reader)
            if (reader_stat.st_dev, reader_stat.st_ino) != (
                anchor_stat.st_dev,
                anchor_stat.st_ino,
            ):
                raise RuntimeError(f"governed upstream cache identity changed: {key}")
            with os.fdopen(reader, "rb") as handle:
                reader = -1
                return _read_source_body(handle, key, declared_size)
        finally:
            if reader >= 0:
                os.close(reader)
    finally:
        os.close(anchor)


def _governed_upstream_sources(
    root: Path, *, allow_network: bool = True
) -> list[tuple[str, str, str]]:
    manifest_path = root / "resources" / "manifests" / "matcher-upstream-sources-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for item in manifest["sources"]:
        key = str(item["key"])
        if key in seen:
            raise RuntimeError(f"duplicate governed upstream source key: {key}")
        seen.add(key)
        url = str(item["url"])
        if not url.startswith("https://"):
            raise RuntimeError(f"governed upstream source is not HTTPS: {key}")
        sha256 = str(item["sha256"])
        size_bytes = int(item["size_bytes"])
        cached = _governed_cache_path(root, item)
        if cached.exists() or cached.is_symlink():
            data = _read_governed_cache(cached, key, size_bytes)
        else:
            if not allow_network:
                raise RuntimeError(
                    f"governed upstream source {key} is absent and network acquisition is disabled"
                )
            request = urllib.request.Request(
                url, headers={"User-Agent": "QualSight-release-audit/0.1"}
            )
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - pinned HTTPS URL
                data = _read_source_body(response, key, size_bytes)
        observed = hashlib.sha256(data).hexdigest()
        if observed != sha256 or len(data) != size_bytes:
            raise RuntimeError(
                f"governed upstream source {key} differs from its pin: "
                f"{observed}/{len(data)} != {sha256}/{size_bytes}"
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError(f"governed upstream source is not UTF-8 Python: {key}") from error
        rows.append((key, sha256, text))
    return rows


def _runtime_loaded_source_keys(root: Path) -> set[str]:
    loaded: set[str] = set()
    for path in sorted((root / "backend" / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "load_module":
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                raise RuntimeError(
                    f"deployment source loader key must be a static string: {path.relative_to(root)}:{node.lineno}"
                )
            loaded.add(node.args[0].value)
    return loaded


def validate_reachability_controls(root: Path) -> dict[str, object]:
    source_paths = sorted((root / "backend" / "src").rglob("*.py"))
    source_paths += sorted((root / "resources" / "upstream").rglob("*.py"))
    totals = [0, 0, 0, 0]
    findings: list[str] = []
    for path in source_paths:
        controls = _source_controls(path, root)
        for index, count in enumerate(controls[:4]):
            totals[index] += count
        findings.extend(controls[4])
    governed_upstream = _governed_upstream_sources(root)
    governed_keys = {key for key, _, _ in governed_upstream}
    executed_keys = _runtime_loaded_source_keys(root)
    if executed_keys - governed_keys:
        raise RuntimeError(
            "deployment source loader references ungoverned keys: "
            + ", ".join(sorted(executed_keys - governed_keys))
        )
    for key, _, text in governed_upstream:
        controls = _source_controls_text(text, f"governed-upstream/{key}.py")
        for index, count in enumerate(controls[:4]):
            totals[index] += count
        findings.extend(controls[4])
    if findings:
        raise RuntimeError("accepted PyTorch advisory surface became reachable: " + "; ".join(findings))
    if totals[0] < 1 or totals[1] < 1:
        raise RuntimeError("the strict registered-checkpoint loader controls are absent")
    return {
        "weights_only_loader_count": totals[0],
        "strict_state_loader_count": totals[1],
        "torch_jit_script_reference_count": totals[2],
        "torch_compile_reference_count": totals[3],
        "scanned_source_count": len(source_paths) + len(governed_upstream),
        "repository_source_count": len(source_paths),
        "source_sha256": {
            path.relative_to(root).as_posix(): _sha256(path) for path in source_paths
        },
        "governed_upstream_source_sha256": {
            key: sha256 for key, sha256, _ in governed_upstream
        },
        "executed_upstream_source_keys": sorted(executed_keys),
    }


def audit(root: Path) -> tuple[dict[str, object], bool]:
    root = root.resolve(strict=True)
    policy_path = root / "resources" / "security" / "vulnerability-policy-v1.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    admitted = set(policy["accepted_findings"])
    for tool in ("uv", "pnpm"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"required audit tool is unavailable: {tool}")
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    expected_auditors = [
        package["version"] for package in lock["package"] if package["name"] == "pip-audit"
    ]
    if len(expected_auditors) != 1:
        raise RuntimeError("uv.lock must contain exactly one project-pinned pip-audit")
    pip_audit_command = [
        "uv",
        "run",
        "--isolated",
        "--frozen",
        "--directory",
        str(root / "backend"),
        "pip-audit",
    ]
    pip_audit_version = _tool_version([*pip_audit_command, "--version"], root)
    if pip_audit_version != f"pip-audit {expected_auditors[0]}":
        raise RuntimeError(
            f"isolated pip-audit version differs from uv.lock: {pip_audit_version}"
        )
    with tempfile.TemporaryDirectory(prefix="qualsight-vulnerability-audit-") as temporary:
        requirements = Path(temporary) / "requirements.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--frozen",
                "--directory",
                str(root / "backend"),
                "--all-groups",
                "--format",
                "requirements-txt",
                "--no-emit-project",
                "--output-file",
                str(requirements),
            ],
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        backend_code, backend, backend_stderr = _run_json(
            [*pip_audit_command, "-r", str(requirements), "--format", "json"],
            root=root,
            accepted_codes={0, 1},
        )
        frontend_code, frontend, frontend_stderr = _run_json(
            ["pnpm", "audit", "--json"], root=root, accepted_codes={0, 1}
        )
    backend_findings = sorted(
        {
            finding["id"]
            for dependency in backend["dependencies"]
            for finding in dependency["vulns"]
        }
    )
    frontend_findings = sorted(str(key) for key in frontend.get("advisories", {}))
    unaccepted = sorted(set(backend_findings) - admitted)
    stale_acceptances = sorted(admitted - set(backend_findings))
    passed = not frontend_findings and not unaccepted
    return (
        {
            "schema_version": "qualsight-vulnerability-audit-v1",
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "locks": {
                "uv.lock": _sha256(root / "uv.lock"),
                "pnpm-lock.yaml": _sha256(root / "pnpm-lock.yaml"),
            },
            "policy": {
                "path": policy_path.relative_to(root).as_posix(),
                "sha256": _sha256(policy_path),
                "accepted_findings": sorted(admitted),
                "stale_acceptances": stale_acceptances,
            },
            "tools": {
                "uv": _tool_version(["uv", "--version"], root),
                "pip_audit": pip_audit_version,
                "pnpm": _tool_version(["pnpm", "--version"], root),
            },
            "reachability_controls": validate_reachability_controls(root),
            "backend": {
                "exit_code": backend_code,
                "finding_ids": backend_findings,
                "stderr": backend_stderr,
                "report": backend,
            },
            "frontend": {
                "exit_code": frontend_code,
                "advisory_ids": frontend_findings,
                "stderr": frontend_stderr,
                "report": frontend,
            },
            "unaccepted_findings": unaccepted,
            "gate": "PASS" if passed else "FAIL",
        },
        passed,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite vulnerability evidence: {args.output}")
    value, passed = audit(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
