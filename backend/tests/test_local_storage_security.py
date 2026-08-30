"""Local storage controls exercised on bytes from a registered real photograph."""

from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from qualsight.api.settings import Settings
from qualsight.security.local_storage import (
    LocalStorageSecurityError,
    activate_private_umask,
    secure_data_tree,
)


REAL_PHOTO = Path("/mnt/projects/02_perception_biometrics/OFIQ-Project/data/tests/images/r-01-frontal.png")


class LocalStorageSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parent = Path(tempfile.mkdtemp(prefix="qualsight-storage-"))
        self.data_dir = self.parent / "data"
        self.data_dir.mkdir(mode=0o755)
        self.photo = self.data_dir / REAL_PHOTO.name
        shutil.copyfile(REAL_PHOTO, self.photo)
        self.photo.chmod(0o644)

    def tearDown(self) -> None:
        shutil.rmtree(self.parent, ignore_errors=True)

    def test_existing_application_tree_is_remediated_to_private_modes(self) -> None:
        result = secure_data_tree(self.data_dir)
        self.assertEqual(result, {"directories": 1, "files": 1})
        self.assertEqual(self.data_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.photo.stat().st_mode & 0o777, 0o600)

    def test_new_application_files_inherit_private_mode(self) -> None:
        previous = activate_private_umask()
        try:
            path = self.data_dir / "copied-real-photo.png"
            shutil.copyfile(REAL_PHOTO, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        finally:
            os.umask(previous)

    def test_symlink_inside_application_tree_is_refused(self) -> None:
        (self.data_dir / "linked-real-photo.png").symlink_to(REAL_PHOTO)
        with self.assertRaises(LocalStorageSecurityError):
            secure_data_tree(self.data_dir)

    def test_symlinked_ancestor_is_refused_without_changing_real_target_modes(self) -> None:
        real_parent = self.parent / "real-parent"
        real_root = real_parent / "managed"
        real_root.mkdir(parents=True, mode=0o755)
        real_file = real_root / REAL_PHOTO.name
        shutil.copyfile(REAL_PHOTO, real_file)
        real_file.chmod(0o644)
        linked_parent = self.parent / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        before = (real_root.stat().st_mode & 0o777, real_file.stat().st_mode & 0o777)

        with self.assertRaises(LocalStorageSecurityError):
            secure_data_tree(linked_parent / "managed")

        self.assertEqual(
            (real_root.stat().st_mode & 0o777, real_file.stat().st_mode & 0o777),
            before,
        )

    def test_filesystem_root_and_protected_ancestors_are_refused_before_traversal(self) -> None:
        with self.assertRaises(LocalStorageSecurityError):
            secure_data_tree(Path("/"))
        protected = self.data_dir / "managed"
        protected.mkdir()
        mode_before = self.data_dir.stat().st_mode & 0o777
        with self.assertRaises(LocalStorageSecurityError):
            secure_data_tree(self.data_dir, protected_roots=(protected,))
        self.assertEqual(self.data_dir.stat().st_mode & 0o777, mode_before)

    def test_environment_startup_enables_network_guard_unless_explicitly_disabled(self) -> None:
        registry = Path(__file__).resolve().parents[2] / "resources" / "resource-registry.json"
        previous = os.environ.pop("QUALSIGHT_NETWORK_GUARD", None)
        try:
            settings = Settings.from_environment(repository_root=registry.parents[1])
            self.assertTrue(settings.network_guard)
            os.environ["QUALSIGHT_NETWORK_GUARD"] = "false"
            settings = Settings.from_environment(repository_root=registry.parents[1])
            self.assertFalse(settings.network_guard)
        finally:
            if previous is None:
                os.environ.pop("QUALSIGHT_NETWORK_GUARD", None)
            else:
                os.environ["QUALSIGHT_NETWORK_GUARD"] = previous

    def test_data_directory_lease_excludes_another_process_and_releases(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        lease_dir = self.parent / "leased-data"
        code = (
            "from pathlib import Path\n"
            "from qualsight.security.process_lease import DataDirectoryLease\n"
            f"lease = DataDirectoryLease.acquire(Path({str(lease_dir)!r}), owner='subprocess-regression-test')\n"
            "print('LEASE_ACQUIRED', flush=True)\n"
            "input()\n"
            "lease.close()\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=repository_root / "backend",
            env={**os.environ, "PYTHONPATH": "src:.."},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertIsNotNone(child.stdout)
            ready = child.stdout.readline().strip()
            if ready != "LEASE_ACQUIRED":
                child.wait(timeout=10)
                self.fail(child.stderr.read() if child.stderr else "lease subprocess exited before acquisition")

            from qualsight.security.process_lease import DataDirectoryLease, DataDirectoryLeaseError

            with self.assertRaises(DataDirectoryLeaseError) as caught:
                DataDirectoryLease.acquire(lease_dir, owner="parent-regression-test")
            self.assertIn(f"pid={child.pid}", str(caught.exception))

            self.assertIsNotNone(child.stdin)
            child.stdin.write("\n")
            child.stdin.flush()
            self.assertEqual(child.wait(timeout=10), 0, child.stderr.read() if child.stderr else "")
            with DataDirectoryLease.acquire(lease_dir, owner="parent-regression-test"):
                pass
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()

    def test_reentrant_process_lease_stays_exclusive_until_the_last_owner_closes(self) -> None:
        from qualsight.security.process_lease import DataDirectoryLease

        repository_root = Path(__file__).resolve().parents[2]
        lease_dir = self.parent / "reentrant-data"
        contender_code = (
            "from pathlib import Path\n"
            "from qualsight.security.process_lease import DataDirectoryLease, DataDirectoryLeaseError\n"
            "try:\n"
            f"    lease = DataDirectoryLease.acquire(Path({str(lease_dir)!r}), owner='subprocess-contender')\n"
            "except DataDirectoryLeaseError:\n"
            "    raise SystemExit(4)\n"
            "lease.close()\n"
        )
        contender_environment = {**os.environ, "PYTHONPATH": "src:.."}

        first = DataDirectoryLease.acquire(lease_dir, owner="first-in-process-owner")
        second = None
        try:
            second = DataDirectoryLease.acquire(lease_dir, owner="second-in-process-owner")
            first.close()
            while_second_owns = subprocess.run(
                [sys.executable, "-c", contender_code],
                cwd=repository_root / "backend",
                env=contender_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(while_second_owns.returncode, 4, while_second_owns.stderr)
            second.close()
            after_release = subprocess.run(
                [sys.executable, "-c", contender_code],
                cwd=repository_root / "backend",
                env=contender_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(after_release.returncode, 0, after_release.stderr)
        finally:
            first.close()
            if second is not None:
                second.close()

    def test_api_lifespan_refuses_concurrent_cli_use_of_the_same_data_directory(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        shared_data_dir = self.parent / "api-cli-data"
        server_code = (
            "import json\n"
            "from pathlib import Path\n"
            "from fastapi.testclient import TestClient\n"
            "from qualsight.api.app import create_app\n"
            "from qualsight.api.settings import Settings\n"
            f"root = Path({str(repository_root)!r})\n"
            "settings = Settings.from_environment(repository_root=root)\n"
            "with TestClient(create_app(settings), base_url='http://127.0.0.1') as client:\n"
            "    health = client.get('/api/health')\n"
            "    health.raise_for_status()\n"
            "    print(json.dumps({'status': health.json()['status']}), flush=True)\n"
            "    input()\n"
        )
        server = subprocess.Popen(
            [sys.executable, "-c", server_code],
            cwd=repository_root / "backend",
            env={
                **os.environ,
                "PYTHONPATH": "src:..",
                "QUALSIGHT_REPOSITORY_ROOT": str(repository_root),
                "QUALSIGHT_DATA_DIR": str(shared_data_dir),
                "QUALSIGHT_NETWORK_GUARD": "false",
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertIsNotNone(server.stdout)
            ready = server.stdout.readline().strip()
            if not ready:
                server.wait(timeout=30)
                self.fail(server.stderr.read() if server.stderr else "API subprocess exited before readiness")
            self.assertEqual(json.loads(ready), {"status": "ok"})

            cli = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "qualsight.cli",
                    "project-create",
                    "--name",
                    "Cross-process ownership verification",
                    "--description",
                    "Real API-versus-CLI exclusion regression",
                    "--scope",
                    "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1",
                ],
                cwd=repository_root / "backend",
                env={
                    **os.environ,
                    "PYTHONPATH": "src:..",
                    "QUALSIGHT_REPOSITORY_ROOT": str(repository_root),
                    "QUALSIGHT_DATA_DIR": str(shared_data_dir),
                    "QUALSIGHT_NETWORK_GUARD": "false",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(cli.returncode, 4, cli.stderr)
            refusal = json.loads(cli.stderr.strip().splitlines()[-1])
            self.assertEqual(refusal["error"], "DATA_DIRECTORY_BUSY")
            self.assertIn(f"pid={server.pid}", refusal["detail"])

            self.assertIsNotNone(server.stdin)
            server.stdin.write("\n")
            server.stdin.flush()
            self.assertEqual(server.wait(timeout=30), 0, server.stderr.read() if server.stderr else "")
        finally:
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=10)
            for stream in (server.stdin, server.stdout, server.stderr):
                if stream is not None:
                    stream.close()

    def test_real_cli_assessment_installs_the_default_network_guard(self) -> None:
        import subprocess
        import sys

        repository_root = Path(__file__).resolve().parents[2]
        cli_data = self.parent / "cli-data"
        code = (
            "import json\n"
            "from qualsight.cli import main\n"
            "from qualsight.security.network_guard import guard_status\n"
            f"exit_code = main(['assess', '--image', {str(REAL_PHOTO)!r}, '--scope', 'REPRODUCE_OFIQ_CONFORMANCE_TESTS'])\n"
            "print(json.dumps({'exit_code': exit_code, 'guard': guard_status()}, sort_keys=True))\n"
            "raise SystemExit(exit_code)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repository_root / "backend",
            env={
                **os.environ,
                "PYTHONPATH": "src:..",
                "QUALSIGHT_REPOSITORY_ROOT": str(repository_root),
                "QUALSIGHT_DATA_DIR": str(cli_data),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["guard"]["installed"])
        self.assertEqual(report["guard"]["refused_attempts"], [])


if __name__ == "__main__":
    unittest.main()
