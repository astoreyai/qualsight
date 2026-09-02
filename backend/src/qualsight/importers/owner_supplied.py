"""Owner-supplied local inputs (decision 30; Plan D QS-Y80/Y82/Y83).

An evaluator working at this workstation may bring their own photograph,
directory, or CSV manifest. Admission is the governance boundary:

* Uploaded byte streams are decoded (refused if they are not an image),
  SHA-256 hashed, and stored content-addressed under the data directory.
* Directory / manifest files are hashed and recorded in place; every path must
  stay inside the declared root — symlinks and rows escaping the root are
  refused, never silently skipped.
* Every admitted input carries source kind ``OWNER_SUPPLIED_LOCAL_INPUT``, the
  attributed operator, and the admitting project; the batch engine admits an
  owner-supplied path only when the store records exactly that path with a
  matching current hash. Nothing here is a registered, rights-reviewed
  resource, and results must always say so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import stat
import struct
import tempfile

import cv2
import numpy as np

from ..api.store import RunStore
from ..domain.comparison import (
    ComparisonLabel,
    PairPolicy,
    ProtocolCategory,
    ProtocolPair,
    VerificationProtocolRecord,
)
from ..domain.entities import (
    AuditEvent,
    AuditEventType,
    DataClassification,
    Dataset,
    Modality,
    PermissionState,
    Project,
    Sample,
)
from ..domain.primitives import (
    ArtifactIdentity,
    CanonicalJson,
    ComponentBinding,
    ComponentState,
    CountEvidence,
    EvidenceState,
    EvidenceText,
)
from ..engines.ofiq.adapter import content_id
from ..provenance import sha256_bytes, sha256_file
from ..security.owner_file_lock import serialized_owner_file_mutation

SOURCE_KIND = "OWNER_SUPPLIED_LOCAL_INPUT"
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_DECODED_PIXELS = 96_000_000  # decode-bomb guard: refuse > 96 MP images
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".ppm"}
OWNER_PAIR_MAX_SOURCE_SAMPLES = 20_000
OWNER_PAIR_MAX_OUTPUT_PAIRS = 20_000
_PARTIAL_PREFIX = ".owner-supplied-"
_PARTIAL_SUFFIX = ".partial"
_OWNER_INPUT_INTENT_DIRECTORY = ".admission-intents"
_OWNER_INPUT_INTENT_SCHEMA_VERSION = "1.0.0"
_OWNER_PROTOCOL_RECEIPT_SCHEMA_VERSION = "1.0.0"


class OwnerSuppliedError(RuntimeError):
    """Raised when an owner-supplied input cannot be admitted honestly."""


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _encoded_dimensions(data: bytes, name: str) -> tuple[int, int]:
    """Read dimensions from admitted image headers without allocating the pixel plane."""

    width = height = 0
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise OwnerSuppliedError(f"{name}: PNG header is incomplete")
        width, height = struct.unpack(">II", data[16:24])
    elif data.startswith(b"\xff\xd8"):
        index = 2
        start_of_frame = {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }
        while index + 3 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                break
            marker = data[index]
            index += 1
            if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if index + 2 > len(data):
                break
            length = struct.unpack(">H", data[index : index + 2])[0]
            if length < 2 or index + length > len(data):
                raise OwnerSuppliedError(f"{name}: JPEG segment length is invalid")
            if marker in start_of_frame:
                if length < 7:
                    raise OwnerSuppliedError(f"{name}: JPEG frame header is incomplete")
                height, width = struct.unpack(">HH", data[index + 3 : index + 7])
                break
            index += length
    elif data.startswith(b"BM"):
        if len(data) < 26:
            raise OwnerSuppliedError(f"{name}: BMP header is incomplete")
        dib_size = struct.unpack("<I", data[14:18])[0]
        if dib_size == 12:
            width, height = struct.unpack("<HH", data[18:22])
        elif dib_size >= 40 and len(data) >= 26:
            signed_width, signed_height = struct.unpack("<ii", data[18:26])
            if signed_width <= 0 or signed_height == 0:
                raise OwnerSuppliedError(f"{name}: BMP dimensions are invalid")
            width, height = signed_width, abs(signed_height)
        else:
            raise OwnerSuppliedError(f"{name}: BMP DIB header is unsupported")
    elif data.startswith((b"P3", b"P6")):
        tokens: list[bytes] = []
        for line in data[:4096].splitlines():
            content = line.partition(b"#")[0]
            tokens.extend(content.split())
            if len(tokens) >= 4:
                break
        if len(tokens) < 4 or tokens[0] not in {b"P3", b"P6"}:
            raise OwnerSuppliedError(f"{name}: PPM header is incomplete")
        try:
            width, height, maximum = (int(token) for token in tokens[1:4])
        except ValueError as error:
            raise OwnerSuppliedError(f"{name}: PPM dimensions are invalid") from error
        if maximum <= 0 or maximum > 65535:
            raise OwnerSuppliedError(f"{name}: PPM sample maximum is invalid")
    else:
        raise OwnerSuppliedError(
            f"{name}: the bytes do not decode as an admitted JPEG, PNG, BMP, or PPM image"
        )
    if width <= 0 or height <= 0:
        raise OwnerSuppliedError(f"{name}: encoded image dimensions are invalid")
    return width, height


def _decode_check(data: bytes, name: str) -> tuple[int, int]:
    if len(data) > MAX_UPLOAD_BYTES:
        raise OwnerSuppliedError(f"{name}: {len(data)} bytes exceeds the {MAX_UPLOAD_BYTES}-byte upload cap")
    encoded_width, encoded_height = _encoded_dimensions(data, name)
    if encoded_width * encoded_height > MAX_DECODED_PIXELS:
        raise OwnerSuppliedError(
            f"{name}: encoded size {encoded_width}x{encoded_height} exceeds the decode guard"
        )
    array = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise OwnerSuppliedError(f"{name}: the bytes do not decode as an image")
    height, width = image.shape[:2]
    if (width, height) != (encoded_width, encoded_height):
        raise OwnerSuppliedError(
            f"{name}: decoded dimensions {width}x{height} differ from the encoded header "
            f"{encoded_width}x{encoded_height}"
        )
    return width, height


@dataclass(frozen=True, slots=True)
class AdmittedInput:
    sha256: str
    path: str
    size_bytes: int
    width: int
    height: int
    original_name: str
    storage: str  # COPIED | IN_PLACE


@dataclass(frozen=True, slots=True)
class PreparedOwnerInputAdmission:
    receipt_path: Path
    stage_path: Path
    target_path: Path
    project_id: str
    request_sha256: str
    admitted: AdmittedInput
    record: dict[str, object]


@dataclass(frozen=True, slots=True)
class PreparedOwnerProtocol:
    receipt_path: Path
    stage_path: Path
    manifest_path: Path
    request_sha256: str
    record: VerificationProtocolRecord
    pairs: tuple[ProtocolPair, ...]
    audit_event: AuditEvent


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_canonical_receipt(path: Path, document: dict[str, object]) -> None:
    """Write one canonical receipt before any governed output is published."""

    encoded = (CanonicalJson.from_value(document).text + "\n").encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    complete = False
    try:
        written = 0
        while written < len(encoded):
            count = os.write(descriptor, encoded[written:])
            if count <= 0:
                raise OSError("owner publication receipt write made no progress")
            written += count
        os.fsync(descriptor)
        complete = True
    finally:
        os.close(descriptor)
        if not complete:
            path.unlink(missing_ok=True)
    _sync_directory(path.parent)


def _read_canonical_receipt(path: Path) -> dict[str, object]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise OwnerSuppliedError("owner publication receipt is not a regular file")
            payload = source.read()
        text = payload.decode("utf-8")
        document = json.loads(text)
        if (
            not isinstance(document, dict)
            or text != CanonicalJson.from_value(document).text + "\n"
        ):
            raise OwnerSuppliedError("owner publication receipt is not canonical")
        return document
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as error:
        if isinstance(error, OwnerSuppliedError):
            raise
        raise OwnerSuppliedError(
            "owner publication receipt failed integrity verification"
        ) from error


def _validate_sha256(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise OwnerSuppliedError(f"owner publication {field} is invalid")
    return value


def _read_validated_regular_bytes(
    path: Path, *, sha256: str, size_bytes: int
) -> tuple[bytes, os.stat_result] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise OwnerSuppliedError("owner publication artifact cannot be opened safely") from error
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != size_bytes:
            raise OwnerSuppliedError("owner publication artifact size differs")
        payload = source.read(size_bytes + 1)
        after = os.fstat(source.fileno())
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise OwnerSuppliedError("owner publication artifact changed during validation")
    if len(payload) != size_bytes or hashlib.sha256(payload).hexdigest() != sha256:
        raise OwnerSuppliedError("owner publication artifact hash differs")
    return payload, after


def _validate_regular_bytes(path: Path, *, sha256: str, size_bytes: int) -> bool:
    return (
        _read_validated_regular_bytes(
            path, sha256=sha256, size_bytes=size_bytes
        )
        is not None
    )


def _publish_regular_file_no_clobber(stage: Path, target: Path) -> None:
    try:
        os.link(stage, target, follow_symlinks=False)
    except FileExistsError:
        return
    except OSError as error:
        raise OwnerSuppliedError("owner artifact could not be published safely") from error
    _sync_directory(target.parent)


def prepared_owner_input_admissions(data_dir: Path) -> tuple[Path, ...]:
    root = data_dir / "owner_supplied" / _OWNER_INPUT_INTENT_DIRECTORY
    if not root.exists():
        return ()
    if root.is_symlink() or not root.is_dir():
        raise OwnerSuppliedError("owner admission intent directory is invalid")
    return tuple(sorted(root.glob("*.json")))


def _load_owner_input_intent(
    receipt_path: Path, *, data_dir: Path
) -> PreparedOwnerInputAdmission:
    storage_root = data_dir / "owner_supplied"
    intent_root = storage_root / _OWNER_INPUT_INTENT_DIRECTORY
    if receipt_path.parent != intent_root or receipt_path.suffix != ".json":
        raise OwnerSuppliedError("owner admission receipt path is invalid")
    document = _read_canonical_receipt(receipt_path)
    if set(document) != {
        "record",
        "request",
        "request_sha256",
        "schema_version",
        "stage_name",
        "target_name",
    } or document.get("schema_version") != _OWNER_INPUT_INTENT_SCHEMA_VERSION:
        raise OwnerSuppliedError("owner admission receipt contract differs")
    request = document["request"]
    record = document["record"]
    if not isinstance(request, dict) or not isinstance(record, dict):
        raise OwnerSuppliedError("owner admission receipt payload differs")
    request_sha256 = _validate_sha256(
        document["request_sha256"], field="request SHA-256"
    )
    if CanonicalJson.from_value(request).sha256 != request_sha256:
        raise OwnerSuppliedError("owner admission request identity differs")
    expected_request_keys = {
        "actor_id",
        "height",
        "original_name",
        "project_id",
        "sha256",
        "size_bytes",
        "source_kind",
        "storage",
        "target_name",
        "width",
    }
    if set(request) != expected_request_keys:
        raise OwnerSuppliedError("owner admission request contract differs")
    digest = _validate_sha256(request["sha256"], field="content SHA-256")
    target_name = document["target_name"]
    stage_name = document["stage_name"]
    project_id = request["project_id"]
    if (
        not isinstance(target_name, str)
        or Path(target_name).name != target_name
        or not target_name.startswith(digest + ".")
        or Path(target_name).suffix.lower() not in IMAGE_SUFFIXES
        or request["target_name"] != target_name
        or not isinstance(stage_name, str)
        or stage_name != f".{target_name}.{request_sha256}.partial"
        or not isinstance(project_id, str)
        or not project_id
        or request["source_kind"] != SOURCE_KIND
        or request["storage"] != "COPIED"
        or not isinstance(request["actor_id"], str)
        or not request["actor_id"]
        or not isinstance(request["original_name"], str)
        or not request["original_name"]
        or not isinstance(request["size_bytes"], int)
        or isinstance(request["size_bytes"], bool)
        or request["size_bytes"] <= 0
        or not isinstance(request["width"], int)
        or isinstance(request["width"], bool)
        or request["width"] <= 0
        or not isinstance(request["height"], int)
        or isinstance(request["height"], bool)
        or request["height"] <= 0
    ):
        raise OwnerSuppliedError("owner admission request values differ")
    expected_record = {
        key: request[key]
        for key in (
            "actor_id",
            "height",
            "original_name",
            "project_id",
            "sha256",
            "size_bytes",
            "source_kind",
            "storage",
            "width",
        )
    }
    target_path = storage_root / target_name
    expected_record["path"] = str(target_path)
    if (
        set(record) != {*expected_record, "admitted_at"}
        or any(record.get(key) != value for key, value in expected_record.items())
        or not isinstance(record.get("admitted_at"), str)
    ):
        raise OwnerSuppliedError("owner admission record differs")
    try:
        admitted_at = datetime.fromisoformat(str(record["admitted_at"]))
    except ValueError as error:
        raise OwnerSuppliedError("owner admission timestamp is invalid") from error
    if admitted_at.tzinfo is None:
        raise OwnerSuppliedError("owner admission timestamp is not timezone-aware")
    if receipt_path.name != f"{request_sha256}.json":
        raise OwnerSuppliedError("owner admission receipt identity differs")
    admitted = AdmittedInput(
        sha256=digest,
        path=str(target_path),
        size_bytes=int(request["size_bytes"]),
        width=int(request["width"]),
        height=int(request["height"]),
        original_name=str(request["original_name"]),
        storage="COPIED",
    )
    return PreparedOwnerInputAdmission(
        receipt_path=receipt_path,
        stage_path=storage_root / stage_name,
        target_path=target_path,
        project_id=project_id,
        request_sha256=request_sha256,
        admitted=admitted,
        record=record,
    )


def recover_prepared_owner_input_admission(
    receipt_path: Path, *, data_dir: Path
) -> PreparedOwnerInputAdmission:
    """Verify and finish one project-bound copied-input publication."""

    prepared = _load_owner_input_intent(receipt_path, data_dir=data_dir)
    if not _validate_regular_bytes(
        prepared.target_path,
        sha256=prepared.admitted.sha256,
        size_bytes=prepared.admitted.size_bytes,
    ):
        if not _validate_regular_bytes(
            prepared.stage_path,
            sha256=prepared.admitted.sha256,
            size_bytes=prepared.admitted.size_bytes,
        ):
            raise OwnerSuppliedError("prepared owner input bytes are unavailable")
        _publish_regular_file_no_clobber(prepared.stage_path, prepared.target_path)
        if not _validate_regular_bytes(
            prepared.target_path,
            sha256=prepared.admitted.sha256,
            size_bytes=prepared.admitted.size_bytes,
        ):
            raise OwnerSuppliedError("published owner input bytes are unavailable")
    validated = _read_validated_regular_bytes(
        prepared.target_path,
        sha256=prepared.admitted.sha256,
        size_bytes=prepared.admitted.size_bytes,
    )
    if validated is None:
        raise OwnerSuppliedError("published owner input bytes are unavailable")
    payload, _ = validated
    width, height = _decode_check(payload, prepared.admitted.original_name)
    if (width, height) != (prepared.admitted.width, prepared.admitted.height):
        raise OwnerSuppliedError("prepared owner input dimensions differ")
    return prepared


def inspect_prepared_owner_input_admission(
    receipt_path: Path, *, data_dir: Path
) -> PreparedOwnerInputAdmission:
    """Validate receipt identity without publishing or deleting any bytes."""

    return _load_owner_input_intent(receipt_path, data_dir=data_dir)


def finalize_prepared_owner_input_admission(
    prepared: PreparedOwnerInputAdmission,
) -> None:
    """Remove only intent/staging evidence after the project reference commits."""

    for path in (prepared.stage_path, prepared.receipt_path):
        try:
            observed = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(observed.st_mode):
            raise OwnerSuppliedError("owner admission cleanup target is invalid")
        path.unlink()
        _sync_directory(path.parent)


def _write_owner_input_stage(path: Path, data: bytes) -> None:
    if _validate_regular_bytes(
        path, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)
    ):
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    complete = False
    try:
        written = 0
        while written < len(data):
            count = os.write(descriptor, data[written:])
            if count <= 0:
                raise OSError("owner input staging write made no progress")
            written += count
        os.fsync(descriptor)
        complete = True
    finally:
        os.close(descriptor)
        if not complete:
            path.unlink(missing_ok=True)
    _sync_directory(path.parent)


def _prepare_owner_input_admission(
    *,
    data_dir: Path,
    actor_id: str,
    project_id: str,
    admitted: AdmittedInput,
    data: bytes,
) -> PreparedOwnerInputAdmission:
    storage_root = data_dir / "owner_supplied"
    intent_root = storage_root / _OWNER_INPUT_INTENT_DIRECTORY
    intent_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _sync_directory(storage_root)
    target_path = Path(admitted.path)
    request = {
        "actor_id": actor_id,
        "height": admitted.height,
        "original_name": admitted.original_name,
        "project_id": project_id,
        "sha256": admitted.sha256,
        "size_bytes": admitted.size_bytes,
        "source_kind": SOURCE_KIND,
        "storage": admitted.storage,
        "target_name": target_path.name,
        "width": admitted.width,
    }
    request_sha256 = CanonicalJson.from_value(request).sha256
    receipt_path = intent_root / f"{request_sha256}.json"
    stage_name = f".{target_path.name}.{request_sha256}.partial"
    if not receipt_path.exists() and not receipt_path.is_symlink():
        record: dict[str, object] = {
            **{key: value for key, value in request.items() if key != "target_name"},
            "path": str(target_path),
            "admitted_at": _now().isoformat(),
        }
        _write_canonical_receipt(
            receipt_path,
            {
                "record": record,
                "request": request,
                "request_sha256": request_sha256,
                "schema_version": _OWNER_INPUT_INTENT_SCHEMA_VERSION,
                "stage_name": stage_name,
                "target_name": target_path.name,
            },
        )
    prepared = _load_owner_input_intent(receipt_path, data_dir=data_dir)
    if prepared.request_sha256 != request_sha256:
        raise OwnerSuppliedError("owner admission intent differs from its request")
    if not _validate_regular_bytes(
        prepared.target_path,
        sha256=admitted.sha256,
        size_bytes=admitted.size_bytes,
    ):
        _write_owner_input_stage(prepared.stage_path, data)
    return recover_prepared_owner_input_admission(
        receipt_path, data_dir=data_dir
    )


class OwnerSuppliedAdmission:
    """Decision-30 admission of operator-picked local inputs."""

    def __init__(self, store: RunStore, data_dir: Path, actor_id: str) -> None:
        self.store = store
        self.data_dir = data_dir
        self.actor = actor_id
        self.storage_root = data_dir / "owner_supplied"

    def _record(self, admitted: AdmittedInput, project_id: str) -> AdmittedInput:
        self.store.record_owner_supplied(
            {
                "path": admitted.path,
                "sha256": admitted.sha256,
                "project_id": project_id,
                "actor_id": self.actor,
                "original_name": admitted.original_name,
                "size_bytes": admitted.size_bytes,
                "width": admitted.width,
                "height": admitted.height,
                "storage": admitted.storage,
                "source_kind": SOURCE_KIND,
                "admitted_at": _now().isoformat(),
            }
        )
        return admitted

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _cleanup_stale_partials(self) -> None:
        changed = False
        for partial in self.storage_root.glob(f"{_PARTIAL_PREFIX}*{_PARTIAL_SUFFIX}"):
            observed = partial.lstat()
            if not stat.S_ISREG(observed.st_mode):
                raise OwnerSuppliedError(
                    "owner-supplied staging contains a non-regular partial file"
                )
            partial.unlink()
            changed = True
        if changed:
            self._sync_directory(self.storage_root)

    @staticmethod
    def _validate_existing_target(target: Path, data: bytes) -> bool:
        try:
            observed = target.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(observed.st_mode):
            raise OwnerSuppliedError(
                "the content-addressed owner-supplied target is not a regular file"
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise OwnerSuppliedError(
                "the content-addressed owner-supplied target cannot be opened safely"
            ) from error
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise OwnerSuppliedError(
                    "the content-addressed owner-supplied target is not a regular file"
                )
            existing = source.read(len(data) + 1)
            after = os.fstat(source.fileno())
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise OwnerSuppliedError(
                "the content-addressed owner-supplied target changed during validation"
            )
        if existing != data:
            raise OwnerSuppliedError(
                "the content-addressed owner-supplied target differs from its requested bytes"
            )
        return True

    def _publish_bytes(self, target: Path, data: bytes) -> None:
        if self._validate_existing_target(target, data):
            return
        descriptor, partial_name = tempfile.mkstemp(
            prefix=_PARTIAL_PREFIX,
            suffix=_PARTIAL_SUFFIX,
            dir=self.storage_root,
        )
        partial = Path(partial_name)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                written = destination.write(data)
                if written != len(data):
                    raise OSError("owner-supplied staging write was incomplete")
                destination.flush()
                os.fsync(destination.fileno())
            try:
                os.link(partial, target, follow_symlinks=False)
            except FileExistsError:
                if not self._validate_existing_target(target, data):
                    raise OwnerSuppliedError(
                        "the content-addressed owner-supplied target disappeared during publication"
                    )
            else:
                self._sync_directory(self.storage_root)
                if not self._validate_existing_target(target, data):
                    raise OwnerSuppliedError(
                        "the published owner-supplied target could not be verified"
                    )
        except OwnerSuppliedError:
            raise
        except OSError as error:
            raise OwnerSuppliedError(
                "owner-supplied bytes could not be published safely"
            ) from error
        finally:
            if partial.exists():
                partial.unlink()
                self._sync_directory(self.storage_root)

    def admit_bytes(self, data: bytes, original_name: str, project_id: str) -> AdmittedInput:
        width, height = _decode_check(data, original_name)
        digest = hashlib.sha256(data).hexdigest()
        suffix = Path(original_name).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            suffix = ".jpg"
        with serialized_owner_file_mutation():
            self.storage_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._sync_directory(self.storage_root.parent)
            self._cleanup_stale_partials()
            target = self.storage_root / f"{digest}{suffix}"
            admitted = AdmittedInput(
                sha256=digest,
                path=str(target),
                size_bytes=len(data),
                width=width,
                height=height,
                original_name=original_name,
                storage="COPIED",
            )
            prepared = _prepare_owner_input_admission(
                data_dir=self.data_dir,
                actor_id=self.actor,
                project_id=project_id,
                admitted=admitted,
                data=data,
            )
            self.store.record_owner_supplied(prepared.record)
            finalize_prepared_owner_input_admission(prepared)
            return prepared.admitted

    def admit_path(self, path: Path, root: Path, project_id: str) -> AdmittedInput:
        with serialized_owner_file_mutation():
            resolved_root = root.resolve(strict=True)
            if path.is_symlink():
                raise OwnerSuppliedError(f"{path}: symlinks are not admitted")
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(resolved_root):
                raise OwnerSuppliedError(f"{path}: resolves outside the declared root {root}")
            data = resolved.read_bytes()
            width, height = _decode_check(data, str(path))
            admitted = AdmittedInput(
                sha256=hashlib.sha256(data).hexdigest(),
                path=str(resolved),
                size_bytes=len(data),
                width=width,
                height=height,
                original_name=resolved.name,
                storage="IN_PLACE",
            )
            return self._record(admitted, project_id)


@dataclass(frozen=True, slots=True)
class OwnerImportResult:
    dataset: Dataset
    sample_count: int
    subject_count: int
    statistics_sha256: str


class OwnerSuppliedImporter:
    """Identity-labelled datasets from an operator directory or CSV manifest (QS-Y82)."""

    def __init__(self, store: RunStore, data_dir: Path, actor_id: str) -> None:
        self.store = store
        self.admission = OwnerSuppliedAdmission(store, data_dir, actor_id)
        self.actor = actor_id

    # -- collection ------------------------------------------------------------

    def _collect_directory(self, root: Path) -> list[tuple[str, Path]]:
        if not root.is_dir():
            raise OwnerSuppliedError(f"{root} is not a directory")
        rows: list[tuple[str, Path]] = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise OwnerSuppliedError(f"{path}: symlinks are not admitted")
            if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            subject = path.parent.name if path.parent != root else ""
            if not subject:
                raise OwnerSuppliedError(
                    f"{path}: images must sit in a subject directory (subject = immediate folder name)"
                )
            rows.append((subject, path))
        if not rows:
            raise OwnerSuppliedError(f"{root} contains no images in subject directories")
        return rows

    def _collect_manifest(self, manifest_path: Path, mapping: dict[str, str], root: Path) -> list[tuple[str, Path]]:
        import csv as _csv

        for field in ("subject_id", "path"):
            if field not in mapping or not mapping[field]:
                raise OwnerSuppliedError(f"the field mapping must name a source column for {field}")
        rows: list[tuple[str, Path]] = []
        with manifest_path.open("r", newline="", encoding="utf-8") as handle:
            reader = _csv.DictReader(handle)
            if reader.fieldnames is None:
                raise OwnerSuppliedError("the manifest has no header row")
            for field, column in mapping.items():
                if column not in reader.fieldnames:
                    raise OwnerSuppliedError(f"mapped column {column} ({field}) is not in the manifest header")
            for index, row in enumerate(reader, start=2):
                subject = (row.get(mapping["subject_id"]) or "").strip()
                raw_path = (row.get(mapping["path"]) or "").strip()
                if not subject or not raw_path:
                    raise OwnerSuppliedError(f"manifest line {index}: empty subject or path")
                path = Path(raw_path)
                if not path.is_absolute():
                    path = root / path
                rows.append((subject, path))
        if not rows:
            raise OwnerSuppliedError("the manifest has no data rows")
        return rows

    # -- import ----------------------------------------------------------------

    def import_rows(
        self,
        project: Project,
        rows: list[tuple[str, Path]],
        *,
        root: Path,
        name: str,
        origin: dict[str, object],
    ) -> OwnerImportResult:
        with serialized_owner_file_mutation():
            return self._import_rows_locked(project, rows, root=root, name=name, origin=origin)

    def _import_rows_locked(
        self,
        project: Project,
        rows: list[tuple[str, Path]],
        *,
        root: Path,
        name: str,
        origin: dict[str, object],
    ) -> OwnerImportResult:
        admitted: list[tuple[str, AdmittedInput]] = []
        for subject, path in rows:
            admitted.append((subject, self.admission.admit_path(path, root, project.project_id)))
        subjects = sorted({subject for subject, _ in admitted})
        digest = hashlib.sha256()
        for subject, item in admitted:
            digest.update(f"{subject}\t{item.sha256}\t{item.path}\n".encode("utf-8"))
        manifest_sha = digest.hexdigest()
        dataset_id = content_id(
            "dataset", project.project_id, SOURCE_KIND, manifest_sha, name,
            json.dumps(origin, sort_keys=True, separators=(",", ":")),
        )
        manifest_descriptor = CanonicalJson.from_value(
            {
                "source_kind": SOURCE_KIND,
                "manifest_version": "1.0.0",
                "tree_root": str(root),
                "identity_rule": "subject_id supplied by the operator (directory name or mapped manifest column)",
                "sample_count": len(admitted),
                "content_sha256": manifest_sha,
                "origin": origin,
                "admitting_actor": self.actor,
            }
        )
        samples: list[Sample] = []
        for subject, item in admitted:
            samples.append(
                Sample(
                    sample_id=content_id("sample", dataset_id, item.sha256, item.path),
                    dataset_id=dataset_id,
                    subject_id=EvidenceText(state=EvidenceState.RECORDED, value=subject),
                    modality=Modality.FACE,
                    source_path=item.path,
                    media_type="image/jpeg" if item.path.lower().endswith((".jpg", ".jpeg")) else "image/png",
                    sha256=item.sha256,
                    size_bytes=item.size_bytes,
                    mtime_ns=Path(item.path).stat().st_mtime_ns,
                    metadata=CanonicalJson.from_value(
                        {
                            "source_kind": SOURCE_KIND,
                            "subject_id": subject,
                            "width": item.width,
                            "height": item.height,
                            "storage": item.storage,
                        }
                    ),
                )
            )
        per_subject: dict[str, int] = {}
        for subject, _ in admitted:
            per_subject[subject] = per_subject.get(subject, 0) + 1
        stats = {
            "population": {
                "subjects": len(subjects),
                "samples": len(admitted),
                "samples_per_subject_min": min(per_subject.values()),
                "samples_per_subject_max": max(per_subject.values()),
            },
            "provenance": {
                "source_kind": SOURCE_KIND,
                "rights_review": "NOT_PERFORMED — operator-supplied local input under decision 30",
                "admitting_actor": self.actor,
            },
            "computed_at": _now().isoformat(),
        }
        stats_text = json.dumps(stats, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        stats_sha = sha256_bytes(stats_text.encode("utf-8"))
        dataset = Dataset(
            dataset_id=dataset_id,
            project_id=project.project_id,
            name=name,
            version="1.0.0",
            source_uri=f"file://{root}",
            license=None,
            rights_status="OWNER_SUPPLIED_UNREVIEWED",
            rights_evidence=(),
            retrieval=EvidenceText(
                state=EvidenceState.RECORDED,
                value="Supplied interactively by the attributed operator on this workstation (decision 30); no third-party rights review performed.",
            ),
            manifest=manifest_descriptor,
            subject_count=CountEvidence(state=EvidenceState.RECORDED, value=len(subjects)),
            sample_count=CountEvidence(state=EvidenceState.RECORDED, value=len(admitted)),
            modality=Modality.FACE,
            identity_schema=ComponentBinding(
                state=ComponentState.CONFIGURED,
                component_id="owner-supplied-identity",
                version="1.0.0",
                configuration_sha256=sha256_bytes(b"subject_id := operator-declared label (directory name or mapped column)"),
            ),
            metadata_schema=ComponentBinding(
                state=ComponentState.CONFIGURED,
                component_id="owner-supplied-metadata",
                version="1.0.0",
                configuration_sha256=sha256_bytes(b"source_kind, subject_id, width, height, storage"),
            ),
            redistribution_permission=PermissionState.PROHIBITED,
            display_permission=PermissionState.LIMITED,
            derived_artifact_policy=(
                "Local analysis only: hashes, quality measures, crops, and embeddings may be recorded on this "
                "workstation; nothing is redistributed and the source kind travels with every result."
            ),
            classification=DataClassification.PRIVATE,
        )
        evidence = sha256_bytes(dataset.model_dump_json().encode("utf-8"))
        occurred_at = _now()
        audit_event = AuditEvent(
            audit_event_id=content_id("audit-event", dataset_id, "DATASET_IMPORTED", evidence, occurred_at.isoformat()),
            project_id=project.project_id,
            actor_id=self.actor,
            event_type=AuditEventType.DATASET_IMPORTED,
            subject_type="Dataset",
            subject_id=dataset_id,
            occurred_at=occurred_at,
            details=CanonicalJson.from_value(
                {
                    "source_kind": SOURCE_KIND,
                    "samples": len(admitted),
                    "subjects": len(subjects),
                    "statistics_sha256": stats_sha,
                    "content_sha256": manifest_sha,
                    "origin": origin,
                }
            ),
            current_evidence_sha256=evidence,
            local_only=True,
        )
        self.store.record_dataset_import(dataset, samples, stats_text, stats_sha, audit_event)
        return OwnerImportResult(dataset=dataset, sample_count=len(admitted), subject_count=len(subjects), statistics_sha256=stats_sha)

    def import_admitted_single(self, project: Project, admitted: AdmittedInput, name: str) -> tuple[Dataset, Sample]:
        """A content-addressed one-sample dataset for an uploaded photo (QS-Y81); idempotent per byte identity."""

        dataset_id = content_id("dataset", project.project_id, SOURCE_KIND, "single", admitted.sha256)
        existing = self.store.dataset(dataset_id)
        sample_id = content_id("sample", dataset_id, admitted.sha256, admitted.path)
        if existing is not None:
            sample = self.store.sample(sample_id)
            if sample is None:
                raise OwnerSuppliedError("recorded single-sample dataset lost its sample record")
            return existing, sample
        manifest_descriptor = CanonicalJson.from_value(
            {
                "source_kind": SOURCE_KIND,
                "manifest_version": "1.0.0",
                "tree_root": str(Path(admitted.path).parent),
                "identity_rule": "no identity label — a single operator-uploaded photograph",
                "sample_count": 1,
                "content_sha256": admitted.sha256,
                "origin": {"kind": "UPLOAD", "original_name": admitted.original_name},
                "admitting_actor": self.actor,
            }
        )
        sample = Sample(
            sample_id=sample_id,
            dataset_id=dataset_id,
            subject_id=EvidenceText(state=EvidenceState.NOT_RECORDED, reason="Uploaded photo carries no operator identity label."),
            modality=Modality.FACE,
            source_path=admitted.path,
            media_type="image/jpeg" if admitted.path.lower().endswith((".jpg", ".jpeg")) else "image/png",
            sha256=admitted.sha256,
            size_bytes=admitted.size_bytes,
            mtime_ns=Path(admitted.path).stat().st_mtime_ns,
            metadata=CanonicalJson.from_value(
                {
                    "source_kind": SOURCE_KIND,
                    "original_name": admitted.original_name,
                    "width": admitted.width,
                    "height": admitted.height,
                    "storage": admitted.storage,
                }
            ),
        )
        stats = {
            "population": {"subjects": 0, "samples": 1},
            "provenance": {
                "source_kind": SOURCE_KIND,
                "rights_review": "NOT_PERFORMED — operator-supplied local input under decision 30",
                "admitting_actor": self.actor,
            },
            "computed_at": _now().isoformat(),
        }
        stats_text = json.dumps(stats, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        stats_sha = sha256_bytes(stats_text.encode("utf-8"))
        dataset = Dataset(
            dataset_id=dataset_id,
            project_id=project.project_id,
            name=name,
            version="1.0.0",
            source_uri=f"upload://{admitted.original_name}",
            license=None,
            rights_status="OWNER_SUPPLIED_UNREVIEWED",
            rights_evidence=(),
            retrieval=EvidenceText(
                state=EvidenceState.RECORDED,
                value="Uploaded interactively by the attributed operator (decision 30); stored content-addressed in the data directory.",
            ),
            manifest=manifest_descriptor,
            subject_count=CountEvidence(state=EvidenceState.NOT_RECORDED, reason="No identity label was supplied."),
            sample_count=CountEvidence(state=EvidenceState.RECORDED, value=1),
            modality=Modality.FACE,
            identity_schema=ComponentBinding(state=ComponentState.NOT_RECORDED, reason="No identity label was supplied with the upload."),
            metadata_schema=ComponentBinding(
                state=ComponentState.CONFIGURED,
                component_id="owner-supplied-metadata",
                version="1.0.0",
                configuration_sha256=sha256_bytes(b"source_kind, original_name, width, height, storage"),
            ),
            redistribution_permission=PermissionState.PROHIBITED,
            display_permission=PermissionState.LIMITED,
            derived_artifact_policy=(
                "Local analysis only: hashes, quality measures, crops, and embeddings may be recorded on this "
                "workstation; nothing is redistributed and the source kind travels with every result."
            ),
            classification=DataClassification.PRIVATE,
        )
        evidence = sha256_bytes(dataset.model_dump_json().encode("utf-8"))
        occurred_at = _now()
        audit_event = AuditEvent(
            audit_event_id=content_id("audit-event", dataset_id, "DATASET_IMPORTED", evidence, occurred_at.isoformat()),
            project_id=project.project_id,
            actor_id=self.actor,
            event_type=AuditEventType.DATASET_IMPORTED,
            subject_type="Dataset",
            subject_id=dataset_id,
            occurred_at=occurred_at,
            details=CanonicalJson.from_value(
                {"source_kind": SOURCE_KIND, "samples": 1, "sha256": admitted.sha256, "origin": "UPLOAD"}
            ),
            current_evidence_sha256=evidence,
            local_only=True,
        )
        self.store.record_dataset_import(dataset, [sample], stats_text, stats_sha, audit_event)
        return dataset, sample

    def import_directory(self, project: Project, root: Path, name: str) -> OwnerImportResult:
        rows = self._collect_directory(root)
        return self.import_rows(project, rows, root=root, name=name, origin={"kind": "DIRECTORY", "root": str(root)})

    def import_manifest(
        self,
        project: Project,
        manifest_path: Path,
        *,
        name: str,
        mapping: dict[str, str],
    ) -> OwnerImportResult:
        manifest_path = manifest_path.resolve(strict=True)
        root = manifest_path.parent
        rows = self._collect_manifest(manifest_path, mapping, root)
        origin = {
            "kind": "CSV_MANIFEST",
            "manifest_path": str(manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
            "mapping": dict(sorted(mapping.items())),
        }
        return self.import_rows(project, rows, root=root, name=name, origin=origin)


# -- QS-Y83: deterministic pair protocol over an owner-supplied dataset ---------


def _owner_pair_preflight(
    subject_counts: dict[str, int],
    *,
    max_mated_pairs_per_subject: int,
    nonmated_multiple: float,
    max_pairs: int,
) -> tuple[int, int]:
    if (
        not isinstance(max_mated_pairs_per_subject, int)
        or isinstance(max_mated_pairs_per_subject, bool)
        or max_mated_pairs_per_subject < 1
    ):
        raise OwnerSuppliedError(
            "owner pair max_mated_pairs_per_subject must be a positive integer"
        )
    if (
        not isinstance(nonmated_multiple, (int, float))
        or isinstance(nonmated_multiple, bool)
        or not math.isfinite(nonmated_multiple)
        or nonmated_multiple <= 0
    ):
        raise OwnerSuppliedError(
            "owner pair nonmated_multiple must be finite and positive"
        )
    source_samples = sum(subject_counts.values())
    if source_samples > OWNER_PAIR_MAX_SOURCE_SAMPLES:
        raise OwnerSuppliedError(
            "owner pair protocol exceeds the beta source limit of "
            f"{OWNER_PAIR_MAX_SOURCE_SAMPLES:,} samples"
        )
    if (
        not isinstance(max_pairs, int)
        or isinstance(max_pairs, bool)
        or max_pairs < 1
        or max_pairs > OWNER_PAIR_MAX_OUTPUT_PAIRS
    ):
        raise OwnerSuppliedError(
            "owner pair max_pairs must be between 1 and "
            f"{OWNER_PAIR_MAX_OUTPUT_PAIRS:,}"
        )
    mated_count = sum(
        min((count * (count - 1)) // 2, max_mated_pairs_per_subject)
        for count in subject_counts.values()
    )
    if mated_count == 0:
        raise OwnerSuppliedError(
            "no subject has two or more samples; no mated pair exists"
        )
    nonmated_count = max(1, int(round(mated_count * nonmated_multiple)))
    planned_pairs = mated_count + nonmated_count
    if planned_pairs > max_pairs:
        raise OwnerSuppliedError(
            f"owner pair protocol plans {planned_pairs:,} pairs, exceeding "
            f"its {max_pairs:,}-pair beta limit"
        )
    nonmated_capacity = source_samples * source_samples - sum(
        count * count for count in subject_counts.values()
    )
    if nonmated_count > nonmated_capacity:
        raise OwnerSuppliedError(
            f"owner pair protocol requests {nonmated_count:,} distinct "
            "non-mated pairs, but the source supports at most "
            f"{nonmated_capacity:,} distinct non-mated pairs"
        )
    return mated_count, nonmated_count


def _combination_at_rank(
    items: list[Sample], rank: int
) -> tuple[Sample, Sample]:
    """Return one lexicographically ordered two-item combination without expansion."""

    item_count = len(items)
    low = 0
    high = item_count - 2
    while low < high:
        middle = (low + high + 1) // 2
        first_at_middle = middle * (2 * item_count - middle - 1) // 2
        if first_at_middle <= rank:
            low = middle
        else:
            high = middle - 1
    left_index = low
    first_at_left = left_index * (2 * item_count - left_index - 1) // 2
    right_index = left_index + 1 + rank - first_at_left
    return items[left_index], items[right_index]


def _mated_pairs_for_subject(
    items: list[Sample], *, maximum: int, rng: random.Random
) -> list[tuple[Sample, Sample]]:
    combination_count = len(items) * (len(items) - 1) // 2
    if combination_count <= maximum:
        return [
            (items[left], items[right])
            for left in range(len(items))
            for right in range(left + 1, len(items))
        ]
    sampled = [
        _combination_at_rank(items, rank)
        for rank in rng.sample(range(combination_count), maximum)
    ]
    sampled.sort(key=lambda pair: (pair[0].source_path, pair[1].source_path))
    return sampled


def prepared_owner_protocols(data_dir: Path) -> tuple[Path, ...]:
    root = data_dir / "protocols"
    if not root.exists():
        return ()
    if root.is_symlink() or not root.is_dir():
        raise OwnerSuppliedError("owner protocol directory is invalid")
    return tuple(sorted(root.glob(".*.jsonl.prepared.json")))


def _owner_protocol_manifest_payload(
    record: VerificationProtocolRecord,
    pairs: tuple[ProtocolPair, ...] | list[ProtocolPair],
) -> bytes:
    metadata = json.loads(record.metadata.text)
    header = {
        "record_type": "PROTOCOL",
        "generator": metadata["generator"],
        "seed": metadata["seed"],
        "max_mated_pairs_per_subject": metadata[
            "max_mated_pairs_per_subject"
        ],
        "nonmated_multiple": metadata["nonmated_multiple"],
        "pair_count": record.pair_count,
        "mated_pairs": record.mated_count,
        "pairs_sha256": record.pairs_sha256,
    }
    lines = [json.dumps(header, sort_keys=True)]
    for pair in pairs:
        lines.append(
            json.dumps(
                {"record_type": "PAIR", **json.loads(pair.model_dump_json())},
                sort_keys=True,
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _load_owner_protocol_receipt(
    receipt_path: Path, *, data_dir: Path
) -> PreparedOwnerProtocol:
    protocol_root = data_dir / "protocols"
    if receipt_path.parent != protocol_root:
        raise OwnerSuppliedError("owner protocol receipt path is invalid")
    document = _read_canonical_receipt(receipt_path)
    if set(document) != {
        "audit_event",
        "pairs",
        "record",
        "request",
        "request_sha256",
        "schema_version",
        "stage_name",
        "target_name",
    } or document.get("schema_version") != _OWNER_PROTOCOL_RECEIPT_SCHEMA_VERSION:
        raise OwnerSuppliedError("owner protocol receipt contract differs")
    request = document["request"]
    if not isinstance(request, dict):
        raise OwnerSuppliedError("owner protocol request contract differs")
    request_sha256 = _validate_sha256(
        document["request_sha256"], field="protocol request SHA-256"
    )
    if CanonicalJson.from_value(request).sha256 != request_sha256:
        raise OwnerSuppliedError("owner protocol request identity differs")
    try:
        record = VerificationProtocolRecord.model_validate_json(
            json.dumps(document["record"], separators=(",", ":"), ensure_ascii=False)
        )
        pairs = tuple(
            ProtocolPair.model_validate_json(
                json.dumps(item, separators=(",", ":"), ensure_ascii=False)
            )
            for item in document["pairs"]
        )
        audit_event = AuditEvent.model_validate_json(
            json.dumps(
                document["audit_event"], separators=(",", ":"), ensure_ascii=False
            )
        )
    except (TypeError, ValueError) as error:
        raise OwnerSuppliedError("owner protocol receipt payload differs") from error
    try:
        metadata = json.loads(record.metadata.text)
    except (TypeError, ValueError) as error:
        raise OwnerSuppliedError("owner protocol metadata differs") from error
    if set(metadata) != {
        "generator",
        "max_mated_pairs_per_subject",
        "nonmated_multiple",
        "seed",
        "source_kind",
    } or metadata.get("source_kind") != SOURCE_KIND:
        raise OwnerSuppliedError("owner protocol metadata differs")
    expected_request = {
        "actor_id": audit_event.actor_id,
        "dataset_id": record.dataset_id,
        "manifest_sha256": record.pair_manifest.sha256,
        "max_mated_pairs_per_subject": metadata["max_mated_pairs_per_subject"],
        "nonmated_multiple": metadata["nonmated_multiple"],
        "pairs_sha256": record.pairs_sha256,
        "project_id": record.project_id,
        "protocol_id": record.protocol_id,
        "seed": metadata["seed"],
    }
    target_name = document["target_name"]
    stage_name = document["stage_name"]
    manifest_path = protocol_root / str(target_name)
    if (
        request != expected_request
        or not isinstance(target_name, str)
        or Path(target_name).name != target_name
        or target_name != f"{record.protocol_id.replace(':', '_')}.jsonl"
        or receipt_path.name != f".{target_name}.prepared.json"
        or not isinstance(stage_name, str)
        or stage_name != f".{target_name}.{request_sha256}.partial"
        or record.pair_manifest.path != str(manifest_path)
        or not pairs
        or len(pairs) != record.pair_count
        or {pair.protocol_id for pair in pairs} != {record.protocol_id}
        or audit_event.project_id != record.project_id
        or audit_event.subject_type != "VerificationProtocol"
        or audit_event.subject_id != record.protocol_id
        or audit_event.event_type is not AuditEventType.CONFIGURATION_CHANGED
        or audit_event.current_evidence_sha256
        != sha256_bytes(record.model_dump_json().encode("utf-8"))
    ):
        raise OwnerSuppliedError("owner protocol receipt identity differs")
    digest = hashlib.sha256()
    for pair in pairs:
        digest.update(pair.model_dump_json().encode("utf-8"))
        digest.update(b"\n")
    if digest.hexdigest() != record.pairs_sha256:
        raise OwnerSuppliedError("owner protocol pair digest differs")
    expected_manifest = _owner_protocol_manifest_payload(record, pairs)
    if (
        sha256_bytes(expected_manifest) != record.pair_manifest.sha256
        or len(expected_manifest) != record.pair_manifest.size_bytes
    ):
        raise OwnerSuppliedError("owner protocol manifest payload differs")
    return PreparedOwnerProtocol(
        receipt_path=receipt_path,
        stage_path=protocol_root / stage_name,
        manifest_path=manifest_path,
        request_sha256=request_sha256,
        record=record,
        pairs=pairs,
        audit_event=audit_event,
    )


def _validate_protocol_manifest(
    path: Path, *, identity: ArtifactIdentity
) -> bool:
    validated = _read_validated_regular_bytes(
        path, sha256=identity.sha256, size_bytes=identity.size_bytes
    )
    if validated is None:
        return False
    _, observed = validated
    if observed.st_mtime_ns != identity.mtime_ns:
        raise OwnerSuppliedError("owner protocol manifest timestamp differs")
    return True


def recover_prepared_owner_protocol(
    receipt_path: Path, *, data_dir: Path
) -> PreparedOwnerProtocol:
    """Verify and finish one project-bound owner protocol publication."""

    prepared = _load_owner_protocol_receipt(receipt_path, data_dir=data_dir)
    identity = prepared.record.pair_manifest
    if not _validate_protocol_manifest(prepared.manifest_path, identity=identity):
        if not _validate_protocol_manifest(prepared.stage_path, identity=identity):
            raise OwnerSuppliedError("prepared owner protocol manifest is unavailable")
        _publish_regular_file_no_clobber(
            prepared.stage_path, prepared.manifest_path
        )
        if not _validate_protocol_manifest(prepared.manifest_path, identity=identity):
            raise OwnerSuppliedError("published owner protocol manifest is unavailable")
    return prepared


def inspect_prepared_owner_protocol(
    receipt_path: Path, *, data_dir: Path
) -> PreparedOwnerProtocol:
    """Validate protocol receipt identity without filesystem mutation."""

    return _load_owner_protocol_receipt(receipt_path, data_dir=data_dir)


def finalize_prepared_owner_protocol(prepared: PreparedOwnerProtocol) -> None:
    """Remove only preparation evidence after protocol/audit commit atomically."""

    for path in (prepared.stage_path, prepared.receipt_path):
        try:
            observed = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(observed.st_mode):
            raise OwnerSuppliedError("owner protocol cleanup target is invalid")
        path.unlink()
        _sync_directory(path.parent)


def _write_protocol_stage(
    path: Path, payload: bytes, *, identity: ArtifactIdentity
) -> None:
    if _validate_protocol_manifest(path, identity=identity):
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    complete = False
    try:
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("owner protocol staging write made no progress")
            written += count
        os.fsync(descriptor)
        os.utime(descriptor, ns=(identity.mtime_ns, identity.mtime_ns))
        os.fsync(descriptor)
        complete = True
    finally:
        os.close(descriptor)
        if not complete:
            path.unlink(missing_ok=True)
    _sync_directory(path.parent)
    if not _validate_protocol_manifest(path, identity=identity):
        raise OwnerSuppliedError("owner protocol staging verification failed")


def _prepare_owner_protocol(
    *,
    data_dir: Path,
    request: dict[str, object],
    record: VerificationProtocolRecord,
    pairs: list[ProtocolPair],
    audit_event: AuditEvent,
    manifest_payload: bytes,
) -> PreparedOwnerProtocol:
    protocol_root = data_dir / "protocols"
    protocol_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _sync_directory(protocol_root.parent)
    target_name = Path(record.pair_manifest.path).name
    request_sha256 = CanonicalJson.from_value(request).sha256
    stage_name = f".{target_name}.{request_sha256}.partial"
    receipt_path = protocol_root / f".{target_name}.prepared.json"
    if not receipt_path.exists() and not receipt_path.is_symlink():
        _write_canonical_receipt(
            receipt_path,
            {
                "audit_event": audit_event.model_dump(mode="json"),
                "pairs": [pair.model_dump(mode="json") for pair in pairs],
                "record": record.model_dump(mode="json"),
                "request": request,
                "request_sha256": request_sha256,
                "schema_version": _OWNER_PROTOCOL_RECEIPT_SCHEMA_VERSION,
                "stage_name": stage_name,
                "target_name": target_name,
            },
        )
    prepared = _load_owner_protocol_receipt(receipt_path, data_dir=data_dir)
    if prepared.request_sha256 != request_sha256:
        raise OwnerSuppliedError("owner protocol receipt differs from its request")
    if not _validate_protocol_manifest(
        prepared.manifest_path, identity=prepared.record.pair_manifest
    ):
        _write_protocol_stage(
            prepared.stage_path,
            manifest_payload,
            identity=prepared.record.pair_manifest,
        )
    return recover_prepared_owner_protocol(receipt_path, data_dir=data_dir)


def freeze_owner_pairs(
    store: RunStore,
    project: Project,
    dataset: Dataset,
    *,
    data_dir: Path,
    actor_id: str,
    seed: int,
    max_pairs: int,
    max_mated_pairs_per_subject: int = 6,
    nonmated_multiple: float = 1.0,
) -> VerificationProtocolRecord:
    descriptor = json.loads(dataset.manifest.text)
    if descriptor.get("source_kind") != SOURCE_KIND:
        raise OwnerSuppliedError("owner pair protocols are defined only over owner-supplied datasets")
    samples = store.samples_for_dataset(
        dataset.dataset_id, limit=OWNER_PAIR_MAX_SOURCE_SAMPLES + 1
    )
    if len(samples) > OWNER_PAIR_MAX_SOURCE_SAMPLES:
        raise OwnerSuppliedError(
            "owner pair protocol exceeds the beta source limit of "
            f"{OWNER_PAIR_MAX_SOURCE_SAMPLES:,} samples"
        )
    by_subject: dict[str, list[Sample]] = {}
    for sample in sorted(samples, key=lambda item: item.source_path):
        subject = sample.subject_id.value or ""
        if not subject:
            raise OwnerSuppliedError(f"sample {sample.sample_id} has no subject label")
        by_subject.setdefault(subject, []).append(sample)
    subjects = sorted(by_subject)
    expected_mated, target_nonmated = _owner_pair_preflight(
        {subject: len(items) for subject, items in by_subject.items()},
        max_mated_pairs_per_subject=max_mated_pairs_per_subject,
        nonmated_multiple=nonmated_multiple,
        max_pairs=max_pairs,
    )
    rng = random.Random(seed)
    mated: list[tuple[Sample, Sample]] = []
    for subject in subjects:
        items = by_subject[subject]
        mated.extend(
            _mated_pairs_for_subject(
                items, maximum=max_mated_pairs_per_subject, rng=rng
            )
        )
    if len(mated) != expected_mated:
        raise OwnerSuppliedError("owner pair preflight count changed during generation")
    nonmated: list[tuple[Sample, Sample]] = []
    seen: set[tuple[str, str]] = set()
    attempts = 0
    while len(nonmated) < target_nonmated and attempts < target_nonmated * 200:
        attempts += 1
        left_subject, right_subject = rng.sample(subjects, 2)
        left = rng.choice(by_subject[left_subject])
        right = rng.choice(by_subject[right_subject])
        key = (left.sample_id, right.sample_id)
        if key in seen:
            continue
        seen.add(key)
        nonmated.append((left, right))
    if len(nonmated) < target_nonmated:
        raise OwnerSuppliedError("could not draw the requested number of distinct non-mated pairs")
    protocol_id = content_id(
        "protocol", project.project_id, dataset.dataset_id,
        f"seed={seed};mated_cap={max_mated_pairs_per_subject};nonmated_multiple={nonmated_multiple}",
        "OWNER_PAIRS_V1",
    )
    pairs: list[ProtocolPair] = []
    digest = hashlib.sha256()
    ordered = [(ComparisonLabel.MATED, pair) for pair in mated] + [(ComparisonLabel.NON_MATED, pair) for pair in nonmated]
    for order, (label, (left, right)) in enumerate(ordered, start=1):
        pair = ProtocolPair(
            pair_id=f"owner-{order:06d}",
            protocol_id=protocol_id,
            fold=1,
            source_order=order,
            label=label,
            left_sample_id=left.sample_id,
            right_sample_id=right.sample_id,
            left_subject_id=left.subject_id.value or "",
            right_subject_id=right.subject_id.value or "",
            left_sha256=left.sha256,
            right_sha256=right.sha256,
        )
        pairs.append(pair)
        digest.update(pair.model_dump_json().encode("utf-8"))
        digest.update(b"\n")
    sample_ids = {p.left_sample_id for p in pairs} | {p.right_sample_id for p in pairs}
    # The frozen pair list is itself the pair manifest: written once, hashed, bound.
    existing = store.protocol(protocol_id)
    if existing is not None:
        return existing
    manifest_dir = data_dir / "protocols"
    manifest_path = manifest_dir / f"{protocol_id.replace(':', '_')}.jsonl"
    created_at = _now()
    created_mtime_ns = (
        int(created_at.timestamp()) * 1_000_000_000
        + created_at.microsecond * 1_000
    )
    metadata = CanonicalJson.from_value(
        {
            "seed": seed,
            "max_mated_pairs_per_subject": max_mated_pairs_per_subject,
            "nonmated_multiple": nonmated_multiple,
            "source_kind": SOURCE_KIND,
            "generator": "qualsight.importers.owner_supplied.freeze_owner_pairs@1.0.0",
        }
    )
    provisional_identity = ArtifactIdentity(
        path=str(manifest_path),
        sha256="0" * 64,
        size_bytes=1,
        mtime_ns=created_mtime_ns,
    )
    record = VerificationProtocolRecord(
        protocol_id=protocol_id,
        project_id=project.project_id,
        dataset_id=dataset.dataset_id,
        name=f"Owner-supplied pairs (seed {seed}) — {dataset.name}",
        pair_policy=PairPolicy.ALL_CROSS_SUBJECT_PAIRS,
        category=ProtocolCategory.NOT_APPLICABLE,
        pair_manifest=provisional_identity,
        protocol_authority=(),
        pair_count=len(pairs),
        mated_count=len(mated),
        nonmated_count=len(nonmated),
        fold_count=1,
        unique_sample_count=len(sample_ids),
        unique_subject_count=len(subjects),
        pairs_sha256=digest.hexdigest(),
        limitations=(
            "The pair list is generated by QualSight from operator-declared identity labels (decision 30), not by an official protocol authority.",
            f"Generation rule: exhaustive within-subject mated pairs capped at {max_mated_pairs_per_subject} per subject; non-mated pairs drawn without replacement by seeded PRNG (seed {seed}).",
            "Identity labels are the operator's claim; QualSight has not verified them against any authority.",
            "No comparison, score, or threshold exists until a matcher Run executes over this protocol.",
        ),
        created_at=created_at,
        metadata=metadata,
    )
    manifest_payload = _owner_protocol_manifest_payload(record, pairs)
    manifest_sha256 = sha256_bytes(manifest_payload)
    manifest_mtime_ns = created_mtime_ns
    existing_manifest = _read_validated_regular_bytes(
        manifest_path,
        sha256=manifest_sha256,
        size_bytes=len(manifest_payload),
    )
    if existing_manifest is not None:
        _, existing_stat = existing_manifest
        manifest_mtime_ns = existing_stat.st_mtime_ns
    manifest_identity = ArtifactIdentity(
        path=str(manifest_path),
        sha256=manifest_sha256,
        size_bytes=len(manifest_payload),
        mtime_ns=manifest_mtime_ns,
    )
    record = record.model_copy(update={"pair_manifest": manifest_identity})
    evidence = sha256_bytes(record.model_dump_json().encode("utf-8"))
    audit_event = AuditEvent(
        audit_event_id=content_id("audit-event", protocol_id, "PROTOCOL_FROZEN", evidence, record.created_at.isoformat()),
        project_id=project.project_id,
        actor_id=actor_id,
        event_type=AuditEventType.CONFIGURATION_CHANGED,
        subject_type="VerificationProtocol",
        subject_id=protocol_id,
        occurred_at=record.created_at,
        details=CanonicalJson.from_value(
            {"pairs": len(pairs), "mated": len(mated), "pairs_sha256": record.pairs_sha256, "seed": seed}
        ),
        current_evidence_sha256=evidence,
        local_only=True,
    )
    request = {
        "actor_id": actor_id,
        "dataset_id": dataset.dataset_id,
        "manifest_sha256": manifest_identity.sha256,
        "max_mated_pairs_per_subject": max_mated_pairs_per_subject,
        "nonmated_multiple": nonmated_multiple,
        "pairs_sha256": record.pairs_sha256,
        "project_id": project.project_id,
        "protocol_id": protocol_id,
        "seed": seed,
    }
    prepared = _prepare_owner_protocol(
        data_dir=data_dir,
        request=request,
        record=record,
        pairs=pairs,
        audit_event=audit_event,
        manifest_payload=manifest_payload,
    )
    store.record_protocol(
        prepared.record, list(prepared.pairs), prepared.audit_event
    )
    finalize_prepared_owner_protocol(prepared)
    return prepared.record
