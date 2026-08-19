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
