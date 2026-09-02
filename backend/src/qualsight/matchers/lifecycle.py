"""Verified operator lifecycle for externally managed matcher assets."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import gc
from pathlib import Path
import threading
from typing import Any, Iterator

import torch

from ..execution.registry_binding import ResourceRegistry
from .acquisition import (
    MANIFEST_NAME as CHECKPOINT_MANIFEST_NAME,
    CheckpointAcquisitionError,
    CheckpointStore,
)
from .runtime import FrozenMatcher, MatcherId, UPSTREAM_KEYS, sha256_file
from .upstream import UpstreamSourceError, UpstreamSources


class MatcherLifecycleError(RuntimeError):
    """Raised when a requested matcher lifecycle transition is unsafe."""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


class MatcherRuntimeManager:
    """Own the matcher instances used by API jobs and expose their real state."""

    def __init__(self, registry: ResourceRegistry, upstream: UpstreamSources) -> None:
        self.registry = registry
        self.upstream = upstream
        self.checkpoints = CheckpointStore(
            registry,
            upstream.manifest_path.with_name(CHECKPOINT_MANIFEST_NAME),
            upstream.root.parent,
        )
        self._lock = threading.RLock()
        self._loaded: dict[MatcherId, FrozenMatcher] = {}
        self._loaded_at: dict[MatcherId, str] = {}
        self._active: dict[MatcherId, int] = {matcher_id: 0 for matcher_id in MatcherId}
        self._checkpoint_cache: dict[
            MatcherId,
            tuple[str, int, int, int, int, int, str],
        ] = {}

    def _matcher_id(self, value: str | MatcherId) -> MatcherId:
        try:
            return value if isinstance(value, MatcherId) else MatcherId(value)
        except ValueError as error:
            raise MatcherLifecycleError(f"unknown matcher: {value}") from error

    def _checkpoint(self, matcher_id: MatcherId) -> tuple[Any, Path]:
        try:
            identity, path, _ = self.checkpoints.resolve(matcher_id.value)
        except CheckpointAcquisitionError as error:
            raise MatcherLifecycleError(str(error)) from error
        return identity, path

    def _checkpoint_status(self, matcher_id: MatcherId) -> dict[str, Any]:
        identity, path, location_state = self.checkpoints.resolve(matcher_id.value)
        if path.is_symlink():
            return {
                "state": "DRIFTED",
                "location_state": location_state,
                "path": str(path),
                "registered_sha256": identity.sha256,
                "registered_size_bytes": identity.size_bytes,
                "observed_sha256": None,
            }
        if not path.is_file():
            return {
                "state": "MISSING",
                "location_state": location_state,
                "path": str(path),
                "registered_sha256": identity.sha256,
                "registered_size_bytes": identity.size_bytes,
                "observed_sha256": None,
            }
        stat = path.stat()
        cache = self._checkpoint_cache.get(matcher_id)
        cache_key = (
            str(path),
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )
        if cache is not None and cache[:6] == cache_key:
            digest = cache[6]
        else:
            digest = sha256_file(path)
            self._checkpoint_cache[matcher_id] = (*cache_key, digest)
        state = "VERIFIED" if stat.st_size == identity.size_bytes and digest == identity.sha256 else "DRIFTED"
        return {
            "state": state,
            "location_state": location_state,
            "path": str(path),
            "registered_sha256": identity.sha256,
            "registered_size_bytes": identity.size_bytes,
            "observed_sha256": digest,
            "observed_size_bytes": stat.st_size,
        }

    def _source_status(self, matcher_id: MatcherId) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for key in UPSTREAM_KEYS[matcher_id]:
            source = self.upstream.sources[key]
            path = self.upstream.path_for(key)
            try:
                self.upstream.verify(key)
                state = "VERIFIED"
                reason = None
            except UpstreamSourceError as error:
                state = "MISSING" if not path.is_file() else "DRIFTED"
                reason = str(error)
            result.append(
                {
                    "key": key,
                    "state": state,
                    "path": str(path),
                    "repository": source.repository,
                    "commit": source.commit,
                    "url": source.url,
                    "sha256": source.sha256,
                    "size_bytes": source.size_bytes,
                    "reason": reason,
                }
            )
        return result

    def status(self, value: str | MatcherId) -> dict[str, Any]:
        matcher_id = self._matcher_id(value)
        with self._lock:
            resource = self.registry.resource(matcher_id.value)
            raw_resource = self.registry.raw_resource(matcher_id.value)
            checkpoint = self._checkpoint_status(matcher_id)
            sources = self._source_status(matcher_id)
            loaded = self._loaded.get(matcher_id)
            blockers: list[str] = []
            if checkpoint["state"] != "VERIFIED":
                blockers.append(f"checkpoint is {checkpoint['state'].lower()}")
            blockers.extend(f"architecture source {item['key']} is {item['state'].lower()}" for item in sources if item["state"] != "VERIFIED")
            return {
                "matcher_id": matcher_id.value,
                "name": resource.name,
                "registry_state": resource.state,
                "third_party_rights_status": raw_resource["third_party_rights_status"],
                "research_use_status": resource.research_use_status,
                "redistribution_status": resource.redistribution_status,
                "data_classification": resource.data_classification,
                "approved_scopes": list(resource.approved_scopes),
                "checkpoint": checkpoint,
                "checkpoint_acquisition": self.checkpoints.acquisition_status(matcher_id.value),
                "architecture_sources": sources,
                "ready_to_load": not blockers,
                "blocking_reasons": blockers,
                "runtime": {
                    "state": "LOADED" if loaded is not None else "UNLOADED",
                    "active_leases": self._active[matcher_id],
                    "loaded_at": self._loaded_at.get(matcher_id),
                    "device": loaded.binding.device if loaded is not None else None,
                    "parameter_count": loaded.binding.parameter_count if loaded is not None else None,
                },
            }

    def catalog(self) -> list[dict[str, Any]]:
        return [self.status(matcher_id) for matcher_id in MatcherId]

    @contextmanager
    def exclusive_control(self) -> Iterator[None]:
        """Exclude scientific leases across one complete lifecycle transaction."""

        with self._lock:
            yield

    def require_ready(self, values: tuple[str | MatcherId, ...]) -> None:
        failures: list[str] = []
        for value in values:
            item = self.status(value)
            failures.extend(f"{item['matcher_id']}: {reason}" for reason in item["blocking_reasons"])
        if failures:
            raise MatcherLifecycleError("matcher dependencies are not ready; use System to pull pinned architecture sources and resolve externally managed checkpoints: " + "; ".join(failures))

    def pull_architecture_sources(self, value: str | MatcherId) -> dict[str, Any]:
        matcher_id = self._matcher_id(value)
        with self._lock:
            try:
                self.upstream.acquire(UPSTREAM_KEYS[matcher_id])
            except Exception as error:  # noqa: BLE001 - preserve network guard/acquisition reason for the operator
                raise MatcherLifecycleError(f"{matcher_id.value}: architecture source pull failed: {error}") from error
            return self.status(matcher_id)

    def acquire_checkpoint(self, value: str | MatcherId) -> dict[str, Any]:
        matcher_id = self._matcher_id(value)
        with self._lock:
            if matcher_id in self._loaded or self._active[matcher_id]:
                raise MatcherLifecycleError(
                    f"{matcher_id.value}: checkpoint acquisition is refused while the matcher is loaded or leased"
                )
            try:
                receipt = self.checkpoints.acquire(matcher_id.value)
            except CheckpointAcquisitionError as error:
                raise MatcherLifecycleError(f"{matcher_id.value}: checkpoint acquisition failed: {error}") from error
            self._checkpoint_cache.pop(matcher_id, None)
            return {"asset": self.status(matcher_id), "receipt": receipt}

    def load(self, value: str | MatcherId) -> dict[str, Any]:
        matcher_id = self._matcher_id(value)
        with self._lock:
            self.require_ready((matcher_id,))
            if matcher_id not in self._loaded:
                try:
                    self._loaded[matcher_id] = FrozenMatcher(
                        matcher_id,
                        self.registry,
                        self.upstream,
                        checkpoint_store=self.checkpoints,
                    )
                except Exception as error:  # noqa: BLE001 - strict runtime errors are operator-facing
                    raise MatcherLifecycleError(f"{matcher_id.value}: load failed: {error}") from error
                self._loaded_at[matcher_id] = _now()
            return self.status(matcher_id)

    def unload(self, value: str | MatcherId) -> dict[str, Any]:
        matcher_id = self._matcher_id(value)
        with self._lock:
            if self._active[matcher_id]:
                raise MatcherLifecycleError(f"{matcher_id.value}: unload refused while {self._active[matcher_id]} job lease(s) are active")
            matcher = self._loaded.pop(matcher_id, None)
            self._loaded_at.pop(matcher_id, None)
            if matcher is not None:
                del matcher
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            return self.status(matcher_id)

    def unload_all(self) -> None:
        with self._lock:
            if any(self._active.values()):
                raise MatcherLifecycleError("cannot shut down matcher runtime while job leases are active")
            self._loaded.clear()
            self._loaded_at.clear()
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    @contextmanager
    def lease(self, value: str | MatcherId) -> Iterator[FrozenMatcher]:
        matcher_id = self._matcher_id(value)
        with self._lock:
            self.load(matcher_id)
            self._active[matcher_id] += 1
            matcher = self._loaded[matcher_id]
        try:
            yield matcher
        finally:
            with self._lock:
                self._active[matcher_id] -= 1
