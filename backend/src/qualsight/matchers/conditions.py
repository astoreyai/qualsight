"""The closed preprocessing conditions of the matcher matrix.

Two conditions start from a registered BGR uint8 source image, run the registered
SCRFD-10G at detection threshold 0.5, and align with insightface's five-point
``norm_crop`` to 112×112 (ArcFace template). They differ only in detector
canvas, face selection, and crop transport:

* ``SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1`` — canvas 128 when the
  maximum source dimension is ≤160, 256 when ≤320, else 640; largest
  bounding-box area; crop encoded as JPEG quality 95 then decoded to BGR;
* ``SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1`` — fixed 640
  canvas; highest detector confidence; in-memory BGR crop, no JPEG round trip.

A missing detection is an explicit ``FAILURE_TO_DETECT``; nothing is
substituted. A third condition is restricted to author-published, hash-verified
112x112 prealigned pair images and performs no detection or realignment.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib

import cv2
import numpy as np

from .alignment import ARCFACE_TEMPLATE_SHA256, norm_crop
from .detection import Detection, SCRFDDetector


class ConditionId(StrEnum):
    ADAPTIVE_LARGEST_JPEG95 = "SCRFD10G_ADAPTIVE_LARGEST_ARCFACE112_JPEG95_V1"
    FIXED640_HIGHEST_INMEMORY = "SCRFD10G_640_HIGHEST_CONFIDENCE_ARCFACE112_INMEMORY_V1"
    PREALIGNED_ARCFACE112 = "AUTHOR_PREALIGNED_ARCFACE112_INMEMORY_V1"


SCRFD_CONDITIONS = (
    ConditionId.ADAPTIVE_LARGEST_JPEG95,
    ConditionId.FIXED640_HIGHEST_INMEMORY,
)
PREALIGNED_CONDITIONS = (ConditionId.PREALIGNED_ARCFACE112,)


@dataclass(frozen=True, slots=True)
class CropResult:
    condition_id: ConditionId
    crop_bgr: np.ndarray | None  # 112×112×3 uint8 BGR
    detection: Detection | None
    canvas: int
    face_count: int | None
    failure: str | None  # FAILURE_TO_DETECT when no face passes the threshold
    crop_sha256: str | None


CONDITION_DEFINITIONS: dict[ConditionId, dict[str, object]] = {
    ConditionId.ADAPTIVE_LARGEST_JPEG95: {
        "detector": "SCRFD-10G (registered det_10g.onnx), threshold 0.5, NMS 0.4",
        "canvas": "128 if max(source dimension) <= 160; 256 if <= 320; otherwise 640",
        "selection": "largest bounding-box area",
        "alignment": "insightface five-point norm_crop, ArcFace template, 112x112",
        "alignment_template_sha256": ARCFACE_TEMPLATE_SHA256,
        "transport": "JPEG quality 95 encode followed by BGR decode",
    },
    ConditionId.FIXED640_HIGHEST_INMEMORY: {
        "detector": "SCRFD-10G (registered det_10g.onnx), threshold 0.5, NMS 0.4",
        "canvas": "fixed 640x640",
        "selection": "highest detector confidence",
        "alignment": "insightface five-point norm_crop, ArcFace template, 112x112",
        "alignment_template_sha256": ARCFACE_TEMPLATE_SHA256,
        "transport": "in-memory BGR crop, no JPEG round trip",
    },
    ConditionId.PREALIGNED_ARCFACE112: {
        "detector": "not applicable; the publisher supplies a pre-aligned 112x112 image",
        "canvas": "native 112x112",
        "selection": "not applicable; exactly one published image is the comparison input",
        "alignment": "no realignment; requires decoded BGR uint8 shape 112x112x3",
        "alignment_template_sha256": "not applicable; no alignment is executed",
        "transport": "in-memory BGR pixels decoded from the hash-verified publisher JPEG",
    },
}


def adaptive_canvas(image_bgr: np.ndarray) -> int:
    largest = max(image_bgr.shape[0], image_bgr.shape[1])
    if largest <= 160:
        return 128
    if largest <= 320:
        return 256
    return 640


def preprocess(
    image_bgr: np.ndarray,
    condition: ConditionId,
    detector: SCRFDDetector | None,
) -> CropResult:
    if image_bgr is None or image_bgr.ndim != 3 or image_bgr.shape[2] != 3 or image_bgr.dtype != np.uint8:
        raise ValueError("preprocessing expects a BGR uint8 image")
    if condition is ConditionId.PREALIGNED_ARCFACE112:
        if image_bgr.shape != (112, 112, 3):
            return CropResult(
                condition,
                None,
                None,
                112,
                None,
                "FAILURE_TO_ACQUIRE",
                None,
            )
        crop = np.ascontiguousarray(image_bgr)
        digest = hashlib.sha256(crop.tobytes()).hexdigest()
        return CropResult(condition, crop, None, 112, None, None, digest)
    if detector is None:
        raise ValueError("SCRFD preprocessing requires the registered detector")
    if condition is ConditionId.ADAPTIVE_LARGEST_JPEG95:
        canvas = adaptive_canvas(image_bgr)
    else:
        canvas = 640
    detections = detector.detect(image_bgr, canvas)
    if not detections:
        return CropResult(condition, None, None, canvas, 0, "FAILURE_TO_DETECT", None)
    if condition is ConditionId.ADAPTIVE_LARGEST_JPEG95:
        chosen = max(detections, key=lambda item: item.area)
    else:
        chosen = max(detections, key=lambda item: item.score)
    crop = norm_crop(image_bgr, chosen.landmarks, 112)
    if condition is ConditionId.ADAPTIVE_LARGEST_JPEG95:
        ok, encoded = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if not ok:
            raise RuntimeError("JPEG encoding of the aligned crop failed")
        crop = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if crop is None or crop.shape != (112, 112, 3):
        raise RuntimeError("aligned crop is not 112x112x3")
    digest = hashlib.sha256(np.ascontiguousarray(crop).tobytes()).hexdigest()
    return CropResult(condition, crop, chosen, canvas, len(detections), None, digest)
