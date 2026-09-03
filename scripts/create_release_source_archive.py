#!/usr/bin/env python3
"""Create the policy-filtered source archive for a controlled QualSight release."""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
from typing import Any
from urllib.parse import quote_from_bytes


POLICY_PATH = Path("resources/security/release-package-policy-v2.json")
PACKAGE_CONTENTS_PATH = PurePosixPath("QUALSIGHT-PACKAGE-CONTENTS.json")
SUMMARY_PATH = PurePosixPath(
    "resources/release-evidence/addon-acquisition-public-summary-v1.json"
)
CHECKPOINT_MANIFEST_PATH = PurePosixPath(
    "resources/manifests/matcher-checkpoint-acquisition-v1.json"
)
DATASET_MANIFEST_PATH = PurePosixPath(
    "resources/manifests/verification-dataset-acquisition-v1.json"
)
UPSTREAM_MANIFEST_PATH = PurePosixPath(
    "resources/manifests/matcher-upstream-sources-v1.json"
)
PROJECTED_MANIFEST_PATHS = (
    CHECKPOINT_MANIFEST_PATH,
    DATASET_MANIFEST_PATH,
    UPSTREAM_MANIFEST_PATH,
)
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
BASE64_TOKEN_PATTERN = re.compile(
    rb"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{8,}={0,2}(?![A-Za-z0-9+/_=-])"
)
FILE_URI_PATTERN = re.compile(
    rb"(?i)file:"
    + rb"(?:/{2,3}|%2f%2f(?:%2f)?)[^\s\"'<>\{\}]+"
)
WINDOWS_PRIVATE_PATH_PATTERN = re.compile(
    rb"(?i)(?:[A-Z]:[\\/]{1,2}(?:Users|Documents|AppData|Documents[ ]and[ ]Settings|ProgramData)[\\/]{1,2})"
)

FORBIDDEN_BINARY_PREFIXES = (
    ("JPEG image", b"\xff\xd8\xff"),
    ("PNG image", b"\x89PNG\r\n\x1a\n"),
    ("GIF image", b"GIF87a"),
    ("GIF image", b"GIF89a"),
    ("TIFF image", b"II*\x00"),
    ("TIFF image", b"MM\x00*"),
    ("BMP image", b"BM"),
    ("ZIP-family archive", b"PK\x03\x04"),
    ("ZIP-family archive", b"PK\x05\x06"),
    ("ZIP-family archive", b"PK\x07\x08"),
    ("gzip archive", b"\x1f\x8b"),
    ("bzip2 archive", b"BZh"),
    ("xz archive", b"\xfd7zXZ\x00"),
    ("7z archive", b"7z\xbc\xaf'\x1c"),
    ("RAR archive", b"Rar!\x1a\x07"),
    ("ELF executable", b"\x7fELF"),
    ("PE executable", b"MZ"),
    ("SQLite database", b"SQLite format 3\x00"),
    ("NumPy array", b"\x93NUMPY"),
    ("Parquet artifact", b"PAR1"),
)


def _forbidden_binary_kind(body: bytes) -> str | None:
    for label, prefix in FORBIDDEN_BINARY_PREFIXES:
        if body.startswith(prefix):
            return label
    if len(body) >= 12 and body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "WebP image"
    if len(body) >= 262 and body[257:262] == b"ustar":
        return "tar archive"
    return None


def _base64_private_locator(body: bytes, reference_home: bytes) -> str | None:
    private_locators = (
        ("operator home", reference_home),
        ("mounted filesystem root", b"/" + b"mnt" + b"/"),
        ("temporary root", b"/" + b"tmp" + b"/"),
    )
    for match in BASE64_TOKEN_PATTERN.finditer(body):
        token = match.group(0)
        if len(token) % 4 == 1:
            continue
        padded = token + b"=" * (-len(token) % 4)
        try:
            decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        except (ValueError, binascii.Error):
            continue
        for label, locator in private_locators:
            if locator in decoded:
                return label
    return None


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
    forbidden_artifact_sha256s = policy.get("source_archive_forbidden_artifact_sha256s")
    if (
        not isinstance(forbidden_artifact_sha256s, list)
        or not forbidden_artifact_sha256s
        or len(forbidden_artifact_sha256s) != len(set(forbidden_artifact_sha256s))
        or any(
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in forbidden_artifact_sha256s
        )
    ):
        raise ValueError(
            f"invalid forbidden artifact identities in release package policy: {policy_path}"
        )
    for key in (
        "source_archive_forbidden_names",
        "source_archive_forbidden_name_prefixes",
        "source_archive_forbidden_path_terms",
        "source_archive_forbidden_content_terms",
        "source_archive_controlled_locator_files",
    ):
        values = policy.get(key)
        if not isinstance(values, list) or not values:
            raise ValueError(f"release package policy has no {key}: {policy_path}")
        if any(
            not isinstance(value, str) or not value or value != value.lower()
            for value in values
        ):
            raise ValueError(f"invalid {key} in release package policy: {policy_path}")
    locator_paths = policy["source_archive_controlled_locator_files"]
    for value in locator_paths:
        pure = PurePosixPath(value)
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != value:
            raise ValueError(f"unsafe controlled locator path: {value!r}")
    reference_root = policy.get("source_archive_reference_home_projection")
    reference_projects_root = policy.get(
        "source_archive_reference_projects_projection"
    )
    reference_uv = policy.get("source_archive_reference_uv_command")
    if (
        reference_root != "/opt/qualsight/" + "external"
        or reference_projects_root != "/opt/qualsight/" + "external/projects"
        or reference_uv != "uv"
    ):
        raise ValueError(f"release package locator projection differs: {policy_path}")
    reference_projects_files = policy.get("source_archive_reference_projects_files")
    if (
        not isinstance(reference_projects_files, list)
        or not reference_projects_files
        or len(reference_projects_files) != len(set(reference_projects_files))
    ):
        raise ValueError(
            f"release package project-root allowlist differs: {policy_path}"
        )
    for value in reference_projects_files:
        if not isinstance(value, str):
            raise ValueError(
                f"release package project-root path is not text: {value!r}"
            )
        pure = PurePosixPath(value)
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != value:
            raise ValueError(f"unsafe release package project-root path: {value!r}")
    reference_uv_files = policy.get("source_archive_reference_uv_files")
    if (
        not isinstance(reference_uv_files, list)
        or not reference_uv_files
        or len(reference_uv_files) != len(set(reference_uv_files))
    ):
        raise ValueError(f"release package uv locator allowlist differs: {policy_path}")
    for value in reference_uv_files:
        if not isinstance(value, str):
            raise ValueError(f"release package uv locator path is not text: {value!r}")
        pure = PurePosixPath(value)
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != value:
            raise ValueError(f"unsafe release package uv locator path: {value!r}")
    json_fields = policy.get("source_archive_controlled_locator_json_fields")
    text_suffixes = policy.get("source_archive_controlled_locator_text_suffixes")
    if not isinstance(json_fields, dict) or not isinstance(text_suffixes, dict):
        raise ValueError(f"release package locator allowlist is absent: {policy_path}")
    if set(locator_paths) != {*json_fields, *text_suffixes}:
        raise ValueError(f"release package locator file/field allowlists differ: {policy_path}")
    for path, values in {**json_fields, **text_suffixes}.items():
        if not isinstance(path, str) or not isinstance(values, list) or not values:
            raise ValueError(f"invalid controlled locator field allowlist: {path!r}")
        if any(not isinstance(value, str) or not value.startswith("/") for value in values):
            raise ValueError(f"invalid controlled locator field in {path}")
    if policy.get("source_archive_secret_scan") is not True:
        raise ValueError(f"release package policy must enable secret scanning: {policy_path}")
    return policy


def _validate_regular_body(
    *, relative: PurePosixPath, body: bytes, policy: dict[str, Any]
) -> None:
    forbidden_binary_kind = _forbidden_binary_kind(body)
    if forbidden_binary_kind is not None:
        raise ValueError(
            f"forbidden release content {forbidden_binary_kind}: {relative}"
        )
    body_sha256 = hashlib.sha256(body).hexdigest()
    if body_sha256 in policy["source_archive_forbidden_artifact_sha256s"]:
        raise ValueError(
            f"forbidden release content registered runtime/model bytes: {relative}"
        )
    reference_home = _reference_home()
    base64_locator = _base64_private_locator(body, reference_home)
    if base64_locator is not None:
        raise ValueError(
            f"forbidden release content encoded {base64_locator}: {relative}"
        )
    if FILE_URI_PATTERN.search(body):
        raise ValueError(f"forbidden release content file URI: {relative}")
    if WINDOWS_PRIVATE_PATH_PATTERN.search(body):
        raise ValueError(f"forbidden release content Windows user path: {relative}")
    encoded_reference_home = (
        base64.b64encode(reference_home),
        reference_home.hex().encode("ascii"),
        reference_home.hex().upper().encode("ascii"),
        ", ".join(str(value) for value in reference_home).encode("ascii"),
        quote_from_bytes(reference_home, safe="").encode("ascii"),
        quote_from_bytes(reference_home, safe="").lower().encode("ascii"),
        reference_home.replace(b"/", b"\\u002f"),
        reference_home.replace(b"/", b"\\u002F"),
    )
    if any(marker in body for marker in encoded_reference_home):
        raise ValueError(f"forbidden release content encoded operator home: {relative}")
    if relative != POLICY_PATH:
        for label, locator in (
            ("mounted filesystem root", b"/" + b"mnt" + b"/"),
            ("temporary root", b"/" + b"tmp" + b"/"),
        ):
            encoded_locators = (
                base64.b64encode(locator),
                locator.hex().encode("ascii"),
                locator.hex().upper().encode("ascii"),
                ", ".join(str(value) for value in locator).encode("ascii"),
                quote_from_bytes(locator, safe="").encode("ascii"),
                quote_from_bytes(locator, safe="").lower().encode("ascii"),
                locator.replace(b"/", b"\\u002f"),
                locator.replace(b"/", b"\\u002F"),
            )
            if any(marker in body for marker in encoded_locators):
                raise ValueError(
                    f"forbidden release content encoded {label}: {relative}"
                )
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
    if relative != POLICY_PATH:
        lowered = body.lower()
        for term in policy["source_archive_forbidden_content_terms"]:
            encoded = term.encode("utf-8")
            if encoded in lowered:
                raise ValueError(f"forbidden release content term {term!r}: {relative}")
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(body):
            raise ValueError(f"forbidden release content {label}: {relative}")


def _json_payload(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _json_value_locations(value: Any, needle: str, pointer: str = "") -> set[str]:
    locations: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            locations.update(_json_value_locations(child, needle, f"{pointer}/{escaped}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            locations.update(_json_value_locations(child, needle, f"{pointer}/{index}"))
    elif isinstance(value, str) and needle in value:
        locations.add(pointer)
    return locations


def _reference_home() -> bytes:
    home = Path.home().resolve(strict=True)
    if not home.is_absolute() or home == Path("/"):
        raise ValueError(f"unsafe operator home directory: {home}")
    return home.as_posix().encode("utf-8")


def _validate_reference_home_location(
    path: PurePosixPath,
    body: bytes,
    policy: dict[str, Any],
    *,
    reference_home: bytes,
) -> None:
    relative = path.as_posix()
    json_fields = policy["source_archive_controlled_locator_json_fields"]
    text_suffixes = policy["source_archive_controlled_locator_text_suffixes"]
    if relative in json_fields:
        actual = _json_value_locations(json.loads(body), reference_home.decode("utf-8"))
        expected = set(json_fields[relative])
        if actual != expected:
            raise ValueError(
                f"operator home path differs from its exact field allowlist in {relative}: "
                f"expected {sorted(expected)}, observed {sorted(actual)}"
            )
        return
    if relative in text_suffixes:
        remaining = body
        for suffix in text_suffixes[relative]:
            target = reference_home + suffix.encode("utf-8")
            if remaining.count(target) != 1:
                raise ValueError(
                    f"operator home path differs from its exact value allowlist in {relative}"
                )
            remaining = remaining.replace(target, b"")
        if reference_home in remaining:
            raise ValueError(
                f"operator home path exceeds its exact value allowlist in {relative}"
            )
        return
    raise ValueError(f"forbidden release content term operator home path: {relative}")


def _project_release_members(
    bodies: dict[PurePosixPath, bytes], policy: dict[str, Any]
) -> tuple[dict[PurePosixPath, bytes], dict[str, str]]:
    projected = dict(bodies)
    required_manifests = [path for path in PROJECTED_MANIFEST_PATHS if path in projected]
    manifest_identities: dict[str, str] = {}
    if required_manifests:
        if SUMMARY_PATH not in projected:
            raise ValueError(f"release evidence summary is absent: {SUMMARY_PATH}")
        summary = json.loads(projected[SUMMARY_PATH])
        if (
            summary.get("distribution_boundary")
            != "CONTROLLED_SINGLE_USER_WORKSTATION_NON_PORTABLE"
            or summary.get("authority")
            != {
                "artifact_identity_authority": False,
                "identity_only": True,
                "rights_authority": False,
                "runtime_authority": False,
            }
        ):
            raise ValueError("release evidence summary claims unsupported authority")
        if CHECKPOINT_MANIFEST_PATH in projected:
            checkpoint_source = json.loads(projected[CHECKPOINT_MANIFEST_PATH])
            expected_identities = {
                (
                    entry["published_evidence"]["repository_relative_path"],
                    entry["published_evidence"]["artifact_sha256"],
                    entry["published_evidence"]["artifact_size_bytes"],
                )
                for entry in checkpoint_source["entries"]
            }
            observed_identities = {
                (
                    entry["repository_relative_path"],
                    entry["sha256"],
                    entry["size_bytes"],
                )
                for entry in summary.get("original_evidence_identities", ())
            }
            if observed_identities != expected_identities:
                raise ValueError("summary evidence identities differ from checkpoint authority")
        summary_body = projected[SUMMARY_PATH]
        summary_sha256 = hashlib.sha256(summary_body).hexdigest()
        summary_size = len(summary_body)

        if CHECKPOINT_MANIFEST_PATH in projected:
            checkpoint = json.loads(projected[CHECKPOINT_MANIFEST_PATH])
            for entry in checkpoint["entries"]:
                evidence = entry["published_evidence"]
                evidence["repository_relative_path"] = SUMMARY_PATH.as_posix()
                evidence["artifact_sha256"] = summary_sha256
                evidence["artifact_size_bytes"] = summary_size
                evidence["resolution_note"] = (
                    f"{evidence['resolution_note']} The packaged summary records identity only; "
                    "it does not verify rights, local presence, runtime readiness, or redistribution authority."
                )
            projected[CHECKPOINT_MANIFEST_PATH] = _json_payload(checkpoint)

        if DATASET_MANIFEST_PATH in projected:
            dataset = json.loads(projected[DATASET_MANIFEST_PATH])
            dataset["archive"]["evidence_path"] = SUMMARY_PATH.as_posix()
            dataset["archive"]["evidence_sha256"] = summary_sha256
            dataset["policy"] = (
                f"{dataset['policy']} The packaged summary records identity only and does not "
                "verify rights, local presence, runtime readiness, or redistribution authority."
            )
            projected[DATASET_MANIFEST_PATH] = _json_payload(dataset)

        if UPSTREAM_MANIFEST_PATH in projected:
            upstream = json.loads(projected[UPSTREAM_MANIFEST_PATH])
            upstream["policy"] = (
                f"{upstream['policy']} The package omits the reviewed source/license evidence "
                "bytes; its public summary is identity-only and grants no rights or runtime authority."
            )
            for source in upstream["sources"]:
                note = source["license_note"].replace(
                    "resources/upstream/", "omitted-controlled-evidence/"
                )
                source["license_note"] = (
                    "CONTROLLED_HOST_CLAIM_NOT_VERIFIED_BY_PACKAGE_SUMMARY: " + note
                )
            projected[UPSTREAM_MANIFEST_PATH] = _json_payload(upstream)

        for path in required_manifests:
            manifest_identities[path.as_posix()] = hashlib.sha256(projected[path]).hexdigest()

    reference_home = _reference_home()
    reference_uv = reference_home + b"/.local/bin/uv"
    reference_uv_files = set(policy["source_archive_reference_uv_files"])
    locator_root = policy["source_archive_reference_home_projection"].encode("utf-8")
    reference_projects_root = b"/" + b"mnt" + b"/" + b"projects" + b"/"
    projects_locator_root = policy[
        "source_archive_reference_projects_projection"
    ].encode("utf-8") + b"/"
    reference_projects_files = set(policy["source_archive_reference_projects_files"])
    uv_command = policy["source_archive_reference_uv_command"].encode("utf-8")
    for path, original in tuple(projected.items()):
        body = original
        if reference_uv in body:
            if path.as_posix() not in reference_uv_files or body.count(reference_uv) != 1:
                raise ValueError(
                    f"operator uv command path appears outside its exact file allowlist: {path}"
                )
            body = body.replace(reference_uv, uv_command)
        if reference_home in body:
            _validate_reference_home_location(
                path, body, policy, reference_home=reference_home
            )
            body = body.replace(reference_home, locator_root)
        if reference_projects_root in body and path != POLICY_PATH:
            if path.as_posix() not in reference_projects_files:
                raise ValueError(
                    f"project root appears outside its exact file allowlist: {path}"
                )
            body = body.replace(reference_projects_root, projects_locator_root)
        projected[path] = body
    return projected, manifest_identities


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
    commit_epoch = int(
        subprocess.run(
            ["git", "show", "-s", "--format=%ct", resolved_commit],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, partial_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".partial", dir=output.parent
    )
    os.close(descriptor)
    partial = Path(partial_name)
    try:
        raw_tar = Path(f"{partial}.raw")
        command = [
            "git",
            "archive",
            "--format=tar",
            f"--prefix={prefix}/",
            "-o",
            str(raw_tar),
            resolved_commit,
            "--",
            ".",
            *(f":(exclude){path}" for path in exclusions),
        ]
        subprocess.run(command, cwd=root, check=True)
        members: list[tuple[tarfile.TarInfo, PurePosixPath | None]] = []
        bodies: dict[PurePosixPath, bytes] = {}
        with tarfile.open(raw_tar, mode="r:") as archive:
            for member in archive:
                pure = PurePosixPath(member.name)
                if pure.is_absolute() or ".." in pure.parts:
                    raise ValueError(f"unsafe path in generated source archive: {member.name}")
                if not pure.parts or pure.parts[0] != prefix:
                    raise ValueError(f"source archive member escapes prefix: {member.name}")
                relative_text = PurePosixPath(*pure.parts[1:]).as_posix()
                if any(
                    relative_text == path or relative_text.startswith(f"{path}/")
                    for path in exclusions
                ):
                    raise ValueError(f"excluded path entered source archive: {relative_text}")
                if not (member.isdir() or member.isreg()):
                    raise ValueError(
                        f"forbidden release content non-regular member: {relative_text}"
                    )
                relative = PurePosixPath(relative_text) if member.isreg() else None
                if relative is not None:
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError(f"cannot inspect source archive member: {relative}")
                    bodies[relative] = source.read()
                members.append((copy.copy(member), relative))

        projected_bodies, projected_identities = _project_release_members(bodies, policy)
        for member, relative in members:
            if relative is None:
                member.mode = 0o755
            else:
                member.mode = 0o755 if member.mode & 0o111 else 0o644
        if PACKAGE_CONTENTS_PATH in projected_bodies:
            raise ValueError(
                f"reserved package contents manifest already exists: {PACKAGE_CONTENTS_PATH}"
            )
        contents_rows = [
            {
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(projected_bodies[relative]).hexdigest(),
                "size_bytes": len(projected_bodies[relative]),
                "unix_mode": f"{member.mode & 0o7777:04o}",
            }
            for member, relative in members
            if relative is not None
        ]
        contents_rows.sort(key=lambda item: item["path"])
        contents_body = (
            json.dumps(
                {
                    "schema_version": "qualsight-source-package-contents-v1",
                    "source_commit": resolved_commit,
                    "files": contents_rows,
                },
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        projected_bodies[PACKAGE_CONTENTS_PATH] = contents_body
        contents_member = tarfile.TarInfo(f"{prefix}/{PACKAGE_CONTENTS_PATH.as_posix()}")
        contents_member.size = len(contents_body)
        contents_member.mode = 0o644
        contents_member.mtime = commit_epoch
        contents_member.uid = 0
        contents_member.gid = 0
        contents_member.uname = ""
        contents_member.gname = ""
        members.append((contents_member, PACKAGE_CONTENTS_PATH))
        with partial.open("wb") as compressed:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=compressed, mtime=commit_epoch
            ) as gzip_file:
                with tarfile.open(
                    fileobj=gzip_file, mode="w", format=tarfile.GNU_FORMAT
                ) as archive:
                    for member, relative in members:
                        if relative is None:
                            archive.addfile(member)
                            continue
                        body = projected_bodies[relative]
                        member.size = len(body)
                        member.pax_headers = {}
                        archive.addfile(member, io.BytesIO(body))

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
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError(f"cannot inspect source archive member: {relative}")
                    _validate_regular_body(
                        relative=PurePosixPath(relative),
                        body=source.read(),
                        policy=policy,
                    )
                    regular_file_count += 1
                member_count += 1
        os.link(partial, output)
    finally:
        partial.unlink(missing_ok=True)
        Path(f"{partial}.raw").unlink(missing_ok=True)
    return {
        "commit": resolved_commit,
        "member_count": member_count,
        "scanned_regular_file_count": regular_file_count,
        "policy_id": policy["policy_id"],
        "excluded_repository_paths": list(exclusions),
        "projected_manifest_count": len(projected_identities),
        "projected_manifest_sha256": projected_identities,
        "package_contents_sha256": hashlib.sha256(contents_body).hexdigest(),
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
