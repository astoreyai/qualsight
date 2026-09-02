"""Plan F QS-S95 — adversarial hardening of decision-30 admission (unit level, real bytes).

Every payload derives from real registered LFW photographs; the oversize case
repeats real bytes past the cap (refused before any decode), the decode-guard
case lowers the guard below a real image's pixel count to exercise the refusal
path on real data.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from qualsight.api.store import RunStore
from qualsight.importers import owner_supplied
from qualsight.importers.owner_supplied import (
    MAX_UPLOAD_BYTES,
    OwnerSuppliedAdmission,
    OwnerSuppliedError,
    OwnerSuppliedImporter,
)

LFW_TREE = Path("/mnt/projects/datasets/lfw/lfw_funneled")
REAL_PHOTO = LFW_TREE / "Colin_Powell" / "Colin_Powell_0001.jpg"
SAME_SIZE_PHOTO = LFW_TREE / "Ahmed_Ghazi" / "Ahmed_Ghazi_0001.jpg"
SAME_SIZE_DRIFT = (
    LFW_TREE
    / "Gloria_Macapagal_Arroyo"
    / "Gloria_Macapagal_Arroyo_0033.jpg"
)


class OwnerAdmissionHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix="qualsight-hard-"))
        self.store = RunStore(self.data_dir / "store.sqlite3")
        self.admission = OwnerSuppliedAdmission(self.store, self.data_dir, "local-user:test")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_oversize_payload_is_refused_before_decode(self) -> None:
        real = REAL_PHOTO.read_bytes()
        oversized = real * (MAX_UPLOAD_BYTES // len(real) + 2)
        self.assertGreater(len(oversized), MAX_UPLOAD_BYTES)
        with self.assertRaises(OwnerSuppliedError) as caught:
            self.admission.admit_bytes(oversized, "big.jpg", "project-x")
        self.assertIn("upload cap", str(caught.exception))
        self.assertFalse((self.data_dir / "owner_supplied").exists(), "nothing may be stored on refusal")

    def test_decode_guard_refuses_when_pixels_exceed_the_cap(self) -> None:
        real = REAL_PHOTO.read_bytes()
        original = owner_supplied.MAX_DECODED_PIXELS
        owner_supplied.MAX_DECODED_PIXELS = 1000  # below the real photo's 250x250
        try:
            with self.assertRaises(OwnerSuppliedError) as caught:
                self.admission.admit_bytes(real, "real.jpg", "project-x")
            self.assertIn("decode guard", str(caught.exception))
        finally:
            owner_supplied.MAX_DECODED_PIXELS = original

    def test_hostile_filename_cannot_escape_the_storage_root(self) -> None:
        real = REAL_PHOTO.read_bytes()
        admitted = self.admission.admit_bytes(real, "../../../../etc/hostile.jpg", "project-x")
        stored = Path(admitted.path).resolve()
        self.assertTrue(stored.is_relative_to((self.data_dir / "owner_supplied").resolve()))
        self.assertEqual(stored.stem, hashlib.sha256(real).hexdigest())

    def test_same_size_drifted_content_addressed_target_is_not_referenced(self) -> None:
        admitted_bytes = SAME_SIZE_PHOTO.read_bytes()
        drifted_bytes = SAME_SIZE_DRIFT.read_bytes()
        self.assertEqual(len(admitted_bytes), len(drifted_bytes))
        self.assertNotEqual(hashlib.sha256(admitted_bytes).digest(), hashlib.sha256(drifted_bytes).digest())
        admitted = self.admission.admit_bytes(admitted_bytes, SAME_SIZE_PHOTO.name, "project-a")
        Path(admitted.path).write_bytes(drifted_bytes)

        with self.assertRaises(OwnerSuppliedError):
            self.admission.admit_bytes(admitted_bytes, SAME_SIZE_PHOTO.name, "project-b")

        self.assertEqual(Path(admitted.path).read_bytes(), drifted_bytes)
        self.assertEqual(self.store.owner_supplied_for_project("project-b"), [])

    def test_existing_symlink_at_content_address_is_refused(self) -> None:
        admitted_bytes = REAL_PHOTO.read_bytes()
        target = (
            self.data_dir
            / "owner_supplied"
            / f"{hashlib.sha256(admitted_bytes).hexdigest()}.jpg"
        )
        target.parent.mkdir(parents=True)
        target.symlink_to(REAL_PHOTO)

        with self.assertRaises(OwnerSuppliedError):
            self.admission.admit_bytes(admitted_bytes, REAL_PHOTO.name, "project-x")

        self.assertTrue(target.is_symlink())
        self.assertIsNone(self.store.owner_supplied_by_path(str(target)))

    def test_failed_real_write_never_publishes_a_partial_target_and_retry_succeeds(self) -> None:
        admitted_bytes = REAL_PHOTO.read_bytes()
        digest = hashlib.sha256(admitted_bytes).hexdigest()
        target = self.data_dir / "owner_supplied" / f"{digest}.jpg"
        child_database = self.data_dir / "write-failure.sqlite3"
        limit = len(admitted_bytes) // 2
        script = """
import resource
import signal
import sys
from pathlib import Path
from qualsight.api.store import RunStore
from qualsight.importers.owner_supplied import OwnerSuppliedAdmission

source = Path(sys.argv[1])
data_dir = Path(sys.argv[2])
store = RunStore(Path(sys.argv[3]))
try:
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    limit = int(sys.argv[4])
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    OwnerSuppliedAdmission(store, data_dir, "local-user:write-failure").admit_bytes(
        source.read_bytes(), source.name, "project-write-failure"
    )
finally:
    store.close()
"""
        failed = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(REAL_PHOTO),
                str(self.data_dir),
                str(child_database),
                str(limit),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(failed.returncode, 0, failed.stdout)
        self.assertFalse(target.exists(), failed.stderr)
        self.assertEqual(list((self.data_dir / "owner_supplied").glob("*.partial")), [])

        admitted = self.admission.admit_bytes(admitted_bytes, REAL_PHOTO.name, "project-retry")
        self.assertEqual(Path(admitted.path), target)
        self.assertEqual(target.read_bytes(), admitted_bytes)

    def test_database_reference_failure_leaves_verified_bytes_retryable(self) -> None:
        admitted_bytes = REAL_PHOTO.read_bytes()
        digest = hashlib.sha256(admitted_bytes).hexdigest()
        target = self.data_dir / "owner_supplied" / f"{digest}.jpg"
        shared = self.admission.admit_bytes(
            admitted_bytes, REAL_PHOTO.name, "project-existing-reference"
        )
        self.assertEqual(Path(shared.path), target)
        with sqlite3.connect(self.store.database_path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_owner_reference BEFORE INSERT ON owner_supplied_project_refs "
                "BEGIN SELECT RAISE(ABORT, 'owner reference persistence rejected'); END"
            )
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                self.admission.admit_bytes(admitted_bytes, REAL_PHOTO.name, "project-db-failure")
        finally:
            with sqlite3.connect(self.store.database_path) as connection:
                connection.execute("DROP TRIGGER reject_owner_reference")

        self.assertEqual(target.read_bytes(), admitted_bytes)
        self.assertEqual(self.store.owner_supplied_for_project("project-db-failure"), [])
        receipts = owner_supplied.prepared_owner_input_admissions(self.data_dir)
        self.assertEqual(len(receipts), 1)
        prepared = owner_supplied.inspect_prepared_owner_input_admission(
            receipts[0], data_dir=self.data_dir
        )
        self.assertEqual(prepared.project_id, "project-db-failure")
        self.assertTrue(
            self.store.owner_supplied_has_other_project_reference(
                str(target), "project-db-failure"
            )
        )
        retried = self.admission.admit_bytes(
            admitted_bytes, REAL_PHOTO.name, "project-db-failure"
        )
        self.assertEqual(Path(retried.path), target)
        self.assertEqual(len(self.store.owner_supplied_for_project("project-db-failure")), 1)
        self.assertEqual(
            len(
                self.store.owner_supplied_for_project(
                    "project-existing-reference"
                )
            ),
            1,
        )
        self.assertFalse(receipts[0].exists())
        self.assertEqual(target.read_bytes(), admitted_bytes)

    def test_same_path_different_bytes_is_an_immutability_conflict(self) -> None:
        from qualsight.api.store import ImmutableRecordConflict

        root = Path(tempfile.mkdtemp(prefix="qualsight-owner-src-"))
        try:
            target = root / "photo.jpg"
            shutil.copyfile(REAL_PHOTO, target)
            self.admission.admit_path(target, root, "project-x")
            shutil.copyfile(LFW_TREE / "Colin_Powell" / "Colin_Powell_0002.jpg", target)
            with self.assertRaises(ImmutableRecordConflict):
                self.admission.admit_path(target, root, "project-x")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_symlink_inside_directory_import_is_refused(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="qualsight-owner-dir-"))
        try:
            subject = root / "person_a"
            subject.mkdir()
            shutil.copyfile(REAL_PHOTO, subject / "real.jpg")
            (subject / "escape.jpg").symlink_to(LFW_TREE / "Tony_Blair" / "Tony_Blair_0001.jpg")
            importer = OwnerSuppliedImporter(self.store, self.data_dir, "local-user:test")
            with self.assertRaises(OwnerSuppliedError) as caught:
                importer._collect_directory(root)  # noqa: SLF001 - the collection guard under test
            self.assertIn("symlinks are not admitted", str(caught.exception))
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
