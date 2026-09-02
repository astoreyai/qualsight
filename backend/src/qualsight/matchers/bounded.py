"""Deadline-bounded execution of a verified matcher in a killable process."""

from __future__ import annotations

import hashlib
import math
import multiprocessing
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
from typing import Any

import numpy as np

from ..execution.registry_binding import ResourceRegistry
from .runtime import MatcherId
from .upstream import UpstreamSources


class MatcherExecutionError(RuntimeError):
    """Raised when isolated matcher execution cannot return valid evidence."""


class MatcherExecutionDeadlineError(MatcherExecutionError):
    """Raised after a matcher worker is terminated at its configured deadline."""


def validate_execution_timeout(value: float) -> float:
    """Return a finite positive timeout suitable for a process deadline."""

    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("matcher execution timeout must be finite and positive")
    return float(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _embedding_worker(
    sender: Any,
    *,
    matcher_id: str,
    registry_path: str,
    upstream_manifest_path: str,
    data_dir: str,
    crops_path: str,
    result_path: str,
) -> None:
    """Load and execute one registered matcher, publishing only a private result."""

    try:
        from ..execution.registry_binding import load_resource_registry
        from .runtime import FrozenMatcher
        from .upstream import UpstreamSources

        registry = load_resource_registry(Path(registry_path))
        upstream = UpstreamSources(Path(upstream_manifest_path), Path(data_dir))
        matcher = FrozenMatcher(MatcherId(matcher_id), registry, upstream)
        crops_bgr = np.load(Path(crops_path), allow_pickle=False)
        embeddings = matcher.embed(crops_bgr)
        path = Path(result_path)
        with path.open("xb") as handle:
            os.chmod(path, 0o600)
            np.save(handle, embeddings, allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        _sync_directory(path.parent)
        sender.send(
            {
                "dtype": str(embeddings.dtype),
                "sha256": _sha256_file(path),
                "shape": list(embeddings.shape),
                "state": "COMPLETED",
            }
        )
    except BaseException as error:
        try:
            sender.send(
                {
                    "error_type": type(error).__name__,
                    "state": "FAILED",
                }
            )
        except BaseException:
            pass
    finally:
        sender.close()


def _terminate(process: multiprocessing.Process) -> None:
    if process.pid is None:
        return
    if not process.is_alive():
        return
    process.terminate()
    process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)


def embed_with_deadline(
    *,
    matcher_id: MatcherId,
    registry: ResourceRegistry,
    upstream: UpstreamSources,
    crops_bgr: np.ndarray,
    timeout_seconds: float,
    process_label: str,
) -> np.ndarray:
    """Execute one real matcher call with a hard, process-terminable deadline."""

    timeout = validate_execution_timeout(timeout_seconds)
    if (
        not isinstance(crops_bgr, np.ndarray)
        or crops_bgr.dtype != np.uint8
        or crops_bgr.ndim != 4
        or crops_bgr.shape[0] == 0
        or crops_bgr.shape[1:] != (112, 112, 3)
    ):
        raise MatcherExecutionError(
            "matcher input must be a non-empty N×112×112×3 uint8 array"
        )
    registry_path = registry.file_identity.path
    if not registry_path:
        raise MatcherExecutionError("matcher registry path is not recorded")

    scratch_root = Path(upstream.root.parent)
    scratch_root.mkdir(parents=True, exist_ok=True)
    scratch = Path(
        tempfile.mkdtemp(prefix=".qualsight-embedding-", dir=str(scratch_root))
    )
    receiver: Any | None = None
    sender: Any | None = None
    process: multiprocessing.Process | None = None
    try:
        os.chmod(scratch, 0o700)
        crops_path = scratch / "crops.npy"
        result_path = scratch / "embeddings.npy"
        with crops_path.open("xb") as handle:
            os.chmod(crops_path, 0o600)
            np.save(handle, crops_bgr, allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        _sync_directory(scratch)
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_embedding_worker,
            kwargs={
                "sender": sender,
                "matcher_id": matcher_id.value,
                "registry_path": str(registry_path),
                "upstream_manifest_path": str(upstream.manifest_path),
                "data_dir": str(upstream.root.parent),
                "crops_path": str(crops_path),
                "result_path": str(result_path),
            },
            name=f"qualsight-matcher-{process_label[-24:]}",
            daemon=True,
        )
        process.start()
        sender.close()
        deadline = time.monotonic() + timeout
        if not receiver.poll(max(0.0, deadline - time.monotonic())):
            raise MatcherExecutionDeadlineError(
                "matcher execution exceeded its configured deadline"
            )
        try:
            payload = receiver.recv()
        except EOFError as error:
            raise MatcherExecutionError(
                "matcher execution process ended without evidence"
            ) from error
        process.join(timeout=max(0.0, deadline - time.monotonic()))
        if process.is_alive():
            raise MatcherExecutionDeadlineError(
                "matcher execution exceeded its configured deadline"
            )
        if process.exitcode != 0 or payload.get("state") != "COMPLETED":
            error_type = payload.get("error_type")
            suffix = f" ({error_type})" if isinstance(error_type, str) else ""
            raise MatcherExecutionError(
                f"matcher execution failed in isolated process{suffix}"
            )
        try:
            result_stat = result_path.lstat()
        except OSError:
            raise MatcherExecutionError(
                "matcher execution did not publish embedding evidence"
            ) from None
        if not stat.S_ISREG(result_stat.st_mode) or result_path.is_symlink():
            raise MatcherExecutionError(
                "matcher execution did not publish a regular evidence file"
            )
        expected_shape = (crops_bgr.shape[0], 512)
        expected_payload_bytes = int(np.prod(expected_shape)) * np.dtype(np.float32).itemsize
        if result_stat.st_size <= expected_payload_bytes or result_stat.st_size > expected_payload_bytes + 4096:
            raise MatcherExecutionError(
                "matcher execution evidence has an invalid encoded size"
            )
        if _sha256_file(result_path) != payload.get("sha256"):
            raise MatcherExecutionError("matcher execution evidence failed integrity validation")
        try:
            embeddings = np.load(result_path, allow_pickle=False)
        except (OSError, ValueError):
            raise MatcherExecutionError(
                "matcher execution evidence could not be decoded"
            ) from None
        if (
            not isinstance(embeddings, np.ndarray)
            or embeddings.dtype != np.float32
            or embeddings.shape != expected_shape
            or payload.get("dtype") != "float32"
            or payload.get("shape") != list(expected_shape)
            or not np.all(np.isfinite(embeddings))
        ):
            raise MatcherExecutionError(
                "matcher execution returned invalid embedding evidence"
            )
        return embeddings
    finally:
        try:
            if receiver is not None:
                receiver.close()
            if sender is not None:
                sender.close()
            if process is not None:
                _terminate(process)
        finally:
            try:
                shutil.rmtree(scratch)
            except FileNotFoundError:
                pass
            except OSError:
                raise MatcherExecutionError(
                    "private matcher handoff cleanup failed"
                ) from None
