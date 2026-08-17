#!/usr/bin/env python3
"""Validate the non-executing InsightFace buffalo_l release-provenance evidence."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
from typing import Any
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROVENANCE_PATH = (
    PROJECT_ROOT
    / "resources"
    / "manifests"
    / "insightface-buffalo-l-release-provenance-v1.json"
)
REGISTRY_PATH = PROJECT_ROOT / "resources" / "resource-registry.json"
AUTHORIZATION_PATH = (
    PROJECT_ROOT
    / "resources"
    / "authorizations"
    / "qualsight-insightface-buffalo-l-evidence-acquisition-2026-08-17.json"
)
RELEASE_JSON_PATH = (
    PROJECT_ROOT / "resources" / "upstream" / "insightface" / "release-v0.7.json"
)
RELEASE_HEADERS_PATH = RELEASE_JSON_PATH.with_suffix(".headers")
ASSET_HEADERS_PATH = (
    PROJECT_ROOT
    / "resources"
    / "upstream"
    / "insightface"
    / "buffalo_l-v0.7-final.headers"
)
UPSTREAM_DOCUMENTATION_PATH = (
    PROJECT_ROOT
    / "resources"
    / "upstream"
    / "insightface"
    / "python-package-README.7fadd420.md"
)
LOCAL_ARCHIVE_PATH = Path("/home/aaron/.insightface/models/buffalo_l.zip")
LOCAL_DETECTOR_PATH = Path(
    "/home/aaron/.insightface/models/buffalo_l/det_10g.onnx"
)
CURL_PATH = Path("/usr/bin/curl")
RESOURCE_ID = "insightface-scrfd-10g-local-candidate"
AUTHORIZATION_SCOPE = (
    "ACQUIRE_INSIGHTFACE_V0_7_BUFFALO_L_FOR_STATIC_PROVENANCE_REVIEW"
)
RELEASE_API_URL = (
    "https://api.github.com/repos/deepinsight/insightface/releases/tags/v0.7"
)
ASSET_URL = (
    "https://github.com/deepinsight/insightface/releases/download/v0.7/"
    "buffalo_l.zip"
)
ARCHIVE_SHA256 = "80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f"
ARCHIVE_MD5 = "6c0e929fd3b6ab517170b732ced18c68"
ARCHIVE_SIZE = 288621354
DETECTOR_SHA256 = "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91"
DETECTOR_SIZE = 16923827
AUTHORIZATION_SHA256 = "4ea9fca18ed5b1a95b55d59aff5f6995eb4f064f6374ebd45a192a2b6aadb481"
RELEASE_JSON_SHA256 = "1458ba41add0f0f022e244bbc44aaebd3b03a06fc1620b9a190725fd1de6dca0"
RELEASE_HEADERS_SHA256 = "cf7a746f39d8e94f2858c832d04dadb8e51a33fcbc85bd24ca80b3e4677d2af3"
ASSET_HEADERS_SHA256 = "ec2eade1010ec13f2a97b3463c13e97cf0fc0b51e8db2ca920bf6fdf5d698543"
CURL_SHA256 = "27125f0331490b7fbf4da11f2bd913ce1b94e071367b2fa8e535ce8c5526e29c"
REGISTRY_SHA256 = "a07904c77fbd3ec8393f147970ca1774651def966f97f5a123fab22b480f61ce"
REGISTRY_SIZE = 140200
REQUIRED_STATE = "APPROVED_LOCAL_RESEARCH_USE"
REQUIRED_APPROVED_SCOPES = [
    "QUALSIGHT_LOCAL_LFW_THREE_MATCHER_TWO_PREPROCESSING_V1",
    "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1",
]
USE_PERMISSION_RECEIPT = (
    PROJECT_ROOT
    / "resources"
    / "authorizations"
    / "qualsight-owner-use-permission-granted-2026-08-17.json"
)
UPSTREAM_DOCUMENTATION_SHA256 = (
    "4d5d26d2f990845b81331dd7589ec291407847d72f13d012e19c712e620e7972"
)
EXPECTED_MEMBERS = [
    {
        "path": "genderage.onnx",
        "size_bytes": 1322532,
        "compressed_size_bytes": 1216027,
        "crc32": "5f7cd633",
        "sha256": "4fde69b1c810857b88c64a335084f1c3fe8f01246c9a191b48c7bb756d6652fb",
        "unix_mode": 33204,
    },
    {
        "path": "2d106det.onnx",
        "size_bytes": 5030888,
        "compressed_size_bytes": 4646511,
        "crc32": "8664693e",
        "sha256": "f001b856447c413801ef5c42091ed0cd516fcd21f2d6b79635b1e733a7109dbf",
        "unix_mode": 33204,
    },
    {
        "path": "det_10g.onnx",
        "size_bytes": DETECTOR_SIZE,
        "compressed_size_bytes": 15721951,
        "crc32": "df529b9e",
        "sha256": DETECTOR_SHA256,
        "unix_mode": 33204,
    },
    {
        "path": "1k3d68.onnx",
        "size_bytes": 143607619,
        "compressed_size_bytes": 106775201,
        "crc32": "3d2d8cff",
        "sha256": "df5c06b8a0c12e422b2ed8947b8869faa4105387f199c477af038aa01f9a45cc",
        "unix_mode": 33204,
    },
    {
        "path": "w600k_r50.onnx",
        "size_bytes": 174383860,
        "compressed_size_bytes": 160260914,
        "crc32": "707f504c",
        "sha256": "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
        "unix_mode": 33188,
    },
]
EXPECTED_CLAIMS = {
    "official_release_asset_metadata_bound": True,
    "fresh_transfer_observation_recorded": True,
    "fresh_transfer_equals_local_archive": True,
    "local_archive_matches_final_server_md5": True,
    "detector_member_equals_registered_model": True,
    "original_local_acquisition_provenance_closed": False,
    "model_rights_closed": False,
    "runtime_validated": False,
    "model_execution_performed": False,
}
EXPECTED_LIMITATIONS = [
    "The fresh official transfer was removed only after byte equality with the preserved local archive; the preserved local archive is the reproducible byte surface.",
    "Raw redirect and request traces were not preserved because GitHub release redirects contain expiring signed query parameters; the request URL is project-recorded and only the final nonsensitive server headers are retained.",
    "Current release-asset equivalence does not reconstruct the historical acquisition event or mtime of the existing local archive.",
    "The upstream noncommercial-research notice is not a redistribution license or an independent legal-rights determination.",
    "No ZIP member was extracted, no face image was decoded, and no detector, model, matcher, ONNX runtime, or OFIQ process was loaded or executed.",
]
MAX_JSON_BYTES = 4 * 1024 * 1024


class SCRFDReleaseProvenanceValidationError(ValueError):
    """Raised when the frozen SCRFD release-provenance evidence is inconsistent."""


def _fail(location: str, message: str) -> None:
    raise SCRFDReleaseProvenanceValidationError(f"{location}: {message}")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("json", f"duplicate key {key}")
        result[key] = value
    return result


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(location, "must be an object")
    return value


def _sequence(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(location, "must be an array")
    return value


def _strict_keys(value: dict[str, Any], location: str, expected: set[str]) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing:
        _fail(location, f"missing fields: {', '.join(missing)}")
    if extra:
        _fail(location, f"unknown fields: {', '.join(extra)}")


def _load_json(path: Path, location: str) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if size < 1 or size > MAX_JSON_BYTES:
            _fail(location, f"JSON size {size} is outside the accepted range")
        return _mapping(
            json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys),
            location,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail(location, f"cannot read strict UTF-8 JSON from {path}: {error}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        _fail(str(path), f"cannot hash file: {error}")
    return digest.hexdigest()


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        _fail(str(path), f"cannot hash file: {error}")
    return digest.hexdigest()


def _identity(
    value: Any,
    location: str,
    *,
    expected_path: Path,
    expected_sha256: str,
    expected_size: int,
    maximum_size: int,
) -> Path:
    record = _mapping(value, location)
    _strict_keys(record, location, {"path", "sha256", "size_bytes"})
    if not isinstance(record["path"], str):
        _fail(f"{location}.path", "must be a string")
    path = Path(record["path"])
    if not path.is_absolute() or path != expected_path:
        _fail(f"{location}.path", f"must equal {expected_path}")
    size = record["size_bytes"]
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or not 1 <= size <= maximum_size
        or size != expected_size
    ):
        _fail(f"{location}.size_bytes", "is outside the accepted range")
    digest = record["sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or digest != expected_sha256
    ):
        _fail(f"{location}.sha256", "must be a lowercase SHA-256")
    try:
        observed_size = path.stat().st_size
    except OSError as error:
        _fail(location, f"cannot stat {path}: {error}")
    if observed_size != size or _sha256(path) != digest:
        _fail(location, f"identity differs for {path}")
    return path


def _parse_time(value: Any, location: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(location, "must be a nonempty ISO-8601 timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        _fail(location, f"cannot parse timestamp: {error}")
    if result.tzinfo is None or result.utcoffset() is None:
        _fail(location, "must include a UTC offset")
    return result


def _read_headers(path: Path, location: str) -> dict[str, str]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        _fail(location, f"cannot read headers: {error}")
    if not 1 <= len(raw) <= 32 * 1024:
        _fail(location, "header byte count is outside the accepted range")
    text = raw.decode("iso-8859-1").replace("\r\n", "\n").strip()
    blocks = [block for block in text.split("\n\n") if block.strip()]
    if len(blocks) != 1:
        _fail(location, "must contain exactly one response block")
    lines = blocks[0].splitlines()
    if lines[0].strip() != "HTTP/2 200":
        _fail(location, "status line must equal HTTP/2 200")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            _fail(location, f"malformed header line {line!r}")
        name, content = line.split(":", 1)
        key = name.strip().lower()
        value = content.strip()
        if not key or not value or key in headers:
            _fail(location, f"empty or duplicate header {name!r}")
        headers[key] = value
    return headers


def _validate_authorization(value: Any) -> datetime:
    location = "provenance.owner_authorization"
    record = _mapping(value, location)
    _strict_keys(
        record,
        location,
        {"path", "sha256", "size_bytes", "decision", "scope"},
    )
    path = _identity(
        {key: record[key] for key in ("path", "sha256", "size_bytes")},
        location,
        expected_path=AUTHORIZATION_PATH,
        expected_sha256=AUTHORIZATION_SHA256,
        expected_size=2109,
        maximum_size=MAX_JSON_BYTES,
    )
    if record["decision"] != "APPROVED_EVIDENCE_ACQUISITION":
        _fail(f"{location}.decision", "must remain evidence-acquisition only")
    if record["scope"] != AUTHORIZATION_SCOPE:
        _fail(f"{location}.scope", f"must equal {AUTHORIZATION_SCOPE}")
    authorization = _load_json(path, "authorization")
    _strict_keys(
        authorization,
        "authorization",
        {
            "schema_version",
            "authorization_id",
            "recorded_at",
            "authority",
            "decision",
            "resource_id",
            "scope",
            "decision_evidence",
            "permitted_actions",
            "prohibited_actions",
            "redistribution_allowed",
        },
    )
    if authorization["schema_version"] != "1.0.0":
        _fail("authorization.schema_version", "must equal 1.0.0")
    if authorization["authorization_id"] != (
        "qualsight-insightface-buffalo-l-evidence-acquisition-2026-08-17"
    ):
        _fail("authorization.authorization_id", "differs")
    if authorization["authority"] != "QualSight project owner (interactive user)":
        _fail("authorization.authority", "differs")
    if authorization["decision"] != record["decision"]:
        _fail("authorization.decision", "differs from bound decision")
    if authorization["resource_id"] != RESOURCE_ID:
        _fail("authorization.resource_id", "differs")
    if authorization["scope"] != record["scope"]:
        _fail("authorization.scope", "differs from bound scope")
    if authorization["redistribution_allowed"] is not False:
        _fail("authorization.redistribution_allowed", "must be false")
    permitted = _sequence(authorization["permitted_actions"], "authorization.permitted_actions")
    prohibited = _sequence(authorization["prohibited_actions"], "authorization.prohibited_actions")
    if len(permitted) != 5 or not all(isinstance(item, str) and item for item in permitted):
        _fail("authorization.permitted_actions", "must contain five bounded actions")
    if len(prohibited) != 4 or not all(isinstance(item, str) and item for item in prohibited):
        _fail("authorization.prohibited_actions", "must contain four boundaries")
    if not any("Inspect the ZIP structure" in item for item in permitted):
        _fail("authorization.permitted_actions", "must authorize static ZIP inspection")
    if not any("Do not load or execute SCRFD" in item for item in prohibited):
        _fail("authorization.prohibited_actions", "must prohibit model execution")
    return _parse_time(authorization["recorded_at"], "authorization.recorded_at")


def _validate_curl(value: Any) -> None:
    location = "provenance.acquisition_tool"
    record = _mapping(value, location)
    _strict_keys(record, location, {"path", "sha256", "size_bytes", "version"})
    path = _identity(
        {key: record[key] for key in ("path", "sha256", "size_bytes")},
        location,
        expected_path=CURL_PATH,
        expected_sha256=CURL_SHA256,
        expected_size=280800,
        maximum_size=2 * 1024 * 1024,
    )
    if record["version"] != "curl 7.88.1":
        _fail(f"{location}.version", "must equal curl 7.88.1")
    try:
        result = subprocess.run(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=10,
            env={"LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        _fail(location, f"cannot observe pinned curl version: {error}")
    try:
        first_line = result.stdout.decode("utf-8").splitlines()[0]
    except (UnicodeDecodeError, IndexError) as error:
        _fail(location, f"cannot decode curl version: {error}")
    if result.returncode != 0 or not first_line.startswith("curl 7.88.1"):
        _fail(location, "live curl version differs")


def _validate_release_api(value: Any, authorized_at: datetime) -> tuple[datetime, str]:
    location = "provenance.release_api"
    record = _mapping(value, location)
    _strict_keys(
        record,
        location,
        {
            "request_url",
            "retrieved_at_start",
            "retrieved_at_end",
            "response_headers",
            "response_json",
            "release_id",
            "tag_name",
            "asset_id",
            "asset_name",
            "asset_size_bytes",
            "asset_created_at",
            "asset_updated_at",
            "asset_url",
        },
    )
    if record["request_url"] != RELEASE_API_URL or record["asset_url"] != ASSET_URL:
        _fail(location, "official GitHub request or asset URL differs")
    started = _parse_time(record["retrieved_at_start"], f"{location}.retrieved_at_start")
    ended = _parse_time(record["retrieved_at_end"], f"{location}.retrieved_at_end")
    if not authorized_at <= started <= ended:
        _fail(location, "release API retrieval does not follow authorization")
    headers_path = _identity(
        record["response_headers"],
        f"{location}.response_headers",
        expected_path=RELEASE_HEADERS_PATH,
        expected_sha256=RELEASE_HEADERS_SHA256,
        expected_size=1320,
        maximum_size=32 * 1024,
    )
    headers = _read_headers(headers_path, f"{location}.response_headers")
    if headers.get("content-type") != "application/json; charset=utf-8":
        _fail(f"{location}.response_headers", "content type differs")
    response_path = _identity(
        record["response_json"],
        f"{location}.response_json",
        expected_path=RELEASE_JSON_PATH,
        expected_sha256=RELEASE_JSON_SHA256,
        expected_size=14330,
        maximum_size=MAX_JSON_BYTES,
    )
    if headers.get("content-length") != str(response_path.stat().st_size):
        _fail(f"{location}.response_headers", "content length differs")
    try:
        response_time = parsedate_to_datetime(headers["date"])
    except (KeyError, TypeError, ValueError) as error:
        _fail(f"{location}.response_headers.date", f"cannot parse: {error}")
    if not started <= response_time <= ended:
        _fail(f"{location}.response_headers.date", "is outside retrieval interval")
    release = _load_json(response_path, "release_api_response")
    if (
        release.get("id") != record["release_id"]
        or release.get("tag_name") != record["tag_name"]
        or release.get("target_commitish") != "master"
    ):
        _fail("release_api_response", "release identity differs")
    if record["release_id"] != 97756821 or record["tag_name"] != "v0.7":
        _fail(location, "frozen release identity differs")
    assets = [
        _mapping(asset, f"release_api_response.assets[{index}]")
        for index, asset in enumerate(_sequence(release.get("assets"), "release_api_response.assets"))
        if isinstance(asset, dict) and asset.get("id") == record["asset_id"]
    ]
    if len(assets) != 1:
        _fail("release_api_response.assets", "must contain one exact buffalo_l asset")
    asset = assets[0]
    expected_asset = {
        "id": 101975360,
        "name": "buffalo_l.zip",
        "size": ARCHIVE_SIZE,
        "content_type": "application/zip",
        "state": "uploaded",
        "created_at": "2023-04-02T07:42:20Z",
        "updated_at": "2023-04-02T07:43:08Z",
        "browser_download_url": ASSET_URL,
        "digest": None,
    }
    for key, expected in expected_asset.items():
        if asset.get(key) != expected:
            _fail(f"release_api_response.asset.{key}", "differs")
    if (
        record["asset_id"] != expected_asset["id"]
        or record["asset_name"] != expected_asset["name"]
        or record["asset_size_bytes"] != expected_asset["size"]
        or record["asset_created_at"] != expected_asset["created_at"]
        or record["asset_updated_at"] != expected_asset["updated_at"]
    ):
        _fail(location, "manifest asset fields differ from official response")
    return ended, record["asset_url"]


def _validate_transfer(
    value: Any,
    *,
    api_ended: datetime,
    expected_asset_url: str,
) -> datetime:
    location = "provenance.fresh_asset_transfer"
    record = _mapping(value, location)
    _strict_keys(
        record,
        location,
        {
            "request_url",
            "retrieved_at_start",
            "retrieved_at_end",
            "final_response_headers",
            "observed_size_bytes",
            "observed_sha256",
            "observed_md5",
            "server_content_md5_base64",
            "byte_comparison_to_local_archive",
            "raw_redirect_request_trace_preserved",
            "temporary_duplicate_removed_after_comparison",
        },
    )
    if record["request_url"] != expected_asset_url:
        _fail(f"{location}.request_url", "differs from official asset metadata")
    started = _parse_time(record["retrieved_at_start"], f"{location}.retrieved_at_start")
    ended = _parse_time(record["retrieved_at_end"], f"{location}.retrieved_at_end")
    if not api_ended <= started <= ended:
        _fail(location, "asset transfer does not follow release API retrieval")
    headers_path = _identity(
        record["final_response_headers"],
        f"{location}.final_response_headers",
        expected_path=ASSET_HEADERS_PATH,
        expected_sha256=ASSET_HEADERS_SHA256,
        expected_size=804,
        maximum_size=32 * 1024,
    )
    headers = _read_headers(headers_path, f"{location}.final_response_headers")
    if headers.get("content-length") != str(ARCHIVE_SIZE):
        _fail(f"{location}.final_response_headers", "content length differs")
    if headers.get("content-type") != "application/octet-stream":
        _fail(f"{location}.final_response_headers", "content type differs")
    try:
        response_time = parsedate_to_datetime(headers["date"])
    except (KeyError, TypeError, ValueError) as error:
        _fail(f"{location}.final_response_headers.date", f"cannot parse: {error}")
    if not started <= response_time <= ended:
        _fail(f"{location}.final_response_headers.date", "is outside transfer interval")
    if (
        record["observed_size_bytes"] != ARCHIVE_SIZE
        or record["observed_sha256"] != ARCHIVE_SHA256
        or record["observed_md5"] != ARCHIVE_MD5
        or record["byte_comparison_to_local_archive"] != "CMP_EXIT_0"
        or record["raw_redirect_request_trace_preserved"] is not False
        or record["temporary_duplicate_removed_after_comparison"] is not True
    ):
        _fail(location, "bounded transfer observation differs")
    encoded_md5 = headers.get("x-ms-blob-content-md5")
    if encoded_md5 != record["server_content_md5_base64"]:
        _fail(f"{location}.server_content_md5_base64", "differs from final headers")
    try:
        decoded_md5 = base64.b64decode(encoded_md5, validate=True).hex()
    except (TypeError, ValueError) as error:
        _fail(f"{location}.server_content_md5_base64", f"cannot decode: {error}")
    if decoded_md5 != ARCHIVE_MD5:
        _fail(f"{location}.server_content_md5_base64", "does not identify the archive")
    return ended


def _validate_archive(value: Any) -> dict[str, dict[str, Any]]:
    location = "provenance.local_archive"
    record = _mapping(value, location)
    _strict_keys(
        record,
        location,
        {"path", "sha256", "md5", "size_bytes", "mtime_ns", "members"},
    )
    if not isinstance(record["path"], str) or Path(record["path"]) != LOCAL_ARCHIVE_PATH:
        _fail(f"{location}.path", f"must equal {LOCAL_ARCHIVE_PATH}")
    try:
        observed = LOCAL_ARCHIVE_PATH.stat()
    except OSError as error:
        _fail(location, f"cannot stat archive: {error}")
    if (
        record["sha256"] != ARCHIVE_SHA256
        or record["md5"] != ARCHIVE_MD5
        or record["size_bytes"] != ARCHIVE_SIZE
        or record["mtime_ns"] != observed.st_mtime_ns
        or observed.st_size != ARCHIVE_SIZE
        or _sha256(LOCAL_ARCHIVE_PATH) != ARCHIVE_SHA256
        or _md5(LOCAL_ARCHIVE_PATH) != ARCHIVE_MD5
    ):
        _fail(location, "local archive identity differs")
    members = _sequence(record["members"], f"{location}.members")
    if members != EXPECTED_MEMBERS:
        _fail(f"{location}.members", "frozen member records differ")
    observed_members: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(LOCAL_ARCHIVE_PATH) as archive:
            infos = archive.infolist()
            if len(infos) != len(EXPECTED_MEMBERS):
                _fail(location, "archive member count differs")
            seen: set[str] = set()
            for index, info in enumerate(infos):
                member_location = f"{location}.members[{index}]"
                pure = PurePosixPath(info.filename)
                if (
                    info.filename in seen
                    or info.filename != pure.as_posix()
                    or pure.is_absolute()
                    or ".." in pure.parts
                    or "\\" in info.filename
                    or info.is_dir()
                ):
                    _fail(member_location, "unsafe, duplicate, or non-file member")
                seen.add(info.filename)
                mode = (info.external_attr >> 16) & 0xFFFF
                if not stat.S_ISREG(mode) or info.flag_bits & 0x1:
                    _fail(member_location, "member is not an unencrypted regular file")
                digest = hashlib.sha256()
                with archive.open(info, "r") as member:
                    for block in iter(lambda: member.read(1024 * 1024), b""):
                        digest.update(block)
                observed_members.append(
                    {
                        "path": info.filename,
                        "size_bytes": info.file_size,
                        "compressed_size_bytes": info.compress_size,
                        "crc32": f"{info.CRC:08x}",
                        "sha256": digest.hexdigest(),
                        "unix_mode": mode,
                    }
                )
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        _fail(location, f"cannot inspect archive safely: {error}")
    if observed_members != EXPECTED_MEMBERS:
        _fail(f"{location}.members", "observed archive inventory differs")
    return {member["path"]: member for member in observed_members}


def _validate_registered_detector(
    value: Any,
    *,
    registry_path: Path,
    members: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    location = "provenance.registered_detector"
    record = _mapping(value, location)
    _strict_keys(
        record,
        location,
        {
            "resource_registry",
            "resource_id",
            "archive_member_path",
            "model_path",
            "model_sha256",
            "model_size_bytes",
            "model_mtime_ns",
            "required_state",
            "required_third_party_rights_status",
            "required_approved_scopes",
        },
    )
    if registry_path.resolve(strict=True) != REGISTRY_PATH.resolve(strict=True):
        _fail("registry", f"must equal {REGISTRY_PATH}")
    _identity(
        record["resource_registry"],
        f"{location}.resource_registry",
        expected_path=REGISTRY_PATH,
        expected_sha256=REGISTRY_SHA256,
        expected_size=REGISTRY_SIZE,
        maximum_size=MAX_JSON_BYTES,
    )
    if (
        record["resource_id"] != RESOURCE_ID
        or record["archive_member_path"] != "det_10g.onnx"
        or record["model_path"] != str(LOCAL_DETECTOR_PATH)
        or record["model_sha256"] != DETECTOR_SHA256
        or record["model_size_bytes"] != DETECTOR_SIZE
        or record["required_state"] != REQUIRED_STATE
        or record["required_third_party_rights_status"] != "RESEARCH_NOTICE_ONLY"
        or record["required_approved_scopes"] != REQUIRED_APPROVED_SCOPES
    ):
        _fail(location, "frozen detector boundary differs")
    try:
        detector_stat = LOCAL_DETECTOR_PATH.stat()
    except OSError as error:
        _fail(location, f"cannot stat detector: {error}")
    if (
        record["model_mtime_ns"] != detector_stat.st_mtime_ns
        or detector_stat.st_size != DETECTOR_SIZE
        or _sha256(LOCAL_DETECTOR_PATH) != DETECTOR_SHA256
    ):
        _fail(location, "local detector identity differs")
    member = members.get("det_10g.onnx")
    if member is None or member["size_bytes"] != DETECTOR_SIZE or member["sha256"] != DETECTOR_SHA256:
        _fail(location, "archive detector member identity differs")
    try:
        with zipfile.ZipFile(LOCAL_ARCHIVE_PATH) as archive, archive.open(
            "det_10g.onnx", "r"
        ) as archived, LOCAL_DETECTOR_PATH.open("rb") as extracted:
            while True:
                archived_block = archived.read(1024 * 1024)
                extracted_block = extracted.read(1024 * 1024)
                if archived_block != extracted_block:
                    _fail(location, "archive detector bytes differ from registered model")
                if not archived_block:
                    break
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        _fail(location, f"cannot compare detector bytes: {error}")
    registry = _load_json(REGISTRY_PATH, "registry")
    resources = _sequence(registry.get("resources"), "registry.resources")
    matches = [
        _mapping(resource, f"registry.resources[{index}]")
        for index, resource in enumerate(resources)
        if isinstance(resource, dict) and resource.get("resource_id") == RESOURCE_ID
    ]
    if len(matches) != 1:
        _fail("registry.resources", f"expected exactly one {RESOURCE_ID}")
    resource = matches[0]
    # This release-provenance child never promotes the detector. Its current
    # APPROVED_LOCAL_RESEARCH_USE state comes only from the owner use-permission
    # receipt; the third-party rights position stays RESEARCH_NOTICE_ONLY.
    owner = resource.get("owner_authorization")
    if (
        resource.get("state") != REQUIRED_STATE
        or resource.get("third_party_rights_status") != "RESEARCH_NOTICE_ONLY"
        or resource.get("approved_scopes") != REQUIRED_APPROVED_SCOPES
        or not isinstance(owner, dict)
        or owner.get("decision") != "APPROVED_LOCAL_NONCOMMERCIAL"
        or owner.get("redistribution_allowed") is not False
        or owner.get("evidence_artifact_path") != str(USE_PERMISSION_RECEIPT)
        or not set(REQUIRED_APPROVED_SCOPES) <= set(owner.get("scopes") or [])
    ):
        _fail(
            "registry.resource",
            "SCRFD must be owner-approved for local research use with a factual "
            "RESEARCH_NOTICE_ONLY rights status and no redistribution",
        )
    model_artifacts = [
        artifact
        for artifact in _sequence(resource.get("artifacts"), "registry.resource.artifacts")
        if isinstance(artifact, dict) and artifact.get("role") == "MODEL"
    ]
    expected_artifact = {
        "role": "MODEL",
        "path": str(LOCAL_DETECTOR_PATH),
        "sha256": DETECTOR_SHA256,
        "size_bytes": DETECTOR_SIZE,
        "mtime_ns": detector_stat.st_mtime_ns,
    }
    if len(model_artifacts) != 1 or model_artifacts[0] != expected_artifact:
        _fail("registry.resource.artifacts", "registered detector artifact differs")
    return resource


def _validate_documentation(value: Any, resource: dict[str, Any]) -> None:
    location = "provenance.upstream_documentation"
    record = _mapping(value, location)
    _strict_keys(record, location, {"path", "sha256", "size_bytes", "required_text_fragments"})
    path = _identity(
        {key: record[key] for key in ("path", "sha256", "size_bytes")},
        location,
        expected_path=UPSTREAM_DOCUMENTATION_PATH,
        expected_sha256=UPSTREAM_DOCUMENTATION_SHA256,
        expected_size=10734,
        maximum_size=2 * 1024 * 1024,
    )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        _fail(location, f"cannot read documentation: {error}")
    fragments = _sequence(record["required_text_fragments"], f"{location}.required_text_fragments")
    if len(fragments) != 3 or not all(isinstance(item, str) and item in text for item in fragments):
        _fail(f"{location}.required_text_fragments", "required source text is absent")
    source = _mapping(resource.get("source"), "registry.resource.source")
    evidence = _sequence(
        source.get("evidence_artifacts"),
        "registry.resource.source.evidence_artifacts",
    )
    if str(path) not in evidence:
        _fail(location, "documentation is not source evidence for the registered resource")


def validate_provenance(
    provenance_path: Path = PROVENANCE_PATH,
    registry_path: Path = REGISTRY_PATH,
) -> dict[str, Any]:
    provenance_path = provenance_path.resolve(strict=True)
    registry_path = registry_path.resolve(strict=True)
    provenance = _load_json(provenance_path, "provenance")
    _strict_keys(
        provenance,
        "provenance",
        {
            "schema_version",
            "provenance_id",
            "recorded_at",
            "scope",
            "owner_authorization",
            "acquisition_tool",
            "release_api",
            "fresh_asset_transfer",
            "local_archive",
            "registered_detector",
            "upstream_documentation",
            "claims",
            "limitations",
        },
    )
    if provenance["schema_version"] != "1.0.0":
        _fail("provenance.schema_version", "must equal 1.0.0")
    if provenance["provenance_id"] != "insightface-buffalo-l-release-provenance-v1":
        _fail("provenance.provenance_id", "differs")
    if provenance["scope"] != "STATIC_RELEASE_ASSET_PROVENANCE_ONLY":
        _fail("provenance.scope", "differs")

    authorized_at = _validate_authorization(provenance["owner_authorization"])
    _validate_curl(provenance["acquisition_tool"])
    api_ended, asset_url = _validate_release_api(
        provenance["release_api"], authorized_at
    )
    transfer_ended = _validate_transfer(
        provenance["fresh_asset_transfer"],
        api_ended=api_ended,
        expected_asset_url=asset_url,
    )
    recorded_at = _parse_time(provenance["recorded_at"], "provenance.recorded_at")
    if recorded_at < transfer_ended:
        _fail("provenance.recorded_at", "must follow the completed transfer")
    if recorded_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        _fail("provenance.recorded_at", "is implausibly in the future")

    members = _validate_archive(provenance["local_archive"])
    resource = _validate_registered_detector(
        provenance["registered_detector"],
        registry_path=registry_path,
        members=members,
    )
    _validate_documentation(provenance["upstream_documentation"], resource)
    if provenance["claims"] != EXPECTED_CLAIMS:
        _fail("provenance.claims", "contains an unsupported claim")
    if provenance["limitations"] != EXPECTED_LIMITATIONS:
        _fail("provenance.limitations", "differs from the frozen evidence boundary")

    return {
        "provenance_id": provenance["provenance_id"],
        "release_tag": provenance["release_api"]["tag_name"],
        "release_asset_id": provenance["release_api"]["asset_id"],
        "archive_size_bytes": provenance["local_archive"]["size_bytes"],
        "archive_members": len(members),
        "fresh_transfer_equals_local_archive": provenance["claims"][
            "fresh_transfer_equals_local_archive"
        ],
        "detector_member_equals_registered_model": provenance["claims"][
            "detector_member_equals_registered_model"
        ],
        "resource_id": resource["resource_id"],
        "resource_state": resource["state"],
        "third_party_rights_status": resource["third_party_rights_status"],
        "approved_scopes": resource["approved_scopes"],
        "model_rights_closed": provenance["claims"]["model_rights_closed"],
        "runtime_validated": provenance["claims"]["runtime_validated"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("provenance", nargs="?", type=Path, default=PROVENANCE_PATH)
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    args = parser.parse_args()
    try:
        summary = validate_provenance(args.provenance, args.registry)
    except (OSError, UnicodeDecodeError, SCRFDReleaseProvenanceValidationError) as error:
        print(f"SCRFD_RELEASE_PROVENANCE_INVALID: {error}", file=sys.stderr)
        return 1
    print(
        "SCRFD_RELEASE_PROVENANCE_VALID "
        f"id={summary['provenance_id']} tag={summary['release_tag']} "
        f"asset_id={summary['release_asset_id']} size={summary['archive_size_bytes']} "
        f"members={summary['archive_members']} "
        f"transfer_equal={str(summary['fresh_transfer_equals_local_archive']).lower()} "
        f"detector_equal={str(summary['detector_member_equals_registered_model']).lower()} "
        f"resource_state={summary['resource_state']} "
        f"rights={summary['third_party_rights_status']} "
        f"approved_scopes={len(summary['approved_scopes'])} "
        f"runtime_validated={str(summary['runtime_validated']).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
