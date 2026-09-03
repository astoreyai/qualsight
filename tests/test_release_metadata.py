from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import inspect
import io
import json
import os
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
from unittest import mock
from pathlib import Path, PurePosixPath
from urllib.parse import quote_from_bytes

import yaml


ROOT = Path(__file__).resolve().parent.parent

RELEASE_SNAPSHOT_FILES = (
    "backend/pyproject.toml",
    "backend/src/qualsight/api/settings.py",
    "backend/src/qualsight/datasets.py",
    "backend/src/qualsight/engines/ofiq/batch.py",
    "docs/INSTALL.md",
    "frontend/package.json",
    "package.json",
    "resources/release-evidence/addon-acquisition-public-summary-v1.json",
    "resources/security/release-package-policy-v2.json",
    "scripts/create_release_source_archive.py",
    "scripts/controlled_inputs.py",
    "scripts/audit_console.py",
    "scripts/install.sh",
    "scripts/observe_demo.py",
    "scripts/run_release_beta_gate.py",
    "scripts/validate_install_preflight.py",
    "scripts/validate_release_browser.py",
    "scripts/validate_release_package.py",
    "uv.lock",
)

BETA3_BROWSER_VALIDATORS = (
    "validate_comparison_studio.py",
    "validate_evidence_warning_privacy.py",
    "validate_manage_system.py",
    "validate_owner_purge_ui.py",
    "validate_stress_studio.py",
    "validate_structured_evidence_navigation.py",
    "validate_upload_working_sample.py",
    "validate_utility_overlay.py",
    "observe_console.py",
)


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    script_directory = str(path.parent)
    sys.path.insert(0, script_directory)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(script_directory)
    return module


def _release_snapshot(destination: Path) -> Path:
    """Commit the real checkout plus the packaging files under active review."""

    repository = destination / "release-snapshot"
    repository.mkdir()
    archive_path = destination / "committed-head.tar"
    subprocess.run(
        ["git", "archive", "--format=tar", "-o", str(archive_path), "HEAD"],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(["tar", "-xf", str(archive_path), "-C", str(repository)], check=True)
    for relative in (*RELEASE_SNAPSHOT_FILES, *(f"scripts/{name}" for name in BETA3_BROWSER_VALIDATORS)):
        source = ROOT / relative
        if not source.is_file():
            raise AssertionError(f"release snapshot input is absent: {relative}")
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    superseded_policy = repository / "resources/security/release-package-policy-v1.json"
    if superseded_policy.exists() and not (
        ROOT / "resources/security/release-package-policy-v1.json"
    ).exists():
        superseded_policy.unlink()
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "QualSight release regression",
        "GIT_AUTHOR_EMAIL": "release-regression@localhost",
        "GIT_COMMITTER_NAME": "QualSight release regression",
        "GIT_COMMITTER_EMAIL": "release-regression@localhost",
    }
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "Bind real release candidate"],
        cwd=repository,
        env=environment,
        check=True,
    )
    return repository


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

    def test_release_uses_the_active_reviewed_node_without_path_rewriting(self) -> None:
        release_script = (ROOT / "scripts" / "release.sh").read_text(encoding="utf-8")
        toolchain_gate = '"$PNPM" --dir "$BUILD_ROOT" run verify:toolchain'
        self.assertNotIn('export PATH="/usr/bin:$PATH"', release_script)
        self.assertIn(toolchain_gate, release_script)
        preamble = release_script[: release_script.index('if [ -n "$(git status')]
        reviewed_node = Path(shutil.which("node") or "")
        self.assertTrue(reviewed_node.is_file())
        observed_reviewed = subprocess.run(
            [str(reviewed_node), "--version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(observed_reviewed, "v26.7.0")
        observed_release = subprocess.run(
            ["bash", "-c", f"{preamble}\nexec node --version"],
            cwd=ROOT,
            env=os.environ,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(observed_release, "v26.7.0")

        browser_scripts = (
            ROOT / "scripts" / "demo.sh",
            ROOT / "scripts" / "docs_workflow_gate.py",
            ROOT / "scripts" / "validate_release_browser.py",
            *(ROOT / "scripts" / name for name in BETA3_BROWSER_VALIDATORS),
        )
        for browser_script in browser_scripts:
            with self.subTest(browser_script=browser_script.name):
                source = browser_script.read_text(encoding="utf-8")
                self.assertNotIn('export PATH="/usr/bin:$PATH"', source)
                self.assertNotIn('"PATH": f"/usr/bin:', source)
                self.assertNotIn('NODE_BIN = "/usr/bin"', source)

    def test_backend_release_timeout_exceeds_the_recorded_real_suite_baseline(self) -> None:
        module = _load_script("run_release_beta_gate")
        gates = module.gate_definitions(ROOT, sys.executable)
        backend_gate = next(gate for gate in gates if gate.name == "backend-tests")
        recorded_real_baseline_seconds = 4047.221
        self.assertGreaterEqual(
            backend_gate.timeout_seconds,
            int(recorded_real_baseline_seconds * 1.25) + 1,
        )

    def test_staged_browser_gate_receives_the_verified_deployment_source_cache(self) -> None:
        module = _load_script("run_release_beta_gate")
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        with tempfile.TemporaryDirectory() as temporary:
            staged_root = Path(temporary)
            gates = module.gate_definitions(
                staged_root,
                sys.executable,
                work_root=staged_root / "work",
                deployment_root=ROOT,
                committed_source_commit=commit,
            )
        browser_gate = next(
            gate for gate in gates if gate.name == "browser-beta3-workflows"
        )
        environment = dict(browser_gate.environment)
        self.assertEqual(
            environment["QUALSIGHT_SOURCE_CACHE_ROOT"],
            str((ROOT / "var" / "upstream").resolve(strict=True)),
        )
        self.assertNotEqual(environment["QUALSIGHT_SOURCE_CACHE_ROOT"], str(staged_root))
        self.assertEqual(environment["QUALSIGHT_COMMITTED_STAGE_ROOT"], str(staged_root))
        self.assertEqual(environment["QUALSIGHT_COMMITTED_STAGE_COMMIT"], commit)

    def test_offline_gate_uses_the_explicit_verified_source_cache_root(self) -> None:
        module = _load_script("offline_gate")
        verified_cache = (ROOT / "var" / "upstream").resolve(strict=True)
        with mock.patch.dict(
            os.environ,
            {"QUALSIGHT_SOURCE_CACHE_ROOT": str(verified_cache)},
            clear=False,
        ):
            self.assertEqual(
                module._resolve_source_cache_root(ROOT),
                verified_cache,
            )
        with tempfile.TemporaryDirectory() as temporary:
            linked_cache = Path(temporary) / "linked-cache"
            linked_cache.symlink_to(verified_cache, target_is_directory=True)
            with mock.patch.dict(
                os.environ,
                {"QUALSIGHT_SOURCE_CACHE_ROOT": str(linked_cache)},
                clear=False,
            ), self.assertRaisesRegex(
                ValueError,
                "absolute, existing, non-symlink directory",
            ):
                module._resolve_source_cache_root(ROOT)

    def test_release_extracts_and_validates_its_projected_source_before_publication(self) -> None:
        release = (ROOT / "scripts" / "release.sh").read_text(encoding="utf-8")
        create = '"$BUILD_ROOT/scripts/create_release_source_archive.py"'
        extract = (
            'tar --extract --gzip --file "$SOURCE_ARCHIVE" '
            '--directory "$PACKAGE_VALIDATION_ROOT"'
        )
        validate = (
            '"$BUILD_ROOT/scripts/validate_release_package.py" '
            '--root "$PACKAGE_VALIDATION_ROOT/$NAME" --expected-commit "$COMMIT"'
        )
        publish = 'mv -T -n "$SET" "$FINAL_DIR"'
        for command in (create, extract, validate, publish):
            self.assertIn(command, release)
        self.assertLess(release.index(create), release.index(extract))
        self.assertLess(release.index(extract), release.index(validate))
        self.assertLess(release.index(validate), release.index(publish))
        self.assertIn("--no-same-owner --no-same-permissions", release)
        umask = "umask 022"
        self.assertIn(umask, release)
        self.assertLess(release.index(umask), release.index('mkdir -p "$OUT"'))
        self.assertLess(release.index(umask), release.index('mkdir "$SET"'))
        self.assertLess(release.index(umask), release.index(create))
        self.assertNotIn(
            '"$PACKAGE_VALIDATION_ROOT/$NAME/scripts/validate_release_package.py"',
            release,
        )

    def test_packaged_startup_instructions_acquire_sources_before_offline_execution(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        install = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        setup_command = "python -m qualsight.cli setup"
        for name, document in (("README.md", readme), ("docs/INSTALL.md", install)):
            with self.subTest(document=name):
                self.assertIn(setup_command, document)
                self.assertLess(document.index(setup_command), document.index("scripts/offline_gate.py"))
        self.assertNotIn("metric-registry and ATG governance validation", install)
        self.assertIn("ATG governance validation runs only", install)
        self.assertIn("scripts/validate_release_browser.py", install)
        self.assertIn('--work-root "$BROWSER_WORK"', install)

    def test_source_archive_excludes_internal_runtime_records_and_real_result_fixture(self) -> None:
        module = _load_script("create_release_source_archive")
        policy = json.loads(
            (ROOT / "resources/security/release-package-policy-v2.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            repository = _release_snapshot(Path(temporary))
            archive_path = Path(temporary) / "qualsight-source.tar.gz"
            result = module.write_source_archive(
                root=repository,
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

    def test_release_package_policy_excludes_private_and_nonportable_material(self) -> None:
        policy = json.loads(
            (ROOT / "resources/security/release-package-policy-v2.json").read_text(
                encoding="utf-8"
            )
        )
        exclusions = set(policy["source_archive_excluded_paths"])
        self.assertEqual(
            policy["policy_id"],
            "qualsight-controlled-workstation-beta-release-v2",
        )
        self.assertTrue(
            {
                "PLAN.md",
                "resources/authorizations",
                "resources/upstream",
                "docs/BASELINE_2026-09-01_IMAGE_WORKFLOW.md",
                "scripts/repin_registry_identity.py",
                "scripts/append_ledger_record.py",
                "scripts/release.sh",
                "scripts/run_release_beta_gate.py",
                "scripts/validate_atg_documents.py",
                "scripts/validate_lfw_protocol_authority.py",
                "scripts/validate_scrfd_release_provenance.py",
                "scripts/validate_shared_crop_lineage.py",
            }.issubset(exclusions)
        )
        forbidden_suffixes = set(policy["source_archive_forbidden_suffixes"])
        self.assertTrue(
            {".7z", ".gz", ".oci", ".tar", ".tgz", ".zip"}.issubset(
                forbidden_suffixes
            )
        )
        forbidden_terms = set(policy["source_archive_forbidden_content_terms"])
        self.assertTrue(
            {
                "/home/",
                "/mnt/",
                "/tmp/",
                "assistant/analysis",
                "anthropic",
                "claude code",
                "co-authored-by:",
                "generated by claude",
                "session_id",
                "thread_id",
                "turn_id",
            }.issubset(forbidden_terms)
        )
        self.assertEqual(
            set(policy["source_archive_reference_uv_files"]),
            {
                "scripts/docs_workflow_gate.py",
                "scripts/observe_console.py",
                "scripts/offline_gate.py",
            },
        )
        self.assertEqual(
            set(policy["source_archive_reference_projects_files"]),
            {
                "docs/USER_GUIDE.md",
                "resources/manifests/insightface-buffalo-l-release-provenance-v1.json",
                "resources/manifests/lfw-full-v1-shared-crop-lineage-reconciliation.json",
                "resources/manifests/lfw-official-protocol-authority-v1.json",
                "resources/manifests/lfw-subset-fixture-v1.json",
                "resources/resource-registry.json",
                "scripts/build_ofiq_landmark_app.sh",
            },
        )
        builder_body = (ROOT / "scripts/create_release_source_archive.py").read_bytes()
        controlled_home = str(Path.home().resolve()).encode("utf-8")
        encoded_home = ", ".join(str(value) for value in controlled_home).encode("ascii")
        self.assertNotIn(controlled_home, builder_body)
        self.assertNotIn(encoded_home, builder_body)
        builder = _load_script("create_release_source_archive")
        real_uv_locator_body = (ROOT / "scripts/docs_workflow_gate.py").read_bytes()
        with self.assertRaisesRegex(ValueError, "outside its exact file allowlist"):
            builder._project_release_members(
                {PurePosixPath("README.md"): real_uv_locator_body},
                policy,
            )
        with self.assertRaisesRegex(ValueError, "project root.*exact file allowlist"):
            builder._project_release_members(
                {
                    PurePosixPath("README.md"): (
                        str(ROOT.resolve()) + "/README.md"
                    ).encode("utf-8")
                },
                policy,
            )

    def test_install_refuses_wrong_toolchain_and_validates_package_before_mutation(self) -> None:
        preflight = _load_script("validate_install_preflight")
        package_validator = _load_script("validate_release_package")
        self.assertEqual(
            preflight.EXPECTED_PLATFORM,
            {
                "system": "Linux",
                "machine": "x86_64",
                "python": "Python 3.11.2",
                "uv": "uv 0.9.26",
                "node": "v26.7.0",
                "pnpm": "10.28.0",
            },
        )
        wrong_node = Path("/usr/bin/node")
        self.assertEqual(
            subprocess.run(
                [str(wrong_node), "--version"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "v20.20.2",
        )
        refused = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "validate_install_preflight.py"),
                "--python",
                "/usr/bin/python3.11",
                "--uv",
                str(Path(shutil.which("uv") or "").resolve()),
                "--node",
                str(wrong_node),
                "--pnpm",
                str(Path(shutil.which("pnpm") or "").resolve()),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("node version differs", refused.stderr)

        install = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        preflight_call = '"$PYTHON" scripts/validate_install_preflight.py'
        package_call = '"$PYTHON" scripts/validate_release_package.py --root "$ROOT"'
        registry_call = '"$PYTHON" scripts/validate_resource_registry.py "$REGISTRY"'
        first_mutation = '"$UV" sync --frozen --directory backend'
        self.assertIn("--validate-package-only", install)
        self.assertLess(install.index(preflight_call), install.index(first_mutation))
        self.assertLess(install.index(package_call), install.index(first_mutation))
        self.assertLess(install.index(registry_call), install.index(first_mutation))
        self.assertNotIn("validate_atg_documents.py", install)
        self.assertNotIn("python3 scripts/validate_", install)
        self.assertNotIn("python3 scripts/", install)
        self.assertIn('"$PNPM" install --frozen-lockfile', install)
        self.assertIn('"$PNPM" --dir frontend run build', install)
        self.assertIn(
            'CONTROLLED_ROOT="${QUALSIGHT_REPOSITORY_ROOT:-$ROOT}"',
            install,
        )
        self.assertIn(
            "this sanitized package requires QUALSIGHT_REPOSITORY_ROOT and "
            "QUALSIGHT_REGISTRY_PATH",
            install,
        )
        self.assertIn(
            'PACKAGE_COMMIT="$("$PYTHON" -c',
            install,
        )
        self.assertIn(
            'CONTROLLED_COMMIT="$(git -C "$CONTROLLED_ROOT" rev-parse HEAD)"',
            install,
        )
        self.assertIn(
            'if [ "$PACKAGE_COMMIT" != "$CONTROLLED_COMMIT" ]; then',
            install,
        )
        self.assertIn(
            "QUALSIGHT_REPOSITORY_ROOT=$CONTROLLED_ROOT "
            "QUALSIGHT_REGISTRY_PATH=$REGISTRY "
            "QUALSIGHT_CONSOLE_ROOT=$ROOT",
            install,
        )
        self.assertIn(
            '$ROOT/scripts/offline_gate.py --observation \\"$ROOT/var/offline-gate-install.json\\" '
            '--data-dir \\"$ROOT/var\\" --source-root \\"$ROOT\\" '
            '--repository-root \\"$CONTROLLED_ROOT\\" '
            '--console-index \\"$ROOT/frontend/dist/index.html\\" '
            '--python \\"$ROOT/.venv/bin/python\\"',
            install,
        )
        self.assertIn(
            Path("scripts/validate_metric_registry.py"),
            package_validator.REQUIRED_FILES,
        )
        self.assertIn(
            Path("scripts/validate_resource_registry.py"),
            package_validator.REQUIRED_FILES,
        )
        self.assertNotIn(
            Path("scripts/run_release_beta_gate.py"),
            package_validator.REQUIRED_FILES,
        )

    def test_installer_refuses_a_real_package_that_is_also_a_git_checkout(self) -> None:
        module = _load_script("create_release_source_archive")
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            repository = _release_snapshot(temporary_root)
            archive_path = temporary_root / "qualsight-source.tar.gz"
            module.write_source_archive(
                root=repository,
                output=archive_path,
                commit="HEAD",
                prefix="qualsight-source-test",
            )
            extracted = temporary_root / "extracted"
            extracted.mkdir()
            subprocess.run(
                ["tar", "-xzf", str(archive_path), "-C", str(extracted)],
                check=True,
            )
            package_root = extracted / "qualsight-source-test"
            subprocess.run(["git", "init", "--quiet"], cwd=package_root, check=True)

            refused = subprocess.run(
                ["bash", "scripts/install.sh", "--validate-package-only"],
                cwd=package_root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
            self.assertIn(
                "installation refused: root contains both package manifest and Git checkout authority",
                refused.stderr,
            )

    def test_installed_settings_revalidate_real_package_commit_and_registry(self) -> None:
        module = _load_script("create_release_source_archive")
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            repository = _release_snapshot(temporary_root)
            archive_path = temporary_root / "qualsight-source.tar.gz"
            module.write_source_archive(
                root=repository,
                output=archive_path,
                commit="HEAD",
                prefix="qualsight-source-test",
            )
            extracted = temporary_root / "extracted"
            extracted.mkdir()
            subprocess.run(
                ["tar", "-xzf", str(archive_path), "-C", str(extracted)],
                check=True,
            )
            package_root = extracted / "qualsight-source-test"
            registry = repository / "resources" / "resource-registry.json"
            authority_environment = {
                **os.environ,
                "QUALSIGHT_REPOSITORY_ROOT": str(repository),
                "QUALSIGHT_REGISTRY_PATH": str(registry),
            }
            authority_check = subprocess.run(
                ["bash", "scripts/install.sh", "--validate-package-only"],
                cwd=package_root,
                env=authority_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                authority_check.returncode,
                0,
                authority_check.stdout + authority_check.stderr,
            )
            self.assertIn("deployment authority passed", authority_check.stdout)

            projected_environment = {
                **authority_environment,
                "QUALSIGHT_REGISTRY_PATH": str(
                    package_root / "resources" / "resource-registry.json"
                ),
            }
            projected_check = subprocess.run(
                ["bash", "scripts/install.sh", "--validate-package-only"],
                cwd=package_root,
                env=projected_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                projected_check.returncode,
                2,
                projected_check.stdout + projected_check.stderr,
            )
            self.assertIn(
                "controlled-host registry differs from the controlled repository registry",
                projected_check.stderr,
            )

            relative_environment = {
                **authority_environment,
                "QUALSIGHT_REGISTRY_PATH": "resources/resource-registry.json",
            }
            relative_check = subprocess.run(
                ["bash", "scripts/install.sh", "--validate-package-only"],
                cwd=package_root,
                env=relative_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                relative_check.returncode,
                2,
                relative_check.stdout + relative_check.stderr,
            )
            self.assertIn(
                "QUALSIGHT_REGISTRY_PATH must be absolute",
                relative_check.stderr,
            )

            console_root = temporary_root / "installed-console"
            shutil.copytree(
                ROOT / "frontend" / "dist",
                console_root / "frontend" / "dist",
            )
            environment = {
                **os.environ,
                "PYTHONPATH": str(package_root / "backend" / "src"),
                "QUALSIGHT_REPOSITORY_ROOT": str(repository),
                "QUALSIGHT_REGISTRY_PATH": str(registry),
                "QUALSIGHT_CONSOLE_ROOT": str(console_root),
            }
            command = [
                sys.executable,
                "-c",
                (
                    "from qualsight.api.settings import Settings; "
                    "value = Settings.from_environment(); "
                    "print(value.repository_root); print(value.registry_path)"
                ),
            ]
            accepted = subprocess.run(
                command,
                cwd=package_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)

            copied_registry = temporary_root / "resource-registry.json"
            shutil.copy2(registry, copied_registry)
            copied_environment = {
                **environment,
                "QUALSIGHT_REGISTRY_PATH": str(copied_registry),
            }
            copied = subprocess.run(
                command,
                cwd=package_root,
                env=copied_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(copied.returncode, 0, copied.stdout + copied.stderr)

            substituted_environment = {
                **environment,
                "QUALSIGHT_REGISTRY_PATH": str(
                    package_root / "resources" / "resource-registry.json"
                ),
            }
            substituted = subprocess.run(
                command,
                cwd=package_root,
                env=substituted_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(substituted.returncode, 0)
            self.assertIn(
                "installed package registry differs from the controlled repository registry",
                substituted.stderr,
            )

            commit_environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "QualSight release regression",
                "GIT_AUTHOR_EMAIL": "release-regression@localhost",
                "GIT_COMMITTER_NAME": "QualSight release regression",
                "GIT_COMMITTER_EMAIL": "release-regression@localhost",
            }
            subprocess.run(
                ["git", "commit", "--quiet", "--allow-empty", "-m", "Advance controlled deployment"],
                cwd=repository,
                env=commit_environment,
                check=True,
            )
            advanced = subprocess.run(
                command,
                cwd=package_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(advanced.returncode, 0)
            self.assertIn(
                "installed package source commit differs from the controlled repository",
                advanced.stderr,
            )

    def test_environment_start_accepts_only_a_byte_exact_committed_release_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            repository = _release_snapshot(temporary_root)
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repository, text=True
            ).strip()
            stage_archive = temporary_root / "committed-stage.tar"
            subprocess.run(
                ["git", "archive", "--format=tar", "-o", str(stage_archive), commit],
                cwd=repository,
                check=True,
            )
            stage = temporary_root / "committed-stage"
            stage.mkdir()
            subprocess.run(
                ["tar", "-xf", str(stage_archive), "-C", str(stage)],
                check=True,
            )
            environment = {
                **os.environ,
                "PYTHONPATH": str(stage / "backend" / "src"),
                "QUALSIGHT_REPOSITORY_ROOT": str(repository),
                "QUALSIGHT_REGISTRY_PATH": str(
                    repository / "resources" / "resource-registry.json"
                ),
                "QUALSIGHT_COMMITTED_STAGE_ROOT": str(stage),
                "QUALSIGHT_COMMITTED_STAGE_COMMIT": commit,
            }
            command = [
                sys.executable,
                "-c",
                "from qualsight.api.settings import Settings; print(Settings.from_environment().repository_root)",
            ]
            accepted = subprocess.run(
                command,
                cwd=stage,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)

            changed_stage = temporary_root / "changed-stage"
            shutil.copytree(stage, changed_stage)
            shutil.copy2(
                repository / "README.md", changed_stage / "docs" / "INSTALL.md"
            )
            changed_environment = {
                **environment,
                "PYTHONPATH": str(changed_stage / "backend" / "src"),
                "QUALSIGHT_COMMITTED_STAGE_ROOT": str(changed_stage),
            }
            refused = subprocess.run(
                command,
                cwd=changed_stage,
                env=changed_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(refused.returncode, 0)

    def test_extracted_archive_self_validates_and_preserves_fail_closed_acquisition(self) -> None:
        module = _load_script("create_release_source_archive")
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            repository = _release_snapshot(temporary_root)
            archive_path = temporary_root / "qualsight-source.tar.gz"
            result = module.write_source_archive(
                root=repository,
                output=archive_path,
                commit="HEAD",
                prefix="qualsight-source-test",
            )
            second_archive = temporary_root / "qualsight-source-second.tar.gz"
            second_result = module.write_source_archive(
                root=repository,
                output=second_archive,
                commit="HEAD",
                prefix="qualsight-source-test",
            )
            self.assertEqual(archive_path.read_bytes(), second_archive.read_bytes())
            self.assertEqual(result, second_result)
            extracted = temporary_root / "extracted"
            extracted.mkdir()
            subprocess.run(
                ["tar", "-xzf", str(archive_path), "-C", str(extracted)],
                check=True,
            )
            package_root = extracted / "qualsight-source-test"

            hardened_extracted = temporary_root / "hardened-extracted"
            hardened_extracted.mkdir(mode=0o700)
            subprocess.run(
                [
                    "bash",
                    "-c",
                    'umask 077; exec tar --extract --gzip --file "$1" --directory "$2" --no-same-owner --no-same-permissions',
                    "qualsight-hardened-extraction",
                    str(archive_path),
                    str(hardened_extracted),
                ],
                check=True,
            )
            package_validator = _load_script("validate_release_package")
            hardened_result = package_validator.validate_release_package(
                hardened_extracted / "qualsight-source-test"
            )
            self.assertEqual(hardened_result["source_commit"], result["commit"])

            read_only_reduced = temporary_root / "read-only-reduced-extracted"
            read_only_reduced.mkdir(mode=0o700)
            subprocess.run(
                [
                    "bash",
                    "-c",
                    'umask 044; exec tar --extract --gzip --file "$1" --directory "$2" --no-same-owner --no-same-permissions',
                    "qualsight-read-only-reduced-extraction",
                    str(archive_path),
                    str(read_only_reduced),
                ],
                check=True,
            )
            read_only_result = package_validator.validate_release_package(
                read_only_reduced / "qualsight-source-test"
            )
            self.assertEqual(read_only_result["source_commit"], result["commit"])

            controlled_inputs = Path("scripts/controlled_inputs.py")
            for required in (
                controlled_inputs,
                Path("scripts/offline_gate.py"),
                Path("pnpm-workspace.yaml"),
            ):
                self.assertIn(required, package_validator.REQUIRED_FILES)
            with tempfile.TemporaryDirectory() as incomplete:
                with self.assertRaisesRegex(
                    package_validator.ReleasePackageValidationError,
                    "required package file is absent: scripts/controlled_inputs.py",
                ):
                    package_validator._exact_file(Path(incomplete), controlled_inputs)

            contents_manifest = package_root / "QUALSIGHT-PACKAGE-CONTENTS.json"
            self.assertTrue(contents_manifest.is_file())
            package_validator.validate_release_package(package_root)

            def rebind_package_member(relative: str) -> None:
                manifest = json.loads(contents_manifest.read_text(encoding="utf-8"))
                target = package_root / relative
                body = target.read_bytes()
                for entry in manifest["files"]:
                    if entry["path"] == relative:
                        entry["sha256"] = hashlib.sha256(body).hexdigest()
                        entry["size_bytes"] = len(body)
                        entry["unix_mode"] = f"{stat.S_IMODE(target.stat().st_mode):04o}"
                        break
                else:
                    self.fail(f"package member is absent from contents manifest: {relative}")
                contents_manifest.write_text(
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

            with self.subTest(case="source-commit"), self.assertRaisesRegex(
                package_validator.ReleasePackageValidationError,
                "source commit differs",
            ):
                prior_commit = subprocess.check_output(
                    ["git", "rev-parse", "HEAD^"],
                    cwd=ROOT,
                    text=True,
                ).strip()
                package_validator.validate_release_package(
                    package_root,
                    expected_commit=prior_commit,
                )
            for case in ("missing", "changed", "mode"):
                altered = temporary_root / f"altered-{case}"
                shutil.copytree(package_root, altered)
                target = altered / "README.md"
                if case == "missing":
                    target.unlink()
                elif case == "changed":
                    target.write_bytes(target.read_bytes() + b"\n")
                else:
                    target.chmod(0o666)
                with self.subTest(case=case), self.assertRaisesRegex(
                    package_validator.ReleasePackageValidationError,
                    "package contents manifest",
                ):
                    package_validator.validate_release_package(altered)

            unsafe_file = temporary_root / "altered-unsafe-file-mode"
            shutil.copytree(package_root, unsafe_file)
            unsafe_manifest_path = unsafe_file / "QUALSIGHT-PACKAGE-CONTENTS.json"
            unsafe_manifest = json.loads(unsafe_manifest_path.read_text(encoding="utf-8"))
            unsafe_target = unsafe_file / "README.md"
            unsafe_target.chmod(0o4777)
            for entry in unsafe_manifest["files"]:
                if entry["path"] == "README.md":
                    entry["unix_mode"] = "4777"
                    break
            unsafe_manifest_path.write_text(
                json.dumps(unsafe_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.subTest(case="self-consistent-unsafe-file-mode"), self.assertRaisesRegex(
                package_validator.ReleasePackageValidationError,
                "unsafe package file mode",
            ):
                package_validator.validate_release_package(unsafe_file)

            unsafe_directory = temporary_root / "altered-unsafe-directory-mode"
            shutil.copytree(package_root, unsafe_directory)
            (unsafe_directory / "backend").chmod(0o777)
            with self.subTest(case="unsafe-directory-mode"), self.assertRaisesRegex(
                package_validator.ReleasePackageValidationError,
                "unsafe package directory mode",
            ):
                package_validator.validate_release_package(unsafe_directory)

            swapped = temporary_root / "altered-path-swap"
            shutil.copytree(package_root, swapped)
            swap_target = swapped / "README.md"
            swap_copy = temporary_root / "readme-real-copy"
            swap_copy.write_bytes(swap_target.read_bytes())
            original_sha256 = package_validator._sha256
            swap_attempted = False

            def swap_during_path_hash(path: Path) -> str:
                nonlocal swap_attempted
                if path == swap_target and not swap_attempted:
                    swap_attempted = True
                    path.unlink()
                    path.symlink_to(swap_copy)
                return original_sha256(path)

            package_validator._sha256 = swap_during_path_hash
            try:
                package_validator.validate_release_package(swapped)
            finally:
                package_validator._sha256 = original_sha256
            with self.subTest(case="path-swap-not-followed"):
                self.assertFalse(swap_attempted)
                self.assertFalse(swap_target.is_symlink())

            self.assertEqual(result["projected_manifest_count"], 3)
            for source_path in package_root.rglob("*"):
                if not source_path.is_file() or source_path.relative_to(
                    package_root
                ).as_posix() == "resources/security/release-package-policy-v2.json":
                    continue
                source_body = source_path.read_bytes()
                self.assertNotIn(b"/mnt/", source_body, source_path)
                self.assertNotIn(b"/tmp/", source_body, source_path)
            self.assertFalse((package_root / "resources" / "authorizations").exists())
            self.assertFalse((package_root / "resources" / "upstream").exists())
            self.assertFalse((package_root / "atg").exists())
            self.assertFalse((package_root / "PLAN.md").exists())
            lfw_manifest_text = (
                package_root / "resources/manifests/lfw-funneled-images-v1.jsonl"
            ).read_text(encoding="utf-8")
            self.assertIn(
                '"path":"Claude_Jorda/Claude_Jorda_0001.jpg"',
                lfw_manifest_text,
            )

            canonical_checkpoints = json.loads(
                (ROOT / "resources/manifests/matcher-checkpoint-acquisition-v1.json").read_text(
                    encoding="utf-8"
                )
            )
            packaged_checkpoints = json.loads(
                (package_root / "resources/manifests/matcher-checkpoint-acquisition-v1.json").read_text(
                    encoding="utf-8"
                )
            )
            for canonical, packaged in zip(
                canonical_checkpoints["entries"], packaged_checkpoints["entries"], strict=True
            ):
                self.assertEqual(packaged["resource_id"], canonical["resource_id"])
                self.assertEqual(packaged["admission_state"], canonical["admission_state"])
                self.assertEqual(
                    packaged["third_party_rights_status"],
                    canonical["third_party_rights_status"],
                )
                self.assertEqual(packaged["source"], canonical["source"])
                self.assertEqual(packaged["artifact"], canonical["artifact"])

            canonical_dataset = json.loads(
                (ROOT / "resources/manifests/verification-dataset-acquisition-v1.json").read_text(
                    encoding="utf-8"
                )
            )
            packaged_dataset = json.loads(
                (package_root / "resources/manifests/verification-dataset-acquisition-v1.json").read_text(
                    encoding="utf-8"
                )
            )
            for field in (
                "published_url",
                "url",
                "host",
                "allowed_redirect_hosts",
                "filename",
                "sha256",
                "size_bytes",
                "managed_relative_path",
            ):
                self.assertEqual(
                    packaged_dataset["archive"][field], canonical_dataset["archive"][field]
                )
            self.assertEqual(packaged_dataset["protocols"], canonical_dataset["protocols"])

            canonical_upstream = json.loads(
                (ROOT / "resources/manifests/matcher-upstream-sources-v1.json").read_text(
                    encoding="utf-8"
                )
            )
            packaged_upstream = json.loads(
                (package_root / "resources/manifests/matcher-upstream-sources-v1.json").read_text(
                    encoding="utf-8"
                )
            )
            for canonical, packaged in zip(
                canonical_upstream["sources"], packaged_upstream["sources"], strict=True
            ):
                for field in ("key", "repository", "commit", "url", "sha256", "size_bytes"):
                    self.assertEqual(packaged[field], canonical[field])

            package_check = subprocess.run(
                [
                    "bash",
                    str(package_root / "scripts" / "install.sh"),
                    "--validate-package-only",
                ],
                cwd=package_root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(package_check.returncode, 0, package_check.stdout + package_check.stderr)
            self.assertIn("RELEASE_PACKAGE_VALID", package_check.stdout)
            self.assertFalse((package_root / "backend" / ".venv").exists())
            self.assertFalse((package_root / "node_modules").exists())

            data_dir = temporary_root / "private-data"
            data_dir.mkdir()
            store_check = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    textwrap.dedent(
                        """
                        import json
                        import sys
                        from pathlib import Path
                        from qualsight.dataset_acquisition import VerificationDatasetStore
                        from qualsight.execution.registry_binding import load_resource_registry
                        from qualsight.matchers.acquisition import CheckpointStore
                        from qualsight.matchers.upstream import UpstreamSourceError, UpstreamSources

                        root = Path(sys.argv[1])
                        data = Path(sys.argv[2])
                        registry = load_resource_registry(Path(sys.argv[3]))
                        checkpoints = CheckpointStore(
                            registry,
                            root / "resources/manifests/matcher-checkpoint-acquisition-v1.json",
                            data,
                        )
                        datasets = VerificationDatasetStore(
                            root / "resources/manifests/verification-dataset-acquisition-v1.json",
                            data,
                        )
                        upstream = UpstreamSources(
                            root / "resources/manifests/matcher-upstream-sources-v1.json",
                            data,
                            cache_root=data / "empty-upstream-cache",
                        )
                        source_state = ""
                        try:
                            upstream.verify("adaface_net")
                        except UpstreamSourceError:
                            source_state = "MISSING_FAIL_CLOSED"
                        print(json.dumps({
                            "checkpoint_entries": len(checkpoints.entries),
                            "checkpoint_pin": checkpoints.entry("adaface-ir101-local-candidate").artifact.sha256,
                            "dataset_state": datasets.status("magface-lfw-verification-v1")["runtime_state"],
                            "dataset_pin": datasets.archive["sha256"],
                            "source_state": source_state,
                            "source_pin": upstream.sources["adaface_net"].sha256,
                        }, sort_keys=True))
                        """
                    ),
                    str(package_root),
                    str(data_dir),
                    str(ROOT / "resources" / "resource-registry.json"),
                ],
                cwd=package_root,
                env={
                    **os.environ,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONPATH": f"{package_root / 'backend' / 'src'}:{package_root}",
                    "QUALSIGHT_REPOSITORY_ROOT": str(ROOT),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(store_check.returncode, 0, store_check.stdout + store_check.stderr)
            observed = json.loads(store_check.stdout)
            self.assertEqual(observed["checkpoint_entries"], 6)
            self.assertEqual(observed["dataset_state"], "MISSING")
            self.assertEqual(observed["source_state"], "MISSING_FAIL_CLOSED")
            self.assertEqual(len(observed["checkpoint_pin"]), 64)
            self.assertEqual(len(observed["dataset_pin"]), 64)
            self.assertEqual(len(observed["source_pin"]), 64)

            policy_relative_path = "resources/security/release-package-policy-v2.json"
            controlled_home = str(Path.home().resolve()).encode("utf-8")
            private_home_markers = (
                controlled_home,
                ", ".join(str(value) for value in controlled_home).encode("ascii"),
                base64.b64encode(controlled_home),
                controlled_home.hex().encode("ascii"),
            )
            for path in package_root.rglob("*"):
                if not path.is_file():
                    continue
                body = path.read_bytes().lower()
                if path.relative_to(package_root).as_posix() == policy_relative_path:
                    continue
                self.assertNotIn(b"/home/", body, str(path))
                for marker in private_home_markers:
                    self.assertNotIn(marker.lower(), body, str(path))
                for term in (
                    b"assistant/analysis",
                    b"assistant/commentary",
                    b"assistant/final",
                    b"anthropic",
                    b"claude code",
                    b"co-authored-by:",
                    b"generated by claude",
                    b"session_id",
                    b"thread_id",
                    b"turn_id",
                ):
                    self.assertNotIn(term, body, str(path))

            registry_path = package_root / "resources" / "resource-registry.json"
            packaged_registry_body = registry_path.read_bytes()
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["recorded_at"] = "/opt/qualsight/external/unapproved-locator"
            registry_path.write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            rebind_package_member("resources/resource-registry.json")
            exact_field_check = subprocess.run(
                [
                    sys.executable,
                    str(package_root / "scripts" / "validate_release_package.py"),
                    "--root",
                    str(package_root),
                ],
                cwd=package_root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(exact_field_check.returncode, 2)
            self.assertIn("exact field allowlist", exact_field_check.stdout)

            registry_path.write_bytes(packaged_registry_body)
            package_policy_path = package_root / policy_relative_path
            package_policy = json.loads(package_policy_path.read_text(encoding="utf-8"))
            package_policy["source_archive_excluded_paths"] = []
            package_policy_path.write_text(
                json.dumps(package_policy, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            rebind_package_member("resources/resource-registry.json")
            rebind_package_member(policy_relative_path)
            weakened_policy_check = subprocess.run(
                [
                    sys.executable,
                    str(package_root / "scripts" / "validate_release_package.py"),
                    "--root",
                    str(package_root),
                ],
                cwd=package_root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(weakened_policy_check.returncode, 2)
            self.assertIn("policy minimum", weakened_policy_check.stdout)

            committed_registry_path = repository / "resources" / "resource-registry.json"
            committed_registry = json.loads(
                committed_registry_path.read_text(encoding="utf-8")
            )
            committed_registry["recorded_at"] = str(Path.home() / "unapproved-locator")
            committed_registry_path.write_text(
                json.dumps(committed_registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "resources/resource-registry.json"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "Exercise locator refusal"],
                cwd=repository,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "QualSight release regression",
                    "GIT_AUTHOR_EMAIL": "release-regression@localhost",
                    "GIT_COMMITTER_NAME": "QualSight release regression",
                    "GIT_COMMITTER_EMAIL": "release-regression@localhost",
                },
                check=True,
            )
            with self.assertRaisesRegex(ValueError, "exact field allowlist"):
                module.write_source_archive(
                    root=repository,
                    output=temporary_root / "forbidden-locator.tar.gz",
                    commit="HEAD",
                    prefix="qualsight-source-test",
                )

            shutil.copy2(ROOT / "resources/resource-registry.json", committed_registry_path)
            summary_path = (
                repository
                / "resources"
                / "release-evidence"
                / "addon-acquisition-public-summary-v1.json"
            )
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            real_lock = (ROOT / "uv.lock").read_bytes()
            summary["original_evidence_identities"][0]["sha256"] = hashlib.sha256(
                real_lock
            ).hexdigest()
            summary["original_evidence_identities"][0]["size_bytes"] = len(real_lock)
            summary_path.write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "git",
                    "add",
                    "resources/resource-registry.json",
                    "resources/release-evidence/addon-acquisition-public-summary-v1.json",
                ],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "Exercise summary identity refusal"],
                cwd=repository,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "QualSight release regression",
                    "GIT_AUTHOR_EMAIL": "release-regression@localhost",
                    "GIT_COMMITTER_NAME": "QualSight release regression",
                    "GIT_COMMITTER_EMAIL": "release-regression@localhost",
                },
                check=True,
            )
            with self.assertRaisesRegex(ValueError, "summary evidence identities differ"):
                module.write_source_archive(
                    root=repository,
                    output=temporary_root / "forbidden-summary.tar.gz",
                    commit="HEAD",
                    prefix="qualsight-source-test",
                )

    def test_source_archive_rejects_private_attribution_and_nested_packages(self) -> None:
        module = _load_script("create_release_source_archive")
        real_private_record = ROOT / "docs" / "BASELINE_2026-09-01_IMAGE_WORKFLOW.md"
        self.assertTrue(real_private_record.is_file())
        real_archive_bytes = ROOT / "uv.lock"
        for relative, source, expected in (
            ("docs/private-observation.json", real_private_record, "forbidden release content term"),
            (
                "assets/nested-release.tar.gz",
                real_archive_bytes,
                "forbidden release content suffix",
            ),
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary) / "repository"
                repository.mkdir()
                subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
                target = repository / relative
                target.parent.mkdir(parents=True)
                shutil.copy2(source, target)
                subprocess.run(["git", "add", "."], cwd=repository, check=True)
                environment = {
                    **os.environ,
                    "GIT_AUTHOR_NAME": "QualSight release regression",
                    "GIT_AUTHOR_EMAIL": "release-regression@localhost",
                    "GIT_COMMITTER_NAME": "QualSight release regression",
                    "GIT_COMMITTER_EMAIL": "release-regression@localhost",
                }
                subprocess.run(
                    ["git", "commit", "--quiet", "-m", "Bind real forbidden bytes"],
                    cwd=repository,
                    env=environment,
                    check=True,
                )
                with self.assertRaisesRegex(ValueError, expected):
                    module.write_source_archive(
                        root=repository,
                        output=Path(temporary) / "forbidden.tar.gz",
                        commit="HEAD",
                        prefix="qualsight-source-test",
                        policy_path=ROOT / "resources/security/release-package-policy-v2.json",
                    )

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
            included = repository / "docs" / "restricted-biometric.txt"
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
                    policy_path=ROOT / "resources/security/release-package-policy-v2.json",
                )
            self.assertFalse(archive_path.exists())

    def test_source_archive_rejects_encoded_real_operator_home_in_included_text(self) -> None:
        module = _load_script("create_release_source_archive")
        controlled_home = str(Path.home().resolve()).encode("utf-8")
        encoded_markers = (
            base64.b64encode(controlled_home),
            controlled_home.hex().encode("ascii"),
            ", ".join(str(value) for value in controlled_home).encode("ascii"),
            quote_from_bytes(controlled_home, safe="").encode("ascii"),
            quote_from_bytes(controlled_home, safe="").lower().encode("ascii"),
            controlled_home.replace(b"/", b"\\u002f"),
            controlled_home.replace(b"/", b"\\u002F"),
        )
        for index, marker in enumerate(encoded_markers):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary) / "repository"
                repository.mkdir()
                subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
                target = repository / "docs" / f"encoded-location-{index}.txt"
                target.parent.mkdir()
                target.write_bytes(marker + b"\n")
                subprocess.run(["git", "add", "."], cwd=repository, check=True)
                environment = {
                    **os.environ,
                    "GIT_AUTHOR_NAME": "QualSight release regression",
                    "GIT_AUTHOR_EMAIL": "release-regression@localhost",
                    "GIT_COMMITTER_NAME": "QualSight release regression",
                    "GIT_COMMITTER_EMAIL": "release-regression@localhost",
                }
                subprocess.run(
                    ["git", "commit", "--quiet", "-m", "Bind encoded controlled location"],
                    cwd=repository,
                    env=environment,
                    check=True,
                )
                archive_path = Path(temporary) / "forbidden-source.tar.gz"
                with self.assertRaisesRegex(ValueError, "encoded operator home"):
                    module.write_source_archive(
                        root=repository,
                        output=archive_path,
                        commit="HEAD",
                        prefix="qualsight-source-test",
                        policy_path=ROOT / "resources/security/release-package-policy-v2.json",
                    )
                self.assertFalse(archive_path.exists())

    def test_source_boundary_rejects_base64_of_full_real_private_paths(self) -> None:
        module = _load_script("create_release_source_archive")
        policy = json.loads(
            (ROOT / "resources/security/release-package-policy-v2.json").read_text(
                encoding="utf-8"
            )
        )
        home_path = Path.home().resolve() / ".profile"
        mounted_path = ROOT.resolve() / "README.md"
        self.assertTrue(home_path.is_file())
        self.assertTrue(mounted_path.is_file())
        with tempfile.NamedTemporaryFile() as temporary_file:
            temporary_path = Path(temporary_file.name).resolve()
            self.assertTrue(temporary_path.is_file())
            for private_path in (home_path, mounted_path, temporary_path):
                encoded = base64.b64encode(str(private_path).encode("utf-8"))
                for marker in (encoded, encoded.rstrip(b"=")):
                    with self.subTest(
                        private_path=private_path, marker=marker
                    ), self.assertRaisesRegex(
                        ValueError,
                        "encoded (operator home|mounted filesystem root|temporary root)",
                    ):
                        module._validate_regular_body(
                            relative=PurePosixPath("docs/private-location.txt"),
                            body=marker,
                        policy=policy,
                    )

    def test_source_boundary_rejects_real_file_uri_and_host_derived_windows_path(
        self,
    ) -> None:
        module = _load_script("create_release_source_archive")
        policy = json.loads(
            (ROOT / "resources/security/release-package-policy-v2.json").read_text(
                encoding="utf-8"
            )
        )
        real_executable = Path("/usr/bin/python3.11").resolve(strict=True)
        windows_evidence = Path(
            "/usr/share/code/resources/app/node_modules/vscode-regexpp/index.mjs.map"
        ).resolve(strict=True)
        windows_map = json.loads(windows_evidence.read_text(encoding="utf-8"))

        def strings(value: object):
            if isinstance(value, str):
                yield value
            elif isinstance(value, list):
                for child in value:
                    yield from strings(child)
            elif isinstance(value, dict):
                for child in value.values():
                    yield from strings(child)

        windows_locator = next(
            value
            for value in strings(windows_map)
            if value.startswith("C:\\Users\\")
        )
        with self.assertRaisesRegex(ValueError, "Windows user path"):
            module._validate_regular_body(
                relative=PurePosixPath("docs/installed-source-map.txt"),
                body=windows_evidence.read_bytes(),
                policy=policy,
            )
        package_validator = _load_script("validate_release_package")
        self.assertIsNotNone(
            package_validator.PRIVATE_BUILD_PATH_PATTERN.search(
                windows_evidence.read_bytes()
            )
        )
        private_locators = (
            (real_executable.as_uri().encode("utf-8"), "file URI"),
            (
                real_executable.as_uri().replace("/", "%2F").encode("utf-8"),
                "file URI",
            ),
            (windows_locator.encode("utf-8"), "Windows user path"),
        )
        for locator, label in private_locators:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, label):
                module._validate_regular_body(
                    relative=PurePosixPath("docs/private-location.txt"),
                    body=locator,
                    policy=policy,
                )

    def test_source_archive_rejects_registered_model_bytes_renamed_as_text(self) -> None:
        module = _load_script("create_release_source_archive")
        source = Path.home() / ".insightface/models/buffalo_l/det_10g.onnx"
        source_body = source.read_bytes()
        self.assertEqual(len(source_body), 16_923_827)
        self.assertEqual(
            hashlib.sha256(source_body).hexdigest(),
            "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
        )
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
            target = repository / "docs" / "registered-runtime.txt"
            target.parent.mkdir()
            shutil.copy2(source, target)
            subprocess.run(["git", "add", "."], cwd=repository, check=True)
            environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "QualSight release regression",
                "GIT_AUTHOR_EMAIL": "release-regression@localhost",
                "GIT_COMMITTER_NAME": "QualSight release regression",
                "GIT_COMMITTER_EMAIL": "release-regression@localhost",
            }
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "Bind registered runtime bytes"],
                cwd=repository,
                env=environment,
                check=True,
            )
            archive_path = Path(temporary) / "forbidden-source.tar.gz"
            with self.assertRaisesRegex(
                ValueError,
                "registered runtime/model bytes",
            ):
                module.write_source_archive(
                    root=repository,
                    output=archive_path,
                    commit="HEAD",
                    prefix="qualsight-source-test",
                    policy_path=ROOT / "resources/security/release-package-policy-v2.json",
                )
            self.assertFalse(archive_path.exists())

    def test_release_policy_binds_every_registered_runtime_model_and_config_identity(self) -> None:
        policy = json.loads(
            (ROOT / "resources/security/release-package-policy-v2.json").read_text(
                encoding="utf-8"
            )
        )
        registry = json.loads(
            (ROOT / "resources/resource-registry.json").read_text(encoding="utf-8")
        )
        registered = {
            artifact["sha256"]
            for resource in registry["resources"]
            for artifact in resource.get("artifacts", [])
            if artifact.get("role") in {"MODEL", "EXECUTABLE", "CONFIG"}
        }
        self.assertEqual(
            set(policy["source_archive_forbidden_artifact_sha256s"]),
            registered,
        )

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
                    policy_path=ROOT / "resources/security/release-package-policy-v2.json",
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

    def test_published_release_boundary_separates_beta_from_dhs_transition(self) -> None:
        requirements = (ROOT / "REQUIREMENTS.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        acceptance = (ROOT / "docs" / "BETA_ACCEPTANCE.md").read_text(
            encoding="utf-8"
        )
        threat_model = (ROOT / "docs" / "THREAT_MODEL.md").read_text(
            encoding="utf-8"
        )
        install = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")

        self.assertIn("## Controlled beta release boundary", requirements)
        self.assertIn("## DHS transition-only controls", requirements)
        self.assertIn("no local beta result grants DHS transition", readme)
        self.assertIn("outside QS-H74 beta acceptance", acceptance)
        self.assertNotIn("post-beta cycle C25 added", readme)
        self.assertIn("loopback HTTP server", threat_model)
        self.assertIn(
            "Python socket guard does not provide OS-level egress control",
            threat_model,
        )
        self.assertIn(".venv/bin/python -m unittest discover -s tests -q", install)
        self.assertNotIn("python3 -m unittest discover -s tests -q", install)
        self.assertIn("Remote API connectors remain unadmitted", requirements)
        self.assertIn("verified local offline backup and no-clobber restore", requirements)
        self.assertIn("operational continuity with defined RTO/RPO", requirements)
        self.assertNotIn("managed encryption, backup/recovery, incident response", requirements)

    def test_ofiq_build_pins_and_validates_the_governed_conan_closure(self) -> None:
        build_script = (ROOT / "scripts" / "ofiq_v112_build.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "IMAGES_SHA=11aaebeb180810e0b2b42a5dafb356938c92ddf01592411d1d8f219988448399",
            build_script,
        )
        self.assertIn("IMAGES_SIZE=374153032", build_script)
        self.assertNotIn("QUALSIGHT_OFIQ_IMAGES_SHA", build_script)
        self.assertIn("resources/locks/ofiq-v1.1.2-conan.lock", build_script)
        self.assertIn("--lockfile \"$CONAN_LOCK\"", build_script)
        self.assertIn("--no-remote --build=never", build_script)
        self.assertIn("--build=missing", build_script)
        self.assertIn("curl --proto '=https' --tlsv1.2", build_script)
        self.assertIn("JOBS=4", build_script)
        self.assertIn('NATIVE_BUILD="$SOURCE/build/build_linux"', build_script)
        self.assertIn('ctest --test-dir "$NATIVE_BUILD" --parallel "$JOBS"', build_script)
        self.assertIn("100% tests passed, 0 tests failed out of 787", build_script)
        self.assertIn('"$ARCHIVES/OFIQ-Models.zip"', build_script)
        self.assertIn('"$LOGS/ldd.log"', build_script)
        self.assertIn('"dynamic_linkage": identity(ldd_log)', build_script)
        self.assertNotIn("uv tool install", build_script)

        manifest_path = (
            ROOT / "resources" / "manifests" / "ofiq-v1.1.2-conan-closure-v1.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["source"]["commit"], "d589ca27896a472446abc9eca8b62947ee47db19")
        self.assertEqual(manifest["toolchain"]["conan_version"], "2.18.1")
        self.assertEqual(len(manifest["dependencies"]), 15)
        self.assertTrue(all("#" in item["ref"] for item in manifest["dependencies"]))

        validation = subprocess.run(
            [str(ROOT / "scripts" / "ofiq_v112_build.sh"), "--validate-lock"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)
        self.assertIn("OFIQ_CONAN_CLOSURE_VALID dependencies=15", validation.stdout)

    def test_release_executes_and_packages_the_complete_staged_beta_gate(self) -> None:
        gate_path = ROOT / "scripts" / "run_release_beta_gate.py"
        self.assertTrue(gate_path.is_file())
        gate = _load_script("run_release_beta_gate")
        venv_entrypoint = Path(sys.executable)
        self.assertTrue(venv_entrypoint.is_symlink())
        self.assertNotEqual(
            venv_entrypoint.absolute(),
            venv_entrypoint.resolve(strict=True),
        )
        self.assertEqual(
            gate._release_interpreter(str(venv_entrypoint)),
            venv_entrypoint.absolute(),
        )
        definitions = gate.gate_definitions(ROOT, sys.executable)
        self.assertEqual(
            [definition.name for definition in definitions],
            [
                "root-tests",
                "backend-tests",
                "frontend-toolchain",
                "frontend-build",
                "dependency-audit",
                "resource-registry",
                "metric-registry",
                "lfw-protocol",
                "lfw-protocol-authority",
                "scrfd-release-provenance",
                "shared-crop-lineage",
                "atg-governance",
                "documentation-workflow",
                "offline-workflow",
                "browser-beta3-workflows",
            ],
        )
        definitions_by_name = {
            definition.name: definition for definition in definitions
        }
        split_root_definitions = gate.gate_definitions(
            ROOT / "backend",
            sys.executable,
            deployment_root=ROOT,
        )
        split_root_gate = next(
            definition
            for definition in split_root_definitions
            if definition.name == "root-tests"
        )
        self.assertEqual(split_root_gate.working_directory, ROOT.resolve())
        split_definitions_by_name = {
            definition.name: definition for definition in split_root_definitions
        }
        backend_argv = definitions_by_name["backend-tests"].argv
        self.assertEqual(
            backend_argv[backend_argv.index("-t") + 1],
            str((ROOT / "backend").resolve()),
        )
        for validator_name in (
            "resource-registry",
            "metric-registry",
            "lfw-protocol",
            "lfw-protocol-authority",
            "scrfd-release-provenance",
            "shared-crop-lineage",
            "atg-governance",
        ):
            self.assertEqual(
                definitions_by_name[validator_name].argv[1],
                "-m",
                validator_name,
            )
            self.assertEqual(
                dict(split_definitions_by_name[validator_name].environment)[
                    "QUALSIGHT_REPOSITORY_ROOT"
                ],
                str(ROOT.resolve()),
            )
        split_lfw_argv = split_definitions_by_name["lfw-protocol"].argv
        self.assertEqual(
            split_lfw_argv[split_lfw_argv.index("--registry") + 1],
            str((ROOT / "resources" / "resource-registry.json").resolve()),
        )
        self.assertEqual(
            split_lfw_argv[split_lfw_argv.index("--manifest") + 1],
            str(
                (
                    ROOT
                    / "resources"
                    / "manifests"
                    / "lfw-local-pairs-v1.jsonl"
                ).resolve()
            ),
        )
        offline_argv = definitions_by_name["offline-workflow"].argv
        self.assertIn("--source-root", offline_argv)
        self.assertIn("--repository-root", offline_argv)
        self.assertIn("--python", offline_argv)
        documentation = definitions_by_name["documentation-workflow"]
        self.assertIn("docs_workflow_gate.py", documentation.argv[1])
        self.assertIn("--observation", documentation.argv)
        self.assertEqual(
            documentation.evidence_path,
            ROOT / ".release-beta-gate-work" / "documentation-workflow.json",
        )
        offline_module = _load_script("offline_gate")
        self.assertEqual(
            offline_module.BETA_REQUIRED_LEAVES,
            (
                "QS-C22.AS",
                "QS-C22.AN",
                "QS-C22.ES",
                "QS-C22.EN",
                "QS-C22.TS",
                "QS-C22.TN",
            ),
        )
        self.assertEqual(
            offline_module.BETA_REQUIRED_MATCHERS,
            (
                "adaface-ir101-local-candidate",
                "elasticface-arc-r100-local-candidate",
                "transface-s-glint360k-local-candidate",
            ),
        )
        release_script = (ROOT / "scripts" / "release.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("run_release_beta_gate.py", release_script)
        self.assertIn("-BETA-GATE.json", release_script)
        self.assertIn('"$BETA_GATE"', release_script)
        self.assertIn('--deployment-root "$ROOT"', release_script)

        browser = definitions_by_name["browser-beta3-workflows"]
        self.assertEqual(browser.evidence_path, ROOT / ".release-beta-gate-work" / "browser-aggregate.json")
        self.assertIn("validate_release_browser.py", browser.argv[1])
        browser_module = _load_script("validate_release_browser")
        self.assertEqual(
            browser_module.BROWSER_VALIDATORS,
            tuple(name.removesuffix(".py") for name in BETA3_BROWSER_VALIDATORS),
        )

    def test_failed_release_preserves_private_gate_diagnostic_outside_stage(
        self,
    ) -> None:
        release_script = (ROOT / "scripts" / "release.sh").read_text(
            encoding="utf-8"
        )
        gate_call = release_script.index(
            'if ! "$RELEASE_PYTHON" "$BUILD_ROOT/scripts/run_release_beta_gate.py"'
        )
        failure_root = release_script.index(
            'FAILURE_ROOT="${OUT}/.release-failures"'
        )
        retained_record = release_script.index(
            'install -m 600 "$BETA_GATE" "$FAILURE_ATTEMPT/BETA-GATE.json"'
        )
        source_archive = release_script.index(
            '"$BUILD_ROOT/scripts/create_release_source_archive.py"'
        )
        self.assertLess(gate_call, failure_root)
        self.assertLess(failure_root, retained_record)
        self.assertLess(retained_record, source_archive)
        self.assertIn('mkdir -m 700 -p "$FAILURE_ROOT"', release_script)
        self.assertIn('chmod 700 "$FAILURE_ROOT"', release_script)
        self.assertIn(
            'mktemp -d "${FAILURE_ROOT}/${NAME}.${COMMIT}.XXXXXX"',
            release_script,
        )
        self.assertIn('chmod 700 "$FAILURE_ATTEMPT"', release_script)
        self.assertIn(
            'release failed; private gate diagnostic retained at '
            '${FAILURE_ATTEMPT}/BETA-GATE.json',
            release_script,
        )
        gate_script = (ROOT / "scripts" / "run_release_beta_gate.py").read_text(
            encoding="utf-8"
        )
        gate_module = _load_script("run_release_beta_gate")
        self.assertIs(gate_module.sys, sys)
        self.assertIn("passed={passed} failed={int(failed is not None)} not_run={not_run}", gate_script)
        self.assertIn('f"name={failed[\'name\']} exit={failed[\'exit_code\']} "', gate_script)
        self.assertNotIn('result["stdout_tail"]', gate_script)
        self.assertNotIn('result["stderr_tail"]', gate_script)
        self.assertIn('result["failure_evidence"]', gate_script)
        self.assertIn('"redacted_stdout_tail"', gate_script)
        self.assertIn('"redacted_stderr_tail"', gate_script)
        self.assertIn("MAX_FAILURE_TAIL_CHARACTERS = 4_000", gate_script)

    def test_release_gate_refuses_pass_if_deployment_changes_during_a_gate(
        self,
    ) -> None:
        gate = _load_script("run_release_beta_gate")
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            deployment = temporary_root / "deployment"
            deployment.mkdir()
            tracked = deployment / "README.md"
            shutil.copy2(ROOT / "README.md", tracked)
            subprocess.run(["git", "init", "--quiet"], cwd=deployment, check=True)
            subprocess.run(["git", "add", "README.md"], cwd=deployment, check=True)
            git_environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "QualSight release regression",
                "GIT_AUTHOR_EMAIL": "release-regression@localhost",
                "GIT_COMMITTER_NAME": "QualSight release regression",
                "GIT_COMMITTER_EMAIL": "release-regression@localhost",
            }
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "Bind deployment bytes"],
                cwd=deployment,
                env=git_environment,
                check=True,
            )
            commit, tree = gate._git_identity(deployment)
            self.assertEqual(
                subprocess.run(
                    ["git", "status", "--porcelain", "--untracked-files=all"],
                    cwd=deployment,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "",
            )
            real_record = json.loads(
                (
                    ROOT
                    / "dist/qualsight-0.1.0b2/qualsight-0.1.0b2-BETA-GATE.json"
                ).read_text(encoding="utf-8")
            )
            unchanged_output = temporary_root / "unchanged-beta-gate.json"
            gate._write_gate_record(
                output=unchanged_output,
                record=real_record,
                deployment_root=deployment,
                commit=commit,
                tree=tree,
            )
            self.assertEqual(
                json.loads(unchanged_output.read_text(encoding="utf-8")),
                real_record,
            )

            started = temporary_root / "gate-started"
            delayed_gate = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    'set -euo pipefail; : > "$1"; sleep 0.5; git fsck --no-dangling',
                    "delayed-git-gate",
                    str(started),
                ],
                cwd=deployment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5.0
            while not started.exists() and delayed_gate.poll() is None:
                if time.monotonic() >= deadline:
                    delayed_gate.kill()
                    delayed_gate.communicate()
                    self.fail("delayed Git gate did not start")
                time.sleep(0.01)
            tracked.write_bytes((ROOT / "CHANGELOG.md").read_bytes())
            stdout, stderr = delayed_gate.communicate(timeout=5)
            self.assertEqual(
                delayed_gate.returncode,
                0,
                (stdout + stderr).decode("utf-8", errors="replace"),
            )
            self.assertEqual(gate._git_identity(deployment), (commit, tree))
            self.assertIn(
                "README.md",
                subprocess.run(
                    ["git", "status", "--porcelain", "--untracked-files=all"],
                    cwd=deployment,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
            )

            output = temporary_root / "terminal-beta-gate.json"
            with self.assertRaisesRegex(
                ValueError,
                "controlled deployment worktree changed while beta gates were running",
            ):
                gate._write_gate_record(
                    output=output,
                    record=real_record,
                    deployment_root=deployment,
                    commit=commit,
                    tree=tree,
                )
            self.assertFalse(output.exists())

            subprocess.run(["git", "add", "README.md"], cwd=deployment, check=True)
            subprocess.run(
                [
                    "git",
                    "commit",
                    "--quiet",
                    "-m",
                    "Record concurrent deployment change",
                ],
                cwd=deployment,
                env=git_environment,
                check=True,
            )
            self.assertNotEqual(gate._git_identity(deployment), (commit, tree))
            self.assertEqual(
                subprocess.run(
                    ["git", "status", "--porcelain", "--untracked-files=all"],
                    cwd=deployment,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "",
            )
            with self.assertRaisesRegex(
                ValueError,
                "controlled deployment Git identity changed while beta gates were running",
            ):
                gate._write_gate_record(
                    output=output,
                    record=real_record,
                    deployment_root=deployment,
                    commit=commit,
                    tree=tree,
                )
            self.assertFalse(output.exists())

    def test_staged_evidence_validators_use_the_controlled_repository_root(self) -> None:
        variable = "QUALSIGHT_REPOSITORY_ROOT"
        previous = os.environ.get(variable)
        validator_names = (
            "validate_lfw_protocol_authority",
            "validate_scrfd_release_provenance",
            "validate_shared_crop_lineage",
        )
        with tempfile.TemporaryDirectory() as temporary:
            staged_scripts = Path(temporary) / "committed-stage" / "scripts"
            staged_scripts.mkdir(parents=True)
            os.environ[variable] = str(ROOT.resolve())
            try:
                for name in validator_names:
                    staged_path = staged_scripts / f"{name}.py"
                    shutil.copy2(ROOT / "scripts" / staged_path.name, staged_path)
                    spec = importlib.util.spec_from_file_location(
                        f"staged_{name}",
                        staged_path,
                    )
                    self.assertIsNotNone(spec)
                    self.assertIsNotNone(spec.loader)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    self.assertEqual(module.PROJECT_ROOT, ROOT.resolve())
            finally:
                if previous is None:
                    del os.environ[variable]
                else:
                    os.environ[variable] = previous

    def test_browser_release_chain_preserves_the_controlled_repository_root(self) -> None:
        browser_scripts = (
            "validate_release_browser.py",
            "validate_comparison_studio.py",
            "validate_evidence_warning_privacy.py",
            "validate_manage_system.py",
            "validate_owner_purge_ui.py",
            "validate_stress_studio.py",
            "validate_structured_evidence_navigation.py",
            "validate_upload_working_sample.py",
            "validate_utility_overlay.py",
            "observe_console.py",
        )
        for name in browser_scripts:
            with self.subTest(name=name):
                source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertNotIn(
                    '"QUALSIGHT_REPOSITORY_ROOT": str(ROOT)',
                    source,
                )
                self.assertNotIn(
                    '"QUALSIGHT_REPOSITORY_ROOT": str(root)',
                    source,
                )
                self.assertNotIn(
                    'env["QUALSIGHT_REPOSITORY_ROOT"] = str(ROOT)',
                    source,
                )
                self.assertRegex(
                    source,
                    r'os\.environ\.get\(\s*"QUALSIGHT_REPOSITORY_ROOT",\s*str\(',
                )

    def test_staged_privacy_browser_validator_uses_the_controlled_git_root(
        self,
    ) -> None:
        variable = "QUALSIGHT_REPOSITORY_ROOT"
        previous = os.environ.get(variable)
        os.environ[variable] = str(ROOT.resolve())
        try:
            module = _load_script("validate_evidence_warning_privacy")
            self.assertEqual(
                module._controlled_repository_root(),
                ROOT.resolve(),
            )
            source = inspect.getsource(module._extract_legacy_producer)
            self.assertNotIn("cwd=ROOT,", source)
            self.assertIn("cwd=_controlled_repository_root(),", source)
        finally:
            if previous is None:
                del os.environ[variable]
            else:
                os.environ[variable] = previous

    def test_installed_privacy_browser_uses_the_controlled_fixture_manifest(
        self,
    ) -> None:
        variable = "QUALSIGHT_REPOSITORY_ROOT"
        registry_variable = "QUALSIGHT_REGISTRY_PATH"
        previous = os.environ.get(variable)
        previous_registry = os.environ.get(registry_variable)
        with tempfile.TemporaryDirectory() as temporary:
            installed_root = Path(temporary) / "installed-package"
            installed_scripts = installed_root / "scripts"
            installed_manifest = (
                installed_root / "resources" / "manifests" / "lfw-subset-fixture-v1.json"
            )
            installed_scripts.mkdir(parents=True)
            installed_manifest.parent.mkdir(parents=True)
            shutil.copy2(
                ROOT / "scripts" / "validate_evidence_warning_privacy.py",
                installed_scripts / "validate_evidence_warning_privacy.py",
            )
            projector = _load_script("create_release_source_archive")
            manifest_relative = PurePosixPath(
                "resources/manifests/lfw-subset-fixture-v1.json"
            )
            projected, _ = projector._project_release_members(
                {
                    manifest_relative: (
                        ROOT / manifest_relative
                    ).read_bytes()
                },
                projector._load_policy(
                    ROOT / "resources" / "security" / "release-package-policy-v2.json"
                ),
            )
            installed_manifest.write_bytes(projected[manifest_relative])
            self.assertNotEqual(
                hashlib.sha256(installed_manifest.read_bytes()).hexdigest(),
                "6d35c169199d83c12e0c76c90a0b1e67c432d0cf78df3aea66df293f65b8b6fe",
            )
            os.environ[variable] = str(ROOT.resolve())
            os.environ[registry_variable] = str(
                (ROOT / "resources" / "resource-registry.json").resolve()
            )
            operator_root = Path(temporary) / "operator-inputs"
            script_path = installed_scripts / "validate_evidence_warning_privacy.py"
            spec = importlib.util.spec_from_file_location("installed_privacy_validator", script_path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            sys.path.insert(0, str(ROOT / "scripts"))
            try:
                spec.loader.exec_module(module)
                copied = module._verified_real_inputs(operator_root)
                self.assertEqual(set(copied), set(module.REAL_INPUTS))
                for relative, path in copied.items():
                    self.assertEqual(
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                        module.REAL_INPUTS[relative],
                    )
            finally:
                sys.path.remove(str(ROOT / "scripts"))
                if previous is None:
                    del os.environ[variable]
                else:
                    os.environ[variable] = previous
                if previous_registry is None:
                    os.environ.pop(registry_variable, None)
                else:
                    os.environ[registry_variable] = previous_registry

    def test_installed_browser_scientific_authorities_use_the_controlled_root(
        self,
    ) -> None:
        authorities = (
            (
                "validate_owner_purge_ui",
                "INVALID_MEDIA",
                PurePosixPath(
                    "resources/manifests/lfw-subset-fixture-v1.json"
                ),
            ),
            (
                "validate_stress_studio",
                "IMAGE_MANIFEST",
                PurePosixPath(
                    "resources/manifests/lfw-funneled-images-v1.jsonl"
                ),
            ),
            (
                "validate_utility_overlay",
                "METRIC_REGISTRY",
                PurePosixPath("metrics/metric-registry.json"),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            repository = _release_snapshot(temporary_root)
            archive_path = temporary_root / "qualsight-source.tar.gz"
            projector = _load_script("create_release_source_archive")
            projector.write_source_archive(
                root=repository,
                output=archive_path,
                commit="HEAD",
                prefix="qualsight-installed-authority-test",
            )
            extracted = temporary_root / "extracted"
            extracted.mkdir()
            subprocess.run(
                ["tar", "-xzf", str(archive_path), "-C", str(extracted)],
                check=True,
            )
            installed_root = extracted / "qualsight-installed-authority-test"
            installed_scripts = installed_root / "scripts"
            environment = {
                **os.environ,
                "PYTHONPATH": str(installed_scripts),
                "QUALSIGHT_REPOSITORY_ROOT": str(ROOT.resolve()),
                "QUALSIGHT_REGISTRY_PATH": str(
                    (ROOT / "resources" / "resource-registry.json").resolve()
                ),
            }
            expression = textwrap.dedent(
                f"""
                import importlib
                import json
                from pathlib import Path
                from controlled_inputs import lfw_tree_root

                authorities = {[(name, attribute) for name, attribute, _ in authorities]!r}
                observed = {{
                    "lfw_root": str(lfw_tree_root(Path.cwd())),
                    "authorities": {{
                        name: str(getattr(importlib.import_module(name), attribute))
                        for name, attribute in authorities
                    }},
                }}
                print(json.dumps(observed, sort_keys=True))
                """
            )
            imported = subprocess.run(
                [sys.executable, "-c", expression],
                cwd=installed_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)
            observed = json.loads(imported.stdout)
            self.assertEqual(
                Path(observed["lfw_root"]),
                _load_script("controlled_inputs").lfw_tree_root(ROOT),
            )
            self.assertEqual(
                observed["authorities"],
                {
                    script_name: str((ROOT / relative).resolve(strict=True))
                    for script_name, _, relative in authorities
                },
            )

    def test_structured_evidence_browser_uses_the_controlled_database_root(
        self,
    ) -> None:
        variable = "QUALSIGHT_REPOSITORY_ROOT"
        previous = os.environ.get(variable)
        with tempfile.TemporaryDirectory() as temporary:
            controlled_root = Path(temporary).resolve()
            os.environ[variable] = str(controlled_root)
            try:
                module = _load_script("validate_structured_evidence_navigation")
                self.assertEqual(
                    module.SOURCE_DATABASE,
                    controlled_root / "var" / "qualsight.sqlite3",
                )
            finally:
                if previous is None:
                    del os.environ[variable]
                else:
                    os.environ[variable] = previous

    def test_staged_documentation_gate_preserves_controlled_repository_root(
        self,
    ) -> None:
        variable = "QUALSIGHT_REPOSITORY_ROOT"
        previous = os.environ.get(variable)
        with tempfile.TemporaryDirectory() as temporary:
            staged_scripts = Path(temporary) / "committed-stage" / "scripts"
            staged_scripts.mkdir(parents=True)
            staged_path = staged_scripts / "docs_workflow_gate.py"
            shutil.copy2(ROOT / "scripts" / staged_path.name, staged_path)
            os.environ[variable] = str(ROOT.resolve())
            try:
                spec = importlib.util.spec_from_file_location(
                    "staged_docs_workflow_gate",
                    staged_path,
                )
                self.assertIsNotNone(spec)
                self.assertIsNotNone(spec.loader)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertNotEqual(module.ROOT, ROOT.resolve())
                self.assertEqual(
                    module._controlled_repository_root(),
                    ROOT.resolve(),
                )
                main_source = inspect.getsource(module.main)
                self.assertNotIn(
                    '(ROOT / "resources" / "resource-registry.json")',
                    main_source,
                )
                self.assertIn(
                    '(repository_root / "resources" / "resource-registry.json")',
                    main_source,
                )
            finally:
                if previous is None:
                    del os.environ[variable]
                else:
                    os.environ[variable] = previous

    def test_stress_browser_gate_uses_only_project_scoped_public_contracts(self) -> None:
        validator = (ROOT / "scripts" / "validate_stress_studio.py").read_text(
            encoding="utf-8"
        )
        module = _load_script("validate_stress_studio")
        self.assertNotIn('derived["directory"]', validator)
        self.assertNotIn("/api/detection?sample_id=", validator)
        self.assertNotIn("/api/ofiq-landmarks?sample_id=", validator)
        self.assertEqual(
            module._sample_api_url("project:one", "sample:two", "detection"),
            "http://127.0.0.1:8480/api/projects/project%3Aone/samples/"
            "sample%3Atwo/detection",
        )

    def test_mutating_export_requires_post_origin_protection_and_project_purge_fence(self) -> None:
        source = (ROOT / "backend" / "src" / "qualsight" / "api" / "app.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        export = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "export_category"
        )
        methods = {
            decorator.func.attr.upper()
            for decorator in export.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "app"
        }
        self.assertEqual(methods, {"POST"})
        fenced = any(
            isinstance(context, ast.Call)
            and isinstance(context.func, ast.Name)
            and context.func.id == "admitted_mutation"
            for node in ast.walk(export)
            if isinstance(node, (ast.With, ast.AsyncWith))
            for item in node.items
            for context in (item.context_expr,)
        )
        self.assertTrue(fenced)

        api_source = (ROOT / "frontend" / "src" / "api.ts").read_text(
            encoding="utf-8"
        )
        reports_source = (ROOT / "frontend" / "src" / "Reports.tsx").read_text(
            encoding="utf-8"
        )
        system_source = (ROOT / "frontend" / "src" / "SystemPanel.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn("exportTable:", api_source)
        self.assertIn(
            'headers: { accept: "application/json" }',
            api_source[api_source.index("exportTable:"):],
        )
        self.assertIn("await api.exportTable", reports_source)
        self.assertNotIn("/exports/", reports_source)
        self.assertIn("download.href", reports_source)
        self.assertIn("link.click()", reports_source)
        self.assertIn("window.setTimeout(() => link.remove(), 0)", reports_source)
        self.assertNotIn("URL.createObjectURL", reports_source)
        self.assertNotIn("URL.revokeObjectURL", reports_source)
        self.assertIn("export created", reports_source)
        app_source = (ROOT / "frontend" / "src" / "App.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            r"(recorded|imported|frozen|saved|completed|created|cancelled)",
            app_source,
        )
        self.assertNotIn("export_parquet", system_source)
        self.assertNotIn("export_json", system_source)
        audit_source = (ROOT / "scripts" / "audit_console.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('page.expect_download(timeout=60_000)', audit_source)
        self.assertIn('[data-governed-download="quality-csv"]', audit_source)
        self.assertIn("export_sha256", audit_source)
        release_browser_source = (ROOT / "scripts" / "observe_console.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('page.expect_download(timeout=60_000)', release_browser_source)
        self.assertIn('[data-governed-download="quality-csv"]', release_browser_source)
        self.assertIn("export_sha256", release_browser_source)

    def test_offline_gate_requires_its_launched_process_to_own_the_listener(self) -> None:
        module = _load_script("offline_gate")
        host = "127.0.0.1"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((host, 0))
            listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(RuntimeError, "already has a listener"):
                module._assert_endpoint_available(host, port)

            unrelated = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                start_new_session=True,
            )
            try:
                with self.assertRaisesRegex(TimeoutError, "not owned"):
                    module._wait_for_owned_listener(
                        unrelated,
                        host,
                        port,
                        timeout=0.2,
                    )
            finally:
                os.killpg(os.getpgid(unrelated.pid), signal.SIGTERM)
                unrelated.wait(timeout=5)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind((host, 0))
            port = reservation.getsockname()[1]
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "http.server",
                str(port),
                "--bind",
                host,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            module._wait_for_owned_listener(server, host, port, timeout=5)
        finally:
            os.killpg(os.getpgid(server.pid), signal.SIGTERM)
            server.wait(timeout=5)

    def test_offline_gate_does_not_mislabel_a_closed_time_wait_connection_as_listener(
        self,
    ) -> None:
        module = _load_script("offline_gate")
        host = "127.0.0.1"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, 0))
            port = listener.getsockname()[1]
            listener.listen()
            with socket.create_connection((host, port)) as client:
                accepted, _ = listener.accept()
                with accepted:
                    accepted.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b"")
        module._assert_endpoint_available(host, port)

    def test_offline_gate_cannot_backpressure_the_server_on_an_undrained_pipe(self) -> None:
        source = (ROOT / "scripts" / "offline_gate.py").read_text(encoding="utf-8")

        self.assertNotIn("stdout=subprocess.PIPE", source)
        self.assertIn("tempfile.TemporaryFile", source)

    def test_offline_gate_rejects_any_authorized_setup_egress(self) -> None:
        source = (ROOT / "scripts" / "offline_gate.py").read_text(encoding="utf-8")

        self.assertIn('["authorized_setup_connections"] == 0', source)

    def test_offline_gate_never_invokes_an_online_setup_route(self) -> None:
        source = (ROOT / "scripts" / "offline_gate.py").read_text(encoding="utf-8")

        self.assertNotIn("/pull-sources", source)
        self.assertIn('"QUALSIGHT_SETUP_NETWORK_DISABLED": "1"', source)

    def test_offline_cached_source_preparation_is_port_checked_and_leased(self) -> None:
        source = (ROOT / "scripts" / "offline_gate.py").read_text(encoding="utf-8")

        port_check = source.index("_assert_endpoint_available(API_HOST, API_PORT)")
        lease = source.index("DataDirectoryLease.acquire(")
        prepare = source.index("upstream.acquire_cached()")
        release = source.index("preparation_lease.close()")
        self.assertLess(port_check, lease)
        self.assertLess(lease, prepare)
        self.assertLess(prepare, release)

    def test_release_gate_clears_ambient_runtime_overrides_and_pins_registry(self) -> None:
        module = _load_script("run_release_beta_gate")
        definitions = module.gate_definitions(ROOT, sys.executable)
        expected = {
            "QUALSIGHT_REPOSITORY_ROOT": str(ROOT.resolve()),
            "QUALSIGHT_REGISTRY_PATH": str(
                (ROOT / "resources/resource-registry.json").resolve()
            ),
            "QUALSIGHT_RUNTIME_RESOURCE_ID": "ofiq-v1-1-2-linux-runtime",
            "QUALSIGHT_INPUT_RESOURCE_IDS": (
                "ofiq-r-01-conformance-input,lfw-funneled-local-candidate"
            ),
            "QUALSIGHT_NETWORK_GUARD": "1",
        }
        for definition in definitions:
            environment = dict(definition.environment)
            if "QUALSIGHT_REPOSITORY_ROOT" in environment:
                with self.subTest(gate=definition.name):
                    self.assertTrue(expected.items() <= environment.items())
        source = (ROOT / "scripts/run_release_beta_gate.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('not key.startswith("QUALSIGHT_")', source)

    def test_lfw_runtime_and_browser_gates_resolve_the_reviewed_registry_tree(self) -> None:
        lfw_consumers = (
            "backend/src/qualsight/datasets.py",
            "backend/src/qualsight/engines/ofiq/batch.py",
            "scripts/audit_console.py",
            "scripts/observe_console.py",
            "scripts/observe_demo.py",
            "scripts/validate_comparison_studio.py",
            "scripts/validate_evidence_warning_privacy.py",
            "scripts/validate_owner_purge_ui.py",
            "scripts/validate_stress_studio.py",
            "scripts/validate_upload_working_sample.py",
        )
        for relative in lfw_consumers:
            with self.subTest(relative=relative):
                self.assertNotIn(
                    "/mnt/projects/datasets/lfw",
                    (ROOT / relative).read_text(encoding="utf-8"),
                )
        resolver = _load_script("controlled_inputs")
        registry = json.loads(
            (ROOT / "resources/resource-registry.json").read_text(encoding="utf-8")
        )
        resource = next(
            item
            for item in registry["resources"]
            if item["resource_id"] == "lfw-funneled-local-candidate"
        )
        expected = Path(
            next(
                check["root"]
                for check in resource["checks"]
                if check["type"] == "tree_manifest"
            )
        )
        self.assertEqual(resolver.lfw_tree_root(ROOT), expected)

    def test_offline_gate_preserves_controlled_venv_entrypoint(self) -> None:
        module = _load_script("offline_gate")
        venv_entrypoint = Path(sys.executable)
        self.assertTrue(venv_entrypoint.is_symlink())
        self.assertEqual(
            module._server_command(venv_entrypoint)[0],
            str(venv_entrypoint.absolute()),
        )

    def test_offline_release_gate_serves_and_hashes_the_staged_console(self) -> None:
        gate = _load_script("run_release_beta_gate")
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "committed-stage"
            console_dist = stage / "frontend" / "dist"
            console_dist.parent.mkdir(parents=True)
            shutil.copytree(ROOT / "frontend" / "dist", console_dist)
            definitions = gate.gate_definitions(
                stage,
                sys.executable,
                deployment_root=ROOT,
            )
        offline = next(
            definition
            for definition in definitions
            if definition.name == "offline-workflow"
        )
        arguments = list(offline.argv)
        self.assertEqual(
            arguments[arguments.index("--repository-root") + 1],
            str(ROOT.resolve()),
        )
        self.assertEqual(
            arguments[arguments.index("--console-index") + 1],
            str((console_dist / "index.html").resolve()),
        )
        environment = dict(offline.environment)
        self.assertEqual(environment["QUALSIGHT_REPOSITORY_ROOT"], str(ROOT.resolve()))
        self.assertIn(
            '"QUALSIGHT_CONSOLE_ROOT": str(source_root)',
            inspect.getsource(_load_script("offline_gate").main),
        )

    def test_release_gate_validates_documentation_evidence_as_documentation(self) -> None:
        gate = _load_script("run_release_beta_gate")
        evidence_body = (
            ROOT / "atg" / "evidence" / "2026-08-19-docs-workflow-gate-04.json"
        ).read_bytes()
        self.assertIn(b"/tmp/qualsight-docs-gate-", evidence_body)

        validated = gate._validate_gate_evidence(
            "documentation-workflow",
            evidence_body,
            Path("/tmp/release-beta-gate-evidence"),
        )

        self.assertEqual(
            validated["path"], "<GATE_WORK>/documentation-workflow.json"
        )
        self.assertEqual(
            validated["report"]["kind"], "QUALSIGHT_DOCS_WORKFLOW_GATE"
        )
        self.assertEqual(validated["report"]["blocks"], 11)
        encoded = json.dumps(validated, sort_keys=True)
        self.assertNotIn("/tmp/", encoded)
        self.assertNotIn("/mnt/", encoded)
        self.assertNotIn("stdout_tail", encoded)
        self.assertNotIn("stderr_tail", encoded)

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
        build_ref = components[("build", "QualSight release toolchain", "0.1.0b5")]["bom-ref"]
        self.assertIn(pydantic_ref, dependency_graph[fastapi_ref])
        self.assertIn(esbuild_ref, dependency_graph[vite_ref])
        self.assertIn(pip_audit_ref, dependency_graph[build_ref])
        conan_components = [
            component
            for component in first["components"]
            if component["properties"][0]["value"] == "conan"
        ]
        self.assertEqual(len(conan_components), 15)
        conan_contexts = [
            next(
                row["value"]
                for row in component["properties"]
                if row["name"] == "qualsight:conan-context"
            )
            for component in conan_components
        ]
        self.assertEqual(conan_contexts.count("build"), 3)
        self.assertEqual(conan_contexts.count("host"), 12)
        self.assertEqual(
            {component["bom-ref"] for component in conan_components},
            {reference for reference in dependency_graph[build_ref] if reference.startswith("pkg:conan/")},
        )
        self.assertTrue(all(component["scope"] == "optional" for component in conan_components))
        self.assertTrue(
            all(
                next(
                    row["value"]
                    for row in component["properties"]
                    if row["name"] == "qualsight:bundled"
                )
                == "false"
                for component in conan_components
            )
        )
        application_ref = first["metadata"]["component"]["bom-ref"]
        self.assertLess(len(dependency_graph[application_ref]), len(first["components"]))
        known_references = {application_ref, *(component["bom-ref"] for component in first["components"])}
        self.assertEqual(set(dependency_graph), known_references)
        self.assertTrue(
            all(target in known_references for targets in dependency_graph.values() for target in targets)
        )

        resource_components = {
            component["name"]: component
            for component in first["components"]
            if any(
                property_["name"] == "qualsight:ecosystem"
                and property_["value"] == "governed-scientific-resource"
                for property_ in component.get("properties", [])
            )
        }
        registry = json.loads(
            (ROOT / "resources" / "resource-registry.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(resource_components), len(registry["resources"]))
        self.assertIn("ofiq-v1-1-2-linux-runtime", resource_components)
        self.assertIn("lfw-funneled-local-candidate", resource_components)
        self.assertIn("transface-s-glint360k-local-candidate", resource_components)
        transface_properties = {
            row["name"]: row["value"]
            for row in resource_components[
                "transface-s-glint360k-local-candidate"
            ]["properties"]
        }
        self.assertEqual(
            transface_properties["qualsight:third-party-rights-status"],
            "NO_LICENSE_FOUND",
        )
        self.assertEqual(transface_properties["qualsight:bundled"], "false")
        self.assertEqual(
            transface_properties["qualsight:distribution-class"],
            "runtime-downloadable-resource-manifest",
        )
        magface_properties = {
            row["name"]: row["value"]
            for row in resource_components[
                "magface-iresnet100-ms1mv2-local-candidate"
            ]["properties"]
        }
        self.assertEqual(
            magface_properties["qualsight:distribution-class"],
            "runtime-downloadable-resource-manifest",
        )
        blocked_native_properties = {
            row["name"]: row["value"]
            for row in resource_components[
                "ofiq-v1-1-2-cmake-runtime-candidate"
            ]["properties"]
        }
        self.assertEqual(
            blocked_native_properties["qualsight:distribution-class"],
            "externally-blocked-native-resolution",
        )
        ofiq_runtime = resource_components["ofiq-v1-1-2-linux-runtime"]
        self.assertEqual(ofiq_runtime["scope"], "required")
        deferred_matchers = {
            resource_components[name]["bom-ref"]
            for name in (
                "magface-iresnet100-ms1mv2-local-candidate",
                "topofr-r100-glint360k-local-candidate",
                "transface-b-glint360k-local-candidate",
            )
        }
        application_ref = first["metadata"]["component"]["bom-ref"]
        self.assertTrue(
            deferred_matchers.isdisjoint(dependency_graph[application_ref])
        )
        fastapi = components[("python", "fastapi", "0.141.1")]
        self.assertTrue(fastapi["licenses"])
        self.assertEqual(
            next(
                row["value"]
                for row in fastapi["properties"]
                if row["name"] == "qualsight:license-evidence"
            ),
            "docs/DEPENDENCY_AUDIT.md",
        )
        for runtime_name in ("react", "react-dom", "scheduler", "loose-envify"):
            runtime_component = next(
                component
                for component in frontend
                if component["name"] == runtime_name
            )
            runtime_properties = {
                row["name"]: row["value"]
                for row in runtime_component["properties"]
            }
            self.assertEqual(runtime_properties["qualsight:bundled"], "true")
            self.assertEqual(
                runtime_properties["qualsight:distribution-class"],
                "frontend-runtime-bundled-into-console-assets",
            )
        for build_name in ("vite", "typescript", "@vitejs/plugin-react"):
            build_component = next(
                component
                for component in frontend
                if component["name"] == build_name
            )
            build_properties = {
                row["name"]: row["value"]
                for row in build_component["properties"]
            }
            self.assertEqual(build_properties["qualsight:bundled"], "false")
            self.assertEqual(
                build_properties["qualsight:distribution-class"],
                "frontend-build-package-not-distributed-as-package",
            )

    def test_install_validates_only_the_real_registered_ofiq_runtime(self) -> None:
        completed = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts" / "ofiq_v112_build.sh"),
                "--validate-existing",
                "--registry",
                str(ROOT / "resources" / "resource-registry.json"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("OFIQ_RUNTIME_VALID", completed.stdout)

        install_script = (ROOT / "scripts" / "install.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("ofiq_v112_build.sh --validate-existing", install_script)
        self.assertIn("includes the reviewed Conan lock/profile/closure", install_script)
        self.assertIn("--build-root set to a new absolute external directory", install_script)
        self.assertNotIn("remains blocked without a reviewed Conan lock", install_script)
        self.assertIn("validate_resource_registry.py", install_script)
        self.assertNotIn("qualsight.cli setup", install_script)
        self.assertNotIn("the OFIQ v1.1.2 tag/model archives", install_script)
        self.assertNotIn("GCC 12 toolchain for the OFIQ build", install_script)
        install_document = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        self.assertNotIn("→ `qualsight setup`", install_document)
        self.assertIn(
            "Add-on downloads are explicit operator actions after installation",
            install_document,
        )

    def test_manifest_hashes_real_staged_files_and_refuses_overwrite(self) -> None:
        module = _load_script("generate_release_manifest")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            artifacts = [ROOT / "uv.lock", ROOT / "pnpm-lock.yaml"]
            output = directory / "manifest.json"
            expected_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            expected_tree = subprocess.run(
                ["git", "rev-parse", f"{expected_commit}^{{tree}}"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            module.write_manifest(
                root=ROOT,
                output=output,
                version="0.1.0b5",
                commit=expected_commit,
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
            release_policy = json.loads(
                (ROOT / "resources/security/release-package-policy-v2.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                value["source_archive_policy"]["excluded_repository_paths"],
                release_policy["source_archive_excluded_paths"],
            )
            with self.assertRaises(FileExistsError):
                module.write_manifest(
                    root=ROOT,
                    output=output,
                    version="0.1.0b5",
                    commit=expected_commit,
                    tree=expected_tree,
                    artifacts=artifacts,
                )


class ReleaseBoundaryHardeningRealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_script("validate_release_package")
        self.policy = json.loads(
            (ROOT / "resources/security/release-package-policy-v2.json").read_text(
                encoding="utf-8"
            )
        )

    def _validate_only_file(self, source: Path) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package_root = Path(temporary) / "package"
            target = package_root / "docs" / "included.txt"
            target.parent.mkdir(parents=True)
            shutil.copy2(source, target)
            package_files = self.module._snapshot_package_tree(package_root)
            self.module._validate_package_tree(package_files, self.policy)

    def test_extracted_validator_rejects_disguised_registered_biometric_and_model_bytes(
        self,
    ) -> None:
        first_record = json.loads(
            (ROOT / "resources/manifests/lfw-funneled-images-v1.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        lfw = Path("/mnt/projects/datasets/lfw/lfw_funneled") / first_record["path"]
        self.assertEqual(lfw.stat().st_size, first_record["size_bytes"])
        self.assertEqual(hashlib.sha256(lfw.read_bytes()).hexdigest(), first_record["sha256"])

        detector_manifest = json.loads(
            (
                ROOT
                / "resources/manifests/insightface-buffalo-l-release-provenance-v1.json"
            ).read_text(encoding="utf-8")
        )["registered_detector"]
        model = Path(detector_manifest["model_path"])
        self.assertEqual(model.stat().st_size, detector_manifest["model_size_bytes"])
        self.assertEqual(
            hashlib.sha256(model.read_bytes()).hexdigest(),
            detector_manifest["model_sha256"],
        )

        for source in (lfw, model):
            with self.subTest(source=source), self.assertRaisesRegex(
                self.module.ReleasePackageValidationError,
                "forbidden release content",
            ):
                self._validate_only_file(source)

    def test_extracted_validator_rejects_real_installed_private_key_material(self) -> None:
        source_boundary = _load_script("create_release_source_archive")
        private_key = Path("/usr/share/doc/libnet-ssleay-perl/examples/server_key.pem")
        self.assertTrue(private_key.is_file())
        body = private_key.read_bytes()
        matching_labels = [
            label for label, pattern in source_boundary.SECRET_PATTERNS if pattern.search(body)
        ]
        self.assertEqual(matching_labels, ["private key"])
        with self.assertRaisesRegex(
            self.module.ReleasePackageValidationError,
            "private key",
        ):
            self._validate_only_file(private_key)

    def test_extracted_validator_rejects_a_real_fifo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package_root = Path(temporary) / "package"
            package_root.mkdir()
            fifo = package_root / "unexpected-pipe"
            os.mkfifo(fifo, mode=0o600)
            self.assertTrue(stat.S_ISFIFO(fifo.stat(follow_symlinks=False).st_mode))
            with self.assertRaisesRegex(
                self.module.ReleasePackageValidationError,
                "non-regular",
            ):
                self.module._snapshot_package_tree(package_root)

    def test_extracted_validator_rejects_encoded_real_operator_home(self) -> None:
        controlled_home = str(Path.home().resolve()).encode("utf-8")
        markers = (
            base64.b64encode(controlled_home),
            controlled_home.hex().encode("ascii"),
            controlled_home.hex().upper().encode("ascii"),
            ", ".join(str(value) for value in controlled_home).encode("ascii"),
            quote_from_bytes(controlled_home, safe="").encode("ascii"),
            quote_from_bytes(controlled_home, safe="").lower().encode("ascii"),
            controlled_home.replace(b"/", b"\\u002f"),
            controlled_home.replace(b"/", b"\\u002F"),
        )
        for marker in markers:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as temporary:
                package_root = Path(temporary) / "package"
                target = package_root / "docs" / "location.txt"
                target.parent.mkdir(parents=True)
                target.write_bytes(marker + b"\n")
                with self.assertRaisesRegex(
                    self.module.ReleasePackageValidationError,
                    "encoded operator home",
                ):
                    package_files = self.module._snapshot_package_tree(package_root)
                    self.module._validate_package_tree(package_files, self.policy)

    def test_release_sidecar_validator_accepts_prior_real_sidecars_and_rejects_encoded_home(
        self,
    ) -> None:
        prior_release = ROOT / "dist" / "qualsight-0.1.0b2"
        sidecars = sorted(
            path
            for path in prior_release.iterdir()
            if path.is_file() and not path.name.endswith(".tar.gz")
        )
        self.assertEqual(len(sidecars), 6)
        result = self.module.validate_release_sidecars(
            sidecars,
            policy_path=ROOT / "resources/security/release-package-policy-v2.json",
        )
        self.assertEqual(result["regular_files"], 6)

        with tempfile.TemporaryDirectory() as temporary:
            unsafe = Path(temporary) / sidecars[0].name
            shutil.copy2(sidecars[0], unsafe)
            unsafe.write_bytes(
                unsafe.read_bytes()
                + b"\n"
                + quote_from_bytes(
                    str(Path.home().resolve()).encode("utf-8"), safe=""
                ).encode("ascii")
            )
            with self.assertRaisesRegex(
                self.module.ReleasePackageValidationError,
                "encoded operator home",
            ):
                self.module.validate_release_sidecars(
                    [unsafe],
                    policy_path=ROOT
                    / "resources/security/release-package-policy-v2.json",
                )
            shutil.copy2(sidecars[0], unsafe)
            unsafe.write_bytes(
                unsafe.read_bytes() + b"\n" + str(ROOT.resolve()).encode("utf-8")
            )
            with self.assertRaisesRegex(
                self.module.ReleasePackageValidationError,
                "forbidden release content term '/mnt/'",
            ):
                self.module.validate_release_sidecars(
                    [unsafe],
                    policy_path=ROOT
                    / "resources/security/release-package-policy-v2.json",
                )

    def test_extracted_validator_requires_the_exact_registered_restricted_identities(
        self,
    ) -> None:
        detector_manifest = json.loads(
            (
                ROOT
                / "resources/manifests/insightface-buffalo-l-release-provenance-v1.json"
            ).read_text(encoding="utf-8")
        )["registered_detector"]
        weakened = json.loads(json.dumps(self.policy))
        weakened["source_archive_forbidden_artifact_sha256s"].remove(
            detector_manifest["model_sha256"]
        )
        self.module._validate_policy_minimum(weakened)
        with self.assertRaisesRegex(
            self.module.ReleasePackageValidationError,
            "registered restricted artifact identities differ",
        ):
            registry_path = Path("resources/resource-registry.json")
            package_files = {
                registry_path.as_posix(): (
                    (ROOT / registry_path).read_bytes(),
                    stat.S_IMODE((ROOT / registry_path).stat().st_mode),
                )
            }
            self.module._validate_registered_restricted_policy(package_files, weakened)

    def test_release_preflight_and_console_validation_precede_mutation_and_archive(
        self,
    ) -> None:
        release = (ROOT / "scripts/release.sh").read_text(encoding="utf-8")
        preflight = 'scripts/validate_install_preflight.py'
        first_dependency_mutation = 'sync --frozen'
        console_validation = '--console-root "$BUILD_ROOT/frontend/dist"'
        console_archive = 'tar --sort=name'
        for token in (preflight, first_dependency_mutation, console_validation, console_archive):
            self.assertIn(token, release)
        self.assertLess(release.index(preflight), release.index(first_dependency_mutation))
        self.assertLess(release.index(console_validation), release.index(console_archive))
        for flag in ("--python", "--uv", "--node", "--pnpm"):
            self.assertIn(flag, release[release.index(preflight) : release.index(first_dependency_mutation)])

    def test_console_validator_rejects_real_path_bearing_maps_and_accepts_runtime_assets(
        self,
    ) -> None:
        dist = ROOT / "frontend" / "dist"
        dependency_map = next(
            path for path in sorted((ROOT / "node_modules").rglob("*.map")) if path.is_file()
        )
        with tempfile.TemporaryDirectory() as temporary:
            runtime_dist = Path(temporary) / "dist"
            shutil.copytree(dist, runtime_dist)
            result = self.module.validate_console_dist(runtime_dist)
            self.assertEqual(
                result["regular_files"],
                sum(path.is_file() for path in runtime_dist.rglob("*")),
            )
            self.assertGreater(result["total_bytes"], 0)

            source_map = runtime_dist / "dependency.js.map"
            shutil.copy2(dependency_map, source_map)
            with self.assertRaisesRegex(
                self.module.ReleasePackageValidationError,
                "source map",
            ):
                self.module.validate_console_dist(runtime_dist)
            source_map.unlink()

            path_record = runtime_dist / "build-location.txt"
            path_record.write_text(str(ROOT.resolve()), encoding="utf-8")
            with self.assertRaisesRegex(
                self.module.ReleasePackageValidationError,
                "private build path",
            ):
                self.module.validate_console_dist(runtime_dist)

            encoded_paths = (
                quote_from_bytes(str(ROOT.resolve()).encode("utf-8"), safe="").encode(
                    "ascii"
                ),
                str(ROOT.resolve()).encode("utf-8").replace(b"/", b"\\u002f"),
            )
            for encoded_path in encoded_paths:
                with self.subTest(encoded_path=encoded_path):
                    path_record.write_bytes(encoded_path)
                    with self.assertRaisesRegex(
                        self.module.ReleasePackageValidationError,
                        "private build path",
                    ):
                        self.module.validate_console_dist(runtime_dist)

    def test_frontend_release_build_disables_source_maps(self) -> None:
        config = (ROOT / "frontend/vite.config.ts").read_text(encoding="utf-8")
        self.assertIn("build: { sourcemap: false }", config)

    def test_release_validates_generated_sidecars_before_atomic_publication(self) -> None:
        release = (ROOT / "scripts/release.sh").read_text(encoding="utf-8")
        validation = (
            'scripts/validate_release_package.py" \\\n'
            '  --policy "$BUILD_ROOT/resources/security/release-package-policy-v2.json" \\\n'
            '  --sidecar "$SBOM"'
        )
        checksums = '"$(basename "$MANIFEST")" "$(basename "$COMMIT_FILE")" > "$(basename "$CHECKSUMS")")'
        publication = 'mv -T -n "$SET" "$FINAL_DIR"'
        self.assertIn(validation, release)
        self.assertLess(release.index(checksums), release.index(validation))
        self.assertLess(release.index(validation), release.index(publication))


if __name__ == "__main__":
    unittest.main()
