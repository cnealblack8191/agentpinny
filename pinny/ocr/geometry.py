"""Box mapping between the canonical page and the images the engine reads.

``rotation`` is the clockwise quarter-turn applied to an image of size
``(width, height)``. Boxes are half-open ``[x, x2) x [y, y2)`` (contracts
section 3), so every mapping here is exact and invertible.
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

import numpy as np

from .types import OcrBox, OcrError, OcrWord

Size = Tuple[int, int]  # (width, height)


def rotated_size(size: Size, rotation: int) -> Size:
    w, h = size
    return (h, w) if rotation in (90, 270) else (w, h)


def rotate_image(image: np.ndarray, rotation: int) -> np.ndarray:
    return np.ascontiguousarray(np.rot90(image, k=-(rotation // 90)))


def rotate_box(box: OcrBox, size: Size, rotation: int) -> OcrBox:
    """Where ``box`` lands after rotating its ``(width, height)`` frame clockwise."""
    w, h = size
    x0, y0, x1, y1 = box.x, box.y, box.x2, box.y2
    if rotation == 0:
        return box
    if rotation == 90:
        return OcrBox(h - y1, x0, y1 - y0, x1 - x0)
    if rotation == 180:
        return OcrBox(w - x1, h - y1, x1 - x0, y1 - y0)
    if rotation == 270:
        return OcrBox(y0, w - x1, y1 - y0, x1 - x0)
    raise OcrError("invalid_rotation", f"rotation must be 0, 90, 180 or 270, got {rotation!r}.")


def unrotate_box(box: OcrBox, size: Size, rotation: int) -> OcrBox:
    """Inverse of :func:`rotate_box`. ``size`` is the frame *before* rotation."""
    inverse = (360 - rotation) % 360
    return rotate_box(box, rotated_size(size, rotation), inverse)


def unscale_box(box: OcrBox, scale: float, size: Size) -> OcrBox:
    """Map a box from an image enlarged by ``scale`` back to ``size``, rounding
    outwards so the original text stays covered."""
    if scale == 1.0:
        return box
    w, h = size
    x0 = min(max(math.floor(box.x / scale), 0), w - 1)
    y0 = min(max(math.floor(box.y / scale), 0), h - 1)
    x1 = max(min(math.ceil(box.x2 / scale), w), x0 + 1)
    y1 = max(min(math.ceil(box.y2 / scale), h), y0 + 1)
    return OcrBox(x0, y0, x1 - x0, y1 - y0)


def overlap_ratio(a: OcrBox, b: OcrBox) -> float:
    """Intersection area over the smaller box's area."""
    iw = min(a.x2, b.x2) - max(a.x, b.x)
    ih = min(a.y2, b.y2) - max(a.y, b.y)
    if iw <= 0 or ih <= 0:
        return 0.0
    return (iw * ih) / min(a.width * a.height, b.width * b.height)


def _evidence(word: OcrWord) -> float:
    # A misread at the wrong angle tends to be short fragments; weight
    # confidence by the characters that carry text.
    return word.confidence * max(sum(c.isalnum() for c in word.text), 1)


def merge_rotations(words_by_rotation: Dict[int, Sequence[OcrWord]], order: Sequence[int], ratio: float) -> List[OcrWord]:
    """Drop words that overlap a stronger word read at another rotation.

    Words from the same rotation never suppress each other. Output keeps the
    engine's word order, rotation by rotation, in ``order``.
    """
    ranked = sorted(
        ((w, r, i) for r in order for i, w in enumerate(words_by_rotation.get(r, ()))),
        key=lambda t: (-_evidence(t[0]), order.index(t[1]), t[2]),
    )
    kept: List[Tuple[OcrWord, int, int]] = []
    for word, rot, idx in ranked:
        if all(k_rot == rot or overlap_ratio(word.box, k.box) <= ratio for k, k_rot, _ in kept):
            kept.append((word, rot, idx))
    kept.sort(key=lambda t: (order.index(t[1]), t[2]))
    return [w for w, _, _ in kept]
