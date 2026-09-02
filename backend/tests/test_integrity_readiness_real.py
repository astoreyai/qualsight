"""Adversarial integrity gates over registered matcher and LFW bytes.

Every mutation in this module is applied only to a temporary derivative of a
registered real artifact.  The repository registry and source artifacts remain
read-only and unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from qualsight.addons import dataset_addons
from qualsight.api.settings import Settings
from qualsight.execution.registry_binding import load_resource_registry
from qualsight.matchers.lifecycle import MatcherRuntimeManager
from qualsight.matchers.runtime import MatcherId
from qualsight.matchers.upstream import UpstreamSources
from qualsight.security.network_guard import (
    NetworkGuardViolation,
    permit_explicit_setup_network,
)


ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "resources" / "resource-registry.json"
UPSTREAM_MANIFEST = ROOT / "resources" / "manifests" / "matcher-upstream-sources-v1.json"
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
ELASTICFACE_RESOURCE_ID = "elasticface-arc-r100-local-candidate"


class IntegrityReadinessRealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="qualsight-integrity-readiness-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _registry_with_lfw_paths(self, *, archive_path: Path, tree_root: Path) -> Path:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        resource = next(
            item for item in registry["resources"] if item["resource_id"] == LFW_RESOURCE_ID
        )
        resource["source"]["local_root"] = str(tree_root.parent)
        archive = next(
            item
            for item in resource["artifacts"]
            if item["role"] == "DATA" and item["path"].endswith("lfw-funneled.tgz")
        )
        registered_archive_path = archive["path"]
        archive["path"] = str(archive_path)
        resource["source"]["evidence_artifacts"] = [
            str(archive_path) if path == registered_archive_path else path
            for path in resource["source"]["evidence_artifacts"]
        ]
        for check in resource["checks"]:
            if check["type"] in {"directory_inventory", "tree_manifest"}:
                check["root"] = str(tree_root)
        derived_registry = self.temp_dir / "resource-registry-derived-from-registered.json"
        derived_registry.write_text(
            json.dumps(registry, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return derived_registry

    def test_upstream_sources_can_prepare_only_from_the_verified_real_cache(self) -> None:
        state = self.temp_dir / "offline-state"
        upstream = UpstreamSources(
            UPSTREAM_MANIFEST,
            state,
            cache_root=ROOT / "var" / "upstream",
        )

        acquired = upstream.acquire_cached()

        self.assertEqual(set(acquired), set(upstream.sources))
        for key, path in acquired.items():
            self.assertEqual(path, upstream.verify(key))
        unavailable = UpstreamSources(
            UPSTREAM_MANIFEST,
            self.temp_dir / "unavailable-state",
            cache_root=self.temp_dir / "empty-cache",
        )
        with self.assertRaisesRegex(
            RuntimeError, "verified local cache is unavailable"
        ):
            unavailable.acquire_cached(("adaface_net",))

    def test_environment_selected_cache_prepares_real_sources_without_publisher_access(self) -> None:
        variable = "QUALSIGHT_SOURCE_CACHE_ROOT"
        previous = os.environ.get(variable)
        verified_cache = (ROOT / "var" / "upstream").resolve(strict=True)
        os.environ[variable] = str(verified_cache)
        try:
            settings = Settings.from_environment(repository_root=ROOT)
            self.assertEqual(settings.upstream_cache_root, verified_cache)
            upstream = UpstreamSources(
                UPSTREAM_MANIFEST,
                self.temp_dir / "environment-cache-state",
                cache_root=settings.upstream_cache_root,
            )
            acquired = upstream.acquire()
            self.assertEqual(set(acquired), set(upstream.sources))
            self.assertTrue(all(path == upstream.verify(key) for key, path in acquired.items()))
        finally:
            if previous is None:
                os.environ.pop(variable, None)
            else:
                os.environ[variable] = previous

    def test_offline_process_refuses_the_explicit_setup_exception(self) -> None:
        variable = "QUALSIGHT_SETUP_NETWORK_DISABLED"
        previous = os.environ.get(variable)
        os.environ[variable] = "1"
        try:
            with self.assertRaisesRegex(
                NetworkGuardViolation, "explicit setup network is disabled"
            ):
                with permit_explicit_setup_network():
                    self.fail("disabled setup exception admitted execution")
        finally:
            if previous is None:
                os.environ.pop(variable, None)
            else:
                os.environ[variable] = previous

    def test_checkpoint_status_rehashes_after_bytes_change_with_restored_mtime(self) -> None:
        registry = load_resource_registry(REGISTRY_PATH)
        upstream = UpstreamSources(UPSTREAM_MANIFEST, self.temp_dir / "state")
        manager = MatcherRuntimeManager(registry, upstream)
        matcher_id = MatcherId(ELASTICFACE_RESOURCE_ID)
        registered_identity = manager.checkpoints.registered_identity(ELASTICFACE_RESOURCE_ID)
        source = Path(registered_identity.path)
        managed = manager.checkpoints.managed_path(ELASTICFACE_RESOURCE_ID)
        managed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, managed)

        initial = manager._checkpoint_status(matcher_id)
        self.assertEqual(initial["state"], "VERIFIED")
        original_stat = managed.stat()
        with managed.open("r+b") as handle:
            first = handle.read(1)
            self.assertEqual(len(first), 1)
            handle.seek(0)
            handle.write(bytes((first[0] ^ 0x01,)))
            handle.flush()
            os.fsync(handle.fileno())
        os.utime(
            managed,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            follow_symlinks=False,
        )
        self.assertEqual(managed.stat().st_size, original_stat.st_size)
        self.assertEqual(managed.stat().st_mtime_ns, original_stat.st_mtime_ns)

        observed = manager._checkpoint_status(matcher_id)
        self.assertEqual(observed["state"], "DRIFTED")
        self.assertNotEqual(observed["observed_sha256"], registered_identity.sha256)

    def test_dataset_addon_blocks_a_post_startup_missing_real_tree_member(self) -> None:
        registered = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        resource = next(
            item for item in registered["resources"] if item["resource_id"] == LFW_RESOURCE_ID
        )
        tree_check = next(item for item in resource["checks"] if item["type"] == "tree_manifest")
        manifest_path = Path(tree_check["manifest_path"])
        first_record = json.loads(manifest_path.read_text(encoding="utf-8").splitlines()[0])
        registered_tree = Path(tree_check["root"])
        derived_tree = self.temp_dir / "lfw_funneled_registered_real_copy"
        shutil.copytree(registered_tree, derived_tree, copy_function=shutil.copy2)
        archive_path = Path(
            next(
                item["path"]
                for item in resource["artifacts"]
                if item["role"] == "DATA" and item["path"].endswith("lfw-funneled.tgz")
            )
        )
        registry = load_resource_registry(
            self._registry_with_lfw_paths(archive_path=archive_path, tree_root=derived_tree)
        )
        (derived_tree / first_record["path"]).unlink()

        addon = dataset_addons(registry)[0]
        self.assertEqual(addon["installation_state"], "DRIFTED")
        self.assertEqual(addon["runtime_state"], "BLOCKED")
        self.assertEqual(addon["verification"]["archive"]["state"], "VERIFIED")
        self.assertEqual(addon["verification"]["tree"]["state"], "DRIFTED")

    def test_dataset_addon_blocks_a_post_startup_corrupt_real_archive(self) -> None:
        registered = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        resource = next(
            item for item in registered["resources"] if item["resource_id"] == LFW_RESOURCE_ID
        )
        archive = next(
            item
            for item in resource["artifacts"]
            if item["role"] == "DATA" and item["path"].endswith("lfw-funneled.tgz")
        )
        registered_archive = Path(archive["path"])
        derived_archive = self.temp_dir / "registered-real-copy" / "lfw-funneled.tgz"
        derived_archive.parent.mkdir()
        shutil.copy2(registered_archive, derived_archive)
        tree_root = Path(
            next(item["root"] for item in resource["checks"] if item["type"] == "tree_manifest")
        )
        registry = load_resource_registry(
            self._registry_with_lfw_paths(archive_path=derived_archive, tree_root=tree_root)
        )
        with derived_archive.open("r+b") as handle:
            first = handle.read(1)
            self.assertEqual(len(first), 1)
            handle.seek(0)
            handle.write(bytes((first[0] ^ 0x01,)))
            handle.flush()
            os.fsync(handle.fileno())

        addon = dataset_addons(registry)[0]
        self.assertEqual(addon["installation_state"], "DRIFTED")
        self.assertEqual(addon["runtime_state"], "BLOCKED")
        self.assertEqual(addon["verification"]["archive"]["state"], "DRIFTED")


if __name__ == "__main__":
    unittest.main()
