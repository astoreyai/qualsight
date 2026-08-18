"""Deterministic, versioned image transformation operators (PRD §27; QS-X61).

Every operator maps a real BGR image and one recorded parameter to a derived
image plus the exact encoding used to store it. No operator invents content:
JPEG re-encoding, Gaussian blur, resolution reduction (downscale then
upscale back to the original size), gamma, contrast, rotation, and a
deterministic occlusion rectangle are all functions of the input bytes and
the parameter. Operators are identified by ``kind`` and ``version`` so a
derived sample's chain can be re-executed byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

OPERATOR_VERSION = "1.0.0"


class TransformError(ValueError):
    """Raised for an unknown operator or an out-of-range parameter."""


@dataclass(frozen=True, slots=True)
class Encoded:
    image_bgr: np.ndarray
    extension: str  # ".jpg" | ".png"
    encode_parameters: tuple[int, ...]
    parameters: dict[str, Any]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TransformError(message)


def jpeg(image: np.ndarray, quality: float) -> Encoded:
    q = int(quality)
    _require(1 <= q <= 100 and q == quality, "jpeg quality must be an integer 1–100")
    return Encoded(image, ".jpg", (int(cv2.IMWRITE_JPEG_QUALITY), q), {"quality": q})


def gaussian_blur(image: np.ndarray, sigma: float) -> Encoded:
    _require(0 < float(sigma) <= 20, "gaussian blur sigma must be in (0, 20]")
    kernel = int(2 * round(3 * float(sigma)) + 1)
    return Encoded(cv2.GaussianBlur(image, (kernel, kernel), sigmaX=float(sigma), sigmaY=float(sigma), borderType=cv2.BORDER_REFLECT_101), ".png", (int(cv2.IMWRITE_PNG_COMPRESSION), 3), {"sigma": float(sigma), "kernel": kernel})


def downscale(image: np.ndarray, factor: float) -> Encoded:
    _require(0 < float(factor) < 1, "downscale factor must be in (0, 1)")
    height, width = image.shape[:2]
    small = cv2.resize(image, (max(1, int(round(width * factor))), max(1, int(round(height * factor)))), interpolation=cv2.INTER_AREA)
    restored = cv2.resize(small, (width, height), interpolation=cv2.INTER_LINEAR)
    return Encoded(restored, ".png", (int(cv2.IMWRITE_PNG_COMPRESSION), 3), {"factor": float(factor), "intermediate_size": [int(small.shape[1]), int(small.shape[0])], "restored_size": [width, height]})


def gamma(image: np.ndarray, value: float) -> Encoded:
    _require(0.1 <= float(value) <= 10, "gamma must be in [0.1, 10]")
    table = np.array([((i / 255.0) ** (1.0 / float(value))) * 255 for i in range(256)]).astype(np.uint8)
    return Encoded(cv2.LUT(image, table), ".png", (int(cv2.IMWRITE_PNG_COMPRESSION), 3), {"gamma": float(value)})


def contrast(image: np.ndarray, alpha: float) -> Encoded:
    _require(0.05 <= float(alpha) <= 5, "contrast alpha must be in [0.05, 5]")
    return Encoded(cv2.convertScaleAbs(image, alpha=float(alpha), beta=128 * (1 - float(alpha))), ".png", (int(cv2.IMWRITE_PNG_COMPRESSION), 3), {"alpha": float(alpha)})


def rotation(image: np.ndarray, degrees: float) -> Encoded:
    _require(-180 <= float(degrees) <= 180, "rotation must be within ±180 degrees")
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), float(degrees), 1.0)
    return Encoded(cv2.warpAffine(image, matrix, (width, height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101), ".png", (int(cv2.IMWRITE_PNG_COMPRESSION), 3), {"degrees": float(degrees)})


def occlusion(image: np.ndarray, fraction: float) -> Encoded:
    """Black rectangle covering ``fraction`` of the image area, anchored at the lower centre (mouth/chin region for portrait framing)."""

    _require(0 < float(fraction) < 1, "occlusion fraction must be in (0, 1)")
    height, width = image.shape[:2]
    area = float(fraction) * height * width
    box_w = int(round(min(width, np.sqrt(area * width / height))))
    box_h = int(round(min(height, area / max(box_w, 1))))
    x0 = (width - box_w) // 2
    y0 = height - box_h - int(0.1 * height)
    y0 = max(0, y0)
    out = image.copy()
    out[y0 : y0 + box_h, x0 : x0 + box_w] = 0
    return Encoded(out, ".png", (int(cv2.IMWRITE_PNG_COMPRESSION), 3), {"fraction": float(fraction), "box": [x0, y0, box_w, box_h]})


OPERATORS = {"jpeg": jpeg, "gaussian_blur": gaussian_blur, "downscale": downscale, "gamma": gamma, "contrast": contrast, "rotation": rotation, "occlusion": occlusion}
PARAMETER_NAMES = {"jpeg": "quality", "gaussian_blur": "sigma", "downscale": "factor", "gamma": "gamma", "contrast": "alpha", "rotation": "degrees", "occlusion": "fraction"}


def apply(kind: str, image_bgr: np.ndarray, parameter: float) -> Encoded:
    operator = OPERATORS.get(kind)
    if operator is None:
        raise TransformError(f"unknown transform {kind}")
    if image_bgr is None or image_bgr.ndim != 3 or image_bgr.dtype != np.uint8:
        raise TransformError("transforms expect a BGR uint8 image")
    return operator(image_bgr, parameter)


def encode(result: Encoded) -> bytes:
    ok, buffer = cv2.imencode(result.extension, result.image_bgr, list(result.encode_parameters))
    if not ok:
        raise TransformError("image encoding failed")
    return buffer.tobytes()
