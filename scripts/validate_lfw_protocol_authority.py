from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_PATH = (
    PROJECT_ROOT
    / "resources"
    / "manifests"
    / "lfw-official-protocol-authority-v1.json"
)
REGISTRY_PATH = PROJECT_ROOT / "resources" / "resource-registry.json"
PAIR_MANIFEST_PATH = (
    PROJECT_ROOT / "resources" / "manifests" / "lfw-local-pairs-v1.jsonl"
)
OWNER_AUTHORIZATION_PATH = (
    PROJECT_ROOT
    / "resources"
    / "authorizations"
    / "qualsight-lfw-protocol-evidence-review-2026-08-17.json"
)
ORIGINAL_PROTOCOL_AUTHORIZATION_PATH = (
    PROJECT_ROOT
    / "resources"
    / "authorizations"
    / "qualsight-lfw-original-protocol-evidence-review-2026-08-17.json"
)
ORIGINAL_2007_PATH = (
    PROJECT_ROOT / "resources" / "upstream" / "lfw" / "lfw-2007.pdf"
)
ORIGINAL_2007_HEADERS_PATH = ORIGINAL_2007_PATH.with_suffix(".headers")
HUANG_2008_PATH = (
    PROJECT_ROOT / "resources" / "upstream" / "lfw" / "Huang_eccv2008-lfw.pdf"
)
HUANG_2008_HEADERS_PATH = HUANG_2008_PATH.with_suffix(".headers")
UPDATES_2014_PATH = (
    PROJECT_ROOT / "resources" / "upstream" / "lfw" / "UM-CS-2014-003.pdf"
)
UPDATES_2014_HEADERS_PATH = UPDATES_2014_PATH.with_suffix(".headers")
CURL_PATH = Path("/usr/bin/curl")
PDFTOTEXT_PATH = Path("/usr/bin/pdftotext")
DATASET_RESOURCE_ID = "lfw-funneled-local-candidate"
MATCHER_RESOURCE_IDS = [
    "adaface-ir101-local-candidate",
    "elasticface-arc-r100-local-candidate",
    "transface-s-glint360k-local-candidate",
]
OWNER_SCOPE = "ACQUIRE_LFW_OFFICIAL_PROTOCOL_DOCUMENTS_FOR_EVIDENCE_REVIEW"
ORIGINAL_PROTOCOL_SCOPE = (
    "ACQUIRE_LFW_ORIGINAL_2007_PROTOCOL_DOCUMENT_FOR_EVIDENCE_REVIEW"
)
TRANSPORT_PROVENANCE_STATUS = (
    "PROJECT_RECORDED_URLS_AND_RESPONSE_HEADERS_NO_VERBOSE_REQUEST_TRACES"
)
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_LFW_THREE_MATCHER_TWO_PREPROCESSING_V1"
SELECTED_CATEGORY = "UNRESTRICTED_WITH_LABELED_OUTSIDE_DATA"
FINDING_STATUS = (
    "OWNER_AUTHORIZED_LOCAL_RESEARCH_USE_MATCHER_TRAINING_EXCLUSION_"
    "RECORDED_AS_PUBLISHED_CLAIM"
)
REMAINING_EVIDENCE = [
    "EACH_MATCHER_TRAINING_SET_EXCLUSION_OF_LFW_TEST_IDENTITIES_IS_A_PUBLISHED_CLAIM_NOT_QUALSIGHT_VERIFIED",
    "FROZEN_NO_DOWNLOAD_NO_RANDOM_WEIGHT_RUNTIME",
    "SIX_IMMUTABLE_CONDITION_SPECIFIC_RUNS",
]
USE_PERMISSION_RECEIPT = (
    PROJECT_ROOT
    / "resources"
    / "authorizations"
    / "qualsight-owner-use-permission-granted-2026-08-17.json"
)
LOCAL_RESEARCH_STATE = "APPROVED_LOCAL_RESEARCH_USE"
LIMITATIONS = [
    "The three official UMass documents define LFW evaluation and reporting protocols; they are not a license or terms grant for the underlying image bytes.",
    "Raw verbose request traces were not preserved; the retrieval URLs are project-recorded and the response headers preserve only the server responses.",
    "The most permissive labeled-outside-data category is selected conservatively because the three planned pretrained face matchers name external face-training datasets.",
    "Complete official-protocol conformance is not claimed because exclusion of every LFW test identity from every matcher training set is not established.",
    "The local archive and pair bytes retain their existing missing-acquisition and missing-rights limitations.",
    "No LFW image was decoded or displayed and no detector, model, matcher, OFIQ runtime, or scientific Run was executed.",
]
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_PDF_BYTES = 2 * 1024 * 1024


class LFWProtocolAuthorityValidationError(ValueError):
    """Raised when the official LFW protocol evidence is inconsistent."""


def _fail(location: str, message: str) -> None:
    raise LFWProtocolAuthorityValidationError(f"{location}: {message}")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise LFWProtocolAuthorityValidationError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(location, "must be an object")
    return value


def _sequence(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(location, "must be an array")
    return value


def _string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(location, "must be a nonempty string without surrounding whitespace")
    return value


def _integer(value: Any, location: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(location, "must be an integer")
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
        text = path.read_text(encoding="utf-8")
        value = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail(location, f"cannot read strict UTF-8 JSON from {path}: {error}")
    return _mapping(value, location)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
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
    maximum_size: int,
) -> Path:
    record = _mapping(value, location)
    _strict_keys(record, location, {"path", "sha256", "size_bytes"})
    path = Path(_string(record["path"], f"{location}.path"))
    if not path.is_absolute() or path != expected_path:
        _fail(f"{location}.path", f"must equal {expected_path}")
    digest = _string(record["sha256"], f"{location}.sha256")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        _fail(f"{location}.sha256", "must be a lowercase SHA-256")
    size = _integer(record["size_bytes"], f"{location}.size_bytes")
    if size < 1 or size > maximum_size:
        _fail(f"{location}.size_bytes", "is outside the accepted range")
    try:
        observed_size = path.stat().st_size
    except OSError as error:
        _fail(location, f"cannot stat {path}: {error}")
    if observed_size != size:
        _fail(location, f"size differs for {path}")
    if _sha256(path) != digest:
        _fail(location, f"SHA-256 differs for {path}")
    return path


def _parse_time(value: Any, location: str) -> datetime:
    text = _string(value, location)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        _fail(location, f"must be an ISO-8601 timestamp: {error}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(location, "must include an explicit UTC offset")
    return parsed


def _read_bounded(path: Path, location: str, maximum_size: int) -> bytes:
    try:
        size = path.stat().st_size
        if size < 1 or size > maximum_size:
            _fail(location, f"file size {size} is outside the accepted range")
        return path.read_bytes()
    except OSError as error:
        _fail(location, f"cannot read {path}: {error}")


def _validate_owner_authorization(
    value: Any,
    *,
    location: str,
    expected_path: Path,
    expected_authorization_id: str,
    expected_scope: str,
) -> tuple[datetime, Path]:
    record = _mapping(value, location)
    _strict_keys(
        record,
        location,
        {"path", "sha256", "size_bytes", "decision", "scope"},
    )
    identity = {key: record[key] for key in ("path", "sha256", "size_bytes")}
    path = _identity(
        identity,
        location,
        expected_path=expected_path,
        maximum_size=MAX_JSON_BYTES,
    )
    if record["decision"] != "APPROVED_EVIDENCE_ACQUISITION":
        _fail(f"{location}.decision", "must remain evidence-acquisition only")
    if record["scope"] != expected_scope:
        _fail(f"{location}.scope", f"must equal {expected_scope}")

    authorization_location = f"{location}.document"
    authorization = _load_json(path, authorization_location)
    _strict_keys(
        authorization,
        authorization_location,
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
        _fail(f"{authorization_location}.schema_version", "must equal 1.0.0")
    if authorization["authorization_id"] != expected_authorization_id:
        _fail(f"{authorization_location}.authorization_id", "differs")
    if authorization["authority"] != "QualSight project owner (interactive user)":
        _fail(f"{authorization_location}.authority", "differs")
    if authorization["decision"] != record["decision"]:
        _fail(f"{authorization_location}.decision", "differs from bound decision")
    if authorization["resource_id"] != DATASET_RESOURCE_ID:
        _fail(f"{authorization_location}.resource_id", "differs")
    if authorization["scope"] != record["scope"]:
        _fail(f"{authorization_location}.scope", "differs from bound scope")
    if authorization["redistribution_allowed"] is not False:
        _fail(f"{authorization_location}.redistribution_allowed", "must be false")
    permitted = _sequence(
        authorization["permitted_actions"],
        f"{authorization_location}.permitted_actions",
    )
    prohibited = _sequence(
        authorization["prohibited_actions"],
        f"{authorization_location}.prohibited_actions",
    )
    if len(permitted) != 4 or not all(isinstance(item, str) and item for item in permitted):
        _fail(
            f"{authorization_location}.permitted_actions",
            "must contain four real actions",
        )
    if len(prohibited) != 4 or not all(isinstance(item, str) and item for item in prohibited):
        _fail(
            f"{authorization_location}.prohibited_actions",
            "must contain four boundaries",
        )
    if not any("pdftotext" in item for item in permitted):
        _fail(
            f"{authorization_location}.permitted_actions",
            "must authorize static PDF text extraction",
        )
    if not any("Do not decode or display an LFW image." == item for item in prohibited):
        _fail(
            f"{authorization_location}.prohibited_actions",
            "must forbid LFW image decoding",
        )
    return _parse_time(
        authorization["recorded_at"], f"{authorization_location}.recorded_at"
    ), path


def _validate_tool(
    value: Any,
    location: str,
    *,
    expected_path: Path,
    expected_version: str,
    version_arguments: list[str],
) -> Path:
    record = _mapping(value, location)
    _strict_keys(record, location, {"path", "sha256", "size_bytes", "version"})
    path = _identity(
        {key: record[key] for key in ("path", "sha256", "size_bytes")},
        location,
        expected_path=expected_path,
        maximum_size=2 * 1024 * 1024,
    )
    if record["version"] != expected_version:
        _fail(f"{location}.version", f"must equal {expected_version}")
    try:
        result = subprocess.run(
            [str(path), *version_arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=10,
            env={"LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        _fail(location, f"cannot observe pinned tool version: {error}")
    first_line = result.stdout.decode("utf-8", errors="strict").splitlines()[0]
    if result.returncode != 0 or not first_line.startswith(expected_version):
        _fail(location, "live tool version differs")
    return path


def _parse_headers(path: Path, location: str) -> dict[str, str]:
    raw = _read_bounded(path, location, 16 * 1024)
    try:
        text = raw.decode("iso-8859-1")
    except UnicodeDecodeError as error:
        _fail(location, f"headers are not ISO-8859-1: {error}")
    blocks = [block for block in text.replace("\r\n", "\n").split("\n\n") if block.strip()]
    if len(blocks) != 1:
        _fail(location, "must contain exactly one HTTP response block")
    lines = blocks[0].splitlines()
    if lines[0] != "HTTP/1.1 200 OK":
        _fail(location, "status line must be HTTP/1.1 200 OK")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            _fail(location, f"malformed header line: {line}")
        name, content = line.split(":", 1)
        key = name.strip().lower()
        value = content.strip()
        if not key or not value or key in headers:
            _fail(location, f"empty or duplicate header: {name}")
        headers[key] = value
    required = {
        "date",
        "last-modified",
        "etag",
        "content-length",
        "content-type",
    }
    if not required.issubset(headers):
        _fail(location, "required HTTP identity headers are absent")
    return headers


def _normalize_text(raw: bytes, location: str) -> str:
    if len(raw) < 1 or len(raw) > MAX_TEXT_BYTES:
        _fail(location, "extracted text size is outside the accepted range")
    try:
        return " ".join(raw.decode("utf-8").split()).casefold()
    except UnicodeDecodeError as error:
        _fail(location, f"extracted text is not UTF-8: {error}")


def _extract_pdf_text(
    tool: Path,
    pdf_path: Path,
    *,
    arguments: list[str],
    timeout_seconds: int,
    location: str,
) -> str:
    argv = [str(tool), *[str(pdf_path) if item == "{pdf}" else item for item in arguments]]
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
            cwd=PROJECT_ROOT,
            env={"LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        _fail(location, f"pinned PDF text extraction failed: {error}")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        _fail(location, f"pinned PDF text extraction exited {result.returncode}: {detail}")
    if result.stderr:
        _fail(location, "pinned PDF text extraction emitted stderr")
    return _normalize_text(result.stdout, location)


def _validate_documents(
    value: Any,
    *,
    general_authorized_at: datetime,
    original_authorized_at: datetime,
    text_tool: Path,
    text_arguments: list[str],
    timeout_seconds: int,
) -> list[datetime]:
    documents = _sequence(value, "authority.documents")
    expected = [
        {
            "id": "HUANG_ET_AL_LFW_2007_REPORT",
            "title": "Labeled Faces in the Wild: A Database for Studying Face Recognition in Unconstrained Environments",
            "authority": "University of Massachusetts Amherst authors",
            "url": "https://people.cs.umass.edu/~elm/papers/lfw.pdf",
            "pdf": ORIGINAL_2007_PATH,
            "headers": ORIGINAL_2007_HEADERS_PATH,
            "authorized_at": original_authorized_at,
        },
        {
            "id": "HUANG_ET_AL_ECCV_2008_LFW",
            "title": "Labeled Faces in the Wild: A Database for Studying Face Recognition in Unconstrained Environments",
            "authority": "University of Massachusetts Amherst authors",
            "url": "https://people.cs.umass.edu/~elm/papers/Huang_eccv2008-lfw.pdf",
            "pdf": HUANG_2008_PATH,
            "headers": HUANG_2008_HEADERS_PATH,
            "authorized_at": general_authorized_at,
        },
        {
            "id": "HUANG_LEARNED_MILLER_UM_CS_2014_003",
            "title": "Labeled Faces in the Wild: Updates and New Reporting Procedures",
            "authority": "University of Massachusetts Amherst technical report",
            "url": "https://web.cs.umass.edu/publication/docs/2014/UM-CS-2014-003.pdf",
            "pdf": UPDATES_2014_PATH,
            "headers": UPDATES_2014_HEADERS_PATH,
            "authorized_at": general_authorized_at,
        },
    ]
    if len(documents) != len(expected):
        _fail("authority.documents", "must contain exactly three official documents")
    retrieval_ends: list[datetime] = []
    for index, (raw_document, contract) in enumerate(zip(documents, expected, strict=True)):
        location = f"authority.documents[{index}]"
        document = _mapping(raw_document, location)
        _strict_keys(
            document,
            location,
            {
                "document_id",
                "title",
                "authority",
                "url",
                "retrieved_at_start",
                "retrieved_at_end",
                "http",
                "response_headers",
                "pdf",
                "required_text_fragments",
            },
        )
        for field in ("document_id", "title", "authority", "url"):
            expected_key = "id" if field == "document_id" else field
            if document[field] != contract[expected_key]:
                _fail(f"{location}.{field}", "differs from the frozen official source")
        started = _parse_time(document["retrieved_at_start"], f"{location}.retrieved_at_start")
        ended = _parse_time(document["retrieved_at_end"], f"{location}.retrieved_at_end")
        if not contract["authorized_at"] <= started <= ended:
            _fail(location, "retrieval does not follow authorization chronology")
        retrieval_ends.append(ended)

        header_path = _identity(
            document["response_headers"],
            f"{location}.response_headers",
            expected_path=contract["headers"],
            maximum_size=16 * 1024,
        )
        headers = _parse_headers(header_path, f"{location}.response_headers")
        http = _mapping(document["http"], f"{location}.http")
        _strict_keys(
            http,
            f"{location}.http",
            {"status", "content_type", "content_length", "etag", "last_modified"},
        )
        if http["status"] != 200 or headers["content-type"] != http["content_type"]:
            _fail(f"{location}.http", "status or content type differs")
        if http["content_type"] != "application/pdf":
            _fail(f"{location}.http.content_type", "must equal application/pdf")
        if headers["etag"] != http["etag"] or headers["last-modified"] != http["last_modified"]:
            _fail(f"{location}.http", "ETag or Last-Modified differs")
        content_length = _integer(http["content_length"], f"{location}.http.content_length")
        if headers["content-length"] != str(content_length):
            _fail(f"{location}.http.content_length", "differs from response headers")
        try:
            response_time = parsedate_to_datetime(headers["date"])
        except (TypeError, ValueError) as error:
            _fail(f"{location}.response_headers.date", f"cannot parse: {error}")
        if response_time.tzinfo is None:
            _fail(f"{location}.response_headers.date", "must be timezone-aware")
        if not started <= response_time <= ended:
            _fail(f"{location}.response_headers.date", "is outside the retrieval interval")

        pdf_path = _identity(
            document["pdf"],
            f"{location}.pdf",
            expected_path=contract["pdf"],
            maximum_size=MAX_PDF_BYTES,
        )
        if pdf_path.stat().st_size != content_length:
            _fail(f"{location}.pdf", "size differs from HTTP Content-Length")
        pdf_bytes = _read_bounded(pdf_path, f"{location}.pdf", MAX_PDF_BYTES)
        if not pdf_bytes.startswith(b"%PDF-") or b"%%EOF" not in pdf_bytes[-1024:]:
            _fail(f"{location}.pdf", "does not have a bounded PDF signature and trailer")
        fragments = _sequence(
            document["required_text_fragments"], f"{location}.required_text_fragments"
        )
        if len(fragments) < 3 or not all(isinstance(item, str) and item for item in fragments):
            _fail(f"{location}.required_text_fragments", "must contain real text claims")
        normalized = _extract_pdf_text(
            text_tool,
            pdf_path,
            arguments=text_arguments,
            timeout_seconds=timeout_seconds,
            location=f"{location}.pdf_text",
        )
        for fragment_index, fragment in enumerate(fragments):
            if fragment.casefold() not in normalized:
                _fail(
                    f"{location}.required_text_fragments[{fragment_index}]",
                    "is absent from pinned extracted PDF text",
                )
    return retrieval_ends


def _find_resource(resources: list[Any], resource_id: str) -> dict[str, Any]:
    matches = [
        _mapping(resource, f"registry.resources[{index}]")
        for index, resource in enumerate(resources)
        if isinstance(resource, dict) and resource.get("resource_id") == resource_id
    ]
    if len(matches) != 1:
        _fail("registry.resources", f"expected exactly one {resource_id}")
    return matches[0]


def _validate_resource_state_boundary(
    resource: dict[str, Any],
    location: str,
    required_state: str,
    required_scopes: list[Any],
) -> None:
    """The evidence child never promotes a resource; only an owner receipt can.

    A BLOCKED boundary must stay BLOCKED with no effective scope. An
    APPROVED_LOCAL_RESEARCH_USE boundary must be backed by the exact owner
    use-permission receipt, must never permit redistribution, and its effective
    scopes must be a subset of the owner-granted scopes.
    """
    if resource.get("state") != required_state:
        _fail(f"{location}.state", f"must equal the boundary state {required_state}")
    if list(resource.get("approved_scopes") or []) != list(required_scopes):
        _fail(f"{location}.approved_scopes", "must equal the boundary scopes")
    owner = _mapping(resource.get("owner_authorization"), f"{location}.owner_authorization")
    scopes = _sequence(owner.get("scopes"), f"{location}.owner_authorization.scopes")
    if RESEARCH_SCOPE not in scopes:
        _fail(f"{location}.owner_authorization.scopes", "missing owner-approved study scope")
    if required_state == "BLOCKED":
        if required_scopes:
            _fail(f"{location}.approved_scopes", "a BLOCKED boundary cannot carry scopes")
        return
    if required_state != LOCAL_RESEARCH_STATE:
        _fail(f"{location}.state", f"unsupported boundary state {required_state}")
    if not required_scopes:
        _fail(f"{location}.approved_scopes", "local research use needs an effective scope")
    if owner.get("decision") != "APPROVED_LOCAL_NONCOMMERCIAL":
        _fail(f"{location}.owner_authorization.decision", "local research use needs APPROVED_LOCAL_NONCOMMERCIAL")
    if not set(required_scopes) <= set(scopes):
        _fail(f"{location}.approved_scopes", "effective scopes exceed the owner grant")
    if owner.get("redistribution_allowed") is not False:
        _fail(f"{location}.owner_authorization.redistribution_allowed", "must be false")
    if owner.get("evidence_artifact_path") != str(USE_PERMISSION_RECEIPT):
        _fail(
            f"{location}.owner_authorization.evidence_artifact_path",
            "local research use must cite the owner use-permission receipt",
        )


def _validate_pair_manifest(path: Path) -> tuple[int, int]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            records = [
                json.loads(line, object_pairs_hook=_no_duplicate_keys)
                for line in handle
                if line.strip()
            ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail("pair_manifest", f"cannot read strict JSONL: {error}")
    if len(records) != 6001:
        _fail("pair_manifest", "must contain one header and 6000 pair records")
    header = _mapping(records[0], "pair_manifest.header")
    if not (
        header.get("record_type") == "PROTOCOL"
        and header.get("resource_id") == DATASET_RESOURCE_ID
        and header.get("pair_count") == 6000
        and header.get("folds") == 10
        and header.get("mated_pairs") == 3000
        and header.get("nonmated_pairs") == 3000
        and header.get("limitations")
        == [
            "NOT_AN_ACQUISITION_RECEIPT",
            "NOT_LICENSE_OR_PERMISSION_EVIDENCE",
            "NOT_COMPLETE_OFFICIAL_PROTOCOL_CONFORMANCE",
            "NO_IMAGE_DECODING_OR_MODEL_EXECUTION",
        ]
    ):
        _fail("pair_manifest.header", "does not retain the accepted local-byte boundary")
    fold_counts = {fold: 0 for fold in range(1, 11)}
    label_counts = {"MATED": 0, "NON_MATED": 0}
    for index, raw_record in enumerate(records[1:], start=1):
        record = _mapping(raw_record, f"pair_manifest[{index}]")
        if record.get("record_type") != "PAIR":
            _fail(f"pair_manifest[{index}].record_type", "must equal PAIR")
        fold = record.get("fold")
        label = record.get("label")
        if fold not in fold_counts or label not in label_counts:
            _fail(f"pair_manifest[{index}]", "has an invalid fold or label")
        fold_counts[fold] += 1
        label_counts[label] += 1
    if list(fold_counts.values()) != [600] * 10 or label_counts != {
        "MATED": 3000,
        "NON_MATED": 3000,
    }:
        _fail("pair_manifest", "fold or class counts differ")
    return 6000, 10


def _validate_resource_boundary(
    value: Any,
    *,
    registry_path: Path,
    pair_manifest_path: Path,
) -> dict[str, Any]:
    location = "authority.resource_boundary"
    boundary = _mapping(value, location)
    _strict_keys(
        boundary,
        location,
        {
            "registry",
            "pair_manifest",
            "dataset_resource_id",
            "matcher_training_evidence",
            "required_state",
            "required_approved_scopes",
            "required_third_party_rights_status",
        },
    )
    if boundary["dataset_resource_id"] != DATASET_RESOURCE_ID:
        _fail(f"{location}.dataset_resource_id", "differs")
    required_state = str(boundary["required_state"])
    required_scopes = list(_sequence(boundary["required_approved_scopes"], f"{location}.required_approved_scopes"))
    if required_state not in {"BLOCKED", LOCAL_RESEARCH_STATE}:
        _fail(location, "boundary state must be BLOCKED or APPROVED_LOCAL_RESEARCH_USE")
    required_rights = str(boundary["required_third_party_rights_status"])
    if required_rights == "VERIFIED_LICENSE":
        _fail(
            f"{location}.required_third_party_rights_status",
            "the official protocol documents grant no image license; VERIFIED_LICENSE cannot be claimed",
        )
    _identity(
        boundary["registry"],
        f"{location}.registry",
        expected_path=registry_path,
        maximum_size=MAX_JSON_BYTES,
    )
    pair_path = _identity(
        boundary["pair_manifest"],
        f"{location}.pair_manifest",
        expected_path=pair_manifest_path,
        maximum_size=MAX_JSON_BYTES,
    )
    pair_count, fold_count = _validate_pair_manifest(pair_path)

    registry = _load_json(registry_path, "registry")
    resources = _sequence(registry.get("resources"), "registry.resources")
    dataset = _find_resource(resources, DATASET_RESOURCE_ID)
    _validate_resource_state_boundary(
        dataset, f"registry.{DATASET_RESOURCE_ID}", required_state, required_scopes
    )
    if dataset.get("third_party_rights_status") != required_rights:
        _fail(
            f"registry.{DATASET_RESOURCE_ID}.third_party_rights_status",
            f"must equal the boundary rights status {required_rights}",
        )

    evidence = _sequence(
        boundary["matcher_training_evidence"], f"{location}.matcher_training_evidence"
    )
    expected_documents = [
        PROJECT_ROOT / "resources" / "upstream" / "adaface" / "README.d8114b3c.md",
        PROJECT_ROOT / "resources" / "upstream" / "elasticface" / "README.5496ec06.md",
        PROJECT_ROOT / "resources" / "upstream" / "transface" / "README.fb6cd56d.md",
    ]
    expected_fragments = [
        "| R100 | WebFace4M",
        "we employ MS1MV2 as the training dataset",
        "| Glint360K | [TransFace-S]",
    ]
    if len(evidence) != 3:
        _fail(f"{location}.matcher_training_evidence", "must contain three matchers")
    states: list[str] = []
    scopes: list[list[Any]] = []
    for index, (raw_evidence, resource_id, document_path, fragment) in enumerate(
        zip(
            evidence,
            MATCHER_RESOURCE_IDS,
            expected_documents,
            expected_fragments,
            strict=True,
        )
    ):
        evidence_location = f"{location}.matcher_training_evidence[{index}]"
        record = _mapping(raw_evidence, evidence_location)
        _strict_keys(
            record,
            evidence_location,
            {"resource_id", "documentation_path", "required_text_fragment"},
        )
        if record != {
            "resource_id": resource_id,
            "documentation_path": str(document_path),
            "required_text_fragment": fragment,
        }:
            _fail(evidence_location, "differs from the frozen matcher evidence")
        resource = _find_resource(resources, resource_id)
        _validate_resource_state_boundary(
            resource, f"registry.{resource_id}", required_state, required_scopes
        )
        artifacts = _sequence(resource.get("artifacts"), f"registry.{resource_id}.artifacts")
        matching_artifacts = [
            item
            for item in artifacts
            if isinstance(item, dict) and item.get("path") == str(document_path)
        ]
        if len(matching_artifacts) != 1:
            _fail(evidence_location, "documentation is not an exact registered artifact")
        artifact = matching_artifacts[0]
        if artifact.get("role") not in {"DOCUMENTATION", "LICENSE"}:
            _fail(evidence_location, "registered evidence has the wrong role")
        actual_identity = {
            "path": artifact.get("path"),
            "sha256": artifact.get("sha256"),
            "size_bytes": artifact.get("size_bytes"),
        }
        _identity(
            actual_identity,
            evidence_location,
            expected_path=document_path,
            maximum_size=MAX_TEXT_BYTES,
        )
        try:
            documentation = document_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            _fail(evidence_location, f"cannot read registered documentation: {error}")
        if fragment not in documentation:
            _fail(evidence_location, "named external training evidence is absent")
        states.append(str(resource.get("state")))
        scopes.append(list(resource.get("approved_scopes", [])))
    return {
        "pair_count": pair_count,
        "fold_count": fold_count,
        "dataset_resource_state": dataset["state"],
        "dataset_approved_scopes": dataset["approved_scopes"],
        "matcher_resource_states": states,
        "matcher_approved_scopes": scopes,
    }


def validate_authority(
    authority_path: Path = AUTHORITY_PATH,
    registry_path: Path = REGISTRY_PATH,
    pair_manifest_path: Path = PAIR_MANIFEST_PATH,
) -> dict[str, Any]:
    authority_path = authority_path.resolve(strict=True)
    registry_path = registry_path.resolve(strict=True)
    pair_manifest_path = pair_manifest_path.resolve(strict=True)
    authority = _load_json(authority_path, "authority")
    _strict_keys(
        authority,
        "authority",
        {
            "schema_version",
            "evidence_id",
            "recorded_at",
            "scope",
            "owner_authorization",
            "original_protocol_authorization",
            "transport_provenance_status",
            "acquisition_tool",
            "documents",
            "text_extractor",
            "protocol_finding",
            "resource_boundary",
            "claims",
            "limitations",
        },
    )
    if authority["schema_version"] != "1.0.0":
        _fail("authority.schema_version", "must equal 1.0.0")
    if authority["evidence_id"] != "lfw-official-protocol-authority-v1":
        _fail("authority.evidence_id", "differs")
    if authority["scope"] != "OFFICIAL_PROTOCOL_AUTHORITY_ONLY":
        _fail("authority.scope", "differs")

    general_authorized_at, _ = _validate_owner_authorization(
        authority["owner_authorization"],
        location="authority.owner_authorization",
        expected_path=OWNER_AUTHORIZATION_PATH,
        expected_authorization_id=(
            "qualsight-lfw-protocol-evidence-review-2026-08-17"
        ),
        expected_scope=OWNER_SCOPE,
    )
    original_authorized_at, _ = _validate_owner_authorization(
        authority["original_protocol_authorization"],
        location="authority.original_protocol_authorization",
        expected_path=ORIGINAL_PROTOCOL_AUTHORIZATION_PATH,
        expected_authorization_id=(
            "qualsight-lfw-original-protocol-evidence-review-2026-08-17"
        ),
        expected_scope=ORIGINAL_PROTOCOL_SCOPE,
    )
    if authority["transport_provenance_status"] != TRANSPORT_PROVENANCE_STATUS:
        _fail(
            "authority.transport_provenance_status",
            "must preserve the missing verbose-request-trace boundary",
        )
    _validate_tool(
        authority["acquisition_tool"],
        "authority.acquisition_tool",
        expected_path=CURL_PATH,
        expected_version="curl 7.88.1",
        version_arguments=["--version"],
    )
    extractor = _mapping(authority["text_extractor"], "authority.text_extractor")
    _strict_keys(
        extractor,
        "authority.text_extractor",
        {"path", "sha256", "size_bytes", "version", "arguments", "timeout_seconds"},
    )
    text_tool = _validate_tool(
        {key: extractor[key] for key in ("path", "sha256", "size_bytes", "version")},
        "authority.text_extractor",
        expected_path=PDFTOTEXT_PATH,
        expected_version="pdftotext version 22.12.0",
        version_arguments=["-v"],
    )
    arguments = _sequence(extractor["arguments"], "authority.text_extractor.arguments")
    if arguments != ["-layout", "{pdf}", "-"]:
        _fail("authority.text_extractor.arguments", "differs")
    timeout_seconds = _integer(
        extractor["timeout_seconds"], "authority.text_extractor.timeout_seconds"
    )
    if timeout_seconds != 30:
        _fail("authority.text_extractor.timeout_seconds", "must equal 30")

    retrieval_ends = _validate_documents(
        authority["documents"],
        general_authorized_at=general_authorized_at,
        original_authorized_at=original_authorized_at,
        text_tool=text_tool,
        text_arguments=arguments,
        timeout_seconds=timeout_seconds,
    )
    recorded_at = _parse_time(authority["recorded_at"], "authority.recorded_at")
    if recorded_at < max(retrieval_ends):
        _fail("authority.recorded_at", "must follow all completed retrievals")
    if recorded_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        _fail("authority.recorded_at", "is implausibly in the future")

    finding = _mapping(authority["protocol_finding"], "authority.protocol_finding")
    _strict_keys(
        finding,
        "authority.protocol_finding",
        {
            "view",
            "pair_count",
            "fold_count",
            "selected_category",
            "classification_basis",
            "status",
            "required_remaining_evidence",
        },
    )
    if finding != {
        "view": "VIEW_2_PERFORMANCE_REPORTING",
        "pair_count": 6000,
        "fold_count": 10,
        "selected_category": SELECTED_CATEGORY,
        "classification_basis": "CONSERVATIVE_MOST_PERMISSIVE_CATEGORY_FOR_PRETRAINED_FACE_MATCHERS_WITH_NAMED_EXTERNAL_TRAINING_DATA",
        "status": FINDING_STATUS,
        "required_remaining_evidence": REMAINING_EVIDENCE,
    }:
        _fail("authority.protocol_finding", "differs from the bounded protocol finding")

    resource_summary = _validate_resource_boundary(
        authority["resource_boundary"],
        registry_path=registry_path,
        pair_manifest_path=pair_manifest_path,
    )
    if (
        finding["pair_count"] != resource_summary["pair_count"]
        or finding["fold_count"] != resource_summary["fold_count"]
    ):
        _fail("authority.protocol_finding", "differs from the exact pair manifest")

    claims = _mapping(authority["claims"], "authority.claims")
    expected_claims = {
        "official_protocol_documents_acquired": True,
        "pdf_text_reproduced": True,
        "protocol_category_selected": True,
        "complete_official_protocol_conformance": False,
        "dataset_rights_closed": False,
        "dataset_acquisition_provenance_closed": False,
        "image_or_model_execution_performed": False,
    }
    _strict_keys(claims, "authority.claims", set(expected_claims))
    if claims != expected_claims:
        _fail("authority.claims", "contains an unsupported claim")
    if authority["limitations"] != LIMITATIONS:
        _fail("authority.limitations", "differs from the frozen evidence boundary")

    return {
        "evidence_id": authority["evidence_id"],
        "official_documents": 3,
        "transport_provenance_status": authority["transport_provenance_status"],
        "view": finding["view"],
        "pair_count": resource_summary["pair_count"],
        "fold_count": resource_summary["fold_count"],
        "selected_category": finding["selected_category"],
        "finding_status": finding["status"],
        "pdf_text_reproduced": claims["pdf_text_reproduced"],
        "complete_official_protocol_conformance": claims[
            "complete_official_protocol_conformance"
        ],
        "dataset_rights_closed": claims["dataset_rights_closed"],
        **resource_summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("authority", nargs="?", type=Path, default=AUTHORITY_PATH)
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    parser.add_argument("--pairs", type=Path, default=PAIR_MANIFEST_PATH)
    args = parser.parse_args()
    try:
        summary = validate_authority(args.authority, args.registry, args.pairs)
    except (LFWProtocolAuthorityValidationError, OSError) as error:
        print(f"LFW_PROTOCOL_AUTHORITY_INVALID {error}", file=sys.stderr)
        return 1
    print(
        "LFW_PROTOCOL_AUTHORITY_VALID "
        f"id={summary['evidence_id']} documents={summary['official_documents']} "
        f"view={summary['view']} pairs={summary['pair_count']} folds={summary['fold_count']} "
        f"category={summary['selected_category']} status={summary['finding_status']} "
        f"dataset_state={summary['dataset_resource_state']} "
        f"approved_scopes={len(summary['dataset_approved_scopes'])} "
        f"rights_closed={str(summary['dataset_rights_closed']).lower()} "
        f"complete_conformance={str(summary['complete_official_protocol_conformance']).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
