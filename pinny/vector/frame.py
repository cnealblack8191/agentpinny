"""Mapping between PDF user space and the canonical raster frame.

Contracts section 2: canonical raster pixels are the page rendered at 200 DPI
*after* ``/Rotate``, origin top-left, ``y`` down, ``px = pt * 200 / 72``.

The visible page area is the CropBox (defaulting to the MediaBox, clipped to
it). Its lower-left corner may be anywhere in user space, so the mapping
first moves the CropBox's top-left corner to the origin and flips ``y``,
then applies the ``/Rotate`` clockwise quarter-turn, then scales.

Matrices use the PDF row-vector convention: ``[x y 1] @ M`` with
``M = [[a, b, 0], [c, d, 0], [e, f, 1]]``, so ``A @ B`` means "A, then B".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Tuple

import numpy as np

CANONICAL_DPI = 200
PT_TO_PX = CANONICAL_DPI / 72.0


def pdf_matrix(a: float, b: float, c: float, d: float, e: float, f: float) -> np.ndarray:
    """3x3 row-vector matrix for the PDF operand array ``[a b c d e f]``."""
    return np.array([[a, b, 0.0], [c, d, 0.0], [e, f, 1.0]], dtype=float)


def apply(m: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Apply a row-vector matrix to an ``(..., 2)`` array of points."""
    pts = np.asarray(pts, dtype=float)
    return pts @ m[:2, :2] + m[2, :2]


@dataclass(frozen=True)
class PageFrame:
    """Geometry of one page and its canonical raster.

    ``crop`` is ``(x0, y0, x1, y1)`` in PDF user space (points, y up).
    """

    crop: Tuple[float, float, float, float]
    rotate: int

    @property
    def crop_width_pt(self) -> float:
        return self.crop[2] - self.crop[0]

    @property
    def crop_height_pt(self) -> float:
        return self.crop[3] - self.crop[1]

    @property
    def width_pt(self) -> float:
        """Displayed (post-rotation) width in points."""
        return self.crop_height_pt if self.rotate in (90, 270) else self.crop_width_pt

    @property
    def height_pt(self) -> float:
        return self.crop_width_pt if self.rotate in (90, 270) else self.crop_height_pt

    @property
    def width_px(self) -> int:
        return int(math.ceil(self.width_pt * PT_TO_PX - 1e-9))

    @property
    def height_px(self) -> int:
        return int(math.ceil(self.height_pt * PT_TO_PX - 1e-9))

    @property
    def user_to_px(self) -> np.ndarray:
        """Row-vector matrix from PDF user space to canonical raster px."""
        x0, _y0, _x1, y1 = self.crop
        w, h = self.crop_width_pt, self.crop_height_pt
        # Top-left of the CropBox to the origin, y down (still in points).
        to_topdown = pdf_matrix(1, 0, 0, -1, -x0, y1)
        # Clockwise quarter-turn in the y-down frame; the page keeps its
        # top-left corner at the origin afterwards.
        if self.rotate == 0:
            rot = pdf_matrix(1, 0, 0, 1, 0, 0)
        elif self.rotate == 90:  # (u, v) -> (H - v, u)
            rot = pdf_matrix(0, 1, -1, 0, h, 0)
        elif self.rotate == 180:  # (u, v) -> (W - u, H - v)
            rot = pdf_matrix(-1, 0, 0, -1, w, h)
        else:  # 270: (u, v) -> (v, W - u)
            rot = pdf_matrix(0, -1, 1, 0, 0, w)
        scale = pdf_matrix(PT_TO_PX, 0, 0, PT_TO_PX, 0, 0)
        return to_topdown @ rot @ scale

    @property
    def px_to_user(self) -> np.ndarray:
        return np.linalg.inv(self.user_to_px)

    def to_px(self, pts: Iterable) -> np.ndarray:
        """PDF user-space points ``(..., 2)`` to canonical px."""
        return apply(self.user_to_px, np.asarray(pts, dtype=float))

    def to_user(self, pts: Iterable) -> np.ndarray:
        """Canonical px points ``(..., 2)`` to PDF user space."""
        return apply(self.px_to_user, np.asarray(pts, dtype=float))

    def descriptor(self) -> dict:
        """The coordinate-frame descriptor from contracts section 2."""
        return {
            "space": "canonical_raster_px",
            "dpi": CANONICAL_DPI,
            "width": self.width_px,
            "height": self.height_px,
            "origin": "top-left",
            "y_axis": "down",
        }


def normalize_rotate(value: object) -> int:
    """``/Rotate`` to 0/90/180/270. Negative multiples of 90 are allowed by the
    spec; anything else is treated as 0 (what viewers do)."""
    try:
        r = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    if r % 90 != 0:
        return 0
    return r % 360


def frame_from_boxes(mediabox, cropbox, rotate) -> PageFrame:
    """Build a frame from raw box arrays (any corner order) and ``/Rotate``."""

    def norm(box) -> Tuple[float, float, float, float]:
        a, b, c, d = (float(v) for v in box)
        return (min(a, c), min(b, d), max(a, c), max(b, d))

    mb = norm(mediabox)
    cb = norm(cropbox) if cropbox is not None else mb
    crop = (max(mb[0], cb[0]), max(mb[1], cb[1]), min(mb[2], cb[2]), min(mb[3], cb[3]))
    if crop[2] <= crop[0] or crop[3] <= crop[1]:
        crop = mb  # CropBox outside MediaBox: viewers fall back to the MediaBox.
    return PageFrame(crop=crop, rotate=normalize_rotate(rotate))
