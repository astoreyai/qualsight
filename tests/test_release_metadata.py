from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseMetadataTests(unittest.TestCase):
    def test_database_snapshot_cli_and_independent_audits_fail_closed(self) -> None:
        live_database = ROOT / "var" / "qualsight.sqlite3"
        self.assertTrue(live_database.is_file())

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            missing_snapshot = temporary_root / "missing.snapshot.sqlite3"
            missing = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "audit_local_data_controls.py"),
                    "--data-dir",
                    str(ROOT / "var"),
                    "--database-snapshot",
                    str(missing_snapshot),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(missing.returncode, 1, missing.stdout + missing.stderr)
            self.assertIn("database snapshot does not exist", missing.stdout)

            wrong_schema = temporary_root / "wrong-schema.snapshot.sqlite3"
            shutil.copy2(ROOT / "uv.lock", wrong_schema)
            invalid = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "audit_local_data_controls.py"),
                    "--data-dir",
                    str(ROOT / "var"),
                    "--database-snapshot",
                    str(wrong_schema),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(invalid.returncode, 1, invalid.stdout + invalid.stderr)
            self.assertIn("LOCAL_DATA_CONTROLS_FAIL", invalid.stdout)

            snapshot_path = temporary_root / "release-audit.snapshot.sqlite3"
            environment = {
                **os.environ,
                "PYTHONPATH": f"{ROOT / 'backend' / 'src'}:{ROOT}",
                "QUALSIGHT_DATA_DIR": str(ROOT / "var"),
                "QUALSIGHT_REPOSITORY_ROOT": str(ROOT),
            }
            created = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "qualsight.cli",
                    "snapshot-database",
                    "--output",
                    str(snapshot_path),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
            record = json.loads(created.stdout)
            self.assertEqual(Path(record["path"]), snapshot_path)
            self.assertEqual(record["size_bytes"], snapshot_path.stat().st_size)
            self.assertEqual(stat.S_IMODE(snapshot_path.stat().st_mode), 0o600)
            self.assertEqual(
                record["sha256"], hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
            )
            connection = sqlite3.connect(
                f"{snapshot_path.resolve(strict=True).as_uri()}?mode=ro&immutable=1",
                uri=True,
            )
            try:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            finally:
                connection.close()

        backend_source = ROOT / "backend" / "src"
        sys.path.insert(0, str(backend_source))
        try:
            from qualsight.security.process_lease import DataDirectoryLease

            lease = DataDirectoryLease.acquire(ROOT / "var", owner="release-regression")
            try:
                busy = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "audit_local_data_controls.py"),
                        "--data-dir",
                        str(ROOT / "var"),
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            finally:
                lease.close()
        finally:
            sys.path.remove(str(backend_source))
        self.assertEqual(busy.returncode, 1, busy.stdout + busy.stderr)
        self.assertIn("application data directory is already in use", busy.stdout)

    def test_cross_matcher_verifier_refuses_the_real_live_database(self) -> None:
        module = _load_script("verify_cross_matcher")
        live_database = ROOT / "var" / "qualsight.sqlite3"
        self.assertTrue(live_database.is_file())
        with self.assertRaisesRegex(ValueError, "live database is not accepted"):
            module._validated_snapshot_path(live_database)
        with tempfile.TemporaryDirectory(dir=ROOT / "var") as temporary:
            live_alias = Path(temporary) / "live-alias.snapshot.sqlite3"
            os.link(live_database, live_alias)
            with self.assertRaisesRegex(ValueError, "single-link"):
                module._validated_snapshot_path(live_alias)

        for relative in ("docs/BETA_ACCEPTANCE.md", "docs/USER_GUIDE.md"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("snapshot-database", text, relative)
            self.assertNotIn(
                'verify_cross_matcher.py "$CROSS_DIR" "$QUALSIGHT_DATA_DIR/qualsight.sqlite3"',
                text,
                relative,
            )

    def test_source_archive_secret_gate_covers_github_fine_grained_tokens(self) -> None:
        module = _load_script("create_release_source_archive")
        patterns = {label: pattern.pattern for label, pattern in module.SECRET_PATTERNS}
        self.assertIn("GitHub fine-grained access token", patterns)
        self.assertIn(b"github_pat_", patterns["GitHub fine-grained access token"])

    def test_backend_source_has_no_empty_loop_implementations(self) -> None:
        offenders: list[str] = []
        for path in sorted((ROOT / "backend" / "src").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.For, ast.AsyncFor, ast.While)) and len(node.body) == 1:
                    if isinstance(node.body[0], ast.Pass):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_release_selects_reference_node_before_frontend_toolchain_gate(self) -> None:
        release_script = (ROOT / "scripts" / "release.sh").read_text(encoding="utf-8")
        reference_path = 'export PATH="/usr/bin:$PATH"'
        toolchain_gate = 'pnpm --dir "$BUILD_ROOT" run verify:toolchain'
        self.assertIn(reference_path, release_script)
        self.assertLess(release_script.index(reference_path), release_script.index(toolchain_gate))
        preamble = release_script[: release_script.index('if [ -n "$(git status')]
        shadow_node = Path.home() / ".local" / "bin" / "node"
        self.assertTrue(shadow_node.is_file())
        observed_shadow = subprocess.run(
            [str(shadow_node), "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertNotEqual(observed_shadow, "v20.20.2")
        observed_release = subprocess.run(
            ["bash", "-c", f"{preamble}\nexec node --version"],
            cwd=ROOT,
            env={**os.environ, "PATH": f"{shadow_node.parent}:/usr/bin:/bin"},
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(observed_release, "v20.20.2")

    def test_source_archive_excludes_internal_runtime_records_and_real_result_fixture(self) -> None:
        module = _load_script("create_release_source_archive")
        policy = json.loads(
            (ROOT / "resources/security/release-package-policy-v1.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "qualsight-source.tar.gz"
            result = module.write_source_archive(
                root=ROOT,
                output=archive_path,
                commit="HEAD",
                prefix="qualsight-source-test",
            )
            with tarfile.open(archive_path, mode="r:gz") as archive:
                names = set(archive.getnames())
            self.assertEqual(
                result["excluded_repository_paths"],
                policy["source_archive_excluded_paths"],
            )
            for path in policy["source_archive_excluded_paths"]:
                member = f"qualsight-source-test/{path}"
                self.assertNotIn(member, names)
                self.assertFalse(any(name.startswith(f"{member}/") for name in names))
            self.assertIn("qualsight-source-test/backend/src/qualsight/api/app.py", names)

    def test_source_archive_rejects_real_biometric_bytes_in_an_included_path(self) -> None:
        module = _load_script("create_release_source_archive")
        first_record = json.loads(
            (ROOT / "resources/manifests/lfw-funneled-images-v1.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        source = Path("/mnt/projects/datasets/lfw/lfw_funneled") / first_record["path"]
        self.assertEqual(source.stat().st_size, first_record["size_bytes"])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), first_record["sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            included = repository / "assets" / "registered.jpg"
            included.parent.mkdir()
            shutil.copy2(source, included)
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            commit_environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "QualSight release regression",
                "GIT_AUTHOR_EMAIL": "release-regression@localhost",
                "GIT_COMMITTER_NAME": "QualSight release regression",
                "GIT_COMMITTER_EMAIL": "release-regression@localhost",
            }
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "Bind registered real image"],
                cwd=repository,
                env=commit_environment,
                check=True,
            )
            archive_path = Path(temporary) / "forbidden-source.tar.gz"
            with self.assertRaisesRegex(ValueError, "forbidden release content"):
                module.write_source_archive(
                    root=repository,
                    output=archive_path,
                    commit="HEAD",
                    prefix="qualsight-source-test",
                    policy_path=ROOT / "resources/security/release-package-policy-v1.json",
                )
            self.assertFalse(archive_path.exists())

    def test_source_archive_rejects_a_credential_filename_with_real_config_bytes(self) -> None:
        module = _load_script("create_release_source_archive")
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            shutil.copy2(ROOT / ".npmrc", repository / ".env")
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            commit_environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "QualSight release regression",
                "GIT_AUTHOR_EMAIL": "release-regression@localhost",
                "GIT_COMMITTER_NAME": "QualSight release regression",
                "GIT_COMMITTER_EMAIL": "release-regression@localhost",
            }
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "Bind real package-manager config"],
                cwd=repository,
                env=commit_environment,
                check=True,
            )
            archive_path = Path(temporary) / "forbidden-source.tar.gz"
            with self.assertRaisesRegex(ValueError, "forbidden release content credential path"):
                module.write_source_archive(
                    root=repository,
                    output=archive_path,
                    commit="HEAD",
                    prefix="qualsight-source-test",
                    policy_path=ROOT / "resources/security/release-package-policy-v1.json",
                )
            self.assertFalse(archive_path.exists())

    def test_vulnerability_waiver_reachability_controls_match_real_loader(self) -> None:
        module = _load_script("audit_vulnerabilities")
        result = module.validate_reachability_controls(ROOT)
        self.assertGreaterEqual(result["weights_only_loader_count"], 1)
        self.assertGreaterEqual(result["strict_state_loader_count"], 1)
        self.assertEqual(result["torch_jit_script_reference_count"], 0)
        self.assertEqual(result["torch_compile_reference_count"], 0)
        expected_sources = {
            *ROOT.joinpath("backend", "src").rglob("*.py"),
            *ROOT.joinpath("resources", "upstream").rglob("*.py"),
        }
        self.assertEqual(result["repository_source_count"], len(expected_sources))
        self.assertEqual(set(result["source_sha256"]), {path.relative_to(ROOT).as_posix() for path in expected_sources})
        upstream_manifest = json.loads(
            ROOT.joinpath(
                "resources", "manifests", "matcher-upstream-sources-v1.json"
            ).read_text(encoding="utf-8")
        )
        expected_upstream = {
            item["key"]: item["sha256"] for item in upstream_manifest["sources"]
        }
        self.assertEqual(
            result["scanned_source_count"], len(expected_sources) + len(expected_upstream)
        )
        self.assertEqual(result["governed_upstream_source_sha256"], expected_upstream)
        self.assertIn("adaface_net", result["governed_upstream_source_sha256"])
        self.assertIn("transface_vit", result["governed_upstream_source_sha256"])
        self.assertEqual(
            set(result["executed_upstream_source_keys"]),
            {"adaface_net", "insightface_arcface_torch_iresnet", "transface_vit"},
        )

    def test_governed_upstream_scan_fails_closed_on_real_source_faults(self) -> None:
        module = _load_script("audit_vulnerabilities")
        manifest_path = (
            ROOT / "resources" / "manifests" / "matcher-upstream-sources-v1.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        rows = manifest["sources"]
        cache_paths = {
            item["key"]: module._governed_cache_path(ROOT, item) for item in rows
        }
        self.assertTrue(all(path.is_file() for path in cache_paths.values()))

        smallest = min(rows, key=lambda item: int(item["size_bytes"]))
        largest = max(rows, key=lambda item: int(item["size_bytes"]))
        with cache_paths[largest["key"]].open("rb") as real_source:
            with self.assertRaisesRegex(RuntimeError, "exceeds its declared size"):
                module._read_source_body(
                    real_source, largest["key"], int(smallest["size_bytes"])
                )

        with tempfile.TemporaryDirectory() as temporary:
            staged = Path(temporary)
            staged_manifest = (
                staged
                / "resources"
                / "manifests"
                / "matcher-upstream-sources-v1.json"
            )
            staged_manifest.parent.mkdir(parents=True)
            shutil.copy2(manifest_path, staged_manifest)
            with self.assertRaisesRegex(RuntimeError, "is absent and network acquisition is disabled"):
                module._governed_upstream_sources(staged, allow_network=False)

            for item in rows:
                source = cache_paths[item["key"]]
                destination = module._governed_cache_path(staged, item)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            drifted = module._governed_cache_path(staged, largest)
            # This is a one-byte-short subsample of the real pinned source, never fabricated content.
            drifted.write_bytes(drifted.read_bytes()[:-1])
            with self.assertRaisesRegex(RuntimeError, "differs from its pin"):
                module._governed_upstream_sources(staged, allow_network=False)

            shutil.copy2(cache_paths[largest["key"]], drifted)
            guarded = module._governed_cache_path(staged, rows[0])
            guarded.unlink()
            guarded.symlink_to(cache_paths[rows[0]["key"]])
            with self.assertRaisesRegex(RuntimeError, "is not a regular file"):
                module._governed_upstream_sources(staged, allow_network=False)

            guarded.unlink()
            os.mkfifo(guarded)
            with self.assertRaisesRegex(RuntimeError, "is not a regular file"):
                module._governed_upstream_sources(staged, allow_network=False)

        with self.assertRaises(SyntaxError):
            module._source_controls_text(
                (ROOT / "uv.lock").read_text(encoding="utf-8"), "real-repository/uv.lock"
            )

    def test_dependency_inventory_covers_installed_transitive_packages_and_locks_them(self) -> None:
        module = _load_script("audit_dependencies")
        python_rows = module.python_inventory()
        node_rows = module.node_inventory()
        self.assertTrue(all(row["pinned"] == "yes" for row in python_rows + node_rows))
        installed = {(row["name"], row["version"]) for row in node_rows}
        self.assertIn(("react", "18.3.1"), installed)
        self.assertIn(("loose-envify", "1.4.0"), installed)
        self.assertGreater(len(node_rows), 300)

    def test_sbom_is_derived_from_real_locks_and_is_deterministic(self) -> None:
        module = _load_script("generate_release_sbom")
        first = module.build_sbom(ROOT)
        second = module.build_sbom(ROOT)
        self.assertEqual(first, second)
        self.assertEqual(first["bomFormat"], "CycloneDX")
        self.assertEqual(first["specVersion"], "1.5")
        components = {
            (component["properties"][0]["value"], component["name"], component["version"]): component
            for component in first["components"]
        }
        self.assertIn(("python", "fastapi", "0.141.1"), components)
        self.assertIn(("javascript", "react", "18.3.1"), components)
        frontend = [
            component
            for component in first["components"]
            if component["properties"][0]["value"] == "javascript"
        ]
        locked_frontend_count = len(
            yaml.safe_load((ROOT / "pnpm-lock.yaml").read_text(encoding="utf-8"))["packages"]
        )
        self.assertEqual(len(frontend), locked_frontend_count)
        self.assertTrue(
            all(component["hashes"][0]["alg"] == "SHA-512" for component in frontend)
        )
        dependency_graph = {row["ref"]: set(row["dependsOn"]) for row in first["dependencies"]}
        fastapi_ref = components[("python", "fastapi", "0.141.1")]["bom-ref"]
        pydantic_ref = components[("python", "pydantic", "2.13.4")]["bom-ref"]
        vite_ref = components[("javascript", "vite", "7.3.6")]["bom-ref"]
        esbuild_ref = components[("javascript", "esbuild", "0.28.2")]["bom-ref"]
        pip_audit_ref = components[("python", "pip-audit", "2.10.0")]["bom-ref"]
        build_ref = components[("build", "QualSight release toolchain", "0.1.0b1")]["bom-ref"]
        self.assertIn(pydantic_ref, dependency_graph[fastapi_ref])
        self.assertIn(esbuild_ref, dependency_graph[vite_ref])
        self.assertIn(pip_audit_ref, dependency_graph[build_ref])
        application_ref = first["metadata"]["component"]["bom-ref"]
        self.assertLess(len(dependency_graph[application_ref]), len(first["components"]))
        known_references = {application_ref, *(component["bom-ref"] for component in first["components"])}
        self.assertEqual(set(dependency_graph), known_references)
        self.assertTrue(
            all(target in known_references for targets in dependency_graph.values() for target in targets)
        )

    def test_manifest_hashes_real_staged_files_and_refuses_overwrite(self) -> None:
        module = _load_script("generate_release_manifest")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            artifacts = [ROOT / "uv.lock", ROOT / "pnpm-lock.yaml"]
            output = directory / "manifest.json"
            expected_tree = subprocess.run(
                ["git", "rev-parse", "8abdf3eac6bdf91e5a347fe3e50bab172c83f052^{tree}"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            module.write_manifest(
                root=ROOT,
                output=output,
                version="0.1.0b1",
                commit="8abdf3eac6bdf91e5a347fe3e50bab172c83f052",
                tree=expected_tree,
                artifacts=artifacts,
            )
            value = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(value["artifacts"]), 2)
            self.assertEqual(value["component_versions"]["vite"], "7.3.6")
            self.assertEqual(value["git_tree"], expected_tree)
            self.assertFalse(value["deployment_boundary"]["portable_turnkey_install"])
            self.assertFalse(value["bundled_biometric_assets"])
            self.assertTrue(value["source_archive_policy"]["secret_scan"])
            self.assertIn(".jpg", value["source_archive_policy"]["forbidden_suffixes"])
            self.assertIn(".pth", value["source_archive_policy"]["forbidden_suffixes"])
            self.assertEqual(
                value["source_archive_policy"]["excluded_repository_paths"],
                [
                    "AGENTS.md",
                    "ATG_LOOP.md",
                    "ATG_PLAN.md",
                    "IMPLEMENTATION_PLAN.md",
                    "PROMPT.md",
                    "atg",
                    "backend/tests/fixtures",
                    "docs/performance",
                ],
            )
            with self.assertRaises(FileExistsError):
                module.write_manifest(
                    root=ROOT,
                    output=output,
                    version="0.1.0b1",
                    commit="8abdf3eac6bdf91e5a347fe3e50bab172c83f052",
                    tree=expected_tree,
                    artifacts=artifacts,
                )


if __name__ == "__main__":
    unittest.main()
