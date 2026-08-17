"""SCRFD face detection on the registered ``det_10g.onnx`` (port of insightface's scrfd.py).

Ported from deepinsight/insightface ``python-package/insightface/model_zoo/scrfd.py``
at commit 7fadd420 (MIT). The registered detector is loaded directly by path
after its SHA-256 is verified against the registry — no model pack resolution,
no download. Decoding (anchor centres, distance2bbox/kps, NMS 0.4) follows the
upstream implementation exactly; the detection canvas is an explicit argument
so that each preprocessing condition states its own sizing rule.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

import cv2
import numpy as np
import onnxruntime


@dataclass(frozen=True, slots=True)
class Detection:
    bbox: np.ndarray  # x1, y1, x2, y2 (float32, source-image coordinates)
    score: float
    landmarks: np.ndarray  # (5, 2) float32

    @property
    def area(self) -> float:
        return float(max(0.0, self.bbox[2] - self.bbox[0]) * max(0.0, self.bbox[3] - self.bbox[1]))


def _distance2bbox(points: np.ndarray, distance: np.ndarray) -> np.ndarray:
    x1 = points[:, 0] - distance[:, 0]
    y1 = points[:, 1] - distance[:, 1]
    x2 = points[:, 0] + distance[:, 2]
    y2 = points[:, 1] + distance[:, 3]
    return np.stack([x1, y1, x2, y2], axis=-1)


def _distance2kps(points: np.ndarray, distance: np.ndarray) -> np.ndarray:
    preds = []
    for i in range(0, distance.shape[1], 2):
        px = points[:, i % 2] + distance[:, i]
        py = points[:, i % 2 + 1] + distance[:, i + 1]
        preds.append(px)
        preds.append(py)
    return np.stack(preds, axis=-1)


def _nms(dets: np.ndarray, thresh: float) -> list[int]:
    x1, y1, x2, y2, scores = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        inds = np.where(ovr <= thresh)[0]
        order = order[inds + 1]
    return keep


class SCRFDDetector:
    """SCRFD-10G with five-point landmarks; fmc=3, strides 8/16/32, two anchors."""

    def __init__(self, model_path: Path, expected_sha256: str, *, det_thresh: float = 0.5, nms_thresh: float = 0.4, providers: tuple[str, ...] = ("CPUExecutionProvider",)) -> None:
        data = model_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha256:
            raise ValueError(f"detector bytes {digest} do not match the registered identity {expected_sha256}")
        self.model_path = model_path
        self.sha256 = digest
        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 4
        # The exported graph declares 640-canvas output shapes; smaller canvases
        # are valid dynamic inputs and only trigger informational shape warnings.
        options.log_severity_level = 3
        self.session = onnxruntime.InferenceSession(data, options, providers=list(providers))
        inputs = self.session.get_inputs()[0]
        self.input_name = inputs.name
        outputs = self.session.get_outputs()
        self.output_names = [item.name for item in outputs]
        self.batched = len(outputs[0].shape) == 3
        if len(outputs) != 9:
            raise ValueError("registered detector does not expose the SCRFD 9-output (scores/bboxes/kps) layout")
        self.fmc = 3
        self.feat_stride_fpn = (8, 16, 32)
        self.num_anchors = 2
        self.det_thresh = det_thresh
        self.nms_thresh = nms_thresh
        self.input_mean = 127.5
        self.input_std = 128.0
        self._center_cache: dict[tuple[int, int, int], np.ndarray] = {}

    def _forward(self, det_img: np.ndarray, threshold: float) -> tuple[list[np.ndarray], list[np.ndarray], list[np.ndarray]]:
        input_size = tuple(det_img.shape[0:2][::-1])
        blob = cv2.dnn.blobFromImage(det_img, 1.0 / self.input_std, input_size, (self.input_mean, self.input_mean, self.input_mean), swapRB=True)
        net_outs = self.session.run(self.output_names, {self.input_name: blob})
        input_height, input_width = blob.shape[2], blob.shape[3]
        scores_list: list[np.ndarray] = []
        bboxes_list: list[np.ndarray] = []
        kpss_list: list[np.ndarray] = []
        for idx, stride in enumerate(self.feat_stride_fpn):
            if self.batched:
                scores = net_outs[idx][0]
                bbox_preds = net_outs[idx + self.fmc][0] * stride
                kps_preds = net_outs[idx + self.fmc * 2][0] * stride
            else:
                scores = net_outs[idx]
                bbox_preds = net_outs[idx + self.fmc] * stride
                kps_preds = net_outs[idx + self.fmc * 2] * stride
            height = input_height // stride
            width = input_width // stride
            key = (height, width, stride)
            anchor_centers = self._center_cache.get(key)
            if anchor_centers is None:
                anchor_centers = np.stack(np.mgrid[:height, :width][::-1], axis=-1).astype(np.float32)
                anchor_centers = (anchor_centers * stride).reshape((-1, 2))
                if self.num_anchors > 1:
                    anchor_centers = np.stack([anchor_centers] * self.num_anchors, axis=1).reshape((-1, 2))
                if len(self._center_cache) < 100:
                    self._center_cache[key] = anchor_centers
            pos_inds = np.where(scores >= threshold)[0]
            bboxes = _distance2bbox(anchor_centers, bbox_preds)
            scores_list.append(scores[pos_inds])
            bboxes_list.append(bboxes[pos_inds])
            kpss = _distance2kps(anchor_centers, kps_preds).reshape((-1, 5, 2))
            kpss_list.append(kpss[pos_inds])
        return scores_list, bboxes_list, kpss_list

    def detect(self, image_bgr: np.ndarray, canvas: int) -> list[Detection]:
        """Detect faces on a square ``canvas``×``canvas`` letterboxed input (upstream sizing)."""

        input_size = (canvas, canvas)
        im_ratio = float(image_bgr.shape[0]) / image_bgr.shape[1]
        model_ratio = float(input_size[1]) / input_size[0]
        if im_ratio > model_ratio:
            new_height = input_size[1]
            new_width = int(new_height / im_ratio)
        else:
            new_width = input_size[0]
            new_height = int(new_width * im_ratio)
        det_scale = float(new_height) / image_bgr.shape[0]
        resized = cv2.resize(image_bgr, (new_width, new_height))
        det_img = np.zeros((input_size[1], input_size[0], 3), dtype=np.uint8)
        det_img[:new_height, :new_width, :] = resized
        scores_list, bboxes_list, kpss_list = self._forward(det_img, self.det_thresh)
        if sum(score.size for score in scores_list) == 0:
            return []
        scores = np.vstack([s.reshape(-1, 1) for s in scores_list])
        order = scores.ravel().argsort()[::-1]
        bboxes = np.vstack(bboxes_list) / det_scale
        kpss = np.vstack(kpss_list) / det_scale
        pre_det = np.hstack((bboxes, scores)).astype(np.float32, copy=False)[order, :]
        kpss = kpss[order, :, :]
        keep = _nms(pre_det, self.nms_thresh)
        det = pre_det[keep, :]
        kpss = kpss[keep, :, :]
        return [Detection(bbox=det[i, :4].copy(), score=float(det[i, 4]), landmarks=kpss[i].astype(np.float32).copy()) for i in range(det.shape[0])]
