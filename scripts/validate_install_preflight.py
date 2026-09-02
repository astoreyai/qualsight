#!/usr/bin/env python3
"""Refuse installation unless the exact reviewed workstation toolchain is active."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess


EXPECTED_PLATFORM = {
    "system": "Linux",
    "machine": "x86_64",
    "python": "Python 3.11.2",
    "uv": "uv 0.9.26",
    "node": "v26.7.0",
    "pnpm": "10.28.0",
}


class InstallPreflightError(ValueError):
    """Raised before installation when the reviewed platform contract differs."""


def _version(executable: Path, label: str) -> str:
    try:
        resolved = executable.resolve(strict=True)
    except OSError as error:
        raise InstallPreflightError(f"{label} executable is unavailable: {executable}") from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise InstallPreflightError(f"{label} executable is not a regular executable: {resolved}")
    completed = subprocess.run(
        [str(resolved), "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=15,
    )
    observed = completed.stdout.strip()
    if completed.returncode != 0 or "\n" in observed or "\r" in observed:
        raise InstallPreflightError(f"{label} version command failed deterministically")
    return observed


def validate_toolchain(*, python: Path, uv: Path, node: Path, pnpm: Path) -> dict[str, str]:
    observed = {
        "system": platform.system(),
        "machine": platform.machine(),
        "python": _version(python, "python"),
        "uv": _version(uv, "uv"),
        "node": _version(node, "node"),
        "pnpm": _version(pnpm, "pnpm"),
    }
    for label, expected in EXPECTED_PLATFORM.items():
        if observed[label] != expected:
            raise InstallPreflightError(
                f"{label} version differs: expected {expected!r}, observed {observed[label]!r}"
            )
    return observed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--pnpm", type=Path, required=True)
    args = parser.parse_args()
    try:
        observed = validate_toolchain(
            python=args.python,
            uv=args.uv,
            node=args.node,
            pnpm=args.pnpm,
        )
    except (InstallPreflightError, subprocess.SubprocessError) as error:
        print(f"INSTALL_PREFLIGHT_REFUSED {error}", file=os.sys.stderr)
        return 2
    print("INSTALL_PREFLIGHT_VALID " + json.dumps(observed, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
