"""Rights-gated, hash-pinned checkpoint acquisition for the closed matcher catalog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, BinaryIO, Iterable, Mapping
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

from ..execution.registry_binding import ResourceRegistry


MANIFEST_NAME = "matcher-checkpoint-acquisition-v1.json"
MANIFEST_SCHEMA_VERSION = "1.0.0"
MANIFEST_ID = "matcher-checkpoint-acquisition-v1"
ADMITTED_STATE = "ADMITTED_LOCAL_NONCOMMERCIAL"
BLOCKED_STATE = "BLOCKED_THIRD_PARTY_RIGHTS"
EXPECTED_RESOURCE_IDS = frozenset(
    {
        "adaface-ir101-local-candidate",
        "elasticface-arc-r100-local-candidate",
        "magface-iresnet100-ms1mv2-local-candidate",
        "topofr-r100-glint360k-local-candidate",
        "transface-b-glint360k-local-candidate",
        "transface-s-glint360k-local-candidate",
    }
)
ADMITTED_RESOURCE_IDS = EXPECTED_RESOURCE_IDS
CHUNK_BYTES = 1024 * 1024
MAX_CHECKPOINT_BYTES = 1024 * 1024 * 1024


class CheckpointManifestError(ValueError):
    """Raised when the versioned acquisition contract is not exact and safe."""


class CheckpointAcquisitionError(RuntimeError):
    """Raised when a checkpoint cannot be safely resolved or acquired."""


@dataclass(frozen=True, slots=True)
class PublishedEvidence:
    repository_url: str
    repository_commit: str
    repository_relative_path: str
    artifact_sha256: str
    artifact_size_bytes: int
    published_url: str
    checkpoint_label: str
    resolution_method: str
    author_link_id: str
    resolved_file_id: str
    resolved_file_view_url: str
    resolution_note: str


@dataclass(frozen=True, slots=True)
class CheckpointSource:
    url: str
    host: str
    allowed_redirect_hosts: tuple[str, ...]
    observed_at: str
    observed_http_status: int
    observed_final_url: str
    observed_redirect_hosts: tuple[str, ...]
    observed_content_type: str
    observed_content_disposition: str
    observed_content_length: int
    observed_last_modified: str


@dataclass(frozen=True, slots=True)
class CheckpointArtifact:
    filename: str
    sha256: str
    size_bytes: int
    managed_relative_path: str


@dataclass(frozen=True, slots=True)
class CheckpointEntry:
    resource_id: str
    admission_state: str
    admission_reason: str
    third_party_rights_status: str
    published_evidence: PublishedEvidence
    source: CheckpointSource
    artifact: CheckpointArtifact

    @property
    def admitted(self) -> bool:
        return self.admission_state == ADMITTED_STATE


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CheckpointManifestError(f"{location} must be an object")
    return value


def _sequence(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise CheckpointManifestError(f"{location} must be an array")
    return value


def _strict_keys(value: Mapping[str, Any], location: str, expected: set[str]) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing or extra:
        raise CheckpointManifestError(
            f"{location} fields differ; missing={missing}, unknown={extra}"
        )


def _string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CheckpointManifestError(
            f"{location} must be a nonempty string without surrounding whitespace"
        )
    return value


def _integer(value: Any, location: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise CheckpointManifestError(f"{location} must be an integer >= {minimum}")
    return value


def _sha256_text(value: Any, location: str) -> str:
    text = _string(value, location)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise CheckpointManifestError(f"{location} must be a lowercase SHA-256")
    return text


def _https_url(value: Any, location: str, *, expected_host: str | None = None) -> str:
    text = _string(value, location)
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as error:
        raise CheckpointManifestError(f"{location} is not a valid URL: {error}") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise CheckpointManifestError(f"{location} must be a credential-free HTTPS URL")
    if expected_host is not None and parsed.hostname != expected_host:
        raise CheckpointManifestError(f"{location} host must equal {expected_host}")
    return text


def _host(value: Any, location: str) -> str:
    text = _string(value, location)
    if text != text.lower() or "/" in text or ":" in text or text.startswith("."):
        raise CheckpointManifestError(f"{location} must be a lowercase DNS hostname")
    return text


def _filename(value: Any, location: str) -> str:
    text = _string(value, location)
    path = Path(text)
    if path.name != text or text in {".", ".."} or "/" in text or "\\" in text:
        raise CheckpointManifestError(f"{location} must be one basename")
    return text


def _relative_path(value: Any, location: str, filename: str) -> str:
    text = _string(value, location)
    path = Path(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CheckpointManifestError(f"{location} must be a safe relative path")
    if path.parts[0] != "checkpoints" or path.name != filename:
        raise CheckpointManifestError(
            f"{location} must be under checkpoints/ and end in {filename}"
        )
    return text


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_evidence(
    value: Any,
    *,
    location: str,
    repository_root: Path,
) -> PublishedEvidence:
    record = _mapping(value, location)
    _strict_keys(
        record,
        location,
        {
            "repository_url",
            "repository_commit",
            "repository_relative_path",
            "artifact_sha256",
            "artifact_size_bytes",
            "published_url",
            "checkpoint_label",
            "resolution_method",
            "author_link_id",
            "resolved_file_id",
            "resolved_file_view_url",
            "resolution_note",
        },
    )
    relative = _string(record["repository_relative_path"], f"{location}.repository_relative_path")
    relative_path = Path(relative)
    if relative_path.is_absolute() or any(part in {"", ".", ".."} for part in relative_path.parts):
        raise CheckpointManifestError(f"{location}.repository_relative_path must be safe and relative")
    evidence_path = repository_root / relative_path
    expected_size = _integer(record["artifact_size_bytes"], f"{location}.artifact_size_bytes", minimum=1)
    expected_sha256 = _sha256_text(record["artifact_sha256"], f"{location}.artifact_sha256")
    try:
        observed_size = evidence_path.stat().st_size
        observed_sha256 = _file_sha256(evidence_path)
    except OSError as error:
        raise CheckpointManifestError(f"{location} cannot verify {evidence_path}: {error}") from error
    if observed_size != expected_size or observed_sha256 != expected_sha256:
        raise CheckpointManifestError(f"{location} evidence bytes differ from the manifest")
    return PublishedEvidence(
        repository_url=_https_url(record["repository_url"], f"{location}.repository_url"),
        repository_commit=_string(record["repository_commit"], f"{location}.repository_commit"),
        repository_relative_path=relative,
        artifact_sha256=expected_sha256,
        artifact_size_bytes=expected_size,
        published_url=_https_url(record["published_url"], f"{location}.published_url", expected_host="drive.google.com"),
        checkpoint_label=_string(record["checkpoint_label"], f"{location}.checkpoint_label"),
        resolution_method=_string(record["resolution_method"], f"{location}.resolution_method"),
        author_link_id=_string(record["author_link_id"], f"{location}.author_link_id"),
        resolved_file_id=_string(record["resolved_file_id"], f"{location}.resolved_file_id"),
        resolved_file_view_url=_https_url(record["resolved_file_view_url"], f"{location}.resolved_file_view_url", expected_host="drive.google.com"),
        resolution_note=_string(record["resolution_note"], f"{location}.resolution_note"),
    )


def _parse_source(value: Any, *, location: str, resolved_file_id: str) -> CheckpointSource:
    record = _mapping(value, location)
    _strict_keys(
        record,
        location,
        {
            "url",
            "host",
            "allowed_redirect_hosts",
            "observed_at",
            "observed_http_status",
            "observed_final_url",
            "observed_redirect_hosts",
            "observed_content_type",
            "observed_content_disposition",
            "observed_content_length",
            "observed_last_modified",
        },
    )
    host = _host(record["host"], f"{location}.host")
    url = _https_url(record["url"], f"{location}.url", expected_host=host)
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if parsed.path != "/download" or query != {
        "id": [resolved_file_id],
        "export": ["download"],
        "confirm": ["t"],
    }:
        raise CheckpointManifestError(
            f"{location}.url must be the exact resolved Google Drive download URL"
        )
    allowed_redirect_hosts = tuple(
        _host(item, f"{location}.allowed_redirect_hosts[{index}]")
        for index, item in enumerate(_sequence(record["allowed_redirect_hosts"], f"{location}.allowed_redirect_hosts"))
    )
    observed_redirect_hosts = tuple(
        _host(item, f"{location}.observed_redirect_hosts[{index}]")
        for index, item in enumerate(_sequence(record["observed_redirect_hosts"], f"{location}.observed_redirect_hosts"))
    )
    if len(set(allowed_redirect_hosts)) != len(allowed_redirect_hosts):
        raise CheckpointManifestError(f"{location}.allowed_redirect_hosts contains duplicates")
    if allowed_redirect_hosts != observed_redirect_hosts:
        raise CheckpointManifestError(
            f"{location}.allowed_redirect_hosts must equal the actually observed redirect hosts"
        )
    observed_at = _string(record["observed_at"], f"{location}.observed_at")
    try:
        parsed_time = datetime.fromisoformat(observed_at)
    except ValueError as error:
        raise CheckpointManifestError(f"{location}.observed_at must be ISO-8601") from error
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise CheckpointManifestError(f"{location}.observed_at must include an offset")
    observed_status = _integer(record["observed_http_status"], f"{location}.observed_http_status", minimum=100)
    if observed_status != 200:
        raise CheckpointManifestError(f"{location}.observed_http_status must equal 200")
    final_url = _https_url(record["observed_final_url"], f"{location}.observed_final_url", expected_host=host)
    if not observed_redirect_hosts and final_url != url:
        raise CheckpointManifestError(
            f"{location}.observed_final_url must equal url when no redirect was observed"
        )
    content_type = _string(record["observed_content_type"], f"{location}.observed_content_type")
    if content_type != "application/octet-stream":
        raise CheckpointManifestError(f"{location}.observed_content_type must be application/octet-stream")
    return CheckpointSource(
        url=url,
        host=host,
        allowed_redirect_hosts=allowed_redirect_hosts,
        observed_at=observed_at,
        observed_http_status=observed_status,
        observed_final_url=final_url,
        observed_redirect_hosts=observed_redirect_hosts,
        observed_content_type=content_type,
        observed_content_disposition=_string(record["observed_content_disposition"], f"{location}.observed_content_disposition"),
        observed_content_length=_integer(record["observed_content_length"], f"{location}.observed_content_length", minimum=1),
        observed_last_modified=_string(record["observed_last_modified"], f"{location}.observed_last_modified"),
    )


def _parse_artifact(value: Any, *, location: str) -> CheckpointArtifact:
    record = _mapping(value, location)
    _strict_keys(record, location, {"filename", "sha256", "size_bytes", "managed_relative_path"})
    filename = _filename(record["filename"], f"{location}.filename")
    size = _integer(record["size_bytes"], f"{location}.size_bytes", minimum=1)
    if size > MAX_CHECKPOINT_BYTES:
        raise CheckpointManifestError(f"{location}.size_bytes exceeds the 1 GiB checkpoint bound")
    return CheckpointArtifact(
        filename=filename,
        sha256=_sha256_text(record["sha256"], f"{location}.sha256"),
        size_bytes=size,
        managed_relative_path=_relative_path(record["managed_relative_path"], f"{location}.managed_relative_path", filename),
    )


class _StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: tuple[str, ...]) -> None:
        super().__init__()
        self.allowed_hosts = frozenset(allowed_hosts)
        self.observed_hosts: list[str] = []

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        try:
            parsed = urlsplit(newurl)
            port = parsed.port
        except ValueError as error:
            raise CheckpointAcquisitionError(f"redirect URL is invalid: {error}") from error
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or host not in self.allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port not in {None, 443}
        ):
            raise CheckpointAcquisitionError(
                f"HTTP {code} redirect to an unadmitted HTTPS host was refused"
            )
        self.observed_hosts.append(host)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class CheckpointStore:
    """Resolve and acquire the closed set of owner-admitted research checkpoints."""

    def __init__(
        self,
        registry: ResourceRegistry,
        manifest_path: Path,
        data_dir: Path,
    ) -> None:
        self.registry = registry
        self.manifest_path = manifest_path
        self.repository_root = manifest_path.resolve(strict=True).parents[2]
        self.data_dir = data_dir.resolve(strict=False)
        self.entries = self._load_manifest()

    def _load_manifest(self) -> dict[str, CheckpointEntry]:
        try:
            raw = json.loads(
                self.manifest_path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_object,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CheckpointManifestError(f"cannot read {self.manifest_path}: {error}") from error
        root = _mapping(raw, "manifest")
        _strict_keys(root, "manifest", {"schema_version", "manifest_id", "policy", "entries"})
        if root["schema_version"] != MANIFEST_SCHEMA_VERSION or root["manifest_id"] != MANIFEST_ID:
            raise CheckpointManifestError("checkpoint acquisition manifest version or identity differs")
        _string(root["policy"], "manifest.policy")
        entries: dict[str, CheckpointEntry] = {}
        for index, value in enumerate(_sequence(root["entries"], "manifest.entries")):
            location = f"manifest.entries[{index}]"
            record = _mapping(value, location)
            _strict_keys(
                record,
                location,
                {
                    "resource_id",
                    "admission_state",
                    "admission_reason",
                    "third_party_rights_status",
                    "published_evidence",
                    "source",
                    "artifact",
                },
            )
            resource_id = _string(record["resource_id"], f"{location}.resource_id")
            if resource_id in entries:
                raise CheckpointManifestError(f"duplicate checkpoint resource: {resource_id}")
            admission_state = _string(record["admission_state"], f"{location}.admission_state")
            if admission_state not in {ADMITTED_STATE, BLOCKED_STATE}:
                raise CheckpointManifestError(f"{location}.admission_state is not admitted")
            evidence = _parse_evidence(
                record["published_evidence"],
                location=f"{location}.published_evidence",
                repository_root=self.repository_root,
            )
            source = _parse_source(
                record["source"],
                location=f"{location}.source",
                resolved_file_id=evidence.resolved_file_id,
            )
            artifact = _parse_artifact(record["artifact"], location=f"{location}.artifact")
            if source.observed_content_length != artifact.size_bytes:
                raise CheckpointManifestError(f"{location} observed and pinned byte lengths differ")
            disposition = Message()
            disposition["content-disposition"] = source.observed_content_disposition
            if disposition.get_filename() != artifact.filename:
                raise CheckpointManifestError(f"{location} observed filename differs from the artifact pin")
            third_party_rights_status = _string(
                record["third_party_rights_status"],
                f"{location}.third_party_rights_status",
            )
            entry = CheckpointEntry(
                resource_id=resource_id,
                admission_state=admission_state,
                admission_reason=_string(record["admission_reason"], f"{location}.admission_reason"),
                third_party_rights_status=third_party_rights_status,
                published_evidence=evidence,
                source=source,
                artifact=artifact,
            )
            self._validate_registry_binding(entry, location)
            entries[resource_id] = entry
        if set(entries) != EXPECTED_RESOURCE_IDS:
            raise CheckpointManifestError(
                f"manifest resource set differs: {sorted(set(entries) ^ EXPECTED_RESOURCE_IDS)}"
            )
        admitted = {resource_id for resource_id, entry in entries.items() if entry.admitted}
        if admitted != ADMITTED_RESOURCE_IDS:
            raise CheckpointManifestError(f"admitted checkpoint set differs: {sorted(admitted)}")
        return entries

    def _validate_registry_binding(self, entry: CheckpointEntry, location: str) -> None:
        try:
            resource = self.registry.resource(entry.resource_id)
            raw = self.registry.raw_resource(entry.resource_id)
        except KeyError as error:
            raise CheckpointManifestError(f"{location} is not registered") from error
        models = [artifact.identity for artifact in resource.artifacts if artifact.role == "MODEL"]
        if len(models) != 1 or models[0].path is None:
            raise CheckpointManifestError(f"{location} must bind exactly one registered MODEL")
        identity = models[0]
        if identity.sha256 != entry.artifact.sha256 or identity.size_bytes != entry.artifact.size_bytes:
            raise CheckpointManifestError(f"{location} checkpoint identity differs from the registry")
        if raw.get("third_party_rights_status") != entry.third_party_rights_status:
            raise CheckpointManifestError(f"{location} rights status differs from the registry")
        if entry.admitted:
            owner = raw.get("owner_authorization", {})
            if (
                resource.state != "APPROVED_LOCAL_RESEARCH_USE"
                or owner.get("decision") != "APPROVED_LOCAL_NONCOMMERCIAL"
                or owner.get("redistribution_allowed") is not False
                or entry.third_party_rights_status == "UNRESOLVED"
            ):
                raise CheckpointManifestError(
                    f"{location} lacks a terminal rights fact and owner-approved local noncommercial boundary"
                )

    def entry(self, resource_id: str) -> CheckpointEntry:
        try:
            return self.entries[resource_id]
        except KeyError as error:
            raise CheckpointAcquisitionError(f"unknown matcher checkpoint: {resource_id}") from error

    def registered_identity(self, resource_id: str) -> Any:
        resource = self.registry.resource(resource_id)
        models = [artifact.identity for artifact in resource.artifacts if artifact.role == "MODEL"]
        if len(models) != 1 or models[0].path is None:
            raise CheckpointAcquisitionError(f"{resource_id} does not register exactly one checkpoint")
        return models[0]

    def managed_path(self, resource_id: str) -> Path:
        entry = self.entry(resource_id)
        candidate = self.data_dir / entry.artifact.managed_relative_path
        parent = candidate.parent.resolve(strict=False)
        if parent != self.data_dir and self.data_dir not in parent.parents:
            raise CheckpointAcquisitionError(f"{resource_id} managed checkpoint escapes the data directory")
        return candidate

    def resolve(self, resource_id: str) -> tuple[Any, Path, str]:
        identity = self.registered_identity(resource_id)
        managed = self.managed_path(resource_id)
        if managed.is_symlink() or os.path.lexists(managed):
            return identity, managed, "MANAGED"
        return identity, Path(identity.path), "LEGACY_EXTERNAL"

    def acquisition_status(self, resource_id: str) -> dict[str, Any]:
        entry = self.entry(resource_id)
        return {
            "available": entry.admitted,
            "state": entry.admission_state,
            "reason": entry.admission_reason,
            "published_url": entry.published_evidence.published_url,
            "source_host": entry.source.host,
            "allowed_redirect_hosts": list(entry.source.allowed_redirect_hosts),
            "managed_path": str(self.managed_path(resource_id)),
            "sha256": entry.artifact.sha256,
            "size_bytes": entry.artifact.size_bytes,
        }

    def _file_state(self, path: Path, entry: CheckpointEntry) -> tuple[str, str | None, int | None]:
        if path.is_symlink():
            return "DRIFTED", None, None
        if not os.path.lexists(path):
            return "MISSING", None, None
        try:
            file_stat = path.stat()
        except OSError as error:
            raise CheckpointAcquisitionError(f"cannot inspect checkpoint {path}: {error}") from error
        if not stat.S_ISREG(file_stat.st_mode):
            return "DRIFTED", None, file_stat.st_size
        if file_stat.st_size != entry.artifact.size_bytes:
            return "DRIFTED", None, file_stat.st_size
        try:
            digest = _file_sha256(path)
        except OSError as error:
            raise CheckpointAcquisitionError(f"cannot hash checkpoint {path}: {error}") from error
        state = "VERIFIED" if digest == entry.artifact.sha256 else "DRIFTED"
        return state, digest, file_stat.st_size

    def _prepare_managed_directory(self, directory: Path) -> None:
        try:
            relative = directory.relative_to(self.data_dir)
        except ValueError as error:
            raise CheckpointAcquisitionError(
                "managed directory escapes the data directory"
            ) from error
        try:
            self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.data_dir.is_symlink() or not self.data_dir.is_dir():
                raise CheckpointAcquisitionError("checkpoint data directory is not a real directory")
            os.chmod(self.data_dir, 0o700)
            current = self.data_dir
            for part in relative.parts:
                current = current / part
                current.mkdir(mode=0o700, exist_ok=True)
                if current.is_symlink() or not current.is_dir():
                    raise CheckpointAcquisitionError(
                        "managed checkpoint directory may not be a symlink or non-directory"
                    )
                os.chmod(current, 0o700)
        except CheckpointAcquisitionError:
            raise
        except OSError as error:
            raise CheckpointAcquisitionError(
                f"cannot prepare private managed directory {directory}: {error}"
            ) from error

    def _prepare_parent(self, target: Path) -> None:
        self._prepare_managed_directory(target.parent)
        resolved_parent = target.parent.resolve(strict=True)
        if resolved_parent != self.data_dir and self.data_dir not in resolved_parent.parents:
            raise CheckpointAcquisitionError("managed checkpoint parent escapes the data directory")

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _stream_to_partial(
        self,
        entry: CheckpointEntry,
        target: Path,
        chunks: Iterable[bytes],
    ) -> tuple[Path, str, int]:
        self._prepare_parent(target)
        handle: BinaryIO | None = None
        partial: Path | None = None
        digest = hashlib.sha256()
        observed_size = 0
        try:
            handle = tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix=f".{entry.artifact.filename}.",
                suffix=".partial",
                dir=target.parent,
                delete=False,
            )
            partial = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            for block in chunks:
                if not isinstance(block, bytes) or not block:
                    raise CheckpointAcquisitionError("checkpoint stream yielded an invalid block")
                observed_size += len(block)
                if observed_size > entry.artifact.size_bytes:
                    raise CheckpointAcquisitionError("checkpoint exceeded its pinned byte length")
                digest.update(block)
                handle.write(block)
            if observed_size != entry.artifact.size_bytes:
                raise CheckpointAcquisitionError(
                    f"checkpoint ended at {observed_size} bytes; expected {entry.artifact.size_bytes}"
                )
            observed_sha256 = digest.hexdigest()
            if observed_sha256 != entry.artifact.sha256:
                raise CheckpointAcquisitionError(
                    f"checkpoint SHA-256 {observed_sha256} differs from the registered {entry.artifact.sha256}"
                )
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            return partial, observed_sha256, observed_size
        except Exception as error:
            cleanup_errors: list[str] = []
            if handle is not None:
                try:
                    handle.close()
                except OSError as close_error:
                    cleanup_errors.append(f"close failed: {close_error}")
            if partial is not None:
                try:
                    partial.unlink(missing_ok=True)
                except OSError as unlink_error:
                    cleanup_errors.append(f"unlink failed: {unlink_error}")
            if cleanup_errors:
                raise CheckpointAcquisitionError(
                    "checkpoint partial cleanup failed after stream rejection: "
                    + "; ".join(cleanup_errors)
                ) from error
            raise

    @staticmethod
    def _rollback_if_same_inode(path: Path, inode: tuple[int, int] | None) -> None:
        if inode is None:
            return
        try:
            observed = path.lstat()
            if stat.S_ISREG(observed.st_mode) and (observed.st_dev, observed.st_ino) == inode:
                path.unlink()
        except FileNotFoundError:
            return

    def _promote_no_clobber(
        self,
        partial: Path,
        target: Path,
        entry: CheckpointEntry,
    ) -> tuple[str, tuple[int, int] | None]:
        installed_inode: tuple[int, int] | None = None
        try:
            partial_stat = partial.stat()
            expected_inode = (partial_stat.st_dev, partial_stat.st_ino)
            os.link(partial, target, follow_symlinks=False)
            installed_inode = expected_inode
            self._sync_directory(target.parent)
            return "INSTALLED", installed_inode
        except FileExistsError:
            state, _, _ = self._file_state(target, entry)
            if state != "VERIFIED":
                raise CheckpointAcquisitionError(
                    f"managed checkpoint appeared with state {state.lower()}; refusing to overwrite {target}"
                )
            return "REUSED_CONCURRENT", None
        except Exception:
            self._rollback_if_same_inode(target, installed_inode)
            raise
        finally:
            try:
                partial.unlink(missing_ok=True)
            except OSError as error:
                self._rollback_if_same_inode(target, installed_inode)
                raise CheckpointAcquisitionError(
                    f"cannot remove checkpoint partial file {partial}: {error}"
                ) from error
            try:
                self._sync_directory(target.parent)
            except OSError as error:
                self._rollback_if_same_inode(target, installed_inode)
                raise CheckpointAcquisitionError(
                    f"cannot durably clean checkpoint partial state: {error}"
                ) from error

    @staticmethod
    def _iter_file(handle: BinaryIO) -> Iterable[bytes]:
        while True:
            block = handle.read(CHUNK_BYTES)
            if not block:
                return
            yield block

    @staticmethod
    def _iter_response(response: Any) -> Iterable[bytes]:
        while True:
            block = response.read(CHUNK_BYTES)
            if not block:
                return
            yield block

    def _receipt(
        self,
        entry: CheckpointEntry,
        *,
        action: str,
        target: Path,
        observed_sha256: str,
        observed_size_bytes: int,
        transport: Mapping[str, Any],
    ) -> dict[str, Any]:
        core = {
            "schema_version": "1.0.0",
            "receipt_type": "MATCHER_CHECKPOINT_ACQUISITION",
            "recorded_at": _now(),
            "resource_id": entry.resource_id,
            "action": action,
            "target_path": str(target),
            "artifact": {
                "filename": entry.artifact.filename,
                "sha256": observed_sha256,
                "size_bytes": observed_size_bytes,
            },
            "source_contract": {
                "published_url": entry.published_evidence.published_url,
                "resolved_file_id": entry.published_evidence.resolved_file_id,
                "download_url": entry.source.url,
                "source_host": entry.source.host,
                "allowed_redirect_hosts": list(entry.source.allowed_redirect_hosts),
            },
            "transport": dict(transport),
        }
        canonical_core = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        evidence_sha256 = hashlib.sha256(canonical_core).hexdigest()
        receipt = {
            **core,
            "receipt_id": f"checkpoint-receipt:sha256:{evidence_sha256}",
            "evidence_sha256": evidence_sha256,
        }
        receipt_bytes = (
            json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        receipt_dir = self.data_dir / "acquisition-receipts" / entry.resource_id
        receipt_path = receipt_dir / f"{evidence_sha256}.json"
        temporary: Path | None = None
        handle: BinaryIO | None = None
        published_inode: tuple[int, int] | None = None
        try:
            self._prepare_managed_directory(receipt_dir)
            handle = tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix=f".{evidence_sha256}.",
                suffix=".partial",
                dir=receipt_dir,
                delete=False,
            )
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(receipt_bytes)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            try:
                temporary_stat = temporary.stat()
                os.link(temporary, receipt_path, follow_symlinks=False)
                published_inode = (temporary_stat.st_dev, temporary_stat.st_ino)
            except FileExistsError:
                receipt_stat = receipt_path.lstat()
                if (
                    not stat.S_ISREG(receipt_stat.st_mode)
                    or receipt_path.is_symlink()
                    or receipt_stat.st_nlink != 1
                ):
                    raise CheckpointAcquisitionError(
                        "existing acquisition receipt is not a private regular file"
                    )
                if receipt_path.read_bytes() != receipt_bytes:
                    raise CheckpointAcquisitionError("existing acquisition receipt bytes differ")
            os.chmod(receipt_path, 0o600)
            temporary.unlink()
            temporary = None
            self._sync_directory(receipt_dir)
        except Exception as error:
            self._rollback_if_same_inode(receipt_path, published_inode)
            cleanup_errors: list[str] = []
            if handle is not None:
                try:
                    handle.close()
                except OSError as close_error:
                    cleanup_errors.append(f"close failed: {close_error}")
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as unlink_error:
                    cleanup_errors.append(f"unlink failed: {unlink_error}")
            if cleanup_errors:
                raise CheckpointAcquisitionError(
                    "receipt publication rollback left partial state: "
                    + "; ".join(cleanup_errors)
                ) from error
            raise CheckpointAcquisitionError(
                f"cannot persist checkpoint acquisition receipt: {error}"
            ) from error
        return {**receipt, "path": str(receipt_path)}

    def _reuse_managed(self, entry: CheckpointEntry, target: Path) -> dict[str, Any] | None:
        state, digest, size = self._file_state(target, entry)
        if state == "MISSING":
            return None
        if state != "VERIFIED" or digest is None or size is None:
            raise CheckpointAcquisitionError(
                f"managed checkpoint is {state.lower()}; refusing to overwrite {target}"
            )
        return self._receipt(
            entry,
            action="REUSED_VERIFIED_MANAGED",
            target=target,
            observed_sha256=digest,
            observed_size_bytes=size,
            transport={"method": "LOCAL_VERIFIED_REUSE", "network_used": False},
        )

    def acquire(self, resource_id: str) -> dict[str, Any]:
        """Reuse managed bytes, import the verified registered legacy file, or fetch."""

        entry = self.entry(resource_id)
        if not entry.admitted:
            raise CheckpointAcquisitionError(
                f"{resource_id} checkpoint acquisition is {entry.admission_state.lower()}: {entry.admission_reason}"
            )
        target = self.managed_path(resource_id)
        reused = self._reuse_managed(entry, target)
        if reused is not None:
            return reused
        identity = self.registered_identity(resource_id)
        legacy = Path(identity.path)
        if legacy.is_symlink() or os.path.lexists(legacy):
            return self._import_legacy(entry, legacy, target)
        return self.acquire_from_network(resource_id)

    def _import_legacy(self, entry: CheckpointEntry, legacy: Path, target: Path) -> dict[str, Any]:
        if legacy.is_symlink() or not legacy.is_file():
            raise CheckpointAcquisitionError(f"registered legacy checkpoint is not a regular file: {legacy}")
        try:
            with legacy.open("rb") as source:
                partial, digest, size = self._stream_to_partial(
                    entry,
                    target,
                    self._iter_file(source),
                )
        except OSError as error:
            raise CheckpointAcquisitionError(f"cannot import registered legacy checkpoint: {error}") from error
        promoted, installed_inode = self._promote_no_clobber(partial, target, entry)
        try:
            return self._receipt(
                entry,
                action=("IMPORTED_VERIFIED_LEGACY" if promoted == "INSTALLED" else "REUSED_VERIFIED_MANAGED"),
                target=target,
                observed_sha256=digest,
                observed_size_bytes=size,
                transport={
                    "method": "LOCAL_REGISTERED_IMPORT",
                    "network_used": False,
                    "legacy_path": str(legacy),
                },
            )
        except Exception:
            if promoted == "INSTALLED":
                self._rollback_if_same_inode(target, installed_inode)
                self._sync_directory(target.parent)
            raise

    def acquire_from_network(self, resource_id: str) -> dict[str, Any]:
        """Fetch the admitted artifact from its exact official endpoint."""

        entry = self.entry(resource_id)
        if not entry.admitted:
            raise CheckpointAcquisitionError(
                f"{resource_id} checkpoint acquisition is {entry.admission_state.lower()}: {entry.admission_reason}"
            )
        target = self.managed_path(resource_id)
        reused = self._reuse_managed(entry, target)
        if reused is not None:
            return reused
        redirect_handler = _StrictRedirectHandler(entry.source.allowed_redirect_hosts)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), redirect_handler)
        request = urllib.request.Request(
            entry.source.url,
            headers={
                "User-Agent": "QualSight-checkpoint-setup/0.1",
                "Accept": "application/octet-stream",
                "Accept-Encoding": "identity",
            },
            method="GET",
        )
        try:
            with opener.open(request, timeout=120) as response:
                status = int(response.status)
                if status != 200:
                    raise CheckpointAcquisitionError(f"checkpoint endpoint returned HTTP {status}")
                final_url = response.geturl()
                parsed_final = urlsplit(final_url)
                admitted_hosts = {entry.source.host, *entry.source.allowed_redirect_hosts}
                if parsed_final.scheme != "https" or parsed_final.hostname not in admitted_hosts:
                    raise CheckpointAcquisitionError("checkpoint response ended at an unadmitted URL")
                if tuple(redirect_handler.observed_hosts) != entry.source.allowed_redirect_hosts:
                    raise CheckpointAcquisitionError("observed redirect chain differs from the manifest")
                content_type = response.headers.get_content_type()
                if content_type != "application/octet-stream":
                    raise CheckpointAcquisitionError(
                        "checkpoint publisher returned "
                        f"{content_type} instead of checkpoint bytes; the publisher may be rate- or quota-limiting downloads"
                    )
                raw_length = response.headers.get("Content-Length")
                try:
                    content_length = int(raw_length) if raw_length is not None else -1
                except ValueError as error:
                    raise CheckpointAcquisitionError("checkpoint Content-Length is invalid") from error
                if content_length != entry.artifact.size_bytes:
                    raise CheckpointAcquisitionError(
                        f"checkpoint Content-Length {content_length} differs from {entry.artifact.size_bytes}"
                    )
                content_disposition = response.headers.get("Content-Disposition", "")
                disposition = Message()
                disposition["content-disposition"] = content_disposition
                if disposition.get_filename() != entry.artifact.filename:
                    raise CheckpointAcquisitionError("checkpoint Content-Disposition filename differs")
                partial, digest, size = self._stream_to_partial(
                    entry,
                    target,
                    self._iter_response(response),
                )
                transport = {
                    "method": "HTTPS_GET_STREAM",
                    "network_used": True,
                    "http_status": status,
                    "final_url": final_url,
                    "observed_redirect_hosts": list(redirect_handler.observed_hosts),
                    "content_type": content_type,
                    "content_disposition": content_disposition,
                    "content_length": content_length,
                    "last_modified": response.headers.get("Last-Modified"),
                }
        except CheckpointAcquisitionError:
            raise
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
            raise CheckpointAcquisitionError(f"checkpoint HTTPS acquisition failed: {error}") from error
        promoted, installed_inode = self._promote_no_clobber(partial, target, entry)
        try:
            return self._receipt(
                entry,
                action=("ACQUIRED_OFFICIAL_HTTPS" if promoted == "INSTALLED" else "REUSED_VERIFIED_MANAGED"),
                target=target,
                observed_sha256=digest,
                observed_size_bytes=size,
                transport=transport,
            )
        except Exception:
            if promoted == "INSTALLED":
                self._rollback_if_same_inode(target, installed_inode)
                self._sync_directory(target.parent)
            raise
