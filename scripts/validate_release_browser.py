#!/usr/bin/env python3
"""Execute and aggregate the complete beta3 browser validation surface."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
from typing import Any


BROWSER_VALIDATORS = (
    "validate_comparison_studio",
    "validate_evidence_warning_privacy",
    "validate_manage_system",
    "validate_owner_purge_ui",
    "validate_stress_studio",
    "validate_structured_evidence_navigation",
    "validate_upload_working_sample",
    "validate_utility_overlay",
    "observe_console",
)


def _identity(path: Path) -> dict[str, Any]:
    observed = path.lstat()
    if path.is_symlink() or not path.is_file() or observed.st_size < 1:
        raise ValueError(f"browser observation is not a non-empty regular file: {path.name}")
    body = path.read_bytes()
    return {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}


def _stream_identity(body: bytes) -> dict[str, Any]:
    return {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}


def _run_validator(
    command: list[str], *, root: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        command,
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=3600)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        return subprocess.CompletedProcess(
            command,
            124,
            stdout,
            stderr + b"browser validator exceeded the 3600-second bound\n",
        )


def validate_browser_workflows(
    *, root: Path, work_root: Path, output: Path, python: Path
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    python = python.absolute()
    if not python.is_file():
        raise ValueError(f"browser gate interpreter is absent: {python}")
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"refusing to overwrite browser aggregate: {output}")
    work_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    environment = {
        **os.environ,
        "PYTHONPATH": f"{root / 'backend' / 'src'}:{root}",
        "QUALSIGHT_REPOSITORY_ROOT": os.environ.get(
            "QUALSIGHT_REPOSITORY_ROOT", str(root)
        ),
    }
    results: list[dict[str, Any]] = []
    for name in BROWSER_VALIDATORS:
        script = root / "scripts" / f"{name}.py"
        if not script.is_file():
            raise FileNotFoundError(f"beta3 browser validator is absent: {name}")
        observation = work_root / f"{name}.json"
        command = [str(python), str(script)]
        if name == "observe_console":
            command.extend(
                [
                    "--screenshot-dir",
                    str(work_root / "console-images"),
                    "--observation",
                    str(observation),
                    "--data-dir",
                    str(work_root / "console-data"),
                ]
            )
        else:
            command.extend(["--observation", str(observation)])
        completed = _run_validator(command, root=root, environment=environment)
        result: dict[str, Any] = {
            "validator": name,
            "exit_code": completed.returncode,
            "stdout": _stream_identity(completed.stdout),
            "stderr": _stream_identity(completed.stderr),
        }
        try:
            result["observation"] = _identity(observation)
        except (OSError, ValueError) as error:
            result["observation_error"] = type(error).__name__
        results.append(result)
        if completed.returncode != 0 or "observation" not in result:
            break
    passed = len(results) == len(BROWSER_VALIDATORS) and all(
        result["exit_code"] == 0 and "observation" in result for result in results
    )
    report: dict[str, Any] = {
        "schema_version": "qualsight-beta3-browser-aggregate-v1",
        "status": "PASS" if passed else "FAIL",
        "expected_validator_count": len(BROWSER_VALIDATORS),
        "completed_validator_count": len(results),
        "validators": results,
        "privacy": {
            "raw_observations_included": False,
            "absolute_paths_included": False,
            "stdout_stderr_included": False,
            "full_sha256_identities_included": True,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = validate_browser_workflows(
            root=args.root,
            work_root=args.work_root,
            output=args.output,
            python=args.python,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"BETA3_BROWSER_AGGREGATE_REFUSED {error}")
        return 2
    print(
        f"BETA3_BROWSER_AGGREGATE_{report['status']} "
        f"{report['completed_validator_count']}/{report['expected_validator_count']}"
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
