"""Hash-pinned acquisition of the author-published face-verification archive."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile
from typing import Any, BinaryIO, Iterable
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

import cv2

from .matchers.acquisition import _StrictRedirectHandler


MANIFEST_NAME = "verification-dataset-acquisition-v1.json"
CHUNK_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
EXTRACTION_MANIFEST_NAME = "extracted-files-v1.json"


class DatasetAcquisitionError(RuntimeError):
    """Raised when the dataset archive or extracted protocol bytes are unsafe."""


@dataclass(frozen=True, slots=True)
class VerificationProtocol:
    addon_id: str
    display_name: str
    archive_directory: str
    sample_count: int
    pair_count: int
    mated_pair_count: int
    nonmated_pair_count: int
    image_list_sha256: str
    pair_list_sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise DatasetAcquisitionError(f"managed path is not a real directory: {path}")
    os.chmod(path, 0o700)


class VerificationDatasetStore:
    """Download, verify, and safely extract the three published pair protocols."""

    def __init__(self, manifest_path: Path, data_dir: Path) -> None:
        self.manifest_path = manifest_path.resolve(strict=True)
        self.repository_root = self.manifest_path.parents[2]
        self.data_dir = data_dir.resolve(strict=False)
        raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if set(raw) != {"schema_version", "manifest_id", "policy", "archive", "protocols"}:
            raise DatasetAcquisitionError("verification dataset manifest fields differ")
        if raw["schema_version"] != "1.0.0" or raw["manifest_id"] != "verification-dataset-acquisition-v1":
            raise DatasetAcquisitionError("verification dataset manifest identity differs")
        archive = raw["archive"]
        required_archive = {
            "published_url", "url", "host", "allowed_redirect_hosts", "filename",
            "sha256", "size_bytes", "managed_relative_path", "observed_at",
            "observed_content_type", "observed_content_disposition",
            "observed_last_modified", "evidence_path", "evidence_sha256",
        }
        if not isinstance(archive, dict) or set(archive) != required_archive:
            raise DatasetAcquisitionError("verification archive fields differ")
        parsed = urlsplit(archive["url"])
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            parsed.scheme != "https"
            or parsed.hostname != archive["host"]
            or parsed.path != "/download"
            or set(query) != {"id", "export", "confirm"}
            or query["export"] != ["download"]
            or query["confirm"] != ["t"]
        ):
            raise DatasetAcquisitionError("verification archive URL is not the exact HTTPS download endpoint")
        if archive["size_bytes"] < 1 or archive["size_bytes"] > MAX_ARCHIVE_BYTES:
            raise DatasetAcquisitionError("verification archive byte bound is invalid")
        if len(archive["sha256"]) != 64:
            raise DatasetAcquisitionError("verification archive SHA-256 is invalid")
        evidence = self.repository_root / archive["evidence_path"]
        if _sha256(evidence) != archive["evidence_sha256"]:
            raise DatasetAcquisitionError("verification archive source evidence drifted")
        disposition = Message()
        disposition["content-disposition"] = archive["observed_content_disposition"]
        if disposition.get_filename() != archive["filename"]:
            raise DatasetAcquisitionError("verification archive observed filename differs")
        self.archive = archive
        protocols: dict[str, VerificationProtocol] = {}
        protocol_fields = {
            "addon_id", "display_name", "archive_directory", "sample_count",
            "pair_count", "mated_pair_count", "nonmated_pair_count",
            "image_list_sha256", "pair_list_sha256",
        }
        for raw_protocol in raw["protocols"]:
            if not isinstance(raw_protocol, dict) or set(raw_protocol) != protocol_fields:
                raise DatasetAcquisitionError("verification protocol fields differ")
            protocol = VerificationProtocol(**raw_protocol)
            if protocol.addon_id in protocols:
                raise DatasetAcquisitionError(f"duplicate verification protocol {protocol.addon_id}")
            if protocol.pair_count != protocol.mated_pair_count + protocol.nonmated_pair_count:
                raise DatasetAcquisitionError(f"pair counts differ for {protocol.addon_id}")
            protocols[protocol.addon_id] = protocol
        if set(protocols) != {
            "magface-lfw-verification-v1",
            "magface-cfp-fp-verification-v1",
            "magface-agedb-30-verification-v1",
        }:
            raise DatasetAcquisitionError("verification protocol set differs")
        self.protocols = protocols

    @property
    def archive_path(self) -> Path:
        relative = Path(self.archive["managed_relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise DatasetAcquisitionError("managed archive path is unsafe")
        return self.data_dir / relative

    @property
    def extraction_root(self) -> Path:
        return self.data_dir / "datasets" / "magface-verification-v1"

    @property
    def extraction_manifest_path(self) -> Path:
        return self._extraction_manifest_path()

    def protocol_root(self, addon_id: str) -> Path:
        protocol = self._protocol(addon_id)
        return self.extraction_root / "data" / protocol.archive_directory

    def _extraction_manifest_path(self, base: Path | None = None) -> Path:
        return (base or self.extraction_root) / EXTRACTION_MANIFEST_NAME

    def _expected_extracted_paths(self, base: Path) -> list[Path]:
        paths: list[Path] = []
        for protocol in sorted(self.protocols.values(), key=lambda item: item.addon_id):
            root = base / "data" / protocol.archive_directory
            paths.extend((root / "img.list", root / "pair.list"))
            paths.extend(root / "imgs" / f"{index}.jpg" for index in range(protocol.sample_count))
        return paths

    def _write_extraction_manifest(self, base: Path) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for path in self._expected_extracted_paths(base):
            observed = path.lstat()
            if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
                raise DatasetAcquisitionError(f"cannot manifest non-regular extracted file: {path}")
            entries.append(
                {
                    "path": path.relative_to(base).as_posix(),
                    "sha256": _sha256(path),
                    "size_bytes": observed.st_size,
                }
            )
        document = {
            "schema_version": "1.0.0",
            "archive_sha256": self.archive["sha256"],
            "files": entries,
        }
        payload = (
            json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        path = self._extraction_manifest_path(base)
        try:
            with path.open("xb") as handle:
                os.chmod(path, 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            if path.read_bytes() != payload:
                raise DatasetAcquisitionError("existing extracted-file manifest differs")
        return {
            "path": str(path),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "file_count": len(entries),
        }

    def _verify_extraction_manifest(self, base: Path, *, verify_bytes: bool) -> dict[str, Any]:
        path = self._extraction_manifest_path(base)
        try:
            observed = path.lstat()
            if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
                raise OSError("not a regular file")
            payload = path.read_bytes()
            document = json.loads(payload)
        except (OSError, json.JSONDecodeError) as error:
            return {"state": "DRIFTED", "reason": f"extracted-file manifest is unavailable: {error}"}
        if (
            not isinstance(document, dict)
            or set(document) != {"schema_version", "archive_sha256", "files"}
            or document.get("schema_version") != "1.0.0"
            or document.get("archive_sha256") != self.archive["sha256"]
            or not isinstance(document.get("files"), list)
        ):
            return {"state": "DRIFTED", "reason": "extracted-file manifest identity differs"}
        expected_relative = [item.relative_to(base).as_posix() for item in self._expected_extracted_paths(base)]
        actual_relative = sorted(
            item.relative_to(base).as_posix()
            for item in base.rglob("*")
            if item.is_file() or item.is_symlink()
        )
        if actual_relative != sorted([*expected_relative, EXTRACTION_MANIFEST_NAME]):
            return {"state": "DRIFTED", "reason": "extracted file set differs from the manifest authority"}
        entries = document["files"]
        if [item.get("path") for item in entries if isinstance(item, dict)] != expected_relative:
            return {"state": "DRIFTED", "reason": "extracted-file manifest path set or order differs"}
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"path", "sha256", "size_bytes"}
                or not isinstance(entry["sha256"], str)
                or len(entry["sha256"]) != 64
                or not isinstance(entry["size_bytes"], int)
                or entry["size_bytes"] < 1
            ):
                return {"state": "DRIFTED", "reason": "extracted-file manifest entry differs"}
            file_path = base / PurePosixPath(entry["path"])
            try:
                file_stat = file_path.lstat()
            except OSError:
                return {"state": "DRIFTED", "reason": f"manifested file is missing: {entry['path']}"}
            if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
                return {"state": "DRIFTED", "reason": f"manifested file is not regular: {entry['path']}"}
            if file_stat.st_size != entry["size_bytes"]:
                return {"state": "DRIFTED", "reason": f"manifested file size differs: {entry['path']}"}
            if verify_bytes and _sha256(file_path) != entry["sha256"]:
                return {"state": "DRIFTED", "reason": f"manifested file hash differs: {entry['path']}"}
            if verify_bytes and entry["path"].endswith(".jpg"):
                image = cv2.imread(str(file_path), cv2.IMREAD_COLOR)
                if image is None or image.shape != (112, 112, 3):
                    return {"state": "DRIFTED", "reason": f"manifested image is not a decoded 112x112 BGR image: {entry['path']}"}
        return {
            "state": "VERIFIED",
            "reason": None,
            "path": str(path),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "file_count": len(entries),
        }

    def _protocol(self, addon_id: str) -> VerificationProtocol:
        try:
            return self.protocols[addon_id]
        except KeyError as error:
            raise DatasetAcquisitionError(f"unknown verification dataset: {addon_id}") from error

    def _archive_state(self) -> str:
        path = self.archive_path
        if path.is_symlink():
            return "DRIFTED"
        if not path.exists():
            return "MISSING"
        observed = path.stat()
        if not stat.S_ISREG(observed.st_mode) or observed.st_size != self.archive["size_bytes"]:
            return "DRIFTED"
        return "VERIFIED" if _sha256(path) == self.archive["sha256"] else "DRIFTED"

    def _validate_protocol(
        self,
        protocol: VerificationProtocol,
        *,
        verify_images: bool,
        extraction_root: Path | None = None,
        require_manifest: bool = True,
    ) -> dict[str, Any]:
        base = extraction_root or self.extraction_root
        if not base.exists():
            return {"state": "MISSING", "reason": "extracted protocol files are missing"}
        if require_manifest:
            manifest = self._verify_extraction_manifest(base, verify_bytes=verify_images)
            if manifest["state"] != "VERIFIED":
                return manifest
        root = base / "data" / protocol.archive_directory
        image_list = root / "img.list"
        pair_list = root / "pair.list"
        if not root.is_dir() or root.is_symlink() or not image_list.is_file() or not pair_list.is_file():
            return {"state": "MISSING", "reason": "extracted protocol files are missing"}
        if _sha256(image_list) != protocol.image_list_sha256 or _sha256(pair_list) != protocol.pair_list_sha256:
            return {"state": "DRIFTED", "reason": "protocol list bytes differ"}
        image_lines = image_list.read_text(encoding="utf-8").splitlines()
        pair_lines = pair_list.read_text(encoding="utf-8").splitlines()
        if len(image_lines) != protocol.sample_count or len(pair_lines) != protocol.pair_count:
            return {"state": "DRIFTED", "reason": "protocol list counts differ"}
        expected_prefix = f"data/{protocol.archive_directory}/imgs/"
        expected_images: list[Path] = []
        for index, line in enumerate(image_lines):
            if line != f"{expected_prefix}{index}.jpg":
                return {"state": "DRIFTED", "reason": f"image list order differs at row {index}"}
            expected_images.append(base / line)
        labels = {0: 0, 1: 0}
        for row_number, line in enumerate(pair_lines):
            fields = line.split()
            if len(fields) != 3 or not all(field.isdigit() for field in fields):
                return {"state": "DRIFTED", "reason": f"invalid pair row {row_number}"}
            left, right, label = map(int, fields)
            if left >= protocol.sample_count or right >= protocol.sample_count or label not in labels:
                return {"state": "DRIFTED", "reason": f"out-of-range pair row {row_number}"}
            labels[label] += 1
        if labels != {0: protocol.nonmated_pair_count, 1: protocol.mated_pair_count}:
            return {"state": "DRIFTED", "reason": "pair label counts differ"}
        if verify_images:
            for image_path in expected_images:
                try:
                    image_stat = image_path.lstat()
                except OSError:
                    return {"state": "DRIFTED", "reason": f"image is missing: {image_path.name}"}
                if stat.S_ISLNK(image_stat.st_mode) or not stat.S_ISREG(image_stat.st_mode) or image_stat.st_size < 4:
                    return {"state": "DRIFTED", "reason": f"image is not a regular JPEG: {image_path.name}"}
                image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
                if image is None or image.shape != (112, 112, 3):
                    return {"state": "DRIFTED", "reason": f"image is not a decoded 112x112 BGR image: {image_path.name}"}
        return {
            "state": "VERIFIED",
            "reason": None,
            "sample_count": protocol.sample_count,
            "pair_count": protocol.pair_count,
            "root": str(root),
        }

    def status(self, addon_id: str, *, verify_images: bool = False) -> dict[str, Any]:
        protocol = self._protocol(addon_id)
        archive_state = self._archive_state()
        extracted = self._validate_protocol(protocol, verify_images=verify_images)
        runtime_state = "READY" if archive_state == "VERIFIED" and extracted["state"] == "VERIFIED" else "MISSING"
        if archive_state == "DRIFTED" or extracted["state"] == "DRIFTED":
            runtime_state = "DRIFTED"
        return {
            "addon_id": protocol.addon_id,
            "display_name": protocol.display_name,
            "archive_directory": protocol.archive_directory,
            "sample_count": protocol.sample_count,
            "pair_count": protocol.pair_count,
            "mated_pair_count": protocol.mated_pair_count,
            "nonmated_pair_count": protocol.nonmated_pair_count,
            "archive_state": archive_state,
            "extraction": extracted,
            "runtime_state": runtime_state,
        }

    @staticmethod
    def _iter_response(response: Any) -> Iterable[bytes]:
        while True:
            block = response.read(CHUNK_BYTES)
            if not block:
                return
            yield block

    def _download(self) -> tuple[str, int, dict[str, Any]]:
        target = self.archive_path
        _private_directory(self.data_dir)
        _private_directory(target.parent)
        redirect_handler = _StrictRedirectHandler(tuple(self.archive["allowed_redirect_hosts"]))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), redirect_handler)
        request = urllib.request.Request(
            self.archive["url"],
            headers={"User-Agent": "QualSight-dataset-setup/0.1", "Accept": "application/octet-stream", "Accept-Encoding": "identity"},
            method="GET",
        )
        partial: Path | None = None
        handle: BinaryIO | None = None
        try:
            with opener.open(request, timeout=120) as response:
                if int(response.status) != 200 or response.geturl() != self.archive["url"]:
                    raise DatasetAcquisitionError("verification archive endpoint identity differs")
                content_length = int(response.headers.get("Content-Length", "-1"))
                content_type = response.headers.get_content_type()
                disposition = Message()
                disposition["content-disposition"] = response.headers.get("Content-Disposition", "")
                if (
                    content_length != self.archive["size_bytes"]
                    or content_type != "application/octet-stream"
                    or disposition.get_filename() != self.archive["filename"]
                    or tuple(redirect_handler.observed_hosts) != tuple(self.archive["allowed_redirect_hosts"])
                ):
                    raise DatasetAcquisitionError("verification archive response headers differ")
                handle = tempfile.NamedTemporaryFile("w+b", prefix=".dataset.", suffix=".partial", dir=target.parent, delete=False)
                partial = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                digest = hashlib.sha256()
                size = 0
                for block in self._iter_response(response):
                    size += len(block)
                    if size > self.archive["size_bytes"]:
                        raise DatasetAcquisitionError("verification archive exceeded its byte bound")
                    digest.update(block)
                    handle.write(block)
                if size != self.archive["size_bytes"] or digest.hexdigest() != self.archive["sha256"]:
                    raise DatasetAcquisitionError("verification archive bytes differ from the pin")
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
                handle = None
                try:
                    os.link(partial, target, follow_symlinks=False)
                    action = "ACQUIRED_OFFICIAL_HTTPS"
                except FileExistsError:
                    if self._archive_state() != "VERIFIED":
                        raise DatasetAcquisitionError("managed verification archive is drifted; refusing to overwrite")
                    action = "REUSED_VERIFIED_ARCHIVE"
                partial.unlink()
                partial = None
                return action, size, {
                    "method": "HTTPS_GET_STREAM",
                    "network_used": True,
                    "final_url": response.geturl(),
                    "content_length": content_length,
                    "content_type": content_type,
                    "content_disposition": response.headers.get("Content-Disposition", ""),
                    "last_modified": response.headers.get("Last-Modified"),
                }
        except DatasetAcquisitionError:
            raise
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as error:
            raise DatasetAcquisitionError(f"verification archive HTTPS acquisition failed: {error}") from error
        finally:
            if handle is not None:
                handle.close()
            if partial is not None:
                partial.unlink(missing_ok=True)

    def _extract(self) -> str:
        if self._archive_state() != "VERIFIED":
            raise DatasetAcquisitionError("verification archive is not verified")
        existing = [
            self._validate_protocol(protocol, verify_images=True, require_manifest=False)
            for protocol in self.protocols.values()
        ]
        if all(item["state"] == "VERIFIED" for item in existing):
            manifest_path = self._extraction_manifest_path()
            if not manifest_path.exists():
                self._write_extraction_manifest(self.extraction_root)
                action = "ADDED_VERIFIED_EXTRACTION_MANIFEST"
            else:
                action = "REUSED_VERIFIED_EXTRACTION"
            manifest = self._verify_extraction_manifest(self.extraction_root, verify_bytes=True)
            if manifest["state"] != "VERIFIED":
                raise DatasetAcquisitionError(f"managed verification extraction drifted: {manifest['reason']}")
            return action
        if self.extraction_root.exists() or self.extraction_root.is_symlink():
            raise DatasetAcquisitionError("managed verification extraction is incomplete or drifted; refusing to overwrite")
        parent = self.extraction_root.parent
        _private_directory(parent)
        temporary = Path(tempfile.mkdtemp(prefix=".magface-verification.", dir=parent))
        allowed_directories = {protocol.archive_directory for protocol in self.protocols.values()}
        try:
            with tarfile.open(self.archive_path, mode="r:") as archive:
                for member in archive:
                    pure = PurePosixPath(member.name)
                    if pure.is_absolute() or ".." in pure.parts or "\\" in member.name:
                        raise DatasetAcquisitionError(f"unsafe archive member: {member.name}")
                    if len(pure.parts) < 2 or pure.parts[0] != "data" or pure.parts[1] not in allowed_directories:
                        continue
                    relative_parts = pure.parts[2:]
                    is_required_file = (
                        len(relative_parts) == 1 and relative_parts[0] in {"img.list", "pair.list"}
                    ) or (
                        len(relative_parts) == 2
                        and relative_parts[0] == "imgs"
                        and relative_parts[1].endswith(".jpg")
                        and relative_parts[1][:-4].isdigit()
                    )
                    is_required_directory = member.isdir() and (
                        len(relative_parts) == 0 or relative_parts == ("imgs",)
                    )
                    if not is_required_file and not is_required_directory:
                        continue
                    target = temporary.joinpath(*pure.parts)
                    if is_required_directory:
                        _private_directory(target)
                        continue
                    if not member.isfile():
                        raise DatasetAcquisitionError(f"required archive member is not regular: {member.name}")
                    _private_directory(target.parent)
                    source = archive.extractfile(member)
                    if source is None:
                        raise DatasetAcquisitionError(f"cannot read archive member: {member.name}")
                    with target.open("xb") as output:
                        os.chmod(target, 0o600)
                        shutil.copyfileobj(source, output, CHUNK_BYTES)
            for protocol in self.protocols.values():
                result = self._validate_protocol(
                    protocol,
                    verify_images=True,
                    extraction_root=temporary,
                    require_manifest=False,
                )
                if result["state"] != "VERIFIED":
                    raise DatasetAcquisitionError(
                        f"extracted {protocol.addon_id} is invalid: {result['reason']}"
                    )
            self._write_extraction_manifest(temporary)
            manifest = self._verify_extraction_manifest(temporary, verify_bytes=True)
            if manifest["state"] != "VERIFIED":
                raise DatasetAcquisitionError(f"extracted-file manifest verification failed: {manifest['reason']}")
            os.rename(temporary, self.extraction_root)
            return "EXTRACTED_VERIFIED_PROTOCOLS"
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _receipt(self, action: str, extraction_action: str, transport: dict[str, Any]) -> dict[str, Any]:
        extraction_manifest = self._verify_extraction_manifest(self.extraction_root, verify_bytes=True)
        if extraction_manifest["state"] != "VERIFIED":
            raise DatasetAcquisitionError(
                f"cannot record acquisition receipt for drifted extraction: {extraction_manifest['reason']}"
            )
        core = {
            "schema_version": "1.0.0",
            "receipt_type": "VERIFICATION_DATASET_ACQUISITION",
            "recorded_at": _now(),
            "action": action,
            "extraction_action": extraction_action,
            "archive": {
                "path": str(self.archive_path),
                "sha256": self.archive["sha256"],
                "size_bytes": self.archive["size_bytes"],
            },
            "extraction_manifest": {
                "path": extraction_manifest["path"],
                "sha256": extraction_manifest["sha256"],
                "file_count": extraction_manifest["file_count"],
            },
            "protocols": [self.status(addon_id, verify_images=True) for addon_id in self.protocols],
            "transport": transport,
        }
        canonical = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        evidence_sha256 = hashlib.sha256(canonical).hexdigest()
        receipt = {**core, "receipt_id": f"dataset-receipt:sha256:{evidence_sha256}", "evidence_sha256": evidence_sha256}
        receipt_dir = self.data_dir / "acquisition-receipts" / "datasets"
        _private_directory(receipt_dir)
        receipt_path = receipt_dir / f"{evidence_sha256}.json"
        payload = (json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with receipt_path.open("xb") as handle:
                os.chmod(receipt_path, 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            if receipt_path.read_bytes() != payload:
                raise DatasetAcquisitionError("existing dataset acquisition receipt differs")
        return {**receipt, "path": str(receipt_path)}

    def acquire(self) -> dict[str, Any]:
        archive_state = self._archive_state()
        if archive_state == "DRIFTED":
            raise DatasetAcquisitionError("managed verification archive is drifted; refusing to overwrite")
        if archive_state == "VERIFIED":
            action = "REUSED_VERIFIED_ARCHIVE"
            transport = {"method": "LOCAL_VERIFIED_REUSE", "network_used": False}
        else:
            action, _size, transport = self._download()
        extraction_action = self._extract()
        return self._receipt(action, extraction_action, transport)
