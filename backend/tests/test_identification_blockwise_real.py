"""Exact blockwise identification ranking over embeddings from registered real LFW photos."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

import cv2
import numpy as np
import torch

from qualsight.execution.registry_binding import load_resource_registry
from qualsight.identification.search import IdentificationSearchJob
from qualsight.matchers.conditions import ConditionId, preprocess
from qualsight.matchers.detection import SCRFDDetector
from qualsight.matchers.matrix import SCRFD_RESOURCE_ID
from qualsight.matchers.runtime import FrozenMatcher, MatcherId
from qualsight.matchers.upstream import UpstreamSources


ROOT = Path(__file__).resolve().parents[2]


class IdentificationBlockwiseRealTests(unittest.TestCase):
    def test_blockwise_topk_matches_full_stable_sort_on_real_embeddings(self) -> None:
        registry = load_resource_registry(ROOT / "resources" / "resource-registry.json")
        detector_identity = registry.resource(SCRFD_RESOURCE_ID).artifact("MODEL").identity
        detector = SCRFDDetector(Path(detector_identity.path), detector_identity.sha256)
        fixture = json.loads(
            (ROOT / "resources" / "manifests" / "lfw-subset-fixture-v1.json").read_text(
                encoding="utf-8"
            )
        )
        lfw_root = Path("/mnt/projects/datasets/lfw/lfw_funneled")
        crops: list[np.ndarray] = []
        for item in fixture["images"][:8]:
            path = lfw_root / item["path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            result = preprocess(image, ConditionId.ADAPTIVE_LARGEST_JPEG95, detector)
            self.assertIsNone(result.failure)
            crops.append(result.crop_bgr)

        upstream = UpstreamSources(
            ROOT / "resources" / "manifests" / "matcher-upstream-sources-v1.json",
            ROOT / "var",
        )
        upstream.acquire()
        matcher = FrozenMatcher(MatcherId.ADAFACE_IR101, registry, upstream)
        embeddings = matcher.embed(np.stack(crops))
        probes = torch.from_numpy(embeddings[:3]).to(matcher.device)
        gallery = torch.from_numpy(embeddings[3:]).to(matcher.device)
        full_scores = probes @ gallery.T
        expected_values, expected_order = torch.sort(
            full_scores, descending=True, stable=True, dim=1
        )
        actual = IdentificationSearchJob._topk_exact(
            probes,
            gallery,
            list(range(gallery.shape[0])),
            rank_depth=3,
            probe_block_size=2,
            gallery_block_size=2,
        )
        for probe_row in range(probes.shape[0]):
            values, positions = actual[probe_row]
            np.testing.assert_allclose(values, expected_values[probe_row, :3].cpu().numpy(), rtol=0, atol=1e-7)
            self.assertEqual(positions.tolist(), expected_order[probe_row, :3].cpu().numpy().tolist())


if __name__ == "__main__":
    unittest.main()
