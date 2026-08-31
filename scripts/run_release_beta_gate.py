"""Run and record the complete controlled-workstation beta release gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import NamedTuple


class GateDefinition(NamedTuple):
    name: str
    argv: tuple[str, ...]
    timeout_seconds: int
    environment: tuple[tuple[str, str], ...] = ()


def _release_interpreter(python: str) -> Path:
    interpreter = Path(python).absolute()
    if not interpreter.is_file():
        raise ValueError(f"release interpreter is not a regular file: {interpreter}")
    return interpreter


def gate_definitions(
    root: Path,
    python: str,
    work_root: Path | None = None,
    deployment_root: Path | None = None,
) -> tuple[GateDefinition, ...]:
    root = root.resolve(strict=True)
    deployment_root = (deployment_root or root).resolve(strict=True)
    work_root = work_root or root / ".release-beta-gate-work"
    backend_environment = (
        ("PYTHONPATH", f"{root / 'backend' / 'src'}:{root}"),
        ("QUALSIGHT_REPOSITORY_ROOT", str(deployment_root)),
    )
    staged_backend_environment = (
        ("PYTHONPATH", f"{root / 'backend' / 'src'}:{root}"),
        ("QUALSIGHT_REPOSITORY_ROOT", str(root)),
    )
    validators = (
        (
            "resource-registry",
            "scripts.validate_resource_registry",
            ("resources/resource-registry.json",),
        ),
        (
            "metric-registry",
            "scripts.validate_metric_registry",
            ("metrics/metric-registry.json",),
        ),
        (
            "lfw-protocol",
            "scripts.validate_lfw_protocol",
            (
                "verify",
                "--registry",
                "resources/resource-registry.json",
                "--manifest",
                "resources/manifests/lfw-local-pairs-v1.jsonl",
            ),
        ),
        (
            "lfw-protocol-authority",
            "scripts.validate_lfw_protocol_authority",
            (
                "resources/manifests/lfw-official-protocol-authority-v1.json",
                "--registry",
                "resources/resource-registry.json",
                "--pairs",
                "resources/manifests/lfw-local-pairs-v1.jsonl",
            ),
        ),
        (
            "scrfd-release-provenance",
            "scripts.validate_scrfd_release_provenance",
            (
                "resources/manifests/insightface-buffalo-l-release-provenance-v1.json",
                "--registry",
                "resources/resource-registry.json",
            ),
        ),
        (
            "shared-crop-lineage",
            "scripts.validate_shared_crop_lineage",
            (
                "resources/manifests/lfw-full-v1-shared-crop-lineage-reconciliation.json",
                "--registry",
                "resources/resource-registry.json",
            ),
        ),
        ("atg-governance", "scripts.validate_atg_documents", ()),
    )
    return (
        GateDefinition(
            "root-tests",
            (
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(deployment_root / "tests"),
                "-q",
            ),
            600,
        ),
        GateDefinition(
            "backend-tests",
            (
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(deployment_root / "backend" / "tests"),
                "-q",
            ),
            3600,
            backend_environment,
        ),
        GateDefinition(
            "frontend-toolchain",
            ("pnpm", "--dir", str(root), "run", "verify:toolchain"),
            120,
        ),
        GateDefinition(
            "frontend-build",
            ("pnpm", "--dir", str(root), "run", "build:frontend"),
            600,
        ),
        GateDefinition(
            "dependency-audit",
            (python, "-m", "scripts.audit_dependencies", "--check"),
            300,
        ),
        *(
            GateDefinition(name, (python, "-m", module, *arguments), 600)
            for name, module, arguments in validators
        ),
        GateDefinition(
            "offline-workflow",
            (
                python,
                str(root / "scripts" / "offline_gate.py"),
                "--observation",
                str(work_root / "offline-workflow.json"),
                "--data-dir",
                str(work_root / "data"),
                "--source-root",
                str(root),
                "--repository-root",
                str(root),
                "--console-index",
                str(root / "frontend" / "dist" / "index.html"),
                "--python",
                python,
            ),
            3600,
            staged_backend_environment,
        ),
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalised_argv(
    root: Path,
    deployment_root: Path,
    argv: tuple[str, ...],
) -> list[str]:
    replacements = (
        (str(root), "<COMMITTED_STAGE>"),
        (str(deployment_root), "<CONTROLLED_DEPLOYMENT>"),
    )
    normalised: list[str] = []
    for value in argv:
        for prefix, label in replacements:
            if value.startswith(prefix):
                value = label + value[len(prefix) :]
                break
        normalised.append(value)
    return normalised


def _git_identity(root: Path) -> tuple[str, str]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return commit, tree


def run_beta_gate(
    root: Path,
    output: Path,
    python: str,
    commit: str,
    tree: str,
    deployment_root: Path | None = None,
) -> dict[str, object]:
    root = root.resolve(strict=True)
    deployment_root = (deployment_root or root).resolve(strict=True)
    output = output.absolute()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite beta gate record: {output}")
    if len(commit) != 40 or len(tree) != 40:
        raise ValueError("commit and tree must be complete 40-character Git identities")
    deployment_commit, deployment_tree = _git_identity(deployment_root)
    if (deployment_commit, deployment_tree) != (commit, tree):
        raise ValueError(
            "controlled deployment Git identity differs from the committed release stage"
        )
    deployment_status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=deployment_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if deployment_status:
        raise ValueError("controlled deployment worktree is not clean")
    interpreter = _release_interpreter(python)

    temporary = Path(tempfile.mkdtemp(prefix=".release-beta-gate.", dir=root.parent))
    results: list[dict[str, object]] = []
    try:
        definitions = gate_definitions(
            root,
            str(interpreter),
            temporary,
            deployment_root,
        )
        for gate in definitions:
            environment = dict(os.environ)
            environment.update(gate.environment)
            completed = subprocess.run(
                gate.argv,
                cwd=root,
                env=environment,
                capture_output=True,
                check=False,
                timeout=gate.timeout_seconds,
            )
            results.append(
                {
                    "name": gate.name,
                    "argv": _normalised_argv(root, deployment_root, gate.argv),
                    "exit_code": completed.returncode,
                    "stdout_bytes": len(completed.stdout),
                    "stdout_sha256": _sha256_bytes(completed.stdout),
                    "stderr_bytes": len(completed.stderr),
                    "stderr_sha256": _sha256_bytes(completed.stderr),
                }
            )
            if completed.returncode != 0:
                break
        expected_gate_count = len(definitions)
        succeeded = len(results) == expected_gate_count and all(
            result["exit_code"] == 0 for result in results
        )
        record: dict[str, object] = {
            "schema_version": "qualsight-controlled-beta-gate-v1",
            "release_authority": "CONTROLLED_SINGLE_USER_WORKSTATION_BETA",
            "git_commit": commit,
            "git_tree": tree,
            "path_bindings": {
                "<COMMITTED_STAGE>": "git archive of git_commit",
                "<CONTROLLED_DEPLOYMENT>": "clean controlled workstation checkout at git_commit",
            },
            "interpreter": {
                "version": subprocess.run(
                    [str(interpreter), "--version"],
                    cwd=root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=True,
                    text=True,
                ).stdout.strip(),
                "sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(),
            },
            "status": "PASS" if succeeded else "FAIL",
            "gate_count": len(results),
            "expected_gate_count": expected_gate_count,
            "results": results,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
        return record
    finally:
        shutil.rmtree(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--deployment-root", type=Path, required=True)
    args = parser.parse_args()
    record = run_beta_gate(
        args.root,
        args.output,
        args.python,
        args.commit,
        args.tree,
        args.deployment_root,
    )
    print(
        f"controlled beta release gate {record['status']} "
        f"({record['gate_count']}/{record['expected_gate_count']})"
    )
    return 0 if record["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
