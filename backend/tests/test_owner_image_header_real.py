from __future__ import annotations

import unittest
from pathlib import Path

import cv2

from qualsight.importers.owner_supplied import _encoded_dimensions


ROOT = Path(__file__).resolve().parents[2]
REAL_IMAGES = (
    Path("/mnt/projects/datasets/lfw/lfw_funneled/Aaron_Eckhart/Aaron_Eckhart_0001.jpg"),
    Path("/mnt/projects/02_perception_biometrics/OFIQ-Project/data/tests/images/r-01-frontal.png"),
)


class OwnerImageHeaderTests(unittest.TestCase):
    def test_header_dimensions_match_complete_decode_for_real_registered_images(self) -> None:
        for path in REAL_IMAGES:
            self.assertTrue(path.is_file(), path)
            data = path.read_bytes()
            decoded = cv2.imread(str(path), cv2.IMREAD_COLOR)
            self.assertIsNotNone(decoded, path)
            with self.subTest(path=path):
                self.assertEqual(_encoded_dimensions(data, path.name), (decoded.shape[1], decoded.shape[0]))


if __name__ == "__main__":
    unittest.main()
