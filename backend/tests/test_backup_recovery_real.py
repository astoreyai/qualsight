"""Offline backup and restore over real QualSight records and managed image bytes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient

from qualsight.api import Settings, create_app
from qualsight.api.store import RunStore
from qualsight.execution.registry_binding import (
    OFIQ_INPUT_RESOURCE_ID,
    OFIQ_V112_RUNTIME_RESOURCE_ID,
)
from qualsight.security.backup import create_backup, restore_backup, verify_backup


ROOT = Path(__file__).resolve().parents[2]
LFW_RESOURCE_ID = "lfw-funneled-local-candidate"
REAL_IMAGE = Path("/mnt/projects/datasets/lfw/lfw_funneled/George_W_Bush/George_W_Bush_0001.jpg")
RESEARCH_SCOPE = "QUALSIGHT_LOCAL_NONCOMMERCIAL_RESEARCH_V1"


class BackupRecoveryRealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="qualsight-backup-recovery-"))
        self.data_dir = self.root / "data"
        self.backup_dir = self.root / "backup"
        self.settings = Settings(
            repository_root=ROOT,
            registry_path=ROOT / "resources" / "resource-registry.json",
            data_dir=self.data_dir,
            runtime_resource_id=OFIQ_V112_RUNTIME_RESOURCE_ID,
            input_resource_ids=(OFIQ_INPUT_RESOURCE_ID, LFW_RESOURCE_ID),
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _cli_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join((str(ROOT / "backend" / "src"), str(ROOT))),
                "QUALSIGHT_REPOSITORY_ROOT": str(ROOT),
                "QUALSIGHT_REGISTRY_PATH": str(self.settings.registry_path),
                "QUALSIGHT_DATA_DIR": str(self.data_dir),
                "QUALSIGHT_RUNTIME_RESOURCE_ID": OFIQ_V112_RUNTIME_RESOURCE_ID,
                "QUALSIGHT_INPUT_RESOURCE_IDS": f"{OFIQ_INPUT_RESOURCE_ID},{LFW_RESOURCE_ID}",
                "QUALSIGHT_NETWORK_GUARD": "0",
            }
        )
        return environment

    def _run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "qualsight.cli", *arguments],
            cwd=ROOT,
            env=self._cli_environment(),
            capture_output=True,
            text=True,
            check=False,
        )

    def _record_real_managed_image(self) -> tuple[str, str, Path, str]:
        source_bytes = REAL_IMAGE.read_bytes()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        with TestClient(create_app(self.settings), base_url="http://127.0.0.1") as client:
            project = client.post(
                "/api/projects",
                json={
                    "name": "Backup recovery gate",
                    "description": "Restore verification over a real registered LFW image.",
                    "purpose_scope": RESEARCH_SCOPE,
                },
            )
            self.assertEqual(project.status_code, 201, project.text)
            project_id = project.json()["project_id"]
            self.assertEqual(client.post(f"/api/projects/{project_id}/open").status_code, 200)
            assessed = client.post(
                "/api/quality/assess-upload",
                params={
                    "project_id": project_id,
                    "purpose_scope": RESEARCH_SCOPE,
                    "filename": REAL_IMAGE.name,
                },
                content=source_bytes,
                headers={"content-type": "application/octet-stream"},
            )
            self.assertEqual(assessed.status_code, 201, assessed.text)
            assessed_body = assessed.json()
            sample_id = assessed_body["sample_id"]
            run_id = assessed_body["run_id"]
            self.assertNotIn("stored_path", assessed_body)
        recorded = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            sample = recorded.sample(sample_id)
            self.assertIsNotNone(sample)
            stored_path = Path(sample.source_path)
            self.assertEqual(hashlib.sha256(stored_path.read_bytes()).hexdigest(), source_sha256)
        finally:
            recorded.close()
        return project_id, run_id, stored_path, source_sha256

    def _add_real_lfw_copy_material(self) -> None:
        real_bytes = REAL_IMAGE.read_bytes()
        material = self.data_dir / "publication-race-real-lfw-copies"
        material.mkdir()
        for index in range(32):
            (material / f"{REAL_IMAGE.stem}-{index:02d}{REAL_IMAGE.suffix}").write_bytes(
                real_bytes
            )

    def test_backup_cli_creates_verifies_and_restores_real_managed_bytes(self) -> None:
        project_id, run_id, stored_path, source_sha256 = self._record_real_managed_image()

        created_process = self._run_cli("backup-create", "--output", str(self.backup_dir))
        self.assertEqual(created_process.returncode, 0, created_process.stderr)
        created = json.loads(created_process.stdout)
        self.assertTrue(created["verified"])

        verified_process = self._run_cli("backup-verify", "--backup", str(self.backup_dir))
        self.assertEqual(verified_process.returncode, 0, verified_process.stderr)
        self.assertEqual(json.loads(verified_process.stdout)["file_count"], created["file_count"])

        displaced = self.root / "data-before-restore"
        self.data_dir.rename(displaced)
        restored_process = self._run_cli("backup-restore", "--backup", str(self.backup_dir))
        self.assertEqual(restored_process.returncode, 0, restored_process.stderr)
        restored = json.loads(restored_process.stdout)
        self.assertTrue(restored["verified"])
        self.assertEqual(hashlib.sha256(stored_path.read_bytes()).hexdigest(), source_sha256)

        recovered = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            self.assertIsNotNone(recovered.project(project_id))
            self.assertIsNotNone(recovered.run(run_id))
        finally:
            recovered.close()

    def test_backup_creation_requires_the_exclusive_data_directory_lease(self) -> None:
        self._record_real_managed_image()
        store = RunStore(self.data_dir / "qualsight.sqlite3")
        try:
            with self.assertRaisesRegex(ValueError, "exclusive data-directory lease"):
                create_backup(store, self.data_dir, self.backup_dir)
        finally:
            store.close()

    def test_backup_verification_rejects_symlinked_managed_file_ancestor(self) -> None:
        self._record_real_managed_image()
        created_process = self._run_cli("backup-create", "--output", str(self.backup_dir))
        self.assertEqual(created_process.returncode, 0, created_process.stderr)
        manifest = json.loads(
            (self.backup_dir / "manifest.json").read_text(encoding="utf-8")
        )
        relative = Path(manifest["files"][0]["path"])
        self.assertGreater(len(relative.parts), 1)
        managed_root = self.backup_dir / "files"
        ancestor = managed_root / relative.parts[0]
        relocated = managed_root / f"{relative.parts[0]}-relocated"
        ancestor.rename(relocated)
        ancestor.symlink_to(relocated.name, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "symlink"):
            verify_backup(self.backup_dir)

    def test_backup_restore_atomically_refuses_a_late_destination(self) -> None:
        self._record_real_managed_image()
        self._add_real_lfw_copy_material()
        created_process = self._run_cli("backup-create", "--output", str(self.backup_dir))
        self.assertEqual(created_process.returncode, 0, created_process.stderr)
        displaced = self.root / "data-before-collision"
        self.data_dir.rename(displaced)

        collision_created = threading.Event()
        contender_error: list[BaseException] = []

        def create_late_destination() -> None:
            deadline = time.monotonic() + 10
            try:
                while time.monotonic() < deadline:
                    if any(self.root.glob(f".{self.data_dir.name}.*.restore")):
                        self.data_dir.mkdir()
                        collision_created.set()
                        return
                    time.sleep(0.001)
                raise TimeoutError("restore staging directory did not appear")
            except BaseException as error:
                contender_error.append(error)

        contender = threading.Thread(target=create_late_destination)
        contender.start()
        try:
            with self.assertRaises(FileExistsError):
                restore_backup(self.backup_dir, self.data_dir)
        finally:
            contender.join(timeout=15)
        self.assertFalse(contender.is_alive())
        self.assertEqual(contender_error, [])
        self.assertTrue(collision_created.is_set())
        self.assertTrue(self.data_dir.is_dir())
        self.assertEqual(list(self.data_dir.iterdir()), [])

    def test_backup_creation_atomically_refuses_a_late_destination(self) -> None:
        self._record_real_managed_image()
        self._add_real_lfw_copy_material()
        collision_created = threading.Event()
        contender_error: list[BaseException] = []

        def create_late_destination() -> None:
            deadline = time.monotonic() + 10
            try:
                while time.monotonic() < deadline:
                    if any(self.root.glob(f".{self.backup_dir.name}.*.partial")):
                        self.backup_dir.mkdir()
                        collision_created.set()
                        return
                    time.sleep(0.001)
                raise TimeoutError("backup staging directory did not appear")
            except BaseException as error:
                contender_error.append(error)

        contender = threading.Thread(target=create_late_destination)
        contender.start()
        try:
            process = self._run_cli("backup-create", "--output", str(self.backup_dir))
        finally:
            contender.join(timeout=15)
        self.assertFalse(contender.is_alive())
        self.assertEqual(contender_error, [])
        self.assertTrue(collision_created.is_set())
        self.assertNotEqual(process.returncode, 0, process.stdout)
        self.assertTrue(self.backup_dir.is_dir())
        self.assertEqual(list(self.backup_dir.iterdir()), [])

    def test_backup_cli_refuses_while_another_process_owns_and_mutates_the_data_directory(self) -> None:
        self._record_real_managed_image()
        holder_code = (
            "from pathlib import Path\n"
            "import sys\n"
            "from qualsight.security.process_lease import DataDirectoryLease\n"
            "root = Path(sys.argv[1])\n"
            "source = Path(sys.argv[2])\n"
            "lease = DataDirectoryLease.acquire(root, owner='concurrent-real-file-writer')\n"
            "(root / 'concurrent-real-image-copy.jpg').write_bytes(source.read_bytes())\n"
            "print('ready', flush=True)\n"
            "sys.stdin.readline()\n"
            "lease.close()\n"
        )
        environment = self._cli_environment()
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code, str(self.data_dir), str(REAL_IMAGE)],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertIsNotNone(holder.stdout)
            self.assertEqual(holder.stdout.readline().strip(), "ready")
            refused = self._run_cli("backup-create", "--output", str(self.backup_dir))
            self.assertEqual(refused.returncode, 4, refused.stderr)
            self.assertEqual(json.loads(refused.stderr)["error"], "DATA_DIRECTORY_BUSY")
            self.assertFalse(self.backup_dir.exists())
        finally:
            if holder.poll() is None:
                self.assertIsNotNone(holder.stdin)
                holder.stdin.write("release\n")
                holder.stdin.flush()
                holder.wait(timeout=30)
            for stream in (holder.stdin, holder.stdout, holder.stderr):
                if stream is not None:
                    stream.close()


if __name__ == "__main__":
    unittest.main()
