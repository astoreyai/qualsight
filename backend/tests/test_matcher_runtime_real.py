"""Frozen matcher runtime on real registered bytes (QS-C22 preparation).

Loads the six registered checkpoints strictly into their pinned upstream
architectures, runs the registered SCRFD-10G detector under both frozen
preprocessing conditions on the real LFW subset fixture, and checks the
embedding contract and pair-score sanity on the fixture's real View 2 pairs.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

import cv2
import numpy as np

from qualsight.execution.registry_binding import load_resource_registry
from qualsight.matchers.alignment import ARCFACE_DST, ARCFACE_TEMPLATE_SHA256, umeyama_similarity
from qualsight.matchers.conditions import (
    CONDITION_DEFINITIONS,
    SCRFD_CONDITIONS,
    ConditionId,
    adaptive_canvas,
    preprocess,
)
from qualsight.matchers.detection import SCRFDDetector
from qualsight.matchers.runtime import FrozenMatcher, MatcherId, MatcherRuntimeError, cosine_similarity
from qualsight.matchers.upstream import UpstreamSourceError, UpstreamSources


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "resources" / "manifests" / "lfw-subset-fixture-v1.json"
TREE = Path("/mnt/projects/datasets/lfw/lfw_funneled")


class RealMatcherRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_resource_registry(ROOT / "resources" / "resource-registry.json")
        cls.upstream = UpstreamSources(ROOT / "resources" / "manifests" / "matcher-upstream-sources-v1.json", ROOT / "var")
        cls.upstream.acquire()
        scrfd = cls.registry.resource("insightface-scrfd-10g-local-candidate")
        model = next(item.identity for item in scrfd.artifacts if item.role == "MODEL")
        cls.detector = SCRFDDetector(Path(model.path), model.sha256)
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.images = [cv2.imread(str(TREE / image["path"]), cv2.IMREAD_COLOR) for image in cls.fixture["images"]]
        cls.index = {image["path"]: position for position, image in enumerate(cls.fixture["images"])}
        cls.crops = {}
        for condition in SCRFD_CONDITIONS:
            results = [preprocess(image, condition, cls.detector) for image in cls.images]
            cls.crops[condition] = results

    def test_pinned_upstream_sources_are_verified_before_import(self) -> None:
        for key, source in self.upstream.sources.items():
            path = self.upstream.verify(key)
            self.assertTrue(path.is_file(), key)
            self.assertEqual(len(source.sha256), 64)
        with self.assertRaises(KeyError):
            self.upstream.verify("not-a-pinned-source")
        tampered = self.upstream.path_for("adaface_net")
        original = tampered.read_bytes()
        try:
            # One-byte-short subsample of the real pinned source; no fabricated test bytes.
            tampered.write_bytes(original[:-1])
            with self.assertRaises(UpstreamSourceError):
                self.upstream.verify("adaface_net")
        finally:
            tampered.write_bytes(original)

    def test_executable_matcher_catalog_is_the_six_registered_real_checkpoints(self) -> None:
        self.assertEqual(
            {matcher_id.value for matcher_id in MatcherId},
            {
                "adaface-ir101-local-candidate",
                "elasticface-arc-r100-local-candidate",
                "magface-iresnet100-ms1mv2-local-candidate",
                "topofr-r100-glint360k-local-candidate",
                "transface-b-glint360k-local-candidate",
                "transface-s-glint360k-local-candidate",
            },
        )

    def test_alignment_matches_the_arcface_template_geometry(self) -> None:
        # Mapping the template onto itself is the identity similarity.
        T = umeyama_similarity(ARCFACE_DST, ARCFACE_DST)
        self.assertTrue(np.allclose(T, np.eye(3), atol=1e-6))
        self.assertEqual(len(ARCFACE_TEMPLATE_SHA256), 64)
        self.assertEqual(adaptive_canvas(self.images[0]), 256)  # LFW funneled is 250×250

    def test_both_scrfd_conditions_detect_every_fixture_face_and_yield_112_crops(self) -> None:
        for condition, results in self.crops.items():
            with self.subTest(condition=condition.value):
                self.assertEqual(len(results), len(self.images))
                for result in results:
                    self.assertIsNone(result.failure, result)
                    self.assertEqual(result.crop_bgr.shape, (112, 112, 3))
                    self.assertEqual(result.crop_bgr.dtype, np.uint8)
                    self.assertGreaterEqual(result.face_count, 1)
                    self.assertGreaterEqual(result.detection.score, 0.5)
                self.assertEqual({r.canvas for r in results}, {256} if condition is ConditionId.ADAPTIVE_LARGEST_JPEG95 else {640})
        # The two conditions are different transports: JPEG-95 crops differ from in-memory crops.
        adaptive = self.crops[ConditionId.ADAPTIVE_LARGEST_JPEG95][0].crop_bgr
        fixed = self.crops[ConditionId.FIXED640_HIGHEST_INMEMORY][0].crop_bgr
        self.assertFalse(np.array_equal(adaptive, fixed))
        self.assertIn("JPEG quality 95", CONDITION_DEFINITIONS[ConditionId.ADAPTIVE_LARGEST_JPEG95]["transport"])

    def test_each_matcher_loads_strictly_and_separates_real_mated_from_nonmated_pairs(self) -> None:
        for matcher_id in MatcherId:
            with self.subTest(matcher=matcher_id.value):
                matcher = FrozenMatcher(matcher_id, self.registry, self.upstream)
                binding = matcher.binding
                self.assertGreater(binding.parameter_count, 60_000_000)
                self.assertEqual(len(binding.checkpoint_sha256), 64)
                for condition, results in self.crops.items():
                    crops = np.stack([r.crop_bgr for r in results])
                    embeddings = matcher.embed(crops)
                    self.assertEqual(embeddings.shape, (len(self.images), 512))
                    self.assertTrue(np.allclose(np.linalg.norm(embeddings, axis=1), 1.0, atol=1e-5))
                    mated, nonmated = [], []
                    for pair in self.fixture["pairs"]:
                        left = embeddings[[self.index[pair["left_path"]]]]
                        right = embeddings[[self.index[pair["right_path"]]]]
                        score = float(cosine_similarity(left, right)[0])
                        (mated if pair["label"] == "MATED" else nonmated).append(score)
                    self.assertGreater(min(mated), max(nonmated), (matcher_id.value, condition.value, mated, nonmated))
                # Repeated embedding of the same bytes is deterministic on this device.
                crops = np.stack([r.crop_bgr for r in self.crops[ConditionId.FIXED640_HIGHEST_INMEMORY]])
                self.assertTrue(np.allclose(matcher.embed(crops), matcher.embed(crops), atol=1e-6))

    def test_wrong_input_shape_is_refused(self) -> None:
        matcher = FrozenMatcher(MatcherId.ELASTICFACE_ARC_R100, self.registry, self.upstream)
        real_crop = self.crops[ConditionId.FIXED640_HIGHEST_INMEMORY][0].crop_bgr
        malformed_real_crop = real_crop[:100, :100][None, ...]
        with self.assertRaises(MatcherRuntimeError):
            matcher.embed(malformed_real_crop)


if __name__ == "__main__":
    unittest.main()
