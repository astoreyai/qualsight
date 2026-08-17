"""Five-point ArcFace alignment (port of insightface ``utils/face_align.py`` @ 7fadd420, MIT).

``estimate_norm`` fits a similarity transform (Umeyama, as skimage's
``SimilarityTransform.estimate`` does) from the five detected landmarks to the
ArcFace 112×112 template and ``norm_crop`` warps with ``cv2.warpAffine``
(border value 0), exactly as upstream. The template digest is exposed so a Run
can bind the alignment it used.
"""

from __future__ import annotations

import hashlib

import cv2
import numpy as np

ARCFACE_DST = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32,
)
ARCFACE_TEMPLATE_SHA256 = hashlib.sha256(ARCFACE_DST.tobytes()).hexdigest()


def umeyama_similarity(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Least-squares similarity transform src→dst (Umeyama 1991), 3×3 homogeneous."""

    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    num, dim = src.shape
    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    src_demean = src - src_mean
    dst_demean = dst - dst_mean
    A = dst_demean.T @ src_demean / num
    d = np.ones((dim,), dtype=np.float64)
    if np.linalg.det(A) < 0:
        d[dim - 1] = -1
    T = np.eye(dim + 1, dtype=np.float64)
    U, S, V = np.linalg.svd(A)
    rank = np.linalg.matrix_rank(A)
    if rank == 0:
        return T * np.nan
    if rank == dim - 1:
        if np.linalg.det(U) * np.linalg.det(V) > 0:
            T[:dim, :dim] = U @ V
        else:
            s = d[dim - 1]
            d[dim - 1] = -1
            T[:dim, :dim] = U @ np.diag(d) @ V
            d[dim - 1] = s
    else:
        T[:dim, :dim] = U @ np.diag(d) @ V
    scale = 1.0 / src_demean.var(axis=0).sum() * (S @ d)
    T[:dim, dim] = dst_mean - scale * (T[:dim, :dim] @ src_mean.T)
    T[:dim, :dim] *= scale
    return T


def estimate_norm(landmarks: np.ndarray, image_size: int = 112) -> np.ndarray:
    if landmarks.shape != (5, 2):
        raise ValueError("five landmarks are required")
    if image_size % 112 == 0:
        ratio = float(image_size) / 112.0
        diff_x = 0.0
    elif image_size % 128 == 0:
        ratio = float(image_size) / 128.0
        diff_x = 8.0 * ratio
    else:
        raise ValueError("image_size must be a multiple of 112 or 128")
    dst = ARCFACE_DST * ratio
    dst[:, 0] += diff_x
    T = umeyama_similarity(landmarks, dst)
    return T[0:2, :]


def norm_crop(image_bgr: np.ndarray, landmarks: np.ndarray, image_size: int = 112) -> np.ndarray:
    M = estimate_norm(landmarks, image_size)
    return cv2.warpAffine(image_bgr, M, (image_size, image_size), borderValue=0.0)
