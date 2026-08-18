"""Setup-time acquisition and verified import of pinned upstream architecture sources.

The pins live in ``resources/manifests/matcher-upstream-sources-v1.json``
(repository, commit, URL, SHA-256, size). Sources are fetched only by the
explicit ``acquire()`` step into the git-ignored data directory and are
imported only after their bytes match the pin; QualSight never bundles them.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types
import urllib.request

MANIFEST_NAME = "matcher-upstream-sources-v1.json"


class UpstreamSourceError(RuntimeError):
    """Raised when a pinned upstream source is absent, drifted, or unfetchable."""


@dataclass(frozen=True, slots=True)
class UpstreamSource:
    key: str
    repository: str
    commit: str
    url: str
    sha256: str
    size_bytes: int

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]


class UpstreamSources:
    def __init__(self, manifest_path: Path, data_dir: Path, cache_root: Path | None = None) -> None:
        self.manifest_path = manifest_path
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.root = data_dir / "upstream"
        # Installed copy (repository var/upstream) reused byte-verified before any network is touched, so a
        # fresh data directory works offline once `qualsight setup` has run on the workstation.
        self.cache_root = cache_root if cache_root is not None else manifest_path.resolve().parents[2] / "var" / "upstream"
        self.sources = {
            item["key"]: UpstreamSource(
                key=item["key"],
                repository=item["repository"],
                commit=item["commit"],
                url=item["url"],
                sha256=item["sha256"],
                size_bytes=int(item["size_bytes"]),
            )
            for item in self.manifest["sources"]
        }

    def path_for(self, key: str) -> Path:
        source = self.sources[key]
        stem = source.filename.rsplit(".", 1)[0]
        return self.root / source.repository.split("/")[0].lower() / f"{stem}.{source.commit[:8]}.py"

    def verify(self, key: str) -> Path:
        source = self.sources[key]
        path = self.path_for(key)
        if not path.is_file():
            raise UpstreamSourceError(f"{key}: pinned source is not acquired at {path}; run acquire()")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != source.sha256 or len(data) != source.size_bytes:
            raise UpstreamSourceError(f"{key}: bytes at {path} do not match the pin ({digest}, {len(data)} bytes)")
        return path

    def acquire(self, keys: tuple[str, ...] | None = None) -> dict[str, Path]:
        """Fetch pinned sources that are missing; refuse and delete on mismatch."""

        acquired: dict[str, Path] = {}
        for key in keys or tuple(self.sources):
            source = self.sources[key]
            path = self.path_for(key)
            if path.is_file():
                try:
                    acquired[key] = self.verify(key)
                    continue
                except UpstreamSourceError:
                    path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            cached = self.cache_root / path.relative_to(self.root) if self.cache_root != self.root else None
            if cached is not None and cached.is_file():
                data = cached.read_bytes()
                if hashlib.sha256(data).hexdigest() == source.sha256 and len(data) == source.size_bytes:
                    path.write_bytes(data)
                    acquired[key] = path
                    continue
            request = urllib.request.Request(source.url, headers={"User-Agent": "QualSight-setup/0.1"})
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - pinned https URL
                data = response.read()
            digest = hashlib.sha256(data).hexdigest()
            if digest != source.sha256 or len(data) != source.size_bytes:
                raise UpstreamSourceError(f"{key}: fetched bytes ({digest}, {len(data)} bytes) do not match the pin; refusing")
            path.write_bytes(data)
            acquired[key] = path
        return acquired

    def load_module(self, key: str, module_name: str | None = None) -> types.ModuleType:
        path = self.verify(key)
        name = module_name or f"qualsight_upstream_{key}"
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise UpstreamSourceError(f"{key}: cannot build an import spec for {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
