"""Duplicate suppression shared by detector implementations."""

from __future__ import annotations

import numpy as np


def suppress_duplicates(
    boxes: np.ndarray,
    scores: np.ndarray,
    iou_threshold: float,
    center_ratio: float,
    limit: int,
) -> np.ndarray:
    """Greedy non-maximum suppression; returns kept indices, best first.

    ``boxes`` is an (N, 4) array of ``x, y, width, height``. A lower-scored
    box is a duplicate of a kept one if their IoU exceeds ``iou_threshold`` or
    their centers are closer than ``center_ratio`` times the smaller of the
    two boxes' short sides. Boxes of any rotation are compared together.
    Ties are broken by position (top-to-bottom, left-to-right) for
    determinism. At most ``limit`` indices are returned.
    """
    n = len(scores)
    if n == 0:
        return np.empty(0, dtype=np.intp)

    boxes = boxes.astype(np.float64)
    x1, y1, w, h = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    x2, y2 = x1 + w, y1 + h
    cx, cy = x1 + w / 2.0, y1 + h / 2.0
    area = w * h
    short = np.minimum(w, h)

    order = np.lexsort((x1, y1, -scores))
    suppressed = np.zeros(n, dtype=bool)
    kept = []
    for i in order:
        if suppressed[i]:
            continue
        kept.append(i)
        if len(kept) >= limit:
            break
        iw = np.clip(np.minimum(x2[i], x2) - np.maximum(x1[i], x1), 0.0, None)
        ih = np.clip(np.minimum(y2[i], y2) - np.maximum(y1[i], y1), 0.0, None)
        inter = iw * ih
        iou = inter / (area[i] + area - inter)
        dist = np.hypot(cx - cx[i], cy - cy[i])
        near = dist < center_ratio * np.minimum(short[i], short)
        suppressed |= (iou > iou_threshold) | near
    return np.asarray(kept, dtype=np.intp)
