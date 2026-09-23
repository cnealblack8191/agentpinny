"""Assumed shared-contract values used by the learning store.

``docs/contracts.md`` did not exist when this package was written, so every
geometry / crop convention the store depends on is isolated here. When the
lead publishes the shared contract, reconcile these values (and bump
``CROP_SPEC_VERSION`` if crop geometry changes) instead of editing the store.

Canonical geometry (assumed):
  * Units are canonical page units (PDF points, 1/72 inch) of the canonical
    page, independent of render DPI.
  * Origin is the top-left corner of the canonical page; +x right, +y down.
  * Boxes are ``(x0, y0, x1, y1)`` with ``x0 < x1`` and ``y0 < y1``.
  * A pin is a point ``(x, y)``; machine detections also carry a box.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Optional, Tuple

Box = Tuple[float, float, float, float]

STORE_SCHEMA_VERSION = 1
EXPORT_SCHEMA = "pinny.learning.export"
EXPORT_SCHEMA_VERSION = 1

CROP_SPEC_VERSION = 1
# Side length of the square crop around a manual pin, in canonical units.
MANUAL_CROP_SIZE = 48.0
# Context added on every side of a machine detection box, in canonical units.
DETECTION_CROP_MARGIN = 8.0
# Resolution crops are rasterised at.
CROP_RENDER_DPI = 200.0
# Geometry is rounded to this many decimals before hashing/storing so that
# float noise cannot produce distinct crops for the same pin.
GEOMETRY_DECIMALS = 3


def _r(v: float) -> float:
    return round(float(v), GEOMETRY_DECIMALS) + 0.0  # +0.0 folds -0.0


def normalize_box(box: Box) -> Box:
    x0, y0, x1, y1 = (_r(v) for v in box)
    if not (x1 > x0 and y1 > y0):
        raise ValueError(f"degenerate box {box!r}")
    return (x0, y0, x1, y1)


def normalize_point(x: float, y: float) -> Tuple[float, float]:
    return (_r(x), _r(y))


@dataclass(frozen=True)
class CropSpec:
    """Everything needed to reproduce one crop from the source page."""

    spec_version: int
    document_version_id: str
    canonical_page_id: str
    page_width: float
    page_height: float
    unclipped_box: Box
    box: Box  # unclipped_box intersected with the page
    clipped_left: bool
    clipped_top: bool
    clipped_right: bool
    clipped_bottom: bool
    dpi: float
    pixel_box: Tuple[int, int, int, int]  # half-open, in rendered page pixels
    method: str  # "manual_point_square" | "detection_box_margin"

    @property
    def clipped(self) -> bool:
        return self.clipped_left or self.clipped_top or self.clipped_right or self.clipped_bottom

    def to_dict(self) -> dict:
        d = asdict(self)
        d["unclipped_box"] = list(self.unclipped_box)
        d["box"] = list(self.box)
        d["pixel_box"] = list(self.pixel_box)
        d["clipped"] = self.clipped
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CropSpec":
        d = dict(d)
        d.pop("clipped", None)
        d["unclipped_box"] = tuple(d["unclipped_box"])
        d["box"] = tuple(d["box"])
        d["pixel_box"] = tuple(d["pixel_box"])
        return cls(**d)


def _clip(unclipped: Box, page_w: float, page_h: float):
    ux0, uy0, ux1, uy1 = unclipped
    x0, y0 = max(ux0, 0.0), max(uy0, 0.0)
    x1, y1 = min(ux1, page_w), min(uy1, page_h)
    if not (x1 > x0 and y1 > y0):
        raise ValueError(f"crop {unclipped!r} lies outside page {page_w}x{page_h}")
    flags = (ux0 < 0.0, uy0 < 0.0, ux1 > page_w, uy1 > page_h)
    return (_r(x0), _r(y0), _r(x1), _r(y1)), flags


def _pixel_box(box: Box, dpi: float, page_w: float, page_h: float) -> Tuple[int, int, int, int]:
    s = dpi / 72.0
    max_px_w = math.ceil(_r(page_w * s))
    max_px_h = math.ceil(_r(page_h * s))
    # Round to GEOMETRY_DECIMALS before floor/ceil so e.g. 99.99999 -> 100.
    px0 = max(0, math.floor(_r(box[0] * s)))
    py0 = max(0, math.floor(_r(box[1] * s)))
    px1 = min(max_px_w, math.ceil(_r(box[2] * s)))
    py1 = min(max_px_h, math.ceil(_r(box[3] * s)))
    return (px0, py0, px1, py1)


def _build(document_version_id: str, canonical_page_id: str, page_w: float, page_h: float,
           unclipped: Box, dpi: float, method: str) -> CropSpec:
    page_w, page_h = _r(page_w), _r(page_h)
    if page_w <= 0 or page_h <= 0:
        raise ValueError("page dimensions must be positive")
    unclipped = tuple(_r(v) for v in unclipped)  # type: ignore[assignment]
    box, (cl, ct, cr, cb) = _clip(unclipped, page_w, page_h)
    return CropSpec(
        spec_version=CROP_SPEC_VERSION,
        document_version_id=document_version_id,
        canonical_page_id=canonical_page_id,
        page_width=page_w,
        page_height=page_h,
        unclipped_box=unclipped,  # type: ignore[arg-type]
        box=box,
        clipped_left=cl, clipped_top=ct, clipped_right=cr, clipped_bottom=cb,
        dpi=float(dpi),
        pixel_box=_pixel_box(box, dpi, page_w, page_h),
        method=method,
    )


def manual_pin_crop(document_version_id: str, canonical_page_id: str, page_w: float, page_h: float,
                    x: float, y: float, size: float = MANUAL_CROP_SIZE,
                    dpi: float = CROP_RENDER_DPI) -> CropSpec:
    """Square of side ``size`` centred on the pin, clipped to the page."""
    x, y = normalize_point(x, y)
    h = size / 2.0
    return _build(document_version_id, canonical_page_id, page_w, page_h,
                  (x - h, y - h, x + h, y + h), dpi, "manual_point_square")


def detection_crop(document_version_id: str, canonical_page_id: str, page_w: float, page_h: float,
                   box: Box, margin: float = DETECTION_CROP_MARGIN,
                   dpi: float = CROP_RENDER_DPI) -> CropSpec:
    """Detection box expanded by ``margin`` on every side, clipped to the page."""
    x0, y0, x1, y1 = normalize_box(box)
    return _build(document_version_id, canonical_page_id, page_w, page_h,
                  (x0 - margin, y0 - margin, x1 + margin, y1 + margin), dpi, "detection_box_margin")


def pin_crop(document_version_id: str, canonical_page_id: str, page_w: float, page_h: float,
             x: float, y: float, box: Optional[Box]) -> CropSpec:
    if box is not None:
        return detection_crop(document_version_id, canonical_page_id, page_w, page_h, box)
    return manual_pin_crop(document_version_id, canonical_page_id, page_w, page_h, x, y)
