#!/usr/bin/env python3
"""Create the policy-filtered source archive for a controlled QualSight release."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
from typing import Any


POLICY_PATH = Path("resources/security/release-package-policy-v1.json")
SECRET_PATTERNS = (
    (
        "private key",
        re.compile(b"-----BEGIN " + b"(?:RSA |EC |OPENSSH )?" + b"PRIVATE KEY-----"),
    ),
    (
        "AWS access key",
        re.compile(b"(?<![A-Z0-9])" + b"AKIA" + b"[0-9A-Z]{16}(?![A-Z0-9])"),
    ),
    (
        "GitHub access token",
        re.compile(
            b"(?<![A-Za-z0-9_])"
            + b"(?:ghp|gho|ghu|ghs|ghr)_"
            + b"[A-Za-z0-9]{30,}(?![A-Za-z0-9])"
        ),
    ),
    (
        "GitHub fine-grained access token",
        re.compile(
            b"(?<![A-Za-z0-9_])"
            + b"github_pat_"
            + b"[A-Za-z0-9_]{20,}(?![A-Za-z0-9_])"
        ),
    ),
    (
        "OpenAI access token",
        re.compile(b"(?<![A-Za-z0-9])" + b"sk-" + b"[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    ),
)


def _load_policy(policy_path: Path) -> dict[str, Any]:
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if policy.get("schema_version") != "1.0" or not policy.get("policy_id"):
        raise ValueError(f"invalid release package policy: {policy_path}")
    exclusions = policy.get("source_archive_excluded_paths")
    if not isinstance(exclusions, list) or not exclusions:
        raise ValueError(f"release package policy has no source exclusions: {policy_path}")
    for value in exclusions:
        if not isinstance(value, str):
            raise ValueError(f"release package exclusion is not text: {value!r}")
        pure = PurePosixPath(value)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts or pure.as_posix() != value:
            raise ValueError(f"unsafe release package exclusion: {value!r}")
    suffixes = policy.get("source_archive_forbidden_suffixes")
    if not isinstance(suffixes, list) or not suffixes:
        raise ValueError(f"release package policy has no forbidden suffixes: {policy_path}")
    if any(
        not isinstance(value, str)
        or not value.startswith(".")
        or value != value.lower()
        or "/" in value
        for value in suffixes
    ):
        raise ValueError(f"invalid forbidden suffix in release package policy: {policy_path}")
    for key in (
        "source_archive_forbidden_names",
        "source_archive_forbidden_name_prefixes",
        "source_archive_forbidden_path_terms",
    ):
        values = policy.get(key)
        if not isinstance(values, list) or not values:
            raise ValueError(f"release package policy has no {key}: {policy_path}")
        if any(
            not isinstance(value, str) or not value or value != value.lower()
            for value in values
        ):
            raise ValueError(f"invalid {key} in release package policy: {policy_path}")
    if policy.get("source_archive_secret_scan") is not True:
        raise ValueError(f"release package policy must enable secret scanning: {policy_path}")
    return policy


def _validate_regular_member(
    *,
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    relative: PurePosixPath,
    policy: dict[str, Any],
) -> None:
    name = relative.name.casefold()
    suffix = relative.suffix.casefold()
    if suffix in policy["source_archive_forbidden_suffixes"]:
        raise ValueError(f"forbidden release content suffix {suffix}: {relative}")
    if name in policy["source_archive_forbidden_names"] or any(
        name.startswith(prefix) for prefix in policy["source_archive_forbidden_name_prefixes"]
    ):
        raise ValueError(f"forbidden release content credential path: {relative}")
    if any(
        term in part.casefold()
        for part in relative.parts
        for term in policy["source_archive_forbidden_path_terms"]
    ):
        raise ValueError(f"forbidden release content observation path: {relative}")
    source = archive.extractfile(member)
    if source is None:
        raise ValueError(f"cannot inspect source archive member: {relative}")
    body = source.read()
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(body):
            raise ValueError(f"forbidden release content {label}: {relative}")


def write_source_archive(
    *, root: Path, output: Path, commit: str, prefix: str, policy_path: Path | None = None
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    if not prefix or "/" in prefix or prefix in {".", ".."}:
        raise ValueError(f"invalid source archive prefix: {prefix!r}")
    policy_path = (policy_path or (root / POLICY_PATH)).resolve(strict=True)
    policy = _load_policy(policy_path)
    exclusions = tuple(policy["source_archive_excluded_paths"])
    resolved_commit = subprocess.run(
        ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, partial_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".partial", dir=output.parent
    )
    os.close(descriptor)
    partial = Path(partial_name)
    try:
        command = [
            "git",
            "archive",
            "--format=tar.gz",
            f"--prefix={prefix}/",
            "-o",
            str(partial),
            resolved_commit,
            "--",
            ".",
            *(f":(exclude){path}" for path in exclusions),
        ]
        subprocess.run(command, cwd=root, check=True)
        member_count = 0
        regular_file_count = 0
        with tarfile.open(partial, mode="r:gz") as archive:
            for member in archive:
                pure = PurePosixPath(member.name)
                if pure.is_absolute() or ".." in pure.parts:
                    raise ValueError(f"unsafe path in generated source archive: {member.name}")
                if not pure.parts or pure.parts[0] != prefix:
                    raise ValueError(f"source archive member escapes prefix: {member.name}")
                relative = PurePosixPath(*pure.parts[1:]).as_posix()
                if any(relative == path or relative.startswith(f"{path}/") for path in exclusions):
                    raise ValueError(f"excluded path entered source archive: {relative}")
                if not (member.isdir() or member.isreg()):
                    raise ValueError(f"forbidden release content non-regular member: {relative}")
                if member.isreg():
                    _validate_regular_member(
                        archive=archive,
                        member=member,
                        relative=PurePosixPath(relative),
                        policy=policy,
                    )
                    regular_file_count += 1
                member_count += 1
        os.link(partial, output)
    finally:
        partial.unlink(missing_ok=True)
    return {
        "commit": resolved_commit,
        "member_count": member_count,
        "scanned_regular_file_count": regular_file_count,
        "policy_id": policy["policy_id"],
        "excluded_repository_paths": list(exclusions),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--policy", type=Path)
    args = parser.parse_args()
    result = write_source_archive(
        root=args.root,
        output=args.output,
        commit=args.commit,
        prefix=args.prefix,
        policy_path=args.policy,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
